# Broker-Deployment (Hostinger VPS + Traefik)

Der Broker läuft als Docker-Compose-Projekt hinter dem vorhandenen Traefik auf
VPS `1601314` (`187.77.70.112`, Ubuntu 24.04 + Docker + Traefik).

## Zielbild

```
Internet ──TLS──▶ Traefik (letsencrypt, HTTP-Challenge)
                    │  Host(`pingpong.kitescout.tech`)
                    ▼
                 broker:8000  (uvicorn, 1 Worker)
                    │
                 pingpong-data  (Volume, SQLite)
```

## Voraussetzungen

1. **DNS:** A-Record `pingpong.kitescout.tech → 187.77.70.112` (analog zu den
   bestehenden Records). Nötig **bevor** Traefik das Let's-Encrypt-Zertifikat per
   HTTP-Challenge holen kann.
2. **Code erreichbar für den Build:** Traefik/Docker baut das Image aus dem
   `git build context` in `pingpong-broker.compose.yml`. Dafür muss `broker/`
   in einem für den VPS erreichbaren Git-Repo liegen (z. B. `github.com/0xaaronx0/pingpong`,
   Branch `main`). Alternativ ein vorgebautes Image (siehe unten).

## Schritte (via Hostinger-MCP)

1. **DNS** — `DNS_updateDNSRecordsV1` für `kitescout.tech`, Record `pingpong`
   (Typ `A`, Content `187.77.70.112`, TTL 300), `overwrite=false`.
2. **Deploy** — `VPS_createNewProjectV1`:
   - `project_name = pingpong-broker`
   - `content = ` Inhalt von `pingpong-broker.compose.yml`
   - `environment = ` (optional, Defaults stehen im Compose)
3. **Verifizieren**
   - `VPS_getProjectListV1` → Container `running`
   - `GET https://pingpong.kitescout.tech/healthz` → `{"ok": true}`
   - Smoke-Test mit dem Skill (`PINGPONG_BROKER_URL=https://pingpong.kitescout.tech`).

## Alternative: vorgebautes Image (wie kitescout)

Statt git-Build ein Image via GitHub Actions nach `ghcr.io/0xaaronx0/pingpong-broker`
pushen und im Compose `image:` statt `build:` referenzieren. Mehr Setup (CI), dafür
schnellere Redeploys und Watchtower-Auto-Update (`com.centurylinklabs.watchtower.enable=true`).

## Betriebshinweise

- **Ein Worker** ist Pflicht, solange Nonce-Store/Rate-Limit in-memory sind
  (`app.py`). Für horizontale Skalierung zuerst nach SQLite/Redis verlagern.
- **Backup:** das `pingpong-data`-Volume enthält die SQLite-DB.
- **Redeploy nach Code-Änderung:** `VPS_updateProjectV1` (zieht/baut neu).
