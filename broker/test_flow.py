"""End-to-end protocol test: two identities, full double-opt-in handshake,
with real Ed25519 signing and X25519 sealed-box contact exchange.

Run:  python -m pytest test_flow.py -q      (or just: python test_flow.py)
Uses FastAPI's TestClient, so no running server is needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import uuid

# Use a throwaway DB so the test never touches real data.
os.environ["PINGPONG_DB"] = os.path.join(tempfile.gettempdir(), f"pingpong_test_{uuid.uuid4().hex}.db")

from fastapi.testclient import TestClient
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey

import app as broker_app


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Identity:
    def __init__(self) -> None:
        self.sk = SigningKey.generate()
        self.bk = PrivateKey.generate()
        self.agent_id = b64u(bytes(self.sk.verify_key))
        self.enc_pubkey = b64u(bytes(self.bk.public_key))

    def headers(self, method: str, path: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        nonce = b64u(os.urandom(16))
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{method}\n{path}\n{body_hash}\n{ts}\n{nonce}".encode()
        sig = self.sk.sign(canonical).signature
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
        box = SealedBox(self.bk)
        return json.loads(box.decrypt(b64u_dec(blob_b64)))


def signed_post(client, ident, path, payload):
    body = json.dumps(payload).encode()
    return client.post(path, content=body, headers=ident.headers("POST", path, body))


def signed_get(client, ident, path):
    return client.get(path, headers=ident.headers("GET", path, b""))


def test_full_handshake():
    client = TestClient(broker_app.app)
    alice = Identity()   # offerer
    bob = Identity()     # interested

    # 1. Alice publishes an offer
    offer = {
        "enc_pubkey": alice.enc_pubkey,
        "activity": "table_tennis",
        "title": "Tischtennis, locker",
        "geocell": "u33dc0",
        "earliest": "2026-06-09T18:00:00+00:00",
        "latest": "2026-06-09T22:00:00+00:00",
        "note": "Halle oder draussen",
    }
    r = signed_post(client, alice, "/offers", offer)
    assert r.status_code == 201, r.text
    offer_id = r.json()["offer_id"]

    # 2. Bob discovers it via the public board
    r = client.get("/offers", params={"cells": "u33dc0,u33dc1", "activity": "table_tennis"})
    assert r.status_code == 200
    found = [o for o in r.json() if o["id"] == offer_id]
    assert found and "note" in found[0]
    assert "sealed_for_owner" not in json.dumps(found[0])  # no contact leaks on the board

    # 3. Bob seals his contact to Alice and expresses interest
    bob_contact = {"telegram": "@bob_pong"}
    r = signed_post(client, bob, f"/offers/{offer_id}/interest", {
        "enc_pubkey": bob.enc_pubkey,
        "sealed_for_owner": bob.seal_to(alice.enc_pubkey, bob_contact),
        "note": "bin in 20 min da",
    })
    assert r.status_code == 201, r.text
    interest_id = r.json()["interest_id"]

    # 4. Alice's cron polls her inbox -> sees a new interest
    r = signed_get(client, alice, "/inbox")
    assert r.status_code == 200
    events = r.json()["events"]
    assert any(e["type"] == "new_interest" and e["interest_id"] == interest_id for e in events)

    # 5. Alice lists interests, unseals Bob's contact
    r = signed_get(client, alice, f"/offers/{offer_id}/interests")
    assert r.status_code == 200
    interest = r.json()[0]
    assert alice.unseal(interest["sealed_for_owner"]) == bob_contact

    # 6. Alice accepts, sealing her own contact to Bob
    alice_contact = {"telegram": "@alice_tt"}
    r = signed_post(client, alice, f"/interests/{interest_id}/accept", {
        "sealed_for_interested": alice.seal_to(bob.enc_pubkey, alice_contact),
    })
    assert r.status_code == 200, r.text

    # 7. Bob polls inbox -> interest_accepted, unseals Alice's contact
    r = signed_get(client, bob, "/inbox")
    accepted = [e for e in r.json()["events"] if e["type"] == "interest_accepted"]
    assert accepted, "Bob never received acceptance"
    assert bob.unseal(accepted[0]["sealed_for_interested"]) == alice_contact

    # 8. Offer is now matched
    r = client.get("/offers")
    assert offer_id not in [o["id"] for o in r.json()]  # matched offers drop off the open board

    print("OK: full handshake + sealed contact exchange verified")


def test_rejects_bad_signature():
    client = TestClient(broker_app.app)
    alice = Identity()
    body = json.dumps({"activity": "x"}).encode()
    headers = alice.headers("POST", "/offers", body)
    headers["X-Signature"] = b64u(b"\x00" * 64)  # garbage
    r = client.post("/offers", content=body, headers=headers)
    assert r.status_code == 401


def test_cannot_interest_own_offer():
    client = TestClient(broker_app.app)
    alice = Identity()
    r = signed_post(client, alice, "/offers", {
        "enc_pubkey": alice.enc_pubkey, "activity": "running",
        "geocell": "u33dc0", "earliest": "2026-06-09T18:00:00+00:00",
        "latest": "2026-06-09T20:00:00+00:00",
    })
    offer_id = r.json()["offer_id"]
    r = signed_post(client, alice, f"/offers/{offer_id}/interest", {
        "enc_pubkey": alice.enc_pubkey,
        "sealed_for_owner": alice.seal_to(alice.enc_pubkey, {"x": 1}),
    })
    assert r.status_code == 400


if __name__ == "__main__":
    test_full_handshake()
    test_rejects_bad_signature()
    test_cannot_interest_own_offer()
    print("All checks passed.")
