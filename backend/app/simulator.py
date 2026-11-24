"""Fake serial port for development and tests: fakes Marlin `ok` responses."""
from __future__ import annotations


class FakeSerial:
    """Implements the serial-port surface MarlinSender needs.

    `responses` maps an exact written line to either a response string or a
    list of response strings (consumed in order); default is "ok".
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
