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
    # Never seal to an unverified key: the offer must carry a valid signature
    # by its claimed author binding all fields incl. enc_pubkey (anti-MITM).
    if not client.verify_offer(offer):
        raise SystemExit("Offer has no valid author signature — aborting "
                         "(possible tampering attempt).")
    sealed = ident.seal_contact(offer["enc_pubkey"], args.offer_id, contact)
    interest_sig = ident.sign_blob(client.interest_canonical(
        ident.agent_id, ident.enc_pubkey, args.offer_id))
    res = client.post(f"/offers/{args.offer_id}/interest",
                      {"enc_pubkey": ident.enc_pubkey, "sealed_for_owner": sealed,
                       "interest_sig": interest_sig, "note": args.note}, ident=ident)
    print(f"Interest sent for offer {args.offer_id} ({offer['activity']}).")
    print(f"  interest_id: {res['interest_id']}")
    print("  Your contact is only released once the other person accepts.")


if __name__ == "__main__":
    main()
