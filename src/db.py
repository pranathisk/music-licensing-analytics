"""Shared database helpers. Everything uses one SQLite file, db/licensing.db."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "licensing.db"
SCHEMA_PATH = ROOT / "db" / "schema.sql"


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def resolve(path: str | Path) -> Path:
    """Paths are stored relative to the repo root; turn one into an absolute path."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
