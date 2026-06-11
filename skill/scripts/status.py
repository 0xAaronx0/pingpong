"""Read-only status: my open offers, waiting interests, recent match events.

Run this whenever the user refers to a notification you don't have in your
conversation context ("annehmen", "wer war das?", "was läuft gerade?") —
cron deliveries are NOT part of the chat session. Does not advance the
inbox cursor or mutate anything.
"""
from __future__ import annotations

import client
import geo


def main() -> None:
    ident = client.Identity.load_or_create()
    profile = client.load_profile()
    home = profile.get("home") or {}
    cells = geo.expand(geo.encode(float(home["lat"]), float(home["lon"]), 6),
                       int(profile.get("radius_rings", 1)))

    offers = client.get("/offers", params={"cells": ",".join(cells)}) or []
    mine = [o for o in offers if o["agent_id"] == ident.agent_id]

    print(f"Deine Identität: {client.fingerprint(ident.agent_id)}")
    if not mine:
        print("Keine eigenen offenen Angebote am Brett.")
    for o in mine:
        print(f"\n🏓 {o['activity']}: {o.get('title') or '(ohne Titel)'}")
        print(f"   offer_id: {o['id']}")
        print(f"   Fenster: {o['earliest']} – {o['latest']}")
        interests = client.get(f"/offers/{o['id']}/interests", ident=ident) or []
        pending = [i for i in interests if i["status"] == "pending"
                   and client.verify_interest(i, o["id"])]
        accepted = [i for i in interests if i["status"] == "accepted"]
        for i in pending:
            note = f" — Notiz: {i['note']}" if i.get("note") else ""
            print(f"   ⏳ wartendes Interesse von {client.fingerprint(i['agent_id'])}{note}")
            print(f"      annehmen → accept.py --offer-id {o['id']} --interest-id {i['id']}")
            print(f"      ablehnen → (decline via API)")
        for i in accepted:
            print(f"   ✅ Match mit {client.fingerprint(i['agent_id'])} (interest_id {i['id']})")
            print(f"      Nachricht → message.py --offer-id {o['id']} --interest-id {i['id']} "
                  f"--kind propose --place ... --time ...")
        if not interests:
            print("   (noch kein Interesse)")

    # Read-only peek at recent inbox events (does NOT advance the poll cursor).
    res = client.get("/inbox", ident=ident, params={"after_id": 0}) or {"events": []}
    msgs = [e for e in res["events"] if e["type"] == "match_message"][-5:]
    if msgs:
        print("\nLetzte Relay-Nachrichten deiner Matches (neueste zuletzt):")
    for e in msgs:
        try:
            offer = client.get(f"/offers/{e['offer_id']}")
            if not client.verify_offer(offer):
                continue
            if offer["agent_id"] == ident.agent_id:
                interests = client.get(f"/offers/{e['offer_id']}/interests", ident=ident)
                m = next((i for i in interests if i["id"] == e["interest_id"]), None)
                sender = m["agent_id"] if m else None
            else:
                sender = offer["agent_id"]
            body = ident.unseal_message(e["sealed_payload"], sender, e["interest_id"])
        except Exception:
            continue
        kind = body.get("kind")
        detail = {"propose": f"📍 VORSCHLAG: {body.get('place','?')} um {body.get('time','?')}",
                  "accept": "🤝 ZUSAGE", "decline": "❌ ABSAGE"}.get(kind, "💬 Nachricht")
        note = f" — {body['note']}" if body.get("note") else ""
        print(f"   {e['ts'][:16]}  {detail}{note}")
        if kind == "propose":
            print(f"      zusagen → message.py --offer-id {e['offer_id']} "
                  f"--interest-id {e['interest_id']} --kind accept")
    accepted_any = [e for e in res["events"] if e["type"] == "interest_accepted"][-2:]
    for e in accepted_any:
        print(f"\n   {e['ts'][:16]}  ✅ deine Anfrage wurde angenommen "
              f"(Details verarbeitet poll.py)")


if __name__ == "__main__":
    main()
