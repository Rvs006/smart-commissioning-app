# ProductOS evaluation for Smart Commissioning Tool

**Research date:** 2026-08-17  
**Upstream revision:** `881b3ec847135056e53bd188282569b4aac64ade` on `main`  
**Decision scope:** whether ProductOS should be used as an SCT dependency or development/product-process aid.  
**Archive handling:** the supplied ZIP was inspected in place with read-only archive enumeration and text reads. It was not extracted or executed.

## Decision

ProductOS is an optional, per-person agent workflow pack. It is useful for
selected product-definition, PRD/roadmap, design-system, code-review, and
security-audit work. It has no SCT runtime value and should not be added to
SCT's Python/Node dependencies, portable executable, Docker image, or tracked
source tree.

The supplied ZIP is an older 1.5.0 snapshot. Current `main` is 1.6.0 and the
1.6.0 changelog records a ZIP-safety rename of 47 files and one folder. Do not
use the ZIP as the installation source. If an individual evaluates ProductOS,
use a reviewed, pinned upstream copy outside tracked SCT files and keep its
generated documents subordinate to SCT's existing records.

## What the project actually is

ProductOS describes a four-phase operating programme: Define, Design, Develop,
and Distribute. The repository supplies checklists, numbered templates,
reference playbooks, and 35 agent skills. Its stated outputs include product,
design, PRD, roadmap, security-audit, deployment, and launch documents. The
system is placed under `productos/`; generated product canon is written under
the host repository's `docs/` folder. [README at the evaluated commit](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/README.md)

The source tree is content and plugin metadata rather than application code:
116 files, approximately 97 Markdown files, 6 JSON files, 2 HTML files, 6 PNG
files, 2 SVG files, and 2 WEBP files. The six JSON files are plugin/marketplace
manifests. The upstream tree contains no `package.json`, Python packaging
manifest, `requirements.txt`, `Dockerfile`, or comparable runtime dependency
manifest. [Upstream tree at the evaluated commit](https://github.com/BuildGreatProducts/product-os-public/tree/881b3ec847135056e53bd188282569b4aac64ade)

The Codex manifest declares `Interactive`, `Read`, and `Write` capabilities and
points at `./skills/`. That makes ProductOS an agent control-plane dependency:
its Markdown instructions can influence how an agent reads and changes a host
repository. Treat every skill as untrusted policy and review it before use.
[Codex manifest](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/.codex-plugin/plugin.json)

## Architecture and install surface

The intended layout is:

```text
SCT repo/
  productos/             ProductOS checklists, templates, playbooks, skills
  docs/                  ProductOS-generated product documents, alongside SCT docs
  AGENTS.md / CLAUDE.md  Setup-wired agent guidance
  .gitignore             productos/ is intended to remain uncommitted
```

The three plugin manifests target Claude Code, Codex, and Cursor. Codex also
gets a local marketplace manifest. The README documents a copy or ZIP extract
for Claude Code and Codex, while Cursor's `/add-plugin` path requires a git
repository with a commit. The setup skill says it writes or appends root
`AGENTS.md`/`CLAUDE.md`, adds `productos/` to `.gitignore`, and moves a shipped
`productos/PLAN.md` to `docs/PLAN.md`; it describes those as the only files it
writes during setup. [README](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/README.md), [setup skill](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/skills/studio-setup/SKILL.md), [Codex marketplace manifest](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/.agents/plugins/marketplace.json)

This is a meaningful SCT integration surface even though no application code is
installed. SCT already maintains byte-identical `AGENTS.md` and `CLAUDE.md`, a
document index, release records, plans, protocol contracts, field acceptance
records, and security guidance. A setup run would need a read-only preview and
a post-change diff check; it must not silently replace SCT's instructions or
create a second canonical roadmap/design/security record. [SCT `AGENTS.md`](../../AGENTS.md), [SCT documentation index](../README.md), [SCT production architecture](../production-architecture.md), [SCT security posture](../security-posture.md)

## Maintenance and versioning

The upstream GitHub API reported the following on 2026-08-17: default branch
`main`, 7 stars, 1 fork, 0 open issues, and a push on 2026-08-17. The commit
listing contains 9 commits, with the evaluated tip at
`881b3ec847135056e53bd188282569b4aac64ade`. The releases and tags endpoints
returned no entries. This is recent activity in a small content repository,
with less release/pinning history than expected from a mature library.
[Repository metadata](https://api.github.com/repos/BuildGreatProducts/product-os-public), [commit history](https://api.github.com/repos/BuildGreatProducts/product-os-public/commits?per_page=10), [releases](https://api.github.com/repos/BuildGreatProducts/product-os-public/releases?per_page=10), [tags](https://api.github.com/repos/BuildGreatProducts/product-os-public/tags?per_page=20)

The evaluated upstream tip identifies itself as 1.6.0. Its changelog says the
1.6.0 path rename was made because plugin ZIP validation rejected spaces and
`&`; the changelog also records the Cursor git-reference requirement. The
local ZIP identifies itself as 1.5.0 in its README, changelog, and plugin
manifests, and still contains the older space-containing paths.
[Current changelog](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/CHANGELOG.md)

Archive evidence:

- Path: `product-os-public.zip` (locally downloaded, not committed)
- SHA-256: `6F44C47DC6A170F2E41B0C879B3AE57AD680A49F49B23C7E2E7DB1A58A701399`
- Contents: 116 files, 50 explicit directory entries, 2,068,236 uncompressed bytes
- No `.git` entry
- Local entries read: `product-os-public-main/README.md`, `START-HERE.md`, `LICENSE.md`, `CHANGELOG.md`, the three plugin manifests, the Codex marketplace manifest, and `setup/AGENTS.md`
- Local source reference: `product-os-public.zip::product-os-public-main/`

## License and redistribution boundary

The project calls itself source-available, not open source. The license grants
one person a personal, non-exclusive, non-transferable right to use and adapt
the materials for that person's own products, including commercial products.
It says generated product outputs belong to the user. It also prohibits
reselling, sublicensing, redistributing, or republishing ProductOS or a
substantial part of it, and prohibits using it to build a competing
product-building system or course. One copy covers one person; teammates are
directed to obtain their own copies.
[ProductOS license](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/LICENSE.md)

Project implication, subject to legal review: a full `productos/` copy in SCT's
tracked repository, a portable bundle, a Docker image, or a customer/team
distribution would conflict with the published redistribution boundary. A
personal, uncommitted copy may fit the grant for individual use, but each
collaborator needs an independent copy and the exact use should be checked
before adoption by a team or external distribution.

## Fit with SCT

| Area | Likely value | Integration cost or boundary |
| --- | --- | --- |
| Product offer, customer, pricing | Medium | Could sharpen SCT's product framing for field engineers, MSIs, controls contractors, building owners, and platform operators. The output needs human review against procurement and field realities. |
| PRD and roadmap synthesis | Medium | Could add a product-level view above SCT's engineering plans. It must not replace release plans, field acceptance checklists, or protocol contracts. |
| Design-system extraction | Medium | Could document the existing React/Vite UI before later UI work. It should produce evidence from the current code, not invent a second visual source of truth. |
| Code review and security audit | Medium to high | The review and audit skills map to SCT's API, worker, database, secrets, authorization, and public-repository concerns. They add review lenses; SCT CI, release checks, and field gates remain authoritative. |
| BACnet, MQTT, UDMI, IP discovery, evidence signing | None | ProductOS contains no protocol drivers, commissioning flows, schema implementation, evidence store, or signing code. |
| Field acceptance and project handover | Low | Its `reply → conversation → signup → activated user → payment` launch ladder is for software-product validation, not proving an installation against an approved register. |
| Runtime and deployment | None | There is no Python/Node runtime package or importable API to add to SCT. |

SCT already has the engineering foundation and document trail that ProductOS
would otherwise help establish: a public README, architecture guidance,
release records, field checklists, protocol plans, and security posture. The
potential gap is cross-cutting product canon and optional agent workflow, not
commissioning capability.

## Recommended scoped use

If an individual chooses to evaluate it after license review, start with the
smallest useful slice and keep generated output subordinate to SCT's current
documents:

1. `studio-define-from-code` for a draft offer and persona, with every
   inference reviewed.
2. `studio-design-design-system-from-code` to record current frontend tokens
   and components without changing the UI.
3. `studio-develop-code-review` for a bounded diff as an additional review.
4. `studio-develop-security-audit` as a separate audit reconciled with SCT's
   security posture, release scans, and authorization model.

Do not begin with the full four-phase programme, a generated mini-launch, a
growth tracker, or a build loop that conflicts with the active SCT plan. Do not
copy substantial ProductOS text or ship its source in SCT artifacts.

## Primary sources

- [Repository](https://github.com/BuildGreatProducts/product-os-public/tree/881b3ec847135056e53bd188282569b4aac64ade)
- [README](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/README.md)
- [CHANGELOG](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/CHANGELOG.md)
- [LICENSE](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/LICENSE.md)
- [Codex manifest](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/.codex-plugin/plugin.json)
- [Claude Code manifest](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/.claude-plugin/plugin.json)
- [Cursor manifest](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/.cursor-plugin/plugin.json)
- [Codex marketplace manifest](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/.agents/plugins/marketplace.json)
- [Setup skill](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/skills/studio-setup/SKILL.md)
- [Define from code skill](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/skills/studio-define-from-code/SKILL.md)
- [Design system from code skill](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/skills/studio-design-design-system-from-code/SKILL.md)
- [Develop code review skill](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/skills/studio-develop-code-review/SKILL.md)
- [Develop security audit skill](https://github.com/BuildGreatProducts/product-os-public/blob/881b3ec847135056e53bd188282569b4aac64ade/skills/studio-develop-security-audit/SKILL.md)
- [GitHub repository metadata API](https://api.github.com/repos/BuildGreatProducts/product-os-public)
- [SCT documentation index](../README.md)
- [SCT root agent guidance](../../AGENTS.md)
- Local archive: `product-os-public.zip` (locally downloaded, not committed)
