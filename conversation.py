import json, re, time, math, os, requests
import pandas as pd
import openai
from databricks import sql as databricks_sql
from config import (
    WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
    VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD,
    SUPERVISOR_ENDPOINT_URL
)

from utils.sql_execution import _execute_sql_with_params, _get_databricks_pat,execute_sql ,_get_type_name , execute_sql_with_types

_CHAT_HISTORY_TABLE = os.environ.get("CHAT_HISTORY_TABLE", "")


def write_chat_message(thread_id, message_id, user_email, role,
                       question=None, answer=None, path=None, payload=None, token=None):
    """Insert one chat message row using parameterised query."""
    print(f"[AUTH] write_chat_message: PAT(_get_databricks_pat)")
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
            access_token=token or _get_databricks_pat(),
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
            # Rename thread_created_at -> created_at for API response consistency
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


def get_messages_for_thread(thread_id, user_email=None, token=None):
    """Return all messages for a thread in chronological order.
    If user_email is provided, only returns messages belonging to that user."""
    if user_email:
        sql = (
            f"SELECT thread_id, message_id, user_email, role, question, answer, "
            f"path, payload, created_at, "
            f"feedback_rating, feedback_request, feedback_comment "
            f"FROM {_CHAT_HISTORY_TABLE} "
            f"WHERE thread_id = ? AND user_email = ? "
            f"ORDER BY created_at ASC"
        )
        params = [thread_id, user_email]
    else:
        sql = (
            f"SELECT thread_id, message_id, user_email, role, question, answer, "
            f"path, payload, created_at, "
            f"feedback_rating, feedback_request, feedback_comment "
            f"FROM {_CHAT_HISTORY_TABLE} "
            f"WHERE thread_id = ? "
            f"ORDER BY created_at ASC"
        )
        params = [thread_id]
    try:
        df = _execute_sql_with_params(sql, params, token)
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