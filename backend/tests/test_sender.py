import re

import pytest

from app.sender import MarlinSender, SenderError
from app.simulator import FakeSerial


class CorruptingPort(FakeSerial):
    """Mangles the checksum of the nth checksummed write(s), like the
    flaky USB-serial adapter in #21: the printer must catch it and
    request a resend."""

    def __init__(self, corrupt_index: int | list[int]):
        super().__init__()
        self.corrupt_indices = set(
            [corrupt_index] if isinstance(corrupt_index, int) else corrupt_index
        )
        self.checksummed = 0

    def write(self, data):
        if isinstance(data, bytes) and b"*" in data:
            self.checksummed += 1
            if self.checksummed in self.corrupt_indices:
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
    assert port.written.count("M105") == 1
    assert not port._pending
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


def test_connect_resets_line_numbering_after_printer_reset():
    """Opening the port resets the printer (DTR pulse), so its expected
    line number is 0: the handshake probes must be unnumbered, M110 N0
    realigns the count, and the first job line is N1."""
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    assert port.raw_written == [b"M105\n", b"M110 N0\n"]
    sender.send_line("G21")
    # the prompt-clearing commands are plain: the first job line is N1
    assert re.match(rb"^N1 G21\*\d+\n$", port.raw_written[-1])


def test_checksum_mismatch_error_is_recovered_not_fatal():
    """Real Marlin answers a corrupted line with the trio 'Error:checksum
    mismatch, Last Line: N' + 'Resend: N+1' + ok. That is a resend
    request, not a job failure — the sender must wait for the Resend and
    rewrite the line."""
    port = FakeSerial(responses={
        "G1 X1": ["Error:checksum mismatch, Last Line: 0", "Resend: 1", "ok", "ok"],
    })
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G1 X1")  # must not raise
    assert len([raw for raw in port.raw_written if b"G1 X1" in raw]) == 2


def test_line_number_error_is_recovered_not_fatal():
    port = FakeSerial(responses={
        "G1 X1": ["Error:Line Number is not Last Line Number+1, Last Line: 0",
                  "Resend: 1", "ok", "ok"],
    })
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G1 X1")  # must not raise


def test_no_line_number_with_checksum_error_is_recovered():
    """The exact hardware failure from 0.5.2: wire noise mangles a line's
    leading bytes so Marlin sees a checksum with no line number and
    answers 'Error:No Line Number with checksum, Last Line: 0' + Resend.
    That is a resend request, not a job failure."""
    port = FakeSerial(responses={
        "G1 X1": ["Error:No Line Number with checksum, Last Line: 0",
                  "Resend: 1", "ok", "ok"],
    })
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G1 X1")  # must not raise
    assert len([raw for raw in port.raw_written if b"G1 X1" in raw]) == 2


def test_no_checksum_with_line_number_error_is_recovered():
    port = FakeSerial(responses={
        "G1 X1": ["Error:No Checksum with line number, Last Line: 0",
                  "Resend: 1", "ok", "ok"],
    })
    sender = MarlinSender(port)
    sender.connect()
    sender.send_line("G1 X1")  # must not raise


def test_other_printer_errors_still_fail_the_job():
    port = FakeSerial(responses={"G1 X1": "Error:Thermal Runaway"})
    sender = MarlinSender(port)
    sender.connect()
    with pytest.raises(SenderError, match="Thermal Runaway"):
        sender.send_line("G1 X1")


def test_connect_does_not_dismiss_user_prompts_or_change_recovery():
    """Connecting must not simulate an LCD click, answer host prompts,
    or change the printer's power-loss recovery setting."""
    port = FakeSerial()
    sender = MarlinSender(port)
    sender.connect()
    assert port.written == ["M105", "M110 N0"]


def test_connect_never_sends_prompt_or_recovery_commands():
    port = FakeSerial()
    MarlinSender(port).connect()
    assert not any(command.startswith(("M876", "M108", "M413")) for command in port.written)


def test_corrupted_line_is_resent_after_resend_request():
    """A duplicated byte on the wire (the CH340 corruption in #21) must be
    caught by the printer's checksum and the line resent — not dropped."""
    port = CorruptingPort(corrupt_index=1)  # first checksummed write: the job command
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


class RefusingPort(FakeSerial):
    """Always refuses one command (echo:Unknown command + ok), however
    often it is retried."""

    def __init__(self, refuse: str):
        super().__init__()
        self.refuse = refuse

    def write(self, data):
        super().write(data)
        if self.written and self.written[-1] == self.refuse:
            self._pending[-1] = 'echo:Unknown command: "XX"'
            self._pending.append("ok")


def test_unknown_command_exhaustion_fails_cleanly():
    port = RefusingPort("G0 Z1.5")
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
    sender = MarlinSender(port, window=4)
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


def test_pipelined_corruption_recovers_via_resync():
    """A corrupted line mid-stream triggers a resync: numbering resets
    (M110 N0) and the unconfirmed window is re-sent from N1 — the job
    completes and no line is dropped."""
    port = CorruptingPort(corrupt_index=4)  # first job command
    sender = MarlinSender(port)
    sender.connect()
    sender.run_gcode(
        ["G0 X0", "G0 X1", "G0 X2"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )
    executed = port.written[2:]
    assert all(line in executed for line in ["G0 X0", "G0 X1", "G0 X2"])


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


def test_mid_job_corruption_burst_recovers_and_executes_every_line():
    """The reported hardware failure: a corrupted line mid-job gets the
    Marlin error trio, and the pipelined followers behind it are rejected
    (number gap) until the corrupted line is retransmitted. The job must
    complete with every line executed."""
    port = CorruptingPort(corrupt_index=3)  # third checksummed line
    sender = MarlinSender(port)
    sender.connect()
    lines = [f"G1 X{i} Y{i}" for i in range(10)]
    progress = []
    sender.run_gcode(
        lines,
        on_progress=lambda i, n: progress.append((i, n)),
        stop=lambda: False,
    )
    assert progress[-1] == (10, 10)
    # every line executed (the resync may re-execute some — moves are
    # idempotent); what must never happen is a dropped line
    executed = port.written[2:]
    assert all(line in executed for line in lines)


def test_window_keeps_pipelining_inside_the_rx_buffer():
    """The sender's window must never outrun Marlin's 128-byte RX buffer:
    a long plot streams to completion with every line intact."""
    port = FakeSerial()
    sender = MarlinSender(port, window=4)
    sender.connect()
    lines = [f"G1 X{i % 200} Y{i % 200} F1500" for i in range(200)]
    progress = []
    sender.run_gcode(
        lines,
        on_progress=lambda i, n: progress.append((i, n)),
        stop=lambda: False,
    )
    assert progress[-1] == (200, 200)
    assert port.written[2:] == lines  # handshake aside: all executed, in order


def test_ok_with_payload_is_an_ok():
    """Real firmware acks M105 with 'ok T:56.3' etc.; anything starting
    with ok acks the line."""
    port = FakeSerial(responses={"M105": "ok T:56.3 /0.0 /0.0 @:0.0"})
    sender = MarlinSender(port)
    sender.connect()  # the handshake's M105 is acked with a payload
    sender.send_line("G28")  # and the job proceeds normally


def test_blocking_command_under_pipelining():
    """A long homing mid-stream acks late, behind echo:busy chatter; the
    pipelined window must wait it out, not resend over it."""
    port = FakeSerial(
        responses={"G28": ["echo:busy: processing"] * 4 + ["ok"]},
        delay=0.1,
    )
    sender = MarlinSender(port, timeout=1.0)
    sender.connect()
    sender.run_gcode(
        ["G28", "G0 X1", "G0 X2"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )  # must not raise
    assert port.written.count("G28") == 1  # never resent over the chatter


def test_recurring_corruption_across_a_long_plot_recovers():
    """Wire noise recurs (~every 55 lines on this machine): a long plot
    must survive repeated corruption bursts as long as progress continues
    between them. Only a link that stops making progress may fail."""
    port = CorruptingPort(corrupt_index=[7, 15, 40, 80, 160, 320])
    sender = MarlinSender(port)
    sender.connect()
    lines = [f"G1 X{i % 200} Y{i % 200} F1500" for i in range(200)]
    progress = []
    sender.run_gcode(
        lines,
        on_progress=lambda i, n: progress.append((i, n)),
        stop=lambda: False,
    )
    assert progress[-1] == (200, 200)
    executed = port.written[2:]
    assert all(line in executed for line in lines)  # none dropped


def test_corrupted_resend_request_is_probed_not_trusted():
    """Wire noise corrupts the printer->host direction too: a 'Resend: 4'
    that should read 'Resend: 24' must not fail the job. The sender
    interrogates the printer's true position (a deliberately-bad line
    makes Marlin answer with it) and recovers from there."""
    class BogusResendPort(FakeSerial):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def readline(self):
            self.reads += 1
            if self.reads == 40 and not self._pending:
                return "Resend: 4"  # corrupted in transit (true: ~20)
            return super().readline()

    port = BogusResendPort()
    sender = MarlinSender(port)
    sender.connect()
    lines = [f"G1 X{i % 200} Y{i % 200} F1500" for i in range(200)]
    progress = []
    sender.run_gcode(
        lines,
        on_progress=lambda i, n: progress.append((i, n)),
        stop=lambda: False,
    )
    assert progress[-1] == (200, 200)
    executed = port.written[2:]
    assert all(line in executed for line in lines)  # none dropped


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
    sender = MarlinSender(port, window=4)
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


def test_pipelined_lost_ok_recovered_by_resend_resync():
    """A lost ok mid-stream surfaces as silence; the rewrite is rejected
    as a duplicate number, which proves the line executed, and the resync
    re-sends the unconfirmed tail. The job completes; the move ran once."""
    port = SwallowOkPort("G0 X2", times=1)
    sender = MarlinSender(port, timeout=0.3)
    sender.connect()
    sender.run_gcode(
        ["G0 X0", "G0 X1", "G0 X2"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )  # must not raise
    # the move executed (the resync may legitimately re-execute it —
    # moves are idempotent); what must never happen is a dropped line
    assert port.written.count("G0 X2") >= 1


def test_persistent_ok_loss_recovered_by_position_oracle():
    """Even when oks never reach us, the printer's duplicate-number
    rejection reveals its position and the job completes — the resend
    buffer needs nothing re-sent because everything executed."""
    port = SwallowOkPort("G0 X2", times=99)
    sender = MarlinSender(port, timeout=0.3)
    sender.connect()
    sender.run_gcode(
        ["G0 X0", "G0 X1", "G0 X2"],
        on_progress=lambda i, n: None,
        stop=lambda: False,
    )  # must not raise
    assert port.written.count("G0 X2") == 1


def test_truly_silent_printer_fails_cleanly():
    """A printer that never answers (and never reveals its position)
    fails the job cleanly — Crealipen must never kill the machine."""
    from app.db import Database
    from app.jobs import JobRunner

    class DeafPort(FakeSerial):
        def readline(self):
            return ""

    db = Database(":memory:")
    runner = JobRunner(db, lambda: MarlinSender(DeafPort(), timeout=0.2, boot_wait=0.3))
    job_id = runner.start(1, ["G28"])
    import time as _t

    job = None
    for _ in range(200):
        job = db.get_job(job_id)
        if job["status"] not in ("pending", "running"):
            break
        _t.sleep(0.05)
    assert job["status"] == "failed"
    assert "M112" not in str(job["error"])
