"""Ensure this agent has a pingpong identity; print it.

Idempotent: creates the Ed25519 + X25519 keypair on first run, then just
reports. The agent_id is your pseudonym on the board. If the profile is
missing and a Telegram identity is derivable from the runtime (Hermes),
a contact suggestion is printed so the agent only needs the user's
confirmation instead of asking for a handle.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

import client


def _suggest_contact():
    """Best-effort: derive the user's Telegram handle from the Hermes runtime."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_HOME_CHANNEL")
    if not (tok and chat):
        try:
            for line in open("/opt/data/.env"):
                if line.startswith("TELEGRAM_BOT_TOKEN=") and not tok:
                    tok = line.strip().split("=", 1)[1]
                if line.startswith("TELEGRAM_HOME_CHANNEL=") and not chat:
                    chat = line.strip().split("=", 1)[1]
        except OSError:
            pass
    if not (tok and chat):
        return None
    try:
        url = (f"https://api.telegram.org/bot{tok}/getChat"
               f"?chat_id={urllib.parse.quote(chat)}")
        with urllib.request.urlopen(url, timeout=8) as r:
            username = json.load(r).get("result", {}).get("username")
        return f"@{username}" if username else None
    except Exception:
        return None


def main() -> None:
    ident = client.Identity.load_or_create()
    print(f"state_dir:   {client.STATE_DIR}")
    print(f"agent_id:    {ident.agent_id}")
    print(f"enc_pubkey:  {ident.enc_pubkey}")
    print(f"fingerprint: {client.fingerprint(ident.agent_id)}  (zum Abgleich nach einem Match)")
    if os.path.exists(client.PROFILE_FILE):
        print("profile:     ok")
    else:
        print("profile:     MISSING — copy profile.example.yaml")
        suggestion = _suggest_contact()
        if suggestion:
            print(f"contact-vorschlag: {suggestion}  (aus Telegram abgeleitet — nur vom "
                  f"Nutzer bestätigen lassen, nicht erfragen)")


if __name__ == "__main__":
    main()
