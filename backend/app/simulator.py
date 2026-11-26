"""Fake serial port for development and tests: emulates Marlin's line protocol."""
from __future__ import annotations

import re

_NUMBERED = re.compile(r"^N(\d+) (.*)\*([0-9]+)$")


class FakeSerial:
    """Implements the serial-port surface MarlinSender needs, emulating
    Marlin's line protocol: `N<n> <cmd>*<checksum>` wrappers are validated
    (a bad checksum yields a `Resend:` request and the command does not
    run); unnumbered lines are accepted as-is. `responses` keys on the
    bare command (wrapper stripped), as does `written`; `raw_written`
    keeps the bytes on the wire.

    `responses` maps a bare command to either a response string or a list
    of response strings (consumed in order); default is "ok".
    """

    def __init__(self, responses: dict[str, str | list[str]] | None = None, delay: float = 0.0):
        self.responses = responses or {}
        self.delay = delay
        self.written: list[str] = []
        self.raw_written: list[bytes] = []
        self._pending: list[str] = []
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes | str) -> None:
        if not self._open:
            raise IOError("port not open")
        if isinstance(data, bytes):
            self.raw_written.append(data)
            data = data.decode("utf-8")
        line = data.strip()
        m = _NUMBERED.match(line)
        if m:
            expected = 0
            for ch in line[:line.index("*")]:
                expected ^= ord(ch)
            if str(expected) != m.group(3):
                self._pending.append(f"Resend:{m.group(1)}")
                return
            line = m.group(2)
        self.written.append(line)
        response = self.responses.get(line, "ok")
        if isinstance(response, str):
            self._pending.append(response)
        else:
            self._pending.extend(response)

    def readline(self) -> str:
        if self.delay:
            import time

            time.sleep(self.delay)
        return self._pending.pop(0) if self._pending else ""
