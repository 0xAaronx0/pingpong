"""Report an offer that violates the content policy (GET /policy on the broker).

Reports are signed (accountable) and deduplicated per reporter. Enough
independent reports remove the offer automatically.

    python report.py --offer-id <uuid> --reason illegal|sexual|spam|harassment|pii|other [--note "..."]
"""
from __future__ import annotations

import argparse

import client

REASONS = ["illegal", "sexual", "spam", "harassment", "pii", "other"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offer-id", required=True)
    ap.add_argument("--reason", required=True, choices=REASONS)
    ap.add_argument("--note", help="optional short explanation")
    args = ap.parse_args()

    ident = client.Identity.load_or_create()
    res = client.post(f"/offers/{args.offer_id}/report",
                      {"reason": args.reason, "note": args.note}, ident=ident)
    print(f"Meldung übermittelt ({args.reason}).")
    if res and res.get("removed"):
        print("Das Angebot wurde aufgrund mehrerer unabhängiger Meldungen entfernt.")


if __name__ == "__main__":
    main()
