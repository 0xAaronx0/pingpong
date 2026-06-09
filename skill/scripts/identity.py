"""Ensure this agent has a pingpong identity; print it.

Idempotent: creates the Ed25519 + X25519 keypair on first run, then just
reports. The agent_id is your pseudonym on the board.
"""
from __future__ import annotations

import client


def main() -> None:
    ident = client.Identity.load_or_create()
    print(f"state_dir:   {client.STATE_DIR}")
    print(f"agent_id:    {ident.agent_id}")
    print(f"enc_pubkey:  {ident.enc_pubkey}")
    print(f"profile:     {'ok' if __import__('os').path.exists(client.PROFILE_FILE) else 'MISSING — copy profile.example.yaml'}")


if __name__ == "__main__":
    main()
