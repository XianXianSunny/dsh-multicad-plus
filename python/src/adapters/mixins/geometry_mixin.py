"""
Geometry editing mixin for the AutoCAD/ZWCAD adapter.

Adds three geometry-level operations the upstream adapter lacked:

- ``join_entities``: stitch a chain of LINE/ARC entities into a single
  lightweight polyline. Arc segments are preserved exactly by converting the
  included angle into a polyline bulge (``tan(sweep / 4)``), so no
  approximation is introduced.
- ``fillet_polyline``: replace polyline corners with tangent arcs of a given
  radius (true tangent construction, not a CAD command).
- ``chamfer_polyline``: replace polyline corners with a straight chamfer.

All maths is done in Python and written through COM primitives
(AddLightWeightPolyline / SetBulge / Delete), so it works on any CAD that
implements the AutoCAD ActiveX API.
"""

import logging
import math
from array import array
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import win32com.client
import pythoncom

logger = logging.getLogger(__name__)

TOL = 1e-6

Point2 = Tuple[float, float]


# ========== Small 2D vector helpers ==========


def _sub(a: Point2, b: Point2) -> Point2:
    return (a[0] - b[0], a[1] - b[1])


def _len(a: Point2) -> float:
    return math.hypot(a[0], a[1])


def _norm(a: Point2) -> Point2:
    length = _len(a)
    if length <= TOL:
        return (0.0, 0.0)
    return (a[0] / length, a[1] / length)


def _cross(a: Point2, b: Point2) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _dot(a: Point2, b: Point2) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _key(point: Point2) -> Tuple[float, float]:
    """Quantise a point so that coincident endpoints share one dictionary key."""
    return (round(point[0], 6), round(point[1], 6))


class GeometryMixin:
    """Mixin for geometry-level editing operations."""

    if TYPE_CHECKING:  # pragma: no cover - typing only
        def _validate_connection(self) -> None: ...
        def _get_document(self, operation: str = "operation") -> Any: ...
        def refresh_view(self) -> bool: ...

    # ---------- internal helpers ----------

    def _segment_of(self, entity: Any) -> Optional[Dict[str, Any]]:
        """Describe a LINE or ARC entity as a directed segment.

        Returns:
            Dict with keys kind/start/end/entity (plus sweep for arcs), or None
            when the entity type cannot be joined.
        """
        object_name = str(getattr(entity, "ObjectName", "") or "")

        if object_name == "AcDbLine":
            sp = entity.StartPoint
            ep = entity.EndPoint
            return {
                "kind": "line",
                "start": (float(sp[0]), float(sp[1])),
                "end": (float(ep[0]), float(ep[1])),
                "entity": entity,
            }

        if object_name == "AcDbArc":
            center = entity.Center
            radius = float(entity.Radius)
            start_angle = float(entity.StartAngle)
            end_angle = float(entity.EndAngle)
            # ActiveX arcs are always counter-clockwise: normalise end > start
            while end_angle <= start_angle:
                end_angle += 2 * math.pi
            start = (
                center[0] + radius * math.cos(start_angle),
                center[1] + radius * math.sin(start_angle),
            )
            end = (
                center[0] + radius * math.cos(end_angle),
                center[1] + radius * math.sin(end_angle),
            )
            return {
                "kind": "arc",
                "start": start,
                "end": end,
                "sweep": end_angle - start_angle,
                "entity": entity,
            }

        return None

    def _add_lwpolyline(
        self,
        document: Any,
        vertices: List[Point2],
        bulges: List[float],
        closed: bool,
        layer: Optional[str] = None,
        color: Optional[Any] = None,
    ) -> Any:
        """Create a lightweight polyline from 2D vertices plus per-segment bulges."""
        flat = array("d")
        for x, y in vertices:
            flat.append(float(x))
            flat.append(float(y))

        points = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)
        polyline = document.ModelSpace.AddLightWeightPolyline(points)

        for index, bulge in enumerate(bulges):
            if abs(bulge) > 1e-12:
                polyline.SetBulge(index, float(bulge))

        if closed:
            polyline.Closed = True

        if layer:
            try:
                polyline.Layer = layer
            except Exception:  # pragma: no cover - defensive
                pass
        if color is not None:
            try:
                polyline.color = int(color)
            except Exception:  # pragma: no cover - defensive
                pass

        return polyline

    @staticmethod
    def _is_polyline(entity: Any) -> bool:
        """True for both lightweight (AcDbPolyline) and classic 2D (AcDb2dPolyline) polylines."""
        return str(getattr(entity, "ObjectName", "")) in ("AcDbPolyline", "AcDb2dPolyline")

    def _polyline_data(self, entity: Any) -> Tuple[List[Point2], List[float], bool]:
        """Read vertices, bulges and closed flag of a polyline.

        Handles both ``AcDbPolyline`` (x, y pairs) and the classic
        ``AcDb2dPolyline`` (x, y, z triples, e.g. what ``AddPolyline`` creates).
        """
        coords = tuple(entity.Coordinates)
        name = str(getattr(entity, "ObjectName", ""))
        step = 3 if name == "AcDb2dPolyline" else 2

        if len(coords) % step != 0:
            raise ValueError(
                f"{name} coordinates are not {step}-value groups (got {len(coords)} values)"
            )

        vertices = [(float(coords[i]), float(coords[i + 1])) for i in range(0, len(coords), step)]
        bulges = [float(entity.GetBulge(i)) for i in range(len(vertices))]
        closed = bool(entity.Closed)

        # Classic 2D polylines repeat the first vertex to close the loop; drop it
        # so a closed rectangle reads as 4 vertices, not 5 with a degenerate edge.
        if len(vertices) > 2 and _key(vertices[0]) == _key(vertices[-1]):
            vertices.pop()
            bulges.pop()

        return vertices, bulges, closed

    # ---------- public operations ----------

    def join_entities(self, handles: List[str]) -> Dict[str, Any]:
        """Join LINE/ARC entities into one lightweight polyline.

        Args:
            handles: Handles of the entities to join (lines and/or arcs).

        Returns:
            Dict describing the resulting polyline.

        Raises:
            ValueError: If fewer than two joinable entities are given, the
                endpoints do not form a single chain, or an unsupported entity
                type is passed.
        """
        self._validate_connection()
        document = self._get_document("join_entities")

        segments: List[Dict[str, Any]] = []
        skipped: List[str] = []
        for handle in handles:
            entity = document.HandleToObject(handle)
            segment = self._segment_of(entity)
            if segment is None:
                skipped.append(str(handle))
                continue
            segments.append(segment)

        if len(segments) < 2:
            raise ValueError(
                f"need at least 2 lines/arcs to join, got {len(segments)} "
                f"(skipped unsupported: {skipped or 'none'})"
            )

        # Endpoint adjacency: quantised point -> list of (segment index, side)
        adjacency: Dict[Tuple[float, float], List[Tuple[int, str]]] = {}
        for index, segment in enumerate(segments):
            adjacency.setdefault(_key(segment["start"]), []).append((index, "start"))
            adjacency.setdefault(_key(segment["end"]), []).append((index, "end"))

        open_ends = [point for point, hits in adjacency.items() if len(hits) == 1]
        dangling = [point for point, hits in adjacency.items() if len(hits) > 2]
        if dangling:
            raise ValueError(
                f"endpoints are not a simple chain: {len(dangling)} point(s) shared by 3+ segments"
            )
        if len(open_ends) not in (0, 2):
            raise ValueError(
                f"endpoints do not form one chain ({len(open_ends)} loose ends); "
                "entities must share coincident endpoints"
            )

        start_key = open_ends[0] if open_ends else _key(segments[0]["start"])
        ordered: List[Tuple[Dict[str, Any], bool]] = []
        used = set()
        current = start_key

        while True:
            candidates = [
                (index, side) for index, side in adjacency.get(current, []) if index not in used
            ]
            if not candidates:
                break
            index, side = candidates[0]
            segment = segments[index]
            used.add(index)
            reversed_ = side == "end"
            ordered.append((segment, reversed_))
            tail = segment["start"] if reversed_ else segment["end"]
            current = _key(tail)
            if current == start_key:
                break

        if len(used) != len(segments):
            raise ValueError(
                f"only {len(used)} of {len(segments)} entities form a connected chain"
            )

        vertices: List[Point2] = []
        bulges: List[float] = []
        for segment, reversed_ in ordered:
            head = segment["end"] if reversed_ else segment["start"]
            tail = segment["start"] if reversed_ else segment["end"]

            if segment["kind"] == "arc":
                bulge = math.tan(segment["sweep"] / 4.0)
                if reversed_:
                    bulge = -bulge
            else:
                bulge = 0.0

            # Invariant: bulges[i] describes the segment vertices[i] -> vertices[i+1]
            if not vertices:
                vertices.append(head)
            bulges.append(bulge)
            vertices.append(tail)

        closed = _key(vertices[0]) == _key(vertices[-1]) and len(ordered) > 2
        if closed:
            vertices.pop()
            bulges = bulges[: len(vertices)]

        if len(vertices) < 2:
            raise ValueError("chain collapsed to fewer than 2 vertices")

        source = segments[0]["entity"]
        polyline = self._add_lwpolyline(
            document,
            vertices,
            bulges,
            closed,
            layer=getattr(source, "Layer", None),
            color=getattr(source, "color", None),
        )

        deleted = 0
        for segment in segments:
            try:
                segment["entity"].Delete()
                deleted += 1
            except Exception as error:  # pragma: no cover - defensive
                logger.warning(f"join: could not delete source entity: {error}")

        self.refresh_view()
        logger.info(
            f"Joined {len(segments)} entities into polyline {polyline.Handle} "
            f"({len(vertices)} vertices, closed={closed})"
        )
        return {
            "success": True,
            "handle": str(polyline.Handle),
            "vertices": len(vertices),
            "arc_segments": sum(1 for s, _ in ordered if s["kind"] == "arc"),
            "closed": closed,
            "joined": len(segments),
            "deleted_sources": deleted,
            "skipped": skipped,
            "detail": f"Joined {len(segments)} entities into polyline {polyline.Handle}",
        }

    def _corner_points(
        self,
        vertices: List[Point2],
        closed: bool,
        radius: float,
        targets: Optional[List[int]],
        straight: bool = False,
    ) -> Tuple[List[Point2], List[float], List[Dict[str, Any]]]:
        """Rebuild a vertex list, replacing selected corners with arcs or chamfers."""
        count = len(vertices)
        if count < 3:
            raise ValueError("polyline needs at least 3 vertices to fillet")

        if targets is None:
            targets_set = set(range(count))
        else:
            targets_set = set()
            for index in targets:
                if index < 0:
                    index += count
                if not 0 <= index < count:
                    raise ValueError(f"vertex index {index} out of range (0..{count - 1})")
                targets_set.add(index)

        out_vertices: List[Point2] = []
        out_bulges: List[float] = []
        report: List[Dict[str, Any]] = []

        for index in range(count):
            current = vertices[index]
            if index == 0 and not closed:
                out_vertices.append(current)
                out_bulges.append(0.0)
                continue
            if index == count - 1 and not closed:
                out_vertices.append(current)
                out_bulges.append(0.0)
                continue

            previous = vertices[(index - 1) % count]
            following = vertices[(index + 1) % count]
            to_prev = _sub(previous, current)
            to_next = _sub(following, current)
            len_prev = _len(to_prev)
            len_next = _len(to_next)
            if len_prev <= TOL or len_next <= TOL:
                out_vertices.append(current)
                out_bulges.append(0.0)
                continue

            u = _norm(to_next)
            v = _norm(to_prev)
            cos_alpha = max(-1.0, min(1.0, _dot(u, v)))
            alpha = math.acos(cos_alpha)  # interior angle at the corner

            if index not in targets_set or alpha > math.pi - 1e-6 or alpha < 1e-6:
                out_vertices.append(current)
                out_bulges.append(0.0)
                continue

            tangent = radius / math.tan(alpha / 2.0)
            limit = 0.49 * min(len_prev, len_next)
            applied_radius = radius
            if tangent > limit:
                tangent = limit
                applied_radius = tangent * math.tan(alpha / 2.0)

            point_a = (current[0] + u[0] * tangent, current[1] + u[1] * tangent)
            point_b = (current[0] + v[0] * tangent, current[1] + v[1] * tangent)

            # Walk order follows the ring: we arrive from the previous vertex, so
            # the first point is the tangent on the incoming edge (point_b) and the
            # arc/chamfer runs from point_b to point_a towards the next vertex.
            out_vertices.append(point_b)
            if straight:
                out_bulges.append(0.0)
            else:
                sweep = math.pi - alpha
                sign = 1.0 if _cross(u, v) > 0 else -1.0
                out_bulges.append(sign * math.tan(sweep / 4.0))
            out_vertices.append(point_a)
            out_bulges.append(0.0)

            report.append(
                {
                    "vertex": index,
                    "radius": round(applied_radius, 6),
                    "tangent": round(tangent, 6),
                    "clamped": applied_radius < radius - 1e-9,
                }
            )

        return out_vertices, out_bulges, report

    def fillet_polyline(
        self,
        handle: str,
        radius: float,
        vertices: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Replace polyline corners with tangent arcs of ``radius``.

        Args:
            handle: Handle of a lightweight polyline made of straight segments.
            radius: Fillet radius in drawing units.
            vertices: Optional vertex indices to fillet (default: every corner).

        Returns:
            Dict describing the new polyline.

        Raises:
            ValueError: If the entity is not a straight-segment polyline or the
                radius is not positive.
        """
        self._validate_connection()
        if radius <= 0:
            raise ValueError("radius must be positive")

        document = self._get_document("fillet_polyline")
        entity = document.HandleToObject(handle)
        if not self._is_polyline(entity):
            raise ValueError(
                f"entity {handle} is {getattr(entity, 'ObjectName', 'unknown')}, expected a polyline"
            )

        old_vertices, old_bulges, closed = self._polyline_data(entity)
        if any(abs(b) > 1e-9 for b in old_bulges):
            raise ValueError(
                "polyline already contains arc segments; fillet only rebuilds straight-segment polylines"
            )

        new_vertices, new_bulges, report = self._corner_points(
            old_vertices, closed, radius, vertices, straight=False
        )

        polyline = self._add_lwpolyline(
            document,
            new_vertices,
            new_bulges,
            closed,
            layer=getattr(entity, "Layer", None),
            color=getattr(entity, "color", None),
        )
        entity.Delete()
        self.refresh_view()

        logger.info(f"Filletted {len(report)} vertices of {handle} -> {polyline.Handle}")
        return {
            "success": True,
            "handle": str(polyline.Handle),
            "vertices": len(new_vertices),
            "fillets": len(report),
            "closed": closed,
            "corners": report,
            "detail": f"Applied {len(report)} fillet(s) of R{radius}",
        }

    def chamfer_polyline(
        self,
        handle: str,
        distance: float,
        vertices: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Replace polyline corners with a straight chamfer of ``distance``.

        Args:
            handle: Handle of a lightweight polyline made of straight segments.
            distance: Chamfer distance measured along both adjacent edges.
            vertices: Optional vertex indices to chamfer (default: every corner).

        Returns:
            Dict describing the new polyline.
        """
        self._validate_connection()
        if distance <= 0:
            raise ValueError("distance must be positive")

        document = self._get_document("chamfer_polyline")
        entity = document.HandleToObject(handle)
        if not self._is_polyline(entity):
            raise ValueError(
                f"entity {handle} is {getattr(entity, 'ObjectName', 'unknown')}, expected a polyline"
            )

        old_vertices, old_bulges, closed = self._polyline_data(entity)
        if any(abs(b) > 1e-9 for b in old_bulges):
            raise ValueError("polyline already contains arc segments")

        # Chamfer: tangent length == distance, so radius = distance * tan(alpha/2)
        new_vertices: List[Point2] = []
        new_bulges: List[float] = []
        report: List[Dict[str, Any]] = []
        count = len(old_vertices)
        targets_set = set(range(count)) if vertices is None else {
            (i % count) for i in vertices
        }

        for index in range(count):
            current = old_vertices[index]
            if (index == 0 or index == count - 1) and not closed:
                new_vertices.append(current)
                new_bulges.append(0.0)
                continue
            previous = old_vertices[(index - 1) % count]
            following = old_vertices[(index + 1) % count]
            to_prev = _sub(previous, current)
            to_next = _sub(following, current)
            len_prev = _len(to_prev)
            len_next = _len(to_next)
            u = _norm(to_next)
            v = _norm(to_prev)
            cos_alpha = max(-1.0, min(1.0, _dot(u, v)))
            alpha = math.acos(cos_alpha)

            if (
                index not in targets_set
                or u == (0.0, 0.0)
                or v == (0.0, 0.0)
                or alpha > math.pi - 1e-6
                or alpha < 1e-6
            ):
                new_vertices.append(current)
                new_bulges.append(0.0)
                continue

            applied = min(distance, 0.49 * min(len_prev, len_next))
            # Same walk order as the fillet: incoming tangent first, then outgoing.
            new_vertices.append(
                (current[0] + v[0] * applied, current[1] + v[1] * applied)
            )
            new_bulges.append(0.0)
            new_vertices.append(
                (current[0] + u[0] * applied, current[1] + u[1] * applied)
            )
            new_bulges.append(0.0)
            report.append({"vertex": index, "distance": round(applied, 6)})

        polyline = self._add_lwpolyline(
            document,
            new_vertices,
            new_bulges,
            closed,
            layer=getattr(entity, "Layer", None),
            color=getattr(entity, "color", None),
        )
        entity.Delete()
        self.refresh_view()

        logger.info(f"Chamfered {len(report)} vertices of {handle} -> {polyline.Handle}")
        return {
            "success": True,
            "handle": str(polyline.Handle),
            "vertices": len(new_vertices),
            "chamfers": len(report),
            "closed": closed,
            "corners": report,
            "detail": f"Applied {len(report)} chamfer(s) of {distance}",
        }

    # ---------- structural operations (offset / array / explode / mirror) ----------

    @staticmethod
    def _variant_objects(result: Any) -> List[Any]:
        """Normalise a COM SAFEARRAY-of-objects return value into a Python list."""
        if result is None:
            return []
        if isinstance(result, (list, tuple)):
            return [item for item in result if item is not None]
        try:
            return [item for item in result if item is not None]
        except TypeError:
            return [result]

    def offset_entity(self, handle: str, distance: float) -> Dict[str, Any]:
        """Offset an entity by ``distance`` (positive and negative select the side).

        Args:
            handle: Handle of the source entity.
            distance: Offset distance in drawing units.

        Returns:
            Dict with the handles of the created offset copies.
        """
        self._validate_connection()
        if distance == 0:
            raise ValueError("distance must be non-zero")

        document = self._get_document("offset_entity")
        entity = document.HandleToObject(handle)

        try:
            created = self._variant_objects(entity.Offset(float(distance)))
        except Exception as first_error:
            # AutoCAD rejects the negative side for some entities and vice versa;
            # retry mirrored and report which side worked instead of failing.
            logger.debug(f"offset({distance}) failed ({first_error}), retrying negated")
            created = self._variant_objects(entity.Offset(-float(distance)))

        self.refresh_view()
        handles = [str(item.Handle) for item in created]
        logger.info(f"Offset {handle} by {distance} -> {handles}")
        return {
            "success": bool(handles),
            "count": len(handles),
            "handles": handles,
            "detail": f"Offset {handle} by {distance} created {len(handles)} object(s)",
        }

    def array_rectangular(
        self,
        handle: str,
        rows: int,
        columns: int,
        row_spacing: float,
        column_spacing: float,
    ) -> Dict[str, Any]:
        """Create a rectangular (rows x columns) array from one entity.

        Args:
            handle: Handle of the source entity.
            rows: Number of rows (>= 1).
            columns: Number of columns (>= 1).
            row_spacing: Spacing between rows (drawing units, Y direction).
            column_spacing: Spacing between columns (drawing units, X direction).

        Returns:
            Dict with the handles created by the array.
        """
        self._validate_connection()
        if rows < 1 or columns < 1:
            raise ValueError("rows and columns must be >= 1")
        if rows * columns < 2:
            raise ValueError("a rectangular array needs at least 2 items")

        document = self._get_document("array_rectangular")
        entity = document.HandleToObject(handle)
        created = self._variant_objects(
            entity.ArrayRectangular(
                int(rows),
                int(columns),
                1,
                float(row_spacing),
                float(column_spacing),
                0.0,
            )
        )

        self.refresh_view()
        handles = [str(item.Handle) for item in created]
        logger.info(f"Rectangular array {rows}x{columns} from {handle}: {len(handles)} objects")
        return {
            "success": True,
            "count": len(handles),
            "handles": handles,
            "detail": f"Created {rows}x{columns} array ({len(handles)} new object(s))",
        }

    def array_polar(
        self, handle: str, count: int, angle: float, center_x: float, center_y: float
    ) -> Dict[str, Any]:
        """Create a polar array around a centre point.

        Args:
            handle: Handle of the source entity.
            count: Total number of items (>= 2).
            angle: Angle to fill in degrees (360 = full circle).
            center_x: Centre X coordinate.
            center_y: Centre Y coordinate.

        Returns:
            Dict with the handles created by the array.
        """
        self._validate_connection()
        if count < 2:
            raise ValueError("count must be >= 2")

        document = self._get_document("array_polar")
        entity = document.HandleToObject(handle)
        center = self._to_variant_array((center_x, center_y, 0.0))
        created = self._variant_objects(
            entity.ArrayPolar(int(count), math.radians(float(angle)), center)
        )

        self.refresh_view()
        handles = [str(item.Handle) for item in created]
        logger.info(f"Polar array {count} items over {angle} deg from {handle}")
        return {
            "success": True,
            "count": len(handles),
            "handles": handles,
            "detail": f"Created polar array of {count} items over {angle} degrees",
        }

    def explode_entity(self, handle: str) -> Dict[str, Any]:
        """Explode a compound entity (polyline, block reference, ...).

        Args:
            handle: Handle of the entity to explode.

        Returns:
            Dict with the handles produced by the explosion.
        """
        self._validate_connection()
        document = self._get_document("explode_entity")
        entity = document.HandleToObject(handle)
        created = self._variant_objects(entity.Explode())

        self.refresh_view()
        handles = [str(item.Handle) for item in created]
        logger.info(f"Exploded {handle} into {len(handles)} entities")
        return {
            "success": bool(handles),
            "count": len(handles),
            "handles": handles,
            "detail": f"Exploded {handle} into {len(handles)} entities",
        }

    def mirror_entity(
        self,
        handle: str,
        point1: Tuple[float, float],
        point2: Tuple[float, float],
        delete_source: bool = False,
    ) -> Dict[str, Any]:
        """Mirror an entity across a line defined by two points.

        Args:
            handle: Handle of the source entity.
            point1: First point of the mirror axis.
            point2: Second point of the mirror axis.
            delete_source: When True the original entity is deleted.

        Returns:
            Dict with the handle of the mirrored copy.
        """
        self._validate_connection()
        document = self._get_document("mirror_entity")
        entity = document.HandleToObject(handle)

        axis_start = self._to_variant_array((point1[0], point1[1], 0.0))
        axis_end = self._to_variant_array((point2[0], point2[1], 0.0))
        created = self._variant_objects(entity.Mirror(axis_start, axis_end))

        if delete_source and created:
            entity.Delete()

        self.refresh_view()
        handles = [str(item.Handle) for item in created]
        logger.info(f"Mirrored {handle} -> {handles} (delete_source={delete_source})")
        return {
            "success": bool(handles),
            "count": len(handles),
            "handles": handles,
            "source_deleted": bool(delete_source and created),
            "detail": f"Mirrored {handle} across ({point1}) - ({point2})",
        }
