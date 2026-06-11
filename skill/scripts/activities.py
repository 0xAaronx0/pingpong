"""List or grow the network-wide activity vocabulary (PROTOCOL §6).

The vocabulary lives at the broker and grows with the community: it starts
with table_tennis + lunch, and every new tag someone publishes or proposes
becomes visible to all users from then on.

    python3 activities.py                       # aktuelle Liste
    python3 activities.py --propose bouldering  # neuen Tag vorschlagen
"""
from __future__ import annotations

import argparse
import re

import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", help="neuen Aktivitäts-Tag registrieren (snake_case, englisch)")
    args = ap.parse_args()

    if args.propose:
        tag = args.propose.strip().lower().replace("-", "_").replace(" ", "_")
        if not re.match(r"^[a-z][a-z0-9_]{0,31}$", tag):
            raise SystemExit(f"Ungültiger Tag '{tag}' — nur a-z, 0-9, _ (max 32 Zeichen).")
        ident = client.Identity.load_or_create()
        body = {"activity": tag}
        try:
            import geo
            home = client.load_profile().get("home") or {}
            body["geocell"] = geo.encode(float(home["lat"]), float(home["lon"]), 6)
        except Exception:
            pass  # ohne Profil: Vorschlag ohne Geo-Bezug (wird nicht regional angekündigt)
        res = client.post("/activities", body, ident=ident)
        if res and res.get("new"):
            print(f"'{tag}' ist jetzt netzwerk-weit als Aktivität verfügbar. ✓")
        else:
            print(f"'{tag}' gab es schon — alles gut.")
        print(f"  Tipp: in profile.yaml unter activities: aufnehmen, um darüber "
              f"benachrichtigt zu werden.")
        return

    tags = client.get("/activities") or []
    print("Aktivitäten im Netzwerk:")
    for t in tags:
        print(f"  {t:24s} {client.activity_label(t)}")
    print("\nNeuen Tag vorschlagen: activities.py --propose <tag>")


if __name__ == "__main__":
    main()
