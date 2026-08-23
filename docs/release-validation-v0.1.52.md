# v0.1.52 release validation

Date: 2026-08-23
Status: release automation required; field acceptance open

Every unchecked row blocks publication as field-accepted.

| Gate | Release evidence | State |
| --- | --- | --- |
| Brief content | Protected preview/live flow, authorization timing, terminal evidence, failures, and retries | Automated content and browser gates required |
| Learning content | IP procedure, status reference, protocol boundaries, reports, exports, and troubleshooting | Automated content and browser gates required |
| Interaction and layout | All Brief/Learning controls, desktop and mobile readability, no console errors | Browser gate required |
| Database compatibility | Retained `a6b7c8d9e0f1` head and no-migration rollback instructions | Automated release gate required |
| Portable executable | Windows portable build, ProductVersion, bundled content, health, report, and smoke evidence | Automated release gate required |
| Immutable container images | Hosted release gates, SBOMs, image digests, and evidence pack | Automated release gate required |
| Field validation | Approved scan authorization and private commissioning evidence | **UNPROVEN** |

Before acceptance, record the approved authorization, exact application commit,
portable or image hash, run and evidence-set IDs, client and technical report
hashes, and owner sign-off. Automated checks do not approve a production scan.
