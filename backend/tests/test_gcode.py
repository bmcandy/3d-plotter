import pytest

from app.gcode import estimate_seconds, order_paths, generate_gcode
from app.machine import MachineProfile

PROFILE = MachineProfile(
    bed_width=300,
    bed_height=300,
    pen_up_z=5.0,
    pen_down_z=0.0,
    travel_feed=3000,
    draw_feed=1500,
)


def test_order_paths_nearest_neighbor():
    far = [(100, 100), (110, 100)]
    near = [(1, 1), (2, 1)]
    ordered = order_paths([far, near], start=(0, 0))
    assert ordered[0] == near


def test_gcode_homes_and_disables_steppers():
    lines = generate_gcode([[(0, 0), (10, 10)]], PROFILE)
    assert lines[0] == "G21"
    assert "G90" in lines
    assert "G28 X Y" in lines
    assert "G28 Z" in lines
    assert lines[-1] == "M84"


def test_custom_init_gcode_is_used_verbatim():
    profile = MachineProfile(
        bed_width=300, bed_height=300, pen_up_z=5.0, pen_down_z=0.0,
        travel_feed=3000, draw_feed=1500,
        init_gcode="G28\nG0 X150 Y150",
    )
    lines = generate_gcode([[(0, 0), (10, 10)]], profile)
    assert "G28" in lines          # custom init used as-is
    assert "G0 X150 Y150" in lines
    assert "G28 X Y" not in lines  # default init not appended
    # the safety lift still precedes the custom init
    lift_idx = next(i for i, l in enumerate(lines) if l.startswith("G1 Z"))
    init_idx = lines.index("G28")
    assert lift_idx < init_idx


def test_pen_is_lifted_before_any_motion():
    """The pen may be resting on the paper; a relative lift must come
    before homing, and homing must not drag the pen (no plain G28)."""
    lines = generate_gcode([[(0, 0), (10, 10)]], PROFILE)
    lift_idx = next(i for i, l in enumerate(lines) if l.startswith("G1 Z7"))  # 5-0+2
    home_idx = next(i for i, l in enumerate(lines) if l.startswith("G28"))
    assert lift_idx < home_idx
    assert "G28" not in lines  # never a bare G28 (would home with pen down)
    assert lines[lift_idx - 1] == "G91"  # lift is relative
    # after homing, the pen goes to the pen-up position before any XY travel
    z_up_idx = next(i for i, l in enumerate(lines) if l == "G0 Z5")
    first_xy = next(i for i, l in enumerate(lines) if l.startswith("G0 X"))
    assert z_up_idx < first_xy


def test_pen_down_before_draw_pen_up_for_travel():
    paths = [[(0, 0), (10, 0)], [(50, 50), (60, 50)]]
    lines = generate_gcode(paths, PROFILE)
    pen_down = False
    for line in lines:
        if line.startswith("G1 Z0"):
            pen_down = True
        elif line.startswith("G0 Z5"):
            pen_down = False
        elif line.startswith("G1 X") and not pen_down:
            raise AssertionError(f"drawing move with pen up: {line}")


def test_estimate_seconds_straight_lines():
    lines = ["G90", "G0 X10 Y0 F3000", "G0 X20 Y0"]
    # two 10mm travels at 3000 mm/min = 0.2s each
    assert estimate_seconds(lines, PROFILE) == pytest.approx(0.4)


def test_estimate_seconds_counts_z_moves_and_feed_rates():
    lines = ["G90", "G0 Z5 F3000", "G1 Z0 F1500"]
    # 5mm at 3000 mm/min = 0.1s, 5mm at 1500 mm/min = 0.2s
    assert estimate_seconds(lines, PROFILE) == pytest.approx(0.3)


def test_estimate_seconds_feed_is_modal():
    lines = ["G90", "G1 X10 Y0 F600", "G1 X20 Y0"]
    # the F600 on the first line applies to the second move too
    assert estimate_seconds(lines, PROFILE) == pytest.approx(2.0)


def test_estimate_seconds_handles_relative_mode():
    lines = ["G91", "G1 X10 F600", "G1 X10"]
    assert estimate_seconds(lines, PROFILE) == pytest.approx(2.0)


def test_estimate_seconds_of_generated_gcode():
    paths = [[(0, 0), (100, 0)], [(100, 100), (0, 100)]]
    lines = generate_gcode(paths, PROFILE)
    seconds = estimate_seconds(lines, PROFILE)
    assert seconds > 0
    # the two 100mm draw moves alone take 2 * 100/1500*60 = 8s
    assert seconds > 8.0


def test_coordinates_stay_within_bed():
    paths = [[(-5, -5), (400, 400)]]  # out of bed; clipped to bed
    lines = generate_gcode(paths, PROFILE)
    for line in lines:
        if line.startswith(("G0 X", "G1 X")):
            for token in line.split():
                if token.startswith(("X", "Y")):
                    assert 0 <= float(token[1:]) <= 300
