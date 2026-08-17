"""ONC C3PO v3 - Suggestion Engine (live typeahead)

Loads all template examples from DB into memory (cached 5 min).
Uses tiered scoring: exact word match > fuzzy match > substring.
Filters out common stop-words so only meaningful keywords drive ranking.
"""

import re
import time
import math
import pandas as pd
import os
from rapidfuzz import fuzz
from databricks import sql as databricks_sql
from config import (
    WORKSPACE_HOST, SQL_WAREHOUSE_HTTP_PATH,
    MAX_SUGGESTIONS, MIN_CHARS_FOR_SUGGESTIONS,
)

# Common English stop-words that should not contribute to scoring.
# These appear in almost every question and cause irrelevant results to
# score high (e.g. typing "What is the" matches everything equally).
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "has", "have", "had", "what", "which", "who",
    "how", "when", "where", "why", "can", "could", "will", "would",
    "shall", "should", "may", "might", "must", "of", "in", "on", "at",
    "to", "for", "by", "with", "from", "and", "or", "but", "not", "no",
    "if", "it", "its", "this", "that", "these", "those", "i", "me", "my",
    "we", "our", "you", "your",
})

# Regex to strip punctuation from word boundaries so "TNBC?" becomes "tnbc"
_WORD_RE = re.compile(r"[a-z0-9#+.%]+", re.IGNORECASE)


def _get_startup_token():
    """PAT for SQL operations (template loading at startup)."""
    import os
    return os.environ.get("DATABRICKS_PAT", "")


_examples_cache = None
_cache_time = 0


def load_template_examples() -> pd.DataFrame:
    """Load ALL example questions into memory. Cached 5 minutes."""
    global _examples_cache, _cache_time
    if _examples_cache is not None and (time.time() - _cache_time) < 300:
        return _examples_cache
    
    SUGGESTION_QUESTIONS_TABLE = os.environ.get("SUGGESTION_QUESTIONS_TABLE","")
    
    query = f"""
    SELECT example_id,template_id, template_group, metric_name, question,
           masked_question, sql_template, masking_contract_json,
           slot_output_contract_json, answer_type, is_active, example_slot_values_json
    FROM {SUGGESTION_QUESTIONS_TABLE}
    WHERE is_active = true
    ORDER BY template_id, question
    """
    token = _get_startup_token()
    with databricks_sql.connect(
        server_hostname=WORKSPACE_HOST,
        http_path=SQL_WAREHOUSE_HTTP_PATH,
        access_token=token,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            df = pd.DataFrame(rows, columns=columns)
    df["_question_lower"] = df["question"].str.lower()
    _examples_cache = df
    _cache_time = time.time()
    print("load template columns retrived: ",df.columns)
    return df


def _clean_value(val):
    if val is None:
        return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    return str(val)


def _tokenize(text: str) -> list[str]:
    """Extract lowercase alphanumeric tokens, stripping punctuation."""
    return _WORD_RE.findall(text.lower())


def _score_question(keywords: list[str], question_lower: str) -> float:
    """Score a question against the user's search keywords.

    Scoring tiers (per keyword):
      - Exact word match:  2.0
      - Fuzzy match (≥75): 1.0
      - Substring match:   0.5

    Stop-words are excluded from keywords so common filler words
    like "what", "is", "the" don't inflate scores for every question.
    """
    q_words = _tokenize(question_lower)
    total = 0.0
    for kw in keywords:
        # Skip stop-words — they match everything and add noise
        if kw in _STOP_WORDS:
            continue
        if kw in q_words:
            total += 2.0
        elif any(fuzz.ratio(kw, w) >= 75 for w in q_words):
            total += 1.0
        elif kw in question_lower:
            total += 0.5
    return total


def search_questions(search_term: str, examples_df: pd.DataFrame) -> list:

    if not search_term or len(search_term.strip()) < MIN_CHARS_FOR_SUGGESTIONS:
        return []
    if examples_df is None or examples_df.empty:
        return []
    keywords = _tokenize(search_term)
    # If the user only typed stop-words (e.g. "what is the"), don't search
    meaningful = [kw for kw in keywords if kw not in _STOP_WORDS]
    if not meaningful:
        return []
    questions = examples_df["_question_lower"]
    scores = questions.apply(lambda q: _score_question(keywords, q))
    mask = scores > 0
    if not mask.any():
        return []
    matched_df = examples_df[mask].assign(_score=scores[mask])
    matched_df = matched_df.sort_values(["_score", "question"], ascending=[False, True])
    # Deduplicate by question text so the same question from multiple
    # template rows doesn't consume all suggestion slots
    matched_df = matched_df.drop_duplicates(subset=["question"], keep="first")
    top = matched_df.head(MAX_SUGGESTIONS)
    results = []
    for _, row in top.iterrows():
        label = row["question"]
        row_dict = row.drop("_score").to_dict()
        clean_dict = {k: _clean_value(v) for k, v in row_dict.items()}
        results.append((label, clean_dict))
    return results
