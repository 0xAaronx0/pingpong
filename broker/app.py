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
import time
import uuid
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import crypto
import db

MAX_TTL_HOURS = int(os.environ.get("PINGPONG_MAX_TTL_HOURS", "24"))
MAX_OPEN_OFFERS = int(os.environ.get("PINGPONG_MAX_OPEN_OFFERS", "5"))
RATE_PER_MIN = int(os.environ.get("PINGPONG_RATE_PER_MIN", "30"))
CLOCK_SKEW = 120          # seconds, §1.1
NONCE_TTL = 300           # seconds to remember a nonce for replay protection

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


def _capped(s: str | None, n: int) -> str | None:
    if s is not None and len(s) > n:
        raise HTTPException(422, f"field too long (max {n})")
    return s


# --- offers ---------------------------------------------------------------

@app.post("/offers", status_code=201)
async def create_offer(request: Request):
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "enc_pubkey", "activity", "geocell", "earliest", "latest")
    _capped(data.get("title"), 200)
    _capped(data.get("note"), 200)

    db.expire_stale()
    if db.open_offer_count(agent_id) >= MAX_OPEN_OFFERS:
        raise HTTPException(429, f"too many open offers (max {MAX_OPEN_OFFERS})")

    created = db.now_iso()
    ttl_cap = _iso_plus_hours(created, MAX_TTL_HOURS)
    expires = min(data["latest"], ttl_cap)
    offer_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO offers (id, agent_id, enc_pubkey, activity, title, geocell, "
        "earliest, latest, note, created_at, expires_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')",
        (offer_id, agent_id, data["enc_pubkey"], data["activity"], data.get("title"),
         data["geocell"], data["earliest"], data["latest"], data.get("note"),
         created, expires),
    )
    return {"offer_id": offer_id, "expires_at": expires}


@app.get("/offers")
async def list_offers(cells: Optional[str] = None, activity: Optional[str] = None):
    """Public, unsigned read. Returns only public fields, never contacts."""
    db.expire_stale()
    sql = "SELECT * FROM offers WHERE status='open' AND expires_at>?"
    params: list = [db.now_iso()]
    if cells:
        cell_list = [c.strip() for c in cells.split(",") if c.strip()]
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
    db.execute("UPDATE offers SET status='withdrawn' WHERE id=?", (offer_id,))
    db.execute(
        "UPDATE interests SET status='expired' WHERE offer_id=? AND status='pending'",
        (offer_id,),
    )
    return JSONResponse(status_code=204, content=None)


# --- interests / handshake ------------------------------------------------

@app.post("/offers/{offer_id}/interest", status_code=201)
async def express_interest(offer_id: str, request: Request):
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "enc_pubkey", "sealed_for_owner")
    _capped(data.get("note"), 200)
    _capped(data.get("sealed_for_owner"), 4096)

    db.expire_stale()
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer or offer["status"] != "open":
        raise HTTPException(404, "offer not open")
    if offer["agent_id"] == agent_id:
        raise HTTPException(400, "cannot express interest in your own offer")

    interest_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO interests (id, offer_id, agent_id, enc_pubkey, sealed_for_owner, "
        "note, status, created_at) VALUES (?,?,?,?,?,?, 'pending', ?)",
        (interest_id, offer_id, agent_id, data["enc_pubkey"], data["sealed_for_owner"],
         data.get("note"), db.now_iso()),
    )
    # Notify the owner (their cron polls /inbox). No sealed payload here — the
    # owner fetches it via GET /offers/{id}/interests.
    db.push_event(offer["agent_id"], "new_interest", {
        "offer_id": offer_id, "interest_id": interest_id,
        "activity": offer["activity"], "note": data.get("note"),
    })
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

    interest = db.query_one("SELECT * FROM interests WHERE id=?", (interest_id,))
    if not interest:
        raise HTTPException(404, "interest not found")
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (interest["offer_id"],))
    if not offer or offer["agent_id"] != agent_id:
        raise HTTPException(403, "not your offer")
    if interest["status"] != "pending":
        raise HTTPException(409, f"interest is {interest['status']}")

    db.execute("UPDATE interests SET status='accepted' WHERE id=?", (interest_id,))
    db.execute("UPDATE offers SET status='matched' WHERE id=?", (offer["id"],))
    # Release the owner's sealed contact to the interested party via their inbox.
    db.push_event(interest["agent_id"], "interest_accepted", {
        "offer_id": offer["id"], "interest_id": interest_id,
        "sealed_for_interested": data["sealed_for_interested"],
    })
    return {"status": "accepted"}


@app.post("/interests/{interest_id}/decline")
async def decline_interest(interest_id: str, request: Request):
    agent_id = await verify_signed(request)
    interest = db.query_one("SELECT * FROM interests WHERE id=?", (interest_id,))
    if not interest:
        raise HTTPException(404, "interest not found")
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (interest["offer_id"],))
    if not offer or offer["agent_id"] != agent_id:
        raise HTTPException(403, "not your offer")
    if interest["status"] != "pending":
        raise HTTPException(409, f"interest is {interest['status']}")
    db.execute("UPDATE interests SET status='declined' WHERE id=?", (interest_id,))
    db.push_event(interest["agent_id"], "interest_declined",
                  {"offer_id": offer["id"], "interest_id": interest_id})
    return {"status": "declined"}


# --- inbox ----------------------------------------------------------------

@app.get("/inbox")
async def inbox(request: Request, since: Optional[str] = None):
    agent_id = await verify_signed(request)
    if since:
        rows = db.query(
            "SELECT id, type, payload, ts FROM events WHERE recipient=? AND ts>? ORDER BY ts",
            (agent_id, since),
        )
    else:
        rows = db.query(
            "SELECT id, type, payload, ts FROM events WHERE recipient=? ORDER BY ts",
            (agent_id,),
        )
    events = [{"id": r["id"], "type": r["type"], "ts": r["ts"], **json.loads(r["payload"])}
              for r in rows]
    return {"events": events}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": db.now_iso()}


# --- util -----------------------------------------------------------------

def _iso_plus_hours(iso_ts: str, hours: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(iso_ts) + timedelta(hours=hours)).isoformat()
