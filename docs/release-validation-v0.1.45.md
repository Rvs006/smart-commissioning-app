# v0.1.45 release validation

Date: 2026-08-14
Status: release automation required; field acceptance open

Every unchecked row blocks publication as field-accepted.

| Gate | Release evidence | State |
| --- | --- | --- |
| Version identity | Package, runtime, frontend fallback, lock, and release contract checks | Automated release gate required |
| Nmap operator flow | One-click approval wording, fixed-profile selection, and no-field guidance | Automated release gate required |
| Portable executable | Windows portable build, ProductVersion, health, report, and smoke evidence | Automated release gate required |
| Immutable container images | Hosted release gates, SBOMs, image digests, and evidence pack | Automated release gate required |
| Field validation | Approved scan authorization and private commissioning evidence | **UNPROVEN** |

Before acceptance, record the approved authorization, exact application commit,
portable or image hash, run and evidence-set IDs, client and technical report
hashes, and owner sign-off. Automated checks do not approve a production scan.
