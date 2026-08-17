# ===================================================================
# SUPERVISOR AGENT
# ===================================================================

import json, re, time, math, os, requests, traceback
from pathlib import Path
import pandas as pd
import openai
from databricks import sql as databricks_sql
from config import (
    WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
    VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD,
    SUPERVISOR_ENDPOINT_URL,
)
from utils.sql_execution import _execute_sql_with_params, _get_databricks_pat,execute_sql ,_get_type_name , execute_sql_with_types
from utils.llm_client import *
from utils.format_data_to_statement_response import *

## Initializing configuration variables

_GENIE_SPACES_PATH = Path(__file__).resolve().parent / "system_files" / "genie_spaces.json"

try:
    with _GENIE_SPACES_PATH.open("r", encoding="utf-8") as f:
        _GENIE_SPACE_NAMES = json.load(f)
    if not isinstance(_GENIE_SPACE_NAMES, dict):
        raise ValueError("genie_spaces.json must contain a JSON object mapping space IDs to names")

except FileNotFoundError:
    _GENIE_SPACE_NAMES = {}
    print(f"[CONFIG] Genie spaces file not found: {_GENIE_SPACES_PATH}")

except json.JSONDecodeError as e:
    raise ValueError(f"Invalid JSON in {_GENIE_SPACES_PATH}: {e}") from e

_SUPERVISOR_EXPERIMENT_ID = os.environ.get("SUPERVISOR_EXPERIMENT_ID")



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
    print(f"\n[SUPERVISOR_CALL] {'='*50}")
    print(f"[SUPERVISOR_CALL] Calling supervisor endpoint")
    print(f"[SUPERVISOR_CALL]   question   = {question[:100]}")
    print(f"[SUPERVISOR_CALL]   endpoint   = {SUPERVISOR_ENDPOINT_URL[:80]}")
    print(f"[SUPERVISOR_CALL]   has_token  = {bool(token)}")
    input_messages = _build_supervisor_input(question, conversation_history)
    print(f"[SUPERVISOR_CALL]   messages   = {len(input_messages)} message(s)")
    print(f"[SUPERVISOR] Sending {len(input_messages)} message(s) (including {len(input_messages)-1} history turn(s))")
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.post(
                SUPERVISOR_ENDPOINT_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"input": input_messages},
                timeout=60,  # 3 attempts x 60s = 180s (aligned with client 180s timeout)
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
    print(f"[SUPERVISOR_CALL]   http_status = {resp.status_code}")
    print(f"[SUPERVISOR_CALL]   resp_keys   = {list(data.keys()) if isinstance(data, dict) else type(data)}")
    print(f"[SUPERVISOR_CALL]   raw_resp    = {str(data)[:400]}")

    parsed = _parse_supervisor_output(data)
    answer = parsed["final_answer"]
    genie_space_name = _resolve_genie_space_name(parsed["genie_space"])
    print(f"[SUPERVISOR_CALL] Parsed output:")
    print(f"[SUPERVISOR_CALL]   answer_len    = {len(answer)} chars")
    print(f"[SUPERVISOR_CALL]   answer_preview= {answer[:150]}")
    print(f"[SUPERVISOR_CALL]   genie_space   = {parsed['genie_space']}")
    print(f"[SUPERVISOR_CALL]   space_name    = {genie_space_name}")
    print(f"[SUPERVISOR_CALL]   table_data    = {len(parsed['table_data'])} rows x {len(parsed['table_columns'])} cols")
    print(f"[SUPERVISOR_CALL]   genie_calls   = {len(parsed['genie_calls'])} call(s)")
    print(f"[SUPERVISOR_CALL]   planning_text = {parsed['planning_text'][:100] if parsed['planning_text'] else 'None'}")
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
# _SUPERVISOR_EXPERIMENT_ID = "1572929100009210"


def extract_genie_calls_from_trace(request_id, call_timestamp_ms=None):
    """Extract per-Genie-space SQL from the MLflow trace matching this supervisor call.
    Returns list of {"space": "genie-<uuid>", "sql": "..."} dicts.
    Uses parent_id chain to correctly attribute each poll_query_results to its
    owning genie-* span — avoids wrong attribution when spans are interleaved.
    """
    print(f"[TRACE] Searching for trace | request_id={str(request_id)[:20]} | ts_ms={call_timestamp_ms}")
    try:
        from mlflow import MlflowClient
        import mlflow
        pat = _get_databricks_pat()
        if pat:
            os.environ["DATABRICKS_TOKEN"] = pat
            os.environ["DATABRICKS_HOST"] = f"https://{WORKSPACE_HOST}"
        mlflow.set_tracking_uri("databricks")
        client = MlflowClient()

        ts_lower = call_timestamp_ms if call_timestamp_ms else int(time.time() * 1000) - 5 * 60 * 1000
        # Brief wait for MLflow to finish writing the trace (race condition mitigation)
        time.sleep(1.5)
        traces_df = mlflow.search_traces(
            locations=[_SUPERVISOR_EXPERIMENT_ID],
            max_results=20,
            order_by=["timestamp_ms DESC"],
            filter_string=f"timestamp_ms >= {ts_lower}",
        )
        if traces_df is None or traces_df.empty:
            # Retry once after additional wait — trace might still be writing
            print("[TRACE] No traces found, retrying after 2s...")
            time.sleep(2)
            traces_df = mlflow.search_traces(
                locations=[_SUPERVISOR_EXPERIMENT_ID],
                max_results=20,
                order_by=["timestamp_ms DESC"],
                filter_string=f"timestamp_ms >= {ts_lower}",
            )
            if traces_df is None or traces_df.empty:
                print("[TRACE] Still no traces after retry")
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
            print(f"[TRACE] No matching trace found for request_id={str(request_id)[:20]}")
            return []

        print(f"[TRACE] Matched trace: {matched_trace.info.trace_id} | {len(matched_trace.data.spans)} spans")
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
        print(f"[TRACE] !! extract_genie_calls_from_trace FAILED: {type(e).__name__}: {e}")
        print(f"[TRACE] Traceback:\n{traceback.format_exc()}")
        return []


def execute_supervisor_path_streaming(user_question, progress_fn=None, token=None, conversation_history=None, session=None):
    result = {"success": False, "steps_log": [], "source": "supervisor"}
    t_start = time.perf_counter()
    tok = token or _get_databricks_pat()
    print(f"\n[SUP_PATH] {'='*50}")
    print(f"[SUP_PATH] START execute_supervisor_path_streaming")
    print(f"[SUP_PATH]   question = {user_question[:100]}")
    print(f"[SUP_PATH]   has_tok  = {bool(tok)}")

    try:
        if progress_fn:
            progress_fn([{"name": "Routing", "details": "Sending to ONC supervisor agent..."}])
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
        result["source"] = "supervisor"

        # ── Write partial result immediately: answer + table visible, SQL pending ──
        if session is not None:
            # Resolve genie_space_name for badge display (before trace fetch)
            partial_genie_calls = []
            for call in sup_meta.get("genie_calls", []):
                partial_genie_calls.append({
                    **call,
                    "genie_space_name": _resolve_genie_space_name(call.get("space", "")),
                })
            partial = {
                "success": True,
                "insight": answer,
                "sql": "",
                "source": "supervisor",
                "answer_type": "table_only",
                "metric_name": "",
                "supervisor_meta": {
                    "genie_calls": partial_genie_calls,
                    "genie_space": sup_meta.get("genie_space", ""),
                    "genie_space_name": sup_meta.get("genie_space_name", ""),
                    "genie_query": sup_meta.get("genie_query", ""),
                },
            }
            # Include table data if supervisor returned it
            if sup_meta.get("table_data") and sup_meta.get("table_columns"):
                partial["statement_response"] = to_statement_response(
                    sup_meta["table_data"], sup_meta["table_columns"]
                )
                partial["row_count"] = len(sup_meta["table_data"])
            # Show table immediately with empty insight — will stream answer progressively
            partial["insight"] = ""
            session["partial_result"] = partial
            session["streaming_insight"] = ""
            print(f"[SUP_PATH] ★ Partial result written (table visible, answer will stream)")

            # Stream answer by character slices — preserves ALL formatting (bold, newlines, bullets)
            def _stream_answer(text, sess):
                stride = 4  # chars per tick (~200 chars/sec → ~15 words per 400ms poll)
                for i in range(0, len(text), stride):
                    sess["streaming_insight"] = text[:i + stride]
                    time.sleep(0.02)
                sess["streaming_insight"] = text.strip()

            import threading
            session["_stream_thread"] = threading.Thread(target=_stream_answer, args=(answer, session), daemon=True)
            session["_stream_thread"].start()

        if progress_fn:
            progress_fn([step1, {"name": "Extracting SQL", "details": "Reading MLflow trace..."}])
        t = time.perf_counter()
        print(f"[SUP_PATH] Extracting SQL from MLflow trace | request_id={str(request_id)[:20]}")
        # Timeout trace fetch at 3s — answer is already ready, SQL is decorative
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        trace_calls = []
        try:
            with ThreadPoolExecutor(max_workers=1) as _trace_executor:
                _future = _trace_executor.submit(extract_genie_calls_from_trace, request_id, call_timestamp_ms=call_timestamp_ms)
                trace_calls = _future.result(timeout=10)  # Safe: partial_result already showing answer to user
        except FuturesTimeout:
            print(f"[SUP_PATH]   Trace fetch timed out after 10s — skipping (answer already visible to user)")
        except Exception as trace_err:
            print(f"[SUP_PATH]   Trace fetch failed: {trace_err} — skipping")
        print(f"[SUP_PATH]   trace_calls = {len(trace_calls)} SQL(s) found")
        for i, tc in enumerate(trace_calls):
            print(f"[SUP_PATH]   trace[{i}]: space={tc.get('space','?')[:30]} | sql={tc.get('sql','')[:80]}")
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

        print(f"[SUP_PATH] Final genie_calls after merge: {len(genie_calls)}")
        for i, gc in enumerate(genie_calls):
            print(f"[SUP_PATH]   call[{i}]: space={gc.get('space','?')[:30]} | has_sql={bool(gc.get('sql'))} | has_data={bool(gc.get('table_data'))}")

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
        print(f"[SUP_PATH] !! EXCEPTION: {type(e).__name__}: {e}")
        print(f"[SUP_PATH] Traceback:\n{traceback.format_exc()}")
        result["error_msg"] = f"Supervisor error: {str(e)}"

    # Wait for answer streaming to finish before returning (prevents "flash" on done=True)
    if session is not None and session.get("_stream_thread"):
        try:
            session["_stream_thread"].join(timeout=15)  # Max 15s wait (safety net)
            print(f"[SUP_PATH] Answer streaming complete")
        except Exception:
            pass

    result["total_time"] = round(time.perf_counter() - t_start, 2)
    print(f"[SUP_PATH] END | success={result.get('success')} | time={result['total_time']}s")
    print(f"[SUP_PATH]   has_answer  = {bool(result.get('answer'))}")
    print(f"[SUP_PATH]   has_data    = {bool(result.get('result_data'))}")
    print(f"[SUP_PATH]   final_sql   = {result.get('final_sql', '')[:80]}")
    print(f"[SUP_PATH]   error       = {result.get('error_msg', 'None')[:100]}")
    print(f"[SUP_PATH] {'='*50}")
    return result
