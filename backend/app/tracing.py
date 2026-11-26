"""Binary threshold tracing: raster image -> vector contour paths.

Paths are lists of (x, y) tuples in image pixel coordinates with the y axis
flipped to math orientation (origin bottom-left) so they map directly onto
bed coordinates.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import Image

MAX_DIMENSION = 2000


def _prepare(img: Image.Image) -> Image.Image:
    img = img.convert("L")
    if max(img.size) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    return img


def trace_image(img: Image.Image, threshold: int, min_area: float = 25.0) -> list[list[tuple[float, float]]]:
    original = img.convert("L")
    prepared = _prepare(original)
    # paths are traced on the downscaled image but returned in original
    # pixel coordinates, so the SVG viewBox and mm/px scale stay consistent
    px_scale = original.width / prepared.width
    arr = np.array(prepared)
    _, binary = cv2.threshold(arr, threshold, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    height = arr.shape[0]
    paths = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area / (px_scale * px_scale):
            continue
        pts = [
            (float(p[0][0] * px_scale), float((height - 1 - p[0][1]) * px_scale))
            for p in contour
        ]
        if pts[0] != pts[-1]:
            pts.append(pts[0])  # close the ring for polygonize/G-code
        paths.append(pts)
    return paths


def _seg_dist(a, p, b) -> float:
    """Distance from point p to the segment a-b."""
    ax, ay = a
    px, py = p
    dx, dy = b[0] - ax, b[1] - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _drop_collinear(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Remove points that lie on the straight line between their neighbours:
    pure move-count reduction with no shape change at all."""
    if len(path) <= 2:
        return path
    out = [path[0]]
    for prev, pt, nxt in zip(path, path[1:], path[2:]):
        if _seg_dist(prev, pt, nxt) > 1e-6:
            out.append(pt)
    out.append(path[-1])
    return out


def _merge_micro(path: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Drop points until the accumulated arc length since the last kept
    point reaches `tolerance`: runs of sub-tolerance segments merge into
    segments of at least that length; longer segments are never altered."""
    if len(path) <= 2:
        return path
    out = [path[0]]
    acc = 0.0
    for pt in path[1:-1]:
        acc += math.dist(out[-1], pt)
        if acc >= tolerance:
            out.append(pt)
            acc = 0.0
    out.append(path[-1])
    if len(out) > 1 and out[-1] == out[-2]:  # fully collapsed ring
        out.pop()
    return out


def simplify_paths(
    paths: list[list[tuple[float, float]]], tolerance: float
) -> list[list[tuple[float, float]]]:
    """Disregard lines shorter than `tolerance` (in the paths' own units):
    collinear points are dropped exactly, and runs of sub-tolerance
    segments merge into segments of at least that length. Longer segments
    are never reshaped — the tolerance is a minimum segment length, not a
    point budget (#18)."""
    if tolerance <= 0:
        return paths
    return [_merge_micro(_drop_collinear(path), tolerance) for path in paths]


def contours_to_svg(paths: list[list[tuple[float, float]]], width: float, height: float) -> str:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'width="{width}" height="{height}">']
    for path in paths:
        if len(path) < 2:
            continue
        d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in path) + " Z"
        parts.append(f'<path d="{d}" fill="none" stroke="black" stroke-width="0.5"/>')
    parts.append("</svg>")
    return "".join(parts)
