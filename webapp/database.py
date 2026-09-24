"""
Database connection management.
"""
import sqlite3
from webapp.extensions import DB_PATH


def get_db():
    """Create a new database connection with WAL mode and appropriate timeouts."""
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.OperationalError as e:
        print(f"[DB] PRAGMA setup warning: {e}")
    return conn
