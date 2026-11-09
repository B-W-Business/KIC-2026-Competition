"""Small local persistence layer for the single-user MVP."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, List, Tuple

DB_PATH = Path(os.getenv("KIC_DB_PATH", Path(__file__).with_name(".kic_data") / "monitor.sqlite3"))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=15)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS assumptions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, ticker TEXT NOT NULL, payload TEXT NOT NULL)")
    return connection


def load_state() -> Tuple[List[dict], List[dict]]:
    with _connect() as connection:
        assumptions = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM assumptions ORDER BY rowid")]
        reports = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM reports ORDER BY created_at DESC")]
    return assumptions, reports


def save_assumption(assumption: Any) -> None:
    payload = asdict(assumption) if hasattr(assumption, "__dataclass_fields__") else assumption
    with _connect() as connection:
        connection.execute("INSERT OR REPLACE INTO assumptions (id, payload) VALUES (?, ?)",
                           (payload["id"], json.dumps(payload, ensure_ascii=False)))


def save_report(report: dict) -> None:
    with _connect() as connection:
        connection.execute("INSERT OR IGNORE INTO reports (id, created_at, ticker, payload) VALUES (?, ?, ?, ?)",
                           (report["id"], report["created_at"], report["ticker"], json.dumps(report, ensure_ascii=False)))
