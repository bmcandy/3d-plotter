import io
import time

import numpy as np
from PIL import Image
from fastapi.testclient import TestClient

from app.main import create_app


def png_bytes():
    px = np.full((100, 100), 255, dtype=np.uint8)
    px[20:80, 20:80] = 0
    buf = io.BytesIO()
    Image.fromarray(px).save(buf, format="PNG")
    return buf.getvalue()


def make_client(tmp_path):
    return TestClient(create_app(data_dir=tmp_path, simulate=True))


def test_upload_traces_to_svg(tmp_path):
    c = make_client(tmp_path)
    r = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["svg"].startswith("<svg")
    assert body["id"] > 0


def test_generate_gcode_for_drawing(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    r = c.post(f"/api/drawings/{d['id']}/gcode")
    assert r.status_code == 200
    assert "G28" in r.json()["gcode"]


def test_machine_profile_roundtrip(tmp_path):
    c = make_client(tmp_path)
    profile = c.get("/api/machine/profile").json()
    assert profile["bed_width"] == 300
    profile["pen_down_z"] = 1.5
    r = c.put("/api/machine/profile", json=profile)
    assert r.status_code == 200
    assert c.get("/api/machine/profile").json()["pen_down_z"] == 1.5


def test_job_runs_to_completion_against_simulator(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    c.post(f"/api/drawings/{d['id']}/gcode")
    r = c.post("/api/jobs", json={"drawing_id": d["id"]})
    assert r.status_code == 200
    job_id = r.json()["id"]
    job = None
    for _ in range(100):
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert job["status"] == "done"
    assert job["progress"] == 100


def test_second_job_refused_while_machine_busy(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    c.post(f"/api/drawings/{d['id']}/gcode")
    runner = c.app.state.job_runner
    runner._active_job_id = 999  # simulate a job that never finishes
    try:
        r = c.post("/api/jobs", json={"drawing_id": d["id"]})
        assert r.status_code == 409
    finally:
        runner._active_job_id = None
