"""pingpong broker — the "schwarzes Brett" (see docs/PROTOCOL.md).

A deliberately dumb board of geo-tagged activity offers that mediates a
double-opt-in, end-to-end-sealed contact exchange. It verifies Ed25519
signatures on every mutating request but never sees plaintext contacts.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

import crypto
import db

MAX_TTL_HOURS = int(os.environ.get("PINGPONG_MAX_TTL_HOURS", "24"))
MAX_OPEN_OFFERS = int(os.environ.get("PINGPONG_MAX_OPEN_OFFERS", "5"))
RATE_PER_MIN = int(os.environ.get("PINGPONG_RATE_PER_MIN", "30"))
CLOCK_SKEW = 120          # seconds, §1.1
NONCE_TTL = 300           # seconds to remember a nonce for replay protection

GEOCELL_RE = re.compile(r"^[0123456789bcdefghjkmnpqrstuvwxyz]{6}$")  # geohash precision 6, §2
ACTIVITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")                  # §6 tag shape
MAX_QUERY_CELLS = 128

OFFER_PUBLIC_FIELDS = (
    "id", "agent_id", "enc_pubkey", "activity", "title",
    "geocell", "earliest", "latest", "note", "created_at", "expires_at", "status",
)

app = FastAPI(title="pingpong broker", version="0.1")

# In-memory guards (single-worker MVP). Move to Redis if you scale horizontally.
_seen_nonces: dict[tuple[str, str], float] = {}
_rate: dict[str, list[float]] = defaultdict(list)


@app.on_event("startup")
def _startup() -> None:
    db.init()


# --- signature / guard dependency ----------------------------------------

def _prune_nonces(now: float) -> None:
    for k, ts in list(_seen_nonces.items()):
        if now - ts > NONCE_TTL:
            del _seen_nonces[k]


def _is_replay(agent_id: str, nonce: str, now: float) -> bool:
    key = (agent_id, nonce)
    if key in _seen_nonces:
        return True
    _seen_nonces[key] = now
    return False


def _rate_ok(agent_id: str, now: float) -> bool:
    window = _rate[agent_id]
    window[:] = [t for t in window if now - t < 60]
    if len(window) >= RATE_PER_MIN:
        return False
    window.append(now)
    return True


async def verify_signed(request: Request) -> str:
    """Verify the signature headers (§1.1) and return the caller's agent_id.
    Stashes the raw body on request.state for handlers to parse."""
    body = await request.body()
    request.state.raw_body = body

    agent_id = request.headers.get("X-Agent-Id")
    sig = request.headers.get("X-Signature")
    ts = request.headers.get("X-Timestamp")
    nonce = request.headers.get("X-Nonce")
    if not (agent_id and sig and ts and nonce):
        raise HTTPException(401, "missing signature headers")

    now = time.time()
    try:
        if abs(now - int(ts)) > CLOCK_SKEW:
            raise HTTPException(401, "stale timestamp")
    except ValueError:
        raise HTTPException(401, "bad timestamp")

    _prune_nonces(now)
    if db.is_blocked(agent_id):
        raise HTTPException(403, "blocked")
    if not crypto.verify(agent_id, sig, request.method, request.url.path, body, ts, nonce):
        raise HTTPException(401, "bad signature")
    if _is_replay(agent_id, nonce, now):
        raise HTTPException(401, "replay")
    if not _rate_ok(agent_id, now):
        raise HTTPException(429, "rate limited")
    return agent_id


def _json_body(request: Request) -> dict:
    raw = getattr(request.state, "raw_body", b"")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")
    return data


def _require(data: dict, *fields: str) -> None:
    missing = [f for f in fields if not data.get(f)]
    if missing:
        raise HTTPException(422, f"missing fields: {', '.join(missing)}")


def _capped(s, n: int):
    if s is not None and len(s) > n:
        raise HTTPException(422, f"field too long (max {n})")
    return s


def _parse_ts(value, field: str) -> datetime:
    """Parse a client timestamp, require a timezone, return it in UTC.

    All timestamps are normalized to one canonical string shape before storage
    (see db.now_iso) — lexicographic comparison in SQL is only valid under that
    invariant."""
    if not isinstance(value, str):
        raise HTTPException(422, f"{field}: ISO-8601 string required")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{field}: invalid ISO-8601 timestamp")
    if dt.tzinfo is None:
        raise HTTPException(422, f"{field}: timezone offset required")
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _valid_pubkey(value, field: str) -> str:
    import crypto as _c
    try:
        if len(_c.b64url_decode(value)) != 32:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field}: must be base64url of 32 bytes")
    return value


# --- offers ---------------------------------------------------------------

@app.post("/offers", status_code=201)
async def create_offer(request: Request):
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "enc_pubkey", "activity", "geocell", "earliest", "latest")
    _capped(data.get("title"), 200)
    _capped(data.get("note"), 200)
    _valid_pubkey(data["enc_pubkey"], "enc_pubkey")
    if not isinstance(data["geocell"], str) or not GEOCELL_RE.match(data["geocell"]):
        raise HTTPException(422, "geocell: geohash with precision 6 required")
    if not isinstance(data["activity"], str) or not ACTIVITY_RE.match(data["activity"]):
        raise HTTPException(422, "activity: lowercase tag required (see PROTOCOL §6)")

    now = datetime.now(timezone.utc)
    earliest = _parse_ts(data["earliest"], "earliest")
    latest = _parse_ts(data["latest"], "latest")
    if latest <= earliest:
        raise HTTPException(422, "latest must be after earliest")
    if latest <= now:
        raise HTTPException(422, "time window is already in the past")
    expires = min(latest, now + timedelta(hours=MAX_TTL_HOURS))

    db.expire_stale()
    if db.open_offer_count(agent_id) >= MAX_OPEN_OFFERS:
        raise HTTPException(429, f"too many open offers (max {MAX_OPEN_OFFERS})")

    offer_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO offers (id, agent_id, enc_pubkey, activity, title, geocell, "
        "earliest, latest, note, created_at, expires_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')",
        (offer_id, agent_id, data["enc_pubkey"], data["activity"], data.get("title"),
         data["geocell"], _iso(earliest), _iso(latest), data.get("note"),
         _iso(now), _iso(expires)),
    )
    return {"offer_id": offer_id, "expires_at": _iso(expires)}


@app.get("/offers")
async def list_offers(cells: Optional[str] = None, activity: Optional[str] = None):
    """Public, unsigned read. Returns only public fields, never contacts."""
    db.expire_stale()
    sql = "SELECT * FROM offers WHERE status='open' AND expires_at>?"
    params: list = [db.now_iso()]
    if cells:
        cell_list = [c.strip() for c in cells.split(",") if c.strip()]
        if len(cell_list) > MAX_QUERY_CELLS:
            raise HTTPException(422, f"too many cells (max {MAX_QUERY_CELLS})")
        if any(not GEOCELL_RE.match(c) for c in cell_list):
            raise HTTPException(422, "cells: geohash precision 6 required")
        if cell_list:
            sql += f" AND geocell IN ({','.join('?' * len(cell_list))})"
            params += cell_list
    if activity:
        sql += " AND activity=?"
        params.append(activity)
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = db.query(sql, tuple(params))
    return [{k: r[k] for k in OFFER_PUBLIC_FIELDS} for r in rows]


@app.get("/offers/{offer_id}")
async def get_offer(offer_id: str):
    """Public single-offer lookup so an interested agent can fetch enc_pubkey to seal to."""
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer:
        raise HTTPException(404, "offer not found")
    return {k: offer[k] for k in OFFER_PUBLIC_FIELDS}


@app.delete("/offers/{offer_id}", status_code=204)
async def withdraw_offer(offer_id: str, request: Request):
    agent_id = await verify_signed(request)
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer:
        raise HTTPException(404, "offer not found")
    if offer["agent_id"] != agent_id:
        raise HTTPException(403, "not your offer")
    db.transaction([
        ("UPDATE offers SET status='withdrawn' WHERE id=?", (offer_id,)),
        ("UPDATE interests SET status='expired' WHERE offer_id=? AND status='pending'",
         (offer_id,)),
    ])
    return Response(status_code=204)


# --- interests / handshake ------------------------------------------------

@app.post("/offers/{offer_id}/interest", status_code=201)
async def express_interest(offer_id: str, request: Request):
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "enc_pubkey", "sealed_for_owner")
    _capped(data.get("note"), 200)
    _capped(data.get("sealed_for_owner"), 4096)
    _valid_pubkey(data["enc_pubkey"], "enc_pubkey")

    db.expire_stale()
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer or offer["status"] != "open":
        raise HTTPException(404, "offer not open")
    if offer["agent_id"] == agent_id:
        raise HTTPException(400, "cannot express interest in your own offer")

    interest_id = str(uuid.uuid4())
    try:
        db.transaction([
            ("INSERT INTO interests (id, offer_id, agent_id, enc_pubkey, sealed_for_owner, "
             "note, status, created_at) VALUES (?,?,?,?,?,?, 'pending', ?)",
             (interest_id, offer_id, agent_id, data["enc_pubkey"], data["sealed_for_owner"],
              data.get("note"), db.now_iso())),
            # Notify the owner (their cron polls /inbox). No sealed payload here —
            # the owner fetches it via GET /offers/{id}/interests.
            ("INSERT INTO events (recipient, type, payload, ts) VALUES (?,?,?,?)",
             (offer["agent_id"], "new_interest", json.dumps({
                 "offer_id": offer_id, "interest_id": interest_id,
                 "activity": offer["activity"], "note": data.get("note")}),
              db.now_iso())),
        ])
    except sqlite3.IntegrityError:
        raise HTTPException(409, "already expressed interest in this offer")
    return {"interest_id": interest_id}


@app.get("/offers/{offer_id}/interests")
async def list_interests(offer_id: str, request: Request):
    agent_id = await verify_signed(request)
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer:
        raise HTTPException(404, "offer not found")
    if offer["agent_id"] != agent_id:
        raise HTTPException(403, "not your offer")
    rows = db.query(
        "SELECT id, offer_id, agent_id, enc_pubkey, sealed_for_owner, note, status, created_at "
        "FROM interests WHERE offer_id=? ORDER BY created_at",
        (offer_id,),
    )
    return [dict(r) for r in rows]


@app.post("/interests/{interest_id}/accept")
async def accept_interest(interest_id: str, request: Request):
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "sealed_for_interested")
    _capped(data.get("sealed_for_interested"), 4096)

    db.expire_stale()
    interest = db.query_one("SELECT * FROM interests WHERE id=?", (interest_id,))
    if not interest:
        raise HTTPException(404, "interest not found")
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (interest["offer_id"],))
    if not offer or offer["agent_id"] != agent_id:
        raise HTTPException(403, "not your offer")
    if offer["status"] != "open":
        raise HTTPException(409, f"offer is {offer['status']}")
    if interest["status"] != "pending":
        raise HTTPException(409, f"interest is {interest['status']}")

    # The offer stays open: it remains listed until expiry/withdrawal and further
    # interests stay acceptable (PROTOCOL §4). Status change + contact release
    # are atomic so a crash can't strand an accepted interest without its event.
    db.transaction([
        ("UPDATE interests SET status='accepted' WHERE id=?", (interest_id,)),
        ("INSERT INTO events (recipient, type, payload, ts) VALUES (?,?,?,?)",
         (interest["agent_id"], "interest_accepted", json.dumps({
             "offer_id": offer["id"], "interest_id": interest_id,
             "sealed_for_interested": data["sealed_for_interested"]}),
          db.now_iso())),
    ])
    return {"status": "accepted"}


@app.post("/interests/{interest_id}/decline")
async def decline_interest(interest_id: str, request: Request):
    agent_id = await verify_signed(request)
    db.expire_stale()
    interest = db.query_one("SELECT * FROM interests WHERE id=?", (interest_id,))
    if not interest:
        raise HTTPException(404, "interest not found")
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (interest["offer_id"],))
    if not offer or offer["agent_id"] != agent_id:
        raise HTTPException(403, "not your offer")
    if interest["status"] != "pending":
        raise HTTPException(409, f"interest is {interest['status']}")
    db.transaction([
        ("UPDATE interests SET status='declined' WHERE id=?", (interest_id,)),
        ("INSERT INTO events (recipient, type, payload, ts) VALUES (?,?,?,?)",
         (interest["agent_id"], "interest_declined", json.dumps(
             {"offer_id": offer["id"], "interest_id": interest_id}), db.now_iso())),
    ])
    return {"status": "declined"}


# --- inbox ----------------------------------------------------------------

@app.get("/inbox")
async def inbox(request: Request, after_id: int = 0):
    """Incremental fetch keyed on the autoincrement event id — timestamps can
    collide, ids cannot (the client cursors on the max id it has seen)."""
    agent_id = await verify_signed(request)
    rows = db.query(
        "SELECT id, type, payload, ts FROM events WHERE recipient=? AND id>? ORDER BY id",
        (agent_id, after_id),
    )
    events = [{"id": r["id"], "type": r["type"], "ts": r["ts"], **json.loads(r["payload"])}
              for r in rows]
    return {"events": events}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": db.now_iso()}
