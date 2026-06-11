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
from fastapi.responses import RedirectResponse, Response

import crypto
import db
import moderation

MAX_TTL_HOURS = int(os.environ.get("PINGPONG_MAX_TTL_HOURS", "24"))
MAX_OPEN_OFFERS = int(os.environ.get("PINGPONG_MAX_OPEN_OFFERS", "5"))
RATE_PER_MIN = int(os.environ.get("PINGPONG_RATE_PER_MIN", "30"))
REPORT_THRESHOLD = int(os.environ.get("PINGPONG_REPORT_THRESHOLD", "3"))
REPORT_REASONS = {"illegal", "sexual", "spam", "harassment", "pii", "other"}
MAX_MATCH_MESSAGES = int(os.environ.get("PINGPONG_MAX_MATCH_MESSAGES", "100"))  # per sender per match
MAX_ACTIVITY_PROPOSALS = int(os.environ.get("PINGPONG_MAX_ACTIVITY_PROPOSALS", "10"))  # per agent
CLOCK_SKEW = 120          # seconds, §1.1
NONCE_TTL = 300           # seconds to remember a nonce for replay protection

GEOCELL_RE = re.compile(r"^[0123456789bcdefghjkmnpqrstuvwxyz]{6}$")  # geohash precision 6, §2
ACTIVITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")                  # §6 tag shape
MAX_QUERY_CELLS = 128

OFFER_PUBLIC_FIELDS = (
    "id", "agent_id", "enc_pubkey", "activity", "title",
    "geocell", "earliest", "latest", "note", "created_at", "expires_at", "status",
    "offer_sig",
)

POLICY_PATH = os.path.join(os.path.dirname(__file__), "CONTENT_POLICY.md")
BOARD_PATH = os.path.join(os.path.dirname(__file__), "board.html")

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
    try:
        if len(crypto.b64url_decode(value)) != 32:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field}: must be base64url of 32 bytes")
    return value


# Canonical strings for the data signatures (PROTOCOL §1.2). Must match the
# client implementation byte for byte.

def _canon(parts: list) -> bytes:
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()


def offer_canonical(agent_id: str, o: dict) -> bytes:
    return _canon(["pingpong-offer-v1", agent_id, o["enc_pubkey"], o["activity"],
                   o["geocell"], o["earliest"], o["latest"],
                   o.get("title") or "", o.get("note") or ""])


def interest_canonical(agent_id: str, enc_pubkey: str, offer_id: str) -> bytes:
    return _canon(["pingpong-interest-v1", agent_id, enc_pubkey, offer_id])


def _check_policy(*texts) -> None:
    category = moderation.check(*texts)
    if category:
        raise HTTPException(
            422, f"content violates policy ({category}) — see GET /policy")


def _register_activity(name: str, agent_id: str) -> bool:
    """Add a new tag to the community vocabulary (idempotent, capped per agent).
    Returns True if the tag was newly registered."""
    if db.query_one("SELECT 1 FROM activities WHERE name=?", (name,)):
        return False
    proposed = db.query_one(
        "SELECT COUNT(*) AS n FROM activities WHERE proposed_by=?", (agent_id,))["n"]
    if proposed >= MAX_ACTIVITY_PROPOSALS:
        return False
    db.execute("INSERT OR IGNORE INTO activities (name, proposed_by, created_at) "
               "VALUES (?,?,?)", (name, agent_id, db.now_iso()))
    return True


# --- offers ---------------------------------------------------------------

@app.post("/offers", status_code=201)
async def create_offer(request: Request):
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "enc_pubkey", "activity", "geocell", "earliest", "latest", "offer_sig")
    _capped(data.get("title"), 200)
    _capped(data.get("note"), 200)
    _capped(data.get("offer_sig"), 200)
    _valid_pubkey(data["enc_pubkey"], "enc_pubkey")
    if not isinstance(data["geocell"], str) or not GEOCELL_RE.match(data["geocell"]):
        raise HTTPException(422, "geocell: geohash with precision 6 required")
    if not isinstance(data["activity"], str) or not ACTIVITY_RE.match(data["activity"]):
        raise HTTPException(422, "activity: lowercase tag required (see PROTOCOL §6)")
    _check_policy(data["activity"], data.get("title"), data.get("note"))

    now = datetime.now(timezone.utc)
    earliest = _parse_ts(data["earliest"], "earliest")
    latest = _parse_ts(data["latest"], "latest")
    if latest <= earliest:
        raise HTTPException(422, "latest must be after earliest")
    if latest <= now:
        raise HTTPException(422, "time window is already in the past")
    expires = min(latest, now + timedelta(hours=MAX_TTL_HOURS))

    # Verify the author's data signature against the values we will store and
    # serve (forces canonical UTC timestamps; clients verify the same bytes).
    stored = dict(data, earliest=_iso(earliest), latest=_iso(latest))
    if not crypto.verify_raw(agent_id, data["offer_sig"],
                             offer_canonical(agent_id, stored)):
        raise HTTPException(422, "offer_sig: invalid signature over canonical offer "
                                 "(send timestamps in canonical UTC form)")

    db.expire_stale()
    if db.open_offer_count(agent_id) >= MAX_OPEN_OFFERS:
        raise HTTPException(429, f"too many open offers (max {MAX_OPEN_OFFERS})")

    offer_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO offers (id, agent_id, enc_pubkey, activity, title, geocell, "
        "earliest, latest, note, created_at, expires_at, status, offer_sig) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (offer_id, agent_id, data["enc_pubkey"], data["activity"], data.get("title"),
         data["geocell"], _iso(earliest), _iso(latest), data.get("note"),
         _iso(now), _iso(expires), data["offer_sig"]),
    )
    # Publishing with a fresh tag grows the community vocabulary (§6).
    _register_activity(data["activity"], agent_id)
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
    _require(data, "enc_pubkey", "sealed_for_owner", "interest_sig")
    _capped(data.get("note"), 200)
    _capped(data.get("sealed_for_owner"), 4096)
    _capped(data.get("interest_sig"), 200)
    _valid_pubkey(data["enc_pubkey"], "enc_pubkey")
    _check_policy(data.get("note"))
    if not crypto.verify_raw(agent_id, data["interest_sig"],
                             interest_canonical(agent_id, data["enc_pubkey"], offer_id)):
        raise HTTPException(422, "interest_sig: invalid signature")

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
             "note, status, created_at, interest_sig) VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
             (interest_id, offer_id, agent_id, data["enc_pubkey"], data["sealed_for_owner"],
              data.get("note"), db.now_iso(), data["interest_sig"])),
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
        "SELECT id, offer_id, agent_id, enc_pubkey, sealed_for_owner, note, status, "
        "created_at, interest_sig FROM interests WHERE offer_id=? ORDER BY created_at",
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


# --- match relay (PROTOCOL §4.1, since 0.4) ---------------------------------

@app.post("/matches/{interest_id}/messages", status_code=201)
async def send_match_message(interest_id: str, request: Request):
    """Relay one sealed negotiation message between the two parties of an
    accepted interest. The broker never sees plaintext — it only checks that
    the sender is one of the two parties and routes the blob to the other."""
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "sealed_payload")
    _capped(data.get("sealed_payload"), 4096)

    interest = db.query_one("SELECT * FROM interests WHERE id=?", (interest_id,))
    if not interest:
        raise HTTPException(404, "interest not found")
    if interest["status"] != "accepted":
        raise HTTPException(409, f"no match on this interest (status {interest['status']})")
    offer = db.query_one("SELECT * FROM offers WHERE id=?", (interest["offer_id"],))
    if not offer:
        raise HTTPException(404, "offer not found")
    parties = {offer["agent_id"], interest["agent_id"]}
    if agent_id not in parties:
        raise HTTPException(403, "not a party of this match")
    recipient = (parties - {agent_id}).pop()

    sent = db.query_one(
        "SELECT COUNT(*) AS n FROM match_messages WHERE interest_id=? AND sender=?",
        (interest_id, agent_id))["n"]
    if sent >= MAX_MATCH_MESSAGES:
        raise HTTPException(429, "message limit for this match reached")

    db.transaction([
        ("INSERT INTO match_messages (id, interest_id, sender, created_at) VALUES (?,?,?,?)",
         (str(uuid.uuid4()), interest_id, agent_id, db.now_iso())),
        ("INSERT INTO events (recipient, type, payload, ts) VALUES (?,?,?,?)",
         (recipient, "match_message", json.dumps({
             "interest_id": interest_id, "offer_id": offer["id"],
             "sealed_payload": data["sealed_payload"]}), db.now_iso())),
    ])
    return {"status": "sent"}


# --- moderation -------------------------------------------------------------

@app.post("/offers/{offer_id}/report", status_code=201)
async def report_offer(offer_id: str, request: Request):
    """Signed, deduplicated abuse report. REPORT_THRESHOLD distinct reporters
    remove the offer automatically (status 'removed'); the signed offer stays
    stored as evidence. See CONTENT_POLICY.md."""
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "reason")
    if data["reason"] not in REPORT_REASONS:
        raise HTTPException(422, f"reason must be one of {sorted(REPORT_REASONS)}")
    _capped(data.get("note"), 200)

    offer = db.query_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer:
        raise HTTPException(404, "offer not found")
    if offer["agent_id"] == agent_id:
        raise HTTPException(400, "cannot report your own offer")

    try:
        db.execute(
            "INSERT INTO reports (id, offer_id, reporter, reason, note, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), offer_id, agent_id, data["reason"],
             data.get("note"), db.now_iso()),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "already reported this offer")

    count = db.query_one("SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE offer_id=?",
                         (offer_id,))["n"]
    removed = False
    if count >= REPORT_THRESHOLD and offer["status"] == "open":
        db.transaction([
            ("UPDATE offers SET status='removed' WHERE id=?", (offer_id,)),
            ("UPDATE interests SET status='expired' WHERE offer_id=? AND status='pending'",
             (offer_id,)),
        ])
        removed = True
    return {"reports": count, "removed": removed}


@app.get("/activities")
async def list_activities():
    """The community-grown activity vocabulary (public). Seeded with
    table_tennis + lunch; grows when agents publish or propose new tags."""
    return [r["name"] for r in db.query("SELECT name FROM activities ORDER BY name")]


@app.post("/activities", status_code=201)
async def propose_activity(request: Request):
    """Propose a new activity tag without publishing an offer (signed)."""
    agent_id = await verify_signed(request)
    data = _json_body(request)
    _require(data, "activity")
    name = data["activity"]
    if not isinstance(name, str) or not ACTIVITY_RE.match(name):
        raise HTTPException(422, "activity: lowercase tag required (^[a-z][a-z0-9_]{0,31}$)")
    _check_policy(name)
    if _register_activity(name, agent_id):
        return {"activity": name, "new": True}
    if db.query_one("SELECT 1 FROM activities WHERE name=?", (name,)):
        return Response(json.dumps({"activity": name, "new": False}),
                        status_code=200, media_type="application/json")
    raise HTTPException(429, f"activity proposal limit reached (max {MAX_ACTIVITY_PROPOSALS})")


@app.get("/")
async def root():
    return RedirectResponse("/board")


@app.get("/board")
async def board():
    """Public read-only web view of the open board (renders /offers client-side)."""
    try:
        with open(BOARD_PATH, encoding="utf-8") as f:
            return Response(f.read(), media_type="text/html; charset=utf-8")
    except OSError:
        raise HTTPException(500, "board file missing")


@app.get("/policy")
async def policy():
    """The public content policy (also in the repo as broker/CONTENT_POLICY.md)."""
    try:
        with open(POLICY_PATH, encoding="utf-8") as f:
            return Response(f.read(), media_type="text/markdown; charset=utf-8")
    except OSError:
        raise HTTPException(500, "policy file missing")


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
