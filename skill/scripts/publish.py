"""Publish an offer to the board.

The agent maps the user's natural-language request ("heute Abend Tischtennis")
onto these flags. Location comes from the profile's home coords, coarsened to a
geohash cell — exact coordinates never leave this machine.

Examples:
    python publish.py --activity table_tennis --title "Tischtennis locker" --hours 5
    python publish.py --activity running --earliest 2026-06-09T18:00:00+00:00 \
                      --latest 2026-06-09T20:00:00+00:00 --note "lockeres Tempo"
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import client
import geo


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--activity", required=True, help="activity tag, see PROTOCOL §6")
    ap.add_argument("--title", help="short free-text description")
    ap.add_argument("--note", help="optional note (no personal data)")
    ap.add_argument("--earliest", help="ISO start; default = now")
    ap.add_argument("--latest", help="ISO end; default = now + --hours")
    ap.add_argument("--hours", type=float, default=4.0, help="window length if --latest omitted")
    args = ap.parse_args()

    ident = client.Identity.load_or_create()
    profile = client.load_profile()
    home = profile.get("home") or {}
    if "lat" not in home or "lon" not in home:
        raise SystemExit("profile.yaml needs home.lat and home.lon")
    # Precision is pinned protocol-wide to 6 (PROTOCOL §2) so publishers and
    # searchers always meet on identical cell strings.
    geocell = geo.encode(float(home["lat"]), float(home["lon"]), 6)

    now = datetime.now(timezone.utc)
    # Canonical UTC form before signing — the signature covers exactly the
    # strings the broker stores and serves (PROTOCOL §1.2).
    earliest = client.canon_ts(args.earliest or now.isoformat())
    latest = client.canon_ts(args.latest or (now + timedelta(hours=args.hours)).isoformat())

    body = {
        "enc_pubkey": ident.enc_pubkey,
        "activity": args.activity,
        "title": args.title,
        "geocell": geocell,
        "earliest": earliest,
        "latest": latest,
        "note": args.note,
    }
    body["offer_sig"] = ident.sign_blob(
        client.offer_canonical({**body, "agent_id": ident.agent_id}))
    res = client.post("/offers", body, ident=ident)
    print(f"Offer published: {args.activity} in cell {geocell}")
    print(f"  window: {earliest} – {latest}")
    print(f"  offer_id: {res['offer_id']}  (expires: {res.get('expires_at')})")


if __name__ == "__main__":
    main()
