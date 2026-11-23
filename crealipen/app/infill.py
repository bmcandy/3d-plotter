"""Infill patterns: fill enclosed regions of traced contours.

Each pattern takes a shapely geometry and a spacing and returns a list of
LineStrings clipped to the region. Patterns are pluggable strategies so new
experimental (e.g. "human-looking") patterns can be added without touching
the pipeline.
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.ops import polygonize, unary_union

PATTERNS = ("hatch", "crosshatch", "concentric")


def _regions(paths: list[list[tuple[float, float]]]) -> list[Polygon]:
    # polygonize over the raw linework, then keep only regions that are
    # "black" under even-odd nesting (holes inside outlines stay white)
    rings = []
    lines = []
    for path in paths:
        if len(path) < 3:
            continue
        closed = list(path)
        if closed[0] != closed[-1]:
            closed.append(closed[0])
        lines.append(LineString(closed))
        poly = Polygon(closed).buffer(0)
        if isinstance(poly, Polygon):
            rings.append(poly)
    if not lines:
        return []
    regions = []
    for region in polygonize(unary_union(lines)):
        point = region.representative_point()
        depth = sum(1 for ring in rings if ring.contains(point))
        if depth % 2 == 1:
            regions.append(region)
    return regions


def hatch(region: Polygon, spacing: float, angle: float = 0.0) -> list[LineString]:
    if region.is_empty:
        return []
    minx, miny, maxx, maxy = region.bounds
    diag = math.hypot(maxx - minx, maxy - miny)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    rad = math.radians(angle)
    lines = []
    n = int(diag / spacing) + 1
    for i in range(-n // 2 - 1, n // 2 + 2):
        offset = i * spacing
        # line through (cx, cy + offset) rotated by angle
        dx, dy = math.cos(rad), math.sin(rad)
        px, py = -dy * offset, dx * offset
        seg = LineString([(cx + px - dx * diag, cy + py - dy * diag),
                          (cx + px + dx * diag, cy + py + dy * diag)])
        clipped = seg.intersection(region)
        if clipped.is_empty:
            continue
        if isinstance(clipped, LineString):
            lines.append(clipped)
        elif isinstance(clipped, MultiLineString):
            lines.extend(clipped.geoms)
    return lines


def crosshatch(region: Polygon, spacing: float) -> list[LineString]:
    return hatch(region, spacing, angle=0) + hatch(region, spacing, angle=90)


def concentric(region: Polygon, spacing: float) -> list[LineString]:
    rings = []
    current = region
    for _ in range(50):
        current = current.buffer(-spacing)
        if current.is_empty:
            break
        if isinstance(current, Polygon):
            rings.append(LineString(current.exterior))
        else:
            rings.extend(LineString(g.exterior) for g in current.geoms if isinstance(g, Polygon))
    return rings


_PATTERN_FNS = {"hatch": hatch, "crosshatch": crosshatch, "concentric": concentric}


def apply_infill(
    paths: list[list[tuple[float, float]]],
    pattern: str,
    spacing: float,
) -> tuple[list[list[tuple[float, float]]], list[LineString]]:
    """Return (outlines, infill_lines) for the given traced paths."""
    if pattern not in _PATTERN_FNS or spacing <= 0:
        return paths, []
    fn = _PATTERN_FNS[pattern]
    infill: list[LineString] = []
    for region in _regions(paths):
        infill.extend(fn(region, spacing))
    return paths, infill
