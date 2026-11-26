"""Fake serial port for development and tests: emulates Marlin's line protocol."""
from __future__ import annotations

import re

_NUMBERED = re.compile(r"^N(\d+) (.*)\*([0-9]+)$")

# Marlin truncates received lines at MAX_CMD_SIZE; the default is 96 bytes
MAX_CMD_SIZE = 96
# Marlin's default serial RX buffer size in bytes; bytes beyond it are
# dropped silently by the UART, exactly as on real hardware
RX_BUFFER_SIZE = 128


class FakeSerial:
    """Emulates Marlin's serial receiver faithfully enough that sender
    desync fails in CI instead of on the printer (#26):

    - numbered lines `N<n> <cmd>*<checksum>` must arrive in order
      (`n == last + 1`) with a valid checksum; anything else is not
      executed and yields Marlin's error trio: the `Error:` line, a
      `Resend: <last+1>` request, and `ok`;
    - a checksum with no parseable line number and a line number with no
      checksum are each rejected with Marlin's wording for them;
    - `M110 N<n>` sets the expected line number;
    - unnumbered lines are accepted without moving the count;
    - `open()` reboots the board: line numbering and pending output reset;
    - `boot_drops` simulates the DTR-reset boot window: the first that
      many writes are silently discarded, as on a real board;
    - lines longer than MAX_CMD_SIZE are truncated before parsing, as
      Marlin does, so over-long lines fail their checksum;
    - the RX buffer holds RX_BUFFER_SIZE bytes between host reads; a
      write that does not fit loses its tail silently, like a real UART
      overflow — pipelining more than the buffer holds is detectable.

    Known limits (cannot be fixed by simulation): the checksum algorithm
    mirrors the sender's rather than independently verifying it (it is
    hardware-validated), and the wire is byte-clean unless a test mangles
    writes — noise shapes beyond the classified ones are not modeled.

    `responses` maps a bare command (wrapper stripped) to a response
    string or a list consumed in order; default is "ok". A list is served
    on the command's first execution only — later executions get the
    default — so a one-off glitch (a refused command that succeeds on
    retry) can be modeled. `written` records executed commands;
    `raw_written` keeps the bytes on the wire.
    """

    def __init__(self, responses: dict[str, str | list[str]] | None = None,
                 delay: float = 0.0, boot_drops: int = 0,
                 rx_buffer_size: int = RX_BUFFER_SIZE):
        self.responses = responses or {}
        self.delay = delay
        self.boot_drops = 0
        self._boot_drops_config = boot_drops
        self.rx_buffer_size = rx_buffer_size
        self.last_line = 0
        self.written: list[str] = []
        self.raw_written: list[bytes] = []
        self._pending: list[str] = []
        self._served_lists: set[str] = set()
        self._rx_budget = rx_buffer_size
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        # opening the port pulses DTR and reboots the board: everything
        # the printer knew is gone, and the boot window starts over
        self._open = True
        self.last_line = 0
        self.boot_drops = self._boot_drops_config
        self._pending.clear()
        self._served_lists.clear()
        self._rx_budget = self.rx_buffer_size

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
        # the UART drops bytes silently when the RX buffer is full: the
        # tail of the line is lost and the line will fail its checksum
        if len(line) > self._rx_budget:
            line = line[:self._rx_budget]
        self._rx_budget -= len(line)
        line = line[:MAX_CMD_SIZE]  # Marlin truncates over-long lines
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
        # the printer's main loop ran: its RX buffer drained
        self._rx_budget = self.rx_buffer_size
        return self._pending.pop(0) if self._pending else ""
