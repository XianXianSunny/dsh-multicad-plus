"""
Unified geometry editing tool.

Fills the gaps left by the upstream tool set: joining a chain of lines/arcs into
one polyline, and building real tangent fillets / chamfers on polyline corners.

SHORTHAND FORMAT (one per line):
    join|handles                  → join|239,23A,23B
    fillet|handle|radius          → fillet|1F|1.5
    fillet|handle|radius|vertices → fillet|1F|1.5|0,2,3
    chamfer|handle|distance       → chamfer|1F|2

    "vertices" = "all" (default) or comma-separated vertex indices (0-based,
    negative indices count from the end).

Arc segments are preserved exactly by converting the arc sweep into a polyline
bulge, so joined geometry is not approximated.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from mcp_tools.decorators import cad_tool, get_current_adapter
from mcp_tools.helpers import parse_handles

logger = logging.getLogger(__name__)


# ========== Action Handlers ==========


def _join(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Join lines/arcs into a single lightweight polyline.

    Args:
        spec: Operation spec with keys: handles (str).

    Returns:
        Adapter result dict.
    """
    handles = parse_handles(spec["handles"])
    return get_current_adapter().join_entities(handles)


def _fillet(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Fillet polyline corners with tangent arcs.

    Args:
        spec: Operation spec with keys: handle (str), radius (float),
            vertices (optional str).

    Returns:
        Adapter result dict.
    """
    radius = float(spec["radius"])
    return get_current_adapter().fillet_polyline(
        spec["handle"], radius, _parse_vertices(spec.get("vertices"))
    )


def _chamfer(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Chamfer polyline corners with straight cuts.

    Args:
        spec: Operation spec with keys: handle (str), distance (float),
            vertices (optional str).

    Returns:
        Adapter result dict.
    """
    distance = float(spec["distance"])
    return get_current_adapter().chamfer_polyline(
        spec["handle"], distance, _parse_vertices(spec.get("vertices"))
    )


def _offset(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Offset an entity towards one side."""
    return get_current_adapter().offset_entity(spec["handle"], float(spec["distance"]))


def _array_rect(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Rectangular array of one entity."""
    return get_current_adapter().array_rectangular(
        spec["handle"],
        int(spec["rows"]),
        int(spec["columns"]),
        float(spec.get("row_spacing", 0) or 0),
        float(spec.get("column_spacing", 0) or 0),
    )


def _array_polar(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Polar array of one entity around a centre point."""
    center = str(spec.get("center", "0,0")).replace(";", ",").split(",")
    if len(center) < 2:
        raise ValueError("array_polar needs a center like '0,0'")
    return get_current_adapter().array_polar(
        spec["handle"],
        int(spec["count"]),
        float(spec.get("angle", 360) or 360),
        float(center[0]),
        float(center[1]),
    )


def _explode(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Explode a compound entity."""
    return get_current_adapter().explode_entity(spec["handle"])


def _mirror(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror an entity across a two-point axis."""

    def _pair(text: str) -> Tuple[float, float]:
        parts = str(text).replace(";", ",").split(",")
        if len(parts) < 2:
            raise ValueError(f"expected 'x,y', got {text!r}")
        return (float(parts[0]), float(parts[1]))

    delete_source = str(spec.get("delete", "false")).strip().lower() in ("1", "true", "yes", "on")
    return get_current_adapter().mirror_entity(
        spec["handle"], _pair(spec["axis_start"]), _pair(spec["axis_end"]), delete_source
    )


def _parse_vertices(value: Optional[str]) -> Optional[List[int]]:
    """Parse an optional vertex-index list ("all" or "0,2,3")."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "all":
        return None
    return [int(part.strip()) for part in text.split(",") if part.strip()]


# Dispatch table: action -> (handler, required_fields, optional fields)
GEOMETRY_DISPATCH: Dict[str, Tuple[Callable, List[str]]] = {
    "join": (_join, ["handles"]),
    "fillet": (_fillet, ["handle", "radius"]),
    "chamfer": (_chamfer, ["handle", "distance"]),
    "offset": (_offset, ["handle", "distance"]),
    "array_rect": (_array_rect, ["handle", "rows", "columns"]),
    "array_polar": (_array_polar, ["handle", "count"]),
    "explode": (_explode, ["handle"]),
    "mirror": (_mirror, ["handle", "axis_start", "axis_end"]),
}


def _validate_required_fields(
    spec: Dict[str, Any], required: List[str], action: str
) -> Optional[str]:
    """Return an error message when required fields are missing."""
    missing = [field for field in required if field not in spec]
    if missing:
        return f"'{action}' requires fields: {', '.join(missing)}"
    return None


# Positional field order for the pipe-separated shorthand, per action.
FIELD_ORDER: Dict[str, List[str]] = {
    "join": ["handles"],
    "fillet": ["handle", "radius", "vertices"],
    "chamfer": ["handle", "distance", "vertices"],
    "offset": ["handle", "distance"],
    "array_rect": ["handle", "rows", "columns", "row_spacing", "column_spacing"],
    "array_polar": ["handle", "count", "angle", "center"],
    "explode": ["handle"],
    "mirror": ["handle", "axis_start", "axis_end", "delete"],
}


def _parse_ops(operations: str) -> List[Dict[str, Any]]:
    """Parse pipe-separated shorthand lines into operation specs."""
    text = (operations or "").strip()
    if not text:
        return []

    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        return [dict(item) for item in data]

    specs: List[Dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        action = parts[0].lower()
        fields = FIELD_ORDER.get(action)
        if fields is None:
            raise ValueError(
                f"unknown action '{parts[0]}'. Supported: {', '.join(FIELD_ORDER)}"
            )

        spec: Dict[str, Any] = {"action": action}
        values = parts[1:]
        for name, value in zip(fields, values):
            if value != "":
                spec[name] = value
        if len(values) > len(fields):
            raise ValueError(
                f"'{action}' takes at most {len(fields)} arguments ({', '.join(fields)}), "
                f"got {len(values)}"
            )
        specs.append(spec)

    return specs


# ========== Tool Registration ==========


def register_geometry_tools(mcp):
    """Register the unified geometry editing tool with FastMCP."""

    @cad_tool(mcp, "manage_geometry")
    def manage_geometry(operations: str) -> str:
        """
        Edit geometry: join chains into polylines, and build real tangent
        fillets or straight chamfers on polyline corners.

        Args:
            operations: Operations in SHORTHAND format (one per line):

                join|handles                       → join|239,23A,23B
                fillet|handle|radius[|vertices]    → fillet|1F|1.5  /  fillet|1F|1.5|0,2
                chamfer|handle|distance[|vertices] → chamfer|1F|2
                offset|handle|distance             → offset|1F|1.5
                array_rect|handle|rows|cols[|row_gap|col_gap]
                                                   → array_rect|1F|3|4|20|20
                array_polar|handle|count[|angle|center]
                                                   → array_polar|1F|6|360|0,0
                explode|handle                     → explode|1F
                mirror|handle|axis_start|axis_end[|delete]
                                                   → mirror|1F|0,0|0,1|true

                "handles" = comma-separated entity handles (lines and/or arcs)
                "vertices" = "all" (default) or comma-separated 0-based vertex
                             indices; negative indices count from the end
                Arc segments in join are converted to exact polyline bulges, so a
                join never approximates the original geometry.

                Example:
                    join|239,23A,23B,23C,23D,23E,23F,241
                    fillet|1F|1.5
                    offset|20|2
                    array_rect|20|3|4|25|25

        Returns:
            JSON result with per-operation status
        """
        try:
            specs = _parse_ops(operations)
        except Exception as error:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid input: {error}",
                    "total": 0,
                    "succeeded": 0,
                    "results": [],
                },
                indent=2,
            )

        results = []
        for index, spec in enumerate(specs):
            action = str(spec.get("action", "")).lower()
            entry = GEOMETRY_DISPATCH.get(action)
            if not entry:
                results.append(
                    {
                        "index": index,
                        "action": action,
                        "success": False,
                        "error": f"Unknown action '{action}'. Supported: "
                        + ", ".join(GEOMETRY_DISPATCH),
                    }
                )
                continue

            handler, required = entry
            field_error = _validate_required_fields(spec, required, action)
            if field_error:
                results.append(
                    {"index": index, "action": action, "success": False, "error": field_error}
                )
                continue

            try:
                result = handler(spec)
                results.append({"index": index, "action": action, **result})
            except Exception as error:
                logger.error(f"Geometry op {index} ({action}) failed: {error}")
                results.append(
                    {
                        "index": index,
                        "action": action,
                        "success": False,
                        "error": str(error),
                    }
                )

        return json.dumps(
            {
                "total": len(specs),
                "succeeded": sum(1 for item in results if item.get("success")),
                "results": results,
            },
            indent=2,
        )
