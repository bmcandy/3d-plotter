import io
import time

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.main import create_app


def png_bytes(size=100):
    px = np.full((size, size), 255, dtype=np.uint8)
    lo, hi = size // 5, size - size // 5
    px[lo:hi, lo:hi] = 0
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


def test_list_drawings_returns_metadata_without_svg(tmp_path):
    c = make_client(tmp_path)
    d1 = c.post(
        "/api/drawings",
        files={"image": ("first.png", png_bytes(), "image/png")},
    ).json()
    d2 = c.post(
        "/api/drawings",
        files={"image": ("second.png", png_bytes(), "image/png")},
    ).json()
    r = c.get("/api/drawings")
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [d1["id"], d2["id"]]
    assert [row["name"] for row in rows] == ["first.png", "second.png"]
    for row in rows:
        assert row["width_px"] == 100
        assert row["height_px"] == 100
        assert "settings" in row
        assert "svg" not in row


def test_get_drawing_returns_full_drawing_for_reopen(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    r = c.get(f"/api/drawings/{d['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["svg"].startswith("<svg")
    assert body["settings"]["size_mm"] > 0
    assert body["width_px"] == 100


def test_generate_gcode_for_drawing(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    r = c.post(f"/api/drawings/{d['id']}/gcode")
    assert r.status_code == 200
    assert "G28" in r.json()["gcode"]


def test_upload_auto_fits_drawing_to_bed(tmp_path):
    """A large image must land on the bed sized to 90% and centered,
    expressed as its max dimension in mm."""
    c = make_client(tmp_path)
    r = c.post(
        "/api/drawings",
        files={"image": ("big.png", png_bytes(size=3000), "image/png")},
    )
    body = r.json()
    s = body["settings"]
    assert s["size_mm"] == pytest.approx(270, abs=1)  # 90% of the 300mm bed
    # centered: drawing width (px * mm-per-px) + 2*x == bed width
    mm_per_px = s["size_mm"] / 3000
    assert abs((3000 * mm_per_px) + 2 * s["x"] - 300) < 1


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
