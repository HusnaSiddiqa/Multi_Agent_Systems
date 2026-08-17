import json, re, time, math, os, requests, sys
from pathlib import Path
import pandas as pd


def _infer_column_type(values: list) -> str:
    """Infer column type from actual data values using pandas.
    
    Pure data-driven — no column name heuristics.
    Returns: 'LONG', 'DOUBLE', 'DATE', or 'STRING'
    """
    # Filter non-null values
    non_null = [v for v in values if v is not None and str(v).strip() not in ("", "None", "null")]
    
    if not non_null:
        return "STRING"  # All nulls — safe default
    
    # Sample up to 20 values for inference
    sample = non_null[:20]
    
    # Try numeric first (most common need)
    numeric_count = 0
    has_decimal = False
    for v in sample:
        s = str(v).strip().rstrip('%').replace(',', '').strip()
        try:
            num = float(s)
            numeric_count += 1
            if '.' in str(v) or '%' in str(v):
                has_decimal = True
        except (ValueError, TypeError):
            pass
    
    # If majority (>= 80%) of non-null values are numeric → numeric type
    if numeric_count >= len(sample) * 0.8:
        return "DOUBLE" if has_decimal else "LONG"
    
    # Try date detection
    date_count = 0
    for v in sample:
        s = str(v).strip()
        # Common date patterns: YYYY-MM-DD, MM/DD/YYYY, Mon YYYY, Q1'25
        if re.match(r'^\d{4}-\d{2}-\d{2}', s):
            date_count += 1
        elif re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}', s):
            date_count += 1
        elif re.match(r"^Q\d'\d{2}", s):
            date_count += 1
        elif re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', s, re.IGNORECASE):
            date_count += 1
    
    if date_count >= len(sample) * 0.8:
        return "DATE"
    
    return "STRING"


def to_statement_response(result_data, result_columns, type_names=None):
    """Convert raw result data to statement_response format.
    
    If type_names not provided, infers types from actual data values
    using pandas-style detection (no column name heuristics).
    """
    if type_names is None:
        type_names = []
        for i, col in enumerate(result_columns):
            # Extract column values from data
            col_values = [row[i] if i < len(row) else None for row in result_data]
            detected = _infer_column_type(col_values)
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
