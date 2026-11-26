"""Fake serial port for development and tests: emulates Marlin's line protocol."""
from __future__ import annotations

import re

_NUMBERED = re.compile(r"^N(\d+) (.*)\*([0-9]+)$")


class FakeSerial:
    """Emulates Marlin's serial receiver faithfully enough that sender
    desync fails in CI instead of on the printer (#26):

    - numbered lines `N<n> <cmd>*<checksum>` must arrive in order
      (`n == last + 1`) with a valid checksum; anything else is not
      executed and yields Marlin's error trio: the `Error:` line, a
      `Resend: <last+1>` request, and `ok`;
    - `M110 N<n>` sets the expected line number;
    - unnumbered lines are accepted without moving the count;
    - `boot_drops` simulates the DTR-reset boot window: the first that
      many writes are silently discarded, as on a real board.

    `responses` maps a bare command (wrapper stripped) to a response
    string or a list consumed in order; default is "ok". A list is served
    on the command's first execution only — later executions get the
    default — so a one-off glitch (a refused command that succeeds on
    retry) can be modeled. `written` records executed commands;
    `raw_written` keeps the bytes on the wire.
    """

    def __init__(self, responses: dict[str, str | list[str]] | None = None,
                 delay: float = 0.0, boot_drops: int = 0):
        self.responses = responses or {}
        self.delay = delay
        self.boot_drops = boot_drops
        self.last_line = 0
        self.written: list[str] = []
        self.raw_written: list[bytes] = []
        self._pending: list[str] = []
        self._served_lists: set[str] = set()
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _reject(self, error: str) -> None:
        self._pending.extend([
            error,
            f"Resend: {self.last_line + 1}",
            "ok",
        ])

    def write(self, data: bytes | str) -> None:
        if not self._open:
            raise IOError("port not open")
        if isinstance(data, bytes):
            self.raw_written.append(data)
            data = data.decode("utf-8")
        line = data.strip()
        if self.boot_drops > 0:
            self.boot_drops -= 1  # dropped during the reset boot window
            return
        m = _NUMBERED.match(line)
        if m:
            n, cmd, cs = int(m.group(1)), m.group(2), m.group(3)
            expected = 0
            for ch in line[:line.index("*")]:
                expected ^= ord(ch)
            if str(expected) != cs:
                self._reject(f"Error:checksum mismatch, Last Line: {self.last_line}")
                return
            if n != self.last_line + 1:
                self._reject(
                    "Error:Line Number is not Last Line Number+1,"
                    f" Last Line: {self.last_line}"
                )
                return
            self.last_line = n
            line = cmd
        elif "*" in line:
            # a checksum with no parseable line number: wire noise mangled
            # the line's leading bytes
            self._reject(f"Error:No Line Number with checksum, Last Line: {self.last_line}")
            return
        elif re.match(r"N\d+", line):
            # a line number with no checksum
            self._reject(f"Error:No Checksum with line number, Last Line: {self.last_line}")
            return
        elif line.startswith("M110"):
            m110 = re.search(r"N(\d+)", line)
            if m110:
                self.last_line = int(m110.group(1))
        self.written.append(line)
        response = self.responses.get(line, "ok")
        if isinstance(response, list):
            if line in self._served_lists:
                response = "ok"
            else:
                self._served_lists.add(line)
        if isinstance(response, str):
            self._pending.append(response)
        else:
            self._pending.extend(response)

    def readline(self) -> str:
        if self.delay:
            import time

            time.sleep(self.delay)
        return self._pending.pop(0) if self._pending else ""
