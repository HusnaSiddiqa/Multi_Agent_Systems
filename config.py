"""HIV C3PO v3 - Configuration"""
import os

WORKSPACE_HOST = os.environ.get("DATABRICKS_HOSTNAME", "")

SQL_WAREHOUSE_HTTP_PATH = os.environ.get("SQL_WAREHOUSE_HTTP_PATH","")

LLM_MODEL = os.environ.get("LLM_MODEL","")

# HIV templates vector index (create this once templates table is populated)

VECTOR_INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME")

# Template matching threshold
SCORE_THRESHOLD = float(os.environ.get("SCORE_THRESHOLD", "0.55"))
UPPER_SCORE_THRESHOLD = float(os.environ.get("UPPER_SCORE_THRESHOLD", "0.95"))

# Typeahead config
MAX_SUGGESTIONS = int(os.environ.get("MAX_SUGGESTIONS", "4"))
MIN_CHARS_FOR_SUGGESTIONS = int(os.environ.get("MIN_CHARS_FOR_SUGGESTIONS", "2"))

# Supervisor agent serving endpoint (fallback when template match fails)
SUPERVISOR_ENDPOINT_NAME = os.environ.get("SUPERVISOR_ENDPOINT_NAME","")

SUPERVISOR_ENDPOINT_URL = os.environ.get("SUPERVISOR_ENDPOINT_URL","")

# UC Volume path for the narrative deck output
NARRATIVE_DECK_VOLUME_PATH = os.environ.get("NARRATIVE_DECK_VOLUME_PATH", "")
