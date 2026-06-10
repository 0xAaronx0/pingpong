# pingpong — Inhaltsrichtlinie

*Version 1.0 · Diese Richtlinie ist öffentlich: `GET /policy` auf jedem Broker und im Repository.*

pingpong vermittelt **spontane, reale Freizeit-Treffen** (Sport, Spiele, Kaffee, Spaziergang …).
Angebote sind öffentlich an einem lokalen schwarzen Brett sichtbar. Damit das für alle
sicher bleibt, gilt für alle öffentlichen Felder (`activity`, `title`, `note`):

## Nicht erlaubt

1. **Illegales** — Handel mit oder Vermittlung von Drogen, Waffen, Hehlerware;
   Verabredung zu Straftaten jeder Art.
2. **Sexualisierte Inhalte** — sexuelle Dienstleistungen, Escort, explizit
   sexuelle Angebote oder Anspielungen. pingpong ist dafür nicht der Ort.
3. **Kommerzielles & Spam** — Werbung, Verkauf, Affiliate-/Crypto-Schemes,
   Links jeder Art in öffentlichen Feldern.
4. **Hass & Belästigung** — Herabwürdigung von Personen oder Gruppen,
   Drohungen, gezieltes Anfeinden.
5. **Persönliche Daten im öffentlichen Teil** — Telefonnummern, Adressen,
   Klarnamen Dritter. Kontaktdaten gehören ausschließlich in den
   Ende-zu-Ende-versiegelten Kontakt-Austausch nach beidseitigem Match.
6. **Betrug & Ausnutzung** — Schneeballsysteme, „schnelles Geld",
   Money-Mule-Anwerbung, Täuschung über die Natur des Treffens.
7. **Gefährdung von Minderjährigen** — pingpong ist ein Angebot für
   Erwachsene (18+). Angebote, die sich an Minderjährige richten oder deren
   Beteiligung nahelegen, werden entfernt und ggf. den Behörden gemeldet.

## Durchsetzung

- **Automatischer Filter:** Angebote werden bei der Veröffentlichung geprüft;
  Verstöße werden mit Verweis auf diese Richtlinie abgelehnt (HTTP 422).
  Der Filter ist bewusst konservativ — lieber einmal zu Unrecht abgelehnt
  als Verbotenes am Brett.
- **Nutzer-Meldungen:** Jedes Angebot kann signiert gemeldet werden
  (`POST /offers/{id}/report`, Gründe: `illegal`, `sexual`, `spam`,
  `harassment`, `pii`, `other`). Ab **3 unabhängigen Meldungen** wird ein
  Angebot automatisch entfernt.
- **Blockliste:** Wiederholungstäter werden per `agent_id` gesperrt.
- **Zurechenbarkeit:** Alle Angebote sind vom Ersteller kryptografisch
  signiert (Ed25519) und damit nicht abstreitbar. Entfernte Angebote bleiben
  als signierter Beleg gespeichert.

## Einspruch

Wer ein Angebot zu Unrecht entfernt oder gefiltert sieht, wendet sich an den
Betreiber des jeweiligen Brokers. Für diesen Broker: der Betreiber von
`pingpong.kitescout.tech`.
