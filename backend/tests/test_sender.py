import re

import pytest

from app.sender import MarlinSender, SenderError
from app.simulator import FakeSerial


class CorruptingPort(FakeSerial):
    """Mangles one write's checksum, like the flaky USB-serial adapter
    in #21: the printer must catch it and request a resend."""

    def __init__(self, corrupt_index: int):
        super().__init__()
        self.corrupt_index = corrupt_index
        self.writes = 0

    def write(self, data):
        self.writes += 1
        if self.writes == self.corrupt_index and isinstance(data, bytes):
            star = data.rindex(b"*")
            data = data[:star + 1] + b"0\n"  # wrong checksum
        return super().write(data)


def test_send_line_waits_for_ok():
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G28")
    assert port.written[-1] == "G28"


def test_error_response_raises():
    port = FakeSerial(responses={"G1 X1": "Error:boom"})
    sender = MarlinSender(port)
    sender.connect()
    with pytest.raises(SenderError):
        sender.send_line("G1 X1")


def test_busy_is_waited_out_then_ok():
    port = FakeSerial(responses={"G28": ["echo:busy: processing", "ok"]})
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G28")  # must not raise
    assert port.written[-1] == "G28"


def test_run_gcode_reports_progress_and_completes():
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    progress = []
    lines = [f"G0 X{i}" for i in range(10)]
    sender.run_gcode(lines, on_progress=lambda i, n: progress.append((i, n)), stop=lambda: False)
    assert progress[-1] == (10, 10)
    # the handshake's M105 plus the ten streamed lines
    assert port.written[-10:] == [f"G0 X{i}" for i in range(10)]


def test_failed_connect_marks_job_failed():
    """A missing device must fail the job, not leave it running forever."""
    from app.db import Database
    from app.jobs import JobRunner

    class ExplodingPort:
        is_open = False

        def open(self):
            raise IOError("no such device")

        def close(self):
            pass

    db = Database(":memory:")
    runner = JobRunner(db, lambda: MarlinSender(ExplodingPort()))
    job_id = runner.start(1, ["G28"])
    import time

    for _ in range(50):
        if db.get_job(job_id)["status"] != "running":
            break
        time.sleep(0.02)
    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert "no such device" in job["error"]
    assert not runner.busy()


def test_failed_sender_factory_marks_job_failed_and_frees_machine():
    """A sender factory that raises (e.g. missing serial device) must fail
    the job and release the machine, not wedge it as permanently busy."""
    from app.db import Database
    from app.jobs import JobRunner

    def factory():
        raise IOError("no such device")

    db = Database(":memory:")
    runner = JobRunner(db, factory)
    job_id = runner.start(1, ["G28"])
    import time

    for _ in range(50):
        if db.get_job(job_id)["status"] != "running":
            break
        time.sleep(0.02)
    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert not runner.busy()  # machine must be usable again


def test_connect_handshakes_through_boot_chatter():
    """Opening the port resets Arduino boards; Marlin boots for a few
    seconds and ignores early commands. connect() must poll until the
    printer answers, tolerating boot chatter."""
    port = FakeSerial(responses={"M105": ["start", "echo:Marlin init", "ok"]})
    sender = MarlinSender(port)
    sender.connect()  # must handshake through the chatter
    sender.send_line("G28")  # and work normally afterwards
    assert port.written[-1] == "G28"


def test_connect_fails_cleanly_when_printer_silent():
    class DeafPort(FakeSerial):
        def readline(self):
            return ""

    sender = MarlinSender(DeafPort(), boot_wait=0.3)
    with pytest.raises(SenderError, match="did not respond"):
        sender.connect()


def test_writes_are_newline_terminated_checksummed_bytes():
    """pyserial requires bytes; Marlin requires newline-terminated commands.
    Lines carry Marlin line numbers and checksums so the printer can detect
    corruption instead of silently dropping commands (#17, #21)."""
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G28")
    raw = port.raw_written[-1]
    assert raw.endswith(b"\n")
    assert re.match(rb"^N\d+ G28\*\d+\n$", raw)


def test_corrupted_line_is_resent_after_resend_request():
    """A duplicated byte on the wire (the CH340 corruption in #21) must be
    caught by the printer's checksum and the line resent — not dropped."""
    port = CorruptingPort(corrupt_index=2)  # first command after the handshake
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G28")
    # the corrupted original plus the resent line hit the wire; only the
    # resent one is recorded as an executed command
    assert len([raw for raw in port.raw_written if b"G28" in raw]) == 2
    assert port.written.count("G28") == 1


def test_unknown_command_is_retried_not_dropped():
    """Marlin acks a refused command with only an echo:Unknown command; a
    dropped pen-lift draws stray lines (#21), so the command is retried
    with a fresh line number until the printer accepts it."""
    port = FakeSerial(responses={
        "G0 Z1.5": ['echo:Unknown command: "GG0 Z1.5"', "ok", "ok"],
    })
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G0 Z1.5")
    assert port.written.count("G0 Z1.5") == 2


def test_unknown_command_exhaustion_fails_cleanly():
    port = FakeSerial(responses={
        "G0 Z1.5": ['echo:Unknown command: "GG0 Z1.5"', "ok"] * 10,
    })
    sender = MarlinSender(port)
    sender.connect()
    with pytest.raises(SenderError, match="refused"):
        sender.send_line("G0 Z1.5")


def test_pipelined_unknown_command_retried_before_fresh_lines():
    """Mid-stream, a refused command is re-queued at the front and no fresh
    lines are fed until it has run: later moves must not depend on a
    pen-state command that never executed."""
    port = FakeSerial(responses={
        "G0 Z1.5": ['echo:Unknown command: "GG0 Z1.5"', "ok", "ok"],
    })
    sender = MarlinSender(port)
    sender.connect()
    port.written.clear()
    sender.run_gcode(
        ["G0 Z1.5", "G0 X10", "G0 X20", "G0 X30", "G0 X40"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )
    assert port.written.count("G0 Z1.5") == 2
    # window is 4: the retry is written before the 5th line is fed
    assert port.written[4] == "G0 Z1.5"
    assert port.written[5] == "G0 X40"


def test_pipelined_corruption_resent_with_same_line_number():
    """On the printer's Resend: request the line is rewritten with its
    original number, keeping the printer's expected-number count aligned."""
    port = CorruptingPort(corrupt_index=2)
    sender = MarlinSender(port)
    sender.connect()
    sender.run_gcode(
        ["G0 X0", "G0 X1", "G0 X2"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )
    resent = [raw for raw in port.raw_written if b"G0 X0" in raw]
    assert len(resent) == 2
    # both the corrupted original and the resend carry the same line number
    numbers = {raw.split(b" ")[0] for raw in resent}
    assert len(numbers) == 1


def test_missing_serial_device_gives_actionable_error(monkeypatch, tmp_path):
    """A missing/empty serial port must produce an error that tells the
    user what to do, not a bare 'could not open port'."""
    import serial

    from app.main import create_app

    def boom(*args, **kwargs):
        raise serial.SerialException("could not open port")

    monkeypatch.setattr(serial, "Serial", boom)
    app = create_app(data_dir=tmp_path, simulate=False)
    db = app.state.db
    drawing_id = db.create_drawing("x", "y", 1, 1, "<svg/>", {})
    db.set_gcode(drawing_id, "G21")
    job_id = app.state.job_runner.start(drawing_id, ["G21"])
    import time

    job = None
    for _ in range(50):
        job = db.get_job(job_id)
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "failed"
    assert "restart the app" in job["error"]


def test_active_job_returns_running_job_then_none():
    """The UI reattaches to a running Job after a page reload (#15):
    active_job() reports it while it runs and None once it finishes."""
    import threading
    import time

    from app.jobs import JobRunner

    from app.db import Database

    class HangingSender:
        def __init__(self):
            self.release = threading.Event()

        def connect(self):
            pass

        def run_gcode(self, lines, on_progress, stop, safe_z=1.5):
            self.release.wait(timeout=5)

        def close(self):
            pass

    db = Database(":memory:")
    sender = HangingSender()
    runner = JobRunner(db, lambda: sender)
    job_id = runner.start(1, ["G0 X0"])
    try:
        for _ in range(100):
            if db.get_job(job_id)["status"] == "running":
                break
            time.sleep(0.02)
        active = runner.active_job()
        assert active is not None and active["id"] == job_id
        assert active["status"] == "running"
    finally:
        sender.release.set()
    for _ in range(100):
        if not runner.busy():
            break
        time.sleep(0.02)
    assert runner.active_job() is None


def test_stale_jobs_failed_on_startup():
    """Jobs left pending/running by a restart are failed, per ADR 0002."""
    from app.db import Database
    from app.jobs import JobRunner

    db = Database(":memory:")
    stuck = db.create_job(1)
    db.update_job(stuck, status="running", progress=40)
    JobRunner(db, lambda: MarlinSender(FakeSerial()))
    assert db.get_job(stuck)["status"] == "failed"


def test_timeout_resends_command_and_recovers():
    """A lost ok (serial corruption) must be recovered by resending the
    command — moves are idempotent for a pen plotter."""
    port = FakeSerial(responses={"G28": [[], ["ok"]]})  # first attempt: silence
    port.open()
    sender = MarlinSender(port, timeout=0.3)
    sender.send_line("G28")
    assert port.written == ["G28", "G28"]


def test_resend_exhaustion_fails_without_m112():
    """When the printer is truly unreachable, the job fails cleanly —
    Crealipen must never kill the machine on its own."""
    port = FakeSerial(responses={"G28": [[], [], []]})
    port.open()
    sender = MarlinSender(port, timeout=0.3)
    with pytest.raises(SenderError):
        sender.send_line("G28")
    assert port.written == ["G28", "G28", "G28"]
    assert "M112" not in port.written


def test_busy_chatter_extends_deadline():
    """Long homing sequences emit 'echo:busy' for minutes; each received
    line proves liveness and must extend the ok deadline."""
    port = FakeSerial(responses={"G28": ["echo:busy: processing"] * 5 + ["ok"]}, delay=0.3)
    port.open()
    sender = MarlinSender(port, timeout=0.5)  # total ~1.8s, silence never > 0.5s
    sender.send_line("G28")  # must not raise


class SilentUntilNPort:
    """Silent for the first N-1 writes, then answers ok (lost-ok simulation)."""

    def __init__(self, respond_on_write: int):
        self.respond_on = respond_on_write
        self.writes = 0
        self.written: list[str] = []
        self._pending_ok = False
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes | str) -> None:
        self.writes += 1
        self.written.append(data.decode().strip())
        if self.writes >= self.respond_on:
            self._pending_ok = True

    def readline(self) -> str:
        if self._pending_ok:
            self._pending_ok = False
            return "ok"
        return ""


def bare(line: str) -> str:
    """Strip Marlin's N/checksum wrapper from a wire-format line."""
    m = re.match(r"^N\d+ (.*)\*\d+$", line)
    return m.group(1) if m else line


def test_timeout_resends_command_and_recovers():
    """A lost ok (serial corruption) must be recovered by resending the
    command — moves are idempotent for a pen plotter."""
    port = SilentUntilNPort(respond_on_write=2)  # first attempt: silence
    port.open()
    sender = MarlinSender(port, timeout=0.3)
    sender.send_line("G28")
    assert [bare(w) for w in port.written] == ["G28", "G28"]


def test_resend_exhaustion_fails_without_m112():
    """When the printer is truly unreachable, the job fails cleanly —
    Crealipen must never kill the machine on its own."""
    port = SilentUntilNPort(respond_on_write=99)
    port.open()
    sender = MarlinSender(port, timeout=0.3)
    with pytest.raises(SenderError):
        sender.send_line("G28")
    assert [bare(w) for w in port.written] == ["G28", "G28", "G28"]
    assert "M112" not in port.written


def test_printer_error_raises_without_m112():
    port = FakeSerial(responses={"G1 X1": "Error:boom"})
    port.open()
    sender = MarlinSender(port)
    with pytest.raises(SenderError):
        sender.send_line("G1 X1")
    assert "M112" not in port.written


def test_cancel_lifts_pen_to_safe_z():
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 3

    sender.run_gcode(
        [f"G0 X{i}" for i in range(100)],
        on_progress=lambda i, n: None,
        stop=stop,
        safe_z=7.5,
    )
    assert port.written[-1] == "G0 Z7.5"
    assert len(port.written) < 100


class RecordingPort(FakeSerial):
    """Records the write/read interleaving so pipelining is observable."""

    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, str]] = []

    def write(self, data):
        super().write(data)
        self.events.append(("write", data.decode().strip()))

    def readline(self):
        line = super().readline()
        self.events.append(("read", line))
        return line


def test_run_gcode_pipelines_lines_within_window():
    """Several lines must be in flight before the first ok is consumed —
    send-one-wait-for-one makes dense plots latency-bound."""
    port = RecordingPort()
    sender = MarlinSender(port)
    sender.connect()
    port.events.clear()
    sender.run_gcode(
        [f"G0 X{i}" for i in range(10)],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )
    kinds = [kind for kind, _ in port.events]
    assert kinds[:4] == ["write"] * 4


def test_pipelined_error_fails_the_job():
    port = FakeSerial(responses={"G0 X1": "Error:boom"})
    sender = MarlinSender(port)
    sender.connect()
    with pytest.raises(SenderError):
        sender.run_gcode(
            ["G0 X0", "G0 X1", "G0 X2"],
            on_progress=lambda i, n: None,
            stop=lambda: False,
        )


class SwallowOkPort(FakeSerial):
    """Swallows the ok of one line's writes, simulating lost oks."""

    def __init__(self, silent: str, times: int = 1):
        super().__init__()
        self.silent = silent
        self.times = times
        self.swallowed = 0

    def write(self, data):
        super().write(data)
        if self.written[-1] == self.silent and self.swallowed < self.times:
            self.swallowed += 1
            if self._pending and self._pending[-1] == "ok":
                self._pending.pop()


def test_pipelined_resends_oldest_on_silence():
    """A lost ok mid-stream surfaces as silence once the buffered oks are
    consumed; the oldest unfinished line is resent (idempotent for moves)."""
    port = SwallowOkPort("G0 X2", times=1)
    sender = MarlinSender(port, timeout=0.3)
    sender.connect()
    sender.run_gcode(
        ["G0 X0", "G0 X1", "G0 X2"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )
    assert port.written.count("G0 X2") == 2


def test_pipelined_resend_exhaustion_fails():
    port = SwallowOkPort("G0 X2", times=99)
    sender = MarlinSender(port, timeout=0.3)
    sender.connect()
    with pytest.raises(SenderError):
        sender.run_gcode(
            ["G0 X0", "G0 X1", "G0 X2"],
            on_progress=lambda i, n: None,
            stop=lambda: False,
        )
    assert "M112" not in port.written
