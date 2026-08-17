import json, re, time, math, os, requests, sys
from pathlib import Path
import pandas as pd
import openai
from databricks import sql as databricks_sql

try:
    from config import (
        WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
        VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD,
        SUPERVISOR_ENDPOINT_URL,
    )
except ModuleNotFoundError:
    project_root = Path(__file__).resolve().parent.parent if "__file__" in globals() else Path.cwd().parent
    sys.path.append(str(project_root))
    from config import (
        WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
        VECTOR_INDEX_NAME, LLM_MODEL, SCORE_THRESHOLD,
        SUPERVISOR_ENDPOINT_URL,
    )


## LLM Client
def get_llm_client(token=None):
    # api_token = token or _get_databricks_pat()
    if token is None:
        token=os.environ.get("DATABRICKS_PAT")
    return openai.OpenAI(
        api_key=token,
        base_url=f"https://{WORKSPACE_HOST}/serving-endpoints",
    )