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

label = client.activity_label


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
    partners = client.load_meetups().get("partners", {})
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
        known = ""
        p = partners.get(o["agent_id"])
        if p:
            skill_map = {2: "much stronger than you", 1: "a bit stronger than you",
                         0: "about your level", -1: "a bit weaker than you",
                         -2: "much weaker than you"}
            bits = [f"met {p['meetups']}×"]
            if p.get("skill") is not None:
                bits.append(skill_map.get(p["skill"], ""))
            if p.get("sympathisch"):
                bits.append(f"likeable: {p['sympathisch']}")
            known = f"\n   🎯 You know them: {', '.join(b for b in bits if b)}"
        lines.append(
            f"🏓 {label(o['activity'])}: {title}{note}\n"
            f"   when: {o['earliest']} – {o['latest']}  (cell {o['geocell']}){known}\n"
            f"   interested? → interest.py --offer-id {o['id']}"
        )
    if skipped_unsigned:
        lines.append(f"⚠️ Skipped {skipped_unsigned} offer(s) with no valid signature.")
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
            note = f" (note: {ev['note']})" if ev.get("note") else ""
            incoming.append(
                f"📨 Someone is interested in your {label(ev.get('activity','?'))} offer{note}.\n"
                f"   accept → accept.py --offer-id {ev['offer_id']} --interest-id {ev['interest_id']}"
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
                    "⚠️ An acceptance arrived, but the contact couldn't be decrypted "
                    "or verified (malformed or tampered) — skipped."
                )
                continue
            matches.append(
                f"✅ Match! Your request was accepted. Contact: {contact}\n"
                f"   Now coordinate place & time via the relay (clarify the user's preference):\n"
                f"   message.py --offer-id {ev['offer_id']} --interest-id {ev['interest_id']}"
                f" --kind propose --place \"...\" --time \"...\""
            )
        elif ev["type"] == "interest_declined":
            matches.append("ℹ️ One of your requests was declined.")
        elif ev["type"] == "match_message":
            try:
                offer = client.get(f"/offers/{ev['offer_id']}")
                if not client.verify_offer(offer):
                    raise ValueError("offer signature invalid")
                if offer["agent_id"] == ident.agent_id:
                    # I'm the owner -> sender is the interested party
                    interests = client.get(f"/offers/{ev['offer_id']}/interests", ident=ident)
                    match = next((i for i in interests if i["id"] == ev["interest_id"]), None)
                    if not match or not client.verify_interest(match, ev["offer_id"]):
                        raise ValueError("interest signature invalid")
                    sender = match["agent_id"]
                else:
                    sender = offer["agent_id"]
                body = ident.unseal_message(ev["sealed_payload"], sender, ev["interest_id"])
            except (CryptoError, ValueError, KeyError, TypeError, client.BrokerError):
                matches.append("⚠️ A match message couldn't be decrypted or verified "
                               "— skipped.")
                continue
            kind = body.get("kind")
            store = client.load_meetups()
            if kind == "propose":
                store["proposals"][ev["interest_id"]] = {
                    "place": body.get("place"), "time": body.get("time"),
                    "when": body.get("when"), "offer_id": ev["offer_id"]}
                client.save_meetups(store)
            elif kind == "accept":
                proposal = store["proposals"].get(ev["interest_id"], {})
                client.record_meetup(store, offer=offer, interest_id=ev["interest_id"],
                                     counterpart=sender, proposal=proposal)
                client.save_meetups(store)
            note = f"\n   Note: {body['note']}" if body.get("note") else ""
            reply_hint = (f"   reply → message.py --offer-id {ev['offer_id']} "
                          f"--interest-id {ev['interest_id']} --kind accept|propose|text")
            if kind == "propose":
                matches.append(
                    f"📍 Proposal from your match:\n"
                    f"   {body.get('place','?')} at {body.get('time','?')}{note}\n{reply_hint}"
                )
            elif kind == "accept":
                matches.append(f"🤝 Your match said yes!{note}\n"
                               f"   The meetup is set — have fun!")
            elif kind == "decline":
                matches.append(f"❌ Your match declined the proposal.{note}\n{reply_hint}")
            else:
                matches.append(f"💬 Message from your match:{note}\n{reply_hint}")
    seen["inbox_after_id"] = after_id
    return incoming, matches


def check_new_activities(profile, seen) -> list[str]:
    """Surface community-proposed activity tags from the user's area so they
    can opt in ('New activity: ... — does this interest you too?')."""
    try:
        detail = client.get("/activities", params={"detail": 1}) or []
    except client.BrokerError:
        return []
    names = sorted(a["name"] for a in detail)
    known = seen.get("known_activities")
    seen["known_activities"] = names
    if known is None:          # first run: baseline silently
        return []
    knownset, mine = set(known), set(profile.get("activities") or [])
    watch = set(watch_cells(profile))
    lines = []
    for a in detail:
        if a["name"] in knownset or a["name"] in mine:
            continue
        if a.get("geocell") and a["geocell"] in watch:
            lines.append(
                f"🆕 New activity in your area: {label(a['name'])} ({a['name']})\n"
                f"   Let me know if this interests you too — then I'll add it to "
                f"your search profile."
            )
    return lines


def check_followups() -> list[str]:
    """~1h after an agreed meetup time: ask the user how it went."""
    from datetime import datetime, timezone
    store = client.load_meetups()
    now = datetime.now(timezone.utc).isoformat()
    lines, changed = [], False
    for m in store["meetups"]:
        if m.get("asked") or m.get("feedback") or m["followup_at"] > now:
            continue
        m["asked"] = True
        changed = True
        skill_q = ""
        if m["activity"] == "table_tennis":
            skill_q = ("\n   3) Who was better? Options: them much stronger · "
                       "them a bit stronger · about even · you a bit stronger · "
                       "you much stronger")
        lines.append(
            f"📋 Follow-up on your meetup: {label(m['activity'])}"
            f"{' at ' + m['time'] if m.get('time') else ''}"
            f"{' @ ' + m['place'] if m.get('place') else ''}\n"
            f"   ASK THE USER:\n"
            f"   1) Did the meetup happen? (if not: why not?)\n"
            f"   2) If yes: was the other person likeable? (yes/neutral/no)"
            f"{skill_q}\n"
            f"   Record → feedback.py --meetup-id {m['id']} --happened yes|no "
            f"[--reason \"...\"] [--sympathisch yes|neutral|no] "
            f"[--skill them_much|them_bit|even|you_bit|you_much]"
        )
    if changed:
        client.save_meetups(store)
    return lines


def main() -> None:
    ident = client.Identity.load_or_create()
    profile = client.load_profile()
    seen = client.load_seen()

    nearby = discover(ident, profile, seen)
    incoming, matches = process_inbox(ident, seen)
    new_tags = check_new_activities(profile, seen)
    followups = check_followups()

    # Print before persisting: a duplicate notification next run is recoverable,
    # a notification marked seen but never delivered is not.
    if not (nearby or incoming or matches or new_tags or followups):
        print("[SILENT]")
    else:
        blocks = []
        if matches:
            blocks.append("\n".join(matches))
        if incoming:
            blocks.append("Incoming interest:\n" + "\n".join(incoming))
        if nearby:
            blocks.append("New offers near you:\n" + "\n".join(nearby))
        if new_tags:
            blocks.append("\n".join(new_tags))
        if followups:
            blocks.append("\n".join(followups))
        print("\n\n".join(blocks))

    client.save_seen(seen)


if __name__ == "__main__":
    main()
