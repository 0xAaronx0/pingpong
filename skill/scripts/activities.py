"""List or grow the network-wide activity vocabulary (PROTOCOL §6).

The vocabulary lives at the broker and grows with the community: it starts
with table_tennis + lunch, and every new tag someone publishes or proposes
becomes visible to all users from then on.

    python3 activities.py                       # current list
    python3 activities.py --propose bouldering  # propose a new tag
"""
from __future__ import annotations

import argparse
import re

import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", help="register a new activity tag (snake_case, English)")
    args = ap.parse_args()

    if args.propose:
        tag = args.propose.strip().lower().replace("-", "_").replace(" ", "_")
        if not re.match(r"^[a-z][a-z0-9_]{0,31}$", tag):
            raise SystemExit(f"Invalid tag '{tag}' — only a-z, 0-9, _ (max 32 chars).")
        ident = client.Identity.load_or_create()
        body = {"activity": tag}
        try:
            import geo
            home = client.load_profile().get("home") or {}
            body["geocell"] = geo.encode(float(home["lat"]), float(home["lon"]), 6)
        except Exception:
            pass  # no profile: propose without geo (won't be announced regionally)
        res = client.post("/activities", body, ident=ident)
        if res and res.get("new"):
            print(f"'{tag}' is now available network-wide as an activity. ✓")
        else:
            print(f"'{tag}' already existed — all good.")
        print(f"  Tip: add it to profile.yaml under activities: to get notified about it.")
        return

    tags = client.get("/activities") or []
    print("Activities in the network:")
    for t in tags:
        print(f"  {t:24s} {client.activity_label(t)}")
    print("\nPropose a new tag: activities.py --propose <tag>")


if __name__ == "__main__":
    main()
