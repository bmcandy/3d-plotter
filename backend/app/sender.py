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
        line number: Marlin consumed the number without executing it, and
        a dropped pen-state command draws stray lines (#21)."""
        self._write(line)
        for attempt in range(retries + 1):
            outcome = self._await_ok(line)
            if outcome == "ok":
                return
            if isinstance(outcome, int) and outcome > self._seq:
                return  # the printer already executed it
            if attempt == retries:
                if outcome == "rejected":
                    raise SenderError(
                        f"printer refused {line!r} after {retries + 1} attempts"
                    )
                break
            if outcome == "timeout":
                # same number: the line may or may not have executed
                self._write_numbered(self._seq, line)
            elif isinstance(outcome, int):
                # the printer discarded it and asked for it again by number
                self._write_numbered(self._seq, line)
            else:
                # refused: fresh number, Marlin consumed the old one
                self._write(line)
        raise SenderError(f"printer stopped responding on {line!r}")

    def run_gcode(self, lines: list[str], on_progress, stop, safe_z: float = 5.0) -> None:
        """Stream lines with a window of in-flight commands so dense plots
        are not latency-bound by one-ok-per-line round trips. `stop()` is
        polled between lines; when it returns True the stream stops feeding
        new lines, drains the in-flight ones, and lifts the pen (safe Z).

        Reliability model: oks are only trustworthy while the stream is
        clean — a rejection trio carries an ok of its own, so a stale ok
        can be misattributed to a line that never executed. So on any
        `Resend:` (a rejected or corrupted line somewhere in the window)
        the sender resynchronizes: numbering is reset with M110 N0, the
        link is drained to quiet, and every unconfirmed line is re-sent
        with clean one-ok-per-line accounting. Moves and pen-state
        commands are idempotent, so re-execution is safe; dropping one is
        not (#17, #21)."""
        total = len(lines)
        queue: deque[tuple[str, int]] = deque((line, 0) for line in lines)
        sent: deque[tuple[int, str, int]] = deque()  # (line number, command, refusals)
        done = 0
        halted = False
        holding = False  # a refused command awaits its retry; hold fresh lines
        timeouts = 0
        resyncs = 0

        def confirm(count: int) -> None:
            nonlocal done
            done += count
            on_progress(done, total)

        while done < total or (halted and sent):
            if not halted and stop():
                halted = True
            while (not halted and len(sent) < self.window and queue
                   and (not holding or not sent)):
                line, refusals = queue.popleft()
                self._write(line)
                sent.append((self._seq, line, refusals))
            if halted and not sent:
                self.send_line(f"G0 Z{safe_z:g}")  # lift pen
                return
            seq, line, refusals = sent[0]
            outcome = self._await_ok(line)
            if outcome == "ok":
                sent.popleft()
                confirm(1)
                if not sent:
                    holding = False
            elif outcome == "rejected":
                # Marlin consumed the line number without executing the
                # command: run it again (fresh number) once the window
                # drains, so nothing depends on a command that never ran
                sent.popleft()
                if refusals >= 2:
                    raise SenderError(
                        f"printer refused {line!r} after {refusals + 1} attempts"
                    )
                queue.appendleft((line, refusals + 1))
                holding = True
            elif outcome == "timeout":
                timeouts += 1
                if timeouts > 2:
                    raise SenderError(f"printer stopped responding on {line!r}")
                self._write_numbered(seq, line)
            else:
                # a Resend anywhere in the window means the ok stream can
                # no longer be trusted: resynchronize and re-send every
                # unconfirmed line
                resyncs += 1
                if resyncs > 2:
                    raise SenderError(
                        f"line number desync at {line!r}:"
                        f" printer requested resend of {outcome}"
                    )
                log.warning("resync: printer requested resend of %d", outcome)
                self._resync()
                for s, l, r in reversed(sent):
                    queue.appendleft((l, r))
                sent.clear()
                holding = False

    def _resync(self) -> None:
        """Reset line numbering (M110 N0) and drain the link until quiet.
        Every buffered line now fails its number check and is flushed by
        the printer, so the stream empties and re-sent lines get clean
        one-ok-per-line accounting."""
        self._write_plain("M110 N0")
        self._seq = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                self._readline(timeout=0.3)
            except SenderError:
                return  # quiet: nothing more buffered

    def _await_ok(self, line: str) -> str | int:
        """Read responses until the fate of the oldest in-flight line is
        decided. Returns "ok" (an ok was consumed), "rejected" (Marlin
        refused the command: echo:Unknown command before the ok),
        "timeout" (silence for one timeout period), or the line number the
        printer asked to resend — it has executed exactly the lines below
        that number, which is the position oracle for the rewind in
        run_gcode."""
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
                # Marlin's serial-receiver errors (checksum mismatch, line
                # number problems in any wording) are its resend request
                # in error form: the Resend: line follows with the
                # position. Anything else is fatal.
                if ("checksum" in response.lower()
                        or "line number" in response.lower()):
                    continue
                raise SenderError(f"printer error on {line!r}: {response}")
            if response.startswith("Resend:"):
                try:
                    return int(response.split(":", 1)[1].strip())
                except ValueError:
                    return "timeout"
            if "Unknown command" in response:
                rejected = True
                continue
            # "echo:busy ..." and other chatter: keep waiting; each
            # received line proves liveness, so extend the deadline —
            # long homing sequences emit busy lines for well over 30s
            deadline = time.monotonic() + self.timeout
        return "timeout"
