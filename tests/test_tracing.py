"""Tests for the raster tracer (no CAD, no numpy: Pillow only)."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python", "src"))

from mcp_tools.tools import tracing  # noqa: E402


def _make_image(path, draw_callback, size=(400, 300)):
    from PIL import Image, ImageDraw

    image = Image.new("L", size, 255)
    drawer = ImageDraw.Draw(image)
    draw_callback(drawer)
    image.save(path)
    return path


def test_otsu_threshold_splits_bimodal_histogram():
    histogram = [0] * 256
    histogram[20] = 1000
    histogram[220] = 800
    threshold = tracing._otsu_threshold(histogram)
    # Otsu's boundary is inclusive: levels 0..threshold are the dark class.
    assert 20 <= threshold < 220


def test_rectangle_with_hole_is_traced_exactly(tmp_path):
    def draw(drawer):
        drawer.rectangle([50, 40, 250, 200], fill=0)
        drawer.rectangle([100, 90, 160, 150], fill=255)

    path = _make_image(str(tmp_path / "shape.png"), draw)
    result = tracing.vectorize_image(path, tolerance=1.2, min_area=10)

    assert result["iou"] > 0.9
    outers = [p for p in result["polygons"] if p["role"] == "outer"]
    holes = [p for p in result["polygons"] if p["role"] == "hole"]
    assert len(outers) == 1
    assert len(holes) == 1

    # 200 x 160 px outer body, 60 x 60 px hole, both within a couple of pixels.
    assert result["polygons"][0]["area"] == pytest.approx(200 * 160, rel=0.02)
    assert abs(result["polygons"][1]["area"]) == pytest.approx(60 * 60, rel=0.05)

    rectangle = outers[0]["points"]
    assert len(rectangle) == 4
    xs = sorted({round(point[0], 3) for point in rectangle})
    ys = sorted({round(point[1], 3) for point in rectangle})
    assert xs == pytest.approx([50.0, 250.0], abs=1.5)
    assert ys == pytest.approx([40.0, 200.0], abs=1.5)


def test_diagonal_edge_is_snapped_to_45_degrees(tmp_path):
    def draw(drawer):
        drawer.polygon([(40, 40), (240, 40), (240, 200), (40, 200)], fill=0)
        drawer.polygon([(40, 200), (140, 200), (240, 100), (240, 200)], fill=255)

    path = _make_image(str(tmp_path / "diagonal.png"), draw)
    result = tracing.vectorize_image(path, tolerance=1.5, min_area=10, snap=True)
    assert result["polygons"], "expected at least one contour"

    diagonal = None
    for polygon in result["polygons"]:
        points = polygon["points"]
        for index in range(len(points)):
            start = points[index]
            end = points[(index + 1) % len(points)]
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            if abs(dx) > 20 and abs(dy) > 20:
                diagonal = (dx, dy)
    assert diagonal is not None, "no slanted edge found"
    assert abs(abs(diagonal[0]) - abs(diagonal[1])) < 1.0


def test_transform_maps_pixels_to_units_with_y_up():
    points = [(0, 0), (10, 20)]
    moved = tracing._transform(points, scale=0.5, origin_x=100, origin_y=10, image_height=40)
    assert moved[0] == pytest.approx([100.0, 30.0])
    assert moved[1] == pytest.approx([105.0, 20.0])


def test_subtolerance_vertex_is_dropped():
    square_with_jog = [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 5.0),
        (10.2, 6.0),  # 1 px staircase leftover on a straight side
        (10.0, 10.0),
        (0.0, 10.0),
    ]
    cleaned = tracing._drop_subtolerance_vertices(square_with_jog, 1.5)
    assert len(cleaned) == 4
    assert (10.2, 6.0) not in cleaned
