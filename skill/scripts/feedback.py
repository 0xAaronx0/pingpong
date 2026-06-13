"""Record post-meetup feedback and build the local skill memory.

Asked by the poll ~1h after an agreed meetup. The answers stay local: they
update the per-counterpart memory (used to annotate future offers, e.g.
"about your level") and the running per-activity level estimate.

    feedback.py --meetup-id <id> --happened yes|no [--reason "..."]
                [--sympathisch yes|neutral|no]
                [--skill them_much|them_bit|even|you_bit|you_much]
"""
from __future__ import annotations

import argparse

import client

SKILL_DELTA = {"gegenueber_deutlich": 2, "gegenueber_etwas": 1, "gleich": 0,
               "ich_etwas": -1, "ich_deutlich": -2,
               # English aliases (canonical in the published skill)
               "them_much": 2, "them_bit": 1, "even": 0, "you_bit": -1, "you_much": -2}
_HAPPENED = {"yes": "yes", "ja": "yes", "no": "no", "nein": "no"}
_SYMPA = {"yes": "yes", "ja": "yes", "neutral": "neutral", "no": "no", "nein": "no"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meetup-id", required=True)
    ap.add_argument("--happened", required=True, choices=sorted(_HAPPENED),
                    help="yes|no (ja|nein also accepted)")
    ap.add_argument("--reason")
    ap.add_argument("--sympathisch", choices=sorted(_SYMPA),
                    help="yes|neutral|no (German also accepted)")
    ap.add_argument("--skill", choices=sorted(SKILL_DELTA),
                    help="them_much|them_bit|even|you_bit|you_much")
    args = ap.parse_args()
    args.happened = _HAPPENED[args.happened]
    if args.sympathisch:
        args.sympathisch = _SYMPA[args.sympathisch]

    store = client.load_meetups()
    meetup = next((m for m in store["meetups"]
                   if m["id"] == args.meetup_id or m["interest_id"].startswith(args.meetup_id)),
                  None)
    if not meetup:
        raise SystemExit(f"No meetup found with id {args.meetup_id} "
                         f"(see meetups.json).")

    meetup["feedback"] = {"happened": args.happened, "reason": args.reason,
                          "sympathisch": args.sympathisch, "skill": args.skill}
    print(f"Feedback recorded for {client.activity_label(meetup['activity'])} "
          f"({'happened' if args.happened == 'yes' else 'did not happen'}).")

    if args.happened == "yes":
        # per-counterpart memory -> "you know them" annotation on future offers
        p = store["partners"].setdefault(meetup["counterpart"], {"meetups": 0})
        p["meetups"] += 1
        p["activity"] = meetup["activity"]
        if args.sympathisch:
            p["sympathisch"] = args.sympathisch
        if args.skill:
            p["skill"] = SKILL_DELTA[args.skill]
        # running per-activity level estimate (positive = opponents were stronger)
        if args.skill:
            lvl = store["levels"].setdefault(meetup["activity"], {"n": 0, "balance": 0})
            lvl["n"] += 1
            lvl["balance"] += SKILL_DELTA[args.skill]
            avg = lvl["balance"] / lvl["n"]
            trend = ("your opponents have been stronger on average so far" if avg > 0.3
                     else "your opponents have been weaker on average so far" if avg < -0.3
                     else "you play about at the level of your opponents")
            print(f"  Level estimate {client.activity_label(meetup['activity'])}: "
                  f"{lvl['n']} meetup(s), balance {lvl['balance']:+d} — {trend}.")
    client.save_meetups(store)


if __name__ == "__main__":
    main()
