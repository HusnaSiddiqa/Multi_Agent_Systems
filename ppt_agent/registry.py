"""PPT Agent - Chart Type Registry (single source of truth).

Each entry declares the XL chart type, required channels, data builder name,
and constraint function. The LLM catalog, validator, and renderer all derive
from this registry so they can never drift apart.
"""

from pptx.enum.chart import XL_CHART_TYPE


# ─── Constraint functions ────────────────────────────────────────────────────

def _no_constraints(profile):
    return True, ""


def _pie_constraints(profile):
    """Pie: single numeric measure, low category cardinality."""
    cats = [c for c, p in profile["columns"].items() if p["coerced_type"] in ("categorical", "temporal")]
    nums = [c for c, p in profile["columns"].items() if p["coerced_type"] == "numeric"]
    if not nums:
        return False, "Pie requires at least one numeric column"
    if cats:
        max_card = max(profile["columns"][c]["cardinality"] for c in cats)
        if max_card > 12:
            return False, f"Pie not suitable for cardinality > 12 (found {max_card})"
    return True, ""


def _stacked_constraints(profile):
    """Stacked: needs at least 2 series (categorical with cardinality >= 2)."""
    cats = [c for c, p in profile["columns"].items() if p["coerced_type"] in ("categorical", "temporal")]
    if len(cats) < 2:
        return False, "Stacked charts need at least 2 categorical/temporal columns (category + series)"
    return True, ""


def _scatter_constraints(profile):
    """Scatter: needs at least 2 numeric columns."""
    nums = [c for c, p in profile["columns"].items() if p["coerced_type"] == "numeric"]
    if len(nums) < 2:
        return False, "Scatter requires at least 2 numeric columns"
    return True, ""


def _bubble_constraints(profile):
    """Bubble: needs at least 3 numeric columns."""
    nums = [c for c, p in profile["columns"].items() if p["coerced_type"] == "numeric"]
    if len(nums) < 3:
        return False, "Bubble requires at least 3 numeric columns (x, y, size)"
    return True, ""


# ─── Registry ────────────────────────────────────────────────────────────────

CHART_REGISTRY = {
    # Column variants
    "column_clustered": {
        "xl_type": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "required_channels": ["category", "value"],
        "optional_channels": ["series"],
        "data_builder": "build_pivot",
        "constraints": _no_constraints,
    },
    "column_stacked": {
        "xl_type": XL_CHART_TYPE.COLUMN_STACKED,
        "required_channels": ["category", "series", "value"],
        "optional_channels": [],
        "data_builder": "build_pivot",
        "constraints": _stacked_constraints,
    },
    "column_stacked_100": {
        "xl_type": XL_CHART_TYPE.COLUMN_STACKED_100,
        "required_channels": ["category", "series", "value"],
        "optional_channels": [],
        "data_builder": "build_pivot",
        "constraints": _stacked_constraints,
    },
    # Bar variants
    "bar_clustered": {
        "xl_type": XL_CHART_TYPE.BAR_CLUSTERED,
        "required_channels": ["category", "value"],
        "optional_channels": ["series"],
        "data_builder": "build_pivot",
        "constraints": _no_constraints,
    },
    "bar_stacked": {
        "xl_type": XL_CHART_TYPE.BAR_STACKED,
        "required_channels": ["category", "series", "value"],
        "optional_channels": [],
        "data_builder": "build_pivot",
        "constraints": _stacked_constraints,
    },
    "bar_stacked_100": {
        "xl_type": XL_CHART_TYPE.BAR_STACKED_100,
        "required_channels": ["category", "series", "value"],
        "optional_channels": [],
        "data_builder": "build_pivot",
        "constraints": _stacked_constraints,
    },
    # Line
    "line": {
        "xl_type": XL_CHART_TYPE.LINE,
        "required_channels": ["category", "value"],
        "optional_channels": ["series"],
        "data_builder": "build_pivot",
        "constraints": _no_constraints,
    },
    "line_markers": {
        "xl_type": XL_CHART_TYPE.LINE_MARKERS,
        "required_channels": ["category", "value"],
        "optional_channels": ["series"],
        "data_builder": "build_pivot",
        "constraints": _no_constraints,
    },
    # Area
    "area": {
        "xl_type": XL_CHART_TYPE.AREA,
        "required_channels": ["category", "value"],
        "optional_channels": ["series"],
        "data_builder": "build_pivot",
        "constraints": _no_constraints,
    },
    "area_stacked": {
        "xl_type": XL_CHART_TYPE.AREA_STACKED,
        "required_channels": ["category", "series", "value"],
        "optional_channels": [],
        "data_builder": "build_pivot",
        "constraints": _stacked_constraints,
    },
    # Pie / Doughnut
    "pie": {
        "xl_type": XL_CHART_TYPE.PIE,
        "required_channels": ["category", "value"],
        "optional_channels": [],
        "data_builder": "build_category",
        "constraints": _pie_constraints,
    },
    "doughnut": {
        "xl_type": XL_CHART_TYPE.DOUGHNUT,
        "required_channels": ["category", "value"],
        "optional_channels": [],
        "data_builder": "build_category",
        "constraints": _pie_constraints,
    },
    # Radar
    "radar": {
        "xl_type": XL_CHART_TYPE.RADAR,
        "required_channels": ["category", "value"],
        "optional_channels": ["series"],
        "data_builder": "build_pivot",
        "constraints": _no_constraints,
    },
    # XY Scatter
    "scatter": {
        "xl_type": XL_CHART_TYPE.XY_SCATTER,
        "required_channels": ["x", "y"],
        "optional_channels": [],
        "data_builder": "build_xy",
        "constraints": _scatter_constraints,
    },
    # Bubble
    "bubble": {
        "xl_type": XL_CHART_TYPE.BUBBLE,
        "required_channels": ["x", "y", "size"],
        "optional_channels": [],
        "data_builder": "build_bubble",
        "constraints": _bubble_constraints,
    },
}


def get_chart_catalog() -> str:
    """Generate the chart catalog string for the LLM prompt.
    
    Lists each chart type with its required/optional channels
    so the LLM knows what to pick from.
    """
    lines = []
    for name, entry in CHART_REGISTRY.items():
        req = ", ".join(entry["required_channels"])
        opt = ", ".join(entry["optional_channels"]) if entry["optional_channels"] else "none"
        lines.append(f"- {name}: required=[{req}], optional=[{opt}]")
    return "\n".join(lines)


def get_chart_type_enum() -> list:
    """Return list of valid chart_type values for schema validation."""
    return list(CHART_REGISTRY.keys())
