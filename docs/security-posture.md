# Security Posture

Last reviewed: Monday, 2026-06-29.

The security model of the Smart Commissioning App: threat model summary, auth
model, secret handling, scan-safety controls, and an honest IEC 62443
**alignment** note (alignment, not certification). Accurate to
`backend/app/core/auth.py`, `backend/app/core/config.py`,
`backend/app/services/configuration_service.py`,
`core/smart_commissioning_core/engines/safety.py`, and the discovery/validation
routes.

## 1. Threat model summary

### Assets

- **Building configuration** — network/BACnet/MQTT settings, imported registers,
  expected-asset schedules. Disclosure or tampering misdirects commissioning.
- **Credentials** — MQTT broker username/password, BACnet/TLS client
  certificates and private keys, the API key, Redis/Postgres passwords, the
  secret-store Fernet key, and the evidence signing key.
- **Evidence** — captured payloads, validation issues, signed report bundles.
  Integrity matters: evidence is a record used for handover/acceptance.

### Trust boundaries

- **Operator ↔ API.** The HTTP boundary. Enforced by the auth dependency
  (`local` loopback vs `api_key`).
- **Edge ↔ Hub.** The portable laptop (edge) holds its own SQLite + secrets and
  trusts only loopback; the hosted hub is a shared server reached over the
  network (behind a TLS-terminating reverse proxy). They are distinct trust
  zones — edge data does not implicitly trust the hub and vice versa. Across this
  boundary the hub trusts an edge only via a pinned-key **trusted-edges
  allowlist** and accepts only signed, hash-verified, immutable run bundles
  (untrusted or tampered bundles are rejected and nothing is written); see
  `docs/sync-architecture.md`.
- **App ↔ OT / site network.** Discovery/publish engines actively touch a live
  building-management / operational-technology network (BACnet broadcasts, IP
  probes, MQTT). This is the **highest-consequence boundary**: an unauthorized or
  aggressive scan can disrupt controllers, trip alarms, or flood a broker. The
  scan-safety controls (section 4) exist for exactly this boundary.
- **App ↔ infrastructure.** Postgres, Redis, the MQTT broker — reached over the
  compose network (hosted) with password auth; never exposed publicly by the app
  (loopback binding + reverse proxy in front).

### Primary threats considered

- Unauthenticated access to project data / configuration → auth gate, fail-closed
  api_key mode, loopback-only edge.
- Credential leakage via API responses or logs → masking, `secret://` indirection,
  credential-free error/probe messages.
- Secret material at rest on a lost edge laptop → Fernet encryption at rest +
  full-disk encryption guidance (`docs/backup-restore.md`).
- Accidental or unauthorized active scan against an OT network → authorization
  gate + dry-run + throttle + cancellation.
- Unintended persistent change to a controller via MQTT config publish →
  publish authorization gate + rollback of the prior config value.
- Supply-chain license/dependency risk → SBOM + license gate (`docs/SBOM.md`).

## 2. Authentication model

Two modes, selected by `AUTH_MODE` and enforced by
`backend/app/core/auth.py:require_auth` (a FastAPI dependency on every `/api/v1`
route except health):

- **`local` (default; edge profile).** Only **loopback** clients (`127.0.0.0/8`,
  `::1`, IPv4-mapped loopback) are accepted, matching the portable desktop where
  uvicorn binds `127.0.0.1`. If an `API_KEY` is *also* configured, a request
  presenting the valid key is accepted from any address. In-process ASGI calls
  (no transport client) are treated as loopback.
- **`api_key` (hosted profile; compose default).** Every request must present the
  key via `X-API-Key` or `Authorization: Bearer <key>`. Key comparison is
  constant-time (`secrets.compare_digest`). With **no key configured the API
  fails closed** — every authenticated request is rejected with 401, and startup
  logs a warning.

Properties:

- **Loopback edge trusts any co-resident process.** In `local` mode a loopback
  request with no key resolves to a synthetic **ADMIN** principal, so on the edge
  the RBAC `require_role()` gates are **not** a security boundary — any local
  process can reach any endpoint as ADMIN. Those gates bite only under
  `api_key` mode with per-user keys (a rejected/inactive key still 401s).
- **Health endpoints are exempt** so liveness/readiness probes work without
  credentials (they expose no project data).
- The configured key is **never echoed** back; 401 detail messages are generic.
- **No cookies / sessions** — header auth only, so credentialed CORS stays
  disabled (`allow_credentials=False`) and CORS is restricted to
  `CORS_ORIGINS`.
- **Schema endpoints** (`/docs`, `/redoc`, `/openapi.json`) are gated off (404)
  in `api_key` mode so the full API surface is not disclosed to unauthenticated
  callers on a hosted deployment; available in loopback `local` mode.
- **Transport security:** the app does not terminate TLS. Hosted deployments
  MUST front it with a TLS-terminating reverse proxy; everything binds loopback
  by default.

## 3. Secret handling

Implemented in `backend/app/services/configuration_service.py` and
`backend/app/core/runtime.py`.

- **Encryption at rest (Fernet).** Uploaded CA certs, client certs, and private
  keys are encrypted with a **Fernet** key (`cryptography`) and written
  owner-only (0o600 best-effort; on Windows real isolation comes from the ACL on
  the secrets root). The key (`.secret_store_key`) is generated on first use and
  stored under the secrets root (`SMART_COMMISSIONING_SECRETS_ROOT`, default
  `backend/runtime/secrets/`).
- **`secret://` indirection.** The database stores only opaque `secret://`
  references — never the secret bytes. The configuration payload carries the
  reference; the material is resolved from disk only by internal consumers (e.g.
  the MQTT connection builder) via the `mask_secrets=False` path.
- **Masking on every API response.** Password-kind fields are returned as an
  all-asterisk sentinel (`********`) and `secret://` references are returned as
  the reference, never the cleartext. The reveal path (`mask_secrets=False`) is
  internal only.
- **Write-only feel for secrets.** Posting a secret stores it and returns only
  masked metadata; re-submitting the asterisk sentinel means "keep the stored
  secret" rather than overwriting it with asterisks.
- **Credential-free errors and probes.** Broker/transport errors are mapped to
  coarse status labels (`tls_error`, `authentication_error`, `broker_timeout`,
  `broker_unreachable`) because raw exception text may embed a connection URL or
  auth detail; the readiness Redis check reports host[:port] only, never the full
  `redis_url`.
- **Rotation.** Per-secret rotation (API key, Redis/Postgres passwords,
  secret-store key, signing key, MQTT/BACnet material) is documented in
  `docs/runbook.md` section 7. The secret-store key rotation is manual and
  consequential — back up first (`docs/backup-restore.md`).

## 4. Scan-safety controls

Active-scan engines (IP, BACnet, MQTT discovery, and MQTT config publish) touch a
live network. The controls below make "did a human authorize this?" an explicit,
testable precondition and limit blast radius.

- **Authorization gate** (`core/smart_commissioning_core/engines/safety.py`,
  `require_scan_authorization`). A real scan runs only when the run's
  `parameters` carry either `authorized = true` (shorthand) **or** the
  audit-friendly `scan_authorization = {authorized: true, authorized_by: "<who>"}`
  (preferred — it records *who* authorized). Anything else raises
  `ScanNotAuthorized` (which carries no parameter contents, so it is safe to
  surface). The API enforces the same contract at the boundary
  (`discovery.py`, `validation.py`) returning **403** with an actionable message;
  the engine re-checks as defense in depth.
- **Dry-run** (`build_dry_run_plan`). A `dry_run = true` run performs **no I/O** —
  no socket opened, no packet/broadcast emitted. It returns the concrete targets
  and actions it *would* execute under `result_summary_extra["dry_run_plan"]`.
  Dry-run is allowed without authorization because it is side-effect free; the
  real scan is gated **after** the dry-run branch.
- **Throttle** (`config.py`, applied to the engines). Conservative defaults —
  `scan_max_concurrency=16`, `scan_rate_limit_per_sec=10`,
  `scan_connect_timeout_s=5` — so a scan pointed at a real building network
  cannot overwhelm controllers or a broker. Per-run parameters may narrow these
  but should not exceed site policy.
- **Cooperative cancellation** (`POST /api/v1/runs/{run_id}/cancel`). Sets the
  run's `cancel_requested` flag; engines poll it between dispatches and stop
  early, returning partial results and flipping the run to `cancelled`. A
  finished run is unaffected.
- **Config-publish rollback** (`POST /api/v1/validation/mqtt-config/runs/{run_id}/rollback`).
  A live MQTT config publish is an active write, so it is gated by the **same
  authorization contract** as a scan. The forward publish records the prior
  retained config value; rollback re-publishes that value to the same topic
  (also authorization-gated — a rollback is itself a live write). If no prior
  value was captured, rollback returns 400 (nothing to roll back to).

> **Honesty:** the real network probes, the bacpypes3 BACnet path, and the live
> MQTT publish/capture require on-site validation against actual hardware/brokers
> (see `docs/protocol-conformance.md`). The authorization gate, dry-run plan
> shape, throttle wiring, cancellation flag, and rollback bookkeeping are
> exercised by unit tests in-process (no live infra); the live network behaviour
> behind them is not asserted here.

## 5. IEC 62443 alignment (alignment, not certification)

This is an **honest self-assessment of alignment** with relevant IEC 62443
foundational requirements for a commissioning tool that touches OT networks. It
is **not** a certification, audit, or conformance claim, and no third party has
assessed it. Framed as "what the app addresses vs what is open / out of scope".

| IEC 62443 area (FR) | Addressed in this app | Open / depends on deployment |
| --- | --- | --- |
| FR1 Identification & authentication control | API auth (`local` loopback / `api_key`, fail-closed); constant-time key compare; app-level user/role model. | Enterprise SSO / external IdP integration is not implemented; pair API keys and app roles with deployment-level access controls. |
| FR2 Use control (authorization) | Active-scan + config-publish **authorization gate** with an auditable `authorized_by`; 403 at the boundary. | Authorization is per-run parameter, not tied to an authenticated identity; pair with deployment-level access control. |
| FR3 System integrity | Evidence signing/hash verification (parallel-phase evidence/verify path); migrations owned by the API; in-memory report generation. | TLS termination is delegated to a reverse proxy (not in-app); signing-key custody is operational. |
| FR4 Data confidentiality | Fernet encryption of secret material at rest; `secret://` indirection; masking in API responses; credential-free errors/logs; TLS for MQTT supported. | Disk/volume encryption is the deployer's responsibility (esp. the edge laptop). No field-level DB encryption beyond secrets. |
| FR5 Restricted data flow | Loopback-only binding by default; CORS restricted to `CORS_ORIGINS`; `/metrics` and schema endpoints kept off public exposure; conservative scan throttle limits OT traffic. | Network segmentation (VLAN/firewall between tool, OT, and hub) is a site-network control outside the app. |
| FR6 Timely response to events | Structured JSON logs with `request_id`/`run_id`; `/metrics` for rate/latency/runs-by-status; readiness probe; cancellation; rollback. | No built-in SIEM/alerting — wire the metrics/logs into your monitoring (`docs/observability.md`). Audit-trail of config changes/runs/exports is recorded; long-term retention is operational. |
| FR7 Resource availability | Throttle + timeouts prevent self-inflicted DoS on OT; readiness gating; worker horizontal scaling. | Broker/DB availability and backups are deployment concerns (`docs/backup-restore.md`). |

Open items, stated plainly: **RBAC exists but is inert on the loopback edge**
(`UserRepository`, `Role`, `require_role`); it is a boundary only under `api_key`
mode with per-user keys — in `local` mode any co-resident loopback process is
trusted as ADMIN. The app does **not terminate TLS** itself (delegated to a
reverse proxy), network **segmentation is a site control** outside the app, and
the **bacpypes3 / live broker** paths are unvalidated against real hardware. None
of these are claimed as solved.
