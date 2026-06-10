"""Protocol tests: full double-opt-in handshake plus regressions for the v0.2
review fixes (multi-accept survives sweeps, offers stay listed after a match,
timestamp normalization, interest dedupe, geocell validation, expiry sweep).

Run:  python test_flow.py   (or pytest). Uses TestClient; no server needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

# Use a throwaway DB so the tests never touch real data.
os.environ["PINGPONG_DB"] = os.path.join(tempfile.gettempdir(), f"pingpong_test_{uuid.uuid4().hex}.db")

from fastapi.testclient import TestClient
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey

import app as broker_app


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def utc_in(hours: float = 0, seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours, seconds=seconds)).isoformat()


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
        return json.loads(SealedBox(self.bk).decrypt(b64u_dec(blob_b64)))


def signed_post(client, ident, path, payload):
    body = json.dumps(payload).encode()
    return client.post(path, content=body, headers=ident.headers("POST", path, body))


def signed_get(client, ident, path, params=None):
    qs = ""
    if params:
        from urllib.parse import urlencode
        qs = "?" + urlencode(params)
    return client.get(path + qs, headers=ident.headers("GET", path, b""))


def make_offer(client, ident, activity="table_tennis", **overrides):
    body = {
        "enc_pubkey": ident.enc_pubkey,
        "activity": activity,
        "geocell": "u33dc0",
        "earliest": utc_in(0),
        "latest": utc_in(hours=4),
    }
    body.update(overrides)
    return signed_post(client, ident, "/offers", body)


def test_full_handshake_offer_stays_listed():
    """Happy path + v0.2 behavior: offer stays on the board after a match and
    a second interested party can still be accepted (multi-accept)."""
    client = TestClient(broker_app.app)
    alice, bob, charlie = Identity(), Identity(), Identity()

    offer_id = make_offer(client, alice, title="Tischtennis, locker").json()["offer_id"]

    # discovery on the public board, no contact leak
    r = client.get("/offers", params={"cells": "u33dc0,u33dc1", "activity": "table_tennis"})
    found = [o for o in r.json() if o["id"] == offer_id]
    assert found and "sealed" not in json.dumps(found[0])

    # bob expresses interest, alice sees + unseals + accepts
    bob_contact = {"telegram": "@bob_pong"}
    r = signed_post(client, bob, f"/offers/{offer_id}/interest", {
        "enc_pubkey": bob.enc_pubkey,
        "sealed_for_owner": bob.seal_to(alice.enc_pubkey, bob_contact),
    })
    interest_id = r.json()["interest_id"]
    interests = signed_get(client, alice, f"/offers/{offer_id}/interests").json()
    assert alice.unseal(interests[0]["sealed_for_owner"]) == bob_contact
    r = signed_post(client, alice, f"/interests/{interest_id}/accept", {
        "sealed_for_interested": alice.seal_to(bob.enc_pubkey, {"telegram": "@alice_tt"}),
    })
    assert r.status_code == 200, r.text

    # bob learns alice's contact via inbox
    events = signed_get(client, bob, "/inbox", {"after_id": 0}).json()["events"]
    accepted = [e for e in events if e["type"] == "interest_accepted"]
    assert bob.unseal(accepted[0]["sealed_for_interested"]) == {"telegram": "@alice_tt"}

    # v0.2: the offer is STILL listed after the match...
    r = client.get("/offers", params={"cells": "u33dc0"})
    assert offer_id in [o["id"] for o in r.json()], "matched offer must stay on the board"

    # ...and charlie can still express interest and be accepted, even though
    # sweeps ran in between (regression: sweep used to expire pending interests
    # on matched offers).
    r = signed_post(client, charlie, f"/offers/{offer_id}/interest", {
        "enc_pubkey": charlie.enc_pubkey,
        "sealed_for_owner": charlie.seal_to(alice.enc_pubkey, {"telegram": "@charlie"}),
    })
    assert r.status_code == 201, r.text
    charlie_interest = r.json()["interest_id"]
    client.get("/offers")  # extra sweep trigger, must not kill charlie's pending interest
    r = signed_post(client, alice, f"/interests/{charlie_interest}/accept", {
        "sealed_for_interested": alice.seal_to(charlie.enc_pubkey, {"telegram": "@alice_tt"}),
    })
    assert r.status_code == 200, f"multi-accept broken: {r.text}"


def test_duplicate_interest_rejected():
    client = TestClient(broker_app.app)
    alice, bob = Identity(), Identity()
    offer_id = make_offer(client, alice, activity="running").json()["offer_id"]
    payload = {
        "enc_pubkey": bob.enc_pubkey,
        "sealed_for_owner": bob.seal_to(alice.enc_pubkey, {"t": "@b"}),
    }
    assert signed_post(client, bob, f"/offers/{offer_id}/interest", payload).status_code == 201
    r = signed_post(client, bob, f"/offers/{offer_id}/interest", payload)
    assert r.status_code == 409, f"duplicate interest must 409, got {r.status_code}"
    # exactly one inbox event for the owner
    events = signed_get(client, alice, "/inbox", {"after_id": 0}).json()["events"]
    assert len([e for e in events if e["type"] == "new_interest"
                and e["offer_id"] == offer_id]) == 1


def test_timestamp_validation_and_normalization():
    client = TestClient(broker_app.app)
    alice = Identity()

    # rejected: naive datetime, garbage, inverted window, past window
    for bad in (
        {"latest": "2026-06-10T22:00:00"},                       # no timezone
        {"latest": "not-a-date"},
        {"earliest": utc_in(hours=3), "latest": utc_in(hours=1)},  # inverted
        {"earliest": utc_in(hours=-5), "latest": utc_in(hours=-1)},  # already past
    ):
        r = make_offer(client, alice, **bad)
        assert r.status_code == 422, f"{bad} should 422, got {r.status_code}: {r.text}"

    # +02:00 input is normalized to UTC on storage
    tz2 = timezone(timedelta(hours=2))
    latest_plus2 = (datetime.now(tz2) + timedelta(hours=3)).isoformat()
    r = make_offer(client, alice, latest=latest_plus2)
    assert r.status_code == 201, r.text
    offer = client.get(f"/offers/{r.json()['offer_id']}").json()
    assert offer["latest"].endswith("+00:00") and offer["earliest"].endswith("+00:00")
    # and the offer is visible (regression: string-compared expiry)
    assert offer["status"] == "open"

    # TTL cap is computed temporally, not lexically: 48h window capped at ~24h
    r = make_offer(client, alice, latest=utc_in(hours=48))
    expires = datetime.fromisoformat(r.json()["expires_at"])
    delta_h = (expires - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23.5 < delta_h < 24.5, f"TTL cap broken: {delta_h}h"


def test_field_validation():
    client = TestClient(broker_app.app)
    alice = Identity()
    assert make_offer(client, alice, geocell="u33dc").status_code == 422      # precision 5
    assert make_offer(client, alice, geocell="U33DC0").status_code == 422     # uppercase
    assert make_offer(client, alice, activity="Tischtennis!").status_code == 422
    assert make_offer(client, alice, enc_pubkey="dG9vc2hvcnQ").status_code == 422  # not 32 bytes
    r = client.get("/offers", params={"cells": "u33dc0,INVALID"})
    assert r.status_code == 422


def test_expiry_sweep_and_late_accept():
    client = TestClient(broker_app.app)
    alice, bob = Identity(), Identity()
    r = make_offer(client, alice, activity="walk", latest=utc_in(seconds=1.2))
    offer_id = r.json()["offer_id"]
    interest_id = signed_post(client, bob, f"/offers/{offer_id}/interest", {
        "enc_pubkey": bob.enc_pubkey,
        "sealed_for_owner": bob.seal_to(alice.enc_pubkey, {"t": "@b"}),
    }).json()["interest_id"]

    time.sleep(1.4)  # let the offer expire
    r = client.get("/offers", params={"cells": "u33dc0"})
    assert offer_id not in [o["id"] for o in r.json()], "expired offer still listed"

    # late accept must be rejected deterministically (sweep runs inside accept)
    r = signed_post(client, alice, f"/interests/{interest_id}/accept", {
        "sealed_for_interested": alice.seal_to(bob.enc_pubkey, {"t": "@a"}),
    })
    assert r.status_code == 409, f"late accept must 409, got {r.status_code}"


def test_withdraw_expires_pending():
    client = TestClient(broker_app.app)
    alice, bob = Identity(), Identity()
    offer_id = make_offer(client, alice, activity="coffee").json()["offer_id"]
    interest_id = signed_post(client, bob, f"/offers/{offer_id}/interest", {
        "enc_pubkey": bob.enc_pubkey,
        "sealed_for_owner": bob.seal_to(alice.enc_pubkey, {"t": "@b"}),
    }).json()["interest_id"]

    r = client.request("DELETE", f"/offers/{offer_id}",
                       headers=alice.headers("DELETE", f"/offers/{offer_id}", b""))
    assert r.status_code == 204
    # accept after withdraw must fail; new interest must fail
    r = signed_post(client, alice, f"/interests/{interest_id}/accept", {
        "sealed_for_interested": alice.seal_to(bob.enc_pubkey, {"t": "@a"}),
    })
    assert r.status_code == 409
    r = signed_post(client, Identity(), f"/offers/{offer_id}/interest", {
        "enc_pubkey": bob.enc_pubkey, "sealed_for_owner": "eA",
    })
    assert r.status_code == 404


def test_inbox_cursor_incremental():
    client = TestClient(broker_app.app)
    alice, bob = Identity(), Identity()
    offer_id = make_offer(client, alice, activity="beer").json()["offer_id"]
    signed_post(client, bob, f"/offers/{offer_id}/interest", {
        "enc_pubkey": bob.enc_pubkey,
        "sealed_for_owner": bob.seal_to(alice.enc_pubkey, {"t": "@b"}),
    })
    events = signed_get(client, alice, "/inbox", {"after_id": 0}).json()["events"]
    assert events
    max_id = max(e["id"] for e in events)
    again = signed_get(client, alice, "/inbox", {"after_id": max_id}).json()["events"]
    assert again == [], "cursor must make the fetch incremental"


def test_rejects_bad_signature():
    client = TestClient(broker_app.app)
    alice = Identity()
    body = json.dumps({"activity": "x"}).encode()
    headers = alice.headers("POST", "/offers", body)
    headers["X-Signature"] = b64u(b"\x00" * 64)
    assert client.post("/offers", content=body, headers=headers).status_code == 401


def test_cannot_interest_own_offer():
    client = TestClient(broker_app.app)
    alice = Identity()
    offer_id = make_offer(client, alice, activity="cycling").json()["offer_id"]
    r = signed_post(client, alice, f"/offers/{offer_id}/interest", {
        "enc_pubkey": alice.enc_pubkey,
        "sealed_for_owner": alice.seal_to(alice.enc_pubkey, {"x": 1}),
    })
    assert r.status_code == 400


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\nAll {len(tests)} checks passed.")
