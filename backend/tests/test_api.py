import io
import time
from collections import Counter
import re

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


def test_gcode_response_includes_estimate(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    r = c.post(f"/api/drawings/{d['id']}/gcode")
    assert r.json()["estimated_seconds"] > 0


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


def test_infill_spacing_is_interpreted_as_mm(tmp_path):
    """Spacing is physical: a 100px image drawn at 50mm is 0.5 mm/px, so a
    2mm spacing must come out as 4px in the traced SVG."""
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(size=100), "image/png")},
    ).json()
    r = c.put(
        f"/api/drawings/{d['id']}",
        json={"threshold": 128, "size_mm": 50.0, "x": 0.0, "y": 0.0,
              "rotation": 0.0, "infill": "hatch", "spacing": 2.0},
    )
    svg = r.json()["svg"]
    ys = sorted({round(float(m), 3) for m in re.findall(r"M\s+[\d.]+\s+([\d.]+)", svg)})
    diffs = [round(b - a, 3) for a, b in zip(ys, ys[1:]) if b - a > 0.5]
    assert Counter(diffs).most_common(1)[0][0] == pytest.approx(4.0, abs=0.2)


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


def noisy_blob_bytes(size=400, seed=7):
    """A disc with a jittered boundary: tracing it yields many short
    segments, the situation simplification is meant to fix."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    r = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    jitter = rng.uniform(-3, 3, size=(size, size))
    px = np.where(r + jitter < size / 4, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(px).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_defaults_detail_mm(tmp_path):
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("test.png", png_bytes(), "image/png")},
    ).json()
    assert d["settings"]["detail_mm"] == 0.5


def test_detail_mm_simplifies_preview_and_gcode_together(tmp_path):
    """The preview must render exactly what will be plotted: the same
    simplification applies to the SVG and the G-code."""
    c = make_client(tmp_path)
    d = c.post(
        "/api/drawings",
        files={"image": ("blob.png", noisy_blob_bytes(), "image/png")},
    ).json()
    fine = dict(d["settings"], detail_mm=0.2)
    coarse = dict(d["settings"], detail_mm=5.0)

    r_fine = c.put(f"/api/drawings/{d['id']}", json=fine).json()
    r_coarse = c.put(f"/api/drawings/{d['id']}", json=coarse).json()
    assert r_coarse["svg"].count("L ") < r_fine["svg"].count("L ")

    g_fine = c.post(f"/api/drawings/{d['id']}/gcode").json()  # coarse stored
    g_coarse = g_fine
    c.put(f"/api/drawings/{d['id']}", json=fine)
    g_fine = c.post(f"/api/drawings/{d['id']}/gcode").json()
    assert g_coarse["lines"] < g_fine["lines"]
