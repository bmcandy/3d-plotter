"""Minimal Marlin sender: stream G-code lines, wait for ok."""
from __future__ import annotations

import logging
import time

log = logging.getLogger("crealipen.serial")


class SenderError(RuntimeError):
    pass


class MarlinSender:
    def __init__(self, port, timeout: float = 30.0, boot_wait: float = 30.0):
        self.port = port
        self.timeout = timeout
        self.boot_wait = boot_wait

    def connect(self) -> None:
        if not self.port.is_open:
            self.port.open()
        self._wait_for_printer()

    def _wait_for_printer(self) -> None:
        """Opening the port resets Arduino-based boards (DTR pulse); Marlin
        boots for a few seconds and drops commands sent during boot. Poll
        M105 until an ok comes back, tolerating boot chatter."""
        deadline = time.monotonic() + self.boot_wait
        while time.monotonic() < deadline:
            try:
                self._write("M105")
                response = self._readline(timeout=2.0)
                if response.startswith("ok"):
                    return
                # boot chatter or a lost response: poll again
            except SenderError:
                pass
        raise SenderError("printer did not respond after connect (is it powered on?)")

    def close(self) -> None:
        self.port.close()

    def _write(self, line: str) -> None:
        log.info(">> %s", line)
        self.port.write((line + "\n").encode())

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
        """Send a command and wait for ok. On a silent timeout the command
        is resent: a lost ok (serial corruption) must not fail the job, and
        move commands are idempotent for a pen plotter. The machine is
        never halted by this sender — a silent printer just fails the job."""
        for attempt in range(retries + 1):
            self._write(line)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    response = self._readline(timeout=2.0)
                except SenderError:
                    continue  # silence; keep waiting until the deadline
                if response.startswith("ok"):
                    return
                if response.startswith("Error"):
                    raise SenderError(f"printer error on {line!r}: {response}")
                # "echo:busy ..." and other chatter: keep waiting; each
                # received line proves liveness, so extend the deadline —
                # long homing sequences emit busy lines for well over 30s
                deadline = time.monotonic() + self.timeout
        raise SenderError(
            f"printer stopped responding on {line!r} after {retries + 1} attempts"
        )

    def run_gcode(self, lines: list[str], on_progress, stop, safe_z: float = 5.0) -> None:
        """Stream lines. `stop()` is polled between lines; when it returns
        True the stream halts and the pen is lifted (safe Z)."""
        total = len(lines)
        for i, line in enumerate(lines):
            if stop():
                self.send_line(f"G0 Z{safe_z:g}")  # lift pen
                return
            self.send_line(line)
            on_progress(i + 1, total)
