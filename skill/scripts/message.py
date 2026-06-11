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
    ap.add_argument("--time")
    ap.add_argument("--note")
    args = ap.parse_args()
    if args.kind == "propose" and not (args.place and args.time):
        raise SystemExit("propose braucht --place und --time")

    ident = client.Identity.load_or_create()
    offer = client.get(f"/offers/{args.offer_id}")
    if not client.verify_offer(offer):
        raise SystemExit("Angebot hat keine gültige Signatur — breche ab.")

    # Determine my role to find the counterpart's verified encryption key.
    if offer["agent_id"] == ident.agent_id:
        interests = client.get(f"/offers/{args.offer_id}/interests", ident=ident)
        match = next((i for i in interests if i["id"] == args.interest_id), None)
        if not match or not client.verify_interest(match, args.offer_id):
            raise SystemExit("Interest nicht gefunden oder Signatur ungültig.")
        recipient_enc = match["enc_pubkey"]
    else:
        recipient_enc = offer["enc_pubkey"]

    body = {"kind": args.kind}
    for k in ("place", "time", "note"):
        v = getattr(args, k)
        if v:
            body[k] = v
    sealed = ident.seal_message(recipient_enc, args.interest_id, body)
    client.post(f"/matches/{args.interest_id}/messages",
                {"sealed_payload": sealed}, ident=ident)
    pretty = {"propose": f"Vorschlag gesendet: {args.place}, {args.time}",
              "accept": "Zusage gesendet ✅", "decline": "Absage gesendet",
              "text": "Nachricht gesendet"}[args.kind]
    print(pretty)
    print("  Die Gegenseite bekommt sie beim nächsten Poll (versiegelt, signiert).")
    if args.kind == "accept":
        print("  → PFLICHT: Bestätige deinem Nutzer jetzt die finale Verabredung")
        print("    (Ort, Zeit, was vereinbart wurde) — er muss wissen, was abgemacht ist.")
    elif args.kind == "propose":
        print("  → Hinweis: propose/accept nur nach expliziter Bestätigung des Nutzers senden.")


if __name__ == "__main__":
    main()
