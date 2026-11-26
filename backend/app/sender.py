"""Minimal Marlin sender: stream G-code lines, wait for ok.

Lines are sent with Marlin line numbers and checksums (`N<n> <cmd>*<cs>`),
so a corrupted command is caught by the printer and requested again
(`Resend:`) instead of being silently dropped (#17, #21).
"""
from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger("crealipen.serial")


class SenderError(RuntimeError):
    pass


class MarlinSender:
    def __init__(self, port, timeout: float = 30.0, boot_wait: float = 30.0,
                 window: int = 4):
        self.port = port
        self.timeout = timeout
        self.boot_wait = boot_wait
        # lines allowed in flight before an ok must be consumed; Marlin's RX
        # buffer is small, so stay well inside it
        self.window = window
        self._seq = 0  # last Marlin line number sent

    def connect(self) -> None:
        if not self.port.is_open:
            self.port.open()
        self._wait_for_printer()

    def _wait_for_printer(self) -> None:
        """Opening the port resets Arduino-based boards (DTR pulse); Marlin
        boots for a few seconds and drops commands sent during boot. Poll
        M105 until an ok comes back, tolerating boot chatter. The probes
        are unnumbered — the freshly reset printer expects line 1 and
        cannot reconcile numbered probes it may have missed. Once the
        printer answers, M110 N0 aligns line numbering explicitly (it also
        covers reconnecting to a board that did not reset) and counting
        restarts at 1."""
        deadline = time.monotonic() + self.boot_wait
        while time.monotonic() < deadline:
            try:
                self._write_plain("M105")
                response = self._readline(timeout=2.0)
                if response.startswith("ok"):
                    break
                # boot chatter or a lost response: poll again
            except SenderError:
                pass
        else:
            raise SenderError("printer did not respond after connect (is it powered on?)")
        self._write_plain("M110 N0")
        while True:
            response = self._readline()
            if response.startswith("ok"):
                break
            if response.startswith("Error"):
                raise SenderError(f"printer error on M110 N0: {response}")
        self._seq = 0

    def close(self) -> None:
        self.port.close()

    @staticmethod
    def _checksum(payload: str) -> int:
        cs = 0
        for ch in payload:
            cs ^= ord(ch)
        return cs

    def _write_plain(self, line: str) -> None:
        log.info(">> %s", line)
        self.port.write((line + "\n").encode())

    def _write(self, line: str) -> None:
        self._seq += 1
        self._write_numbered(self._seq, line)

    def _write_numbered(self, seq: int, line: str) -> None:
        payload = f"N{seq} {line}"
        log.info(">> %s", line)
        self.port.write(f"{payload}*{self._checksum(payload)}\n".encode())

    def _readline(self, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while time.monotonic() < deadline:
            line = self.port.readline()
            if line:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.strip()
                if line:
                    log.info("<< %s", line)
                    return line
        raise SenderError("timeout waiting for printer response")

    def send_line(self, line: str, retries: int = 2) -> None:
        """Send one command and wait for its ok (no pipelining). A command
        the printer refuses (echo:Unknown command) is resent with a fresh
        line number: Marlin consumed the number without executing it, and a
        dropped pen-state command draws stray lines (#21)."""
        for attempt in range(retries + 1):
            self._write(line)
            if self._await_ok(self._seq, line, retries) == "ok":
                return
        raise SenderError(f"printer refused {line!r} after {retries + 1} attempts")

    def run_gcode(self, lines: list[str], on_progress, stop, safe_z: float = 5.0) -> None:
        """Stream lines with a window of in-flight commands so dense plots
        are not latency-bound by one-ok-per-line round trips. `stop()` is
        polled between lines; when it returns True the stream stops feeding
        new lines, drains the in-flight ones, and lifts the pen (safe Z).

        A refused command (echo:Unknown command) is re-queued at the front
        and no fresh lines are fed until it has run, so later moves never
        depend on a command that did not execute."""
        total = len(lines)
        queue: deque[tuple[str, int]] = deque((line, 0) for line in lines)
        inflight: deque[tuple[int, str, int]] = deque()  # (seq, line, refusals)
        done = 0
        halted = False
        holding = False  # a refused command awaits its retry; hold fresh lines
        while done < total or (halted and inflight):
            if not halted and stop():
                halted = True
            while (not halted and len(inflight) < self.window and queue
                   and not (holding and inflight)):
                line, refusals = queue.popleft()
                self._write(line)
                inflight.append((self._seq, line, refusals))
            if halted and not inflight:
                self.send_line(f"G0 Z{safe_z:g}")  # lift pen
                return
            seq, line, refusals = inflight[0]
            outcome = self._await_ok(seq, line)
            inflight.popleft()
            if outcome == "ok":
                done += 1
                holding = False
                on_progress(done, total)
            elif refusals >= 2:
                raise SenderError(f"printer refused {line!r} after {refusals + 1} attempts")
            else:
                queue.appendleft((line, refusals + 1))
                holding = True

    def _await_ok(self, seq: int, line: str, retries: int = 2) -> str:
        """Wait for the ok of the in-flight line `seq`. On a silent timeout
        or the printer's `Resend:` request the line is rewritten with its
        original number, keeping the printer's expected-number count
        aligned. Returns "ok", or "rejected" when Marlin refused the
        command (echo:Unknown command)."""
        for attempt in range(retries + 1):
            if attempt:
                log.warning("resending %r as line %d", line, seq)
                self._write_numbered(seq, line)
            rejected = False
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    response = self._readline(timeout=2.0)
                except SenderError:
                    continue  # silence; keep waiting until the deadline
                if response.startswith("ok"):
                    return "rejected" if rejected else "ok"
                if response.startswith("Error"):
                    raise SenderError(f"printer error on {line!r}: {response}")
                if response.startswith("Resend:"):
                    break  # rewrite with the same number on the next attempt
                if "Unknown command" in response:
                    rejected = True
                    continue
                # "echo:busy ..." and other chatter: keep waiting; each
                # received line proves liveness, so extend the deadline —
                # long homing sequences emit busy lines for well over 30s
                deadline = time.monotonic() + self.timeout
        raise SenderError(
            f"printer stopped responding on {line!r} after {retries + 1} attempts"
        )
