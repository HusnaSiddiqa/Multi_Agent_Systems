"""HIV C3PO v3 - Feedback Module

Records user feedback (thumbs-up / thumbs-down, category chip, free-text comment)
against an existing assistant message row in the chat history table.
Keeps feedback logic isolated so app.py stays clean.
"""

import os
from databricks import sql as databricks_sql
from config import WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH
from utils.sql_execution import _execute_sql_with_params, _get_databricks_pat,execute_sql ,_get_type_name , execute_sql_with_types

# Must match the hardcoded constant in executor.py
# _CHAT_HISTORY_TABLE = "`commercial-us-hiv-iiaf-dev`.`c3po_basic`.`hiv_c3po_v3_chat_history`"

_CHAT_HISTORY_TABLE = os.environ.get("CHAT_HISTORY_TABLE")


# def _get_sql_token():
#     """Same PAT used by executor.py for all DB operations."""
#     return os.environ.get("DATABRICKS_PAT", "")

# from utils.sql_execution import _execute_sql_with_params, _get_databricks_pat,execute_sql ,_get_type_name , execute_sql_with_types


def save_feedback(
    message_id: str,
    rating: str,
    category: str = None,
    comment: str = None,
    token: str = None,
) -> bool:
    """UPDATE the existing assistant row identified by message_id.

    Args:
        message_id: DB message_id of the assistant row to update.
        rating:     'positive' or 'negative'.
        category:   Chip label selected in negative panel (optional).
        comment:    Free-text typed by user (optional).
        token:      Override PAT (defaults to DATABRICKS_PAT env var).

    Returns True on success, False on any error.
    Swallows all exceptions so it never blocks the request flow.
    """
    if not message_id:
        print(f"[FEEDBACK] invalid args: message_id={message_id!r}")
        return False
    if rating is not None and rating not in ("positive", "negative"):
        print(f"[FEEDBACK] invalid rating: {rating!r}")
        return False
    try:
        sql = (
            f"UPDATE {_CHAT_HISTORY_TABLE} "
            "SET feedback_rating = ?, "
            "    feedback_request = ?, "
            "    feedback_comment = ?, "
            "    feedback_at = current_timestamp() "
            "WHERE message_id = ?"
        )
        params = [rating, category, comment, message_id]
        with databricks_sql.connect(
            server_hostname=WORKSPACE_HOST,
            http_path=SQL_WAREHOUSE_HTTP_PATH,
            access_token=token or _get_databricks_pat(),
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
        print(f"[FEEDBACK] saved rating={rating} category={category!r} for message_id={message_id}")
        return True
    except Exception as e:
        print(f"[FEEDBACK] save_feedback failed: {e}")
        return False
