"""Accept an interest on one of your offers (= your opt-in).

Looks up the interested party's enc_pubkey, seals YOUR contact to it, and posts
the acceptance. This completes the double-opt-in: the broker then releases your
sealed contact to them and you have already received theirs via the inbox.

    python accept.py --offer-id <uuid> --interest-id <uuid>
"""
from __future__ import annotations

import argparse

import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offer-id", required=True)
    ap.add_argument("--interest-id", required=True)
    args = ap.parse_args()

    ident = client.Identity.load_or_create()
    profile = client.load_profile()
    contact = profile.get("contact")
    if not contact:
        raise SystemExit("profile.yaml needs a `contact:` block to share on a match")

    interests = client.get(f"/offers/{args.offer_id}/interests", ident=ident)
    match = next((i for i in interests if i["id"] == args.interest_id), None)
    if not match:
        raise SystemExit(f"interest {args.interest_id} not found on offer {args.offer_id}")

    # Their contact (sealed to us) — reveal it now that we're accepting.
    their_contact = ident.unseal(match["sealed_for_owner"])
    sealed_back = ident.seal_to(match["enc_pubkey"], contact)
    client.post(f"/interests/{args.interest_id}/accept",
                {"sealed_for_interested": sealed_back}, ident=ident)
    print("Match bestätigt! 🎉")
    print(f"  Kontakt der anderen Person: {their_contact}")
    print("  Macht jetzt Ort & Uhrzeit konkret aus.")


if __name__ == "__main__":
    main()
