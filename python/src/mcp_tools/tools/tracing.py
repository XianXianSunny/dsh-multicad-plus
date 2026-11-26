"""
Raster tracing helpers plus the ``trace_image`` tool.

Guessing coordinates by looking at a picture is the most error-prone way to
redraw a logo or a reference drawing: proportions drift, holes are missed and
diagonals get flattened. This module measures instead of guessing. It
thresholds the bitmap, walks the real pixel boundaries, simplifies them into
straight segments and snaps them to horizontal / vertical / 45 degrees - the
same result a careful manual trace would produce, but immune to eyeballing.

Deliberately dependency-light: only Pillow is used (the connector's virtualenv
does not ship numpy), so tracing never needs a second environment.
"""

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

# A pixel boundary shorter than this (in px) counts as axis aligned.
_AXIS_TOLERANCE = 1.5
# Largest deviation from horizontal / vertical that still counts as an axis.
_AXIS_ANGLE_DEG = 3.5
# Largest deviation from 45 deg that still gets snapped, in degrees.
_SNAP_TOLERANCE_DEG = 8.0


# ========== Image handling ==========


def _load_gray(image_path: str):
    """Open ``image_path`` as 8-bit grayscale, compositing transparency on white."""
    from PIL import Image

    image = Image.open(image_path)
    if image.mode in ("RGBA", "LA", "P", "PA"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image)
    return image.convert("L")


def _otsu_threshold(histogram: Sequence[int]) -> int:
    """Return Otsu's threshold for a 256-bin histogram."""
    total = sum(histogram)
    if total <= 0:
        return 128

    sum_all = sum(index * count for index, count in enumerate(histogram))
    sum_background = 0.0
    weight_background = 0
    best_variance = -1.0
    threshold = 128

    for index, count in enumerate(histogram):
        weight_background += count
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break
        sum_background += index * count
        mean_background = sum_background / weight_background
        mean_foreground = (sum_all - sum_background) / weight_foreground
        variance = (
            weight_background
            * weight_foreground
            * (mean_background - mean_foreground) ** 2
        )
        if variance > best_variance:
            best_variance = variance
            threshold = index

    return threshold


def _build_mask(
    image, threshold: int, invert: bool
) -> Tuple[bytes, int, int, int]:
    """Build a 1-byte-per-pixel ink mask.

    Returns:
        ``(mask, width, height, used_threshold)``. ``threshold <= 0`` means
        "pick one with Otsu"; Otsu's boundary level is inclusive (class 0 is
        ``0 .. t``), otherwise an image whose only levels are 0 and 255 would
        produce an empty mask.
    """
    width, height = image.size

    if threshold <= 0:
        used_threshold = _otsu_threshold(image.histogram())
        used_threshold = max(0, min(254, used_threshold))
        table = bytes(1 if value < used_threshold else 0 for value in range(256))
        raw = image.tobytes()
        mask = raw.translate(table)
        if not mask.count(1):
            # Two-level source (0 / 255): Otsu lands on the dark level itself,
            # and a strict comparison would select nothing. Include it.
            table = bytes(1 if value <= used_threshold else 0 for value in range(256))
    else:
        used_threshold = threshold
        table = bytes(1 if value < used_threshold else 0 for value in range(256))
        raw = image.tobytes()

    if invert:
        table = bytes(1 - value for value in table)

    return raw.translate(table), width, height, used_threshold


# ========== Boundary tracing ==========


def _trace_loops(mask: bytes, width: int, height: int) -> List[List[Point]]:
    """Walk every boundary loop of the mask, expressed on the pixel corner grid.

    Each ink pixel contributes the edges that separate it from background, so
    the walk produces exact staircases around outer contours *and* around holes.
    """
    edges = set()
    for index, value in enumerate(mask):
        if not value:
            continue
        x = index % width
        y = index // width
        if y == 0 or not mask[index - width]:
            edges.add(((x, y), (x + 1, y)))
        if y == height - 1 or not mask[index + width]:
            edges.add(((x + 1, y + 1), (x, y + 1)))
        if x == 0 or not mask[index - 1]:
            edges.add(((x, y + 1), (x, y)))
        if x == width - 1 or not mask[index + 1]:
            edges.add(((x + 1, y), (x + 1, y + 1)))

    if not edges:
        return []

    adjacency: Dict[Point, List[Point]] = {}
    for start, end in edges:
        adjacency.setdefault(start, []).append(end)
        adjacency.setdefault(end, []).append(start)

    used = set()
    loops: List[List[Point]] = []

    for start, end in sorted(edges):
        if (start, end) in used:
            continue

        loop: List[Point] = [start]
        previous, current = start, end
        used.add((start, end))
        used.add((end, start))

        while current != start:
            loop.append(current)
            candidates = [
                neighbour
                for neighbour in adjacency[current]
                if (current, neighbour) not in used
            ]
            if not candidates:
                break

            direction_x = current[0] - previous[0]
            direction_y = current[1] - previous[1]

            def turn_rank(neighbour: Point) -> int:
                step_x = neighbour[0] - current[0]
                step_y = neighbour[1] - current[1]
                cross = direction_x * step_y - direction_y * step_x
                if cross < 0:  # right turn keeps outer/inner orientation stable
                    return 0
                if cross > 0:
                    return 2
                return 1

            next_point = min(candidates, key=turn_rank)
            used.add((current, next_point))
            used.add((next_point, current))
            previous, current = current, next_point

        if len(loop) > 7:
            loops.append(loop)

    return loops


# ========== Simplification ==========


def _median(values: Sequence[float]) -> float:
    """Median of a sequence of numbers (empty input returns 0.0)."""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    middle = count // 2
    if count % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    return abs(dx * (point[1] - start[1]) - dy * (point[0] - start[0])) / length


def _simplify_indices(points: Sequence[Point], tolerance: float) -> List[int]:
    """Ramer-Douglas-Peucker simplification returning the kept indices."""
    if len(points) < 3:
        return list(range(len(points)))

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst_index = -1
        worst_distance = 0.0
        for index in range(first + 1, last):
            distance = _point_line_distance(points[index], points[first], points[last])
            if distance > worst_distance:
                worst_distance = distance
                worst_index = index
        if worst_index >= 0 and worst_distance > tolerance:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))

    return [index for index, kept in enumerate(keep) if kept]


def _segment_between(points: Sequence[Point], start: int, end: int) -> List[Point]:
    """Slice the traced staircase between two indices, wrapping at the end."""
    if start <= end:
        return list(points[start : end + 1])
    return list(points[start:]) + list(points[: end + 1])


def _drop_subtolerance_vertices(
    points: Sequence[Point], tolerance: float
) -> List[Point]:
    """Greedily remove vertices whose removal moves the outline by < tolerance.

    The one-pass RDP above works on open runs and can therefore keep a 1-2 px
    jog (the place where a diagonal meets an axis) because the chord it is
    measured against spans two real corners. Removing the *cheapest* vertex
    first, repeatedly, is the standard fix and keeps every real corner: a
    genuine corner deviates by far more than the tolerance.
    """
    remaining = list(points)
    while len(remaining) > 3:
        count = len(remaining)
        best_index = -1
        best_deviation = None

        for index in range(count):
            previous = remaining[index - 1]
            current = remaining[index]
            following = remaining[(index + 1) % count]
            deviation = _point_line_distance(current, previous, following)
            if deviation <= tolerance and (
                best_deviation is None or deviation < best_deviation
            ):
                best_deviation = deviation
                best_index = index

        if best_index < 0:
            break
        remaining.pop(best_index)

    return remaining


def _fit_direction(segment: Sequence[Point]) -> Tuple[Point, Point]:
    """Least-squares line through a staircase segment: (centroid, unit vector).

    Fitting the whole staircase instead of its two end points keeps long
    diagonals centred on the ink; averaging only the ends biased a traced
    diagonal by half a pixel.
    """
    count = len(segment)
    if count < 2:
        return ((0.0, 0.0), (1.0, 0.0))

    mean_x = sum(point[0] for point in segment) / count
    mean_y = sum(point[1] for point in segment) / count
    sxx = sum((point[0] - mean_x) ** 2 for point in segment)
    syy = sum((point[1] - mean_y) ** 2 for point in segment)
    sxy = sum((point[0] - mean_x) * (point[1] - mean_y) for point in segment)

    if sxx + syy < 1e-9:
        return ((mean_x, mean_y), (1.0, 0.0))

    angle = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    direction = (math.cos(angle), math.sin(angle))
    if sxx > syy and direction[0] < 0:
        direction = (-direction[0], -direction[1])
    if syy > sxx and direction[1] < 0:
        direction = (-direction[0], -direction[1])

    return ((mean_x, mean_y), direction)


def _classify_edges(
    vertices: Sequence[Point], indices: Sequence[int], snap: bool
) -> List[Tuple[str, Point, Point]]:
    """Turn the simplified edges into fixed directions (line anchor + unit vector)."""
    lines: List[Tuple[str, Point, Point]] = []
    count = len(indices)

    for position in range(count):
        start_index = indices[position]
        end_index = indices[(position + 1) % count]
        segment = _segment_between(vertices, start_index, end_index)
        if len(segment) < 2:
            continue

        anchor, direction = _fit_direction(segment)
        dx = segment[-1][0] - segment[0][0]
        dy = segment[-1][1] - segment[0][1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue

        # Classify by angle, not by raw offset: a 90 px long side whose
        # staircase bleeds two rows at a corner is still horizontal, while a
        # 3 px edge with a 2 px rise is not.
        angle = abs(math.degrees(math.atan2(dy, dx)))
        if angle > 90.0:
            angle = 180.0 - angle

        # Axis-aligned edges are measured with the median: a staircase that
        # runs along one pixel row is exactly that row, while the mean drifts
        # where the corner steps bleed into the segment.
        if angle <= _AXIS_ANGLE_DEG or abs(dy) <= _AXIS_TOLERANCE:
            lines.append(("H", (0.0, _median(point[1] for point in segment)), (1.0, 0.0)))
            continue
        if angle >= 90.0 - _AXIS_ANGLE_DEG or abs(dx) <= _AXIS_TOLERANCE:
            lines.append(("V", (_median(point[0] for point in segment), 0.0), (0.0, 1.0)))
            continue

        if snap and abs(angle - 45.0) <= _SNAP_TOLERANCE_DEG:
            sign_x = 1.0 if dx > 0 else -1.0
            sign_y = 1.0 if dy > 0 else -1.0
            lines.append(
                ("D", anchor, (sign_x / math.sqrt(2.0), sign_y / math.sqrt(2.0)))
            )
        else:
            lines.append(("S", anchor, direction))

    return lines

def _intersect(
    first: Tuple[str, Point, Point], second: Tuple[str, Point, Point]
) -> Point:
    """Intersect two infinite lines given as (anchor, direction)."""
    _, anchor_a, direction_a = first
    _, anchor_b, direction_b = second

    determinant = direction_a[0] * (-direction_b[1]) - (-direction_b[0]) * direction_a[1]
    if abs(determinant) < 1e-12:
        return anchor_a

    delta_x = anchor_b[0] - anchor_a[0]
    delta_y = anchor_b[1] - anchor_a[1]
    parameter = (delta_x * (-direction_b[1]) - (-direction_b[0]) * delta_y) / determinant
    return (
        anchor_a[0] + direction_a[0] * parameter,
        anchor_a[1] + direction_a[1] * parameter,
    )


def _rebuild_polygon(
    vertices: Sequence[Point], snap: bool, tolerance: float
) -> List[Point]:
    """Simplify, snap and re-intersect a traced loop into clean vertices."""
    indices = _simplify_indices(vertices, tolerance)

    # A traced loop stops one pixel step before its first point. That last
    # step is the closing edge, not a vertex: keeping it turns a 1 px stub into
    # a fake horizontal/vertical segment, and intersecting neighbours with that
    # bogus line produced stray vertices such as (0, 489).
    while len(indices) > 1:
        first = vertices[indices[0]]
        last = vertices[indices[-1]]
        if abs(first[0] - last[0]) + abs(first[1] - last[1]) <= 1.0:
            indices = indices[:-1]
        else:
            break

    if len(indices) < 3:
        return []

    lines = _classify_edges(vertices, indices, snap)
    if len(lines) < 3:
        return []

    polygon: List[Point] = []
    for index in range(len(lines)):
        polygon.append(_intersect(lines[index], lines[(index + 1) % len(lines)]))

    # Rounded to keep JSON compact, then de-duplicated: two collinear edges in
    # the source (a 1 px jog on a long side) collapse onto the same vertex.
    rounded = [(round(x, 4), round(y, 4)) for x, y in polygon]
    cleaned: List[Point] = []
    for point in rounded:
        if cleaned and abs(cleaned[-1][0] - point[0]) < 1e-6 and abs(
            cleaned[-1][1] - point[1]
        ) < 1e-6:
            continue
        cleaned.append(point)

    if len(cleaned) > 1 and abs(cleaned[0][0] - cleaned[-1][0]) < 1e-6 and abs(
        cleaned[0][1] - cleaned[-1][1]
    ) < 1e-6:
        cleaned.pop()

    if len(cleaned) < 3:
        return []

    # Final pass: 1-2 px staircase leftovers (a tiny jog where a diagonal meets
    # an axis) are below the requested tolerance, so they are not geometry.
    cleaned = _drop_subtolerance_vertices(cleaned, tolerance)
    return cleaned if len(cleaned) >= 3 else []


def _polygon_area(points: Sequence[Point]) -> float:
    """Signed area (shoelace); positive and negative identify outer/inner loops."""
    area = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        area += x1 * y2 - x2 * y1
    return area / 2.0


# ========== Public vectoriser ==========


def vectorize_image(
    image_path: str,
    threshold: int = 0,
    invert: bool = False,
    tolerance: float = 1.5,
    min_area: float = 30.0,
    snap: bool = True,
) -> Dict[str, Any]:
    """Vectorise the ink shapes of a raster image into closed polygons.

    Args:
        image_path: Path to a Pillow-readable image.
        threshold: 0-255 cut-off; 0 picks an Otsu threshold automatically.
        invert: Trace light shapes on a dark background instead of dark on light.
        tolerance: Simplification tolerance in pixels (1.0-2.5 is a good range).
        min_area: Polygons smaller than this (px²) are discarded as noise.
        snap: Snap near-axis and near-45-degree edges to exact directions.

    Returns:
        Dict with ``width``, ``height``, ``threshold``, ``polygons`` (each with
        ``points`` as pixel coordinates, ``area`` and ``role``) and ``iou``.
    """
    from PIL import Image, ImageDraw

    path = Path(image_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    gray = _load_gray(str(path))
    mask, width, height, used_threshold = _build_mask(gray, threshold, invert)

    polygons: List[Dict[str, Any]] = []
    for loop in _trace_loops(mask, width, height):
        rebuilt = _rebuild_polygon(loop, snap, tolerance)
        if len(rebuilt) < 3:
            continue
        area = _polygon_area(rebuilt)
        if abs(area) < min_area:
            continue
        polygons.append(
            {
                "points": rebuilt,
                "area": round(area, 3),
                "role": "outer" if area > 0 else "hole",
            }
        )

    polygons.sort(key=lambda item: -abs(item["area"]))

    # Round-trip check: rasterise the result and compare it with the source mask.
    iou = 0.0
    try:
        probe = Image.new("L", (width, height), 0)
        drawer = ImageDraw.Draw(probe)
        for item in polygons:
            fill = 255 if item["role"] == "outer" else 0
            drawer.polygon([tuple(point) for point in item["points"]], fill=fill)
        result_mask = probe.tobytes()
        intersection = 0
        union = 0
        for original, rebuilt_value in zip(mask, result_mask):
            a = 1 if original else 0
            b = 1 if rebuilt_value else 0
            if a or b:
                union += 1
                if a and b:
                    intersection += 1
        iou = intersection / union if union else 0.0
    except Exception as error:  # pragma: no cover - validation is best effort
        logger.debug(f"Trace validation skipped: {error}")

    return {
        "width": width,
        "height": height,
        "threshold": used_threshold,
        "polygons": polygons,
        "iou": round(iou, 4),
    }


def _transform(
    points: Sequence[Point],
    scale: float,
    origin_x: float,
    origin_y: float,
    image_height: int,
) -> List[List[float]]:
    """Map pixel coordinates (y down) to drawing units (y up)."""
    transformed = []
    for x, y in points:
        transformed.append(
            [
                round(origin_x + x * scale, 4),
                round(origin_y + (image_height - y) * scale, 4),
            ]
        )
    return transformed


# ========== Tool registration ==========


def register_tracing_tools(mcp):
    """Register the ``trace_image`` tool."""

    @mcp.tool()
    def trace_image(
        image_path: str,
        mode: str = "preview",
        scale: float = 1.0,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        anchor: str = "bbox",
        threshold: int = 0,
        invert: bool = False,
        tolerance: float = 1.5,
        min_area: float = 30.0,
        snap: bool = True,
        layer: str = "0",
        color: str = "white",
        lineweight: int = 0,
        max_vertices: int = 4000,
    ) -> str:
        """Vectorise a raster image into closed polylines (outline tracing).

        Use this instead of eyeballing an image and typing coordinates: the
        contours are measured from the pixels, so proportions, holes and
        diagonals are correct by construction.

        Args:
            image_path: Absolute path to a PNG/JPG/BMP image.
            mode: "preview" (default) returns the polygons as JSON without
                touching CAD; "draw" also creates closed polylines in the
                active drawing.
            scale: Drawing units per pixel (default 1.0). A 64 x 36 px logo at
                scale 0.5 becomes 32 x 18 units.
            origin_x: X of the image's bottom-left corner in drawing units.
            origin_y: Y of the image's bottom-left corner in drawing units.
            anchor: "bbox" (default) moves the traced content so its own
                bottom-left corner sits on the origin - what you want for a
                delivery. "image" keeps the picture frame, so a logo that sits
                in the middle of a 192 px canvas keeps its offset.
            threshold: 0-255 ink cut-off; 0 auto-selects Otsu.
            invert: Trace light shapes on a dark background.
            tolerance: Simplification tolerance in pixels (default 1.5; use
                0.8 for fine detail, 3.0 for a coarse sketch).
            min_area: Drop polygons smaller than this many px² (default 30).
            snap: Snap near-axis / near-45-degree edges to exact directions.
            layer: Layer for draw mode (created automatically when missing).
            color: Colour name or ACI index for draw mode.
            lineweight: Line weight in hundredths of a mm (0 = default).
            max_vertices: Refuse to return more vertices than this, so a noisy
                photo cannot flood the context.

        Returns:
            JSON string with the polygons, their areas, the bounding box, the
            round-trip fit (``iou``) and, in draw mode, the created handles.
        """
        try:
            result = vectorize_image(
                image_path,
                threshold=threshold,
                invert=invert,
                tolerance=tolerance,
                min_area=min_area,
                snap=snap,
            )
        except Exception as error:
            return json.dumps({"success": False, "error": str(error)}, indent=2)

        polygons = result["polygons"]
        total_vertices = sum(len(item["points"]) for item in polygons)
        if total_vertices > max_vertices:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"{total_vertices} vertices exceed max_vertices="
                        f"{max_vertices}; raise tolerance, raise min_area or "
                        "split the image"
                    ),
                    "width": result["width"],
                    "height": result["height"],
                },
                indent=2,
            )

        image_height = result["height"]
        payload: List[Dict[str, Any]] = []
        for index, item in enumerate(polygons):
            points = _transform(
                item["points"], scale, origin_x, origin_y, image_height
            )
            payload.append(
                {
                    "index": index,
                    "role": item["role"],
                    "area": round(abs(item["area"]) * scale * scale, 4),
                    "vertices": len(points),
                    "points": points,
                }
            )

        # Anchoring: a delivery wants the drawing itself on the origin, not the
        # white margin of the source picture.
        if payload and (anchor or "bbox").lower() == "bbox":
            shift_x = min(point[0] for item in payload for point in item["points"])
            shift_y = min(point[1] for item in payload for point in item["points"])
            if shift_x or shift_y:
                for item in payload:
                    item["points"] = [
                        [round(x - shift_x, 4), round(y - shift_y, 4)]
                        for x, y in item["points"]
                    ]

        xs = [point[0] for item in payload for point in item["points"]]
        ys = [point[1] for item in payload for point in item["points"]]
        bbox = (
            [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]
            if xs
            else [0.0, 0.0, 0.0, 0.0]
        )

        response: Dict[str, Any] = {
            "success": True,
            "mode": mode,
            "image": {
                "path": str(Path(image_path).expanduser()),
                "width": result["width"],
                "height": result["height"],
                "threshold": result["threshold"],
            },
            "units": {"scale": scale, "origin": [origin_x, origin_y], "bbox": bbox},
            "fit": {"iou": result["iou"]},
            "count": len(payload),
            "polygons": payload,
        }

        if result["iou"] < 0.9:
            response["warning"] = (
                "Round-trip fit is low (iou < 0.9): check threshold/tolerance, or "
                "the source is too blurry to trace reliably."
            )

        if mode.lower() != "draw":
            return json.dumps(response, indent=2)

        try:
            from adapters.adapter_manager import get_adapter
            from mcp_tools.decorators import set_current_adapter

            adapter = get_adapter(None)
            set_current_adapter(adapter)

            handles = []
            for item in payload:
                handle = adapter.draw_polyline(
                    [tuple(point) for point in item["points"]],
                    True,
                    layer,
                    color,
                    lineweight,
                    _skip_refresh=True,
                )
                handles.append(handle)
                item["handle"] = handle

            try:
                adapter.refresh_view()
            except Exception as error:  # pragma: no cover
                logger.debug(f"Refresh after trace skipped: {error}")

            response["drawn"] = {"count": len(handles), "handles": handles, "layer": layer}
        except Exception as error:
            response["success"] = False
            response["error"] = f"Drawing failed: {error}"

        return json.dumps(response, indent=2)
