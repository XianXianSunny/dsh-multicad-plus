"""
Constants and configuration for multiCAD-MCP server.

Centralized storage for colors, selection set names, timing, and other constants.
"""

# ========== AutoCAD Color Index (ACI) ==========

COLOR_MAP = {
    "black": 0,
    "red": 1,
    "yellow": 2,
    "green": 3,
    "cyan": 4,
    "blue": 5,
    "magenta": 6,
    "white": 7,
    "gray": 8,
    "light_gray": 252,
    "dark_gray": 251,
    "orange": 30,
    "bylayer": 256,  # Special value for entities to inherit layer color
}

# Special color constants
AC_BY_BLOCK = 0  # Color ByBlock
AC_BY_LAYER = 256  # Color ByLayer (for entities only)

# ========== Selection Set Names ==========

SS_COLOR_SELECT = "_color_select_ss"
SS_LAYER_SELECT = "_layer_select_ss"
SS_TYPE_SELECT = "_type_select_ss"
SS_COPY = "_copy_ss"
SS_SELECTION_GET = "_selection_get"

# ========== AutoCAD Window Classes ==========

AUTOCAD_WINDOW_CLASSES = [
    "AutoCAD Main Window",
    "ACAD:Main",
    "AutoCADMainFrame",
]

# ========== CAD Main Window Discovery ==========

# Window titles to look for per CAD type. Localised builds do not contain the
# English product name, so keep every known spelling: a Chinese ZWCAD 2026
# window is titled "中望CAD 2026 专业版 - [drawing.dwg]", not "ZWCAD 2026".
CAD_WINDOW_SEARCH_TERMS = {
    "autocad": ["Autodesk AutoCAD", "AutoCAD"],
    "zwcad": ["ZWCAD", "中望CAD", "中望 CAD"],
    "gcad": ["GstarCAD", "浩辰CAD", "浩辰 CAD"],
    "bricscad": ["BricsCAD"],
}

# Titles that contain a search term but are never the drawing window.
CAD_WINDOW_TITLE_EXCLUDE = ["VBA", "Visual LISP", "Console Window Host"]

# Classes that are never the CAD main frame (loose matching safety net).
CAD_WINDOW_CLASS_EXCLUDE = ["ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"]

# ========== Selection Set Modes ==========

SELECTION_SET_IMPLIED = 3  # acSelectionSetImplied

# ========== DXF Filter Codes ==========

DXF_LAYER = 8
DXF_COLOR = 62

# ========== Timing Constants (milliseconds) ==========

CLIPBOARD_DELAY = 300
CLIPBOARD_STABILITY_DELAY = 500
CLICK_DELAY = 50
CLICK_HOLD_DELAY = 20
COMMAND_EXECUTE_DELAY = 100
EXPORT_WRITE_DELAY = 1500
COM_INIT_TIMEOUT = 20000
