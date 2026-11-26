"""Hardware-in-the-loop tests: run against a real printer.

Skipped unless the CREALIPEN_HARDWARE_PORT environment variable names the
serial port of a connected, powered-on printer:

    CREALIPEN_HARDWARE_PORT=/dev/creality python -m pytest tests/test_hardware.py -v

The smoke plot keeps the pen up (dry run): it exercises the full serial
protocol — handshake, numbering, checksums, pipelining, recovery — without
touching the paper. The simulator cannot be a true oracle for Marlin's
firmware (the checksum algorithm and wire noise are mirrored, not
independent), so these tests are the final gate before a release.
"""
import os
import time

import pytest

from app.sender import MarlinSender, SenderError

PORT = os.environ.get("CREALIPEN_HARDWARE_PORT")

pytestmark = pytest.mark.skipif(
    not PORT, reason="set CREALIPEN_HARDWARE_PORT to run against a real printer"
)


def real_sender() -> MarlinSender:
    import serial

    return MarlinSender(serial.Serial(PORT, 115200, timeout=1))


def test_connects_and_handshakes():
    sender = real_sender()
    sender.connect()  # must handshake through the boot window
    sender.close()


def test_numbered_lines_are_accepted():
    """The printer must accept consecutively numbered, checksummed lines:
    a numbering or checksum disagreement fails here, not on a plot."""
    sender = real_sender()
    sender.connect()
    try:
        sender.send_line("G21")  # mm units
        sender.send_line("G90")  # absolute positioning
    finally:
        sender.close()


def test_smoke_plot_completes():
    """A dry-run plot: home, lift the pen clear of the paper, and stream a
    few hundred moves. Exercises pipelining and the recovery paths against
    real wire noise; the pen never touches the paper."""
    sender = real_sender()
    sender.connect()
    try:
        lines = ["G21", "G90", "G28"]
        lines += [f"G0 Z10 F600"]  # pen well clear
        lines += [f"G0 X{i % 100} Y{i % 100} F3000" for i in range(300)]
        lines += ["G0 X0 Y0 F3000", "M84"]
        progress = []
        sender.run_gcode(
            lines,
            on_progress=lambda i, n: progress.append((i, n)),
            stop=lambda: False,
            safe_z=10.0,
        )
        assert progress[-1] == (len(lines), len(lines))
    finally:
        sender.close()
