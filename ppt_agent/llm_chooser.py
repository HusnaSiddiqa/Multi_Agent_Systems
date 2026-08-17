"""PPT Agent - LLM Chart Chooser, Validator & Deterministic Fallback.

The LLM only CHOOSES the chart type and maps columns to channels.
It never writes code or touches raw data.
"""

import json
import traceback

from .registry import CHART_REGISTRY, get_chart_catalog, get_chart_type_enum
from .profiler import profile_df


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_enc(enc: dict, profile: dict) -> tuple:
    """Validate an enc dict against the registry and profile.
    
    Returns (is_valid: bool, error_message: str).
    """
    # Check chart_type exists
    chart_type = enc.get("chart_type")
    if chart_type not in CHART_REGISTRY:
        return False, f"Unknown chart_type: '{chart_type}'. Valid types: {list(CHART_REGISTRY.keys())}"

    reg_entry = CHART_REGISTRY[chart_type]

    # Check channels exist
    channels = enc.get("channels", {})
    if not isinstance(channels, dict):
        return False, "channels must be a dict mapping channel names to column names"

    # Check required channels are present
    for req_ch in reg_entry["required_channels"]:
        if req_ch not in channels:
            return False, f"Missing required channel '{req_ch}' for chart_type '{chart_type}'"
        col_name = channels[req_ch]
        if col_name not in profile["columns"]:
            return False, f"Channel '{req_ch}' references column '{col_name}' which does not exist. Available: {list(profile['columns'].keys())}"

    # Check optional channels reference valid columns
    for ch_name, col_name in channels.items():
        if ch_name not in reg_entry["required_channels"] and ch_name not in reg_entry.get("optional_channels", []):
            continue  # Extra channels are ignored, not rejected
        if col_name not in profile["columns"]:
            return False, f"Channel '{ch_name}' references non-existent column '{col_name}'"

    # Check constraints
    ok, reason = reg_entry["constraints"](profile)
    if not ok:
        return False, f"Constraint failed for '{chart_type}': {reason}"

    return True, ""


# ─── Deterministic Fallback ──────────────────────────────────────────────────

def deterministic_fallback(profile: dict) -> dict:
    """Rules-based chart chooser. Always returns a valid enc.
    
    Rules:
    - temporal + 1 numeric + 1 categorical series -> line (multi-series)
    - temporal + 1 numeric -> line (single series)
    - 1 categorical (low card) + 1 numeric -> pie
    - 1 categorical + 1 numeric -> bar_clustered
    - 2+ categoricals + 1 numeric -> column_stacked
    - 2 numerics -> scatter
    - else -> column_clustered with first cat + first numeric
    """
    cols = profile["columns"]
    temporals = [c for c, p in cols.items() if p["coerced_type"] == "temporal"]
    numerics = [c for c, p in cols.items() if p["coerced_type"] == "numeric"]
    categoricals = [c for c, p in cols.items() if p["coerced_type"] == "categorical"]

    # Prefer share/percentage columns over count/volume columns
    share_keywords = ("share", "pct", "percent", "ratio", "proportion", "rate")
    count_keywords = ("count", "patient", "volume", "qty", "quantity", "num_", "n_")

    def _pick_best_numeric(nums):
        """Pick share column if available, else first numeric."""
        if not nums:
            return None
        share_cols = [c for c in nums if any(k in c.lower() for k in share_keywords)]
        if share_cols:
            return share_cols[0]
        # Deprioritize count columns — pick first non-count
        non_count = [c for c in nums if not any(k in c.lower() for k in count_keywords)]
        return non_count[0] if non_count else nums[0]

    # Use _pick_best_numeric for value column selection
    numerics_best = [_pick_best_numeric(numerics)] + [n for n in numerics if n != _pick_best_numeric(numerics)] if numerics else []
    if numerics_best and numerics_best[0]:
        numerics = numerics_best

    enc = {"category_transform": "none", "aggregate": "sum", "rationale": "deterministic fallback", "y_axis_label": "", "insight_bullets": [], "category_sort_column": "auto"}

    # Temporal + numeric + categorical series -> multi-series line
    if temporals and numerics and categoricals:
        enc["chart_type"] = "line"
        enc["channels"] = {
            "category": temporals[0],
            "value": numerics[0],
            "series": categoricals[0],
        }
        enc["title"] = f"{numerics[0]} over {temporals[0]} by {categoricals[0]}"
        return enc

    # Temporal + numeric -> single line
    if temporals and numerics:
        enc["chart_type"] = "line"
        enc["channels"] = {"category": temporals[0], "value": numerics[0]}
        enc["title"] = f"{numerics[0]} over {temporals[0]}"
        return enc

    # 2+ categoricals + numeric -> stacked column
    if len(categoricals) >= 2 and numerics:
        # Pick the one with lower cardinality as series
        sorted_cats = sorted(categoricals, key=lambda c: cols[c]["cardinality"])
        enc["chart_type"] = "column_stacked"
        enc["channels"] = {
            "category": sorted_cats[-1],  # higher cardinality = category axis
            "series": sorted_cats[0],     # lower cardinality = series/stacks
            "value": numerics[0],
        }
        enc["title"] = f"{numerics[0]} by {sorted_cats[-1]}"
        return enc

    # 1 categorical (low card) + 1 numeric -> pie
    if categoricals and numerics:
        cat = categoricals[0]
        if cols[cat]["cardinality"] <= 8:
            enc["chart_type"] = "pie"
            enc["channels"] = {"category": cat, "value": numerics[0]}
            enc["title"] = f"{numerics[0]} by {cat}"
            return enc
        else:
            enc["chart_type"] = "bar_clustered"
            enc["channels"] = {"category": cat, "value": numerics[0]}
            enc["title"] = f"{numerics[0]} by {cat}"
            return enc

    # 2 numerics -> scatter
    if len(numerics) >= 2:
        enc["chart_type"] = "scatter"
        enc["channels"] = {"x": numerics[0], "y": numerics[1]}
        enc["title"] = f"{numerics[1]} vs {numerics[0]}"
        return enc

    # Ultimate fallback: column with whatever we have
    all_cols = list(cols.keys())
    enc["chart_type"] = "column_clustered"
    if len(all_cols) >= 2:
        enc["channels"] = {"category": all_cols[0], "value": all_cols[1]}
    elif len(all_cols) == 1:
        enc["channels"] = {"category": all_cols[0], "value": all_cols[0]}
    else:
        enc["channels"] = {"category": "index", "value": "index"}
    enc["title"] = "Data Overview"
    return enc


# ─── LLM Chooser ────────────────────────────────────────────────────────────

def _build_system_prompt(profile: dict, user_question: str, sql_hint: str, insight: str = "") -> str:
    """Build the system prompt for chart type selection."""
    catalog = get_chart_catalog()
    col_info = json.dumps(
        {col: {"type": p["coerced_type"], "cardinality": p["cardinality"], "samples": p["samples"][:3]}
         for col, p in profile["columns"].items()},
        indent=2
    )

    insight_section = ""
    if insight:
        insight_section = f"""
INSIGHT TEXT (for reference — insight_bullets will be derived separately):
{insight[:300]}
"""

    return f"""You are a chart-type selection engine for PowerPoint presentations.

TASK: Given a data profile, user question, and SQL context, choose the BEST chart type,
map dataframe columns to chart channels, provide a y-axis label, and summarize the insight
into exactly 2 short bullet points for the slide.

AVAILABLE CHART TYPES (pick exactly one):
{catalog}

DATA PROFILE:
- Rows: {profile['n_rows']}
- Columns:
{col_info}

USER QUESTION: {user_question}

SQL CONTEXT (intent hint only): {sql_hint[:500] if sql_hint else 'N/A'}
{insight_section}

RULES:
- Pick the chart_type that best visualizes the user's question.
- Map column names EXACTLY as they appear in the profile to channels.
- PRIORITY: When both share/percentage columns AND count/patient columns exist, prefer the share column as the "value" channel UNLESS the user explicitly asks about counts or volume.
- For temporal data on x-axis, use the "category" channel.
- If data has composition/share values summing to ~100%, prefer stacked or pie.
- If data shows trends over time, prefer line.
- If comparing categories, prefer bar or column.
- Only use channels that the chart_type requires/supports.

Return JSON with EXACTLY these keys:
- chart_type: one of the enum values listed above
- channels: object mapping channel name to column name
- category_transform: "none" | "quarter" | "month" | "year"
- aggregate: "sum" | "mean" | "count" | "none"
- category_sort_column: the column name to sort the category axis chronologically (e.g. "period_order", "date", "year_month", "quarter"). Pick any column that provides chronological ordering for the category axis. If the category column itself is a parseable date (e.g. "2024-01-15", "Q1 2024"), set to "auto". If no ordering is possible, set to "none".
- title: short descriptive chart title
- y_axis_label: intuitive axis label that combines the metric meaning from the question with the unit. Examples: "Patient Share (%)", "NPS Patient Count", "Next Therapy Share (%)", "Market Share (%)", "Prescription Volume", "Total Patients". Derive from the user question context — not just the column name.
- insight_bullets: array of 2 short strings (can be empty [] — will be overridden by the system with actual insight sentences).
- rationale: 1-sentence explanation of your choice"""



def _enforce_temporal_axis(enc: dict, profile: dict) -> dict:
    """Post-validation override: when data has a temporal column, ensure it's on the x-axis
    AND the chart type is line (not column/bar with too many series).
    
    Rules:
    1. If temporal column exists but NOT on x-axis → force line chart with time on x
    2. If temporal IS on x-axis but chart_type is column/bar → force line chart
       (column_clustered with 50 series = 50 bars per group = unreadable/corrupt PPT)
    """
    cols = profile["columns"]
    temporals = [c for c, p in cols.items() if p["coerced_type"] == "temporal"]
    categoricals = [c for c, p in cols.items() if p["coerced_type"] == "categorical"]
    numerics = [c for c, p in cols.items() if p["coerced_type"] == "numeric"]

    # FALLBACK: if profiler missed temporal columns, detect by column name keywords
    # This catches cases where DATE dtype wasn't coerced (string "2026-07-24" stays as categorical)
    if not temporals:
        _date_keywords = ("date", "week", "month", "quarter", "year", "period", "time", "day")
        name_temporals = [c for c in categoricals if any(k in c.lower() for k in _date_keywords)]
        if name_temporals:
            # Verify at least one value looks like a date
            for candidate in name_temporals:
                samples = cols[candidate].get("samples", [])
                sample_str = str(samples[0]) if samples else ""
                import re as _re
                if _re.match(r"\d{4}[-/]\d{2}[-/]\d{2}", sample_str) or                    _re.match(r"Q[1-4]", sample_str, _re.IGNORECASE) or                    _re.match(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", sample_str, _re.IGNORECASE):
                    temporals = [candidate]
                    categoricals = [c for c in categoricals if c != candidate]
                    print(f"[PPT_LLM] FALLBACK: detected temporal column '{candidate}' by name+sample")
                    break

    if not temporals or not numerics:
        return enc  # No temporal data — nothing to override

    channels = enc.get("channels", {})
    cat_col = channels.get("category", "")
    chart_type = enc.get("chart_type", "")

    # Rule 1: temporal column exists but NOT on x-axis → force line chart
    if cat_col not in temporals and categoricals and temporals:
        print(f"[PPT_LLM] OVERRIDE: temporal column '{temporals[0]}' not on x-axis — forcing line chart")
        val_col = channels.get("value", numerics[0])
        if val_col not in numerics:
            val_col = numerics[0]
        series_col = cat_col if cat_col in categoricals else categoricals[0]

        enc["chart_type"] = "line"
        enc["channels"] = {
            "category": temporals[0],
            "value": val_col,
            "series": series_col,
        }
        enc["title"] = enc.get("title", f"{val_col} over {temporals[0]} by {series_col}")
        enc["category_sort_column"] = "auto"
        return enc

    # Rule 2: temporal IS on x-axis but chart is column/bar with a series column
    # → force line chart (multi-series column charts with >8 series render as empty/corrupt in PPT)
    bar_types = ("column_clustered", "column_stacked", "bar_clustered", "bar_stacked")
    if cat_col in temporals and chart_type in bar_types and channels.get("series"):
        series_col = channels["series"]
        # Check cardinality of the series column
        series_card = cols.get(series_col, {}).get("cardinality", 0)
        if series_card > 8:
            print(f"[PPT_LLM] OVERRIDE: {chart_type} with {series_card} series on temporal axis — forcing line chart")
            enc["chart_type"] = "line"
        else:
            # ≤8 series in column chart is fine, don't override
            pass
        return enc

    return enc


def choose_chart(llm_client, profile: dict, user_question: str, sql_hint: str,
                 insight: str = "", model: str = "", max_retries: int = 2) -> dict:
    """Use LLM to choose chart type and channel mapping.
    
    Falls back to deterministic_fallback on any failure.
    """
    system_prompt = _build_system_prompt(profile, user_question, sql_hint, insight)

    for attempt in range(max_retries + 1):
        try:
            print(f"[PPT_LLM] Attempt {attempt + 1}/{max_retries + 1}")
            resp = llm_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Select the best chart type and return the JSON encoding."},
                ],
                model=model,
                temperature=0,
                max_tokens=500,
            )
            text = resp.choices[0].message.content.strip()
            print(f"[PPT_LLM] Raw response: {text}")

            # Extract JSON
            import re
            cleaned = re.sub(r'```(?:json)?\s*', '', text)
            cleaned = cleaned.replace('```', '')
            start = cleaned.find('{')
            if start == -1:
                raise ValueError("No JSON object in response")
            
            # Find matching brace
            depth = 0
            for i in range(start, len(cleaned)):
                if cleaned[i] == '{':
                    depth += 1
                elif cleaned[i] == '}':
                    depth -= 1
                    if depth == 0:
                        json_str = cleaned[start:i+1]
                        break
            else:
                raise ValueError("Unclosed JSON")

            enc = json.loads(json_str)
            print(f"[PPT_LLM] Parsed enc: chart_type={enc.get('chart_type')}, channels={enc.get('channels')}")

            # Validate
            is_valid, error = validate_enc(enc, profile)
            if is_valid:
                # POST-VALIDATION: force temporal on x-axis when time data exists
                enc = _enforce_temporal_axis(enc, profile)
                print(f"[PPT_LLM] Validation PASSED")
                return enc
            else:
                print(f"[PPT_LLM] Validation FAILED: {error}")
                if attempt < max_retries:
                    # Re-prompt with error
                    system_prompt += f"\n\nPREVIOUS ATTEMPT FAILED VALIDATION: {error}\nPlease fix and try again."
                continue

        except Exception as e:
            print(f"[PPT_LLM] Exception on attempt {attempt + 1}: {type(e).__name__}: {e}")
            if attempt < max_retries:
                continue

    # All attempts failed -> deterministic fallback
    print(f"[PPT_LLM] All attempts failed. Using deterministic fallback.")
    enc = deterministic_fallback(profile)
    enc = _enforce_temporal_axis(enc, profile)
    print(f"[PPT_LLM] Fallback enc: {enc}")
    return enc
