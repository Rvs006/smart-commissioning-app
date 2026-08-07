# Unified AI-assisted testing platform for the portable Windows app

**Research date:** 2026-08-05
**Scope:** Testsigma, ACCELQ, Vorflux.ai, and TesterArmy
**Source rule:** Official vendor documentation and first-party pages only

## Decision

**Run a proof-of-concept with ACCELQ first.** It is the only candidate whose official documentation explicitly supports all of these building blocks in one test scenario:

- invoke a Windows executable by path;
- move between Windows, web, and API steps in one end-to-end flow;
- execute through a Windows agent inside a private network;
- access local APIs, files, databases, and Windows applications;
- schedule and report long-running jobs; and
- use AI to generate test steps and test cases.

This is a conditional recommendation. No reviewed vendor documents establish native MQTT support, UDMI-aware assertions, or content validation of the app's XLSX, DOCX, and ZIP evidence. ACCELQ must prove those points on the real portable build before purchase.

Testsigma is the second choice. Its documentation covers Win32 applications, local agents, private/local web URLs, REST API steps, AI-generated tests, and scheduled runs. The missing proof is whether one uninterrupted test can launch this EXE, follow its dynamically selected loopback URL into the browser, drive API or MQTT setup, and then validate exported evidence.

Vorflux.ai and TesterArmy should not be selected as the unified release-acceptance platform for this EXE. Vorflux is documented primarily as a cloud software-engineering agent with browser-based QA. TesterArmy is a web/mobile QA service; its remote workers reject localhost and private addresses, although its CLI can perform an ad-hoc local browser run.

## Application-specific requirements

The repository documents a hybrid application rather than a conventional native desktop UI: `SmartCommissioningApp.exe` is a PyInstaller launcher that starts FastAPI on a dynamically selected `127.0.0.1` port and opens a React browser UI. UDMI validation communicates with an MQTT broker, and the reporting stage exports XLSX, DOCX, and ZIP evidence. See the [repository README](../../README.md) and [Windows launcher](../../packaging/windows_portable/run_smart_commissioning_app.py).

A unified platform therefore has to coordinate four surfaces:

1. Windows process launch and shutdown.
2. Browser UI and loopback FastAPI requests.
3. Private MQTT stimulus and UDMI expected results.
4. Downloaded report files, application logs, and unattended reliability evidence.

"Unified" can mean one test-control and reporting platform. It does not mean that the platform will understand UDMI or generate trustworthy MQTT expectations by itself. Approved payload fixtures and explicit expected findings are still required.

## Evidence matrix

Legend: **Documented** means the vendor states the capability. **Inference** means the capability looks technically possible but is not promised for this app. **Not established** means no supporting official evidence was found.

| Requirement | Testsigma | ACCELQ | Vorflux.ai | TesterArmy |
| --- | --- | --- | --- | --- |
| Launch a Windows EXE | **Documented:** Windows Lite accepts an application path and supports Win32 apps. PyInstaller compatibility is an **inference**. | **Documented:** a Windows app can be invoked by giving its EXE path to `Execute Windows System Command`. | **Not established:** "desktop" is mentioned, but the documented QA action opens a browser. No Windows runner or EXE invocation is described. | **Not established:** documented run platforms are web, iOS, and Android. |
| Test `127.0.0.1` browser UI | **Documented:** local agent execution and a tunnel for locally hosted/private web apps. | **Documented:** the Local Agent executes behind the firewall and can access local URLs and APIs. | **Not established for the user's Windows machine:** the documented model runs the cloned stack on dedicated cloud compute. | **Partly documented:** `ta tests run --local` runs an ad-hoc local browser. Remote runs block localhost and private IPs. |
| REST API/backend testing | **Documented:** REST API steps can call endpoints and verify status, headers, and bodies. | **Documented:** web UI and API validation can be combined in one platform and one flow. | **Not established as a managed test surface:** full-stack execution is described, but no formal API-test workflow or assertions are documented. | **Not established as part of the same E2E run:** the hosted product documents browser/mobile runs; Scout is advertised separately as an API-testing CLI. |
| One flow across EXE, browser, and API | **Inference:** multiple application types can exist in a project, but reviewed docs do not prove one cross-technology scenario. | **Documented:** a scenario can combine Windows, web, backend, and API steps. | **Not established.** | **Not established.** |
| Private/local network access | **Documented:** local Agent and Testsigma Tunnel cover local/private web access. Direct private MQTT access is **not established**. | **Documented:** Local Agent can reach in-network URLs, APIs, databases, filesystem resources, and Windows apps without exposing inbound ports. Direct MQTT commands remain a **PoC item**. | **Not established:** no customer-side Windows agent or private OT-network runner is documented. | **Remote: no.** Official docs say remote testing blocks localhost, private ranges, and internal names. Local browser CLI is available. |
| Long-running execution | **Partly documented:** schedules and recurring runs exist. A single 10-hour local run is **not established**; step timeout is capped at 120 seconds, and published cloud-lab maximum-duration examples top out at three hours. | **Best documented fit:** recurring schedules and long-running jobs are supported; official release notes say jobs idle for more than 24 hours are aborted. A 10-hour active soak is still a **PoC gate**. | **Partly documented:** agents can run for hours, but there is no test-run duration contract or soak-test reporting model. | **Partly documented:** recurring monitoring exists, but no 10-hour single-run limit or local unattended runner contract was found. |
| MQTT/UDMI orchestration | **Not native in reviewed docs.** A Java add-on could provide a custom MQTT action, which is an **inference** requiring a PoC. | **Not native in reviewed docs.** Other message systems are supported, and Windows commands can launch a helper, so an MQTT fixture runner is an **inference** requiring a PoC. | **Not established.** | **Not established.** |
| Validate XLSX/DOCX/ZIP exports | **Partly documented:** browser download confirmation exists; custom add-ons could inspect contents. Format-aware validation is **not established**. | **Partly documented:** Local Agent can access the filesystem and execute local commands. Parsing all three formats is **not established**. | **Not established.** | **Partly documented for browser downloads/artifacts; format-aware local file inspection is not established.** |
| AI assistance | **Documented:** GenAI creates tests for web, desktop, and API inputs; Copilot authors, executes, and debugs tests. | **Documented:** Autopilot generates steps and test cases from natural language, with AI-assisted reconciliation and web element healing. | **Documented:** multi-agent planning, implementation, browser QA, and video evidence. It is an engineering agent rather than a dedicated test-management product. | **Documented:** plain-language, vision-based browser/mobile tests and exploration. |

## Vendor findings

### ACCELQ

Documented facts:

- [Windows automation logic](https://support.accelq.com/hc/en-us/articles/360061368912-Writing-Windows-automation-logic) says an EXE path can be passed to a Windows system command.
- [ACCELQ 5.0](https://support.accelq.com/hc/en-us/articles/4402947829517-What-s-new-in-ACCELQ-Release-5-0) says one scenario can cross Web, Desktop, backend, and API surfaces.
- The [Local Agent](https://support.accelq.com/hc/en-us/articles/360019691372-Securely-execute-tests-behind-firewall-using-Local-Agent) can reach private URLs, local API endpoints, databases, filesystems, and Windows applications. It runs in pull-only mode and continues an already-dispatched suite if its connection to ACCELQ is interrupted.
- [API support](https://support.accelq.com/hc/en-us/articles/115007527587-Support-for-API-testing) includes REST, SOAP, databases, and several message-service products, but it does not list MQTT.
- [Execution controls](https://support.accelq.com/hc/en-us/articles/115010299188-Running-Automation-Test) include schedules, recurring runs, screenshots, timeouts, run properties, and real-time progress.
- [Release 6.0 notes](https://support.accelq.com/hc/en-us/articles/10127655884813-What-s-new-in-ACCELQ-6-0) discuss long-running test jobs and state that jobs idle for more than 24 hours are aborted.
- [Release 7.0](https://support.accelq.com/hc/en-us/articles/33991400145165-What-s-new-in-Release-7-0) documents AI-generated natural-language logic, test steps, modular actions, test cases, and web element healing.

Assessment: ACCELQ has the strongest documented architecture match. My main concern is MQTT: calling an external fixture tool is plausible, but the vendor must prove stdout, exit-code, TLS certificate, and payload-result handling inside the same reported scenario.

### Testsigma

Documented facts:

- [Windows Lite](https://testsigma.com/docs/windows-lite-automation/introduction/) supports UWP, WinForms, WPF, and Win32 applications, accepts an application path, and executes locally through the Testsigma Agent.
- [Testsigma Tunnel](https://testsigma.com/docs/testsigma-tunnel/intro/) connects cloud browser/device execution to private server URLs and locally hosted web applications.
- [REST API test steps](https://testsigma.com/docs/test-cases/step-types/rest-api/) support HTTP methods, request data, authorization, and response verification.
- [GenAI capabilities](https://testsigma.com/docs/atto/generative-ai/overview/) cover generating tests for web, desktop, and APIs.
- [Project management](https://testsigma.com/docs/projects/overview/) permits multiple application types in one project.
- [Scheduling](https://testsigma.com/docs/test-plans/schedule-plans/) supports one-time and recurring test-plan runs.
- [Step settings](https://testsigma.com/docs/test-cases/create-test-steps/actions-and-options-manual/step-settings/) cap an individual step wait at 120 seconds.
- [Add-ons](https://testsigma.com/docs/addons/create/) permit custom Java actions for behavior absent from built-in actions.

Assessment: Testsigma is credible for separate desktop, browser, and API coverage. The official material reviewed does not explicitly promise one continuous cross-technology flow or a 10-hour local session. Those are purchasing gates, not details to assume.

### Vorflux.ai

Documented facts:

- The [Vorflux product page](https://vorflux.ai/) describes dedicated compute, full-stack setup on a real machine, browser QA, and recorded video evidence. It says agents can run for hours.
- The [Vorflux manifesto](https://vorflux.ai/manifesto) describes raw cloud compute that starts the application stack, browser-driven end-to-end checks, recordings, and native phone testing.

Assessment: no first-party material reviewed describes a Windows execution host, local/private-network runner, formal desktop automation, API assertions, MQTT actions, or soak-test result management. The word "desktop" on the product page is too broad to count as Windows EXE support. Vorflux may help build a test harness in the repository, but it is not presently evidenced as the single acceptance platform.

### TesterArmy

Documented facts:

- [TesterArmy run API](https://docs.tester.army/api-reference/tester-army-api/tests/trigger-a-test-run) lists web, iOS, and Android as run platforms.
- Its [CLI documentation](https://docs.tester.army/cli) says remote execution is the default and `--local` can run an ad-hoc test in a local browser.
- Its [custom-infrastructure documentation](https://docs.tester.army/integrations/custom-infrastructure) says remote workers block localhost, private IP ranges, and internal hostnames.
- The [product page](https://tester.army/) documents plain-language, vision-based browser/mobile testing, recurring monitoring, screenshots, recordings, and reports.

Assessment: TesterArmy could test the React UI after the EXE is already running by using its local CLI. It cannot be considered the unified Windows release platform because no Windows EXE runner is documented and remote runs cannot reach this loopback-only app.

## Required ACCELQ proof-of-concept

Ask ACCELQ to demonstrate the following on a Windows 11 VM using the actual portable bundle, with the app initially stopped:

1. Launch the unsigned PyInstaller EXE as a normal user.
2. Read or discover the dynamically selected `127.0.0.1` port, then drive the React UI without a hard-coded port.
3. Run one scenario across Windows launch, browser UI, and FastAPI verification.
4. Connect from the same Windows agent to a private MQTT broker on ports 1883 and 8883, including client-certificate authentication where used.
5. Invoke a controlled MQTT fixture publisher, capture its exit status and output, and correlate each payload with the expected UDMI finding.
6. Import an XLSX register, run UDMI validation, and assert exact pass/fail codes and counts rather than relying on visual interpretation.
7. Download XLSX, DOCX, and ZIP evidence; open or parse each file; verify required rows, headings, manifest, hashes, and signature metadata.
8. Preserve the app's runtime logs, browser console/network evidence, screenshots, downloaded reports, and the ACCELQ result under one run identifier.
9. Run unattended for 10 hours with the Windows session locked or RDP disconnected, then prove that the job remained active and the EXE, browser, broker reconnect, CPU/memory observations, and exports were captured.
10. Repeat after an intentional broker interruption and app restart to prove recovery and evidence continuity.

Vendor questions that need written answers:

- Which ACCELQ subscriptions are required for Windows, web, API, Autopilot, and Local Agent in one project?
- Can one scenario switch from a launched EXE to the browser instance opened by that EXE, or must it open a separate browser?
- Can a scenario discover a dynamic loopback port from process output or a port probe?
- Can local commands return stdout, stderr, and exit code as assertion inputs?
- Is there a supported MQTT or generic TCP client, or must the team maintain an external helper?
- What active-job, idle, command, browser-session, and agent-heartbeat limits apply to a 10-hour run?
- Does Windows automation require an unlocked interactive desktop? What is the supported RDP-disconnect procedure?
- Can the agent attach arbitrary local files and entire directories to the result without uploading secrets or certificates?
- Where are test steps, screenshots, logs, payloads, credentials, and exported evidence stored, and what retention and data-residency controls apply?
- Can the Local Agent operate on an OT-connected laptop without exposing the MQTT broker or loopback API to ACCELQ cloud?

## Purchase gate

Choose ACCELQ only if all ten PoC actions pass and the resulting evidence can be reviewed by the three engineers without vendor assistance. A slide deck or demonstration against a sample WinForms app is not sufficient. The proof must use this EXE, its dynamic local URL, a private MQTT broker, the approved UDMI fixtures, and a real 10-hour run.

If ACCELQ fails the cross-surface or 10-hour gate, run the same PoC with Testsigma. Do not select Vorflux.ai or TesterArmy as the sole release-acceptance platform for the portable Windows build on the evidence currently published.
