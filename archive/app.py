"""
HIV C3PO v3 - Flask API Backend
Template matching first → supervisor agent fallback on no match.
"""

import os, uuid, time, threading
from flask import Flask, request, jsonify, send_from_directory

from config import SCORE_THRESHOLD
from suggestions import load_template_examples, search_questions
from executor import (
    execute_click_path_streaming,
    execute_semantic_path_streaming,
    generate_insight,
    to_statement_response,
    write_chat_message,
    get_threads_for_user,
    get_messages_for_thread,
    step_exact_match,
)
from feedback_module import save_feedback

# ── App init ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="")

try:
    examples_df = load_template_examples()
    print(f"[STARTUP] Templates loaded: {len(examples_df)} rows")
except Exception as e:
    print(f"[STARTUP WARNING] Template load failed: {e} — will retry on first request")
    examples_df = None

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
        template_row = body.get("template_row")
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

        threading.Thread(
            target=_process_question,
            args=(session_id, question, template_row, None, conversation_history, user_email, thread_id),
            daemon=True,
        ).start()

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
    if session["done"]:
        response["result"] = session["result"]
        session["_expire"] = time.time() + 300
    return jsonify(response)


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok", "templates_loaded": len(examples_df) if examples_df is not None else 0})


@app.route("/api/debug", methods=["GET"])
def api_debug():
    import traceback
    results = {}
    from executor import _get_sql_token
    token = _get_sql_token()
    results["token_set"] = bool(token)
    results["token_source"] = "PAT (DATABRICKS_PAT)"
    try:
        from executor import execute_sql
        df = execute_sql(
            "SELECT count(*) as cnt FROM `commercial-us-hiv-iiaf-dev`.`c3po_basic`.`hiv_metric_template_examples` WHERE is_active = true",
        )
        results["sql_ok"] = True
        results["template_count"] = int(df.iloc[0]["cnt"])
    except Exception as e:
        results["sql_ok"] = False
        results["sql_error"] = str(e)
    try:
        from executor import get_llm_client
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
            def on_step(name, detail):
                session["steps"].append({"name": name, "details": detail, "time": time.time()})
            result = execute_click_path_streaming(question, template_row, on_step, token=token, conversation_history=conversation_history)
        else:
            # STEP 1: Exact match check — cheap SQL lookup before any LLM/vector work
            session["steps"].append({"name": "Exact Match", "details": "Checking template table...", "time": time.time()})
            matched_row = step_exact_match(question)
            if matched_row:
                # Exact match found — but only use click path if SQL is defined
                sql_val = matched_row.get("sql_template") or ""
                if sql_val.strip():
                    session["path"] = "click"
                    session["steps"][-1]["details"] = f"Matched: {matched_row.get('template_id', '?')}"
                    def on_step(name, detail):
                        session["steps"].append({"name": name, "details": detail, "time": time.time()})
                    result = execute_click_path_streaming(question, matched_row, on_step, token=token, conversation_history=conversation_history)
                else:
                    # Template matched but has no SQL — fall through to semantic/supervisor
                    session["steps"][-1]["details"] = f"Matched template {matched_row.get('template_id', '?')} but no SQL — routing to supervisor"
                    matched_row = None  # force fall-through
            else:
                # STEP 2: No exact match — fall through to semantic path (includes supervisor fallback)
                session["steps"][-1]["details"] = "No exact match — trying semantic search"
                def on_steps(steps_done):
                    session["steps"] = session["steps"][:1] + steps_done
                result = execute_semantic_path_streaming(question, on_steps, token=token, conversation_history=conversation_history)

        # Insight generation (template path only — supervisor already has answer text)
        if result.get("success") and result.get("result_data") and result.get("source") == "template":
            session["steps"].append({"name": "Insight", "details": "Generating summary...", "time": time.time()})
            result["insight"] = generate_insight(
                question,
                result.get("slot_values", {}),
                result.get("result_data", []),
                result.get("result_columns", []),
                token=token,
            )
        elif result.get("source") == "supervisor":
            # Supervisor answer text IS the insight
            result["insight"] = result.get("answer", "")

        # Shape data for chart rendering
        if result.get("success") and result.get("result_data"):
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
        }
        session["result"] = final_result

        # Write assistant reply after processing completes. The user row is already written in api_ask.
        if thread_id and user_email:
            write_chat_message(
                thread_id=thread_id,
                message_id=assistant_message_id,
                user_email=user_email,
                role="assistant",
                answer=(result.get("insight", "") or result.get("answer", "") or result.get("error_msg", "")),
                path=session.get("path", "semantic"),
                payload=_json.dumps(final_result, default=str),
            )
    except Exception as e:
        session["result"] = {"success": False, "error_msg": f"{type(e).__name__}: {str(e)}"}
    finally:
        session["done"] = True


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
    messages = get_messages_for_thread(thread_id)
    print(f"[HISTORY] thread {thread_id}: returning {len(messages)} messages")
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
        expired = [sid for sid, s in list(_sessions.items()) if s.get("_expire") and s["_expire"] < now]
        for sid in expired:
            _sessions.pop(sid, None)

threading.Thread(target=_cleanup_sessions, daemon=True).start()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
