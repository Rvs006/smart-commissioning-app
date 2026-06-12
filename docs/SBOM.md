# Software Bill of Materials (SBOM) and License Inventory

This document inventories the third-party Python dependencies of the Smart
Commissioning App across its three first-party distributions
(`smart-commissioning-core`, `smart-commissioning-api`,
`smart-commissioning-worker`), with the license of each and a flag on any
license that warrants review.

Scope: this is a **Python runtime** SBOM. The frontend (`frontend/`, npm) is a
static Vite build served by nginx and is inventoried separately by
`npm` (`npm ls --all`, `npm sbom`); it is not covered here. The optional
`bacpypes3` BACnet extra is listed but is **not installed** in this
environment (see `docs/protocol-conformance.md`).

## How this was generated

`scripts/generate_sbom.py` walks the declared `[project].dependencies` of the
three `pyproject.toml` files plus the installed transitive tree, reading each
package's license from its installed metadata via the standard-library
`importlib.metadata` (no external SBOM tool required, so it runs in CI without
a version-pinning burden):

```bash
# CycloneDX JSON + run the allowlist gate (exit 2 on a disallowed license)
python scripts/generate_sbom.py --json deliverables/sbom.json --check

# Refresh the generated markdown table block (committed alongside this file)
python scripts/generate_sbom.py --markdown docs/SBOM.generated.md
```

The script emits CycloneDX-1.5-flavoured JSON and a markdown table. It is
**honest about what is installed**: a declared-but-absent dependency (the
optional `bacpypes3` extra here) is listed with `installed: false` and no
fabricated version or license; the license string is whatever the package
metadata declares (`License-Expression` per PEP 639, the free-form `License`
field, or a `License ::` trove classifier) — none is inferred.

### Alternative: pip-licenses / cyclonedx-py

The two common off-the-shelf tools produce richer output but add a
version-pinning burden in CI. If you prefer them, install into the SBOM venv
and run:

```bash
pip install pip-licenses cyclonedx-bom
# Human-readable license table
pip-licenses --with-urls --format=markdown
# CycloneDX SBOM (per environment)
cyclonedx-py environment -o deliverables/sbom.cyclonedx.json
```

These are **not pinned** in `pyproject.toml` and the CI job does not depend on
them; the in-repo `scripts/generate_sbom.py` is the supported path. The
optional `sbom` dependency group in each `pyproject.toml` documents the pinned
versions if you want CI to use the external tools instead.

## Runtime dependency licenses

Each runtime dependency, its license, and what it is used for. Versions reflect
the environment in which this inventory was last generated; the generated table
in `docs/SBOM.generated.md` (or the `--json` output) is authoritative for exact
pinned versions at any point in time.

| Package | License | Used for |
| --- | --- | --- |
| fastapi | MIT | HTTP API framework (routing, validation, OpenAPI). |
| starlette | BSD-3-Clause | ASGI toolkit under FastAPI (middleware, routing). |
| uvicorn | BSD-3-Clause | ASGI server that runs the API. |
| pydantic | MIT | Typed request/response and domain models. |
| pydantic-core | MIT | Compiled validation core for Pydantic v2. |
| pydantic-settings | MIT | Environment-variable settings loading. |
| sqlalchemy | MIT | Database access layer (runs, imports, configuration). |
| alembic | MIT | Schema migrations applied on API startup. |
| dramatiq | **LGPL-3.0-or-later** | Background job queue (worker actors over Redis). |
| redis | MIT | Redis client (Dramatiq broker + readiness ping). |
| psycopg | **LGPL-3.0-only** | PostgreSQL driver (hosted profile `DATABASE_URL`). |
| psycopg-binary | **LGPL-3.0-only** | Prebuilt libpq bindings for psycopg. |
| cryptography | Apache-2.0 OR BSD-3-Clause | Fernet encryption of secret material at rest. |
| openpyxl | MIT | XLSX import parsing and report generation. |
| prometheus-client | Apache-2.0 AND BSD-2-Clause | `/metrics` exposition. |
| httpx | BSD-3-Clause | HTTP client (API/tests). |
| python-multipart | Apache-2.0 | `multipart/form-data` parsing for uploads. |
| bacpypes3 *(optional, not installed)* | MIT | Real BACnet/IP transport — UNVALIDATED extra. |

Common transitive dependencies (`anyio`, `sniffio`, `idna`, `certifi`,
`h11`, `httpcore`, `click`, `colorama`, `MarkupSafe`, `Mako`, `greenlet`,
`cffi`, `pycparser`, `typing-extensions`, `annotated-types`,
`typing-inspection`, `python-dotenv`, `PyJWT`, `et-xmlfile`, `zipp`,
`importlib-metadata`, `tzdata`) are all permissive: MIT, BSD, Apache-2.0,
PSF-2.0, or MPL-2.0 (`certifi`). The full list with versions is in the
generated table.

## License risk assessment

The allowlist used by the CI gate is permissive + weak-copyleft:
**MIT, BSD, Apache-2.0, ISC, PSF, MPL, and LGPL**. Strong copyleft
(**GPL / AGPL**) is intentionally *not* on the allowlist, so the gate fails if
such a dependency is ever introduced.

Flagged for review (present, but accepted):

- **`dramatiq` — LGPL-3.0-or-later.** Weak copyleft. Used as an unmodified,
  separately-installed library (imported, not statically linked / vendored), so
  the LGPL's reciprocal obligation is limited to offering the library's own
  source — which is publicly available upstream. **Acceptable** for an app that
  ships it as a dependency. Obligation: do not modify-and-redistribute the
  dramatiq source without offering those modifications.
- **`psycopg` / `psycopg-binary` — LGPL-3.0-only.** Same reasoning: the
  PostgreSQL driver is used as an unmodified library. **Acceptable.** Only
  relevant to the hosted (Postgres) profile; the portable edge profile uses
  SQLite (stdlib) and does not ship psycopg at runtime.
- **`certifi` — MPL-2.0.** File-level weak copyleft over certifi's own files
  only; used unmodified. **Acceptable.**

No **GPL** or **AGPL** dependency is present. The previously-considered
object-storage service (**MinIO**, whose server is AGPL-3.0) was removed from
the stack — nothing in `core/`, `backend/`, or `worker/` references it (see
`infra/README.md`). Reports are generated in-memory and evidence/import files
are written to local disk or the `api_runtime` volume, so no S3/MinIO client is
required.

Action items if redistribution model changes (e.g. shipping a closed binary
that statically embeds these libraries): re-review the LGPL components, since
static linking changes the obligation. As shipped (dynamically-imported Python
packages), the current posture is clean.

## Frontend (npm) note

The React frontend's dependency licenses are governed by `frontend/package.json`
/ `package-lock.json` and are out of scope for `scripts/generate_sbom.py`.
Generate that inventory with `npm sbom --sbom-format cyclonedx` or
`npm ls --all` from `frontend/`. There is no GPL/AGPL package known in the
React/Vite/TanStack toolchain (all MIT/ISC/BSD), but run the npm tooling to
confirm before a release.
