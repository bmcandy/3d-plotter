"""Job runner: executes a Drawing's G-code on the machine. One active Job."""
from __future__ import annotations

import threading

from app.db import Database
from app.sender import MarlinSender


class JobRunner:
    def __init__(self, db: Database, sender_factory, safe_z: float = 1.5):
        self.db = db
        self.sender_factory = sender_factory  # () -> MarlinSender
        self.safe_z = safe_z  # pen-up Z, used for cancel/estop lifts
        self._active_job_id: int | None = None
        self._sender: MarlinSender | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        # ADR 0002: a Job never survives a restart; stale ones are failed
        self.db.fail_stale_jobs()

    def busy(self) -> bool:
        return self._active_job_id is not None

    def start(self, drawing_id: int, gcode: list[str]) -> int:
        with self._lock:
            if self._active_job_id is not None:
                raise BusyError(self._active_job_id)
            job_id = self.db.create_job(drawing_id)
            self._active_job_id = job_id
            self._cancel.clear()
        thread = threading.Thread(target=self._run, args=(job_id, gcode), daemon=True)
        thread.start()
        return job_id

    def cancel(self, job_id: int) -> bool:
        if self._active_job_id == job_id:
            self._cancel.set()
            return True
        return False

    def estop(self) -> bool:
        """Lift the pen immediately on the job's own sender (the serial port
        is exclusively owned while a Job runs)."""
        if self._sender is not None:
            try:
                self._sender.send_line(f"G0 Z{self.safe_z:g}")
            except Exception:
                pass  # best effort; the M112-on-failure path still applies
            self._cancel.set()
            return True
        return False

    def _run(self, job_id: int, gcode: list[str]) -> None:
        self.db.update_job(job_id, status="running", progress=0)
        sender = None
        status, error = "done", None
        try:
            sender = self.sender_factory()
            self._sender = sender
            sender.connect()
            sender.run_gcode(
                gcode,
                on_progress=lambda i, n: self.db.update_job(
                    job_id, status="running", progress=int(100 * i / n)
                ),
                stop=self._cancel.is_set,
                safe_z=self.safe_z,
            )
            if self._cancel.is_set():
                status = "cancelled"
        except Exception as exc:
            status, error = "failed", str(exc)
        finally:
            if sender is not None:
                sender.close()
            self._sender = None
            # release the machine before the terminal status is visible,
            # so a new Plot is never refused spuriously
            self._active_job_id = None
        self.db.update_job(
            job_id, status=status,
            progress=100 if status != "failed" else None, error=error,
        )


class BusyError(RuntimeError):
    def __init__(self, active_job_id: int):
        self.active_job_id = active_job_id
        super().__init__(f"machine busy with job {active_job_id}")
