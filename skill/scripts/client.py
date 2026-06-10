"""Shared client for the pingpong skill: identity, signing, broker calls, sealing.

State lives in PINGPONG_STATE_DIR (default ~/.pingpong):
  identity.json   Ed25519 + X25519 private keys (chmod 600)  -- secret
  profile.yaml    where/what you search + your contact        -- you edit this
  seen.json       dedup of notified offers + inbox cursor      -- managed

Broker base URL: env PINGPONG_BROKER_URL, else `broker_url:` in <state>/config.yaml.
Crypto and the request-signature scheme follow docs/PROTOCOL.md §1.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

STATE_DIR = os.path.expanduser(os.environ.get("PINGPONG_STATE_DIR", "~/.pingpong"))
IDENTITY_FILE = os.path.join(STATE_DIR, "identity.json")
PROFILE_FILE = os.path.join(STATE_DIR, "profile.yaml")
CONFIG_FILE = os.path.join(STATE_DIR, "config.yaml")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")


# --- base64url ------------------------------------------------------------

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- data signatures (PROTOCOL §1.2) ---------------------------------------
# Offers, interests and contact payloads are signed by their author so a
# tampering broker cannot swap keys or alter fields unnoticed. Canonical form
# is a compact JSON array — both halves must build it identically.

def canon_ts(value: str) -> str:
    """Normalize a client timestamp to the protocol's canonical UTC form.
    Must mirror the broker's normalization exactly, or signatures break."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if dt.tzinfo is None:
        raise SystemExit(f"timestamp needs a timezone: {value}")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _canon(parts: list) -> bytes:
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()


def offer_canonical(o: dict) -> bytes:
    return _canon(["pingpong-offer-v1", o["agent_id"], o["enc_pubkey"], o["activity"],
                   o["geocell"], o["earliest"], o["latest"],
                   o.get("title") or "", o.get("note") or ""])


def interest_canonical(agent_id: str, enc_pubkey: str, offer_id: str) -> bytes:
    return _canon(["pingpong-interest-v1", agent_id, enc_pubkey, offer_id])


def contact_canonical(from_id: str, recipient_enc_pubkey: str, offer_id: str,
                      contact: dict) -> bytes:
    contact_json = json.dumps(contact, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True)
    return _canon(["pingpong-contact-v1", from_id, recipient_enc_pubkey,
                   offer_id, contact_json])


def message_canonical(from_id: str, recipient_enc_pubkey: str, interest_id: str,
                      body: dict) -> bytes:
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return _canon(["pingpong-msg-v1", from_id, recipient_enc_pubkey,
                   interest_id, body_json])


def verify_sig(agent_id: str, sig_b64: str, canonical: bytes) -> bool:
    try:
        VerifyKey(b64u_dec(agent_id)).verify(canonical, b64u_dec(sig_b64))
        return True
    except Exception:
        return False


def verify_offer(o: dict) -> bool:
    return bool(o.get("offer_sig")) and verify_sig(o["agent_id"], o["offer_sig"],
                                                   offer_canonical(o))


def verify_interest(i: dict, offer_id: str) -> bool:
    return bool(i.get("interest_sig")) and verify_sig(
        i["agent_id"], i["interest_sig"],
        interest_canonical(i["agent_id"], i["enc_pubkey"], offer_id))


def fingerprint(agent_id: str) -> str:
    """Short human-comparable key fingerprint. Both people should compare these
    in their first direct chat — that's what catches a fully-MITMing broker."""
    h = hashlib.sha256(agent_id.encode()).hexdigest()[:12]
    return "-".join(h[i:i + 4] for i in (0, 4, 8))


# --- identity -------------------------------------------------------------

class Identity:
    def __init__(self, ed_sk: SigningKey, x_sk: PrivateKey) -> None:
        self.ed_sk = ed_sk
        self.x_sk = x_sk
        self.agent_id = b64u(bytes(ed_sk.verify_key))
        self.enc_pubkey = b64u(bytes(x_sk.public_key))

    @classmethod
    def load_or_create(cls) -> "Identity":
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(IDENTITY_FILE):
            data = json.load(open(IDENTITY_FILE))
            return cls(SigningKey(b64u_dec(data["ed25519_sk"])),
                       PrivateKey(b64u_dec(data["x25519_sk"])))
        ed_sk, x_sk = SigningKey.generate(), PrivateKey.generate()
        ident = cls(ed_sk, x_sk)
        with open(IDENTITY_FILE, "w") as f:
            json.dump({
                "ed25519_sk": b64u(bytes(ed_sk)),
                "x25519_sk": b64u(bytes(x_sk)),
                "agent_id": ident.agent_id,
                "enc_pubkey": ident.enc_pubkey,
            }, f, indent=2)
        os.chmod(IDENTITY_FILE, 0o600)
        return ident

    def sign_headers(self, method: str, path: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        nonce = b64u(os.urandom(16))
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{method}\n{path}\n{body_hash}\n{ts}\n{nonce}".encode()
        sig = self.ed_sk.sign(canonical).signature
        return {
            "X-Agent-Id": self.agent_id,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": b64u(sig),
            "Content-Type": "application/json",
        }

    def sign_blob(self, canonical: bytes) -> str:
        return b64u(self.ed_sk.sign(canonical).signature)

    def seal_contact(self, recipient_enc_pubkey: str, offer_id: str, contact: dict) -> str:
        """Seal a contact to the recipient's X25519 key, with an inner Ed25519
        signature binding it to us, the recipient key and the offer — so a
        broker can neither forge contacts nor swap them between matches."""
        payload = {
            "v": "pingpong-contact-v1",
            "from": self.agent_id,
            "offer_id": offer_id,
            "contact": contact,
            "sig": self.sign_blob(contact_canonical(self.agent_id, recipient_enc_pubkey,
                                                    offer_id, contact)),
        }
        box = SealedBox(PublicKey(b64u_dec(recipient_enc_pubkey)))
        return b64u(box.encrypt(json.dumps(payload).encode()))

    def seal_message(self, recipient_enc_pubkey: str, interest_id: str, body: dict) -> str:
        """Seal a negotiation message (PROTOCOL §4.1) to the counterpart,
        signed and bound to this match."""
        payload = {
            "v": "pingpong-msg-v1",
            "from": self.agent_id,
            "interest_id": interest_id,
            "body": body,
            "sig": self.sign_blob(message_canonical(self.agent_id, recipient_enc_pubkey,
                                                    interest_id, body)),
        }
        box = SealedBox(PublicKey(b64u_dec(recipient_enc_pubkey)))
        return b64u(box.encrypt(json.dumps(payload).encode()))

    def unseal_message(self, blob_b64: str, expected_from: str, interest_id: str) -> dict:
        payload = json.loads(SealedBox(self.x_sk).decrypt(b64u_dec(blob_b64)))
        frm, body = payload.get("from"), payload.get("body")
        if (frm != expected_from or payload.get("interest_id") != interest_id
                or not isinstance(body, dict)
                or not verify_sig(frm, payload.get("sig", ""),
                                  message_canonical(frm, self.enc_pubkey, interest_id, body))):
            raise ValueError("message payload failed verification")
        return body

    def unseal_contact(self, blob_b64: str, expected_from: str, offer_id: str) -> dict:
        """Unseal and verify a contact payload. Raises ValueError on any
        mismatch (wrong sender, wrong offer, bad signature)."""
        payload = json.loads(SealedBox(self.x_sk).decrypt(b64u_dec(blob_b64)))
        frm, contact = payload.get("from"), payload.get("contact")
        if (frm != expected_from or payload.get("offer_id") != offer_id
                or not isinstance(contact, dict)
                or not verify_sig(frm, payload.get("sig", ""),
                                  contact_canonical(frm, self.enc_pubkey, offer_id, contact))):
            raise ValueError("contact payload failed verification")
        return contact


# --- config / profile / seen ---------------------------------------------

def broker_url() -> str:
    url = os.environ.get("PINGPONG_BROKER_URL")
    if not url and os.path.exists(CONFIG_FILE):
        url = (yaml.safe_load(open(CONFIG_FILE)) or {}).get("broker_url")
    if not url:
        raise SystemExit("No broker URL: set PINGPONG_BROKER_URL or broker_url in config.yaml")
    return url.rstrip("/")


def load_profile() -> dict:
    if not os.path.exists(PROFILE_FILE):
        raise SystemExit(f"No profile at {PROFILE_FILE}. Copy profile.example.yaml and edit it.")
    return yaml.safe_load(open(PROFILE_FILE)) or {}


_SEEN_DEFAULTS = {"notified_offers": [], "inbox_after_id": 0}


def load_seen() -> dict:
    seen = dict(_SEEN_DEFAULTS)
    if os.path.exists(SEEN_FILE):
        try:
            data = json.load(open(SEEN_FILE))
            if isinstance(data, dict):
                seen.update(data)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt state file: start fresh rather than killing every poll
    return seen


def save_seen(seen: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f, indent=2)
    os.replace(tmp, SEEN_FILE)  # atomic: a crash mid-write can't truncate the file


# --- broker HTTP ----------------------------------------------------------

class BrokerError(RuntimeError):
    pass


def _request(method: str, path: str, *, ident: Identity | None = None,
             body: dict | None = None, params: dict | None = None):
    url = broker_url() + path
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url += "?" + qs
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json"}
    if ident is not None:
        # Signature covers the path only (not the query string) — see PROTOCOL §1.1.
        headers = ident.sign_headers(method, path, raw)
    req = urllib.request.Request(url, data=raw if raw else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BrokerError(f"{method} {path} -> {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise BrokerError(f"{method} {path} -> connection error: {e.reason}") from None


def get(path, *, ident=None, params=None):
    return _request("GET", path, ident=ident, params=params)


def post(path, body, *, ident):
    return _request("POST", path, ident=ident, body=body)


def delete(path, *, ident):
    return _request("DELETE", path, ident=ident)
