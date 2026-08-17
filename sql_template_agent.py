"""HIV C3PO v3 - Executor with streaming progress + supervisor fallback"""

import json, re, time, math, os, requests, traceback
from pathlib import Path
import pandas as pd
import openai
from databricks import sql as databricks_sql
from config import (
    WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
    VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD, UPPER_SCORE_THRESHOLD,
    SUPERVISOR_ENDPOINT_URL,
)
from utils.sql_execution import _execute_sql_with_params, _get_databricks_pat,execute_sql ,_get_type_name , execute_sql_with_types
from utils.llm_client import *
from utils.format_data_to_statement_response import *
from supervisor_agent import *



def escape_sql(value):
    return (value or "").replace("'", "''")


# ── Dynamic latest-quarter cache ──────────────────────────────────────────────
_latest_quarter_cache = {"value": None, "timestamp": 0}
_QUARTER_CACHE_TTL = 300  # 5 minutes

def _get_latest_quarter(token=None):
    """Fetch the latest quarter from the template examples table, cached for 5 min.
    Falls back to Q4'25 if query fails."""
    import time as _time
    now = _time.time()
    if _latest_quarter_cache["value"] and (now - _latest_quarter_cache["timestamp"]) < _QUARTER_CACHE_TTL:
        return _latest_quarter_cache["value"]
    try:
        SUGGESTION_QUESTIONS_TABLE = os.environ.get("SUGGESTION_QUESTIONS_TABLE", "")
        sql = f"""
        SELECT DISTINCT example_slot_values_json
        FROM {SUGGESTION_QUESTIONS_TABLE}
        WHERE is_active = true AND example_slot_values_json IS NOT NULL
        LIMIT 50
        """
        df = execute_sql(sql, token)
        # Parse all slot JSONs and find the max quarter value
        quarters = []
        for val in df["example_slot_values_json"].dropna():
            try:
                slots = json.loads(val) if isinstance(val, str) else val
                q = slots.get("quarter") or slots.get("wave")
                if q and isinstance(q, str) and len(q) >= 4:
                    quarters.append(q)
            except (json.JSONDecodeError, AttributeError):
                pass
        if quarters:
            # Sort quarters: Q4'25 > Q3'25 > Q2'25 etc.
            # Format is Q{1-4}'{YY} — sort by year desc then quarter desc
            def quarter_sort_key(q):
                import re
                m = re.match(r"Q([1-4])'(\d{2})", q)
                if m:
                    return (int(m.group(2)), int(m.group(1)))
                return (0, 0)
            quarters.sort(key=quarter_sort_key, reverse=True)
            latest = quarters[0]
            _latest_quarter_cache["value"] = latest
            _latest_quarter_cache["timestamp"] = now
            print(f"[QUARTER_CACHE] Refreshed: latest quarter = {latest}")
            return latest
    except Exception as e:
        print(f"[QUARTER_CACHE] Query failed: {e} — using fallback")
    # Fallback
    fallback = "Q4'25"
    _latest_quarter_cache["value"] = fallback
    _latest_quarter_cache["timestamp"] = now
    return fallback



# ── Result cache for repeated questions ───────────────────────────────────────
_result_cache = {}
_RESULT_CACHE_TTL = 300  # 5 minutes

def _get_cached_result(masked_question, slot_values):
    """Check if we have a cached result for this exact query."""
    cache_key = (masked_question, tuple(sorted(slot_values.items())))
    entry = _result_cache.get(cache_key)
    if entry and (time.time() - entry["timestamp"]) < _RESULT_CACHE_TTL:
        print(f"[CACHE] HIT for: {masked_question[:60]} | slots={slot_values}")
        return entry["result"]
    return None

def _set_cached_result(masked_question, slot_values, result):
    """Store a successful result in cache."""
    cache_key = (masked_question, tuple(sorted(slot_values.items())))
    _result_cache[cache_key] = {"result": result, "timestamp": time.time()}
    print(f"[CACHE] SET for: {masked_question[:60]} | slots={slot_values}")
    # Evict old entries (keep cache bounded)
    if len(_result_cache) > 100:
        oldest_key = min(_result_cache, key=lambda k: _result_cache[k]["timestamp"])
        _result_cache.pop(oldest_key, None)


def _make_serializable(df):
    for col in df.columns:
        if hasattr(df[col], "dt"):
            df[col] = df[col].astype(str)
        elif df[col].dtype == "object":
            df[col] = df[col].fillna("")
    df = df.where(df.notna(), None)
    clean_data = []
    for row in df.values.tolist():
        clean_data.append([
            "" if (v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))))
            else v
            for v in row
        ])
    return clean_data, df.columns.tolist()



def _extract_json_object(text: str) -> str:
    """Extract the first top-level JSON object from text using brace-depth counting.
    
    This is more reliable than regex `\{.*\}` which is greedy and can capture
    trailing garbage or fail on nested braces.
    """
    # Strip markdown code fences if present (```json ... ```)
    cleaned = re.sub(r'```(?:json)?\s*', '', text)
    cleaned = cleaned.replace('```', '')
    
    start = cleaned.find('{')
    if start == -1:
        raise ValueError(f"No JSON object found in LLM response: {text[:300]}")
    
    depth = 0
    in_string = False
    escape_next = False
    
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            if in_string:
                escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return cleaned[start:i+1]
    
    # Fallback: depth never reached 0 — try the greedy regex as last resort
    m = re.search(r'\{.*\}', text, flags=re.DOTALL)
    if m:
        return m.group(0)
    raise ValueError(f"Unclosed JSON object in LLM response: {text[:300]}")


def call_llm_json(llm_client, system_prompt, user_payload, max_tokens=800):
    print(f"[LLM_JSON] Calling LLM | model={LLM_MODEL} | max_tokens={max_tokens}")
    print(f"[LLM_JSON]   system_prompt = {system_prompt[:100]}...")
    print(f"[LLM_JSON]   user_payload_keys = {list(user_payload.keys()) if isinstance(user_payload, dict) else 'not_dict'}")
    t_llm = time.perf_counter()
    try:
        resp = llm_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            model=LLM_MODEL, temperature=0, max_tokens=max_tokens,
        )
    except Exception as llm_err:
        print(f"[LLM_JSON] !! LLM API call FAILED: {type(llm_err).__name__}: {llm_err}")
        raise
    elapsed = round(time.perf_counter() - t_llm, 2)
    text = resp.choices[0].message.content.strip()
    print(f"[LLM_JSON]   response_time  = {elapsed}s")
    print(f"[LLM_JSON]   response_len   = {len(text)} chars")
    print(f"[LLM_JSON]   raw_response   = {text[:300]}")
    json_str = _extract_json_object(text)
    try:
        parsed = json.loads(json_str)
        print(f"[LLM_JSON]   parsed_keys    = {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}")
        return parsed
    except json.JSONDecodeError as e:
        print(f"[LLM_JSON] !! JSON PARSE ERROR at char {e.pos}: {e.msg}")
        print(f"[LLM_JSON]   extracted_json = {json_str[:500]}")
        print(f"[LLM_JSON]   full_response  = {text[:500]}")
        raise


def generate_insight(user_question, slot_values, result_data, result_columns, token=None):
    try:
        if not result_data:
            return ""
        print("[AUTH] generate_insight LLM: PAT(_get_databricks_pat)")
        llm_client = get_llm_client(_get_databricks_pat())
        df = pd.DataFrame(result_data, columns=result_columns)
        # Send ALL data — no truncation. Typical datasets are ≤50 rows, well within context.
        data_summary = df.to_string(index=False)
        print(f"[INSIGHT] Sending {len(df)} rows ({len(data_summary)} chars) to LLM")

        # Read insight prompt from file (not hardcoded)
        insight_prompt_path = Path(__file__).resolve().parent / "system_files" / "generate_insight_prompt.txt"
        system_prompt = insight_prompt_path.read_text(encoding="utf-8")

        resp = llm_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Question: {user_question}\nParams: {json.dumps(slot_values)}\nData ({len(df)} rows):\n{data_summary}"},
            ],
            model=LLM_MODEL, temperature=0.1, max_tokens=250,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[INSIGHT] Exception: {type(e).__name__}: {e}")
        return ""



def generate_insight_streaming(user_question, slot_values, result_data, result_columns, session, token=None):
    """Stream insight tokens into session['streaming_insight'] for real-time frontend updates.
    
    Falls back to non-streaming on error. Returns the final complete insight string.
    Thread-safe: single writer (this thread), readers poll session dict (GIL-safe pointer swap).
    """
    try:
        if not result_data:
            return ""
        llm_client = get_llm_client(_get_databricks_pat())
        df = pd.DataFrame(result_data, columns=result_columns)
        data_summary = df.to_string(index=False)
        print(f"[INSIGHT_STREAM] Sending {len(df)} rows ({len(data_summary)} chars) to LLM (streaming)")

        insight_prompt_path = Path(__file__).resolve().parent / "system_files" / "generate_insight_prompt.txt"
        system_prompt = insight_prompt_path.read_text(encoding="utf-8")

        stream = llm_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Question: {user_question}\nParams: {json.dumps(slot_values)}\nData ({len(df)} rows):\n{data_summary}"},
            ],
            model=LLM_MODEL, temperature=0.1, max_tokens=250,
            stream=True,
        )

        accumulated = ""
        chunk_count = 0
        for chunk in stream:
            chunk_count += 1
            # Robust extraction: handle various chunk formats from Databricks serving
            try:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                token_text = getattr(delta, "content", None)
                if not token_text:
                    continue
                accumulated += token_text
                # Write to session for frontend polling (atomic pointer swap under GIL)
                session["streaming_insight"] = accumulated
            except (IndexError, AttributeError):
                continue

        final_insight = accumulated.strip()
        session["streaming_insight"] = final_insight
        print(f"[INSIGHT_STREAM] Complete: {len(final_insight)} chars ({chunk_count} chunks)")
        return final_insight

    except Exception as e:
        print(f"[INSIGHT_STREAM] Exception: {type(e).__name__}: {e}")
        # Return whatever was accumulated (graceful degradation)
        partial = session.get("streaming_insight", "")
        if partial:
            print(f"[INSIGHT_STREAM] Returning {len(partial)} chars of partial insight")
            return partial.strip()
        # Complete fallback: try non-streaming
        print("[INSIGHT_STREAM] Falling back to non-streaming generate_insight")
        return generate_insight(user_question, slot_values, result_data, result_columns, token)


def _normalise_quarter(val):
    """Normalise LLM quarter output to Q1'25 format.
    Handles: Q1 2025, Q12025, Q1'25, Q125, 2025 Q1, q1'25 etc."""
    import re
    v = str(val).strip()
    if re.match(r"^Q[1-4]'\d{2}$", v):
        return v
    m = re.match(r"[Qq]([1-4])\s*['\'']?\s*(20)?(\d{2})$", v)
    if m:
        return f"Q{m.group(1)}'{m.group(3)}"
    m = re.match(r"(20)?(\d{2})\s*[Qq]([1-4])", v)
    if m:
        return f"Q{m.group(3)}'{m.group(2)}"
    return v


def step_extract_slots(llm_client, user_question, template_row):
    slot_contract_raw = template_row.get("slot_output_contract_json", "{}")
    try:
        slot_contract = json.loads(slot_contract_raw) if isinstance(slot_contract_raw, str) else (slot_contract_raw or {})
    except json.JSONDecodeError as e:
        print(f"[SLOTS] Failed to parse slot_output_contract_json: {e}")
        print(f"[SLOTS] Raw value ({len(str(slot_contract_raw))} chars): {str(slot_contract_raw)[:300]}")
        raise ValueError(f"Malformed slot_output_contract_json in template: {e}")
    required_keys = slot_contract.get("required_keys", [])

    system_prompt_path = Path(__file__).resolve().parent / "system_files" /"sql_template_agent_slot_extraction.txt"

    system_prompt = system_prompt_path.read_text(encoding="utf-8")

    system_prompt = system_prompt.format(
        required_keys = json.dumps(required_keys),
        slot_contract_rules = json.dumps(slot_contract.get("rules", []))
    )
    slot_values = call_llm_json(llm_client, system_prompt, {
        "user_question": user_question,
        "masked_question": template_row.get("masked_question"),
        "sql_template_preview": str(template_row.get("sql_template", ""))[:500],
        "required_keys": required_keys,
    }, max_tokens=500)

    latest_q = _get_latest_quarter(token=None)
    LATEST_DEFAULTS = {"wave": latest_q, "quarter": latest_q}
    result = {}
    for k in required_keys:
        raw = slot_values.get(k)
        if not raw or str(raw).lower() in ("null", "none", ""):
            if k in LATEST_DEFAULTS:
                v = LATEST_DEFAULTS[k]
                print(f"[SLOTS] '{k}' not found in question — defaulting to {v}")
            else:
                raise ValueError(f"Required slot '{k}' could not be extracted")
        else:
            v = str(raw).strip()
        if k in ("wave", "quarter"):
            v = _normalise_quarter(v)
        result[k] = v

    print(f"[SLOTS] Final extracted slots: {result}")
    print(f"[SLOTS]   keys = {list(result.keys())}")
    for k, v in result.items():
        print(f"[SLOTS]   {k} = '{v}'")
    return result


def step_render_sql(sql_template, slot_values):
    """Replace {key} placeholders with raw values."""
    print(f"[SQL] Rendering SQL template ({len(sql_template)} chars)")
    print(f"[SQL]   slot_values = {slot_values}")
    sql = sql_template
    for k, v in slot_values.items():
        placeholder = "{" + k + "}"
        if placeholder not in sql:
            print(f"[SQL]   WARNING: placeholder '{placeholder}' NOT FOUND in template!")
        sql = sql.replace(placeholder, v)
    # Check for unreplaced placeholders
    import re as _re
    remaining = _re.findall(r'\{[a-z_]+\}', sql)
    if remaining:
        print(f"[SQL]   WARNING: unreplaced placeholders still in SQL: {remaining}")
    print(f"[SQL]   final_sql ({len(sql)} chars): {sql[:500]}")
    return sql


MASK_PROMPT_PATH = Path(__file__).resolve().parent / "system_files" / "sql_template_agent_mask_prompt.txt"
MASK_PROMPT = MASK_PROMPT_PATH.read_text(encoding="utf-8")


def _build_judge_prompt(template_row):
    slot_contract_raw = template_row.get("slot_output_contract_json", "{}")
    try:
        slot_contract = json.loads(slot_contract_raw) if isinstance(slot_contract_raw, str) else (slot_contract_raw or {})
    except json.JSONDecodeError as e:
        print(f"[JUDGE] Failed to parse slot_output_contract_json: {e}")
        slot_contract = {}
    supported_params = slot_contract.get("required_keys", [])
    params_str = ", ".join(supported_params) if supported_params else "none"

    judge_prompt_path = Path(__file__).resolve().parent / "system_files" / "sql_template_agent_llm_judge_prompt.txt"
    
    judge_prompt = judge_prompt_path.read_text(encoding="utf-8")
    
    judge_prompt=judge_prompt.format(metric_name=str(template_row.get("metric_name", "")),
                                     answer_type=str(template_row.get("answer_type", "")),params_str=params_str)
    
    return judge_prompt


def step_resolve_context(llm_client, user_question, conversation_history):
    """If conversation history exists, attempt to resolve context.
    Only revises vague/incomplete follow-ups — complete questions pass through unchanged."""
    
    if not conversation_history:
        print(f"[CONTEXT] No conversation history - using original question")
        return user_question
    print(f"[CONTEXT] Resolving context | history_len={len(conversation_history)} chars")
    print(f"[CONTEXT]   original_question = {user_question[:150]}")
    print(f"[CONTEXT]   history_preview   = {conversation_history[:200]}")
    
    revise_context_prompt_path = Path(__file__).resolve().parent / "system_files" / "revise_context.txt"
    
    prompt = revise_context_prompt_path.read_text(encoding="utf-8")
    try:
        result = call_llm_json(llm_client, prompt, {
            "conversation_history": conversation_history,
            "follow_up_question": user_question
        }, max_tokens=300)
        resolved = result.get("resolved_question", user_question).strip()
        was_revised = result.get("was_revised", resolved != user_question)
        if was_revised and resolved != user_question:
            print(f"[CONTEXT] ✓ REVISED: '{user_question}' → '{resolved}'")
        else:
            print(f"[CONTEXT] ✓ NOT REVISED (complete question): '{user_question[:100]}'")
        return resolved
    except Exception as e:
        print(f"[CONTEXT] !! Exception during resolve: {type(e).__name__}: {e}")
        return user_question


def step_mask_question(llm_client, user_question):
    print(f"[MASK] Masking question: {user_question[:100]}")
    result = call_llm_json(llm_client, MASK_PROMPT, {"user_question": user_question})
    masked = result["masked_question"].strip().rstrip("?.! ")
    print(f"[MASK] Result: {masked}")
    return masked


def step_exact_match(user_question, token=None):
    """Case-insensitive exact match against template question column."""
    
    q = escape_sql(user_question.strip())
    
    SUGGESTION_QUESTIONS_TABLE = os.environ.get("SUGGESTION_QUESTIONS_TABLE","")
    
    sql = f"""
    SELECT example_id,template_id, template_group, metric_name, question,
           masked_question, sql_template, masking_contract_json,
           slot_output_contract_json, answer_type, is_active, example_slot_values_json
    FROM {SUGGESTION_QUESTIONS_TABLE}
    WHERE is_active = true
      AND LOWER(question) = LOWER('{q}')
    LIMIT 1
    """
    try:
        df = execute_sql(sql, token)
        if df.empty:
            print(f"[EXACT MATCH] No match for: {user_question[:80]}")
            return None
        row = df.iloc[0].to_dict()
        print(f"[EXACT MATCH] Found template_id={row.get('template_id')} for: {user_question[:80]}")
        return row
    except Exception as e:
        print(f"[EXACT MATCH] Query failed: {e} — falling through to semantic path")
        return None


def fetch_template_by_example_id(example_id, token=None):
    """Securely fetch a template row by example_id from the database.
    Used to re-validate client-supplied template data before execution."""
    SUGGESTION_QUESTIONS_TABLE = os.environ.get("SUGGESTION_QUESTIONS_TABLE", "")
    sql = f"""
    SELECT example_id, template_id, template_group, metric_name, question,
           masked_question, sql_template, masking_contract_json,
           slot_output_contract_json, answer_type, is_active, example_slot_values_json
    FROM {SUGGESTION_QUESTIONS_TABLE}
    WHERE is_active = true AND example_id = ?
    LIMIT 1
    """
    try:
        df = _execute_sql_with_params(sql, [str(example_id)], token)
        if df.empty:
            print(f"[FETCH_TEMPLATE] No active template found for example_id={example_id}")
            return None
        row = df.iloc[0].to_dict()
        print(f"[FETCH_TEMPLATE] Fetched template_id={row.get('template_id')} for example_id={example_id}")
        return row
    except Exception as e:
        print(f"[FETCH_TEMPLATE] Query failed for example_id={example_id}: {e}")
        return None


def step_retrieve_template(masked_question, token=None):
    sql = f"SELECT * FROM vector_search(index => '{VECTOR_INDEX_NAME}', query_text => ?, num_results => 3) WHERE is_active = true ORDER BY search_score DESC LIMIT 1"
    print(f"[RETRIEVE] Vector search | masked_question: {masked_question[:100]}")
    t_vs = time.perf_counter()
    df = _execute_sql_with_params(sql, [masked_question], token)
    elapsed = round(time.perf_counter() - t_vs, 2)
    if df.empty:
        print(f"[RETRIEVE] !! NO RESULTS from vector search in {elapsed}s")
        raise ValueError(f"No template found for: {masked_question}")
    row = df.iloc[0].to_dict()
    row["_search_score"] = row.get("search_score", row.get("_score", row.get("score", 0)))
    print(f"[RETRIEVE] Found template in {elapsed}s:")
    print(f"[RETRIEVE]   template_id    = {row.get('template_id', '?')}")
    print(f"[RETRIEVE]   metric_name    = {row.get('metric_name', '?')}")
    print(f"[RETRIEVE]   search_score   = {row['_search_score']}")
    print(f"[RETRIEVE]   question       = {str(row.get('question', ''))[:80]}")
    print(f"[RETRIEVE]   masked_q       = {str(row.get('masked_question', ''))[:80]}")
    return row


def step_llm_judge(llm_client, user_question, masked_question, template_row):
    print(f"[JUDGE] Evaluating match:")
    print(f"[JUDGE]   user_question     = {user_question[:80]}")
    print(f"[JUDGE]   masked_question   = {masked_question[:80]}")
    print(f"[JUDGE]   template_id       = {template_row.get('template_id', '?')}")
    print(f"[JUDGE]   template_metric   = {template_row.get('metric_name', '?')}")
    return call_llm_json(llm_client, _build_judge_prompt(template_row), {
        "user_question": user_question,
        "masked_question": masked_question,
        "retrieved_template_id": template_row.get("template_id", ""),
        "retrieved_example_question": template_row.get("question", ""),
        "supported_parameters": (json.loads(template_row.get("slot_output_contract_json", "{}")) if isinstance(template_row.get("slot_output_contract_json"), str) else (template_row.get("slot_output_contract_json") or {})).get("required_keys", []),
    }, max_tokens=200)




# ===================================================================
# STREAMING EXECUTORS
# ===================================================================

def execute_click_path_streaming(user_question, template_row, progress_fn=None, token=None, conversation_history=None):
    result = {"success": False, "steps_log": [], "source": "template"}
    print(f"\n[CLICK_PATH] {'='*50}")
    print(f"[CLICK_PATH] START")
    print(f"[CLICK_PATH]   question       = {user_question[:100]}")
    print(f"[CLICK_PATH]   template_id    = {template_row.get('template_id', '?')}")
    print(f"[CLICK_PATH]   metric_name    = {template_row.get('metric_name', '?')}")
    print(f"[CLICK_PATH]   has_sql        = {bool((template_row.get('sql_template') or '').strip())}")
    print(f"[CLICK_PATH]   slot_json      = {str(template_row.get('example_slot_values_json', ''))[:100]}")
    llm_client = get_llm_client(_get_databricks_pat())
    t_start = time.perf_counter()
    try:
        original_question = user_question
        t_ctx = time.perf_counter()
        user_question = step_resolve_context(llm_client, user_question, conversation_history or '')
        result["resolved_question"] = user_question
        result["original_question"] = original_question
        result["conversation_context"] = conversation_history or ''

        was_revised = (user_question != original_question)
        ctx_detail = f"Revised: {user_question[:100]}" if was_revised else "No revision needed"
        result["steps_log"].append({"name": "Resolve Context", "duration": round(time.perf_counter()-t_ctx, 2), "details": ctx_detail})

        sql_template_val = template_row.get("sql_template") or ""
        if not sql_template_val.strip():
            raise ValueError(f"Template {template_row.get('template_id', '?')} has no sql_template defined")

        print("TEMPLATE ROW : ,",template_row)

        if progress_fn: progress_fn("Fetching parameters", "analyzing question...")
        t = time.perf_counter()
        slot_values = template_row.get('example_slot_values_json', '?')
        print("slot values fetched from table ,",slot_values)
        print("type before loads:", type(slot_values))
        slot_values = json.loads(slot_values)
        result["slot_values"] = slot_values
        slot_summary = " | ".join(f"{k}: {v}" for k, v in slot_values.items())
        result["steps_log"].append({"name": "Extract Params", "duration": round(time.perf_counter()-t,2), "details": slot_summary})
        if progress_fn: progress_fn("Parameters extracted", slot_summary)

        if progress_fn: progress_fn("Executing query", "running SQL...")
        t = time.perf_counter()
        final_sql = step_render_sql(sql_template_val, slot_values)
        result["final_sql"] = final_sql
        df, type_names = execute_sql_with_types(final_sql, token)
        data, columns = _make_serializable(df)
        result.update({"result_data": data, "result_columns": columns, "type_names": type_names})
        result["steps_log"].append({"name": "Execute SQL", "duration": round(time.perf_counter()-t,2), "details": f"{len(df)} rows"})
        result["answer_type"] = template_row.get("answer_type", "table_only")
        result["metric_name"] = template_row.get("metric_name", "Metric")
        result["success"] = True
    except Exception as e:
        print(f"[CLICK_PATH] !! EXCEPTION: {type(e).__name__}: {e}")
        print(f"[CLICK_PATH] Traceback:\n{traceback.format_exc()}")
        result["error_msg"] = str(e)
    result["total_time"] = round(time.perf_counter() - t_start, 2)
    print(f"[CLICK_PATH] END | success={result.get('success')} | time={result['total_time']}s | error={result.get('error_msg', 'None')[:100]}")
    print(f"[CLICK_PATH] {'='*50}")
    return result


def execute_semantic_path_streaming(user_question, progress_fn=None, token=None, conversation_history=None, session=None):
    """Semantic path: mask -> retrieve -> judge -> slots -> SQL.
    On judge failure or low score: routes to supervisor."""
    result = {"success": False, "steps_log": [], "source": "template"}
    print(f"\n[SEMANTIC_PATH] {'='*50}")
    print(f"[SEMANTIC_PATH] START")
    print(f"[SEMANTIC_PATH]   question = {user_question[:100]}")
    print(f"[SEMANTIC_PATH]   context  = {bool(conversation_history)}")
    llm_client = get_llm_client(_get_databricks_pat())
    t_start = time.perf_counter()
    try:
        original_question = user_question
        t_ctx = time.perf_counter()
        user_question = step_resolve_context(llm_client, user_question, conversation_history or '')
        result["resolved_question"] = user_question
        result["original_question"] = original_question
        result["conversation_context"] = conversation_history or ''

        # Show resolve context as a visible step
        was_revised = (user_question != original_question)
        ctx_detail = f"Revised: {user_question[:100]}" if was_revised else "No revision needed (standalone question)"
        step0 = {"name": "Resolve Context", "duration": round(time.perf_counter()-t_ctx, 2), "details": ctx_detail}
        result["steps_log"].append(step0)
        if progress_fn: progress_fn([step0])
        t = time.perf_counter()
        masked = step_mask_question(llm_client, user_question)
        step1 = {"name": "Understand", "duration": round(time.perf_counter()-t,2), "details": f"Masked: {masked}"}
        result["steps_log"].append(step1)
        if progress_fn: progress_fn([step0, step1])

        t = time.perf_counter()
        template_row = step_retrieve_template(masked, token=token)
        score = float(template_row.get("_search_score", 0))
        step2 = {"name": "Retrieve", "duration": round(time.perf_counter()-t,2), "details": f"{template_row.get('template_id','?')} (score: {score:.3f})"}
        result["steps_log"].append(step2)
        if progress_fn: progress_fn([step0, step1, step2])

        # ── Score-based routing: skip judge on obvious cases ──
        # Thresholds from app.yaml: SCORE_THRESHOLD (lower), UPPER_SCORE_THRESHOLD (upper)

        if score < SCORE_THRESHOLD:
            # Low score — no point asking judge, route to supervisor directly
            print(f"[SEMANTIC_PATH] Score {score:.3f} < {SCORE_THRESHOLD} — skipping judge, routing to supervisor")
            step3 = {"name": "Judge", "duration": 0, "details": f"Skipped (score {score:.3f} below threshold) → routing to supervisor"}
            result["steps_log"].append(step3)
            if progress_fn: progress_fn([step0, step1, step2, step3])
            sup = execute_supervisor_path_streaming(
                user_question,
                progress_fn=(lambda steps: progress_fn([step0, step1, step2, step3] + steps)) if progress_fn else None,
                token=token,
                conversation_history=conversation_history,
                session=session,
            )
            sup["steps_log"] = result["steps_log"] + sup["steps_log"]
            sup["resolved_question"] = user_question
            sup["original_question"] = original_question
            return sup

        if score > UPPER_SCORE_THRESHOLD:
            # High score — trust retrieval, skip judge
            print(f"[SEMANTIC_PATH] Score {score:.3f} > {UPPER_SCORE_THRESHOLD} — skipping judge, trusting match")
            step3 = {"name": "Judge", "duration": 0, "details": f"Skipped (score {score:.3f} — high confidence match)"}
            result["steps_log"].append(step3)
            if progress_fn: progress_fn([step0, step1, step2, step3])
        else:
            # Ambiguous band — call judge LLM
            t = time.perf_counter()
            judge = step_llm_judge(llm_client, user_question, masked, template_row)
            verdict = judge.get("verdict", "no").lower().strip()
            reason = judge.get("reason", "")
            v_label = "PASS" if verdict == "yes" else "Answering from first principles"
            step3 = {"name": "Judge", "duration": round(time.perf_counter()-t,2), "details": f"{v_label}: {reason}"}
            result["steps_log"].append(step3)
            if progress_fn: progress_fn([step0, step1, step2, step3])

            if verdict != "yes":
                step3["details"] += " → routing to supervisor"
                if progress_fn: progress_fn([step0, step1, step2, step3])
                sup = execute_supervisor_path_streaming(
                    user_question,
                    progress_fn=(lambda steps: progress_fn([step0, step1, step2, step3] + steps)) if progress_fn else None,
                    token=token,
                    conversation_history=conversation_history,
                    session=session,
                )
                sup["steps_log"] = result["steps_log"] + sup["steps_log"]
                sup["resolved_question"] = user_question
                sup["original_question"] = original_question
                return sup

        t = time.perf_counter()
        slot_values = step_extract_slots(llm_client, user_question, template_row)
        result["slot_values"] = slot_values
        slot_summary = " | ".join(f"{k}: {v}" for k, v in slot_values.items())
        step4 = {"name": "Params", "duration": round(time.perf_counter()-t,2), "details": slot_summary}
        result["steps_log"].append(step4)
        if progress_fn: progress_fn([step1, step2, step3, step4])

        # Check cache before executing SQL
        cached = _get_cached_result(masked, slot_values)
        if cached:
            result.update(cached)
            result["steps_log"] = result.get("steps_log", [])
            step5 = {"name": "Execute", "duration": 0, "details": "Cached result (instant)"}
            result["steps_log"].append(step5)
            if progress_fn: progress_fn([step1, step2, step3, step4, step5])
            result["success"] = True
            result["total_time"] = round(time.perf_counter() - t_start, 2)
            return result

        t = time.perf_counter()
        final_sql = step_render_sql(template_row.get("sql_template", ""), slot_values)
        result["final_sql"] = final_sql
        print(f"[SEMANTIC_PATH] Executing SQL ({len(final_sql)} chars)...")
        df, type_names = execute_sql_with_types(final_sql, token)
        print(f"[SEMANTIC_PATH] SQL returned {len(df)} rows, {len(df.columns)} columns")
        data, columns = _make_serializable(df)
        result.update({"result_data": data, "result_columns": columns, "type_names": type_names})
        step5 = {"name": "Execute", "duration": round(time.perf_counter()-t,2), "details": f"{len(df)} rows"}
        result["steps_log"].append(step5)
        if progress_fn: progress_fn([step1, step2, step3, step4, step5])

        result["answer_type"] = template_row.get("answer_type", "table_only")
        result["metric_name"] = template_row.get("metric_name", "Metric")
        result["success"] = True
        # Cache successful result for repeated questions
        _set_cached_result(masked, slot_values, {
            "result_data": result.get("result_data"),
            "result_columns": result.get("result_columns"),
            "type_names": result.get("type_names"),
            "final_sql": result.get("final_sql"),
            "answer_type": result.get("answer_type"),
            "metric_name": result.get("metric_name"),
        })
    except Exception as e:
        print(f"[SEMANTIC_PATH] !! EXCEPTION: {type(e).__name__}: {e}")
        print(f"[SEMANTIC_PATH] Traceback:\n{traceback.format_exc()}")
        result["error_msg"] = str(e)
    result["total_time"] = round(time.perf_counter() - t_start, 2)
    print(f"[SEMANTIC_PATH] END | success={result.get('success')} | time={result['total_time']}s | error={result.get('error_msg', 'None')[:100]}")
    print(f"[SEMANTIC_PATH] {'='*50}")
    return result
