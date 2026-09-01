"""
table_store.py — Phase 2: Structured table storage.

Extracts tables from PDFs using PyMuPDF's table detection and stores them
in a SQLite database so the LLM can query exact rows via SQL-based tools
instead of guessing from text fragments in ChromaDB.

Architecture:
  - One SQLite database file: ./table_db/cogni_tables.db
  - Each detected table becomes a real SQLite table named:
      doc_{document_id}_page_{N}_table_{M}
  - Column names are derived from the table's header row
  - Section header rows (e.g. "FOYER & ENTRANCE") are stored as a
    `section` column on the next data row, not as separate rows full of NULLs
  - Numeric columns (Qty, Rate, Amount) are stored as REAL so aggregation works

Generic — works for any PDF with detectable tables, not just villa inventories.
"""

import os
import sqlite3
import re
import threading
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "table_db", "cogni_tables.db")
_db_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Returns a connection to the SQLite DB, creating the directory if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _sanitize_identifier(name: str) -> str:
    """Sanitizes a string into a valid SQLite identifier (column or table name)."""
    # Replace non-alphanumeric with underscore, collapse duplicates
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.strip("_").lower()
    if not cleaned:
        cleaned = "col"
    if cleaned[0].isdigit():
        cleaned = "c_" + cleaned
    return cleaned


def _is_section_header_row(row: list) -> bool:
    """Detects a section header row — e.g. ['FOYER & ENTRANCE', None, None, '6'].

    A section header has text in the first column that is NOT a code pattern
    (codes contain a hyphen + alphanumeric, like FLR-101) and all other
    columns are either None or contain only a number (which is the item count
    for that section, not real data).
    """
    if not row or not row[0]:
        return False
    first = str(row[0]).strip()
    # Code patterns: contain a hyphen followed by alphanumeric (FLR-101, DOR-102)
    if re.match(r"^[A-Z]+-\d+", first):
        return False
    # If the first column looks like a section name (uppercase words, no hyphen-code)
    # and the second column is None, treat it as a section header
    if len(row) > 1 and (row[1] is None or str(row[1]).strip() == ""):
        return True
    return False


def _try_float(value):
    """Converts a cell value to float, handling Indian number format (1,94,820)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Remove currency symbols, spaces, commas
    s = re.sub(r"[₹$\s,]", "", s)
    # Remove trailing non-numeric (e.g. "sqft" won't parse, that's fine)
    try:
        return float(s)
    except ValueError:
        return None


def _infer_column_types(rows: list, num_cols: int) -> list:
    """Infers whether each column should be TEXT or REAL based on data content."""
    types = []
    for col_idx in range(num_cols):
        numeric_count = 0
        text_count = 0
        for row in rows:
            if col_idx >= len(row) or row[col_idx] is None:
                continue
            # Skip section header rows — they have text in col 0, None elsewhere
            if _is_section_header_row(row):
                continue
            if _try_float(row[col_idx]) is not None:
                numeric_count += 1
            else:
                text_count += 1
        # If >70% of non-null values are numeric, treat as REAL
        total = numeric_count + text_count
        if total > 0 and numeric_count / total > 0.7:
            types.append("REAL")
        else:
            types.append("TEXT")
    return types


def extract_and_store_tables(pdf_path: str, document_id: str) -> list:
    """Extracts all tables from a PDF and stores them in SQLite.

    Args:
        pdf_path: Path to the PDF file.
        document_id: Unique document identifier (used in table names).

    Returns:
        List of table names created, e.g. ["doc_abc_page_1_table_1", ...]
    """
    import pymupdf

    created_tables = []
    doc = pymupdf.open(pdf_path)

    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            found = page.find_tables()

            for t_idx, table in enumerate(found.tables):
                rows = table.extract()
                if len(rows) < 2:  # need at least header + 1 data row
                    continue

                # PyMuPDF sometimes returns the header row twice (rows[0] == rows[1]).
                # Detect and skip the duplicate.
                header = rows[0]
                data_start = 1
                if len(rows) > 1 and rows[1] == rows[0]:
                    data_start = 2

                num_cols = table.col_count

                # Sanitize column names
                col_names = [_sanitize_identifier(h or f"col_{i}") for i, h in enumerate(header)]
                # Ensure unique column names
                seen = {}
                for i, name in enumerate(col_names):
                    if name in seen:
                        seen[name] += 1
                        col_names[i] = f"{name}_{seen[name]}"
                    else:
                        seen[name] = 0

                # Infer column types (skip header and duplicate header)
                col_types = _infer_column_types(rows[data_start:], num_cols)

                # Add a section column to track section headers
                col_defs = ["section TEXT DEFAULT NULL"]
                for i, (name, ctype) in enumerate(zip(col_names, col_types)):
                    col_defs.append(f'"{name}" {ctype}')
                col_defs_str = ", ".join(col_defs)

                table_name = f"doc_{document_id}_page_{page_num + 1}_table_{t_idx + 1}"
                # Drop if exists (re-upload case)
                cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                cursor.execute(f'CREATE TABLE "{table_name}" ({col_defs_str})')

                # Insert rows, tracking section headers
                current_section = None
                for row in rows[data_start:]:
                    if not row or all(c is None or str(c).strip() == "" for c in row):
                        continue
                    if _is_section_header_row(row):
                        current_section = str(row[0]).strip()
                        continue

                    # Build values: section + converted cells
                    values = [current_section]
                    for i, (ctype, cell) in enumerate(zip(col_types, row)):
                        if i >= len(col_names):
                            break
                        if cell is None or str(cell).strip() == "":
                            values.append(None)
                        elif ctype == "REAL":
                            values.append(_try_float(cell))
                        else:
                            values.append(str(cell).strip())

                    placeholders = ", ".join(["?"] * len(values))
                    quoted_cols = ", ".join(['"section"'] + [f'"{c}"' for c in col_names[:len(values) - 1]])
                    cursor.execute(
                        f'INSERT INTO "{table_name}" ({quoted_cols}) VALUES ({placeholders})',
                        values
                    )

                created_tables.append(table_name)

        conn.commit()
        conn.close()

    doc.close()
    return created_tables


def list_tables(document_ids: list) -> list:
    """Returns all table names belonging to the given document IDs."""
    if not document_ids:
        return []
    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        all_tables = [row[0] for row in cursor.fetchall()]
        conn.close()

    # Filter to tables matching any of the document_ids
    result = []
    for doc_id in document_ids:
        prefix = f"doc_{doc_id}_"
        result.extend([t for t in all_tables if t.startswith(prefix)])
    return sorted(result)


def describe_table(table_name: str) -> dict:
    """Returns column names, types, and 3 sample rows for a table."""
    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        # Get column info
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        columns = [{"name": row[1], "type": row[2]} for row in cursor.fetchall()]
        # Get sample rows
        cursor.execute(f'SELECT * FROM "{table_name}" LIMIT 3')
        sample_rows = [list(row) for row in cursor.fetchall()]
        # Get row count
        cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        row_count = cursor.fetchone()[0]
        conn.close()

    return {
        "table": table_name,
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample_rows,
    }


def query_table(table_name: str, columns: Optional[list] = None,
                where: Optional[str] = None, limit: int = 50) -> list:
    """Runs a SELECT query on a table.

    Args:
        table_name: The table to query.
        columns: List of column names to select. None = all columns.
        where: Optional WHERE clause (without the "WHERE" keyword).
        limit: Max rows to return (default 50).

    Returns:
        List of row dicts.
    """
    col_str = "*" if not columns else ", ".join(f'"{c}"' for c in columns)
    sql = f'SELECT {col_str} FROM "{table_name}"'
    if where:
        sql += f" WHERE {where}"
    sql += f" LIMIT {limit}"

    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        col_names = [desc[0] for desc in cursor.description]
        conn.close()

    return [dict(zip(col_names, row)) for row in rows]


def compare_tables(table_a: str, table_b: str, key_column: str,
                   mode: str = "a_not_in_b") -> list:
    """Compares two tables based on a shared key column.

    Args:
        table_a: First table name.
        table_b: Second table name.
        key_column: The column to compare on (must exist in both tables).
        mode: "a_not_in_b" = rows in A but not in B
              "b_not_in_a" = rows in B but not in A
              "in_both" = rows in both A and B

    Returns:
        List of row dicts from the relevant table.
    """
    if mode == "a_not_in_b":
        sql = f'''SELECT a.* FROM "{table_a}" a
                  WHERE a."{key_column}" NOT IN (
                      SELECT b."{key_column}" FROM "{table_b}" b
                      WHERE b."{key_column}" IS NOT NULL
                  )'''
        result_table = table_a
    elif mode == "b_not_in_a":
        sql = f'''SELECT b.* FROM "{table_b}" b
                  WHERE b."{key_column}" NOT IN (
                      SELECT a."{key_column}" FROM "{table_a}" a
                      WHERE a."{key_column}" IS NOT NULL
                  )'''
        result_table = table_b
    elif mode == "in_both":
        sql = f'''SELECT a.* FROM "{table_a}" a
                  WHERE a."{key_column}" IN (
                      SELECT b."{key_column}" FROM "{table_b}" b
                      WHERE b."{key_column}" IS NOT NULL
                  )'''
        result_table = table_a
    else:
        return [{"error": f"Invalid mode: {mode}. Use a_not_in_b, b_not_in_a, or in_both."}]

    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        col_names = [desc[0] for desc in cursor.description]
        conn.close()

    return [dict(zip(col_names, row)) for row in rows]


def aggregate_column(table_name: str, column: str,
                     operation: str = "sum") -> dict:
    """Aggregates a numeric column.

    Args:
        table_name: The table to query.
        column: The numeric column to aggregate.
        operation: sum, avg, count, min, max

    Returns:
        Dict with the operation, column, and result.
    """
    valid_ops = {"sum": "SUM", "avg": "AVG", "count": "COUNT", "min": "MIN", "max": "MAX"}
    if operation not in valid_ops:
        return {"error": f"Invalid operation: {operation}. Use: {', '.join(valid_ops.keys())}"}

    sql_func = valid_ops[operation]
    sql = f'SELECT {sql_func}("{column}") as result FROM "{table_name}"'

    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(sql)
        result = cursor.fetchone()[0]
        conn.close()

    return {
        "table": table_name,
        "column": column,
        "operation": operation,
        "result": result if result is not None else 0,
    }


def join_tables(table_a: str, table_b: str, left_key: str, right_key: str,
                compute: str = None, limit: int = 100) -> list:
    """Joins two tables on a shared key column and optionally computes a
    derived column (e.g. qty * rate = total_cost).

    Uses an INNER JOIN — only rows where the key exists in both tables are
    returned. Columns from table_a are prefixed with "a_" and columns from
    table_b with "b_" to avoid name collisions (both tables may have "code",
    "item", "qty", etc.).

    Args:
        table_a: Left table name (e.g. an inventory with quantities).
        table_b: Right table name (e.g. a rate schedule with prices).
        left_key: Column in table_a to join on (e.g. "code").
        right_key: Column in table_b to join on (e.g. "code").
        compute: Optional SQL expression for a computed column, using the
                 prefixed column names. Example: "a_qty * b_rate_inr"
                 The result is returned as a "computed" column.
        limit: Max rows to return (default 100).

    Returns:
        List of row dicts with joined + computed columns.
    """
    # Build the SELECT — prefix all columns to avoid collisions
    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()

        # Get column names from both tables
        cursor.execute(f'PRAGMA table_info("{table_a}")')
        a_cols = [row[1] for row in cursor.fetchall()]
        cursor.execute(f'PRAGMA table_info("{table_b}")')
        b_cols = [row[1] for row in cursor.fetchall()]

        # Build prefixed column list
        select_parts = []
        for c in a_cols:
            select_parts.append(f'a."{c}" as "a_{c}"')
        for c in b_cols:
            select_parts.append(f'b."{c}" as "b_{c}"')

        # Translate the compute expression: replace a_colname with a."colname"
        # and b_colname with b."colname" so the user can write natural expressions
        # like "a_qty * b_rate_inr" without knowing SQL quoting rules.
        compute_sql = compute
        if compute_sql:
            for c in reversed(a_cols):  # reversed so longer names match first
                compute_sql = compute_sql.replace(f"a_{c}", f'a."{c}"')
            for c in reversed(b_cols):
                compute_sql = compute_sql.replace(f"b_{c}", f'b."{c}"')
            select_parts.append(f'({compute_sql}) as "computed"')

        select_str = ", ".join(select_parts)
        sql = f'''SELECT {select_str}
                  FROM "{table_a}" a
                  INNER JOIN "{table_b}" b ON a."{left_key}" = b."{right_key}"
                  WHERE a."{left_key}" IS NOT NULL AND b."{right_key}" IS NOT NULL
                  LIMIT {limit}'''

        cursor.execute(sql)
        rows = cursor.fetchall()
        col_names = [desc[0] for desc in cursor.description]
        conn.close()

    return [dict(zip(col_names, row)) for row in rows]


def delete_tables_for_document(document_id: str) -> int:
    """Deletes all SQLite tables for a given document_id (chat deletion cleanup).

    Returns the number of tables dropped.
    """
    prefix = f"doc_{document_id}_"
    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        all_tables = [row[0] for row in cursor.fetchall()]
        dropped = 0
        for t in all_tables:
            if t.startswith(prefix):
                cursor.execute(f'DROP TABLE IF EXISTS "{t}"')
                dropped += 1
        conn.commit()
        conn.close()
    return dropped
