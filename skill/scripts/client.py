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

import yaml
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey

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

    def seal_to(self, enc_pubkey_b64: str, contact: dict) -> str:
        box = SealedBox(PublicKey(b64u_dec(enc_pubkey_b64)))
        return b64u(box.encrypt(json.dumps(contact).encode()))

    def unseal(self, blob_b64: str) -> dict:
        return json.loads(SealedBox(self.x_sk).decrypt(b64u_dec(blob_b64)))


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


def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        return json.load(open(SEEN_FILE))
    return {"notified_offers": [], "handled_interests": [], "inbox_cursor": None}


def save_seen(seen: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(seen, open(SEEN_FILE, "w"), indent=2)


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
