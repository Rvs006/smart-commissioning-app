# v0.1.36 field-run handoff

**Prepared:** 2026-08-04
**Purpose:** Give the next Codex session a precise starting point for reviewing the supplied v0.1.36 two-hour validation package.

## Read this first

The report job completed and all selected source runs were processed, but this
is not a passing field-acceptance result. The evidence reports **0/752 assets
compliant**, **62/2,256 payloads correct**, **2,051 blocking issues**, and
**797 warnings**. Treat `report_job_status: succeeded` as report generation
success only.

The source archive was supplied outside the repository. The four report files
and the nested JSON evidence pack were inspected read-only. No raw application,
server, broker, or independent JSONL capture logs were included in the supplied
archive.

Source archive filename: `Smart Commissioning Tool v0.1.36 - reports_export.zip`
(kept outside the repository, normally in the user's Downloads folder).

## Release baseline

- Application: v0.1.36
- Release: https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.36
- Release commit: `7104f6e43b30472b14ebed405f787571d4b801ff`
- Field acceptance: still open; use `docs/v0.1.36-field-acceptance-checklist.md`
- No database migration is claimed for v0.1.36.

## Run and report provenance

| Field | Recorded value |
| --- | --- |
| Report title | `v0.1.36 2 hour run - UDMI Validation Report - 03 Aug 2026` |
| Report job status | `succeeded` |
| Scope status | `complete` |
| Selected source runs | 1; status `succeeded` |
| Source run identifier | `run_20260803152640_65f62642` |
| Last validation update | `2026-08-03T17:27:02.818155+00:00` |
| Report generated | `2026-08-04T08:21:11.271505+00:00` |
| Asset-topic discovery | enabled; capture `completed`; scope `#`; scope source `all` |

The broad `#` discovery scope should be treated as reconciliation evidence.
Confirm that this scope was approved before using it as acceptance evidence.

## Overall result

| Metric | Result |
| --- | ---: |
| Expected assets | 752 |
| Observed assets | 470 |
| Not observed assets | 282 |
| Assets with issues | 752 |
| Successfully validated assets | 0 |
| Registered assets on wrong topics | 17 |
| Unexpected devices | 27 |
| Expected payloads | 2,256 |
| Received payloads | 1,018 |
| Not received payloads | 1,238 |
| Payloads with issues | 991 |
| Successfully validated payloads | 62 |
| Overall compliance | 0/752 (0%) |
| Payloads correct | 62/2,256 (3%) |
| Payloads incorrect | 2,194/2,256 (97%) |
| Blocking issues | 2,051 |
| Warnings | 797 |

The report's definitions matter: “not observed” means no retained expected
payload evidence during the validation window; it does not prove that an asset
was disconnected.

## Result by system

| System | Expected assets | Observed | Not observed | Passed assets | Expected payloads | Received | Passed payloads | Blocking | Warnings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BMS | 25 | 23 | 2 | 0 | 75 | 45 | 0 | 82 | 26 |
| EMS | 464 | 391 | 73 | 0 | 1,392 | 821 | 0 | 1,731 | 494 |
| Fire | 198 | 38 | 160 | 0 | 594 | 105 | 62 | 63 | 204 |
| HVAC | 65 | 18 | 47 | 0 | 195 | 47 | 0 | 175 | 72 |

## Main findings

- Payload-formatting issues: **2,161**.
- Stale or cadence issues: **313**.
- Other issues: **331**.
- Missing points: **29**.
- Additional points: **9**.
- Point-naming issues: **5**.
- The evidence identifies **17 registered assets on alternate MQTT topic
  roots**. The nested `wrong_topic_assets.json` retains each expected and
  actual topic; keep those device-level details out of public notes.
- The discovery evidence records **27 unexpected publishers**. They are kept
  separate from registered-asset compliance totals.
- Only the Fire system has passing payloads in this package (62); no asset is
  marked successfully validated because every expected asset still has at
  least one failed or missing requirement.

## What v0.1.36 behavior to verify next

Use the reports and evidence pack to check that the live-console changes remain
honest:

1. Expected-topic, alternate-topic, and no-matching-topic outcomes stay
   distinct in the exported run summary and topic-discovery evidence.
2. Missing evidence is represented as unavailable / “Waiting for evidence”,
   never as a fabricated zero.
3. The UI heartbeat is sampling of run state, not a broker-message rate.
4. Unexpected publishers remain visible but do not inflate expected-asset
   compliance.
5. The report, DOCX, XLSX, PDF, and JSON evidence pack retain one title and one
   scope.

## Acceptance boundary and next run

This package does not close the checklist. Before calling v0.1.36 field-ready,
the next session must reconcile the following:

- an unfiltered scale run using the approved register and scope;
- a separate run covering the longest expected reporting cadence (24 hours when
  a required payload is daily);
- the paired independent append-only JSONL capture;
- the exact input register and its SHA-256;
- application, broker, machine, scope, engineer, and start/end records;
- raw topic, receive time, expected topic, and app status for every absent or
  unassociated in-scope message;
- SHA-256 values for every retained artifact; and
- a credential scan of all retained and shared files.

Do not turn the zero compliance result into a software defect without first
separating publisher/schema/cadence problems from application association
problems. The nested evidence is the source for that comparison.

## Supplied artifact inventory

The outer archive SHA-256 is:

`2118FFEB8B16425415965226383EAC141BA874DA7F47416DAFAC689631514CAB`

| File | Size (bytes) | SHA-256 |
| --- | ---: | --- |
| `v0.1.36_2_hour_run_-_UDMI_Validation_Report_-_03_Aug_2026_run_20260804082109_51b6e75b.pdf` | 9,035,567 | `4C80DD3BF248119DB5A8380D2DE5F0C06C77F7AACFD6993B785763AF87F26EDA` |
| `v0.1.36_2_hour_run_-_UDMI_Validation_Report_-_03_Aug_2026_run_20260804082111_96700fa2.docx` | 132,176 | `CF2C8132AF0761E35B3D79D09389D0F07806E84847CE86A96A67076165BA682C` |
| `v0.1.36_2_hour_run_-_UDMI_Validation_Report_-_03_Aug_2026_run_20260804082112_ecd95ca8.zip` | 225,855 | `21D2EFC86906DFB57246EB146F780FC6735934B7BDA0611D17F6B2D68066BB9E` |
| `v0.1.36_2_hour_run_-_UDMI_Validation_Report_-_03_Aug_2026_run_20260804082115_133e9297.xlsx` | 310,641 | `EF00D56F0AAC9FA31DA2E97E677773863C0E00475D97E3AB5568F29D152D865A` |

The nested evidence ZIP contains these JSON files: `annotated_input_register`,
`asset_topic_discovery`, `asset_validation_schedule`, `fault_details`,
`fault_matrix`, `findings`, `metric_definitions`, `summary`,
`validation_summary`, and `wrong_topic_assets`.

## Next-session prompt

> Read `docs/handoff-v0.1.36-2026-08-04.md` and
> `docs/v0.1.36-field-acceptance-checklist.md` first. Then inspect the local
> v0.1.36 `reports_export.zip` and its nested JSON evidence read-only. Start
> with `validation_summary.json`, `asset_topic_discovery.json`,
> `wrong_topic_assets.json`, `findings.json`, and `fault_details.json`.
> Treat `report_job_status: succeeded` as export success, not field acceptance.
> Keep site, device, topic, credential, and personnel details out of public
> commits. Separate publisher/schema/cadence findings from app association
> defects before proposing a v0.1.37 fix.
