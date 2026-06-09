"""End-to-end integration: real broker server + two agents via the real skill
scripts. Proves both halves work together over HTTP with signing + sealing.

Run from the skill/ dir:  python test_integration.py
Starts its own broker on a free port and cleans up afterwards.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")
BROKER = os.path.abspath(os.path.join(HERE, "..", "broker"))
PORT = "8099"
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable

PROFILE = """
home: {{lat: 52.5200, lon: 13.4050}}
geohash_precision: 6
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


def run(state_dir, script, *args):
    env = dict(os.environ, PINGPONG_BROKER_URL=BASE, PINGPONG_STATE_DIR=state_dir)
    res = subprocess.run([PY, os.path.join(SCRIPTS, script), *args],
                         cwd=SCRIPTS, env=env, capture_output=True, text=True)
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
        assert offer_id in out and "Tischtennis" in out, out
        print("· Bob discovered the offer")

        # Bob expresses interest
        out = run(bob, "interest.py", "--offer-id", offer_id, "--note", "bin gleich da")
        interest_id = uuid_after(out, "interest_id")
        print("· Bob expressed interest", interest_id)

        # Alice's poll surfaces the incoming interest
        out = run(alice, "poll.py")
        assert interest_id in out and "Interesse" in out, out
        print("· Alice saw incoming interest")

        # Alice accepts -> learns Bob's contact
        out = run(alice, "accept.py", "--offer-id", offer_id, "--interest-id", interest_id)
        assert "@bob_pong" in out, out
        print("· Alice accepted, sees Bob's contact")

        # Bob's poll reveals the match + Alice's contact
        out = run(bob, "poll.py")
        assert "Match" in out and "@alice_tt" in out, out
        print("· Bob sees the match + Alice's contact")

        # Offer is gone from the open board
        with urllib.request.urlopen(BASE + "/offers") as r:
            assert offer_id not in r.read().decode()
        print("· Offer left the open board")

        print("\nOK: full two-agent flow verified end-to-end")
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
