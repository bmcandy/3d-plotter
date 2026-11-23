"""Binary threshold tracing: raster image -> vector contour paths.

Paths are lists of (x, y) tuples in image pixel coordinates with the y axis
flipped to math orientation (origin bottom-left) so they map directly onto
bed coordinates.
"""
from __future__ import annotations

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
