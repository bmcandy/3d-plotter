"""Crealipen FastAPI app: drawings, jobs, machine control."""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from app.db import Database
from app.gcode import generate_gcode, transform_paths
from app.infill import PATTERNS, apply_infill
from app.jobs import BusyError, JobRunner
from app.machine import MachineProfile, ProfileStore
from app.sender import MarlinSender
from app.simulator import FakeSerial
from app.tracing import contours_to_svg, trace_image

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def create_app(data_dir: str | Path = DATA_DIR, simulate: bool = False) -> FastAPI:
    data_dir = Path(data_dir)
    app = FastAPI(title="Crealipen")
    db = Database(data_dir / "crealipen.db")
    profiles = ProfileStore(data_dir / "machine-profile.json")
    app.state.db = db
    app.state.profiles = profiles
    app.state.simulate = simulate

    def sender_factory() -> MarlinSender:
        if simulate:
            return MarlinSender(FakeSerial())
        import serial

        port = serial.Serial("/dev/creality", 115200, timeout=1)
        return MarlinSender(port)

    app.state.job_runner = JobRunner(db, sender_factory)

    @app.post("/api/drawings")
    async def upload_drawing(image: UploadFile, threshold: int = 128):
        raw = await image.read()
        try:
            img = Image.open(io.BytesIO(raw))
        except Exception:
            raise HTTPException(400, "not a readable image")
        paths = trace_image(img, threshold=threshold)
        svg = contours_to_svg(paths, width=img.width, height=img.height)
        image_path = data_dir / "images" / f"{uuid.uuid4().hex}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(raw)
        settings = {"threshold": threshold, "scale": 1.0, "x": 0.0, "y": 0.0,
                    "rotation": 0.0, "infill": "none", "spacing": 2.0}
        drawing_id = db.create_drawing(
            name=image.filename or "drawing", image_path=str(image_path),
            width_px=img.width, height_px=img.height, svg=svg, settings=settings,
        )
        return {"id": drawing_id, "svg": svg, "settings": settings,
                "width_px": img.width, "height_px": img.height}

    @app.get("/api/drawings/{drawing_id}")
    async def get_drawing(drawing_id: int):
        drawing = db.get_drawing(drawing_id)
        if drawing is None:
            raise HTTPException(404, "drawing not found")
        return drawing

    @app.put("/api/drawings/{drawing_id}")
    async def update_drawing(drawing_id: int, settings: dict):
        drawing = db.get_drawing(drawing_id)
        if drawing is None:
            raise HTTPException(404, "drawing not found")
        img = Image.open(drawing["image_path"])
        paths = trace_image(img, threshold=int(settings.get("threshold", 128)))
        outlines, infill = apply_infill(paths, settings.get("infill", "none"),
                                        float(settings.get("spacing", 2.0)))
        svg = contours_to_svg(outlines + [list(line.coords) for line in infill],
                              width=img.width, height=img.height)
        db.update_drawing(drawing_id, svg=svg, settings=settings)
        return {"id": drawing_id, "svg": svg, "settings": settings,
                "width_px": img.width, "height_px": img.height}

    @app.post("/api/drawings/{drawing_id}/gcode")
    async def build_gcode(drawing_id: int):
        drawing = db.get_drawing(drawing_id)
        if drawing is None:
            raise HTTPException(404, "drawing not found")
        settings = drawing["settings"]
        img = Image.open(drawing["image_path"])
        paths = trace_image(img, threshold=int(settings.get("threshold", 128)))
        outlines, infill = apply_infill(paths, settings.get("infill", "none"),
                                        float(settings.get("spacing", 2.0)))
        all_paths = transform_paths(
            outlines + [list(line.coords) for line in infill],
            scale=float(settings.get("scale", 1.0)),
            x=float(settings.get("x", 0.0)),
            y=float(settings.get("y", 0.0)),
            rotation=float(settings.get("rotation", 0.0)),
        )
        gcode = generate_gcode(all_paths, profiles.get())
        db.set_gcode(drawing_id, "\n".join(gcode))
        return {"gcode": "\n".join(gcode), "lines": len(gcode)}

    @app.post("/api/jobs")
    async def start_job(body: dict):
        drawing = db.get_drawing(int(body["drawing_id"]))
        if drawing is None:
            raise HTTPException(404, "drawing not found")
        if not drawing["gcode"]:
            raise HTTPException(400, "drawing has no G-code; generate it first")
        try:
            job_id = app.state.job_runner.start(drawing["id"], drawing["gcode"].splitlines())
        except BusyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int):
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        if not app.state.job_runner.cancel(job_id):
            raise HTTPException(404, "job is not active")
        return {"ok": True}

    @app.get("/api/machine/profile")
    async def get_profile():
        return profiles.get()

    @app.put("/api/machine/profile")
    async def put_profile(profile: MachineProfile):
        return profiles.set(profile)

    @app.post("/api/machine/estop")
    async def estop():
        sender = sender_factory()
        try:
            sender.connect()
            sender.send_line(f"G0 Z{profiles.get().pen_up_z:g}")
        finally:
            sender.close()
        return {"ok": True}

    @app.websocket("/api/jobs/{job_id}/ws")
    async def job_progress(ws: WebSocket, job_id: int):
        await ws.accept()
        try:
            while True:
                job = db.get_job(job_id)
                if job is None:
                    await ws.send_json({"error": "job not found"})
                    break
                await ws.send_json({"status": job["status"], "progress": job["progress"]})
                if job["status"] in ("done", "failed", "cancelled"):
                    break
                await ws.receive_text()  # client pings to poll
        except WebSocketDisconnect:
            pass

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
