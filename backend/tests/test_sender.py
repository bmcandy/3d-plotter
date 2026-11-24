import pytest

from app.sender import MarlinSender, SenderError
from app.simulator import FakeSerial


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
    assert len(port.written) == 10


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


def test_stale_jobs_failed_on_startup():
    """Jobs left pending/running by a restart are failed, per ADR 0002."""
    from app.db import Database
    from app.jobs import JobRunner

    db = Database(":memory:")
    stuck = db.create_job(1)
    db.update_job(stuck, status="running", progress=40)
    JobRunner(db, lambda: MarlinSender(FakeSerial()))
    assert db.get_job(stuck)["status"] == "failed"


def test_error_mid_job_sends_m112_before_raising():
    """If a command fails mid-job (e.g. homing grinds forever and times
    out), the machine must be halted with M112 — motion must not run away."""
    port = FakeSerial(responses={"G0 X5": "Error:boom"})
    sender = MarlinSender(port)
    sender.connect()
    with pytest.raises(SenderError):
        sender.run_gcode(["G0 X5", "G0 X6"], on_progress=lambda i, n: None, stop=lambda: False)
    assert port.written[-1] == "M112"


def test_timeout_mid_job_sends_m112_before_raising():
    class DeafPort(FakeSerial):
        def readline(self):
            return ""  # never responds

    port = DeafPort()
    sender = MarlinSender(port, timeout=0.2)
    sender.connect()
    with pytest.raises(SenderError):
        sender.run_gcode(["G28 X Y"], on_progress=lambda i, n: None, stop=lambda: False)
    assert port.written[-1] == "M112"


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
