# Zauth Vector assessment for the Smart Commissioning App EXE

Date: 5 August 2026
Overall verdict: **NO as a unified end-to-end Windows EXE acceptance platform. PARTIAL as a web security add-on.**

## Decision

Zauth Vector is documented as a cloud-hosted black-box vulnerability scanner for web applications. It can exercise browser pages and HTTP APIs and produce security evidence. The official material does not document a Windows runner, arbitrary EXE launch, localhost access, MQTT/UDMI fixture control, Office/ZIP content validation, binary inspection, or a 10-hour run. Its longest documented scan is three hours.

It could help security-test a separately hosted and domain-verified copy of the Smart Commissioning App web/API layer. It cannot validate the complete portable package from `SmartCommissioningApp.exe` through MQTT/UDMI processing and exported-report correctness.

## Capability matrix

| Required capability | Verdict | Documented fact | Inference for this app |
|---|---|---|---|
| Launch an arbitrary Windows EXE | **No** | Vector runs scans in isolated worker nodes with Chromium and bash tooling. Zauth describes it as a scanner for web applications and advertises “no agent to run.” No Windows desktop runner or EXE launcher is documented. | It cannot prove EXE startup, PyInstaller extraction, port selection, Windows permissions, SmartScreen behaviour, or process cleanup. |
| Test `localhost` / `127.0.0.1` through a local runner | **No** | Targets require DNS TXT or public `/.well-known/` domain verification. Vector’s HTTP tooling has private-IP blocking, and no local/on-prem runner is documented. | A service bound only to loopback on an engineer’s PC is unreachable from Vector’s cloud worker. |
| Drive browser workflows | **Yes, security-focused** | Vector runs Chromium, navigates pages, fills forms, clicks controls, evaluates JavaScript, captures screenshots, and inspects browser/network state. | It can explore the React UI if that UI is reachable on a verified domain. The docs do not describe deterministic user-authored functional test cases or expected-result assertions. |
| Test REST APIs | **Partial** | It sends raw GET, POST, PUT, DELETE, PATCH, and OPTIONS requests, discovers SPA APIs, and tests JSON inputs for security flaws. | Useful for FastAPI security probing. Functional contract testing, business assertions, and repeatable regression suites are not documented. |
| Publish MQTT/UDMI fixtures | **No** | MQTT and UDMI support are not documented. Bash and custom scripts are available inside the scanner, but no broker fixture interface or private-network connectivity is described. | Treat custom-script MQTT support as unproven. It would still not establish control over the local Windows application process. |
| Inspect XLSX, DOCX, and ZIP exports | **No** | The docs mention browser screenshots, HTTP/network inspection, and security reports. They do not document downloading and parsing Office or ZIP artifacts for content assertions. | Vector cannot be relied on to compare exported report contents with UDMI findings. |
| Run a 10-hour soak/reliability test | **No** | Quick scans are documented at 30 minutes and Deep scans at three hours. | It cannot provide the required 10-hour memory, CPU, reconnect, crash, or persistence evidence. |
| Inspect EXE binaries and signatures | **No** | Vector assesses running web applications. RepoScan assesses GitHub repositories. PE-file analysis, Authenticode verification, dependency extraction, and Windows binary hardening checks are not documented. | Separate Windows binary and signing tools remain necessary. |
| Produce acceptance evidence | **Partial** | Vector produces security findings with severity, affected endpoints, proof-of-concept steps, remediation, screenshots, and streamed tool/network activity. | Good evidence for a security workstream. It is not full acceptance evidence for functional UDMI correctness, MQTT behaviour, exported reports, Windows packaging, or soak reliability. |

## Recommended use

Use Vector only as an optional security scan against an authorised, isolated, domain-verifiable test deployment of the web/API layer. Do not expose the portable localhost application merely to make it scannable. Keep EXE launch, browser regression, MQTT/UDMI fixtures, report inspection, Windows binary checks, and the 10-hour run in a Windows-capable acceptance harness.

Zauth’s terms require ownership or explicit written authorisation for every scanned target and state that a clean Vector scan is not a guarantee or a substitute for other security measures.

## First-party sources

- [Zauth Vector documentation](https://zauth.inc/docs/vector)
- [Zauth Vector product page](https://zauth.inc/vector)
- [Zauth homepage](https://zauth.inc/)
- [Zauth documentation index](https://zauth.inc/docs)
- [Zauth Terms of Service](https://zauth.inc/tos)
