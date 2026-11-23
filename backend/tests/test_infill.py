from shapely.geometry import Polygon

from app.infill import hatch, crosshatch, concentric, apply_infill


def square():
    return Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])


def test_hatch_lines_stay_inside_region():
    lines = hatch(square(), spacing=2.0, angle=0)
    assert len(lines) >= 4
    for line in lines:
        assert square().contains(line)


def test_crosshatch_has_two_directions():
    lines = crosshatch(square(), spacing=2.0)
    assert len(lines) >= 8
    horizontal = sum(1 for l in lines if abs(l.coords[0][0] - l.coords[-1][0]) > abs(l.coords[0][1] - l.coords[-1][1]))
    vertical = len(lines) - horizontal
    assert horizontal > 0 and vertical > 0


def test_concentric_nests_inward():
    rings = concentric(square(), spacing=1.5)
    assert len(rings) >= 3
    sizes = [ring.length for ring in rings]
    assert sizes == sorted(sizes, reverse=True)


def test_apply_infill_only_fills_enclosed_regions():
    paths = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]
    outlines, infill = apply_infill(paths, pattern="hatch", spacing=2.0)
    assert outlines == paths
    assert len(infill) > 0
    for line in infill:
        assert Polygon(paths[0]).contains(line)


def test_apply_infill_respects_holes_even_odd():
    # black square with a white hole: the hole must NOT be filled
    outer = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
    hole = [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5)]
    _, infill = apply_infill([outer, hole], pattern="hatch", spacing=2.0)
    assert len(infill) > 0
    hole_area = Polygon(hole)
    for line in infill:
        assert not line.intersection(hole_area).length > 0
