"""PPT Agent - Main Orchestrator.

Receives request from app.py, profiles data,
chooses chart via LLM, builds and renders the PPTX in-memory.
Returns bytes directly — no file storage.
"""

import io
import json
import time
import traceback
import pandas as pd

from .profiler import profile_df
from .llm_chooser import choose_chart
from .chart_builder import render_pptx

import re as _re


def _strip_markdown(text: str) -> str:
    """Remove markdown bold/italic markers for PPT plain text."""
    return _re.sub(r'\*{1,3}', '', text)


def extract_insight_bullets(insight: str, llm_client=None, model: str = "") -> list:
    """Extract 2 insight bullets for PPT, each 150-200 chars max.

    Uses LLM to pick the 2 most important data points from the insight,
    preserving all drug names, numbers, and metric names.
    Falls back to first 2 sentences (truncated) if LLM fails.
    """
    if not insight:
        return []

    clean = _strip_markdown(insight).strip()

    # Try LLM extraction
    if llm_client and model:
        try:
            prompt = f"""From this insight paragraph, write exactly 2 bullet points for a PowerPoint slide.

RULES:
- Each bullet MUST be between 100-180 characters (hard limit: 180 chars max)
- Pick the 2 most important findings with the LATEST data
- MUST preserve ALL drug names, metric names, percentages, and numbers exactly as written
- Do NOT summarize numbers (32.4% stays 32.4%, not "about 32%")
- Do NOT add any new information
- Write as concise factual statements, no filler words
- Return ONLY a JSON array of 2 strings, nothing else

INSIGHT:
{clean}

Return format: ["bullet 1 text here", "bullet 2 text here"]"""

            resp = llm_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0,
                max_tokens=400,
            )
            text = resp.choices[0].message.content.strip()
            # Parse JSON array
            arr_start = text.find('[')
            arr_end = text.rfind(']')
            if arr_start != -1 and arr_end != -1:
                bullets = json.loads(text[arr_start:arr_end + 1])
                if isinstance(bullets, list) and len(bullets) >= 2:
                    # Enforce hard cap
                    bullets = [b[:180] for b in bullets[:2]]
                    print(f"[PPT_INSIGHT] LLM bullets: {[len(b) for b in bullets]} chars")
                    return bullets
        except Exception as e:
            print(f"[PPT_INSIGHT] LLM failed: {e}")

    # Fallback: first 2 sentences, truncated at 180 chars
    sentences = _re.split(r'(?<=[.!?])\s+', clean)
    sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 15]
    bullets = []
    for s in sentences[:2]:
        if len(s) <= 180:
            bullets.append(s)
        else:
            # Cut at last comma before 180
            trunc = s[:180]
            last_break = max(trunc.rfind(', '), trunc.rfind('; '))
            if last_break > 90:
                bullets.append(trunc[:last_break])
            else:
                bullets.append(trunc[:trunc.rfind(' ')].rstrip())
    return bullets


# ─── Data extraction ─────────────────────────────────────────────────────────

def _payload_to_dataframe(statement_response: dict) -> pd.DataFrame:
    """Convert statement_response (manifest + data_array) to a DataFrame."""
    if not statement_response:
        raise ValueError("No statement_response provided")

    manifest = statement_response.get("manifest", {})
    schema = manifest.get("schema", {})
    columns_meta = schema.get("columns", [])
    result = statement_response.get("result", {})
    data_array = result.get("data_array", [])

    if not columns_meta or not data_array:
        raise ValueError(f"Empty data: {len(columns_meta)} columns, {len(data_array)} rows")

    col_names = [c["name"] for c in columns_meta]
    df = pd.DataFrame(data_array, columns=col_names)

    # Coerce columns based on type_name hints from statement manifest
    for col_info in columns_meta:
        col_name = col_info["name"]
        type_name = col_info.get("type_name", "STRING").upper()
        if type_name in ("DOUBLE", "FLOAT", "DECIMAL", "INT", "BIGINT", "LONG", "TINYINT", "SMALLINT"):
            df[col_name] = pd.to_numeric(df[col_name], errors="coerce")
        elif type_name in ("DATE", "TIMESTAMP", "TIMESTAMP_NTZ"):
            df[col_name] = pd.to_datetime(df[col_name], errors="coerce")

    print(f"[PPT_AGENT] DataFrame: {len(df)} rows x {len(df.columns)} cols")
    print(f"[PPT_AGENT]   columns = {list(df.columns)}")
    print(f"[PPT_AGENT]   dtypes  = {dict(df.dtypes)}")
    return df


# ─── Main orchestrator ───────────────────────────────────────────────────────

def generate_ppt(
    thread_id: str,
    message_id: str,
    statement_response: dict,
    sql: str,
    question: str,
    metric_name: str,
    insight: str = "",
    llm_client=None,
    model: str = "",
) -> dict:
    """Main entry point: generate a PPT chart from query results.

    Returns: {"success": bool, "pptx_bytes": bytes | None, "error": str | None}
    The pptx_bytes are the in-memory file content ready to send to the client.
    """
    print(f"\n[PPT_AGENT] {'='*50}")
    print(f"[PPT_AGENT] START generate_ppt")
    print(f"[PPT_AGENT]   thread_id    = {thread_id}")
    print(f"[PPT_AGENT]   message_id   = {message_id}")
    print(f"[PPT_AGENT]   question     = {question[:100]}")
    print(f"[PPT_AGENT]   metric_name  = {metric_name}")
    print(f"[PPT_AGENT]   has_sql      = {bool(sql)}")
    t_start = time.perf_counter()

    try:
        # 1. Parse data
        print(f"[PPT_AGENT] Step 1: Parsing statement_response to DataFrame...")
        df = _payload_to_dataframe(statement_response)

        if df.empty:
            return {"success": False, "error": "No data available to chart", "pptx_bytes": None}

        # 2. Profile
        print(f"[PPT_AGENT] Step 2: Profiling DataFrame...")
        profile = profile_df(df)
        print(f"[PPT_AGENT]   type_summary = {profile.get('type_summary')}")

        # 3. LLM chart selection
        print(f"[PPT_AGENT] Step 3: LLM chart type selection...")
        enc = choose_chart(
            llm_client=llm_client,
            profile=profile,
            user_question=question,
            sql_hint=sql or "",
            insight=insight or "",
            model=model,
        )

        # Override insight_bullets with LLM-condensed bullets (≤180 chars each)
        if insight:
            enc["insight_bullets"] = extract_insight_bullets(
                insight, llm_client=llm_client, model=model
            )
        print(f"[PPT_AGENT]   chosen chart = {enc.get('chart_type')}")
        print(f"[PPT_AGENT]   channels     = {enc.get('channels')}")
        print(f"[PPT_AGENT]   title        = {enc.get('title')}")

        # 4. Render PPTX to memory
        print(f"[PPT_AGENT] Step 4: Rendering PPTX in-memory...")
        pptx_buffer = render_pptx(df, enc)

        elapsed = round(time.perf_counter() - t_start, 2)
        print(f"[PPT_AGENT] SUCCESS in {elapsed}s ({len(pptx_buffer.getvalue())} bytes)")
        print(f"[PPT_AGENT] {'='*50}")

        return {
            "success": True,
            "pptx_bytes": pptx_buffer,
            "chart_type": enc.get("chart_type"),
            "title": enc.get("title"),
            "elapsed": elapsed,
        }

    except Exception as e:
        elapsed = round(time.perf_counter() - t_start, 2)
        print(f"[PPT_AGENT] FAILED in {elapsed}s: {type(e).__name__}: {e}")
        print(f"[PPT_AGENT] Traceback:\n{traceback.format_exc()}")
        print(f"[PPT_AGENT] {'='*50}")
        return {"success": False, "error": str(e), "pptx_bytes": None}
