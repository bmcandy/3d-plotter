import numpy as np
from PIL import Image

from app.tracing import trace_image, contours_to_svg


def black_square_image(size=100, lo=20, hi=80):
    img = Image.new("L", (size, size), 255)
    px = np.array(img)
    px[lo:hi, lo:hi] = 0
    return Image.fromarray(px)


def test_trace_finds_black_square():
    paths = trace_image(black_square_image(), threshold=128)
    assert len(paths) == 1
    xs = [p[0] for p in paths[0]]
    ys = [p[1] for p in paths[0]]
    assert min(xs) == 20 and max(xs) == 79
    assert min(ys) == 20 and max(ys) == 79


def test_trace_empty_image_has_no_paths():
    img = Image.new("L", (100, 100), 255)
    assert trace_image(img, threshold=128) == []


def test_y_axis_flipped_to_math_orientation():
    # A square occupying the top half of the image must come out with the
    # largest y values (origin flipped to bottom-left for the bed).
    img = Image.new("L", (100, 100), 255)
    px = np.array(img)
    px[0:50, 20:80] = 0
    paths = trace_image(Image.fromarray(px), threshold=128)
    ys = [p[1] for p in paths[0]]
    assert min(ys) == 50 and max(ys) == 99


def test_trace_scales_back_to_original_pixels():
    # images larger than MAX_DIMENSION are traced downscaled, but the
    # returned paths must be in original pixel coordinates
    size = 3000
    img = Image.new("L", (size, size), 255)
    px = np.array(img)
    px[600:2400, 600:2400] = 0
    paths = trace_image(Image.fromarray(px), threshold=128)
    xs = [p[0] for path in paths for p in path]
    ys = [p[1] for path in paths for p in path]
    assert max(xs) > 2000  # original space, not the 2000px downscaled space
    assert min(xs) > 500 and max(xs) < 2500


def test_contours_to_svg_contains_path_and_viewbox():
    paths = trace_image(black_square_image(), threshold=128)
    svg = contours_to_svg(paths, width=100, height=100)
    assert svg.startswith("<svg")
    assert 'viewBox="0 0 100 100"' in svg
    assert "<path" in svg
