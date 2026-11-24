"""G-code generation: ordered paths -> Marlin G-code lines."""
from __future__ import annotations

import math
import re

from app.machine import MachineProfile

_PARAM = re.compile(r"([A-Za-z])(-?\d*\.?\d+)")


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
    # Pen-safe preamble: lift the pen with a relative move before any other
    # motion (it may be resting on the paper). The homing strategy itself is
    # user-configurable via the machine profile.
    lift = profile.pen_up_z - profile.pen_down_z + 2.0
    lines = [
        "G21",
        "G90",
        "G91",
        f"G1 Z{lift:g} F600",
        "G90",
    ]
    lines += [line.strip() for line in profile.init_gcode.splitlines() if line.strip()]
    lines.append(f"G0 Z{profile.pen_up_z:g} F{profile.travel_feed:g}")
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


def estimate_seconds(lines: list[str], profile: MachineProfile) -> float:
    """Constant-velocity estimate of job duration: each move's distance
    divided by its feed rate, including Z and travel moves. No acceleration
    model, so real jobs take longer. The start position is assumed to be
    (0, 0, pen_down_z)."""
    x, y, z = 0.0, 0.0, profile.pen_down_z
    feed = profile.draw_feed
    absolute = True
    total = 0.0
    for raw in lines:
        parts = raw.strip().split()
        if not parts:
            continue
        cmd = parts[0].upper()
        params = {m.group(1).upper(): float(m.group(2))
                  for m in _PARAM.finditer(" ".join(parts[1:]))}
        if cmd in ("G0", "G1"):
            if "F" in params:
                feed = params["F"]
            nx = params.get("X", x) if absolute else x + params.get("X", 0.0)
            ny = params.get("Y", y) if absolute else y + params.get("Y", 0.0)
            nz = params.get("Z", z) if absolute else z + params.get("Z", 0.0)
            dist = math.dist((x, y, z), (nx, ny, nz))
            if dist > 0 and feed > 0:
                total += dist / feed * 60.0
            x, y, z = nx, ny, nz
        elif cmd == "G90":
            absolute = True
        elif cmd == "G91":
            absolute = False
        # G28 and other non-motion commands are ignored
    return total
