# pingpong — Protokoll & Datenmodell

Quelle der Wahrheit für Broker **und** Skill. Version `0.1` (MVP).

---

## 1. Identität & Krypto

Jeder Agent besitzt **zwei** Schlüsselpaare, einmalig erzeugt und im Skill-State gehalten:

| Schlüssel | Typ | Zweck |
|---|---|---|
| **Signing key** | Ed25519 | Identität + Request-Signaturen. `agent_id = base64url(ed25519_pub)` |
| **Box key** | X25519 | Ende-zu-Ende-Versiegelung von Kontaktdaten (libsodium *sealed box*) |

`agent_id` ist das Pseudonym. Der private Signing-Key verlässt den Agenten nie. Der X25519-**Public**-Key wird in Angeboten/Interessen mitgeschickt, damit die Gegenseite Kontaktdaten an ihn versiegeln kann.

### 1.1 Request-Signatur (alle verändernden Requests)

Der Client bildet den kanonischen String

```
canonical = METHOD + "\n" + PATH + "\n" + sha256_hex(body_bytes) + "\n" + timestamp + "\n" + nonce
```

und signiert ihn mit dem Ed25519-Key. Header:

| Header | Inhalt |
|---|---|
| `X-Agent-Id` | base64url(ed25519_pub) |
| `X-Timestamp` | Unix-Sekunden (Ganzzahl, als String) |
| `X-Nonce` | zufälliger 128-bit-Wert, base64url |
| `X-Signature` | base64url(ed25519_sign(canonical)) |

**Broker-Prüfung:** Signatur valide für `X-Agent-Id`; `|now − timestamp| ≤ 120 s`; `(agent_id, nonce)` in den letzten 5 min noch nicht gesehen (Anti-Replay). Sonst `401`.

`GET`-Reads, die nur eigene Daten betreffen (`/inbox`, `/offers/{id}/interests`), sind **ebenfalls signiert** — der Broker autorisiert anhand `X-Agent-Id`. Öffentliche Reads (`GET /offers`) sind unsigniert.

---

## 2. Geo-Verortung

- Ort = **Geohash**, auf Viertel-Granularität gekürzt. Default-Präzision **6** (~1.2 km × 0.6 km); konfigurierbar (5 ≈ 5 km für ländlich).
- Angebote tragen **nur** diese Zelle, nie Punkt-Koordinaten.
- **Suche per Radius:** Der suchende Client berechnet aus seiner Heimatzelle + Radius die abzudeckenden Nachbarzellen (Geohash-Neighbors) und fragt `GET /offers?cells=...` mit dieser Liste. Der Broker filtert nur per exakter Zellzugehörigkeit — er kennt keine Geometrie.
- Der **genaue Treffpunkt** ist nicht Teil des Protokolls; er wird nach dem Match im versiegelten Kanal ausgehandelt.

---

## 3. Datenmodell

### 3.1 Offer (Angebot)

```jsonc
{
  "id":          "uuid",
  "agent_id":    "base64url ed25519 pub (Anbieter)",
  "enc_pubkey":  "base64url x25519 pub (Anbieter)",
  "activity":    "table_tennis",        // normalisierter Tag (Abschnitt 6)
  "title":       "Tischtennis, locker", // freie Kurzbeschreibung, optional
  "geocell":     "u33dc0",              // Geohash, Präzision aus Angebot ableitbar
  "earliest":    "2026-06-09T18:00:00Z",
  "latest":      "2026-06-09T22:00:00Z",
  "note":        "Halle oder draußen, egal",  // optional, KEINE PII (Skill warnt)
  "created_at":  "2026-06-09T15:12:00Z",
  "expires_at":  "2026-06-09T22:00:00Z",       // = min(latest, created_at + max_ttl)
  "status":      "open"                          // open | matched | closed | withdrawn
}
```

`GET /offers` liefert genau diese öffentlichen Felder. **Keine** Kontaktdaten im Angebot.

### 3.2 Interest (Interessensbekundung)

```jsonc
{
  "id":            "uuid",
  "offer_id":      "uuid",
  "agent_id":      "base64url ed25519 pub (Interessent)",
  "enc_pubkey":    "base64url x25519 pub (Interessent)",
  "sealed_for_owner": "base64url sealed_box(contact_of_interested -> owner.enc_pubkey)",
  "note":          "bin in 20 min da",   // optional
  "status":        "pending",            // pending | accepted | declined | expired
  "created_at":    "..."
}
```

`sealed_for_owner` ist der Kontakt des Interessenten, **versiegelt an den X25519-Key des Anbieters**. Der Broker kann ihn nicht lesen.

### 3.3 Contact (Klartext, nur clientseitig)

Frei wählbar, was zur Koordination reicht — z. B. `{"telegram":"@handle"}` oder ein Einmal-Relay-Token. Wird **nie** unversiegelt übertragen oder gespeichert.

---

## 4. Handshake-State-Machine

```
                 POST /offers
   (nichts) ───────────────────────► Offer.open
                                         │
       B: POST /offers/{id}/interest     │   (B versiegelt B-Kontakt an A)
                                         ▼
                                   Interest.pending ──────────────┐
                                         │                        │
   A: POST /interests/{id}/accept        │   A: POST .../decline   │  Offer expires
   (A versiegelt A-Kontakt an B)         ▼                        ▼  / withdraw
                                   Interest.accepted        Interest.declined / expired
                                         │
                          Offer.status = matched
                                         │
        Freigabe: A erhält sealed_for_owner (B→A) über GET /offers/{id}/interests
                  B erhält sealed_for_interested (A→B) über GET /inbox
                                         │
                  Beide entsiegeln Kontakt, koordinieren privat genauen Treffpunkt
```

- **Opt-in B** = die Interessensbekundung selbst (B hat „ja" gesagt, bevor der Skill sie sendet).
- **Opt-in A** = `accept`. Erst hier entsteht `matched` und A legt `sealed_for_interested` bei.
- **Mehrere Interessenten:** A sieht die Liste via `GET /offers/{id}/interests` und kann eine *oder mehrere* annehmen (z. B. Doppel im TT). Jedes `accept` ist pro Interest. Ein angenommenes Angebot bleibt bis `expires_at` sichtbar, sofern A es nicht via `DELETE` schließt.
- **Ablauf/Rückzug** schließt alle offenen `pending`-Interessen (`expired`).

---

## 5. API

Basis-URL z. B. `https://pingpong.example.org`. Alle Bodies JSON. Signatur-Header gemäß §1.1.

| Methode & Pfad | Signiert | Body / Query | Antwort |
|---|---|---|---|
| `POST /offers` | ✅ | Offer-Felder ohne `id/created_at/expires_at/status` | `201 {offer_id}` |
| `GET /offers` | ✖ | `?cells=u33dc0,u33dc1&activity=table_tennis` | `200 [Offer...]` (öffentliche Felder) |
| `GET /offers/{id}` | ✖ | — | `200 Offer` (öffentliche Felder, für `enc_pubkey` zum Versiegeln) |
| `DELETE /offers/{id}` | ✅ (Owner) | — | `204` |
| `POST /offers/{id}/interest` | ✅ | `{enc_pubkey, sealed_for_owner, note?}` | `201 {interest_id}` |
| `GET /offers/{id}/interests` | ✅ (Owner) | — | `200 [Interest...]` inkl. `sealed_for_owner` |
| `POST /interests/{id}/accept` | ✅ (Owner) | `{sealed_for_interested}` | `200` |
| `POST /interests/{id}/decline` | ✅ (Owner) | — | `200` |
| `GET /inbox` | ✅ | `?since=<iso>` | `200 {events:[...]}` |

### 5.1 `/inbox`-Events (so erfährt der Suchende vom Match)

```jsonc
{ "type": "interest_accepted",
  "offer_id": "...", "interest_id": "...",
  "sealed_for_interested": "base64url sealed_box(contact_of_owner -> interested.enc_pubkey)",
  "ts": "..." }
```

Weitere Typen später: `interest_declined`, `new_interest` (Spiegel zu `GET interests`).

---

## 6. Aktivitäts-Vokabular

Kontrolliertes Tag-Set für zuverlässiges Matching, plus freies `title`. Start:

```
table_tennis, running, cycling, bouldering, tennis, basketball,
football, badminton, swimming, walk, board_games, coffee, beer, other
```

Unbekanntes → `other` + sprechendes `title`. Erweiterbar; der Skill mappt natürliche Sprache („Tischtennis", „zocken") auf Tags.

---

## 7. Anti-Abuse (MVP-Minimum)

- **Rate-Limit** pro `agent_id`: z. B. ≤ 5 offene Angebote, ≤ 30 Requests/min.
- **Anti-Replay** via Timestamp+Nonce (§1.1).
- **Blockliste** pro `agent_id` (Broker-seitig manuell setzbar; später nutzerseitige Reports).
- **Payload-Limits**: `note`/`title` ≤ 200 Zeichen; `sealed_*` ≤ 4 KB.
- **TTL-Cap**: `max_ttl` (Default 24 h) begrenzt `expires_at`. Abgelaufene Angebote werden per Sweep entfernt.

---

## 8. Bewusst (noch) nicht im MVP

Gruppen-Events mit Kapazität · Reputation/Bewertungen · Relay-Chat über den Broker · Friends-of-friends-Sichtbarkeit · Push statt Cron-Poll · Föderation mehrerer Broker.

**Geplant (Richtung):** *Psychologisches/Interessen-Profil je Nutzer* als zusätzliche Match-Dimension. Da Matching client-seitig läuft, genügt dafür ein grober, freiwilliger Profil-Vektor im Angebot (kein Klartext-PII) plus lokaler Kompatibilitäts-Check beim Empfänger — der Broker bleibt unverändert. Siehe §3.1 (`activity`/`title` würden um ein optionales `profile_vector` ergänzt).
