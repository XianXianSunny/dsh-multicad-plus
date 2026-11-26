"""
View mixin for AutoCAD adapter.

Handles view operations (zoom, refresh, undo, redo).
"""

import logging
import base64
import os
import time
import win32gui
import win32con
from typing import TYPE_CHECKING, Any, Dict
from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)

# Characters that can terminate a command or escape the argument in SendCommand
UNSAFE_COMMAND_CHARS = {';', '"', "'", "\n", "\r", "\t"}


class ViewMixin:
    """Mixin for view operations."""

    if TYPE_CHECKING:
        # Tell type checker this mixin is used with CADAdapterProtocol
        from typing import Any

        # Attributes from AutoCADAdapter
        cad_type: str

        def _get_application(self, operation: str = "operation") -> Any: ...
        def _get_document(self, operation: str = "operation") -> Any: ...
        def _to_variant_array(self, point: Any) -> Any: ...
        def _simulate_autocad_click(self) -> bool: ...
        def _validate_connection(self) -> None: ...
        def resolve_export_path(
            self, filename: str, folder_type: str = "drawings"
        ) -> str: ...

    def _sanitize_command_input(self, user_input: str) -> str:
        """Sanitize input for SendCommand to prevent command injection.

        Only characters that can actually break out of a command line are
        stripped: control characters, semicolons (AutoCAD treats ``;`` as
        Enter) and quotes.

        Everything else is kept - including CJK characters. The previous
        ASCII-only whitelist silently deleted Chinese characters, so an export
        directory under a Chinese path (E:\\项目\\导出) produced a mangled
        filename and a failed PNGOUT.

        Args:
            user_input: The user-provided input to sanitize

        Returns:
            str: The sanitized input safe for SendCommand
        """
        sanitized = "".join(
            ch
            for ch in user_input
            if ch.isprintable() and ch not in UNSAFE_COMMAND_CHARS
        )
        if sanitized != user_input:
            logger.warning(f"Input sanitized due to unsafe characters: {user_input!r}")
            logger.debug(f"Sanitized to: {sanitized!r}")
        return sanitized

    def _find_cad_window(self) -> int:
        """Find CAD application window with strict matching.

        Uses both window title and class name matching to avoid VBA and other windows.
        Returns the handle of the main CAD window.

        Returns:
            int: Window handle (HWND) of the CAD application, 0 if not found

        Raises:
            Exception: If CAD window cannot be found
        """
        from mcp_tools.constants import (
            CAD_WINDOW_CLASS_EXCLUDE,
            CAD_WINDOW_SEARCH_TERMS,
            CAD_WINDOW_TITLE_EXCLUDE,
        )

        # 1) Preferred: ask the COM application for its own main window handle.
        #    This is exact and immune to localised titles / custom window classes
        #    (e.g. ZWCAD uses "Afx:00007FF...:8:..." classes and a Chinese title).
        try:
            application = self._get_application("_find_cad_window")
            com_hwnd = int(application.HWND)
            if com_hwnd and win32gui.IsWindow(com_hwnd):
                logger.debug(f"CAD window from COM HWND property: {com_hwnd}")
                return com_hwnd
        except Exception as error:
            logger.debug(f"application.HWND unavailable, falling back to enumeration: {error}")

        # 2) Fallback: enumerate visible top-level windows and match the title.
        terms = CAD_WINDOW_SEARCH_TERMS.get(self.cad_type, [])
        if isinstance(terms, str):  # tolerate the old single-string constant shape
            terms = [terms]
        terms_lower = [term.lower() for term in terms]

        hwnd = 0

        def enum_windows_callback(handle, _):
            nonlocal hwnd
            if hwnd or not win32gui.IsWindowVisible(handle):
                return True

            title = win32gui.GetWindowText(handle)
            if not title:
                return True
            lowered = title.lower()

            if any(bad.lower() in lowered for bad in CAD_WINDOW_TITLE_EXCLUDE):
                return True

            class_name = win32gui.GetClassName(handle)
            if class_name in CAD_WINDOW_CLASS_EXCLUDE:
                return True

            if any(term in lowered for term in terms_lower):
                hwnd = handle
                logger.debug(f"Found CAD window: title='{title}', class='{class_name}', hwnd={handle}")
            return True

        win32gui.EnumWindows(enum_windows_callback, None)

        if not hwnd:
            raise Exception(
                f"Could not find window for {self.cad_type} "
                f"(title searched for: {', '.join(terms) or 'nothing configured'})"
            )

        return hwnd

    def get_screenshot(self) -> Dict[str, str]:
        """
        Capture a screenshot of the CAD application window.

        Returns:
            dict: Dictionary with 'path' and 'data' (base64)

        Raises:
            Exception: If screenshot fails
        """
        try:
            self._validate_connection()

            # Find the CAD window using strict matching
            hwnd = self._find_cad_window()

            # Bring to front (optional, but good for clean screenshot)
            # Handle minimized state
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                logger.warning(f"Could not bring window to front: {e}")

            # Get window bounds
            rect = win32gui.GetWindowRect(hwnd)
            x, y, w, h = rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
            logger.debug(f"Capturing screenshot for HWND {hwnd} at {x},{y} {w}x{h}")

            # Capture
            image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)

            # A full-screen grab of a 4K display is ~35 MB of base64 and was
            # enough to kill the stdio server mid-call. Downscale before
            # encoding; the picture is only used as a visual sanity check.
            max_side = 1600
            if max(image.size) > max_side:
                ratio = max_side / float(max(image.size))
                image = image.resize(
                    (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                    Image.LANCZOS,
                )

            # Prepare filename and resolve path using centralized utility
            filename = f"cad_screenshot_{os.getpid()}.png"
            filepath = self.resolve_export_path(filename, "images")

            image.save(filepath, "PNG")

            # Convert to base64
            with open(filepath, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

            logger.info(f"Screenshot saved to {filepath}")

            return {"path": filepath, "data": encoded_string}

        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            raise Exception(f"Failed to capture screenshot: {e}")

    def export_view(self) -> Dict[str, str]:
        """Export current view using internal PNGOUT command.

        This method uses ZWCAD's built-in PNGOUT command to export the drawing
        view to a PNG file. Unlike get_screenshot(), this method:
        - Works even if the window is minimized or obscured
        - Captures only the drawing content (no UI chrome)
        - Uses the CAD application's internal rendering

        Returns:
            Dictionary with 'path' and 'data' (base64 encoded image)

        Raises:
            Exception: If export fails
        """
        document = None
        prev_filedia = None
        try:
            self._validate_connection()
            document = self._get_document("export_view")

            # Prepare filename and resolve path using centralized utility
            filename = f"cad_export_{os.getpid()}.png"
            filepath = self.resolve_export_path(filename, "images")

            # PNGOUT does not overwrite: when the target file already exists the
            # overwrite prompt is swallowed by SendCommand, nothing is written and
            # the caller receives a stale image of the previous drawing state.
            # Always delete the previous export first.
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    logger.debug(f"Removed stale export before PNGOUT: {filepath}")
            except OSError as error:
                logger.warning(f"Could not remove stale export {filepath}: {error}")

            # Ensure path uses backslashes for CAD command and is absolute
            filepath_cad = filepath.replace("/", "\\")

            logger.debug(f"Exporting view to {filepath_cad}")
            started_at = time.time()

            # Find the CAD window for focusing (with error handling for headless mode)
            try:
                hwnd = self._find_cad_window()
                try:
                    if win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"Could not focus window for export: {e}")
            except Exception as e:
                logger.debug(f"Could not find CAD window for focusing: {e}")

            # Remember the caller's FILEDIA so it can always be put back.
            # FILEDIA lives in the user profile, so leaking it as 0 silently
            # removes every file dialog until someone types FILEDIA 1.
            try:
                prev_filedia = int(document.GetVariable("FILEDIA"))
            except Exception as e:
                logger.debug(f"Could not read FILEDIA, assuming 1: {e}")
                prev_filedia = 1

            # Disable the file dialog only for the duration of the export.
            # NOTE: no ESC/control characters here - AutoCAD 2024 rejects them
            # with "输入无效" (logged 2026-10-26) before the export even starts.
            document.SendCommand("FILEDIA 0\n")
            time.sleep(0.1)

            # Execute PNGOUT command with file path
            # Sequence: command, path, selection (all), finish selection
            safe_path = self._sanitize_command_input(filepath_cad)
            document.SendCommand(f"_PNGOUT\n{safe_path}\n_ALL\n\n")

            # Wait briefly for file to be written
            time.sleep(1.5)  # Increased wait for render

            # Verify file was created
            if not os.path.exists(filepath):
                # Try fallback: maybe it didn't like _ALL, try just \n\n
                logger.debug(
                    "PNGOUT with _ALL failed, trying with default selection..."
                )
                safe_path_fallback = self._sanitize_command_input(filepath_cad)
                document.SendCommand(f"_PNGOUT\n{safe_path_fallback}\n\n")
                time.sleep(1.5)

            if not os.path.exists(filepath):
                raise Exception(f"Export file was not created at {filepath}")

            # Guard against a file that survived the deletion attempt above.
            if os.path.getmtime(filepath) < started_at - 1.0:
                raise Exception(
                    f"Export file at {filepath} was not refreshed by PNGOUT "
                    "(stale image); check that the CAD window is responsive"
                )

            # Convert to base64
            with open(filepath, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

            logger.info(f"View exported to {filepath}")

            return {"path": filepath, "data": encoded_string}

        except Exception as e:
            logger.error(f"Export view failed: {e}")
            raise Exception(f"Failed to export view: {e}")
        finally:
            # Always put FILEDIA back, even if the export blew up halfway.
            if document is not None and prev_filedia is not None:
                try:
                    # Let any pending command finish first, otherwise the
                    # restore text gets eaten as command input.
                    self._wait_for(
                        lambda: document.GetVariable("CMDACTIVE") == 0, timeout=5
                    )
                    document.SendCommand(f"FILEDIA {prev_filedia}\n")
                    logger.debug(f"FILEDIA restored to {prev_filedia}")
                except Exception as e:
                    logger.error(
                        f"Could not restore FILEDIA (should be {prev_filedia}): {e}. "
                        "Run FILEDIA and set it back to 1 manually."
                    )

    def zoom_extents(self) -> bool:
        """Zoom the active viewport to fit all drawing entities via COM.

        Returns:
            True if successful, False otherwise.
        """
        try:
            application = self._get_application("zoom_extents")
            document = self._get_document("zoom_extents")

            # A freshly created document answers ZoomExtents before its view is
            # recalculated, which leaves the model enormously zoomed in (a 64 mm
            # logo filled the window in the 2026-11-27 session). Activate,
            # settle, then call it again.
            try:
                document.Activate()
            except Exception as error:
                logger.debug(f"Activate before zoom skipped: {error}")

            # Prefer an explicit window around the drawing extents: several CAD
            # builds clamp ZoomExtents to a minimum view size, which crops a
            # small drawing (a 64 mm logo) instead of fitting it.
            if self._zoom_to_drawing_extents(application, document):
                return True

            application.ZoomExtents()
            time.sleep(0.2)
            application.ZoomExtents()

            try:
                application.Refresh()
            except Exception as error:
                logger.debug(f"Refresh after zoom skipped: {error}")

            logger.debug("Zoomed to extents")
            return True
        except Exception as e:
            logger.error(f"Failed to zoom extents: {e}")
            return False

    def _zoom_to_drawing_extents(self, application: Any, document: Any) -> bool:
        """Zoom a window that fits ``EXTMIN``/``EXTMAX`` with a small margin.

        Returns:
            True when the window zoom was applied, False to let the caller fall
            back to ``ZoomExtents``.
        """
        try:
            ext_min = document.GetVariable("EXTMIN")
            ext_max = document.GetVariable("EXTMAX")
            x1, y1 = float(ext_min[0]), float(ext_min[1])
            x2, y2 = float(ext_max[0]), float(ext_max[1])
        except Exception as error:
            logger.debug(f"Could not read drawing extents: {error}")
            return False

        width = abs(x2 - x1)
        height = abs(y2 - y1)
        if width < 1e-6 and height < 1e-6:
            logger.debug("Drawing extents are degenerate, falling back to ZoomExtents")
            return False
        # EXTMIN/EXTMAX are zeroed on a fresh drawing; ignore that case too.
        if abs(x1) < 1e-9 and abs(y1) < 1e-9 and width < 1e-6:
            return False

        margin = max(width, height) * 0.08 + 0.5
        try:
            application.ZoomWindow(
                self._to_variant_array((x1 - margin, y1 - margin, 0.0)),
                self._to_variant_array((x2 + margin, y2 + margin, 0.0)),
            )
            try:
                application.Refresh()
            except Exception:
                pass
            logger.debug(
                f"Zoomed to window ({x1:.3f},{y1:.3f})-({x2:.3f},{y2:.3f})"
            )
            return True
        except Exception as error:
            logger.debug(f"ZoomWindow failed, falling back to ZoomExtents: {error}")
            return False

    def refresh_view(self) -> bool:
        """Refresh the view using multiple techniques for maximum compatibility.

        Uses a combination of techniques in fallback order:
        1. Application.Refresh() (COM API - no undo/redo impact)
        2. SendCommand with REDRAW (most reliable visual update)

        The old third step (a real simulated mouse click in the middle of the
        CAD window) has been removed: it hijacked the user's cursor and could
        drop a point if a command happened to be active.

        Note: REDRAW command is not wrapped in UNDO to avoid complicating
        the undo/redo stack. If refresh_view is called during user operations,
        the REDRAW will be undone by the user's undo command anyway.

        Returns:
            True if refresh was attempted (best effort approach)
        """
        try:
            application = self._get_application("refresh_view")
            document = self._get_document("refresh_view")

            # Technique 1: COM API Refresh (doesn't affect undo/redo)
            try:
                application.Refresh()
                logger.debug("Refresh: COM Refresh executed")
            except Exception as e:
                logger.debug(f"COM Refresh failed: {e}")

            # Technique 2: Send REDRAW command (most reliable visual update)
            try:
                document.SendCommand("_redraw\n")
                logger.debug("Refresh: REDRAW command sent")
            except Exception as e:
                logger.debug(f"REDRAW command failed: {e}")

            # Technique 3 (simulated click) deliberately removed - see docstring.
            return True
        except Exception as e:
            logger.debug(f"refresh_view error: {e}")
            return False

    def undo(self, count: int = 1) -> bool:
        """Undo last action(s).

        Args:
            count: Number of operations to undo (default: 1)

        Returns:
            True if successful, False otherwise
        """
        try:
            self._validate_connection()
            if count < 1:
                logger.warning(f"Invalid undo count: {count}. Must be >= 1")
                return False

            app = self._get_application("undo")
            app.ActiveDocument.SendCommand(f"_undo {count}\n")
            logger.info(f"Undo executed ({count} operation(s))")
            return True
        except Exception as e:
            logger.error(f"Failed to undo: {e}")
            return False

    def redo(self, count: int = 1) -> bool:
        """Redo last undone action(s).

        Args:
            count: Number of operations to redo (default: 1)

        Returns:
            True if successful, False otherwise
        """
        try:
            self._validate_connection()
            if count < 1:
                logger.warning(f"Invalid redo count: {count}. Must be >= 1")
                return False

            app = self._get_application("redo")
            app.ActiveDocument.SendCommand(f"_redo {count}\n")
            logger.info(f"Redo executed ({count} operation(s))")
            return True
        except Exception as e:
            logger.error(f"Failed to redo: {e}")
            return False
