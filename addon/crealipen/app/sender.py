"""Minimal Marlin sender: stream G-code lines, wait for ok."""
from __future__ import annotations

import time


class SenderError(RuntimeError):
    pass


class MarlinSender:
    def __init__(self, port, timeout: float = 30.0):
        self.port = port
        self.timeout = timeout

    def connect(self) -> None:
        if not self.port.is_open:
            self.port.open()

    def close(self) -> None:
        self.port.close()

    def _readline(self) -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            line = self.port.readline()
            if line:
                return line
        raise SenderError("timeout waiting for printer response")

    def send_line(self, line: str) -> None:
        self.port.write(line)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = self._readline()
            if response.startswith("ok"):
                return
            if response.startswith("Error"):
                raise SenderError(f"printer error on {line!r}: {response}")
            # "echo:busy ..." and other chatter: keep waiting
        raise SenderError(f"timeout waiting for ok on {line!r}")

    def run_gcode(self, lines: list[str], on_progress, stop) -> None:
        """Stream lines. `stop()` is polled between lines; when it returns
        True the stream halts and the pen is lifted (safe Z)."""
        total = len(lines)
        for i, line in enumerate(lines):
            if stop():
                self.send_line("G0 Z5")  # lift pen
                return
            self.send_line(line)
            on_progress(i + 1, total)
