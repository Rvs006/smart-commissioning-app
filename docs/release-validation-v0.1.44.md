# v0.1.44 release validation

Date: 2026-08-14
Status: release automation required; field acceptance open

Every unchecked row blocks publication as field-accepted.

| Gate | Release evidence | State |
| --- | --- | --- |
| Version and contract identity | Package, runtime, frontend lock, and release contract checks | Local automated gate |
| BACnet fixes | Legacy BBMD endpoint, property-child relation, and target ceiling regression tests | Automated release gate required |
| Nmap approval | Concurrent canonical authority, RBAC, and portable profile tests | Automated release gate required |
| Portable executable | Windows portable build, ProductVersion, health, report, and smoke evidence | Automated release gate required |
| Immutable container images | Hosted release gates, SBOMs, image digests, and evidence pack | Automated release gate required |
| Field validation | Approved scan authorization and private commissioning evidence | **UNPROVEN** |

Before acceptance, record the approved authorization, exact application commit,
portable or image hash, run and evidence-set IDs, client and technical report
hashes, and owner sign-off. Automated checks do not approve a production scan.
