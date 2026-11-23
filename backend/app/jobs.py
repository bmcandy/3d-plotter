"""Job runner: executes a Drawing's G-code on the machine. One active Job."""
from __future__ import annotations

import threading

from app.db import Database
from app.sender import MarlinSender


class JobRunner:
    def __init__(self, db: Database, sender_factory):
        self.db = db
        self.sender_factory = sender_factory  # () -> MarlinSender
        self._active_job_id: int | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

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

    def _run(self, job_id: int, gcode: list[str]) -> None:
        self.db.update_job(job_id, status="running", progress=0)
        sender: MarlinSender = self.sender_factory()
        try:
            sender.connect()
            sender.run_gcode(
                gcode,
                on_progress=lambda i, n: self.db.update_job(
                    job_id, status="running", progress=int(100 * i / n)
                ),
                stop=self._cancel.is_set,
            )
            status = "cancelled" if self._cancel.is_set() else "done"
            self.db.update_job(job_id, status=status, progress=100)
        except Exception as exc:
            self.db.update_job(job_id, status="failed", error=str(exc))
        finally:
            sender.close()
            self._active_job_id = None


class BusyError(RuntimeError):
    def __init__(self, active_job_id: int):
        self.active_job_id = active_job_id
        super().__init__(f"machine busy with job {active_job_id}")
