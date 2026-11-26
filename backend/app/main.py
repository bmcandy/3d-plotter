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
from app.gcode import estimate_seconds, generate_gcode, transform_paths
from app.infill import PATTERNS, apply_infill
from app.jobs import BusyError, JobRunner
from app.machine import MachineProfile, ProfileStore
from app.sender import MarlinSender
from app.simulator import FakeSerial
from app.tracing import contours_to_svg, simplify_paths, trace_image

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class StripIngressPath:
    """If the ingress proxy forwards the /ingress/<slug> prefix instead of
    stripping it, rewrite it away so routes and static files resolve."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope["path"].startswith("/ingress/"):
            parts = scope["path"].split("/", 3)
            scope["path"] = "/" + parts[3] if len(parts) > 3 else "/"
        await self.app(scope, receive, send)


def _mm_per_px(settings: dict, img: Image.Image) -> float:
    return float(settings.get("size_mm", 300.0)) / max(img.width, img.height)


def _spacing_px(settings: dict, img: Image.Image) -> float:
    """Infill spacing is specified in mm; convert to image pixels using the
    drawing's current mm-per-pixel so physical spacing is exact."""
    return float(settings.get("spacing", 2.0)) / _mm_per_px(settings, img)


def _plot_paths(settings: dict, img: Image.Image) -> list[list[tuple[float, float]]]:
    """Traced and infilled paths, with segments shorter than the detail
    tolerance (mm) collapsed. Both the preview SVG and the G-code are built
    from these, so the preview renders exactly what will be plotted."""
    paths = trace_image(img, threshold=int(settings.get("threshold", 128)))
    outlines, infill = apply_infill(paths, settings.get("infill", "none"),
                                    _spacing_px(settings, img))
    tolerance_px = float(settings.get("detail_mm", 0.5)) / _mm_per_px(settings, img)
    return simplify_paths(outlines + [list(line.coords) for line in infill],
                          tolerance_px)


def create_app(data_dir: str | Path = DATA_DIR, simulate: bool = False) -> FastAPI:
    import logging

    logging.basicConfig(level=logging.INFO)  # serial exchange goes to stdout/app logs
    data_dir = Path(data_dir)
    app = FastAPI(title="Crealipen")
    app.add_middleware(StripIngressPath)
    db = Database(data_dir / "crealipen.db")
    profiles = ProfileStore(data_dir / "machine-profile.json")
    app.state.db = db
    app.state.profiles = profiles
    app.state.simulate = simulate

    def sender_factory() -> MarlinSender:
        if simulate:
            return MarlinSender(FakeSerial())
        import os

        import serial

        port = os.environ.get("SERIAL_PORT") or "/dev/creality"
        try:
            conn = serial.Serial(port, 115200, timeout=1)
        except serial.SerialException as exc:
            raise RuntimeError(
                f"could not open serial port {port!r}: {exc}. "
                "Connect the CR-10s and restart the app (serial devices are "
                "detected at startup), or set serial_port in the app configuration."
            ) from exc
        return MarlinSender(conn)

    app.state.job_runner = JobRunner(db, sender_factory, safe_z=profiles.get().pen_up_z)

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
        # auto-fit: size the drawing to 90% of the bed and center it;
        # size_mm is the drawing's max dimension in mm on the bed
        profile = profiles.get()
        max_dim = max(img.width, img.height)
        fit = min(profile.bed_width / img.width, profile.bed_height / img.height) * 0.9
        settings = {"threshold": threshold, "size_mm": round(max_dim * fit, 1),
                    "x": round((profile.bed_width - img.width * fit) / 2, 2),
                    "y": round((profile.bed_height - img.height * fit) / 2, 2),
                    "rotation": 0.0, "infill": "none", "spacing": 2.0,
                    "detail_mm": 0.5}
        drawing_id = db.create_drawing(
            name=image.filename or "drawing", image_path=str(image_path),
            width_px=img.width, height_px=img.height, svg=svg, settings=settings,
        )
        return {"id": drawing_id, "svg": svg, "settings": settings,
                "width_px": img.width, "height_px": img.height}

    @app.get("/api/drawings")
    async def list_drawings():
        return db.list_drawings()

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
        svg = contours_to_svg(_plot_paths(settings, img),
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
        all_paths = transform_paths(
            _plot_paths(settings, img),
            scale=float(settings.get("size_mm", 300.0)) / max(img.width, img.height),
            x=float(settings.get("x", 0.0)),
            y=float(settings.get("y", 0.0)),
            rotation=float(settings.get("rotation", 0.0)),
        )
        gcode = generate_gcode(all_paths, profiles.get())
        db.set_gcode(drawing_id, "\n".join(gcode))
        return {"gcode": "\n".join(gcode), "lines": len(gcode),
                "estimated_seconds": estimate_seconds(gcode, profiles.get())}

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

    @app.get("/api/jobs/active")
    async def get_active_job():
        # declared before /api/jobs/{job_id} so "active" is not eaten by it
        return app.state.job_runner.active_job()

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
        if app.state.job_runner.estop():
            return {"ok": True, "job_halted": True}
        sender = sender_factory()
        try:
            sender.connect()
            sender.send_line(f"G0 Z{profiles.get().pen_up_z:g}")
        finally:
            sender.close()
        return {"ok": True, "job_halted": False}

    @app.websocket("/api/jobs/{job_id}/ws")
    async def job_progress(ws: WebSocket, job_id: int):
        await ws.accept()
        try:
            while True:
                job = db.get_job(job_id)
                if job is None:
                    await ws.send_json({"error": "job not found"})
                    break
                await ws.send_json({"status": job["status"], "progress": job["progress"],
                                    "error": job["error"]})
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
    import os

    import uvicorn

    simulate = os.environ.get("CREALIPEN_SIMULATE", "").lower() in ("1", "true", "yes")
    uvicorn.run(create_app(simulate=simulate), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
