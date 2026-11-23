"""G-code generation: ordered paths -> Marlin G-code lines."""
from __future__ import annotations

import math

from app.machine import MachineProfile


def order_paths(
    paths: list[list[tuple[float, float]]],
    start: tuple[float, float] = (0.0, 0.0),
) -> list[list[tuple[float, float]]]:
    """Nearest-neighbor ordering to minimize travel moves."""
    remaining = list(paths)
    ordered = []
    pos = start
    while remaining:
        best = min(
            range(len(remaining)),
            key=lambda i: math.dist(pos, remaining[i][0]),
        )
        path = remaining.pop(best)
        ordered.append(path)
        pos = path[-1]
    return ordered


def _clip_to_bed(
    paths: list[list[tuple[float, float]]], profile: MachineProfile
) -> list[list[tuple[float, float]]]:
    clipped = []
    for path in paths:
        pts = [
            (min(max(x, 0.0), profile.bed_width), min(max(y, 0.0), profile.bed_height))
            for x, y in path
        ]
        if len(pts) >= 2:
            clipped.append(pts)
    return clipped


def transform_paths(
    paths: list[list[tuple[float, float]]],
    scale: float,
    x: float,
    y: float,
    rotation: float,
) -> list[list[tuple[float, float]]]:
    """Apply the preview transform: image pixels -> bed mm.

    Scale is mm per pixel; rotation is degrees clockwise about the image
    origin; (x, y) is the bed position of the image origin.
    """
    rad = math.radians(rotation)
    cos, sin = math.cos(rad), math.sin(rad)
    out = []
    for path in paths:
        pts = []
        for px, py in path:
            mx, my = px * scale, py * scale
            pts.append((x + mx * cos + my * sin, y - mx * sin + my * cos))
        out.append(pts)
    return out


def generate_gcode(
    paths: list[list[tuple[float, float]]],
    profile: MachineProfile,
) -> list[str]:
    # Pen-safe initialization: lift the pen with a relative move before any
    # other motion (it may be resting on the paper), then home X/Y and Z
    # separately — a plain G28 would drag the pen across the paper.
    lift = profile.pen_up_z - profile.pen_down_z + 2.0
    lines = [
        "G21",
        "G90",
        "G91",
        f"G1 Z{lift:g} F600",
        "G90",
        "G28 X Y",
        "G28 Z",
        f"G0 Z{profile.pen_up_z:g} F{profile.travel_feed:g}",
    ]
    for path in order_paths(_clip_to_bed(paths, profile)):
        x0, y0 = path[0]
        lines.append(f"G0 Z{profile.pen_up_z:g}")
        lines.append(f"G0 X{x0:.2f} Y{y0:.2f} F{profile.travel_feed:g}")
        lines.append(f"G1 Z{profile.pen_down_z:g} F{profile.draw_feed:g}")
        for x, y in path[1:]:
            lines.append(f"G1 X{x:.2f} Y{y:.2f} F{profile.draw_feed:g}")
    lines.append(f"G0 Z{profile.pen_up_z:g}")
    lines.append("M84")
    return lines
