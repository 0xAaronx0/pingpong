"""SQLite storage for the pingpong broker.

Deliberately dumb: the broker stores offers, interests and per-recipient inbox
events. It never stores plaintext contact data — only opaque sealed blobs that
it cannot decrypt (see docs/PROTOCOL.md §3.3, §4).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("PINGPONG_DB", os.path.join(os.path.dirname(__file__), "pingpong.db"))

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    enc_pubkey  TEXT NOT NULL,
    activity    TEXT NOT NULL,
    title       TEXT,
    geocell     TEXT NOT NULL,
    earliest    TEXT NOT NULL,
    latest      TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    offer_sig   TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_lookup ON offers (status, geocell, activity);
CREATE INDEX IF NOT EXISTS idx_offers_agent  ON offers (agent_id, status);

CREATE TABLE IF NOT EXISTS interests (
    id              TEXT PRIMARY KEY,
    offer_id        TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    enc_pubkey      TEXT NOT NULL,
    sealed_for_owner TEXT NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    interest_sig    TEXT
);
CREATE INDEX IF NOT EXISTS idx_interests_offer ON interests (offer_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interests_unique ON interests (offer_id, agent_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient   TEXT NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_recipient ON events (recipient, ts);

CREATE TABLE IF NOT EXISTS blocklist (
    agent_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS reports (
    id          TEXT PRIMARY KEY,
    offer_id    TEXT NOT NULL,
    reporter    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (offer_id, reporter)
);
"""

# Columns added after the first deployment; applied idempotently on init so an
# existing SQLite volume upgrades in place.
_MIGRATIONS = [
    ("offers", "offer_sig", "ALTER TABLE offers ADD COLUMN offer_sig TEXT"),
    ("interests", "interest_sig", "ALTER TABLE interests ADD COLUMN interest_sig TEXT"),
]


def now_iso() -> str:
    # timespec is pinned so every stored timestamp has the identical shape
    # ("...±HH:MM" with 6-digit microseconds) — string comparison in SQL is
    # only sound because all timestamps share this exact format (UTC-normalized
    # at ingestion, see app._parse_ts).
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init() -> None:
    global _conn
    if _conn is not None:
        return
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            _conn.execute(ddl)
    _conn.commit()


def conn() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur


def transaction(statements: list) -> None:
    """Run [(sql, params), ...] atomically: one lock, one commit, rollback on error."""
    with _lock:
        try:
            for sql, params in statements:
                conn().execute(sql, params)
            conn().commit()
        except Exception:
            conn().rollback()
            raise


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, params).fetchone()


# --- helpers --------------------------------------------------------------

def is_blocked(agent_id: str) -> bool:
    return query_one("SELECT 1 FROM blocklist WHERE agent_id=?", (agent_id,)) is not None


def open_offer_count(agent_id: str) -> int:
    row = query_one(
        "SELECT COUNT(*) AS n FROM offers WHERE agent_id=? AND status='open' AND expires_at>?",
        (agent_id, now_iso()),
    )
    return row["n"] if row else 0


def expire_stale() -> None:
    """Mark offers whose expiry passed as closed, and their pending interests expired.

    The interest sweep is scoped to closed/withdrawn offers only — an offer that
    already produced a match stays open and its remaining pending interests stay
    acceptable (PROTOCOL §4)."""
    transaction([
        ("UPDATE offers SET status='closed' WHERE status='open' AND expires_at<=?",
         (now_iso(),)),
        ("UPDATE interests SET status='expired' WHERE status='pending' AND offer_id IN "
         "(SELECT id FROM offers WHERE status IN ('closed','withdrawn','removed'))", ()),
    ])


def push_event(recipient: str, type_: str, payload: dict) -> None:
    execute(
        "INSERT INTO events (recipient, type, payload, ts) VALUES (?,?,?,?)",
        (recipient, type_, json.dumps(payload), now_iso()),
    )
