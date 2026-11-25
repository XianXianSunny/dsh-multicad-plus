"""Pure-geometry tests for :mod:`adapters.mixins.geometry_mixin`.

These tests never touch COM: they exercise the maths that fillet, chamfer and
join rely on, using a stub adapter. Run with ``pytest`` from the repository
root after ``pip install -r python/requirements.txt pytest``.

The adapter modules import win32com at import time, so the suite only runs on
Windows - the same platform the server targets.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "python", "src"))

from adapters.mixins.geometry_mixin import GeometryMixin  # noqa: E402

TAN_22_5 = math.tan(math.radians(22.5))  # bulge of a 90 degree arc


class StubAdapter(GeometryMixin):
    """GeometryMixin with just enough surface for the pure-math methods."""

    cad_type = "autocad"

    def __init__(self):
        self.captured = None

    # -- stubs used by the mixin ------------------------------------------------
    def _validate_connection(self):
        return None

    def _get_document(self, operation="operation"):
        raise AssertionError("no document access expected in pure-math tests")

    def refresh_view(self):
        return True

    def _add_lwpolyline(self, document, vertices, bulges, closed, layer=None, color=None):
        self.captured = {
            "vertices": list(vertices),
            "bulges": list(bulges),
            "closed": closed,
            "layer": layer,
            "color": color,
        }

        class FakePolyline:
            Handle = "FAKE"

        return FakePolyline()


class FakeEntity:
    """Minimal stand-in exposing the COM properties the mixin reads."""

    def __init__(self, object_name, **properties):
        self.ObjectName = object_name
        for key, value in properties.items():
            setattr(self, key, value)


# ========== fillet maths ==========


def test_fillet_square_radius_10():
    adapter = StubAdapter()
    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]

    vertices, bulges, report = adapter._corner_points(square, True, 10.0, None)

    assert len(vertices) == 8
    assert len(bulges) == 8
    assert len(report) == 4
    # Ring order is (0,0) -> (100,0) -> (100,100) -> (0,100), so at the first corner
    # we arrive from (0,100) and leave towards (100,0):
    assert vertices[0] == pytest.approx((0.0, 10.0))   # tangent on the incoming edge
    assert vertices[1] == pytest.approx((10.0, 0.0))   # tangent on the outgoing edge
    # 90 degree corner -> sweep 90 degrees -> bulge tan(90/4)
    assert bulges[0] == pytest.approx(TAN_22_5)
    # every fillet arc is preceded by its own bulge and followed by a straight run
    assert sum(1 for b in bulges if abs(b) > 1e-9) == 4


def test_fillet_area_and_perimeter_match_analytic_values():
    adapter = StubAdapter()
    side, radius = 100.0, 10.0
    square = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]

    vertices, bulges, _ = adapter._corner_points(square, True, radius, None)

    area = _polygon_area(vertices, bulges)
    perimeter = _polygon_perimeter(vertices, bulges)
    # four corners cut away, then four quarter circles added back
    expected_area = side**2 - 4 * (radius**2 - math.pi * radius**2 / 4)
    expected_perimeter = 4 * (side - 2 * radius) + 2 * math.pi * radius

    assert area == pytest.approx(expected_area, rel=1e-9)
    assert perimeter == pytest.approx(expected_perimeter, rel=1e-9)


def test_fillet_clamps_radius_on_short_edges():
    adapter = StubAdapter()
    narrow = [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (0.0, 4.0)]

    _, _, report = adapter._corner_points(narrow, True, 100.0, None)

    assert report, "corners should still be filleted, not skipped"
    assert all(corner["clamped"] for corner in report)
    assert all(corner["radius"] < 100.0 for corner in report)


def test_fillet_only_selected_vertices():
    adapter = StubAdapter()
    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]

    vertices, _, report = adapter._corner_points(square, True, 5.0, [1])

    assert [corner["vertex"] for corner in report] == [1]
    assert len(vertices) == 5  # 4 original corners, one replaced by two tangent points


def test_chamfer_has_no_bulges():
    adapter = StubAdapter()
    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]

    vertices, bulges, report = adapter._corner_points(square, True, 10.0, None, straight=True)

    assert len(vertices) == 8
    assert all(abs(b) < 1e-12 for b in bulges)
    assert len(report) == 4
    # chamfered square: area = side^2 - 4 * d^2/2
    assert _polygon_area(vertices) == pytest.approx(100**2 - 4 * 10**2 / 2)


def test_collinear_vertices_are_left_alone():
    adapter = StubAdapter()
    straight_run = [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0), (100.0, 50.0)]

    vertices, _, report = adapter._corner_points(straight_run, False, 5.0, None)

    # the (50, 0) vertex is collinear -> untouched, so nothing is filleted there
    assert (50.0, 0.0) in vertices
    assert all(corner["vertex"] != 1 for corner in report)


def test_open_polyline_keeps_end_points():
    adapter = StubAdapter()
    path = [(0.0, 0.0), (50.0, 50.0), (100.0, 0.0)]

    vertices, bulges, report = adapter._corner_points(path, False, 10.0, None)

    assert vertices[0] == (0.0, 0.0)
    assert vertices[-1] == (100.0, 0.0)
    assert len(report) == 1
    assert len(vertices) == len(bulges)


# ========== segment decoding ==========


def test_segment_of_line():
    adapter = StubAdapter()
    entity = FakeEntity("AcDbLine", StartPoint=(1.0, 2.0, 0.0), EndPoint=(3.0, 6.0, 0.0))

    segment = adapter._segment_of(entity)

    assert segment["kind"] == "line"
    assert segment["start"] == (1.0, 2.0)
    assert segment["end"] == (3.0, 6.0)


def test_segment_of_arc_wraps_angles_past_zero():
    adapter = StubAdapter()
    # Quarter arc from -90 to 0 degrees (radians), i.e. the bottom-right fillet
    entity = FakeEntity(
        "AcDbArc",
        Center=(0.0, 0.0, 0.0),
        Radius=1.5,
        StartAngle=-math.pi / 2,
        EndAngle=0.0,
    )

    segment = adapter._segment_of(entity)

    assert segment["kind"] == "arc"
    assert segment["sweep"] == pytest.approx(math.pi / 2)
    assert segment["start"] == pytest.approx((0.0, -1.5))
    assert segment["end"] == pytest.approx((1.5, 0.0))


def test_segment_of_unsupported_entity_returns_none():
    adapter = StubAdapter()
    assert adapter._segment_of(FakeEntity("AcDbCircle")) is None


# ========== polyline decoding ==========


def test_classic_2d_polyline_duplicate_close_point_is_dropped():
    adapter = StubAdapter()
    entity = FakeEntity(
        "AcDb2dPolyline",
        # 5 vertices, the last one repeating the first
        Coordinates=(0.0, 20.0, 0.0, 20.0, 20.0, 0.0, 20.0, 30.0, 0.0, 0.0, 30.0, 0.0, 0.0, 20.0, 0.0),
        Closed=True,
        GetBulge=lambda index: 0.0,
    )

    vertices, bulges, closed = adapter._polyline_data(entity)

    assert closed is True
    assert len(vertices) == 4
    assert len(bulges) == 4


def test_lightweight_polyline_reads_pairs():
    adapter = StubAdapter()
    entity = FakeEntity(
        "AcDbPolyline",
        Coordinates=(0.0, 0.0, 10.0, 0.0, 10.0, 10.0),
        Closed=True,
        GetBulge=lambda index: 0.4 if index == 1 else 0.0,
    )

    vertices, bulges, closed = adapter._polyline_data(entity)

    assert vertices == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    assert bulges == [0.0, 0.4, 0.0]
    assert closed is True


# ========== helpers ==========


def _bulge_area_and_length(p1, p2, bulge):
    """Area between chord and arc, plus the arc length, for one polyline segment."""
    chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if abs(bulge) < 1e-12 or chord < 1e-12:
        return 0.0, chord
    theta = 4.0 * math.atan(bulge)          # signed included angle
    radius = chord / (2.0 * math.sin(theta / 2.0))
    area = (radius**2 / 2.0) * (theta - math.sin(theta))
    return area, radius * theta


def _polygon_area(vertices, bulges=None):
    """Signed area of a polyline: shoelace over chords plus bulged segment areas."""
    bulges = bulges or [0.0] * len(vertices)
    area = 0.0
    count = len(vertices)
    for index in range(count):
        x1, y1 = vertices[index]
        x2, y2 = vertices[(index + 1) % count]
        area += x1 * y2 - x2 * y1
        segment_area, _ = _bulge_area_and_length(vertices[index], vertices[(index + 1) % count], bulges[index])
        area += segment_area * 2.0          # shoelace counts half, segments count once
    return abs(area) / 2.0


def _polygon_perimeter(vertices, bulges=None):
    bulges = bulges or [0.0] * len(vertices)
    total = 0.0
    count = len(vertices)
    for index in range(count):
        _, length = _bulge_area_and_length(
            vertices[index], vertices[(index + 1) % count], bulges[index]
        )
        total += length
    return total
