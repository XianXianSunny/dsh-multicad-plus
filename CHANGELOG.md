# Changelog

All notable changes to this fork are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-11-25

Fork of `a494359163-byte/dsh-multicad` 0.1.0 (bundling `multiCAD-mcp` 0.2.0).

### Added

- `manage_geometry` tool with eight actions, implemented as pure Python geometry
  written back through `AddLightWeightPolyline` / `SetBulge`:
  - `join` — stitch LINE/ARC chains into one polyline; arc sweeps become exact
    bulges (`tan(sweep/4)`), so nothing is approximated by line segments.
  - `fillet` — real tangent fillets on polyline corners, all corners or a
    selected subset, with automatic radius clamping on short edges.
  - `chamfer` — equal-distance straight chamfers on polyline corners.
  - `offset` — offset an entity, retrying the opposite side when the CAD rejects
    one direction.
  - `array_rect` / `array_polar` — rectangular and polar arrays.
  - `explode` — explode compound entities.
  - `mirror` — mirror across a two-point axis, optionally deleting the source.
- `tests/test_geometry.py`: 12 pytest cases covering fillet/chamfer maths,
  analytic area/perimeter checks, radius clamping, segment decoding (including
  arcs that wrap past 0°) and classic 2D polyline decoding. No CAD required.

### Fixed

- Window discovery failed on localised builds: `_find_cad_window` only matched
  English titles and AutoCAD window classes, so ZWCAD 2026 (titled
  `中望CAD 2026 专业版`) could not be found and `screenshot` always errored.
  It now prefers the COM `Application.HWND`, then falls back to title matching
  across localised names, excluding VBA/console windows.
- `export_view` returned stale images: PNGOUT never overwrote an existing file
  and its overwrite prompt was swallowed by `SendCommand`. The target file is now
  deleted first and its mtime verified after the command.
- Arc angles were restricted to `[0, 360)`, so `-90` (straight down) and `360`
  were rejected. Angles are now normalised, and `270 → 0` works for a
  bottom-right fillet.
- `draw_rectangle` emitted five vertices (first point repeated) while also
  setting `Closed`, creating a degenerate zero-length edge. Consequences:
  `fillet` saw 5 corners and skipped one, `explode` produced a phantom entity,
  and the polyline read back with a duplicate vertex. Rectangles now use four
  vertices; `draw_polyline` also drops a repeated closing point.

### Changed

- `CAD_WINDOW_SEARCH_TERMS` values are lists of localised titles, plus new
  `CAD_WINDOW_TITLE_EXCLUDE` / `CAD_WINDOW_CLASS_EXCLUDE` constants.

## [0.1.0] - 2026-10-28

- Upstream release: DSH bundle wrapping `multiCAD-mcp` with a self-healing
  private virtualenv.
