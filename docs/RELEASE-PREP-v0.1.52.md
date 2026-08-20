# Release prep hand-off — v0.1.52

Hand-off checklist for a **human** to finish the v0.1.52 release. Everything
here is honest scaffolding: the version is pinned and the identity/security
gates exist, but the release-content docs and the content-assertion gate
scripts are **not written yet**. They must be authored from their v0.1.50
counterparts and filled with the **real v0.1.52** release facts — never
blind-cloned, never fabricated.

Branch: `release/v0.1.52` (HEAD already on it). Canonical version pin:
`0.1.52` in `core/smart_commissioning_core/__init__.py`.

> **Authorization rule.** Do **not** merge, tag, or publish this release
> without the user's explicit authorization. Field acceptance is real on-site
> testing and is recorded **privately** per `AGENTS.md` — never commit
> fabricated field-acceptance, evidence, or validation content.

---

## 1. Already scaffolded (done — verify, don't redo)

- Version pin `0.1.52` in `core/smart_commissioning_core/__init__.py`
  (surfaced through `backend/app/versioning.py`).
- `scripts/test_v0152_version_identity.py`
- `scripts/scan_v0152_release_secrets.py`
- `scripts/test_v0152_security_scan.py`
- `docs/migration-rollback-v0.1.52.md`
- `CHANGELOG.md` — `[0.1.52] - 2026-08-20` section present.
- Workflows re-pinned to v0.1.52: `.github/workflows/release-gates.yml`,
  `.github/workflows/windows-portable.yml` (default `version: v0.1.52`, and the
  gate already invokes the v0152 scripts listed below — so the gate will
  **fail** until section 2 and 3 are complete).
- `AGENTS.md` / `CLAUDE.md` hand-off text updated (v0.1.52 is the current
  candidate; field acceptance open, recorded privately).

---

## 2. Governance docs to author

Author each from its v0.1.50 counterpart as the template, then replace every
v0.1.50 fact with the **real v0.1.52** content (changelog scope, Brief/Learning
guidance, migration notes, evidence hashes). Copy structure, not claims.

| Create (missing) | Template to copy from |
| --- | --- |
| `docs/release-notes-v0.1.52.md` | `docs/release-notes-v0.1.50.md` |
| `docs/release-validation-v0.1.52.md` | `docs/release-validation-v0.1.50.md` — **TODO: real validation results only** |
| `docs/docker-deployment-rollback-v0.1.52.md` | `docs/docker-deployment-rollback-v0.1.50.md` |
| `docs/v0.1.52-evidence-manifest.md` | `docs/v0.1.50-evidence-manifest.md` — **TODO: real captured evidence only** |
| `docs/v0.1.52-field-acceptance-checklist.md` | `docs/v0.1.50-field-acceptance-checklist.md` — **RECORDED PRIVATELY per `AGENTS.md`. Do NOT commit fabricated field-acceptance content.** |

`docs/migration-rollback-v0.1.52.md` already exists (section 1).

---

## 3. Gate scripts to author

These are **not** identity checks — they assert **v0.1.52-specific release
content**: that the changelog sections, Brief/Learning operator guidance, the
evidence manifest, and the release/docker docs actually say what v0.1.52 ships.
They must be pointed at the **real v0.1.52 artifacts from section 2** and pass
against real content. Cloning the v0150 body without updating the asserted
strings will either pass falsely or fail — update them deliberately.

| Create (missing) | Template to copy from | Asserts |
| --- | --- | --- |
| `scripts/check_v0152_release_contracts.py` | `scripts/check_v0150_release_contracts.py` | Static fail-closed contracts across the v0.1.52 release paths (version, notes, migration, validation, docker docs, evidence validator/test names). |
| `scripts/check_v0152_docker_contracts.py` | `scripts/check_v0150_docker_contracts.py` | Docker deployment/rollback contract content for v0.1.52. |
| `scripts/test_v0152_release_evidence.py` | `scripts/test_v0150_release_evidence.py` | The v0.1.52 evidence manifest/content shape is present and correct. |
| `scripts/validate_v0152_release_evidence.py` | `scripts/validate_v0150_release_evidence.py` | Validates the real captured v0.1.52 evidence bundle. |
| `scripts/test_v0152_real_mqtt_socket.py` | `scripts/test_v0150_real_mqtt_socket.py` | Real-broker MQTT socket gate (thin wrapper over the retained `RealMqttSocketGate`). |

Note: the v0150 contract scripts delegate to a shared base
(`check_v0128_release_contracts`, `test_v0142_real_mqtt_socket`) and only pass
the version-specific paths/strings — so the real work is getting those v0.1.52
arguments and asserted content right, not rewriting logic.

After authoring, the whole set is already wired into `release-gates.yml` (ruff
line + the `python scripts/*_v0152_*.py` invocations). No workflow edit needed.

---

## 4. Run the gates (once sections 2–3 are complete)

1. **Release gates** — Actions → *v0.1.52 Release Gates* → Run workflow
   (`workflow_dispatch`):
   - `version`: `v0.1.52`
   - `release_sha`: the full **40-character** SHA that equals current
     `origin/main` (the workflow asserts `HEAD == release_sha == origin/main`).
2. **Windows portable** — Actions → *windows-portable* → Run workflow:
   - `version`: `v0.1.52`
   - `release_sha`: same full 40-char SHA built from current main.
3. **Publish** — from the CI-built artifact, run
   `scripts/release-portable.ps1` (Windows PowerShell 5.1). It downloads the CI
   `SmartCommissioningApp-windows-portable` artifact archive, verifies it, and
   attaches it to the GitHub Release. Ship the CI artifact archive itself; do
   not rebuild the exe locally.

---

## 5. Do not skip

- Field acceptance is private and real — never fabricate it into a committed
  doc.
- Do not merge/tag/publish without the user's explicit authorization.
- This repo is **public** — keep site/network/personnel/commercial detail out
  of any doc or commit added above.
