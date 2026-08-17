
import json, re, time, math, os, requests, sys
from pathlib import Path
import pandas as pd
import openai
from databricks import sql as databricks_sql

sys.path.append(str(Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd().parent))

from config import (
        WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
        VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD,
        SUPERVISOR_ENDPOINT_URL,
    )


def _get_databricks_pat():
    """PAT for all operations (SQL, LLM, supervisor, vector search, MLflow).
    Has UC catalog + Genie + endpoint access via group membership.
    """
    return os.environ.get("DATABRICKS_PAT", "")


def execute_sql(sql_query, token=None):
    print(f"[AUTH] execute_sql: {'PAT(_get_databricks_pat)' if not token else 'explicit_token'}")
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token or _get_databricks_pat(),
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_query)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns)



def _execute_sql_with_params(sql, params, token=None):
    """Execute parameterised SQL (? placeholders), return DataFrame."""
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token or _get_databricks_pat(),
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns)


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

def execute_sql_with_types(sql_query, token=None):
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token or _get_databricks_pat(),
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_query)
            columns = [desc[0] for desc in cursor.description]
            type_names = [_get_type_name(desc[1]) for desc in cursor.description]
            rows = cursor.fetchall()
            return pd.DataFrame(rows, columns=columns), type_names
