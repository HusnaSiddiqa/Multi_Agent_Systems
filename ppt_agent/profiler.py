"""PPT Agent - DataFrame Profiler (deterministic, pandas only).

Produces a compact profile dict the LLM uses to choose chart type + channel mapping.
No LLM calls here — pure computation.
"""

import pandas as pd
import numpy as np


def _coerce_type(series: pd.Series) -> str:
    """Determine semantic type: temporal, numeric, or categorical."""
    # Already datetime
    if pd.api.types.is_datetime64_any_dtype(series):
        return "temporal"

    # Try parsing as datetime
    sample = series.dropna().head(50)
    if len(sample) == 0:
        return "categorical"

    # Check for period-like strings (Q1'25, 2024-Q1, Jan-2024, etc.)
    str_sample = sample.astype(str)
    period_patterns = str_sample.str.match(
        r"^(Q[1-4]|\d{4}[-/]Q[1-4]|Q[1-4][-/']\d{2,4}|"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}|"
        r"\d{4}[-/](0[1-9]|1[0-2])|Week\s*\d+)",
        case=False
    )
    if period_patterns.mean() > 0.8:
        return "temporal"

    # Try pd.to_datetime
    try:
        pd.to_datetime(sample, errors="raise")
        return "temporal"
    except (ValueError, TypeError):
        pass

    # Try numeric (strip $, %, commas first)
    try:
        cleaned = str_sample.str.replace(r"[\$,%]", "", regex=True).str.strip()
        pd.to_numeric(cleaned, errors="raise")
        return "numeric"
    except (ValueError, TypeError):
        pass

    # Already numeric dtype
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"

    return "categorical"


def _column_profile(series: pd.Series, coerced_type: str) -> dict:
    """Profile a single column."""
    n = len(series)
    null_count = int(series.isna().sum())
    non_null = series.dropna()

    profile = {
        "coerced_type": coerced_type,
        "cardinality": int(non_null.nunique()),
        "null_rate": round(null_count / n, 3) if n > 0 else 0.0,
        "samples": [str(v) for v in non_null.head(5).tolist()],
    }

    if coerced_type == "numeric":
        # Convert to numeric for stats
        numeric_vals = pd.to_numeric(
            non_null.astype(str).str.replace(r"[\$,%]", "", regex=True),
            errors="coerce"
        ).dropna()
        if len(numeric_vals) > 0:
            profile["min"] = float(numeric_vals.min())
            profile["max"] = float(numeric_vals.max())
            profile["non_negative"] = bool(numeric_vals.min() >= 0)
            # Check if values sum to ~100 (share/percentage data)
            profile["sums_near_100"] = bool(95 <= numeric_vals.sum() <= 105) if len(numeric_vals) <= 20 else False

    return profile


def profile_df(df: pd.DataFrame) -> dict:
    """Profile a DataFrame for chart-type selection.

    Returns a compact dict suitable for LLM prompt injection.
    Keeps total token count low regardless of DataFrame size.
    """
    if df is None or df.empty:
        return {"n_rows": 0, "n_cols": 0, "columns": {}}

    profile = {
        "n_rows": len(df),
        "n_cols": len(df.columns),
        "columns": {},
    }

    for col in df.columns:
        coerced = _coerce_type(df[col])
        profile["columns"][col] = _column_profile(df[col], coerced)

    # Summary counts for quick LLM reasoning
    types = [v["coerced_type"] for v in profile["columns"].values()]
    profile["type_summary"] = {
        "temporal": types.count("temporal"),
        "numeric": types.count("numeric"),
        "categorical": types.count("categorical"),
    }

    return profile
