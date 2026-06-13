"""Ensure this agent has a pingpong identity; print it.

Idempotent: creates the Ed25519 + X25519 keypair on first run, then just
reports. The agent_id is your pseudonym on the board. If the profile is
missing it prints a self-contained setup runbook for the agent.
"""
from __future__ import annotations

import os

import client


def main() -> None:
    ident = client.Identity.load_or_create()
    print(f"state_dir:   {client.STATE_DIR}")
    print(f"agent_id:    {ident.agent_id}")
    print(f"enc_pubkey:  {ident.enc_pubkey}")
    print(f"fingerprint: {client.fingerprint(ident.agent_id)}  (to compare after a match)")
    if os.path.exists(client.PROFILE_FILE):
        print("profile:     ok — setup complete, go ahead.")
        return
    # Self-contained setup runbook: whoever runs this (agent or human) gets
    # everything needed without having read the rest of SKILL.md.
    print("profile:     MISSING — setup needed. NEXT STEPS:")
    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"""
  1) Ask the user (do NOT guess anything!):
     - Rough location? (a neighbourhood is enough — only a coarse ~1 km cell
       is published, never the address)
     - Which activities are they into? Current network list:
       python3 {skill_dir}/scripts/activities.py  (custom proposals are fine)
     - Contact for matches: you usually already know the user's messaging
       handle from your own context — propose it and just have them confirm
       ("after a match I'll share @you, sealed — ok?"); otherwise ask.
       (end-to-end sealed, shared only after a mutual yes)

  2) Write the profile to: {client.PROFILE_FILE}
     Template: {skill_dir}/profile.example.yaml
     (fields: home.lat/lon, radius_rings, activities, contact)

  3) Set up the match-check — WITHOUT asking the user (details in SKILL.md,
     'Match-check' section): poll.py every 5 min via cron;
     Hermes: copy scripts/pingpong-poll.sh to /opt/data/scripts/ +
     hermes cron create "every 5m" --name pingpong-poll --no-agent
       --script pingpong-poll.sh --deliver telegram

  4) Then run this script again — once it shows 'profile: ok' you can publish
     (publish.py) and everything else runs on its own.""")


if __name__ == "__main__":
    main()
