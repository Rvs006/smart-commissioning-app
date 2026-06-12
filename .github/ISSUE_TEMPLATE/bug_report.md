---
name: Bug report
about: Report something that is broken or behaving incorrectly
title: "[Bug]: "
labels: bug
assignees: ""
---

## Describe the bug

A clear and concise description of what the bug is.

## Affected area

- [ ] Frontend (`frontend/`)
- [ ] Backend API (`backend/`)
- [ ] Worker (`worker/`)
- [ ] Core (`core/`)
- [ ] Infra / Docker (`infra/`)
- [ ] Docs
- [ ] Other / not sure

## To reproduce

Steps to reproduce the behavior:

1. ...
2. ...
3. ...

## Expected behavior

What you expected to happen instead.

## Actual behavior

What actually happened. Include any error messages or stack traces.

## Environment

- Deployment profile: <!-- hosted (Docker Compose) | edge / portable -->
- OS:
- Python version (if backend/worker/core):
- Node / npm version (if frontend):
- Commit / branch:

## Was real infrastructure involved?

<!--
If this touches a live path (network scan, MQTT broker, Postgres/Redis, Docker,
edge→hub sync), say which infrastructure you ran against. These paths are pending
on-site validation — see docs/phase5-onsite-validation.md.
-->

## Logs / screenshots

Add structured logs (single-line JSON), screenshots, or the crash log if relevant.

## Additional context

Anything else that would help us reproduce or understand the issue.
