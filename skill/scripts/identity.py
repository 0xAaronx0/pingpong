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
        print("profile:     ok — Setup fertig, direkt loslegen.")
        return
    # Self-contained setup runbook: whoever runs this (agent or human) gets
    # everything needed without having read the rest of SKILL.md.
    print("profile:     MISSING — Setup nötig. NÄCHSTE SCHRITTE:")
    suggestion = _suggest_contact()
    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"""
  1) Frage den Nutzer (NICHTS raten!):
     - Ungefährer Standort? (Kiez/Viertel reicht — es wird nur eine grobe
       ~1-km-Zelle veröffentlicht, nie die Adresse)
     - Welche Aktivitäten interessieren ihn? Aktuelle Netzwerk-Liste:
       python3 {skill_dir}/scripts/activities.py  (eigene Vorschläge möglich)
     - Kontakt für Matches:{f' Vorschlag {suggestion} (aus Telegram abgeleitet) — nur bestätigen lassen' if suggestion else ' z. B. Telegram-Handle erfragen'}
       (wird Ende-zu-Ende versiegelt, erst nach beidseitigem Ja geteilt)

  2) Schreibe das Profil nach: {client.PROFILE_FILE}
     Vorlage: {skill_dir}/profile.example.yaml
     (Felder: home.lat/lon, radius_rings, activities, contact)

  3) Richte den Match-Check ein — OHNE den Nutzer zu fragen (Details im
     SKILL.md, Abschnitt 'Match-Check'): alle 5 Min poll.py via Cron;
     Hermes: scripts/pingpong-poll.sh nach /opt/data/scripts/ kopieren +
     hermes cron create "every 5m" --name pingpong-poll --no-agent
       --script pingpong-poll.sh --deliver telegram

  4) Danach diesem Skript erneut ausführen — zeigt es 'profile: ok',
     kann publiziert werden (publish.py) und alles Weitere läuft von selbst.""")


if __name__ == "__main__":
    main()
