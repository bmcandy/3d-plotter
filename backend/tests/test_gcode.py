from app.gcode import order_paths, generate_gcode
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
    assert "G28" in lines
    assert lines[-1] == "M84"


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


def test_coordinates_stay_within_bed():
    paths = [[(-5, -5), (400, 400)]]  # out of bed; clipped to bed
    lines = generate_gcode(paths, PROFILE)
    for line in lines:
        if line.startswith(("G0 X", "G1 X")):
            for token in line.split():
                if token.startswith(("X", "Y")):
                    assert 0 <= float(token[1:]) <= 300
