"""Express interest in an offer (= your opt-in).

Fetches the offer's enc_pubkey, seals YOUR contact (from profile.yaml) to it,
and posts the interest. The broker only relays the sealed blob; the offerer
must still accept before they ever see your contact.

    python interest.py --offer-id <uuid> [--note "bin in 20 min da"]
"""
from __future__ import annotations

import argparse

import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offer-id", required=True)
    ap.add_argument("--note")
    args = ap.parse_args()

    ident = client.Identity.load_or_create()
    profile = client.load_profile()
    contact = profile.get("contact")
    if not contact:
        raise SystemExit("profile.yaml needs a `contact:` block to share on a match")

    offer = client.get(f"/offers/{args.offer_id}")
    sealed = ident.seal_to(offer["enc_pubkey"], contact)
    res = client.post(f"/offers/{args.offer_id}/interest",
                      {"enc_pubkey": ident.enc_pubkey, "sealed_for_owner": sealed,
                       "note": args.note}, ident=ident)
    print(f"Interesse gesendet an Angebot {args.offer_id} ({offer['activity']}).")
    print(f"  interest_id: {res['interest_id']}")
    print("  Dein Kontakt wird erst freigegeben, wenn die andere Person annimmt.")


if __name__ == "__main__":
    main()
