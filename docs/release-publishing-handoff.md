# Handoff — publish the GitHub Releases for v0.1.11 / v0.1.12 / v0.1.13

**Written 2026-07-16.** For the next Claude Code session, on an account that HAS
GitHub credentials (the session that produced this did not, so it could tag and
push but could not publish Releases). Self-contained: you do not need the prior
conversation.

## State when this was written (verify it still holds)

- `main` @ `aedbfd3` — local == origin, working tree clean, **0 open PRs**,
  one worktree (main only).
- Tags on GitHub: `v0.1.11` → `0abb63c`, `v0.1.12` → `e6ba552`,
  `v0.1.13` → `8beb523`. All three exist and are pushed.
- CI, Windows Compatibility, and Windows Portable Bundle are green on `main`.
- The source code for every version is already on GitHub (the repo IS the
  source; a Release is only a labelled pointer + notes). The **only** thing
  missing is published Releases for 11/12/13 — the Releases page currently stops
  at v0.1.10.

Verify before doing anything:
```bash
git fetch --all --tags
git rev-parse main origin/main            # must match
git ls-remote --tags origin | grep -E 'v0.1.1[123]$'
gh pr list --state open                   # expect none
```

## The job: publish 3 Releases from the existing tags

Do NOT create new tags — they exist. Publish a Release for each, with the notes
below. Source-code zips attach automatically; optionally also attach the exe
(see the last section).

With the gh CLI (preferred):
```bash
gh release create v0.1.11 --title "v0.1.11 — Reports you can find, imports that explain themselves" --notes-file /tmp/v0111.md
gh release create v0.1.12 --title "v0.1.12 — BACnet foreign-device registration"                        --notes-file /tmp/v0112.md
gh release create v0.1.13 --title "v0.1.13 — The rest of the walkthrough"                                --notes-file /tmp/v0113.md
```
Or the REST API (`POST /repos/Rvs006/smart-commissioning-app/releases` with
`tag_name`, `name`, `body`), or the web UI (Releases → Draft a new release →
choose the tag → paste → Publish).

Publish oldest first (v0.1.11, then 12, then 13) so the Releases page orders
sensibly. None of these is a pre-release.

---

### v0.1.11 — Reports you can find, imports that explain themselves

The 2026-07-15 walkthrough punch list, first batch. Nothing changes how a scan
works; every item is about the app telling you the truth about what it found.

- Reports are visible in the Reports tab on arrival (they were always being
  created, just hidden behind a step nobody knew to click).
- Each head remembers its last run when you navigate away and back.
- Result tables no longer invent sample rows.
- A rejected register import lists the actual per-row reasons; re-picking a
  same-named corrected file works instead of silently doing nothing.
- Excel-saved CSVs import instead of failing (CR-only line endings, Windows-1252,
  UTF-16, and "60.0" in the interval column).
- The report button sits at the end of the results; results snap to the top; the
  ELECTRACOM logo is served in the exe; consistent "&lt;Protocol&gt; Discovery"
  menu naming; the duplicate top Run-UDMI card is gone.
- A build-stamped version pill in the app header.

Full detail: `CHANGELOG.md` `[0.1.11]`.

---

### v0.1.12 — BACnet foreign-device registration

Discovery can register with a BBMD as a foreign device, so it reaches devices on
subnets a local broadcast cannot cross — the thing a third-party BACnet browser
does, and why a browser could see a device this app could not. It runs only when
Foreign Device is Enabled with a real BBMD Address, on its own UDP port (47809),
alongside the ordinary local-broadcast scan. If the BBMD refuses or ignores the
registration, the run fails and says so with the BVLL result code; it never
quietly scans the local subnet instead. Every empty or failed scan now records
diagnostics you can read from the run record alone. Also fixes a bug where the
UDP socket was never released, so the second scan of a session silently returned
zero devices.

Two things to know: an existing install does not pick this up until Foreign
Device and BBMD Address are set by hand, and the live BACnet path has not run
against real hardware yet (first contact is the on-site lab session — see
`docs/lab-day-2026-07-20-runbook.md`).

Full detail: `CHANGELOG.md` `[0.1.12]`.

---

### v0.1.13 — The rest of the walkthrough

The remaining walkthrough items: honest config, better reports, MQTT and UDMI
upgrades.

- Reports carry ELECTRACOM header/footer branding and a per-head discovery
  inventory (the devices, points and topics found, not just run metadata).
- IP scan shows every register entry including non-responders ("no response on
  scanned ports", never a fabricated "offline").
- MQTT: the whole-broker scan is compared against the imported register (green
  matched / red foreign), capture can run for days, and the inspector shows a
  selected topic's payload plus retained flag, QoS and received-at time.
- UDMI: a silent device no longer fails the whole run; red/amber/green on the
  results; a downloadable schema template for non-published sets.
- The certificates status pill is derived from what is actually stored; logging
  writes to a local file and can upload a masked bundle to a URL; the never-wired
  syslog fields are removed.
- Placeholder content purged (Block B Plantroom, sample dashboard, the fake
  "Last Backup Status: Success"); the tolerances template accepts its own example
  row (0.5, 5%, abs:0.5, percent:5).

Deferred to a later release: a general nmap-style discovery pane (needs the
operator's curated port list first) and an internal component refactor (the
scroll-unbind that operators actually feel did ship).

Full detail: `CHANGELOG.md` `[Unreleased]` (this content should be cut to a dated
`[0.1.13]` section when convenient).

---

## Optional: attach a version-stamped exe to each Release

The exes built by the auto-triggered Windows Portable Bundle runs are
**unversioned** (the version pill reads a git-describe SHA). For a clean stamped
build, dispatch the workflow with a version input, then download and attach it:
```bash
gh workflow run "Windows Portable Bundle" -f version=v0.1.13   # repeat per version tag
# when the run finishes:
gh run download <run-id> -n SmartCommissioningApp-windows-portable -D ./exe
gh release upload v0.1.13 ./exe/SmartCommissioningApp*.exe
```
`packaging/windows_portable/build.ps1` writes that version into `README_FIRST.txt`
and the EXE Properties → Details metadata.

## Machine note (only if you are on the same ThreatLocker/WDAC laptop)

If `python -m ruff`/`unittest` fail to read repo `.py` files, you are on the
locked-down machine. Two workarounds the prior session proved: install real ruff
to a scratch dir and run `PYTHONPATH=<scratch> python -m ruff check backend
worker core` (its binary loads under WDAC); and run the Python test suites from a
copy of the packages placed OUTSIDE the repo tree with deps pip-installed to a
`--target` dir. See `AGENTS.md` "Gotchas" and, if present, the account-local
memory. On any normal machine ignore this — the standard commands in `AGENTS.md`
just work.
