"""Record post-meetup feedback and build the local skill memory.

Asked by the poll ~1h after an agreed meetup. The answers stay local: they
update the per-counterpart memory (used to annotate future offers, e.g.
"etwa dein Niveau") and the running per-activity level estimate.

    feedback.py --meetup-id <id> --happened ja|nein [--reason "..."]
                [--sympathisch ja|neutral|nein]
                [--skill gegenueber_deutlich|gegenueber_etwas|gleich|ich_etwas|ich_deutlich]
"""
from __future__ import annotations

import argparse

import client

SKILL_DELTA = {"gegenueber_deutlich": 2, "gegenueber_etwas": 1, "gleich": 0,
               "ich_etwas": -1, "ich_deutlich": -2}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meetup-id", required=True)
    ap.add_argument("--happened", required=True, choices=["ja", "nein"])
    ap.add_argument("--reason")
    ap.add_argument("--sympathisch", choices=["ja", "neutral", "nein"])
    ap.add_argument("--skill", choices=sorted(SKILL_DELTA))
    args = ap.parse_args()

    store = client.load_meetups()
    meetup = next((m for m in store["meetups"]
                   if m["id"] == args.meetup_id or m["interest_id"].startswith(args.meetup_id)),
                  None)
    if not meetup:
        raise SystemExit(f"Kein Treffen mit ID {args.meetup_id} gefunden "
                         f"(siehe meetups.json).")

    meetup["feedback"] = {"happened": args.happened, "reason": args.reason,
                          "sympathisch": args.sympathisch, "skill": args.skill}
    print(f"Feedback erfasst für {client.activity_label(meetup['activity'])} "
          f"({'stattgefunden' if args.happened == 'ja' else 'nicht stattgefunden'}).")

    if args.happened == "ja":
        # per-counterpart memory -> "Kennst du schon"-Annotation künftiger Angebote
        p = store["partners"].setdefault(meetup["counterpart"], {"meetups": 0})
        p["meetups"] += 1
        p["activity"] = meetup["activity"]
        if args.sympathisch:
            p["sympathisch"] = args.sympathisch
        if args.skill:
            p["skill"] = SKILL_DELTA[args.skill]
        # running per-activity level estimate (positive = Gegner waren stärker)
        if args.skill:
            lvl = store["levels"].setdefault(meetup["activity"], {"n": 0, "balance": 0})
            lvl["n"] += 1
            lvl["balance"] += SKILL_DELTA[args.skill]
            avg = lvl["balance"] / lvl["n"]
            tendenz = ("deine Gegner waren bisher im Schnitt stärker" if avg > 0.3
                       else "deine Gegner waren bisher im Schnitt schwächer" if avg < -0.3
                       else "du spielst etwa auf dem Niveau deiner Gegner")
            print(f"  Niveau-Schätzer {client.activity_label(meetup['activity'])}: "
                  f"{lvl['n']} Begegnung(en), Bilanz {lvl['balance']:+d} — {tendenz}.")
    client.save_meetups(store)


if __name__ == "__main__":
    main()
