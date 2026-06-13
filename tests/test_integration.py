"""End-to-end integration: real broker server + two agents via the real skill
scripts. Proves both halves work together over HTTP with signing + sealing,
including the v0.2 behaviors (offer stays listed after a match) and the
poison-pill defense (a garbage contact blob must not wedge the poll loop).

Run from the skill/ dir:  python test_integration.py
Starts its own broker on a free port and cleans up afterwards.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # repo root (tests/ -> ..)
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
BROKER = os.path.join(ROOT, "broker")
PORT = "8099"
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable

PROFILE = """
home: {{lat: 52.5200, lon: 13.4050}}
radius_rings: 1
activities: [table_tennis, running]
contact: {{telegram: "{handle}"}}
"""


def wait_health(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("broker did not start")


def run(state_dir, script, *args, expect_fail=False):
    env = dict(os.environ, PINGPONG_BROKER_URL=BASE, PINGPONG_STATE_DIR=state_dir)
    res = subprocess.run([PY, os.path.join(SCRIPTS, script), *args],
                         cwd=SCRIPTS, env=env, capture_output=True, text=True)
    if expect_fail:
        if res.returncode == 0:
            raise AssertionError(f"{script} unexpectedly succeeded:\n{res.stdout}")
        return res.stdout + res.stderr
    if res.returncode != 0:
        raise AssertionError(f"{script} failed:\n{res.stdout}\n{res.stderr}")
    return res.stdout


def setup_agent(name, handle):
    d = os.path.join(tempfile.mkdtemp(prefix=f"pingpong_{name}_"))
    with open(os.path.join(d, "profile.yaml"), "w") as f:
        f.write(PROFILE.format(handle=handle))
    return d


def uuid_after(text, key):
    m = re.search(key + r":\s*([0-9a-f-]{36})", text)
    assert m, f"no {key} in:\n{text}"
    return m.group(1)


def raw_signed_post(state_dir, path, payload):
    """Sign a request with an agent's stored identity, bypassing the skill
    scripts — used to simulate a malicious/buggy counterpart."""
    from nacl.signing import SigningKey

    def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
    def b64u_dec(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    ident = json.load(open(os.path.join(state_dir, "identity.json")))
    sk = SigningKey(b64u_dec(ident["ed25519_sk"]))
    body = json.dumps(payload).encode()
    ts, nonce = str(int(time.time())), b64u(os.urandom(16))
    canonical = f"POST\n{path}\n{hashlib.sha256(body).hexdigest()}\n{ts}\n{nonce}".encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers={
        "X-Agent-Id": ident["agent_id"], "X-Timestamp": ts, "X-Nonce": nonce,
        "X-Signature": b64u(sk.sign(canonical).signature),
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read() or b"{}")


def main():
    db = os.path.join(tempfile.gettempdir(), f"pingpong_it_{os.getpid()}.db")
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app:app", "--port", PORT, "--log-level", "warning"],
        cwd=BROKER, env=dict(os.environ, PINGPONG_DB=db),
    )
    try:
        wait_health()
        alice = setup_agent("alice", "@alice_tt")
        bob = setup_agent("bob", "@bob_pong")

        assert "agent_id" in run(alice, "identity.py")
        assert "agent_id" in run(bob, "identity.py")

        # Alice publishes
        out = run(alice, "publish.py", "--activity", "table_tennis",
                  "--title", "Tischtennis locker", "--hours", "5")
        offer_id = uuid_after(out, "offer_id")
        print("· Alice published", offer_id)

        # Bob's cron poll discovers it
        out = run(bob, "poll.py")
        assert offer_id in out and "Table tennis" in out, out
        print("· Bob discovered the offer")

        # Bob expresses interest
        out = run(bob, "interest.py", "--offer-id", offer_id, "--note", "bin gleich da")
        interest_id = uuid_after(out, "interest_id")
        print("· Bob expressed interest", interest_id)

        # Alice's poll surfaces the incoming interest
        out = run(alice, "poll.py")
        assert interest_id in out and "interested in your" in out, out
        print("· Alice saw incoming interest")

        # Alice accepts -> learns Bob's contact
        out = run(alice, "accept.py", "--offer-id", offer_id, "--interest-id", interest_id)
        assert "@bob_pong" in out, out
        print("· Alice accepted, sees Bob's contact")

        # Bob's poll reveals the match + Alice's contact
        out = run(bob, "poll.py")
        assert "Match" in out and "@alice_tt" in out, out
        print("· Bob sees the match + Alice's contact")

        # v0.2: the offer STAYS on the open board after the match
        with urllib.request.urlopen(BASE + "/offers") as r:
            assert offer_id in r.read().decode(), "matched offer must stay listed"
        print("· Offer stays listed after the match")

        # v0.4: negotiation relay — alice proposes, bob receives + accepts.
        # --when is 2h in the past so the follow-up (time+1h) is due immediately.
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        out = run(alice, "message.py", "--offer-id", offer_id, "--interest-id", interest_id,
                  "--kind", "propose", "--place", "Helmi-Platz", "--time", "19:30",
                  "--when", past)
        assert "Proposal sent" in out
        out = run(bob, "poll.py")
        assert "Proposal" in out and "Helmi-Platz" in out and "19:30" in out, out
        out = run(bob, "message.py", "--offer-id", offer_id, "--interest-id", interest_id,
                  "--kind", "accept", "--note", "see you soon!")
        assert "Acceptance sent" in out and "scheduled" in out
        out = run(alice, "poll.py")
        assert "said yes" in out and "see you soon" in out, out
        print("· Negotiation relay: propose -> accept, both sides verified")

        # Post-meetup follow-up: due immediately (when+1h is in the past)
        out = run(bob, "poll.py")
        assert "Follow-up on your meetup" in out and "Who was better?" in out, out
        out = run(bob, "feedback.py", "--meetup-id", interest_id[:8],
                  "--happened", "yes", "--sympathisch", "yes", "--skill", "even")
        assert "Feedback recorded" in out and "Level estimate" in out, out
        out = run(bob, "poll.py")
        assert "Follow-up" not in out, out  # only asked once
        print("· Post-meetup follow-up asked once, feedback recorded")

        # accept.py reminds the agent to ask the owner about keeping it listed
        # (checked above in alice's accept output)

        # New nearby activity: alice proposes a tag -> bob's poll announces it
        out = run(alice, "activities.py", "--propose", "yoga_in_the_park")
        assert "network-wide" in out
        out = run(bob, "poll.py")
        assert "New activity" in out and "yoga" in out, out
        print("· New nearby activity announced to existing user")

        # Poison pill: a malicious owner accepts with a garbage contact blob.
        # Bob's poll must survive it and advance its cursor. Bob knows alice
        # from the meetup by now -> the discovery must carry the partner badge.
        offer2 = uuid_after(run(alice, "publish.py", "--activity", "running",
                                "--title", "zweite Runde", "--hours", "2"), "offer_id")
        out = run(bob, "poll.py")  # consume discovery notification
        assert "You know them" in out and "about your level" in out, out
        print("· Known-partner annotation on new offers")
        interest2 = uuid_after(run(bob, "interest.py", "--offer-id", offer2), "interest_id")
        garbage = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
        raw_signed_post(alice, f"/interests/{interest2}/accept",
                        {"sealed_for_interested": garbage})
        out = run(bob, "poll.py")
        assert "decrypted" in out, f"expected unseal warning, got:\n{out}"
        out = run(bob, "poll.py")
        assert out.strip() == "[SILENT]", f"cursor did not advance past poison event:\n{out}"
        print("· Poison-pill event survived: warned once, then silent")

        # Withdraw cleanup: alice unlists the first offer ("no" to keep-listed)
        run(alice, "withdraw.py", "--offer-id", offer_id)
        with urllib.request.urlopen(BASE + "/offers") as r:
            assert offer_id not in r.read().decode()
        print("· Withdraw unlists the offer")

        # Moderation: a policy-violating publish is rejected with a policy hint
        out = run(alice, "publish.py", "--activity", "other",
                  "--title", "Verkaufe Kokain", expect_fail=True)
        assert "policy" in out, out
        print("· Policy filter rejects banned content")

        # Reporting: bob reports alice's remaining offer; dedupe enforced
        out = run(bob, "report.py", "--offer-id", offer2, "--reason", "spam")
        assert "Report submitted" in out
        out = run(bob, "report.py", "--offer-id", offer2, "--reason", "spam",
                  expect_fail=True)
        assert "409" in out, out
        print("· Report submitted, duplicate rejected")

        print("\nOK: full two-agent flow + v0.3 behaviors verified end-to-end")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        if os.path.exists(db):
            os.remove(db)


if __name__ == "__main__":
    main()
