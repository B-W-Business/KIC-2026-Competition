"""Small local persistence layer for the single-user MVP."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    connection.execute("CREATE TABLE IF NOT EXISTS filing_matches (ticker TEXT NOT NULL, accession TEXT NOT NULL, assumption_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (ticker, accession, assumption_id))")
    connection.execute("CREATE TABLE IF NOT EXISTS processed_events (ticker TEXT NOT NULL, accession TEXT NOT NULL, assumption_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, error TEXT, PRIMARY KEY (ticker, accession, assumption_id))")
    return connection


@contextmanager
def _session():
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_state() -> Tuple[List[dict], List[dict]]:
    with _session() as connection:
        assumptions = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM assumptions ORDER BY rowid")]
        reports = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM reports ORDER BY created_at DESC")]
    return assumptions, reports


def save_assumption(assumption: Any) -> None:
    payload = asdict(assumption) if hasattr(assumption, "__dataclass_fields__") else assumption
    if isinstance(payload, dict) and "tolerance_basis" not in payload:
        payload["tolerance_basis"] = getattr(assumption, "tolerance_basis", "")
    with _session() as connection:
        connection.execute("INSERT OR REPLACE INTO assumptions (id, payload) VALUES (?, ?)",
                           (payload["id"], json.dumps(payload, ensure_ascii=False)))


def save_report(report: dict) -> None:
    with _session() as connection:
        connection.execute("INSERT OR IGNORE INTO reports (id, created_at, ticker, payload) VALUES (?, ?, ?, ?)",
                           (report["id"], report["created_at"], report["ticker"], json.dumps(report, ensure_ascii=False)))


def get_filing_match(ticker: str, accession: str, assumption_id: str):
    with _session() as connection:
        row = connection.execute("SELECT payload FROM filing_matches WHERE ticker=? AND accession=? AND assumption_id=?",
                                 (ticker, accession, assumption_id)).fetchone()
    return json.loads(row[0]) if row else None


def save_filing_match(ticker: str, accession: str, assumption_id: str, result: dict) -> None:
    with _session() as connection:
        connection.execute("INSERT OR REPLACE INTO filing_matches (ticker, accession, assumption_id, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (ticker, accession, assumption_id, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))


def claim_event(ticker: str, accession: str, assumption_id: str, max_attempts: int = 3, lease_minutes: int = 30) -> bool:
    """Atomically claim an event for report generation, with bounded retry after failure."""
    now = datetime.now(timezone.utc)
    with _session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status, attempts, updated_at FROM processed_events WHERE ticker=? AND accession=? AND assumption_id=?",
                                 (ticker, accession, assumption_id)).fetchone()
        if not row:
            connection.execute("INSERT INTO processed_events (ticker, accession, assumption_id, status, attempts, updated_at) VALUES (?, ?, ?, 'processing', 1, ?)",
                               (ticker, accession, assumption_id, now.isoformat()))
            return True
        status, attempts, updated_at = row
        if status == "completed" or attempts >= max_attempts:
            return False
        try:
            stale = now - datetime.fromisoformat(updated_at) > timedelta(minutes=lease_minutes)
        except ValueError:
            stale = True
        if status == "processing" and not stale:
            return False
        connection.execute("UPDATE processed_events SET status='processing', attempts=attempts+1, updated_at=?, error=NULL WHERE ticker=? AND accession=? AND assumption_id=?",
                           (now.isoformat(), ticker, accession, assumption_id))
        return True


def finish_event(ticker: str, accession: str, assumption_id: str, report: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR IGNORE INTO reports (id, created_at, ticker, payload) VALUES (?, ?, ?, ?)",
                           (report["id"], report["created_at"], report["ticker"], json.dumps(report, ensure_ascii=False)))
        connection.execute("UPDATE processed_events SET status='completed', updated_at=?, error=NULL WHERE ticker=? AND accession=? AND assumption_id=?",
                           (now, ticker, accession, assumption_id))


def fail_event(ticker: str, accession: str, assumption_id: str, error: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _session() as connection:
        connection.execute("UPDATE processed_events SET status='failed', updated_at=?, error=? WHERE ticker=? AND accession=? AND assumption_id=?",
                           (now, error[:1500], ticker, accession, assumption_id))
