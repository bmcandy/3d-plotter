"""The simulator must behave like Marlin's serial receiver, so sender
desync fails in CI instead of on the printer (#26)."""
import re

from app.simulator import FakeSerial


def checksummed(n: int, cmd: str) -> bytes:
    payload = f"N{n} {cmd}"
    cs = 0
    for ch in payload:
        cs ^= ord(ch)
    return f"{payload}*{cs}\n".encode()


def test_valid_numbered_line_executes_and_advances_the_count():
    port = FakeSerial()
    port.open()
    port.write(checksummed(1, "G28"))
    assert port.written == ["G28"]
    assert port.readline() == "ok"
    port.write(checksummed(2, "G28"))
    assert port.written == ["G28", "G28"]


def test_out_of_order_line_is_rejected_with_marlins_error_trio():
    port = FakeSerial()
    port.open()
    port.write(checksummed(5, "G28"))  # expected 1
    assert port.written == []  # not executed
    assert port.readline() == "Error:Line Number is not Last Line Number+1, Last Line: 0"
    assert port.readline() == "Resend: 1"
    assert port.readline() == "ok"


def test_bad_checksum_is_rejected_with_marlins_error_trio():
    port = FakeSerial()
    port.open()
    port.write(b"N1 G28*0\n")  # wrong checksum
    assert port.written == []
    assert port.readline() == "Error:checksum mismatch, Last Line: 0"
    assert port.readline() == "Resend: 1"
    assert port.readline() == "ok"


def test_checksum_without_line_number_is_rejected():
    """Wire noise can strip a line's leading bytes: a checksummed line
    with no parseable N number gets Marlin's 'No Line Number with
    checksum' trio and does not execute."""
    port = FakeSerial()
    port.open()
    port.write(b"G28*97\n")
    assert port.written == []
    assert port.readline() == "Error:No Line Number with checksum, Last Line: 0"
    assert port.readline() == "Resend: 1"
    assert port.readline() == "ok"


def test_line_number_without_checksum_is_rejected():
    """A numbered line with no checksum gets Marlin's 'No Checksum with
    line number' trio and does not execute."""
    port = FakeSerial()
    port.open()
    port.write(b"N1 G28\n")
    assert port.written == []
    assert port.readline() == "Error:No Checksum with line number, Last Line: 0"
    assert port.readline() == "Resend: 1"
    assert port.readline() == "ok"


def test_open_reboots_the_printer():
    """Opening the port reboots the board: line numbering, pending output
    and one-off glitches all reset, so a reconnect starts clean."""
    port = FakeSerial()
    port.open()
    port.write(checksummed(1, "G28"))
    assert port.readline() == "ok"
    port.close()
    port.open()
    port.write(checksummed(1, "G28"))  # accepted again: the count reset
    assert port.written == ["G28", "G28"]


def test_overlong_line_is_truncated_and_rejected():
    """Marlin truncates lines at MAX_CMD_SIZE; the truncation cuts off
    the checksum, so the line is rejected and not executed."""
    port = FakeSerial()
    port.open()
    long_cmd = "G1 " + "X9" * 60  # well over 96 bytes
    port.write(checksummed(1, long_cmd))
    assert port.written == []
    assert port.readline().startswith("Error:No Checksum with line number")


def test_rx_overflow_drops_bytes_silently():
    """A UART drops bytes silently when the RX buffer fills: the line
    arrives truncated and is rejected — pipelining more than the buffer
    holds is detectable, not absorbed."""
    port = FakeSerial(rx_buffer_size=20)
    port.open()
    port.write(checksummed(1, "G1 X1 Y1"))
    port.write(checksummed(2, "G1 X2 Y2"))  # does not fit: tail dropped
    assert port.readline() == "ok"
    assert port.readline().startswith("Error:")


def test_m110_sets_the_expected_line_number():
    port = FakeSerial()
    port.open()
    port.write(b"M110 N7\n")
    assert port.readline() == "ok"
    port.write(checksummed(8, "G28"))  # expected 8 after M110 N7
    assert port.readline() == "ok"
    assert port.written == ["M110 N7", "G28"]
    port.write(checksummed(1, "G28"))  # stale number: rejected
    assert port.readline().startswith("Error:Line Number is not")


def test_unnumbered_lines_do_not_move_the_count():
    port = FakeSerial()
    port.open()
    port.write(b"M105\n")
    assert port.readline() == "ok"
    port.write(checksummed(1, "G28"))  # still expected: 1
    assert port.written == ["M105", "G28"]


def test_boot_drops_writes_until_the_printer_is_up():
    port = FakeSerial(boot_drops=2)
    port.open()
    port.write(b"M105\n")  # dropped during boot
    port.write(b"M105\n")  # dropped during boot
    assert port.written == [] and not port._pending
    port.write(b"M105\n")  # printer is up
    assert port.readline() == "ok"
