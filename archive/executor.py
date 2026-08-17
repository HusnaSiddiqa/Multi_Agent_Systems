"""HIV C3PO v3 - Executor with streaming progress + supervisor fallback"""

import json, re, time, math, os, requests
import pandas as pd
import openai
from databricks import sql as databricks_sql
from config import (
    WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
    VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD,
    SUPERVISOR_ENDPOINT_URL,
)


def _get_sql_token():
    """PAT for all operations (SQL, LLM, supervisor, vector search, MLflow).
    Has UC catalog + Genie + endpoint access via group membership.
    """
    return os.environ.get("DATABRICKS_PAT", "")


def get_llm_client(token=None):
    api_token = token or _get_sql_token()
    return openai.OpenAI(
        api_key=api_token,
        base_url=f"https://{WORKSPACE_HOST}/serving-endpoints",
    )


def escape_sql(value):
    return (value or "").replace("'", "''")


def execute_sql(sql_query, token=None):
    print(f"[AUTH] execute_sql: {'PAT(_get_sql_token)' if not token else 'explicit_token'}")
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token or _get_sql_token(),
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_query)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns)


def execute_sql_with_types(sql_query, token=None):
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token or _get_sql_token(),
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_query)
            columns = [desc[0] for desc in cursor.description]
            type_names = [_get_type_name(desc[1]) for desc in cursor.description]
            rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns), type_names


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


_TYPE_CODE_MAP = {
    "BOOLEAN":"BOOLEAN","TINYINT":"TINYINT","SMALLINT":"SMALLINT",
    "INT":"INT","INTEGER":"INT","BIGINT":"BIGINT","LONG":"LONG",
    "FLOAT":"FLOAT","DOUBLE":"DOUBLE","DECIMAL":"DECIMAL","NUMERIC":"DECIMAL",
    "STRING":"STRING","VARCHAR":"STRING","CHAR":"STRING",
    "DATE":"DATE","TIMESTAMP":"TIMESTAMP","TIMESTAMP_NTZ":"TIMESTAMP_NTZ",
    "BINARY":"BINARY","ARRAY":"ARRAY","MAP":"MAP","STRUCT":"STRUCT",
}

def _get_type_name(type_code):
    if isinstance(type_code, str):
        return _TYPE_CODE_MAP.get(type_code.upper(), "STRING")
    return "STRING"


def to_statement_response(result_data, result_columns, type_names=None):
    if type_names is None:
        type_names = []
        for i, col in enumerate(result_columns):
            col_lower = col.lower()
            if any(k in col_lower for k in ("date","month","quarter","year","period","week")):
                type_names.append("DATE")
            elif any(k in col_lower for k in ("share","rate","ratio","pct","percent")):
                type_names.append("DOUBLE")
            elif any(k in col_lower for k in ("count","volume","total","num","nbrx","trx","qty")):
                type_names.append("LONG")
            else:
                detected = "STRING"
                for row in result_data[:10]:
                    if i < len(row) and row[i] not in (None, "", "None"):
                        try:
                            float(str(row[i])); detected = "DOUBLE"
                        except (ValueError, TypeError):
                            pass
                        break
                type_names.append(detected)

    columns = [{"name": n, "type_name": t} for n, t in zip(result_columns, type_names)]
    numeric_types = {"DOUBLE", "FLOAT", "DECIMAL", "INT", "BIGINT", "LONG", "TINYINT", "SMALLINT"}
    numeric_cols = {i for i, t in enumerate(type_names) if t in numeric_types}
    data_array = []
    for row in result_data:
        clean_row = []
        for i, v in enumerate(row):
            if v is None or v == "" or (isinstance(v, float) and v != v):
                clean_row.append(None)
            elif i in numeric_cols and isinstance(v, str):
                stripped = v.strip().rstrip("%").strip()
                try:
                    float(stripped)
                    clean_row.append(stripped)
                except (ValueError, TypeError):
                    clean_row.append(str(v))
            else:
                clean_row.append(str(v))
        data_array.append(clean_row)
    return {
        "manifest": {"schema": {"columns": columns}},
        "result": {"data_array": data_array},
    }


def call_llm_json(llm_client, system_prompt, user_payload, max_tokens=800):
    resp = llm_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        model=LLM_MODEL, temperature=0, max_tokens=max_tokens,
    )
    text = resp.choices[0].message.content.strip()
    m = re.search(r'\{.*\}', text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"No JSON in LLM response: {text}")
    return json.loads(m.group(0))


def generate_insight(user_question, slot_values, result_data, result_columns, token=None):
    try:
        if not result_data:
            return []
        print("[AUTH] generate_insight LLM: PAT(_get_sql_token)")
        llm_client = get_llm_client(_get_sql_token())
        df = pd.DataFrame(result_data, columns=result_columns)
        data_summary = df.to_string(index=False, max_rows=15)
        resp = llm_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a concise HIV commercial analytics insight generator. Provide 2-3 sentences. Focus on trend direction and one actionable observation. SHORT, no bullets, no headers."},
                {"role": "user", "content": f"Question: {user_question}\nParams: {json.dumps(slot_values)}\nData:\n{data_summary}"},
            ],
            model=LLM_MODEL, temperature=0.3, max_tokens=150,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return []


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
    slot_contract = json.loads(template_row.get("slot_output_contract_json", "{}"))
    required_keys = slot_contract.get("required_keys", [])
    system_prompt = f"""You are a deterministic slot extraction engine for HIV analytics SQL templates. Return JSON only.
Required keys: {json.dumps(required_keys)}
STRICT RULES — follow exactly:
- Return exactly one JSON object with ONLY the required keys, no extra keys.
- brand / competitor: use the EXACT brand name from the question (Biktarvy, Dovato, Apretude, Descovy, Yeztugo, Cabenuva, Triumeq, Genvoya, Truvada, etc.)
- wave: MUST be formatted as Q4'25 or Q3'25 (Q + quarter number + apostrophe + 2-digit year). Example: "Q4 2025" → "Q4'25", "Q1 2024" → "Q1'24"
- quarter: MUST be formatted as Q1'25 or Q4'24 (same format as wave). Example: "Q4 2024" → "Q4'24"
- Do NOT output "Q4 2024" or "Q42024" or "Q4 24" — ALWAYS use apostrophe: "Q4'24"
- Template-specific rules: {json.dumps(slot_contract.get("rules", []))}"""

    slot_values = call_llm_json(llm_client, system_prompt, {
        "user_question": user_question,
        "masked_question": template_row.get("masked_question"),
        "sql_template_preview": str(template_row.get("sql_template", ""))[:500],
        "required_keys": required_keys,
    }, max_tokens=500)

    LATEST_DEFAULTS = {"wave": "Q4'25", "quarter": "Q4'25"}
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

    print(f"[SLOTS] Extracted: {result}")
    return result


def step_render_sql(sql_template, slot_values):
    """Replace {key} placeholders with raw values."""
    return sql_template.format(**{k: escape_sql(v) for k, v in slot_values.items()})


MASK_PROMPT = """You are a deterministic masking engine for HIV commercial analytics retrieval. Return JSON only: {"masked_question": string}
Rules: lowercase, preserve metric phrase (prscriptions/nbrx/trx/share/volume/switch rate/penetration/awareness/nps/familiarity), replace brand with {brand_name}, time period/wave with {time_period}, regimen with {regimen}, hcp segment with {hcp_segment}. Remove trailing punctuation.- DO NOT Change word prescriptions to Nbrx in revised question."""


def _build_judge_prompt(template_row):
    slot_contract = json.loads(template_row.get("slot_output_contract_json", "{}"))
    supported_params = slot_contract.get("required_keys", [])
    params_str = ", ".join(supported_params) if supported_params else "none"
    return f"""You are a relevance judge for HIV analytics template queries. Return JSON: {{"verdict": "yes" or "no", "reason": "brief"}}

Template capabilities:
- Metric: {template_row.get("metric_name", "")}
- Answer type: {template_row.get("answer_type", "")}
- Supported slots: {params_str}

Rules:
- "yes" if user question asks for the SAME metric type, even with different parameter values.
- Slot value differences are NOT grounds for rejection.
- "no" ONLY if fundamentally different metric or structurally impossible to answer.
- When in doubt and metric matches, prefer "yes"."""


def step_resolve_context(llm_client, user_question, conversation_history):
    """If conversation history exists, always attempt to resolve context."""
    if not conversation_history:
        return user_question
    prompt = """You are a question resolver for HIV analytics. Given a conversation history and a short follow-up question,
rewrite the follow-up as a complete standalone question by inferring missing context (brand, wave/quarter, metric) from the history.
Return JSON: {"resolved_question": string}
Rules:
- If the follow-up specifies a new brand but no wave/metric, keep the same metric and wave from the last question.
- If the follow-up specifies a new wave but no brand/metric, keep the same brand and metric.
- Never invent data not implied by the follow-up or history.
- DO NOT Change word prescriptions to Nbrx in revised question
- If it's already a complete question, return it unchanged."""
    try:
        result = call_llm_json(llm_client, prompt, {
            "conversation_history": conversation_history,
            "follow_up_question": user_question
        }, max_tokens=200)
        resolved = result.get("resolved_question", user_question).strip()
        if resolved and resolved != user_question:
            print(f"[CONTEXT] Resolved '{user_question}' → '{resolved}'")
        return resolved
    except Exception:
        return user_question


def step_mask_question(llm_client, user_question):
    result = call_llm_json(llm_client, MASK_PROMPT, {"user_question": user_question})
    return result["masked_question"].strip().rstrip("?.! ")


def step_exact_match(user_question, token=None):
    """Case-insensitive exact match against template question column."""
    q = escape_sql(user_question.strip())
    sql = f"""
    SELECT template_id, template_group, metric_name, question,
           masked_question, sql_template, masking_contract_json,
           slot_output_contract_json, answer_type, is_active
    FROM `commercial-us-hiv-iiaf-dev`.`c3po_basic`.`hiv_metric_template_examples`
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


def step_retrieve_template(masked_question, token=None):
    sql = f"SELECT * FROM vector_search(index => '{VECTOR_INDEX_NAME}', query_text => '{escape_sql(masked_question)}', num_results => 1)"
    df = execute_sql(sql, token)
    if df.empty:
        raise ValueError(f"No template found for: {masked_question}")
    row = df.iloc[0].to_dict()
    row["_search_score"] = row.get("search_score", row.get("_score", row.get("score", 0)))
    return row


def step_llm_judge(llm_client, user_question, masked_question, template_row):
    return call_llm_json(llm_client, _build_judge_prompt(template_row), {
        "user_question": user_question,
        "masked_question": masked_question,
        "retrieved_template_id": template_row.get("template_id", ""),
        "retrieved_example_question": template_row.get("question", ""),
        "supported_parameters": json.loads(template_row.get("slot_output_contract_json", "{}")).get("required_keys", []),
    }, max_tokens=200)


# ===================================================================
# SUPERVISOR FALLBACK
# ===================================================================

_GENIE_SPACE_NAMES = {
    "01f122c468a11184877f9dc95297a618": "C3PO Genie - PrEP Chart Audit",
    "01f1245f5abb15aca0f1e0e3ea65f749": "C3PO Genie - HIV Treatment",
    "01f1527e79d112bc8c2b007906e91916": "C3PO Genie - PrEP Brand ATU Study",
    "01f129d06b4013d1a98a0ca3386bc670": "C3PO Genie - PrEP Analytics"
}

def _resolve_genie_space_name(genie_func_name):
    if not genie_func_name or not genie_func_name.startswith("genie-"):
        return genie_func_name
    space_id = genie_func_name.replace("genie-", "").replace("_", "-")
    return _GENIE_SPACE_NAMES.get(space_id, genie_func_name)


def _deduplicate_consecutive(calls, space_key="space"):
    if not calls:
        return []
    blocks, current = [], [calls[0]]
    for c in calls[1:]:
        if c.get(space_key) == current[-1].get(space_key):
            current.append(c)
        else:
            blocks.append(current)
            current = [c]
    blocks.append(current)
    return [block[-1] for block in blocks]


def _parse_supervisor_output(data):
    """Parse Responses API output[] using structured type/name/content fields."""
    r = {
        "final_answer": "", "genie_space": "", "genie_query": "",
        "table_data": [], "table_columns": [],
        "planning_text": "",
        "genie_calls": [],
    }
    output_list = data.get("output") if isinstance(data, dict) else []
    if not isinstance(output_list, list):
        r["final_answer"] = str(data.get("text", "") or data)[:500]
        return r

    genie_calls_raw = []
    pending_call = None

    for item in output_list:
        if not isinstance(item, dict):
            continue
        it = item.get("type", "")
        if it == "function_call":
            pending_call = {
                "space": item.get("name", ""),
                "query": "",
                "table_data": [],
                "table_columns": [],
            }
            try:
                args = json.loads(item.get("arguments", "{}")) if isinstance(item.get("arguments"), str) else (item.get("arguments") or {})
                pending_call["query"] = args.get("genie_query", args.get("query", ""))
            except (json.JSONDecodeError, TypeError):
                pass
            genie_calls_raw.append(pending_call)
            r["genie_space"] = pending_call["space"]
            r["genie_query"] = pending_call["query"]
        elif it == "function_call_output":
            raw_out = item.get("output", "")
            if pending_call and isinstance(raw_out, str) and "|" in raw_out:
                hdr, rows = None, []
                for ln in raw_out.split("\n"):
                    cells = [c.strip() for c in ln.split("|") if c.strip()]
                    if not cells or all(c.replace("-", "") == "" for c in cells):
                        continue
                    if hdr is None and len(cells) > 1:
                        hdr = cells
                    elif hdr:
                        if len(cells) == len(hdr) + 1:
                            cells = cells[1:]
                        if len(cells) == len(hdr):
                            rows.append(cells)
                if hdr:
                    pending_call["table_data"] = rows
                    pending_call["table_columns"] = hdr
                    r["table_columns"], r["table_data"] = hdr, rows
        elif it == "message":
            content = item.get("content", [])
            txt = ""
            if isinstance(content, list):
                txt = "\n".join(str(c.get("text") or c.get("output_text") or "").strip() for c in content if isinstance(c, dict) and (c.get("text") or c.get("output_text")))
            elif isinstance(content, str):
                txt = content.strip()
            if txt:
                if txt.startswith("<name>") and txt.endswith("</name>"):
                    continue
                if "|" in txt and txt.count("\n") >= 2:
                    lines = [l.strip() for l in txt.split("\n") if l.strip()]
                    has_sep = any(all(c.replace("-", "") == "" for c in l.split("|") if c.strip()) for l in lines)
                    if has_sep:
                        hdr, rows = None, []
                        non_table_lines = []
                        for ln in lines:
                            cells = [c.strip() for c in ln.split("|") if c.strip()]
                            is_sep = all(c.replace("-", "") == "" for c in cells) if cells else False
                            if is_sep:
                                continue
                            if hdr is None and len(cells) > 1:
                                hdr = cells
                            elif hdr:
                                if len(cells) == len(hdr) + 1:
                                    cells = cells[1:]
                                if len(cells) == len(hdr):
                                    rows.append(cells)
                                else:
                                    non_table_lines.append(ln)
                            else:
                                non_table_lines.append(ln)
                        if hdr:
                            r["table_columns"], r["table_data"] = hdr, rows
                        _STRUCTURAL = ("combined data table", "combined table", "data table")
                        non_table_lines = [
                            ln for ln in non_table_lines
                            if ln.lower().replace("*", "").replace(":", "").strip() not in _STRUCTURAL
                            and not ln.lower().replace("*", "").strip().startswith("type:")
                        ]
                        remaining_text = "\n".join(non_table_lines).strip()
                        if remaining_text:
                            r["final_answer"] = remaining_text
                        continue
                if not r["planning_text"]:
                    r["planning_text"] = txt
                r["final_answer"] = txt

    if genie_calls_raw:
        seen = {}
        for i, entry in enumerate(genie_calls_raw):
            seen[entry["space"]] = (i, entry)
        r["genie_calls"] = [entry for _, entry in sorted(seen.values(), key=lambda x: x[0])]

    if not r["final_answer"]:
        custom = data.get("custom_outputs") or {}
        if isinstance(custom, dict) and custom.get("final_response"):
            r["final_answer"] = str(custom["final_response"]).strip()
        elif data.get("text"):
            r["final_answer"] = str(data["text"]).strip()
    return r


def _build_supervisor_input(question, conversation_history=None):
    input_messages = []
    if conversation_history:
        for line in conversation_history.strip().split("\n"):
            line = line.strip()
            if line.startswith("User: "):
                input_messages.append({"role": "user", "content": line[6:]})
            elif line.startswith("Assistant: "):
                input_messages.append({"role": "assistant", "content": line[11:]})
    input_messages.append({"role": "user", "content": question})
    return input_messages


def call_supervisor_endpoint(question, token, conversation_history=None):
    input_messages = _build_supervisor_input(question, conversation_history)
    print(f"[SUPERVISOR] Sending {len(input_messages)} message(s) (including {len(input_messages)-1} history turn(s))")
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.post(
                SUPERVISOR_ENDPOINT_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"input": input_messages},
                timeout=240,
            )
            resp.raise_for_status()
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            print(f"[SUPERVISOR] Attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    else:
        raise last_err

    data = resp.json()
    print(f"[SUPERVISOR] Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
    print(f"[SUPERVISOR] Raw response: {str(data)[:300]}")

    parsed = _parse_supervisor_output(data)
    answer = parsed["final_answer"]
    genie_space_name = _resolve_genie_space_name(parsed["genie_space"])
    print(f"[SUPERVISOR] Table: {len(parsed['table_data'])} rows, {len(parsed['table_columns'])} cols")
    sup_meta = {
        "genie_space": parsed["genie_space"],
        "genie_space_name": genie_space_name,
        "genie_query": parsed["genie_query"],
        "table_data": parsed["table_data"],
        "table_columns": parsed["table_columns"],
        "genie_calls": parsed["genie_calls"],
    }

    if not answer:
        answer = str(data.get("text") or "")[:500]
    print(f"[SUPERVISOR] Extracted answer ({len(answer)} chars): {answer[:100]}")
    request_id = (
        resp.headers.get("X-Request-Id") or
        resp.headers.get("X-Databricks-Request-Id") or
        data.get("id", "")
    )
    return answer, request_id, sup_meta


# Experiment ID for the supervisor agent endpoint traces
_SUPERVISOR_EXPERIMENT_ID = "1572929100009210"


def extract_genie_calls_from_trace(request_id, call_timestamp_ms=None):
    """Extract per-Genie-space SQL from the MLflow trace matching this supervisor call.
    Returns list of {"space": "genie-<uuid>", "sql": "..."} dicts.
    Uses parent_id chain to correctly attribute each poll_query_results to its
    owning genie-* span — avoids wrong attribution when spans are interleaved.
    """
    try:
        from mlflow import MlflowClient
        import mlflow
        pat = _get_sql_token()
        if pat:
            os.environ["DATABRICKS_TOKEN"] = pat
            os.environ["DATABRICKS_HOST"] = f"https://{WORKSPACE_HOST}"
        mlflow.set_tracking_uri("databricks")
        client = MlflowClient()

        ts_lower = call_timestamp_ms if call_timestamp_ms else int(time.time() * 1000) - 5 * 60 * 1000
        traces_df = mlflow.search_traces(
            locations=[_SUPERVISOR_EXPERIMENT_ID],
            max_results=20,
            order_by=["timestamp_ms DESC"],
            filter_string=f"timestamp_ms >= {ts_lower}",
        )
        if traces_df is None or traces_df.empty:
            return []

        matched_trace = None
        for _, row in traces_df.iterrows():
            tid = row.get("trace_id")
            if not tid:
                continue
            t = client.get_trace(tid)
            if not t or not t.data or not t.data.spans:
                continue
            root_attrs = getattr(t.data.spans[0], "attributes", {}) or {}
            stored_rid = str(root_attrs.get("request_id", "")).strip('"').strip()
            if stored_rid == request_id:
                matched_trace = t
                break

        if matched_trace is None:
            return []

        spans = matched_trace.data.spans

        # Build span lookup for parent-chain traversal
        span_by_id = {s.span_id: s for s in spans if hasattr(s, "span_id")}

        def _genie_owner(span):
            """Walk up parent_id chain to find the genie-* ancestor of this span."""
            pid = getattr(span, "parent_id", None)
            while pid:
                parent = span_by_id.get(pid)
                if not parent:
                    break
                if parent.name.startswith("genie-"):
                    return parent.name
                pid = getattr(parent, "parent_id", None)
            return None

        seen = {}  # space -> (index, sql)
        idx = 0

        for span in spans:
            if span.name.startswith("genie-"):
                # Capture sql_query attribute if present on the genie span itself
                attrs = getattr(span, "attributes", {}) or {}
                sql = str(attrs.get("sql_query", "")).strip('"').strip()
                if sql and sql.strip():
                    seen[span.name] = (idx, sql)
                    idx += 1
            elif span.name == "poll_query_results":
                # Use parent chain to find the correct genie space — not current_genie heuristic
                owner = _genie_owner(span)
                if owner:
                    inputs = getattr(span, "inputs", None) or {}
                    sql = inputs.get("query_str", "")
                    if sql and isinstance(sql, str) and sql.strip():
                        seen[owner] = (idx, sql.strip())
                        idx += 1

        if seen:
            return [{"space": space, "sql": sql} for space, (_, sql) in sorted(seen.items(), key=lambda x: x[1][0])]
        return []
    except Exception as e:
        print(f"[TRACE] extract_genie_calls_from_trace failed: {e}")
        return []


def execute_supervisor_path_streaming(user_question, progress_fn=None, token=None, conversation_history=None):
    result = {"success": False, "steps_log": [], "source": "supervisor"}
    t_start = time.perf_counter()
    tok = token or _get_sql_token()

    try:
        if progress_fn:
            progress_fn([{"name": "Routing", "details": "Sending to HIV supervisor agent..."}])
        t = time.perf_counter()
        call_timestamp_ms = int(time.time() * 1000)
        answer, request_id, sup_meta = call_supervisor_endpoint(user_question, tok, conversation_history=conversation_history)
        step1 = {"name": "Supervisor", "duration": round(time.perf_counter() - t, 2),
                 "details": f"Answer received (id: {str(request_id)[:8]}...)"}
        result["steps_log"].append(step1)
        if progress_fn:
            progress_fn([step1])

        result["answer"] = answer
        result["request_id"] = request_id
        result["success"] = True

        if progress_fn:
            progress_fn([step1, {"name": "Extracting SQL", "details": "Reading MLflow trace..."}])
        t = time.perf_counter()
        trace_calls = extract_genie_calls_from_trace(request_id, call_timestamp_ms=call_timestamp_ms)
        genie_calls = sup_meta.get("genie_calls", [])

        if not genie_calls and trace_calls:
            # Supervisor didn't expose function_call items — reconstruct from trace
            genie_calls = [
                {"space": tc["space"], "sql": tc["sql"], "table_data": [], "table_columns": []}
                for tc in trace_calls
            ]
        else:
            # Merge SQL from trace into genie_calls by space name (not position)
            # Position-based merge fails when genie_calls order differs from trace_calls order
            trace_by_space = {tc["space"]: tc for tc in trace_calls}
            for call in genie_calls:
                tc = trace_by_space.get(call["space"])
                if tc and tc.get("sql"):
                    call["sql"] = tc["sql"]

        # Enrich each call with statement_response + resolved name
        for call in genie_calls:
            call["statement_response"] = (
                to_statement_response(call["table_data"], call["table_columns"])
                if call.get("table_data") and call.get("table_columns")
                else None
            )
            call["genie_space_name"] = _resolve_genie_space_name(call.get("space", ""))

        n_sqls = sum(1 for c in genie_calls if c.get("sql"))
        step2 = {"name": "SQL Trace", "duration": round(time.perf_counter() - t, 2),
                 "details": f"{n_sqls} SQL(s) across {len(genie_calls)} Genie call(s)" if genie_calls else "No SQL (general answer)"}
        result["steps_log"].append(step2)
        if progress_fn:
            progress_fn([step1, step2])

        result["answer_type"] = "table_only"
        result["metric_name"] = ""

        last_with_data = next((c for c in reversed(genie_calls) if c.get("table_data")), None)
        if last_with_data:
            result["result_data"] = last_with_data["table_data"]
            result["result_columns"] = last_with_data["table_columns"]
        elif sup_meta.get("table_data") and sup_meta.get("table_columns"):
            result["result_data"] = sup_meta["table_data"]
            result["result_columns"] = sup_meta["table_columns"]

        last_with_sql = next((c for c in reversed(genie_calls) if c.get("sql")), None)
        result["final_sql"] = last_with_sql["sql"] if last_with_sql else ""

        result["supervisor_meta"] = {
            "genie_calls": genie_calls,
            "genie_space": sup_meta.get("genie_space", ""),
            "genie_space_name": sup_meta.get("genie_space_name", ""),
            "genie_query": sup_meta.get("genie_query", ""),
        }

    except Exception as e:
        result["error_msg"] = f"Supervisor error: {str(e)}"

    result["total_time"] = round(time.perf_counter() - t_start, 2)
    return result


# ===================================================================
# STREAMING EXECUTORS
# ===================================================================

def execute_click_path_streaming(user_question, template_row, progress_fn=None, token=None, conversation_history=None):
    result = {"success": False, "steps_log": [], "source": "template"}
    print("[AUTH] click_path LLM calls: PAT(_get_sql_token)")
    print(f"[AUTH] click_path SQL: {'PAT(_get_sql_token)' if not token else 'explicit_token'}")
    llm_client = get_llm_client(_get_sql_token())
    t_start = time.perf_counter()
    try:
        user_question = step_resolve_context(llm_client, user_question, conversation_history or '')

        sql_template_val = template_row.get("sql_template") or ""
        if not sql_template_val.strip():
            raise ValueError(f"Template {template_row.get('template_id', '?')} has no sql_template defined")

        if progress_fn: progress_fn("Extracting parameters", "analyzing question...")
        t = time.perf_counter()
        slot_values = step_extract_slots(llm_client, user_question, template_row)
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
        result["error_msg"] = str(e)
    result["total_time"] = round(time.perf_counter() - t_start, 2)
    return result


def execute_semantic_path_streaming(user_question, progress_fn=None, token=None, conversation_history=None):
    """Semantic path: mask -> retrieve -> judge -> slots -> SQL.
    On judge failure or low score: routes to supervisor."""
    result = {"success": False, "steps_log": [], "source": "template"}
    print("[AUTH] semantic_path LLM calls: PAT(_get_sql_token)")
    print(f"[AUTH] semantic_path SQL/vector: {'PAT(_get_sql_token)' if not token else 'explicit_token'}")
    llm_client = get_llm_client(_get_sql_token())
    t_start = time.perf_counter()
    try:
        user_question = step_resolve_context(llm_client, user_question, conversation_history or '')

        if progress_fn: progress_fn([])
        t = time.perf_counter()
        masked = step_mask_question(llm_client, user_question)
        step1 = {"name": "Understand", "duration": round(time.perf_counter()-t,2), "details": f"Masked: {masked}"}
        result["steps_log"].append(step1)
        if progress_fn: progress_fn([step1])

        t = time.perf_counter()
        template_row = step_retrieve_template(masked, token=token)
        score = float(template_row.get("_search_score", 0))
        step2 = {"name": "Retrieve", "duration": round(time.perf_counter()-t,2), "details": f"{template_row.get('template_id','?')} (score: {score:.3f})"}
        result["steps_log"].append(step2)
        if progress_fn: progress_fn([step1, step2])

        t = time.perf_counter()
        judge = step_llm_judge(llm_client, user_question, masked, template_row)
        verdict = judge.get("verdict", "no").lower().strip()
        reason = judge.get("reason", "")
        v_label = "PASS" if verdict == "yes" else "Answering from first principles"
        step3 = {"name": "Judge", "duration": round(time.perf_counter()-t,2), "details": f"{v_label}: {reason}"}
        result["steps_log"].append(step3)
        if progress_fn: progress_fn([step1, step2, step3])

        if score < SCORE_THRESHOLD or verdict != "yes":
            step3["details"] += " → routing to supervisor"
            if progress_fn: progress_fn([step1, step2, step3])
            sup = execute_supervisor_path_streaming(
                user_question,
                progress_fn=(lambda steps: progress_fn([step1, step2, step3] + steps)) if progress_fn else None,
                token=token,
                conversation_history=conversation_history,
            )
            sup["steps_log"] = result["steps_log"] + sup["steps_log"]
            return sup

        t = time.perf_counter()
        slot_values = step_extract_slots(llm_client, user_question, template_row)
        result["slot_values"] = slot_values
        slot_summary = " | ".join(f"{k}: {v}" for k, v in slot_values.items())
        step4 = {"name": "Params", "duration": round(time.perf_counter()-t,2), "details": slot_summary}
        result["steps_log"].append(step4)
        if progress_fn: progress_fn([step1, step2, step3, step4])

        t = time.perf_counter()
        final_sql = step_render_sql(template_row.get("sql_template", ""), slot_values)
        result["final_sql"] = final_sql
        df, type_names = execute_sql_with_types(final_sql, token)
        data, columns = _make_serializable(df)
        result.update({"result_data": data, "result_columns": columns, "type_names": type_names})
        step5 = {"name": "Execute", "duration": round(time.perf_counter()-t,2), "details": f"{len(df)} rows"}
        result["steps_log"].append(step5)
        if progress_fn: progress_fn([step1, step2, step3, step4, step5])

        result["answer_type"] = template_row.get("answer_type", "table_only")
        result["metric_name"] = template_row.get("metric_name", "Metric")
        result["success"] = True
    except Exception as e:
        result["error_msg"] = str(e)
    result["total_time"] = round(time.perf_counter() - t_start, 2)
    return result


# ===================================================================
# CHAT HISTORY
# ===================================================================

_CHAT_HISTORY_TABLE = "`commercial-us-hiv-iiaf-dev`.`c3po_basic`.`hiv_c3po_v3_chat_history`"


def _execute_sql_with_params(sql, params, token=None):
    """Execute parameterised SQL (? placeholders), return DataFrame."""
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token or _get_sql_token(),
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns)


def write_chat_message(thread_id, message_id, user_email, role,
                       question=None, answer=None, path=None, payload=None, token=None):
    """Insert one chat message row using parameterised query."""
    print(f"[AUTH] write_chat_message: PAT(_get_sql_token)")
    try:
        sql = (
            f"INSERT INTO {_CHAT_HISTORY_TABLE} "
            "(thread_id, message_id, user_email, role, question, answer, path, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp())"
        )
        params = [thread_id, message_id, user_email, role, question, answer, path, payload]
        with databricks_sql.connect(
            server_hostname=WORKSPACE_HOST,
            http_path=SQL_WAREHOUSE_HTTP_PATH,
            access_token=token or _get_sql_token(),
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
        print(f"[HISTORY] wrote {role} row thread={thread_id}")
    except Exception as e:
        print(f"[HISTORY] write_chat_message failed: {e}")


def get_threads_for_user(user_email, token=None):
    """Return up to 50 threads for a user, newest first."""
    sql = (
        f"SELECT thread_id, MIN(created_at) AS thread_created_at, "
        f"MIN_BY(question, created_at) AS title "
        f"FROM {_CHAT_HISTORY_TABLE} "
        f"WHERE user_email = ? AND role = 'user' "
        f"AND created_at >= current_timestamp() - INTERVAL 7 DAYS "
        f"GROUP BY thread_id "
        f"ORDER BY MIN(created_at) DESC "
        f"LIMIT 50"
    )
    try:
        df = _execute_sql_with_params(sql, [user_email], token)
        records = df.to_dict(orient="records")
        for r in records:
            ts = r.pop("thread_created_at", None)
            r["created_at"] = str(ts) if ts is not None else None
        return records
    except Exception as e:
        import traceback
        underlying = getattr(e, 'error', None)
        detail = f" | caused by: {type(underlying).__name__}: {underlying}" if underlying else ""
        print(f"[HISTORY] get_threads_for_user failed: {e}{detail}")
        traceback.print_exc()
        return []


def get_messages_for_thread(thread_id, token=None):
    """Return all messages for a thread in chronological order."""
    sql = (
        f"SELECT thread_id, message_id, user_email, role, question, answer, "
        f"path, payload, created_at, "
        f"feedback_rating, feedback_request, feedback_comment "
        f"FROM {_CHAT_HISTORY_TABLE} "
        f"WHERE thread_id = ? "
        f"ORDER BY created_at ASC"
    )
    try:
        df = _execute_sql_with_params(sql, [thread_id], token)
        records = df.to_dict(orient="records")
        for r in records:
            if r.get("created_at") is not None:
                r["created_at"] = str(r["created_at"])
        return records
    except Exception as e:
        import traceback
        underlying = getattr(e, 'error', None)
        detail = f" | caused by: {type(underlying).__name__}: {underlying}" if underlying else ""
        print(f"[HISTORY] get_messages_for_thread failed: {e}{detail}")
        traceback.print_exc()
        return []
