# Changelog

All notable changes to the Smart Commissioning App are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

This is the pre-1.0 development line. Entries below summarize the program by
theme and are derived from the actual git history (`git log --oneline`), from
the MVP scaffold baseline through the phase 0–4b production-hardening work.

### Added

- **Production scaffold (MVP baseline)** — multi-service layout: React + TypeScript
  + Vite frontend, FastAPI backend, Dramatiq worker, `infra/` Docker Compose
  stack, and `docs/`.
- **Shared core package** — extracted `smart_commissioning_core`, a shared
  package for UDMI validation and MQTT logic consumed by both the backend and
  the worker.
- **Persistence** — moved runs, configuration, and imports to SQLAlchemy
  persistence (with Alembic migrations).
- **Real discovery and validation engines** — implemented real discovery and
  validation engines with scan-safety controls (replacing mocked flows).
- **Frontend wired to real data** — connected the frontend to real discovery,
  run, and validation data.
- **Observability, integrity, and DR** — structured logging, the Prometheus
  metrics surface, evidence integrity (SHA-256 + Ed25519 signing),
  backup/restore/retention, and Server-Sent Events (SSE).
- **Edge → hub synchronization** — signed, immutable edge-to-hub run + evidence
  record synchronization (per-edge Ed25519 identity, watermark-based ingest).
- **CI/CD and tooling** — lint, typecheck, and test tooling plus a GitHub
  Actions CI workflow with `python`, `frontend`, and `sbom` jobs.
- **SBOM** — additive SBOM + license-inventory job and generated inventory
  (`docs/SBOM.generated.md`).
- **On-site validation checklist** — Phase 5 checklist (`docs/phase5-onsite-validation.md`)
  enumerating the live-network / real-infrastructure steps that must pass before
  production rollout.
- **Electracom UI theme + in-app Brief & Learning** — restyled the operator
  console to the Electracom "Smart Point" look & feel (warm palette, teal accent,
  brand logo) with a **light/dark theme toggle**, and added two standalone
  onboarding surfaces: a **Product Brief** (`/#/brief` — Basics, Key Features,
  Section Reference, and a role-based Guided Tour) and a **Learning** path
  (`/#/learning` — role-based walkthroughs). Content is scoped to this app's own
  modules — theme and format only, no feature copy.
- **Step-based module layout** — each module page is now split into a
  **Setup / Run / Results** segmented flow so the operator works one screen at a
  time instead of scrolling every panel at once. The step auto-advances (Run when
  a run is queued, Results on success) and manual step clicks always override.
- **Workflow-stage navigation** — the module tabs are now grouped under the stage
  they belong to (**Configure / Discover / Validate / Report / Operate**) instead
  of a flat row of equal tabs, so the nav mirrors the order of the job.
- **Reviewer guide** — `docs/review-guide.md`: a single page for an engineer
  picking up the app to review — how to run it (frontend-only or full-stack
  Docker), what to look at, and what is in scope for this round. Linked from the
  README header and the documentation table.

### Changed

- Refactored the standalone UDMI payload validator into the shared core package
  with an app-level API, a shared issue model, and persistent run history.
- Aligned the frontend CI Node version to the lockfile's npm and raised the test
  timeout to stabilize the frontend job.
- Restyled the whole operator console via a design-token override (warm cream +
  teal Electracom palette) with a dark mode, and ran a dark-mode legibility pass
  (review-comments launcher, badge, and card elevation on the dark page).

### Security

- Added authentication, secret encryption at rest (Fernet-based secret store
  holding `secret://` references in the database, never secret bytes), and infra
  hardening.
- Added evidence integrity via SHA-256 hashing and detached Ed25519 signatures,
  reused by both the evidence-pack and edge → hub sync paths.

### Fixed

- Fixed a secret-corruption bug, a dead error panel, and a fixture path-traversal
  issue.
- Regenerated the frontend lockfile with **npm 10** for cross-npm compatibility,
  and regenerated it to include the full esbuild dependency tree.
- Replaced placeholder/marketing figures on the Product Brief "at a glance"
  stats (`∞`, `100%`, "console for every site") with honest, verifiable facts
  (deployment profiles, access roles, evidence signature schemes), and fixed the
  stat value/label rendering inline (now stacked) so they no longer overlap.
- Failed/cancelled runs stay on the module **Run** step (where the monitor shows
  the error) instead of auto-advancing to an empty Results view.

### Removed

- Removed the dead UI prototypes and the zip-inspector dev tool (still available
  in git history at the baseline commit `3471050`).

### Not yet validated

The following paths were implemented and unit-tested but developed **without
access to the corresponding real infrastructure**, and require on-site
validation before production rollout:

- **Live-network discovery/scanning** — active IP sweep and BACnet Who-Is
  against a real BMS/OT network (only ever run in dry-run / offline fixtures).
- **Live MQTT broker** — real broker connectivity, TLS, and UDMI message capture.
- **Postgres** — the hosted profile's PostgreSQL system of record under load.
- **Docker image build** — building and running the `infra/` Compose stack on a
  real Docker daemon.
- **Edge → hub sync over the wire** — synchronization against a remote staging
  hub.

See [docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md) for the
full checklist.

[Unreleased]: https://github.com/Rvs006/smart-commissioning-app/commits/main
