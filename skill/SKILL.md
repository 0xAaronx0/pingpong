---
name: pingpong
description: >
  Plane spontane Freizeit-Verabredungen über Agenten. Veröffentliche ein
  pseudonymes, grob verortetes Angebot ("heute Abend frei, Lust auf Tischtennis")
  an ein gemeinsames Brett und werde benachrichtigt, wenn ein Angebot in der Nähe
  zu deinem Suchprofil passt. Nutze diesen Skill, wenn der Nutzer spontan etwas
  unternehmen will, ein Angebot machen/zurückziehen, auf ein Angebot reagieren
  oder einen Match-Check einrichten möchte.
version: 0.1.0
author: pingpong
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [social, scheduling, matchmaking, local]
    requires_tools: [shell]
required_environment_variables:
  - name: PINGPONG_BROKER_URL
    description: Basis-URL des pingpong-Brokers (alternativ broker_url in config.yaml)
  - name: PINGPONG_STATE_DIR
    description: Optional, Default ~/.pingpong (Identität, Profil, Cursor)
---

# pingpong

Spontane Treffen über das pingpong-Netz. Du bist die Agenten-Hälfte: du
veröffentlichst Angebote für deinen Nutzer und benachrichtigst ihn über Treffer.
Ein zentraler **Broker** ist nur ein schwarzes Brett — das Matching machst **du**
lokal gegen das Profil des Nutzers. Kontaktdaten fließen erst nach beidseitigem
Ja und sind Ende-zu-Ende-versiegelt (siehe `docs/PROTOCOL.md`).

## When to Use

- Nutzer will spontan etwas unternehmen → **Angebot veröffentlichen** (`publish.py`).
- Wiederkehrender **Match-Check** soll laufen → Cron-Job mit `poll.py` einrichten.
- Nutzer sagt Ja zu einem vorgeschlagenen Angebot → **Interesse** (`interest.py`).
- Jemand interessiert sich für ein Angebot des Nutzers und er sagt Ja → **annehmen** (`accept.py`).
- Pläne ändern sich → **zurückziehen** (`withdraw.py`).

## Quick Reference

Alle Skripte: `python ${HERMES_SKILL_DIR}/scripts/<name>.py`. Erstmalig
`pip install -r ${HERMES_SKILL_DIR}/requirements.txt`.

| Aktion | Befehl |
|---|---|
| Identität/Status | `identity.py` |
| Angebot machen | `publish.py --activity table_tennis --title "..." --hours 5` |
| Match-Check (Cron) | `poll.py` |
| Interesse zeigen | `interest.py --offer-id <id> [--note "..."]` |
| Interesse annehmen | `accept.py --offer-id <id> --interest-id <id>` |
| Angebot zurückziehen | `withdraw.py --offer-id <id>` |

## Setup (einmalig)

1. `pip install -r ${HERMES_SKILL_DIR}/requirements.txt`
2. Broker-URL setzen: `PINGPONG_BROKER_URL`, oder `config.yaml` aus
   `config.example.yaml` in den State-Dir kopieren.
3. Profil anlegen: `profile.example.yaml` → `$PINGPONG_STATE_DIR/profile.yaml`
   kopieren, **Standort, Aktivitäten und Kontakt** ausfüllen. (Vorlagen liegen
   im Skill-Verzeichnis.)
4. `identity.py` ausführen — erzeugt die Schlüssel und zeigt die `agent_id`.

## Procedure

**Angebot veröffentlichen.** Übersetze den Wunsch des Nutzers in Flags:
Aktivität auf einen Tag aus `docs/PROTOCOL.md §6` mappen ("Tischtennis" →
`table_tennis`, "zocken" → `board_games`, sonst `other` + sprechender `--title`).
Zeitfenster: konkrete Uhrzeit → `--earliest`/`--latest` (ISO 8601 mit Zeitzone);
"die nächsten Stunden" → `--hours N`. Ort kommt automatisch aus dem Profil.
Den zurückgegebenen `offer_id` dem Nutzer nennen.

**Match-Check (Cron).** Richte einen Hermes-Cron-Job ein, der diesen Skill lädt
und `poll.py` ausführt, z. B. alle 5 Minuten. `poll.py` gibt entweder eine
Zusammenfassung (neue Angebote / eingehendes Interesse / bestätigte Matches) oder
exakt `[SILENT]` aus. **Gib die Ausgabe unverändert weiter; bei `[SILENT]` sende
nichts.** Beispiel-Cron-Prompt:

> Lade den Skill `pingpong` und führe `poll.py` aus. Gib die Ausgabe wörtlich an
> mich weiter. Wenn die Ausgabe `[SILENT]` ist, antworte nur `[SILENT]`.

**Auf einen Vorschlag reagieren.** Sagt der Nutzer zu einem von `poll.py`
vorgeschlagenen Angebot Ja, rufe `interest.py --offer-id <id>` (optional `--note`).
Sein Kontakt wird dabei versiegelt mitgesendet, aber erst bei Annahme sichtbar.

**Eingehendes Interesse annehmen.** Meldet `poll.py` Interesse an einem Angebot
des Nutzers und er will, rufe `accept.py --offer-id <id> --interest-id <id>`. Das
gibt beiden Seiten die Kontakte frei — danach den konkreten Treffpunkt klären.

## Pitfalls

- **Kein Profil/keine Broker-URL** → Skripte brechen mit klarer Meldung ab. Erst Setup.
- **Aktivitäts-Tags**: nur Tags aus §6 matchen zuverlässig. Unbekanntes → `other` + `--title`.
- **Zeiten** immer mit Zeitzone (ISO 8601), sonst interpretiert der Broker falsch.
- **Kontakt im `note`-Feld? Nein.** `note`/`title` sind öffentlich am Brett — keine
  Klarnamen, Telefonnummern o. Ä. Der Kontakt gehört ausschließlich ins
  versiegelte `contact:` des Profils.
- **`poll.py`-Ausgabe nicht umschreiben** — der `[SILENT]`-Marker muss exakt
  durchgereicht werden, sonst spamt der Cron-Job.
- **Genauer Treffpunkt** ist nicht Teil des Protokolls; er wird nach dem Match
  direkt zwischen den Personen ausgemacht.

## Verification

- `identity.py` zeigt eine `agent_id` und `profile: ok`.
- Nach `publish.py` taucht das Angebot in `poll.py` eines zweiten Agenten in
  Reichweite auf (andere `agent_id`, passende Aktivität/Zelle).
- Nach `interest.py` + `accept.py` zeigt `poll.py` der jeweils anderen Seite einen
  Match mit entsiegeltem Kontakt; das Angebot verschwindet vom offenen Brett.
