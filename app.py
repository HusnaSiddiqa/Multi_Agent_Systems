"""
HIV C3PO v3 - Flask API Backend Modularised
Template matching first → supervisor agent fallback on no match.
"""

import os, uuid, time, threading, traceback
from flask import Flask, request, jsonify, send_from_directory, send_file

from config import SCORE_THRESHOLD, NARRATIVE_DECK_VOLUME_PATH 
SUGGESTION_QUESTIONS_TABLE = os.environ.get("SUGGESTION_QUESTIONS_TABLE")
from suggestions import load_template_examples, search_questions

from conversation import write_chat_message , get_threads_for_user , get_messages_for_thread
from sql_template_agent import *
from supervisor_agent import *
from feedback_module import save_feedback
from utils.llm_client import *
from utils.format_data_to_statement_response import *
from utils.sql_execution import _execute_sql_with_params, _get_databricks_pat,execute_sql ,_get_type_name , execute_sql_with_types
from ppt_agent.agent import generate_ppt


# ── App init ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="")

try:
    examples_df = load_template_examples()
    print(f"[STARTUP] Templates loaded: {len(examples_df)} rows")
except Exception as e:
    print(f"[STARTUP WARNING] Template load failed: {e} — will retry on first request")
    examples_df = None

from concurrent.futures import ThreadPoolExecutor
_question_executor = ThreadPoolExecutor(max_workers=10)

_sessions = {}   # session_id -> {steps, done, result, question, path}


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/suggestions", methods=["GET"])
def api_suggestions():
    """Typeahead suggestions as user types."""
    try:
        global examples_df
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify([])
        if examples_df is None:
            try:
                examples_df = load_template_examples()
            except Exception:
                return jsonify([])
        results = search_questions(q, examples_df)
        return jsonify([{"label": label, "data": data} for label, data in results])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Submit a question. Returns session_id for progress polling."""
    try:
        body = request.get_json(force=True, silent=True)
        if not body or not body.get("question"):
            return jsonify({"error": "Missing question"}), 400

        question = body["question"].strip()
        # Security: never trust client-supplied sql_template.
        # Accept only example_id, re-fetch the full template row from DB.
        client_template = body.get("template_row")
        template_row = None
        if client_template and client_template.get("example_id"):
            from sql_template_agent import fetch_template_by_example_id
            template_row = fetch_template_by_example_id(client_template["example_id"])
            if not template_row:
                print(f"[SECURITY] Client sent example_id={client_template.get('example_id')} but DB lookup failed — falling to semantic path")
        conversation_history = body.get("context", "")
        session_id = str(uuid.uuid4())
        initial_path = "click" if template_row else "semantic"
        # Prefer X-Forwarded-Email (real email); fall back to X-Forwarded-User (numeric id@workspace)
        user_email = (request.headers.get("X-Forwarded-Email") or
                      request.headers.get("X-Forwarded-User", "unknown"))
        print(f"[AUTH] user_email resolved: {user_email}")
        thread_id = body.get("thread_id") or str(uuid.uuid4())

        _sessions[session_id] = {
            "steps": [], "done": False, "result": None,
            "question": question,
            "path": initial_path,
            "start_time": time.time(),
            "user_email": user_email,
            "thread_id": thread_id,
        }

        # Persist the user row immediately so the sidebar shows the thread before the answer finishes.
        if thread_id and user_email:
            write_chat_message(
                thread_id=thread_id,
                message_id=str(uuid.uuid4()),
                user_email=user_email,
                role="user",
                question=question,
                path=initial_path,
            )

        _question_executor.submit(_process_question, session_id, question, template_row, None, conversation_history, user_email, thread_id)

        return jsonify({"session_id": session_id, "path": initial_path, "thread_id": thread_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/progress/<session_id>", methods=["GET"])
def api_progress(session_id):
    """Poll for processing progress."""
    session = _sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    response = {
        "steps": session["steps"],
        "done": session["done"],
        "path": session["path"],
        "question": session["question"],
    }
    # Surface partial result (table ready, insight pending/streaming) before done
    if not session["done"] and session.get("partial_result"):
        response["result"] = dict(session["partial_result"])  # shallow copy to avoid mutation
        # Overlay streaming insight tokens as they arrive
        streaming = session.get("streaming_insight", "")
        if streaming:
            response["result"]["insight"] = streaming
    if session["done"]:
        response["result"] = session["result"]
        # Surface resolved_question for UI display (always show for debugging)
        result = session.get("result") or {}
        if result.get("resolved_question") and result["resolved_question"] != session["question"]:
            response["resolved_question"] = result["resolved_question"]
        # Also include genie_query if available (what was sent to Genie)
        sup_meta = result.get("supervisor_meta") or {}
        if sup_meta.get("genie_query"):
            response["genie_query"] = sup_meta["genie_query"]
        session["_expire"] = time.time() + 300
    return jsonify(response)




@app.route("/api/download-narrative-deck", methods=["GET"])
def api_download_narrative_deck():
    """Download the latest narrative deck .pptx from UC Volume."""
    import requests as _req
    try:
        token = os.environ.get("DATABRICKS_PAT", "")
        host = os.environ.get("DATABRICKS_HOSTNAME", "")
        volume_path = NARRATIVE_DECK_VOLUME_PATH

        # List files in volume to find the .pptx
        list_url = f"https://{host}/api/2.0/fs/directories/{volume_path}"
        list_resp = _req.get(list_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        list_resp.raise_for_status()
        entries = list_resp.json().get("contents", [])
        pptx_files = [e for e in entries if e.get("path", "").endswith(".pptx")]
        if not pptx_files:
            return jsonify({"error": "No .pptx file found in narrative deck volume"}), 404

        # Get the most recent .pptx (by last_modified or just take first if single file)
        target = sorted(pptx_files, key=lambda e: e.get("last_modified", 0), reverse=True)[0]
        file_path = target["path"]
        file_name = file_path.split("/")[-1]

        # Download the file content
        download_url = f"https://{host}/api/2.0/fs/files/{file_path}"
        file_resp = _req.get(download_url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        file_resp.raise_for_status()

        # Return as downloadable attachment
        import io
        buffer = io.BytesIO(file_resp.content)
        buffer.seek(0)
        return send_file(
            buffer,
            as_attachment=True,
            download_name=file_name,
            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    except _req.exceptions.HTTPError as e:
        print(f"[NARRATIVE_DECK] HTTP error: {e.response.status_code} {e.response.text[:200]}")
        return jsonify({"error": f"Failed to fetch narrative deck: {e.response.status_code}"}), 500
    except Exception as e:
        print(f"[NARRATIVE_DECK] Error: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/refresh-templates", methods=["POST"])
def api_refresh_templates():
    """Force reload templates from DB (clears 5-min cache)."""
    global examples_df
    try:
        from suggestions import load_template_examples, _examples_cache
        import suggestions
        # Clear the cache
        suggestions._examples_cache = None
        suggestions._cache_time = 0
        # Reload
        examples_df = load_template_examples()
        count = len(examples_df) if examples_df is not None else 0
        active_count = int(examples_df["is_active"].sum()) if examples_df is not None and "is_active" in examples_df.columns else count
        print(f"[ADMIN] Templates refreshed: {count} total, {active_count} active")
        return jsonify({"success": True, "total_templates": count, "active_templates": active_count})
    except Exception as e:
        print(f"[ADMIN] Refresh failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok", "templates_loaded": len(examples_df) if examples_df is not None else 0})


@app.route("/api/debug", methods=["GET"])
def api_debug():
    if os.environ.get("ENABLE_DEBUG", "false").lower() != "true":
        return jsonify({"error": "Debug endpoint disabled"}), 403
    import traceback
    results = {}
    # from executor import _get_databricks_pat
    token = _get_databricks_pat()
    results["token_set"] = bool(token)
    results["token_source"] = "PAT (DATABRICKS_PAT)"
    try:
        # from executor import execute_sql
        df = execute_sql(
            f"SELECT count(*) as cnt FROM {SUGGESTION_QUESTIONS_TABLE} WHERE is_active = true"
        )
        results["sql_ok"] = True
        results["template_count"] = int(df.iloc[0]["cnt"])
    except Exception as e:
        results["sql_ok"] = False
        results["sql_error"] = str(e)
    try:
        # from executor import get_llm_client
        client = get_llm_client()
        resp = client.chat.completions.create(
            messages=[{"role": "user", "content": "Say hi in 3 words"}],
            model="databricks-claude-sonnet-4-5", max_tokens=20,
        )
        results["llm_ok"] = True
        results["llm_response"] = resp.choices[0].message.content[:50]
    except Exception as e:
        results["llm_ok"] = False
        results["llm_error"] = str(e)
        results["llm_traceback"] = traceback.format_exc()[-500:]
    return jsonify(results)


# ── Background processing ─────────────────────────────────────────────────────

def _process_question(session_id, question, template_row, token, conversation_history='', user_email='unknown', thread_id=None):
    """Runs in background thread. On template failure → supervisor fallback."""
    session = _sessions[session_id]
    try:
        if template_row:
            # CLICK PATH
            print(f"[PROCESS] -> Entering CLICK PATH (suggestion click)")
            def on_step(name, detail):
                session["steps"].append({"name": name, "details": detail, "time": time.time()})
            result = execute_click_path_streaming(question, template_row, on_step, token=token, conversation_history=conversation_history)
            print(f"[PROCESS] <- Click path done: success={result.get('success')}, rows={len(result.get('result_data') or [])}, time={result.get('total_time')}s")
            if not result.get('success'):
                print(f"[PROCESS] !! Click path ERROR: {result.get('error_msg', 'unknown')}")
        else:
            # STEP 1: Exact match check — cheap SQL lookup before any LLM/vector work
            session["steps"].append({"name": "Exact Match", "details": "Checking template table...", "time": time.time()})
            matched_row = step_exact_match(question)
            print(f"[PROCESS]   exact_match_result = {bool(matched_row)} | template_id={matched_row.get('template_id') if matched_row else 'N/A'}")
            if matched_row:
                # Exact match found — but only use click path if SQL is defined
                sql_val = matched_row.get("sql_template") or ""
                if sql_val.strip():
                    session["path"] = "click"
                    session["steps"][-1]["details"] = f"Matched: {matched_row.get('template_id', '?')}"
                    def on_step(name, detail):
                        session["steps"].append({"name": name, "details": detail, "time": time.time()})
                    print(f"[PROCESS] -> Entering CLICK PATH via exact match (template_id={matched_row.get('template_id')})")
                    result = execute_click_path_streaming(question, matched_row, on_step, token=token, conversation_history=conversation_history)
                    print(f"[PROCESS] <- Click path done: success={result.get('success')}, rows={len(result.get('result_data') or [])}, time={result.get('total_time')}s")
                    if not result.get('success'):
                        print(f"[PROCESS] !! Click path ERROR: {result.get('error_msg', 'unknown')}")
                else:
                    # Template matched but has no SQL — fall through to semantic/supervisor
                    session["steps"][-1]["details"] = f"Matched template {matched_row.get('template_id', '?')} but no SQL — routing to supervisor"
                    matched_row = None  # force fall-through
            if not matched_row:
                print(f"[PROCESS] -> Entering SEMANTIC PATH (mask -> retrieve -> judge -> slots -> SQL)")
                # No exact match (or matched but no SQL) — try semantic path
                session["steps"][-1]["details"] = "No exact match — trying semantic search"
                def on_steps(steps_done):
                    session["steps"] = session["steps"][:1] + steps_done
                result = execute_semantic_path_streaming(question, on_steps, token=token, conversation_history=conversation_history, session=session)

        # ── GENIE FALLBACK SHIELD ─────────────────────────────────────────
        # If template/semantic path failed (LLM format error, SQL error,
        # slot extraction failure, etc.), route to supervisor instead of
        # showing raw error to the user.
        if not result.get("success") and result.get("source") != "supervisor":
            original_error = result.get("error_msg", "Unknown error")
            print(f"[FALLBACK] Template/semantic path failed: {original_error} — trying supervisor")
            session["steps"].append({"name": "Fallback", "details": "Routing to Genie supervisor...", "time": time.time()})
            try:
                fallback_result = execute_supervisor_path_streaming(
                    question, progress_fn=None, token=token,
                    conversation_history=conversation_history,
                    session=session,
                )
                if fallback_result.get("success"):
                    print(f"[FALLBACK] SUCCEEDED - supervisor answered | rows={len(fallback_result.get('result_data') or [])}")
                    result = fallback_result
                    session["path"] = "supervisor"
                else:
                    # Supervisor also failed
                    print(f"[FALLBACK] FAILED - supervisor also returned error: {fallback_result.get('error_msg', 'unknown')}")
                    result["error_msg"] = "I wasn't able to answer this question. Please try rephrasing or ask a different question."
            except Exception as fallback_err:
                print(f"[FALLBACK] EXCEPTION during supervisor fallback: {type(fallback_err).__name__}: {fallback_err}")
                print(f"[FALLBACK] Traceback:\n{traceback.format_exc()}")
                result["error_msg"] = "I wasn't able to answer this question. Please try rephrasing or ask a different question."

        # Insight generation (template path only — supervisor already has answer text)
        # Skip if insight already present (e.g., supervisor provides its own answer)
        if result.get("success") and result.get("result_data") and result.get("source") == "template" and not result.get("insight"):
            # Build statement_response early so table can render while insight generates
            if result.get("result_data"):
                early_sr = to_statement_response(
                    result["result_data"], result["result_columns"], result.get("type_names"))
                result["statement_response"] = early_sr
                session["partial_result"] = {
                    "success": True,
                    "statement_response": early_sr,
                    "insight": "",
                    "sql": result.get("final_sql", ""),
                    "slot_values": result.get("slot_values", {}),
                    "metric_name": result.get("metric_name", ""),
                    "answer_type": result.get("answer_type", "table_only"),
                    "row_count": len(result.get("result_data") or []),
                    "source": "template",
                }
            session["steps"].append({"name": "Insight", "details": "Generating summary...", "time": time.time()})
            session["streaming_insight"] = ""  # Initialize for progress endpoint
            result["insight"] = generate_insight_streaming(
                question,
                result.get("slot_values", {}),
                result.get("result_data", []),
                result.get("result_columns", []),
                session=session,
                token=token,
            )
        elif result.get("source") == "supervisor":
            # Supervisor answer text IS the insight
            result["insight"] = result.get("answer", "")

        # Sync path from actual source (handles semantic→supervisor routing)
        if result.get("source") == "supervisor":
            session["path"] = "supervisor"

        # Shape data for chart rendering (skip if already computed in partial_result block)
        if result.get("success") and result.get("result_data") and not result.get("statement_response"):
            result["statement_response"] = to_statement_response(
                result["result_data"],
                result["result_columns"],
                result.get("type_names"),
            )

        import json as _json

        # Generate assistant_message_id before write so it can be surfaced to the frontend
        # via final_result — the frontend uses it to call /api/feedback for the correct DB row.
        assistant_message_id = str(uuid.uuid4())

        final_result = {
            "success":            result.get("success", False),
            "statement_response": result.get("statement_response"),
            "insight":            result.get("insight", ""),
            "sql":                result.get("final_sql", ""),
            "slot_values":        result.get("slot_values", {}),
            "metric_name":        result.get("metric_name", ""),
            "answer_type":        result.get("answer_type", "table_only"),
            "total_time":         result.get("total_time", 0),
            "steps_log":          result.get("steps_log", []),
            "error_msg":          result.get("error_msg", ""),
            "row_count":          len(result.get("result_data") or []),
            "source":             result.get("source", "template"),
            "supervisor_meta":    result.get("supervisor_meta"),
            "message_id":         assistant_message_id,   # surfaced so frontend can call /api/feedback
            # ── Debug fields for client issue tracking ──
            "resolved_question":    result.get("resolved_question", ""),
            "original_question":    result.get("original_question", question),
            "conversation_context": result.get("conversation_context", ""),
        }
        session["result"] = final_result

        print(f"[PROCESS] {'~'*60}")
        print(f"[PROCESS] FINAL RESULT session={session_id[:8]}")
        print(f"[PROCESS]   success        = {final_result['success']}")
        print(f"[PROCESS]   source         = {final_result['source']}")
        print(f"[PROCESS]   row_count      = {final_result['row_count']}")
        print(f"[PROCESS]   total_time     = {final_result['total_time']}s")
        print(f"[PROCESS]   metric_name    = {final_result['metric_name']}")
        print(f"[PROCESS]   answer_type    = {final_result['answer_type']}")
        print(f"[PROCESS]   has_insight    = {bool(final_result['insight'])}")
        print(f"[PROCESS]   has_sql        = {bool(final_result['sql'])}")
        print(f"[PROCESS]   error_msg      = {final_result['error_msg'][:200] if final_result['error_msg'] else 'None'}")
        print(f"[PROCESS] {'~'*60}")

        # History write moved to finally block for reliability
    except Exception as e:
        print(f"[ERROR] {'!'*60}")
        print(f"[ERROR] UNHANDLED EXCEPTION in _process_question")
        print(f"[ERROR]   type       = {type(e).__name__}")
        print(f"[ERROR]   message    = {e}")
        print(f"[ERROR]   question   = {question[:120]}")
        print(f"[ERROR]   session    = {session_id[:8]}")
        print(f"[ERROR] Full traceback:")
        print(traceback.format_exc())
        print(f"[ERROR] {'!'*60}")
        session["result"] = {"success": False, "error_msg": "I wasn't able to answer this question. Please try rephrasing or ask a different question."}
    finally:
        # Always write assistant history row (even on error) for debugging
        try:
            import json as _json_f
            final = session.get("result") or {}
            # Include session steps in payload for debugging
            final["steps_log"] = session.get("steps", [])
            assistant_msg_id = final.get("message_id") or str(uuid.uuid4())
            if thread_id and user_email:
                write_chat_message(
                    thread_id=thread_id,
                    message_id=assistant_msg_id,
                    user_email=user_email,
                    role="assistant",
                    question=session.get("question", ""),  # Store question on assistant row for debugging
                    answer=(final.get("insight", "") or final.get("answer", "") or final.get("error_msg", "")),
                    path=session.get("path", "semantic"),
                    payload=_json_f.dumps(final, default=str),
                )
        except Exception as hist_err:
            print(f"[HISTORY] Failed to write assistant row in finally: {hist_err}")
        session["done"] = True




# ── PPT Generation route ──────────────────────────────────────────────────────

@app.route("/api/generate-ppt", methods=["POST"])
def api_generate_ppt():
    """Generate and return a PowerPoint chart file directly.
    
    Returns the .pptx file as a download (no intermediate storage).
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        message_id = (body.get("message_id") or "").strip()
        thread_id = (body.get("thread_id") or "").strip()
        statement_response = body.get("statement_response")
        sql = body.get("sql", "")
        question = body.get("question", "Data Export")
        metric_name = body.get("metric_name", "")
        insight = body.get("insight", "")

        if not message_id or not thread_id:
            return jsonify({"error": "message_id and thread_id are required"}), 400
        if not statement_response:
            return jsonify({"error": "statement_response is required"}), 400

        print(f"[PPT_ROUTE] Generate request: thread={thread_id[:8]} msg={message_id[:8]}")

        # Get LLM client
        from utils.llm_client import get_llm_client
        from config import LLM_MODEL
        llm_client = get_llm_client(_get_databricks_pat())

        result = generate_ppt(
            thread_id=thread_id,
            message_id=message_id,
            statement_response=statement_response,
            sql=sql,
            question=question,
            metric_name=metric_name,
            insight=insight,
            llm_client=llm_client,
            model=LLM_MODEL,
        )

        if result["success"] and result["pptx_bytes"]:
            # Build filename from chart title (sanitized for filesystem)
            import re as _re
            raw_title = result.get("title") or question or "chart"
            safe_title = _re.sub(r'[^a-zA-Z0-9_ -]+', '', raw_title).strip().replace(' ', '_')[:60]
            filename = f"{safe_title or 'c3po_chart'}.pptx"
            return send_file(
                result["pptx_bytes"],
                as_attachment=True,
                download_name=filename,
                mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        else:
            return jsonify({"success": False, "error": result.get("error", "Generation failed")}), 500

    except Exception as e:
        print(f"[PPT_ROUTE] Exception: {type(e).__name__}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Feedback route ────────────────────────────────────────────────────────────

@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """Record thumbs-up / thumbs-down feedback for a specific assistant message.

    Body JSON:
        message_id  str   required  DB message_id of the assistant row
        rating      str   required  'positive' or 'negative'
        category    str   optional  chip label (negative panel only)
        comment     str   optional  free-text comment
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        message_id = (body.get("message_id") or "").strip()
        rating     = (body.get("rating") or "").strip().lower() or None
        category   = (body.get("category") or "").strip() or None
        comment    = (body.get("comment") or "").strip() or None

        if not message_id:
            return jsonify({"error": "message_id is required"}), 400
        if rating is not None and rating not in ("positive", "negative"):
            return jsonify({"error": "rating must be 'positive' or 'negative'"}), 400

        ok = save_feedback(message_id=message_id, rating=rating, category=category, comment=comment)
        if ok:
            return jsonify({"status": "recorded"})
        else:
            return jsonify({"error": "feedback save failed — check server logs"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── History routes ───────────────────────────────────────────────────────────────

@app.route("/api/thread/new", methods=["POST"])
def api_thread_new():
    """Create a new thread ID (server-owned)."""
    return jsonify({"thread_id": str(uuid.uuid4())})


@app.route("/api/history", methods=["GET"])
def api_history():
    """Return thread list for the authenticated user."""
    user_email = (request.headers.get("X-Forwarded-Email") or
                  request.headers.get("X-Forwarded-User", "unknown"))
    threads = get_threads_for_user(user_email)
    return jsonify(threads)


@app.route("/api/thread/<thread_id>/messages", methods=["GET"])
def api_thread_messages(thread_id):
    """Return all messages for a thread in order."""
    user_email = request.headers.get("X-Forwarded-Email", request.headers.get("X-Forwarded-User", ""))
    messages = get_messages_for_thread(thread_id, user_email=user_email)
    print(f"[HISTORY] thread {thread_id}: returning {len(messages)} messages for {user_email}")
    return jsonify(messages)


# ── Static serving ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/img/<path:filename>")
def serve_images(filename):
    return send_from_directory("assets", filename)

@app.route("/<path:path>")
def catch_all(path):
    return send_from_directory(app.static_folder, "index.html")


# ── Session cleanup ────────────────────────────────────────────────────────────

def _cleanup_sessions():
    while True:
        time.sleep(5)  # short sleep so SIGTERM exits quickly
        now = time.time()
        expired = [
            sid for sid, s in list(_sessions.items())
            if (s.get("_expire") and s["_expire"] < now)          # polled & expired
            or (now - s.get("start_time", 0) > 600)               # absolute TTL: 10 min
        ]
        for sid in expired:
            _sessions.pop(sid, None)

threading.Thread(target=_cleanup_sessions, daemon=True).start()


# ── Entry point ────────────────────────────────────────────────────────────────



# ── EXCEL DOWNLOAD ────────────────────────────────────────────────────────────
@app.route("/api/download-excel", methods=["POST"])
def api_download_excel():
    """Download full query result as Excel.
    
    If 'sql' is provided, re-executes the query against the warehouse
    to get ALL rows (not just the 1000-row UI sample).
    Falls back to statement_response data if no SQL available.
    """
    import pandas as pd
    from io import BytesIO

    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Missing request body"}), 400

    try:
        sql = body.get("sql", "").strip()

        if sql:
            # Re-execute SQL to get full dataset (no row limit)
            from utils.sql_execution import execute_sql
            print(f"[EXCEL] Executing SQL for full download: {sql[:100]}...")
            df = execute_sql(sql)
            print(f"[EXCEL] Full result: {len(df)} rows x {len(df.columns)} cols")
        elif body.get("statement_response"):
            # Fallback: use the limited UI data
            sr = body["statement_response"]
            columns = [c["name"] for c in sr["manifest"]["schema"]["columns"]]
            data = sr["result"]["data_array"]
            df = pd.DataFrame(data, columns=columns)
            print(f"[EXCEL] Using statement_response: {len(df)} rows")
        else:
            return jsonify({"error": "No SQL or data provided"}), 400

        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Data")
        buf.seek(0)

        question = body.get("question", "data")[:40].strip()
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in question)
        filename = f"c3po_{safe_name}.xlsx"

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        print(f"[EXCEL] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ── README ────────────────────────────────────────────────────────────────────
@app.route("/api/readme", methods=["GET"])
def api_readme():
    """Return README.md content for the info modal."""
    from pathlib import Path
    readme_path = Path(__file__).resolve().parent / "README.md"
    if not readme_path.exists():
        return jsonify({"content": "# C3PO\n\nNo README found."}), 200
    return jsonify({"content": readme_path.read_text(encoding="utf-8")}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
