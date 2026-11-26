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
        ready = False
        while time.monotonic() < deadline and not ready:
            self._write_plain("M105")
            probe_deadline = min(deadline, time.monotonic() + 2.0)
            while time.monotonic() < probe_deadline:
                try:
                    response = self._readline(
                        timeout=min(0.5, probe_deadline - time.monotonic())
                    )
                except SenderError:
                    continue
                if response.startswith("ok"):
                    ready = True
                    break
                # Consume boot chatter without queuing another M105 behind it.
        if not ready:
            raise SenderError("printer did not respond after connect (is it powered on?)")
        self._write_plain("M110 N0")
        for attempt in range(3):
            try:
                response = self._readline()
            except SenderError:
                if attempt == 2:
                    raise
                self._write_plain("M110 N0")  # lost ok: try again
                continue
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

    def send_line(self, line: str, retries: int = 2, required: bool = True) -> None:
        """Send one command and wait for its ok (no pipelining). A command
        the printer refuses (echo:Unknown command) is resent with a fresh
        line number: Marlin consumed the number without executing it, and
        a dropped pen-state command draws stray lines (#21). Optional
        commands (required=False) are dropped after a single refusal —
        firmware without them must not fail the connect."""
        self._write(line)
        for attempt in range(retries + 1):
            outcome = self._await_ok(line)
            if outcome == "ok":
                return
            if isinstance(outcome, int) and outcome > self._seq:
                return  # the printer already executed it
            if outcome == "rejected" and not required:
                log.info("printer refused optional command %r", line)
                return
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

        Reliability model: the printer's `Resend: N` is the position
        oracle — it has executed exactly the lines below N — so on a
        resend the counter is rewound to N (correcting any ok that a
        rejection trio misattributed) and lines N onward are re-sent with
        their original numbers, as OctoPrint does. Re-execution is
        idempotent for a pen plotter; dropping a line is not (#17, #21).
        A silent printer fails the job cleanly if it never answers."""
        total = len(lines)
        queue: deque[tuple[str, int]] = deque((line, 0) for line in lines)
        sent: deque[tuple[int, str, int]] = deque()  # (line number, command, refusals)
        history: deque[tuple[int, str]] = deque(maxlen=self.window * 4)
        done = 0
        halted = False
        holding = False  # a refused command awaits its retry; hold fresh lines
        timeouts = 0

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
                history.append((seq, line))
                done += 1
                timeouts = 0
                on_progress(done, total)
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
                if timeouts > 5:
                    raise SenderError(f"printer stopped responding on {line!r}")
                self._write_numbered(seq, line)
            else:  # the printer asked for line `outcome`
                # the backlog holds stale rejection trios from earlier
                # cycles; drain it and act on the freshest position only
                fresh = self._drain_to_quiet()
                position = fresh if fresh is not None else outcome
                if position <= done - len(history):
                    # the request is older than the resend buffer: either
                    # the request itself was corrupted in transit, or the
                    # printer rebooted. Interrogate the true position.
                    position = self._probe_position()
                    if position is None or position <= done - len(history):
                        raise SenderError(
                            f"line number desync at {line!r}: printer"
                            f" requested resend of {outcome}; it may have"
                            " restarted mid-plot — re-home and re-plot"
                        )
                # the printer's position is ground truth: correct the
                # counter and re-send everything from that line — both
                # lines it already confirmed (within the resend buffer)
                # and the unconfirmed window behind it
                while sent and sent[0][0] < position:
                    history.append(sent.popleft())
                requeue = []
                while history and history[-1][0] >= position:
                    requeue.append(history.pop())
                requeue.reverse()
                requeue.extend((s, l) for s, l, _r in sent)
                sent.clear()
                done = position - 1
                on_progress(done, total)
                for s, l in requeue:
                    self._write_numbered(s, l)
                    sent.append((s, l, 0))
                timeouts = 0

    def _probe_position(self) -> int | None:
        """Interrogate the printer's true line position: a line that
        deliberately fails its checksum makes Marlin answer with the
        position in its error trio. The probe's answer travels over the
        same noisy wire, so two consecutive agreeing probes are required;
        None means the printer would not give a trustworthy answer."""
        agreed = None
        for _ in range(4):
            self._write_plain("x*0")  # checksum without a line number
            try:
                position = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        response = self._readline(timeout=2.0)
                    except SenderError:
                        continue
                    if response.startswith("Resend:"):
                        try:
                            position = int(response.split(":", 1)[1].strip())
                        except ValueError:
                            position = None
                        break
                    if response.startswith("ok"):
                        continue  # ack of the refused line; keep waiting
                if position is None:
                    return agreed if agreed is not None else None
                if position == agreed:
                    return position  # two probes agree
                agreed = position
            except SenderError:
                return None
        return None

    def _drain_to_quiet(self) -> int | None:
        """Consume every buffered response — stale rejection trios and
        misattributed oks alike — and return the freshest `Resend:`
        position seen, so the sender acts on the printer's current state
        instead of a stale rejection. The printer's line counter only
        grows (a reboot surfaces as silence, not as a stale trio), so the
        freshest position is the trustworthy one."""
        last = None
        quiet = 0
        deadline = time.monotonic() + 2.0
        while quiet < 3 and time.monotonic() < deadline:
            try:
                response = self._readline(timeout=0.3)
            except SenderError:
                break  # quiet
            quiet = 0
            if response.startswith("Resend:"):
                try:
                    last = int(response.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return last

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
