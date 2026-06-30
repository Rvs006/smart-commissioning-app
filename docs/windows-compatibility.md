# Windows Compatibility

Last reviewed: Tuesday, 2026-06-30.

## Supported targets

| Target | Supported profiles | Notes |
| --- | --- | --- |
| Windows 11 Pro | Portable `.exe`, local dev, Docker Compose through Docker Desktop | Best engineer laptop target. |
| Windows Server 2022 | Portable `.exe`, local dev, native services, or Linux VM/host for Docker Compose | Do not rely on Docker Desktop on Windows Server. |

## How we prove it

- CI runs `Windows Server 2022 smoke` on GitHub Actions `windows-2022`: Python
  install, lint, core/backend/worker unit tests, frontend install, lint,
  typecheck, tests, and build.
- Windows 11 Pro must be checked on a real laptop or self-hosted runner because
  GitHub-hosted Actions does not provide a Windows 11 Pro runner.
- For either Windows target, after the app is running locally:

```powershell
pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000
```

## Deployment guidance

- Use the portable `.exe` for a single engineer laptop or site machine.
- Use local dev mode for engineering work: Python 3.12, Node 22, SQLite, inline
  jobs, no Redis/Postgres required.
- Use Docker Compose on Windows 11 Pro through Docker Desktop.
- On Windows Server 2022, run the hosted Compose stack on a Linux VM/host, or
  install Postgres/Redis/API/frontend as native services. Docker Desktop is not
  supported on Windows Server 2019/2022.
