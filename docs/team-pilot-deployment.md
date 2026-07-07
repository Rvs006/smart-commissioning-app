# Team Pilot Deployment Guide

How to stand up the Smart Commissioning App for your MSI team as a **controlled
pilot** — so the team can learn the tool, validate the workflow, and surface
gaps — **without** running unvalidated live-network actions against a real
building.

> **Read this first — the boundary.** Everything that does NOT touch a live
> building network is code-complete, hardened, and CI-green. Every LIVE-network
> path (real IP/BACnet/MQTT scans, live MQTT broker/TLS, live config publish +
> rollback) is implemented but **has never run against real hardware**. Do those
> on a lab segment first per
> [phase5-onsite-validation.md](phase5-onsite-validation.md) — not on a
> production building during the pilot.

---

## 1. What the pilot CAN and CANNOT do

### ✅ In scope (safe, validated in dev/CI)
- Configuration (network/BACnet/MQTT/cert/time/backup), with masked secrets.
- Register imports (IP/BACnet/MQTT/asset/mapping/tolerances) from the downloadable
  CSV/XLSX templates.
- **Fixture-based UDMI validation** (bundled fixture — no broker).
- **Dry-run** discovery (IP/BACnet/MQTT) — returns a plan, sends **no packets**.
- Reports & evidence (XLSX/DOCX/ZIP), backup/restore, retention.
- RBAC (viewer/reviewer/engineer/admin), multi-project hub, edge→hub bundle
  export/ingest (file carry).

### 🔴 Out of scope for the pilot (on-site-untested — Phase 5 only)
- Real IP TCP sweep / BACnet Who-Is against live controllers.
- Live MQTT broker connect/subscribe/**TLS** and live payload capture.
- Live MQTT **config publish + rollback** to real gateways.
- Worker mutual-TLS against a real broker; real Redis/Postgres/edge→hub-over-network.

Tell pilot users plainly: **a green run on a dry-run/fixture is not evidence the
live path works.** The authorization checkbox + dry-run gates are there so nobody
fires a live scan/publish by accident.

---

## 2. Pick a profile

| Profile | Use when | Auth | Store |
| --- | --- | --- | --- |
| **Hosted (Docker Compose)** | several users share a server | `api_key` | Postgres + Redis |
| **Portable / local** | one technician on a laptop | `local` (loopback only) | SQLite, jobs inline |

For a **team** pilot, use **Hosted**. (Portable is for the single-user on-site
laptop and binds `127.0.0.1` only — see [quickstart.md](quickstart.md) §B.)

---

## 3. Hosted deploy — step by step

### 3.1 Prerequisite: build the images (NOT yet done in CI/dev)
The Docker images have never been built in this project's dev environment (no
daemon). Build them once on a machine with Docker:

```sh
docker compose -f infra/docker-compose.yml build api worker frontend
docker compose -f infra/docker-compose.yml config   # renders with your .env
```
**STOP** if either fails — fix before going further.

> The `frontend` image bakes in the in-app **Review Comments** widget by default.
> To build a GA image without it, pass the Vite build-time flag at frontend build
> time (`VITE_REVIEW_COMMENTS=false`). It is a build-time variable, **not** a
> compose runtime value — do not add it to `infra/.env` (it has no effect there).

### 3.2 Secrets
```sh
# from the repo root — fills every CHANGE_ME with a crypto-random secret and prints the API_KEY
sh scripts/bootstrap-env.sh        # Windows: pwsh scripts/bootstrap-env.ps1
```
(Manual fallback: `cp infra/.env.example infra/.env`, then generate each
`CHANGE_ME` with `openssl rand -hex 32`.) The script covers the three secrets;
still set `CORS_ORIGINS` in `infra/.env` by hand:

| Variable | Purpose |
| --- | --- |
| `API_KEY` | Bootstrap admin key (`X-API-Key`). Treat as bootstrap-only — see §4. |
| `POSTGRES_PASSWORD` | Postgres password. |
| `REDIS_PASSWORD` | Redis `requirepass`. |
| `CORS_ORIGINS` | The real frontend origin(s), e.g. `https://commissioning.yourco.internal`. NOT the localhost dev default. |

Compose **fails fast** if a required secret is missing.

### 3.3 Bring it up
```sh
docker compose -f infra/docker-compose.yml up -d
```
Startup order is healthcheck-gated: Postgres + Redis become healthy, the API
applies Alembic migrations on start (schema head `d1f2a3b4c5d6`), then the worker
starts (it now has a Redis-ping healthcheck). App is served behind nginx on
`127.0.0.1:8080`, API on `127.0.0.1:8000` — **loopback-bound on purpose.**

### 3.4 Put TLS + isolation in front (REQUIRED for a multi-user deploy)
- A reverse proxy terminates **TLS**; only 443 is public.
- API, Postgres, Redis, and `/metrics` are **NOT** publicly reachable
  (`/metrics` is intentionally unauthenticated — firewall/network-isolate it).
- `/docs`, `/redoc`, `/openapi.json` already return **404** in `api_key` mode.

### 3.5 Smoke test
```sh
SC_API_KEY="<your API_KEY>" scripts/smoke_local.sh http://127.0.0.1:8000
SC_API_KEY="<your API_KEY>" scripts/phase5_preflight.sh http://127.0.0.1:8000
```
Both must exit 0 (preflight's broker-TCP check may legitimately fail if the
broker is unreachable from the server — that is expected off-site).

---

## 4. Provision per-user accounts (do NOT share the bootstrap key)

The `API_KEY` authenticates as a synthetic **admin** with no per-user
accountability, and the frontend stores whatever key a user enters in browser
`localStorage`. For a real team, create named users so actions are attributable
and the shared key is bootstrap-only:

```sh
# As the bootstrap admin (X-API-Key: $API_KEY):
curl -sX POST https://<host>/api/v1/users -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","role":"engineer"}'
# -> returns the plaintext key; hand it to that user. It is DISPLAYED only this
#    once (only a hash is stored) — but the key itself does NOT expire: it keeps
#    working until the user is deactivated or an admin re-issues it.
```
Roles: `viewer` < `reviewer` < `engineer` < `admin`. Discovery/validation/publish
runs require **engineer+**.

- **Lost key?** Keys can never be retrieved, only replaced: an admin can
  re-issue one via `POST /api/v1/users/<id>/key` (or the Users page "Re-issue
  key" button). The old key stops working immediately and the new plaintext is
  shown once, exactly like at creation.
- **Last-admin guard**: you cannot deactivate/demote the last active admin user
  (returns 409). Recovery if you ever lock out all admin rows: drop to
  `AUTH_MODE=local` (loopback) or the shared bootstrap key.
- **Secrets at rest**: uploaded cert/key material is Fernet-encrypted (`0600`),
  the secrets directory is owner-only (`0700`), and broker passwords / inline
  keys passed as run parameters are **redacted** from API responses. Back up the
  `.secret_store_key` and the evidence signing key — losing them makes secrets
  unreadable / evidence unverifiable.

---

## 5. Pilot runbook for users (hand this to the team)

1. Sign in with your personal key (Set API key).
2. Fill in Configuration; download the import templates; upload your registers.
3. Run a **fixture** UDMI validation and a **dry-run** discovery to learn the flow.
4. Generate a report; confirm it downloads and contains your run's findings.
5. **Do not** tick "I am authorized to scan this network" or run a live MQTT
   config publish against a real building. If you need to, that is a Phase 5
   on-site activity with a change window and lab validation first.
6. Log issues/UX feedback — that is the point of the pilot.

---

## 6. Escalating from pilot → production

The pilot does not make the tool production-ready for live commissioning. Before
trusting it against a real building:

1. Work through [phase5-onsite-validation.md](phase5-onsite-validation.md): each
   live surface, dry-run → lab segment → real building with authorization.
2. See the live-surface map in
   [phase5-live-surface-inventory.md](phase5-live-surface-inventory.md) and the
   operations [runbook.md](runbook.md).

Only after the Phase 5 safety-critical drills (throttle/authorize/cancel,
lab-validated BACnet, lab-validated config rollback) pass should the team run
active scans or live publishes on a production building.
