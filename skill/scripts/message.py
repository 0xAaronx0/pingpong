"""Send a sealed negotiation message to your match (PROTOCOL §4.1).

After a match, both agents can negotiate place & time through the broker —
sealed, signed, broker sees nothing. Kinds:

  propose : suggest place/time     --place "Helmi-Platz" --time "19:30" [--note]
  accept  : agree to the proposal  [--note]
  decline : reject the proposal    [--note]
  text    : free-form message      --note "..."

    python message.py --offer-id X --interest-id Y --kind propose --place "..." --time "..."
"""
from __future__ import annotations

import argparse

import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offer-id", required=True)
    ap.add_argument("--interest-id", required=True)
    ap.add_argument("--kind", required=True, choices=["propose", "accept", "decline", "text"])
    ap.add_argument("--place")
    ap.add_argument("--time", help="display text, e.g. '12:30'")
    ap.add_argument("--when", help="meetup time as an ISO timestamp with timezone "
                                   "(enables the automatic follow-up ~1h later)")
    ap.add_argument("--note")
    args = ap.parse_args()
    if args.kind == "propose" and not (args.place and args.time):
        raise SystemExit("propose needs --place and --time (and ideally --when as ISO)")

    ident = client.Identity.load_or_create()
    offer = client.get(f"/offers/{args.offer_id}")
    if not client.verify_offer(offer):
        raise SystemExit("Offer has no valid signature — aborting.")

    # Determine my role to find the counterpart's verified encryption key.
    if offer["agent_id"] == ident.agent_id:
        interests = client.get(f"/offers/{args.offer_id}/interests", ident=ident)
        match = next((i for i in interests if i["id"] == args.interest_id), None)
        if not match or not client.verify_interest(match, args.offer_id):
            raise SystemExit("Interest not found or signature invalid.")
        recipient_enc, counterpart = match["enc_pubkey"], match["agent_id"]
    else:
        recipient_enc, counterpart = offer["enc_pubkey"], offer["agent_id"]

    body = {"kind": args.kind}
    for k in ("place", "time", "when", "note"):
        v = getattr(args, k)
        if v:
            body[k] = v
    sealed = ident.seal_message(recipient_enc, args.interest_id, body)
    client.post(f"/matches/{args.interest_id}/messages",
                {"sealed_payload": sealed}, ident=ident)
    pretty = {"propose": f"Proposal sent: {args.place}, {args.time}",
              "accept": "Acceptance sent ✅", "decline": "Decline sent",
              "text": "Message sent"}[args.kind]
    print(pretty)
    print("  The other side receives it on their next poll (sealed, signed).")

    # Local meetup memory: proposals are remembered so an accept (either side)
    # can schedule the post-meetup follow-up ~1h after the agreed time.
    store = client.load_meetups()
    if args.kind == "propose":
        store["proposals"][args.interest_id] = {
            "place": args.place, "time": args.time, "when": args.when,
            "offer_id": args.offer_id,
        }
        client.save_meetups(store)
        print("  → Note: only send propose/accept after the user has explicitly confirmed.")
    elif args.kind == "accept":
        proposal = store["proposals"].get(args.interest_id, {})
        client.record_meetup(store, offer=offer, interest_id=args.interest_id,
                             counterpart=counterpart, proposal=proposal)
        client.save_meetups(store)
        print("  → REQUIRED: now confirm the final arrangement to your user")
        print("    (place, time, what was agreed) — they must know what's set.")
        print("  → A follow-up (did it happen? how was it?) is scheduled for ~1h after the time.")


if __name__ == "__main__":
    main()
