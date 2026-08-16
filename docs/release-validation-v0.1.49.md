# v0.1.49 release validation

Date: 2026-08-16
Status: release automation required; field acceptance open

Every unchecked row blocks publication as field-accepted.

| Gate | Release evidence | State |
| --- | --- | --- |
| Epoch isolation | Same-ID preview/live event, auxiliary-evidence, report, and download fences | Automated release gate required |
| IP retry compatibility | Equivalent retry and exact v0.1.48 raw replay contract | Automated release gate required |
| Database compatibility | Retained `a6b7c8d9e0f1` head and no-migration rollback instructions | Automated release gate required |
| Portable executable | Windows portable build, ProductVersion, health, report, and smoke evidence | Automated release gate required |
| Immutable container images | Hosted release gates, SBOMs, image digests, and evidence pack | Automated release gate required |
| Field validation | Approved scan authorization and private commissioning evidence | **UNPROVEN** |

Before acceptance, record the approved authorization, exact application commit,
portable or image hash, run and evidence-set IDs, client and technical report
hashes, and owner sign-off. Automated checks do not approve a production scan.
