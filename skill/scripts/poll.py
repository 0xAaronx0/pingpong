"""The cron brain. Run on a schedule; Hermes auto-delivers stdout to the user.

Three jobs each run:
  1. Discover new nearby offers that match the local profile -> suggest to user.
  2. Surface new interest on the user's own offers -> prompt to accept.
  3. Reveal contacts for the user's interests that got accepted -> it's a match.

If nothing is new, prints exactly "[SILENT]" so Hermes suppresses the message
(see docs/PROTOCOL.md and the Hermes cron `[SILENT]` convention).
"""
from __future__ import annotations

import client
import geo
from nacl.exceptions import CryptoError

ACTIVITY_LABELS = {
    "table_tennis": "Tischtennis", "running": "Laufen", "cycling": "Radfahren",
    "bouldering": "Bouldern", "tennis": "Tennis", "basketball": "Basketball",
    "football": "Fußball", "badminton": "Badminton", "swimming": "Schwimmen",
    "walk": "Spaziergang", "board_games": "Brettspiele", "coffee": "Kaffee",
    "beer": "Bier", "lunch": "Mittagessen", "other": "Sonstiges",
}


def label(activity: str) -> str:
    return ACTIVITY_LABELS.get(activity, activity)


def watch_cells(profile: dict) -> list[str]:
    home = profile.get("home") or {}
    if "lat" not in home or "lon" not in home:
        raise SystemExit("profile.yaml needs home.lat and home.lon")
    rings = int(profile.get("radius_rings", 1))
    # Precision is pinned protocol-wide (PROTOCOL §2); a bigger search radius
    # comes from more rings, not coarser cells.
    center = geo.encode(float(home["lat"]), float(home["lon"]), 6)
    return geo.expand(center, rings)


def discover(ident, profile, seen) -> list[str]:
    cells = watch_cells(profile)
    activities = set(profile.get("activities") or [])
    notified = set(seen["notified_offers"])
    lines = []
    skipped_unsigned = 0
    offers = client.get("/offers", params={"cells": ",".join(cells)}) or []
    for o in offers:
        if o["agent_id"] == ident.agent_id:       # my own offer
            continue
        if activities and o["activity"] not in activities:
            continue
        if o["id"] in notified:
            continue
        if not client.verify_offer(o):
            # unsigned/tampered offer: never suggest it, never seal to its key
            skipped_unsigned += 1
            notified.add(o["id"])
            continue
        notified.add(o["id"])
        title = o.get("title") or label(o["activity"])
        note = f" — {o['note']}" if o.get("note") else ""
        lines.append(
            f"🏓 {label(o['activity'])}: {title}{note}\n"
            f"   wann: {o['earliest']} – {o['latest']}  (Zelle {o['geocell']})\n"
            f"   interessiert? → interest.py --offer-id {o['id']}"
        )
    if skipped_unsigned:
        lines.append(f"⚠️ {skipped_unsigned} Angebot(e) ohne gültige Signatur übersprungen.")
    seen["notified_offers"] = list(notified)
    return lines


def process_inbox(ident, seen) -> tuple[list[str], list[str]]:
    incoming, matches = [], []
    after_id = int(seen.get("inbox_after_id", 0))
    res = client.get("/inbox", ident=ident, params={"after_id": after_id}) or {"events": []}
    for ev in res["events"]:
        # The cursor advances no matter what happens below: one malformed event
        # (e.g. a contact blob sealed to the wrong key) must never wedge the
        # poll loop forever.
        after_id = max(after_id, int(ev["id"]))
        if ev["type"] == "new_interest":
            note = f" (Notiz: {ev['note']})" if ev.get("note") else ""
            incoming.append(
                f"📨 Jemand interessiert sich für dein {label(ev.get('activity','?'))}-Angebot{note}.\n"
                f"   annehmen → accept.py --offer-id {ev['offer_id']} --interest-id {ev['interest_id']}"
            )
        elif ev["type"] == "interest_accepted":
            try:
                # Learn + verify the owner's identity from the signed offer,
                # then require the sealed contact to be signed by exactly them.
                offer = client.get(f"/offers/{ev['offer_id']}")
                if not client.verify_offer(offer):
                    raise ValueError("offer signature invalid")
                contact = ident.unseal_contact(ev["sealed_for_interested"],
                                               expected_from=offer["agent_id"],
                                               offer_id=ev["offer_id"])
            except (CryptoError, ValueError, KeyError, TypeError, client.BrokerError):
                matches.append(
                    "⚠️ Eine Annahme kam an, aber der Kontakt ließ sich nicht "
                    "entschlüsseln oder verifizieren (fehlerhaft oder manipuliert) "
                    "— übersprungen."
                )
                continue
            matches.append(
                f"✅ Match! Deine Anfrage wurde angenommen. Kontakt: {contact}\n"
                f"   Key-Fingerprint Gegenseite: {client.fingerprint(offer['agent_id'])}"
                f" — vergleicht das im ersten Chat.\n"
                f"   Macht Ort & Uhrzeit konkret aus."
            )
        elif ev["type"] == "interest_declined":
            matches.append("ℹ️ Eine deiner Anfragen wurde abgelehnt.")
    seen["inbox_after_id"] = after_id
    return incoming, matches


def main() -> None:
    ident = client.Identity.load_or_create()
    profile = client.load_profile()
    seen = client.load_seen()

    nearby = discover(ident, profile, seen)
    incoming, matches = process_inbox(ident, seen)

    # Print before persisting: a duplicate notification next run is recoverable,
    # a notification marked seen but never delivered is not.
    if not (nearby or incoming or matches):
        print("[SILENT]")
    else:
        blocks = []
        if matches:
            blocks.append("\n".join(matches))
        if incoming:
            blocks.append("Eingehendes Interesse:\n" + "\n".join(incoming))
        if nearby:
            blocks.append("Neue Angebote in deiner Nähe:\n" + "\n".join(nearby))
        print("\n\n".join(blocks))

    client.save_seen(seen)


if __name__ == "__main__":
    main()
