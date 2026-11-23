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


def test_estop_lifts_pen_and_stops_streaming():
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 3

    lines = [f"G0 X{i}" for i in range(100)]
    sender.run_gcode(lines, on_progress=lambda i, n: None, stop=stop)
    # after stop triggers, the pen must be lifted as the last move
    assert port.written[-1] == "G0 Z5"
    assert len(port.written) < 100
