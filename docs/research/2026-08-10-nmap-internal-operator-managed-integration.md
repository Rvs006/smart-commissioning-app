# Internal, operator-managed Nmap integration on Windows

**Research date:** 2026-08-10  
**Decision scope:** Smart Commissioning is used only inside the same legal organization, and an authorized operator performs one interactive Nmap installation on each Windows workstation.  
**Source rule:** Licensing and platform claims use Nmap, Npcap, and Microsoft primary documentation only. The pinned EDQ source is used solely as an implementation comparison.

## Decision

Proceed with Nmap as an optional process provider for the internal build. The operator downloads and installs the official Windows Nmap package once. `SmartCommissioningApp.exe` locates that separately installed `nmap.exe`, checks its installation identity and capabilities, starts it as a contained child process, and parses XML written to stdout.

The portable bundle must contain no Nmap or Npcap binary, installer, driver, DLL, data file, NSE script, archive, or download routine. The application must never install, update, or silently fetch either product. It must never call the Npcap API directly.

This model can provide the core Nmap feature set on an internal workstation: TCP connect, SYN, UDP, service/version detection, OS detection, traceroute, host discovery, and individually approved NSE scripts. It does not expose an arbitrary Nmap command box. Fixed profiles and a typed argument builder are required for authorization, OT safety, and command-injection prevention.

## Licensing boundary

### What the official terms say

- Nmap's OEM page says organizations that only use Nmap themselves or within their organization may download and use it for free. OEM licensing is presented for companies that redistribute Nmap or use it in products they distribute. [Nmap OEM Edition](https://nmap.org/oem/)
- The Nmap Public Source License (NPSL) says software designed specifically to execute Nmap and parse its results is treated by the licensor as a derivative work. It also says some vendors execute an end-user-installed Nmap or parse end-user-provided results and may rely on copyright doctrines such as fair use without exercising NPSL rights. That paragraph is a limitation on the licensor's claim, not an express permission for every proprietary integration. [NPSL section 3](https://svn.nmap.org/nmap/LICENSE)
- The NPSL treats an installer as including Nmap even when the installer fetches Nmap from the Internet at runtime. An application-controlled downloader therefore does not avoid the distribution issue. [NPSL section 3](https://svn.nmap.org/nmap/LICENSE)
- The official Windows build contains Npcap under separate terms, and Nmap states that the Windows build containing Npcap cannot be redistributed without special permission. [NPSL section 10](https://svn.nmap.org/nmap/LICENSE) and [Nmap legal notices](https://nmap.org/book/man-legal.html)
- Npcap Free cannot be redistributed. Its published exception allows unlimited installations when Npcap is used only with Nmap, Wireshark, and/or Microsoft Defender for Identity. [Npcap licence summary](https://npcap.com/guide/index.html#npcap-license)

### Working interpretation for this project

The published Nmap position supports the stated same-organization, internal-use case without an Nmap OEM redistribution licence. The app may use the operator's separately installed Nmap, provided the internal build is not given to customers, partners, or unrelated legal entities and is not offered as a scanning service to them.

The dedicated execute-and-parse wording creates a clear external-release risk. "The customer installs Nmap themselves" is not a safe external distribution strategy on its own. Before any third-party release with execution or parsing enabled, obtain written Nmap OEM terms or a written licensor waiver covering the exact use and version. Counsel advice alone does not enable external mode. Npcap redistribution rights must be handled separately.

The installed Nmap package's included `LICENSE` controls that version. Record its version and licence digest with the workstation capability record because Nmap notes that release terms can differ. [NPSL overview](https://nmap.org/npsl/)

## One-time operator installation

1. The operator uses a maintenance window and downloads the deployment-approved supported Windows self-installer version directly from [nmap.org/download.html](https://nmap.org/download.html). Nmap publishes detached signatures and release digests for verification. [Nmap download verification](https://nmap.org/book/install.html#inst-integrity)
2. The operator runs the graphical installer. The public installer offers Nmap and Npcap together; the Windows guide identifies the self-installer as the normal installation route. [Nmap Windows installation](https://nmap.org/book/inst-windows.html#inst-win-exe)
3. The operator installs Npcap when raw-packet features are required. Npcap installation changes the network stack and may cause a brief network interruption, so it must not be installed during an active commissioning run. [Npcap installation notes](https://npcap.com/guide/npcap-users-guide.html#npcap-installation)
4. The operator decides whether to select Npcap's administrator-only option. That option restricts capture and injection handles to SYSTEM and Built-in Administrators and uses a UAC helper when a non-admin process opens a capture handle. [Npcap `/admin_only`](https://npcap.com/guide/npcap-users-guide.html#npcap-installation-options)
5. Smart Commissioning discovers candidates only from official 32-bit/64-bit uninstall registry metadata and protected standard installation roots. It shows the canonical executable and installed data-directory identity to an administrator for one-time confirmation. Arbitrary path entry, PATH lookup, the current directory, user profiles, and network shares are not accepted. The common default is under `C:\Program Files (x86)\Nmap`. [Executing Nmap on Windows](https://nmap.org/book/inst-windows.html#inst-win-exec)
6. Smart Commissioning runs a non-network health check using the confirmed absolute executable path and `--version`, then records the Nmap/Npcap versions, executable digest, installed data-directory manifest digest, installed NPSL version and licence digest, installation paths, Npcap admin-only state, current token capability, and supported profiles.

The public Nmap and Npcap installers are interactive. Silent Nmap installation is an OEM feature, and Npcap documents `/S` as Npcap OEM only. Smart Commissioning must not invoke either installer. [Nmap installer options](https://nmap.org/book/inst-windows.html#inst-win-exe) and [Npcap installer options](https://npcap.com/guide/npcap-users-guide.html#npcap-installation-options)

Nmap's installer can apply system-wide TCP performance registry changes. The operator and workstation owner should review that option rather than allowing the application to change the registry. [Nmap Windows performance changes](https://nmap.org/book/inst-windows.html)

## How Nmap runs from the portable EXE

Nmap remains a separate installed program. PyInstaller does not embed or import it. The backend creates one child `nmap.exe` process for each bounded scan batch and consumes its stdout/stderr pipes.

### Invocation contract

1. Resolve only the administrator-confirmed executable and data directory discovered from trusted installation evidence. Require regular files in an administrator-controlled installation directory and compare executable, data-manifest, version, publisher, reparse, ACL, and licence identity with the recorded capability entry.
2. Build arguments from typed fields such as profile, authorized target addresses, ports, interface, and limits. The application may generate one owner-only `-iL` file containing only the final expanded, deduplicated, post-exclusion literal IPv4 addresses from the sealed preview. Reject free-form flags, response files, operator-supplied filenames, target/exclusion files, script paths, script arguments, hostnames, wildcards, provider-side target expressions, positional targets, and shell metacharacters.
3. Call `CreateProcessW` with a non-null, fully qualified `lpApplicationName`; never invoke `cmd.exe`, PowerShell, `ShellExecute`, a batch file, or `shell=True`. Microsoft documents executable-path ambiguity when `CreateProcess` receives only a command string containing spaces. [CreateProcessW security remarks](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw#security-remarks)
4. Create the process suspended and without a visible console, associate it with a Windows Job Object, then resume it. Microsoft documents this sequence for assigning a new process before it runs. [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)
5. Set `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, bounded process/job memory, a small active-process limit, and CPU-time limits. Enforce a separate wall-clock deadline in the parent. Job Objects manage a process group as one unit, inherit child processes by default, and can terminate the full group. [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) and [Job Object limits](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)
6. Inherit only the intended stdout/stderr handles. Use a fixed working directory and a reduced environment. Pin Nmap data lookup to the installed, recorded data directory with an application-supplied `--datadir`; Nmap otherwise searches user and process locations including `%APPDATA%\nmap` and the current directory. [Nmap data-directory search](https://nmap.org/book/man-misc-options.html)
7. On Stop, authorization loss, deadline, output cap, or parser failure, terminate the Job Object and mark the provider result cancelled or failed. Never leave `nmap.exe` running after the parent closes.

Smart Commissioning must not request UAC elevation. TCP connect mode runs as a normal user. For a privileged profile, the operator starts the internal application explicitly with the required Windows rights or uses an Npcap configuration already approved for that workstation. When Npcap is admin-only and the current token lacks the required rights, the raw profile is unavailable and no Nmap child launches, so the adapter never enters a path expected to invoke Npcap's UAC helper. Capability checks fail closed when raw access is absent.

## Windows capability matrix

| Feature | Windows requirement | Network behaviour | Product policy |
| --- | --- | --- | --- |
| `-sT` TCP connect | Normal user; Npcap is not required with `-Pn` | Uses the Windows `connect` API and completes the TCP handshake on open ports. It works across networking types unsupported by raw Ethernet scans and is more likely to be logged. | Default internal Nmap mode. Explicit targets and ports only. |
| `-sS` TCP SYN | Npcap/raw Ethernet capability; Administrator is recommended by Nmap | Sends half-open SYN probes and distinguishes open, closed, and filtered. Windows raw scans support Ethernet-style interfaces, including many Wi-Fi and VPN adapters, but not every tunnel. | Optional raw TCP profile after a capability check. No silent fallback that changes evidence meaning. |
| `-sU` UDP | Npcap/raw capability; Administrator recommended | Sends UDP payloads or empty packets. No response produces `open|filtered`, not a proven open port. UDP scans can be slow because of timeouts and ICMP rate limiting. | Explicit reviewed UDP ports only. Preserve `open|filtered` as uncertain. |
| `-sV` service/version | Inherits the selected TCP/UDP scan requirements; version probes themselves do not require raw packets | Actively interrogates discovered ports. Default intensity is 7; `--version-light` is intensity 2. Nmap normally excludes TCP 9100 because some printers print received probes. | Opt-in inventory profile, explicit ports, `--version-light`; forbid `--allports`. |
| `-O` OS detection | Raw-packet and capture access; Administrator recommended | Sends several TCP, UDP, and ICMP probes. Accuracy is best when one open and one closed TCP port are found. | Controlled lab or approved maintenance window; `--max-os-tries 1`; no aggressive guessing. |
| `--traceroute` | Privileged/raw capability and Npcap on Windows | Uses scan results to select probes and sends TTL-limited packets. It is not supported with `-sT`. | Controlled routed-network profile only. Disabled in the default OT profile. |
| Host discovery | Full Windows defaults use ICMP, TCP raw probes, and local ARP/ND when raw capability exists | `-Pn` skips normal discovery and causes every explicit target to be scanned. Nmap specifically documents `-sT -Pn` as the no-Npcap Windows fallback. | Connect profile uses explicit targets with `-Pn`; raw discovery is a distinct previewed profile. |
| NSE | Depends on the script and underlying scan | Scripts are not sandboxed. The official set includes exploit, brute-force, DoS, intrusive, external, and other high-impact categories. Even `-sC` can include scripts Nmap calls intrusive. | No arbitrary script input. Exact first-party script allowlist only, pinned to a tested Nmap release. No NSE in the initial OT profile. |

Sources: [port scanning techniques](https://nmap.org/book/man-port-scanning-techniques.html), [Windows limitations](https://nmap.org/book/inst-windows.html), [version detection](https://nmap.org/book/man-version-detection.html), [OS detection](https://nmap.org/book/man-os-detection.html), [host discovery and traceroute](https://nmap.org/book/man-host-discovery.html), and [NSE safety](https://nmap.org/book/man-nse.html).

"Full Nmap" in this product means the adapter and evidence model can represent each capability above when the selected internal profile permits it. It does not mean `-A`, unrestricted NSE, arbitrary command text, or every feature in one run. Nmap states that `-A` enables OS detection, version detection, default scripts, and traceroute, and warns that its default scripts can be intrusive. [Nmap miscellaneous options](https://nmap.org/book/man-misc-options.html)

## XML integration contract

Use these fixed integration arguments for every provider run:

```text
--noninteractive --no-stylesheet -oX -
```

`-oX -` sends XML alone to stdout while serious errors remain on stderr. Nmap calls XML the preferred format for a non-trivial application interface. `--noninteractive` stops Nmap from reading runtime keystrokes, and `--no-stylesheet` removes the XML stylesheet directive. [Nmap output](https://nmap.org/book/man-output.html)

The parser must:

- stream stdout with byte, depth, element-count, text-length, and elapsed-time limits;
- disable DTD resolution, external entities, XInclude, stylesheet loading, and network access;
- reject processing instructions and never render Nmap XML or script output as HTML;
- preserve Nmap states exactly, including `open|filtered`, `closed|filtered`, `filtered`, and `unfiltered`;
- treat a non-zero exit, killed process, truncated XML, missing final `runstats/finished`, output overflow, or unsupported schema as a provider failure rather than partial success;
- bound and sanitize stdout, stderr, service banners, hostnames, and NSE text before storage or display;
- record the executable digest, Nmap version, profile ID/version, normalized arguments, authorization/preview IDs, exit code, XML digest, start/end times, and cancellation reason; and
- keep raw XML in the restricted evidence store under the normal retention policy, while ordinary logs and public exports receive normalized fields only.

Do not use `--webxml`, a remote stylesheet, normal-text parsing, grepable output, or XML resume files. Nmap documents XML as stable for program use and grepable output as deprecated. [Nmap output formats](https://nmap.org/book/man-output.html)

## OT-safe profile controls

The application's initial Nmap profiles should enforce these product limits:

- This release accepts IPv4 CIDRs/ranges/addresses in the sealed authorization preview, expands them itself, applies exclusions, and gives Nmap only literal IPv4 addresses through the application-generated private `-iL` file. No IPv6, hostnames, random targets, operator target files, implicit route expansion, or newly discovered targets. IPv6 requires a separately planned and gated slice.
- Ports come from a reviewed commissioning profile. No default 1,000-port scan and no all-port scan.
- The default profile is TCP connect with no DNS: `-sT -Pn -n` plus explicit ports.
- A gentle profile starts at a low `--max-rate`, one retry, per-host timeout, and a bounded batch size. Those values are versioned product settings, not operator command text.
- `--max-rate` covers port and host-discovery traffic only. Service probes, OS detection, traceroute, and NSE need separate feature caps and wall-clock deadlines. [Nmap timing controls](https://nmap.org/book/man-performance.html)
- Version detection uses `--version-light`; OS detection uses one try; NSE uses an exact reviewed script name plus a short `--script-timeout`.
- `-A`, `-sC`, `--script=all`, script categories, wildcards, `+` forced scripts, third-party scripts, script files, and user script arguments are rejected.
- Evasion and spoofing options, decoys, source spoofing, fragmentation, custom payloads, packet flooding, external-category scripts, and new-target discovery are rejected.
- `-n` disables reverse DNS. External NSE scripts are rejected so scan details do not leave the site.
- Each run has a packet/attempt budget, target/port cap, overall deadline, Stop control, one active resource reservation per interface, and evidence showing the exact profile.
- Authorization and cancellation are checked before every bounded batch. Closing the Job Object stops the current batch; no later batch starts after state loss.
- Raw, UDP, version, OS, traceroute, and NSE profiles remain unproven until controlled hardware and packet-capture tests show their traffic and Stop timing.

Nmap warns that scans have occasionally crashed poorly written applications, network stacks, and operating systems, and says not to scan mission-critical systems unless downtime is acceptable. It also advises obtaining permission before even a light scan. These warnings justify maintenance-window controls and field-evidence gates for building controls. [Nmap legal and safety notices](https://nmap.org/book/man-legal.html#legal-notices)

## Actions the application must never take

- Bundle, copy, extract, cache, patch, or redistribute Nmap, Zenmap, Npcap, Ncat, Nping, Ndiff, NSE scripts, Nmap databases, DLLs, drivers, installers, or archives.
- Download Nmap or Npcap, invoke their installers, pass silent-install flags, modify their installations, or update them.
- Call `Packet.dll`, `wpcap.dll`, the Npcap SDK, or the Npcap driver directly.
- Search `PATH`, the current directory, a writable project directory, `%APPDATA%`, or a network share for an executable, data file, or script. Operator-supplied filenames and target files are also prohibited; only the application's sealed-preview literal-IP `-iL` artifact is allowed.
- Run through a shell, concatenate a command string, accept raw flags, allow environment-controlled data paths, or inherit unnecessary handles.
- Trigger UAC, store administrator credentials, run a persistent privileged scanning service, or automatically restart as Administrator.
- Interpret a missing feature as permission to substitute a different scan type. Capability changes must return an explicit unavailable result.
- Run `-A`, unrestricted NSE, aggressive timing, broad UDP, OS detection, or traceroute as a default commissioning action.
- Claim Nmap findings as verified device identity without protocol-level confirmation and source/authority checks.

## External-release guardrails

The Nmap process provider and XML-import route are available only behind `internal_operator_managed`, which defaults to disabled outside the controlled internal channel.

Release checks must fail when an external bundle contains:

- an enabled Nmap execution or parsing route;
- Nmap/Npcap executables, libraries, drivers, installers, data files, scripts, archives, or embedded resources;
- installer or runtime code that downloads or installs Nmap/Npcap;
- silent-install flags or direct Npcap API imports;
- an Nmap path, profile, or provider capability in a customer-facing configuration; or
- a service endpoint that lets a third party request Nmap scans.

The release SBOM, PyInstaller manifest, installer manifest, archive inventory, and binary-resource inventory must prove absence. Any customer, partner, public, unrelated-entity, or externally operated hosted-service/appliance/VM deployment requires written Nmap OEM terms or a written licensor waiver before enabling the adapter, plus the appropriate Npcap redistribution terms if Npcap is supplied. A same-organization internal VM remains within the recorded internal posture. The operator-installed arrangement does not remove the NPSL execute-and-parse issue for an externally distributed proprietary application.

## EDQ reference comparison

The user supplied Electracom Device Qualifier as a working reference. The inspected baseline is commit [`76364b59780579da07d995a8a0422c287eb87b15`](https://github.com/Rvs006/edq/tree/76364b59780579da07d995a8a0422c287eb87b15).

EDQ does not embed Nmap or Npcap in its Electron EXE. Its normal Docker route installs Debian's Nmap package and grants raw-network capabilities, while its optional Windows host route discovers Nmap through PATH and can install Nmap/Npcap through an elevated winget setup script. The host scanner launches Nmap with a list-form process call, validates targets/flags/scripts, caps timeout/concurrency/rate, uses `-oX -`, parses with `defusedxml`, streams progress, and records scanner provenance. See [Docker packaging](https://github.com/Rvs006/edq/blob/76364b59780579da07d995a8a0422c287eb87b15/server/backend/Dockerfile#L58-L93), [Windows setup](https://github.com/Rvs006/edq/blob/76364b59780579da07d995a8a0422c287eb87b15/scripts/setup-engineer-workstation.ps1#L184-L235), [Nmap policy](https://github.com/Rvs006/edq/blob/76364b59780579da07d995a8a0422c287eb87b15/tools/server.py#L243-L270), [process runner](https://github.com/Rvs006/edq/blob/76364b59780579da07d995a8a0422c287eb87b15/tools/server.py#L1088-L1234), and [XML parser](https://github.com/Rvs006/edq/blob/76364b59780579da07d995a8a0422c287eb87b15/server/backend/app/services/parsers/nmap_parser.py#L1-L220).

Smart Commissioning should reuse EDQ's typed target validation, fixed product profiles, list-form arguments, XML normalization, rate/concurrency/timeout limits, capability reporting, provenance, and cancellation wiring. It should not copy PATH/Get-Command trust, auto-winget installation, auto-UAC relaunch, Docker-installed Nmap, a generic raw-argument endpoint, silent SYN-to-connect fallback, direct-PID-only kill, 50,000-character raw-evidence truncation, or EDQ's broad allowance for `-A`, `-sC`, `-p-`, `-T5`, and dangerous NSE categories. The existing worker can launch the confirmed Nmap executable directly, so a network-visible helper service is unnecessary.

## Acceptance checklist for the internal provider

- [ ] The deployment owner records that every user and workstation is inside the same legal organization.
- [ ] The operator installed Nmap interactively from the official site and retained signature/digest evidence.
- [ ] Nmap and Npcap are absent from the Smart Commissioning bundle and SBOM.
- [ ] The app detects candidates only from trusted registry/protected-root evidence, records the confirmed executable, data-directory manifest, installed NPSL version/licence digest, and rejects path, publisher, reparse, ACL, version, licence, or digest drift.
- [ ] `--version` capability detection covers Nmap present, missing, changed, unsupported, Npcap unavailable/admin-only, insufficient rights, and poisoned data lookup locations.
- [ ] Normal-user `-sT -Pn` succeeds without Npcap; privileged profiles fail closed without raw capability, create no child process when Npcap admin-only would prompt, and never trigger UAC.
- [ ] Each matrix feature has a packet-capture fixture and preserves uncertain states.
- [ ] XML entity, DTD, stylesheet, oversized-output, truncated-output, hostile-text, and unsupported-version tests fail safely.
- [ ] Job Object cancellation kills the process tree within the product Stop target and leaves no `nmap.exe` process.
- [ ] Command-injection tests prove that targets, ports, operator filenames, environment variables, and NSE fields cannot add arguments; the only input file is the app-generated literal-IPv4 `-iL` artifact from the sealed preview.
- [ ] The OT profile rejects every prohibited feature and respects target, port, rate, retry, time, and batch caps.
- [ ] EDQ comparison fixtures prove the intended patterns were retained and its weaker PATH, setup, unsafe-profile, semantic-fallback, direct-PID, network-helper, and evidence-truncation behavior was not copied.
- [ ] An external-channel build test proves execution and XML parsing are disabled and no Nmap/Npcap material is present.

## Primary sources

- [Nmap Public Source License](https://svn.nmap.org/nmap/LICENSE)
- [NPSL overview and version note](https://nmap.org/npsl/)
- [Nmap OEM Edition](https://nmap.org/oem/)
- [Nmap legal notices](https://nmap.org/book/man-legal.html)
- [Nmap Windows installation and limitations](https://nmap.org/book/inst-windows.html)
- [Nmap output and XML](https://nmap.org/book/man-output.html)
- [Nmap port scan techniques](https://nmap.org/book/man-port-scanning-techniques.html)
- [Nmap version detection](https://nmap.org/book/man-version-detection.html)
- [Nmap OS detection](https://nmap.org/book/man-os-detection.html)
- [Nmap host discovery and traceroute](https://nmap.org/book/man-host-discovery.html)
- [Nmap NSE](https://nmap.org/book/man-nse.html)
- [Nmap timing controls](https://nmap.org/book/man-performance.html)
- [Npcap licence summary](https://npcap.com/guide/index.html#npcap-license)
- [Npcap installation and administrator-only mode](https://npcap.com/guide/npcap-users-guide.html)
- [Microsoft CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)
- [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Microsoft AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)
