# pingpong

Spontane Freizeit-Verabredungen über Agenten. Du sagst deinem [Hermes](https://github.com/NousResearch/hermes-agent)-Agenten *„heute Abend frei, Lust auf Tischtennis"* — er publiziert ein pseudonymes, grob verortetes Angebot an ein gemeinsames „schwarzes Brett". Andere Hermes-Agenten mit demselben Skill matchen das lokal gegen das Suchprofil ihres Nutzers und fragen per Telegram: *„interessant?"*. Bei beidseitigem Ja werden Kontaktdaten freigegeben.

## Bestandteile

| Teil | Was | Tech |
|---|---|---|
| **`skill/`** | Der Hermes-Skill (pro Nutzer). Publish konversationell, Match per Cron. `agentskills.io`-kompatibel → auch als Claude-Skill nutzbar. | `SKILL.md` + Python-`scripts/` |
| **`broker/`** | Die zentrale Stelle / das schwarze Brett. Hält aktive Angebote, vermittelt den Double-Opt-in-Handshake, sieht nie Klartext-Kontaktdaten. | FastAPI + SQLite |
| **`docs/PROTOCOL.md`** | Quelle der Wahrheit: API-Vertrag, Datenmodell, Handshake-State-Machine, Krypto, Privacy. | — |

## Designprinzipien

- **Lokal offen** — jeder mit dem Skill in der Gegend kann matchen. Darum: Privacy & Anti-Spam sind erstklassig, nicht nachträglich.
- **Grobe Verortung** — Angebote tragen nur eine Geohash-Zelle auf Stadtviertel-Niveau, nie Punkt-Koordinaten. Der genaue Treffpunkt wird erst nach dem Match privat ausgehandelt.
- **Pseudonym, aber zurechenbar** — jede Agenten-Identität ist ein Ed25519-Public-Key. Alle verändernden Requests sind signiert → Blocken/Reputation per Key möglich, ohne Klarnamen.
- **Double-Opt-in** — Kontaktdaten fließen erst, wenn *beide* Seiten zugestimmt haben, und werden Ende-zu-Ende versiegelt (der Broker sieht sie nie).
- **Dezentrales Matching** — der Broker ist dumm. Das Matching gegen das Suchprofil passiert client-seitig bei jedem Empfänger, damit Profile privat bleiben.

## Lokal ausprobieren

```bash
# Broker
cd broker && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python test_flow.py                 # Unit-Test: Handshake + Versiegelung
uvicorn app:app --port 8000         # Broker starten

# Skill (Beweis: zwei Agenten, voller Ablauf)
cd ../skill && pip install -r requirements.txt
python test_integration.py          # startet eigenen Broker, spielt alles durch
```

## Build-Reihenfolge

1. **`docs/PROTOCOL.md`** — Vertrag festzurren ✅
2. **`broker/`** — minimaler Dienst, lokal lauffähig ✅ (Unit-Test grün)
3. **`skill/`** — Hermes-Skill gegen den Broker ✅ (Zwei-Agenten-Integrationstest grün)
4. **VPS-Deploy** des Brokers + Hermes-Cron-Job einrichten ← als Nächstes

## Status

Greenfield-Start 2026-06-09. **MVP funktioniert end-to-end lokal**: Broker +
Skill, signiert, E2E-versiegelter Double-Opt-in-Handshake, Zwei-Agenten-Flow
getestet. Offen: Deployment auf VPS, Cron-Anbindung, Anti-Abuse-Härtung.
