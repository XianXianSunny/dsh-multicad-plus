# Changelog

All notable changes to this fork are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-27

### Added

- `trace_image` tool: vectorises a raster reference (logo, screenshot, scanned
  sketch) into closed polylines. It thresholds the bitmap (Otsu when no
  threshold is given), walks the real pixel boundaries, simplifies them, and
  snaps edges to horizontal / vertical / 45 degrees. `mode="preview"` returns
  the polygons plus a round-trip fit (`iou`) without touching CAD;
  `mode="draw"` creates the polylines on a layer (created when missing).
  Pillow only: the private virtualenv has no numpy.
- `trace_image` `anchor` option: `bbox` (default) moves the traced content so
  its own bottom-left corner sits on the origin, `image` keeps the picture's
  white margin.
- `manage_files "tidy"`: closes every unsaved, empty drawing window.
- Server-level MCP instructions describing the delivery workflow (one drawing
  per deliverable, tidy before answering) and the tracing workflow.
- `manage_files "list"` reports a per-document inventory (path, entity count,
  blank flag, active flag) and hints when empty scratch drawings are open.
- `tests/test_tracing.py`: five cases (Otsu, rectangle with hole, 45 degree
  snapping, coordinate mapping, sub-tolerance vertex removal).

### Fixed

- Leftover `Drawing1` / `Drawing4` windows. `Documents.Add` was called for every
  `new`, for every `close` (to "keep a connection alive") and whenever no
  document was open, so a session that delivered two drawings left two empty
  windows behind. `new` now reuses an unsaved empty drawing, `save` closes the
  empty scratch drawings, `close` no longer adds a replacement document, and
  `tidy` is available for an explicit sweep.
- `save` reported a bare "Failed to save drawing" when the target file was
  already open in the same session; the conflict is now named in the result.
- Entities silently landed on layer 0 when the requested layer did not exist:
  CAD rejects the assignment and the error was swallowed. Missing layers are
  now created before the property is applied.
- `zoom_extents` on a freshly created document left the model zoomed far too
  close (a 64 mm logo filled the whole window). The document is activated first
  and the zoom is repeated after a short settle.
- `screenshot` could take down the stdio server: a 4K grab produced about 35 MB
  of base64 in one response. Captures are downscaled to 1600 px before encoding.

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
