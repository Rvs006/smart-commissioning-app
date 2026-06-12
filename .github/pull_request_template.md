<!--
Thanks for the contribution. Fill in the summary and tick every box that applies.
See CONTRIBUTING.md for setup, the CI gates, and the live-infrastructure honesty rule.
-->

## Summary

<!-- What does this PR change, and why? Link any related issue (Closes #123). -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / cleanup (no behavior change)
- [ ] Documentation
- [ ] Infrastructure / CI

## Checklist

- [ ] Tests pass locally (`python -m unittest discover` for core/backend; `npm test -- --run` for frontend)
- [ ] Lint is clean (`ruff check backend worker core`; `npm run lint`) and typecheck passes (`npm run typecheck`)
- [ ] **Live-infrastructure paths are honestly marked** — no fabricated test/scan/broker results; live-untested paths are labeled as such (see CONTRIBUTING.md and `docs/phase5-onsite-validation.md`)
- [ ] Docs updated where behavior changed (`docs/`, `README.md`)
- [ ] `CHANGELOG.md` `[Unreleased]` section updated if the change is user- or operator-visible
- [ ] Security considered (auth, secret handling, scan-safety, input validation) — see `docs/security-posture.md`

## How was this tested?

<!--
State exactly what you ran. If a live-network / MQTT broker / Postgres / Docker /
hub path is involved, say whether it was run against real infrastructure or only
against fixtures/dry-run. Do not imply a live pass for an untested live path.
-->
