import type {
  DiscoveryAssetObservation,
  DiscoveryResultsResponse,
  DiscoveryRowRecord,
  ObservedPort,
} from "../../api/client";
import { formatRelativeTime } from "./runFormat";

// Column sets for each discovery module's live results table. These mirror the
// real shapes returned by GET /discovery/runs/{id}/results, dropping the
// register-comparison "Result" columns (those verdicts are NOT in the discovery
// response — they come from validation result_summary).
export const ipResultColumns = [
  "Asset",
  "Observed IP",
  "MAC Address",
  "Hostname",
  "Ports",
  "Match Basis",
  "Last Seen",
  "Detailed Status",
];

export const bacnetResultColumns = [
  "Device",
  "Instance",
  "Address",
  "IP Address",
  "Network Number",
  "Vendor",
  "Objects",
  "Discovered",
  "Detailed Status",
];

export const mqttResultColumns = [
  "Topic",
  "Asset",
  "Message Count",
  "Last Payload Seen",
  "Detailed Status",
  "Raw Payload",
];

function str(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function formatPorts(ports: ObservedPort[] | undefined): string {
  if (!ports || ports.length === 0) {
    return "—";
  }
  return ports
    .map((entry) => {
      const base = `${entry.port}/${entry.protocol}`;
      return entry.service ? `${base} (${entry.service})` : base;
    })
    .join(", ");
}

// The IP engine appends "<MARKER> PORTS OPEN: <ports>" to an asset's status_detail
// — FORBIDDEN (a port the site disallows) or UNEXPECTED (open but not in the
// asset's "Expected services/ports"). Pull the port list back out so the results
// table can flag the host; "" for clean hosts (and any non-IP status_detail).
function portsFromDetail(statusDetail: string | undefined | null, marker: string): string {
  const match = new RegExp(`${marker} PORTS OPEN:\\s*([^|]+)`).exec(statusDetail ?? "");
  return match ? match[1].trim() : "";
}

export const forbiddenOpenPorts = (statusDetail: string | undefined | null): string =>
  portsFromDetail(statusDetail, "FORBIDDEN");
export const unexpectedOpenPorts = (statusDetail: string | undefined | null): string =>
  portsFromDetail(statusDetail, "UNEXPECTED");

// The engine's expected-port coverage verdicts (the register's "Expected
// services/ports" are now genuinely probed): "MISSING EXPECTED PORTS: <ports>"
// lists expected ports that did not answer, and "EXPECTED PORTS OK: <n>/<n>
// open" is the explicit all-clear when every expected port is open and nothing
// forbidden/unexpected fired. Both return "" when the verdict is absent.
export const missingExpectedPorts = (statusDetail: string | undefined | null): string => {
  const match = /MISSING EXPECTED PORTS:\s*([^|]+)/.exec(statusDetail ?? "");
  return match ? match[1].trim() : "";
};
export const expectedPortsOk = (statusDetail: string | undefined | null): string => {
  const match = /EXPECTED PORTS OK:\s*([^|]+)/.exec(statusDetail ?? "");
  return match ? match[1].trim() : "";
};

// IP discovery rows come from discovered_assets (DiscoveryAssetObservation).
export function ipRowsFromResults(results: DiscoveryResultsResponse): Record<string, string>[] {
  return results.discovered_assets.map((asset: DiscoveryAssetObservation) => ({
    Asset: str(asset.asset_id),
    "Observed IP": str(asset.ip_address),
    "MAC Address": str(asset.mac_address),
    Hostname: str(asset.hostname),
    Ports: formatPorts(asset.observed_ports),
    "Match Basis": str(asset.match_basis ?? "none"),
    "Last Seen": asset.last_seen_at ? formatRelativeTime(asset.last_seen_at) : "—",
    "Detailed Status": str(asset.status_detail),
  }));
}

// BACnet device rows come from the structured devices[] (with per-engine
// attributes carrying device_instance / point_count / vendor_id).
export function bacnetRowsFromResults(
  results: DiscoveryResultsResponse,
): Record<string, string>[] {
  // point_count per device is summarised from discovered_assets, which the
  // engine stamps with device_instance + point_count.
  const pointCountByInstance = new Map<string, number>();
  for (const asset of results.discovered_assets) {
    const instance = asset.device_instance;
    const count = asset.point_count;
    if (instance !== undefined && instance !== null && typeof count === "number") {
      pointCountByInstance.set(String(instance), count);
    }
  }

  return results.devices.map((device: DiscoveryRowRecord) => {
    const attributes = (device.attributes as Record<string, unknown> | undefined) ?? {};
    const instance = attributes.device_instance ?? "";
    const pointCount = pointCountByInstance.get(String(instance));
    // IP address and BACnet network number are optional per device. They may be
    // stamped on the engine attributes (ip_address / network_number) or, for
    // routed BMS networks, surfaced top-level; read both and show blank when the
    // engine did not report them (e.g. a local MS/TP segment with no IP).
    const ipAddress = attributes.ip_address ?? device.ip_address;
    const networkNumber = attributes.network_number ?? device.network_number;
    return {
      Device: str(device.name),
      Instance: str(instance),
      Address: str(device.address),
      "IP Address": str(ipAddress),
      "Network Number": str(networkNumber),
      Vendor: str(device.vendor),
      Objects: pointCount === undefined ? "—" : String(pointCount),
      Discovered: device.created_at ? formatRelativeTime(String(device.created_at)) : "—",
      "Detailed Status": str(device.device_type),
    };
  });
}

// MQTT topic rows come from the structured topics[].
export function mqttRowsFromResults(results: DiscoveryResultsResponse): Record<string, string>[] {
  return results.topics.map((topic: DiscoveryRowRecord) => {
    const attributes = (topic.attributes as Record<string, unknown> | undefined) ?? {};
    const lastPayload = topic.last_payload;
    return {
      Topic: str(topic.topic),
      Asset: str(attributes.device_ref),
      "Message Count": str(topic.message_count),
      "Last Payload Seen": topic.created_at ? formatRelativeTime(String(topic.created_at)) : "—",
      "Detailed Status": str(attributes.status_detail ?? attributes.broker_status_detail),
      "Raw Payload":
        lastPayload && typeof lastPayload === "object" && Object.keys(lastPayload).length > 0
          ? JSON.stringify(lastPayload)
          : "",
    };
  });
}

// MQTT wildcard match used by the Explorer-like capture panel's topic filter
// (mq9nhbzu): '#' matches the rest of the topic, '+' matches exactly one level.
// Mirrors broker semantics so a filter like "334os/+/+/state" behaves as the
// operator expects against the captured topic list.
export function matchesTopicFilter(topic: string, filter: string): boolean {
  const trimmed = filter.trim();
  if (trimmed === "" || trimmed === "#") {
    return true;
  }
  const filterParts = trimmed.split("/");
  const topicParts = topic.split("/");
  for (let index = 0; index < filterParts.length; index += 1) {
    const part = filterParts[index];
    if (part === "#") {
      return true;
    }
    if (index >= topicParts.length) {
      return false;
    }
    if (part === "+") {
      continue;
    }
    if (part !== topicParts[index]) {
      return false;
    }
  }
  return filterParts.length === topicParts.length;
}

export type DiscoveryView = {
  columns: string[];
  rows: Record<string, string>[];
};

export function discoveryViewFor(
  route: string,
  results: DiscoveryResultsResponse,
): DiscoveryView | null {
  if (route === "ip-scanner") {
    return { columns: ipResultColumns, rows: ipRowsFromResults(results) };
  }
  if (route === "bacnet-discovery") {
    return { columns: bacnetResultColumns, rows: bacnetRowsFromResults(results) };
  }
  if (route === "mqtt-discovery") {
    return { columns: mqttResultColumns, rows: mqttRowsFromResults(results) };
  }
  return null;
}

// result_summary keys and values the BACnet engine stamps to describe the
// transport a live run ACTUALLY used. Their spellings are owned on the Python
// side by core/smart_commissioning_core/engines/bacnet_params.py
// (PARAM_BACNET_MODE, PARAM_BBMD_ADDRESS, MODE_BROADCAST, MODE_FOREIGN_DEVICE);
// TypeScript cannot import that module, so they are mirrored here ONCE, by
// name, and never re-spelled at a call site. If the Python constants move,
// these move with them — the vitest cases pin the current spellings so the
// mirror cannot drift silently.
const SUMMARY_BACNET_MODE = "bacnet_mode";
const SUMMARY_BBMD_ADDRESS = "bbmd_address";
const MODE_BROADCAST = "broadcast";
const MODE_FOREIGN_DEVICE = "foreign_device";

// The engine-authored sentence explaining a live scan that found nothing. It is
// built from what the run actually did (transport, BBMD registration outcome,
// instance range, unanswered directed Who-Is), none of which this module can
// derive from the summary alone.
const SUMMARY_EMPTY_SCAN_HINT = "empty_scan_hint";

// A non-blank string value from a result_summary, or null. Blank is treated as
// absent: an empty stamp carries no information and must not be rendered as
// though the engine had said something.
function summaryText(summary: Record<string, unknown> | undefined, key: string): string | null {
  const value = summary?.[key];
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

// The transport clause appended to a live-scan label. This is the operator's
// proof that a configured Foreign Device registration was honoured: v0.1.12
// exists because a transport setting was read, stored, and then silently
// ignored, and a run whose pill says "local broadcast only" while the
// Configuration page says Foreign Device = Enabled makes that visible in
// seconds instead of never.
//
// An ABSENT mode returns "" rather than claiming broadcast. Runs from before
// this stamp existed did not record their transport, and reporting "local
// broadcast only" for them would be a fabricated observation about a run
// nobody observed.
function bacnetTransportClause(summary: Record<string, unknown> | undefined): string {
  const mode = summaryText(summary, SUMMARY_BACNET_MODE);
  if (mode === null) {
    return "";
  }
  if (mode === MODE_BROADCAST) {
    return " — local broadcast only (no foreign-device registration configured)";
  }
  if (mode === MODE_FOREIGN_DEVICE) {
    const bbmd = summaryText(summary, SUMMARY_BBMD_ADDRESS);
    // A foreign-device run with no BBMD recorded should not reach here (the
    // engine fails such a run outright), so say what is missing rather than
    // invent an address or downgrade the claim to broadcast.
    return bbmd === null
      ? " — foreign-device registration (BBMD address not recorded)"
      : ` — foreign-device registration via BBMD ${bbmd}`;
  }
  // Unrecognised but present: report it, exactly as an unrecognised backend is
  // reported below. Guessing a mode here would re-create the silently-ignored
  // transport bug in the one place meant to expose it.
  return ` — transport: ${mode}`;
}

// The BACnet engine stamps result_summary.backend with the backend that actually
// ran: "simulated" (demo/dry-run sample devices — Acme Controls / Globex BMS) or
// "bacpypes3" (a real on-wire Who-Is / ReadProperty scan). Surface it so an
// engineer never mistakes simulated sample data for a live scan. Only BACnet has
// a simulated backend (IP/MQTT do not), so callers gate on the bacnet-discovery
// route; returns null when the summary carries no backend label (e.g. a run that
// predates the label, or a failed run that persisted no summary).
//
// A live scan additionally names its transport (see bacnetTransportClause), so
// the pill answers both "was this real?" and "how did it actually reach the
// network?" — the second question is what Pete's zero-device run could not
// answer.
export type BacnetBackendLabel = {
  kind: "simulated" | "live" | "unknown";
  text: string;
};

export function bacnetBackendLabel(
  results: DiscoveryResultsResponse,
): BacnetBackendLabel | null {
  const backend = results.result_summary?.backend;
  if (typeof backend !== "string" || backend === "") {
    return null;
  }
  if (backend === "simulated") {
    return { kind: "simulated", text: "SIMULATED — demo data, not a real BACnet scan." };
  }
  if (backend === "bacpypes3") {
    return {
      kind: "live",
      text: `Live bacpypes3 scan${bacnetTransportClause(results.result_summary)}.`,
    };
  }
  // Unrecognised but present: report it neutrally rather than swallow it, so an
  // unexpected backend value is still visible instead of silently trusted.
  return { kind: "unknown", text: `Backend: ${backend}` };
}

// Honest primary/secondary metrics derived from the discovery result_summary.
export function discoveryMetrics(
  route: string,
  results: DiscoveryResultsResponse,
): { primary: string; primaryLabel: string; secondary: string; secondaryLabel: string } | null {
  const summary = results.result_summary;
  const num = (key: string): number | undefined => {
    const value = summary[key];
    return typeof value === "number" ? value : undefined;
  };

  if (route === "ip-scanner") {
    const responsive = num("hosts_responsive") ?? results.discovered_assets.length;
    const scanned = num("hosts_scanned") ?? responsive;
    return {
      primary: String(responsive),
      primaryLabel: "responsive hosts",
      secondary: String(scanned),
      secondaryLabel: "hosts scanned",
    };
  }
  if (route === "bacnet-discovery") {
    const devices = num("device_count") ?? results.devices.length;
    const points = num("point_count") ?? results.points.length;
    return {
      primary: String(devices),
      primaryLabel: "devices discovered",
      secondary: String(points),
      secondaryLabel: "points indexed",
    };
  }
  if (route === "mqtt-discovery") {
    const topics = num("topics_discovered") ?? results.topics.length;
    const messages = num("messages_captured") ?? 0;
    return {
      primary: String(topics),
      primaryLabel: "topics observed",
      secondary: String(messages),
      secondaryLabel: "messages captured",
    };
  }
  return null;
}

// Honest primary/secondary metrics for the validation routes, derived from a
// terminal validation run's result_summary. Returns null for run kinds that
// carry no comparable counts (e.g. mqtt_config_publish, which can run under
// either validation route) so the card shows a neutral empty state, never NaN.
export function validationMetrics(
  route: string,
  summary: Record<string, unknown> | undefined,
): { primary: string; primaryLabel: string; secondary: string; secondaryLabel: string } | null {
  if (!summary) {
    return null;
  }
  const num = (key: string): number | undefined => {
    const value = summary[key];
    return typeof value === "number" ? value : undefined;
  };

  if (route === "udmi-validation") {
    // UDMI/MQTT payload validation: expected_devices is present only for the
    // udmi_validation kind; absent for an mqtt_config_publish run -> null.
    const expected = num("expected_devices");
    if (expected === undefined) {
      return null;
    }
    // The engine stamps an explicit null when there is nothing to score (no
    // expected devices): that means "unscoreable", so show the neutral empty
    // state rather than falling through to the liveness ratio's bogus 0%.
    // Only a summary with the key absent entirely (a pre-upgrade run) keeps
    // the ratio fallback below.
    if (summary.payload_conformance_percent === null) {
      return null;
    }
    // Prefer the engine-stamped score (already floor'd and clamped <=99 while
    // blocking issues remain); the publishing ratio is only a fallback for
    // pre-upgrade runs whose summary lacks payload_conformance_percent.
    const stamped = num("payload_conformance_percent");
    const seen = num("publishing_seen") ?? 0;
    const conformance = stamped ?? (expected > 0 ? Math.round((seen / expected) * 100) : 0);
    // blocking_issue_count is critical+high+medium; the legacy issue_count is
    // ALL issues, so it must not be labelled "blocking".
    const blocking = num("blocking_issue_count");
    return {
      primary: `${conformance}%`,
      primaryLabel: "payload conformance",
      secondary: String(blocking ?? num("issue_count") ?? 0),
      secondaryLabel: blocking === undefined ? "issues found" : "blocking issues",
    };
  }

  if (route === "data-validation") {
    // BACnet point / mapping comparison runs carry total + ok + issue_count;
    // a publish run does not -> null (neutral empty state).
    const ok = num("ok");
    const total = num("total");
    if (ok === undefined || total === undefined) {
      return null;
    }
    return {
      primary: String(ok),
      primaryLabel: "checks passed",
      secondary: String(num("issue_count") ?? total - ok),
      secondaryLabel: "issues found",
    };
  }

  return null;
}

// Explicit empty-state copy for a *terminal* discovery run that produced no
// rows. Without this every such outcome collapses into the same "No results
// yet" as a head that has never run (Pete 2026-07-15: "it can't find anything,
// but it doesn't really tell us").
//
// The honesty rule cuts both ways here:
//  - Zero found on a SUCCEEDED run is a real observation, not a failure. Say
//    what was probed and what answered, and never imply the run went wrong.
//  - A FAILED or CANCELLED run must never read as "nothing found". No result
//    was recorded at all, which is an entirely different claim.
//
// Ordering is load-bearing: status is resolved BEFORE summary.dry_run. Engines
// stamp `dry_run` via base.py `_apply_success`, which writes result_summary on
// every non-exception engine return and only *then* resolves the terminal
// status through `_terminal_status` (base.py:363-382) — so a cancelled or
// self-diagnosed-failed run still carries `dry_run: true`. Testing dry_run
// first would label a cancelled dry run "Dry run complete", i.e. report a
// completion that never happened.
export type DiscoveryEmptyState = { title: string; detail: string };

export function discoveryEmptyStateFor(
  route: string,
  results: DiscoveryResultsResponse | undefined,
  errorMessage?: string | null,
): DiscoveryEmptyState | null {
  if (!results) {
    return null;
  }
  const summary = results.result_summary ?? {};
  const num = (key: string): number | undefined => {
    const value = summary[key];
    return typeof value === "number" ? value : undefined;
  };

  // A failure is not an observation: echo the engine's own diagnosis rather
  // than implying the scan looked and found nothing. The run monitor remains
  // the primary failure surface, so point at it instead of duplicating it.
  if (results.status === "failed") {
    return {
      title: "Run failed — no results recorded",
      detail:
        errorMessage ??
        "The run ended in failure before recording results. See the run monitor on the Run step for details.",
    };
  }
  if (results.status === "cancelled") {
    return {
      title: "Run cancelled",
      detail: "The run was cancelled before any results were recorded.",
    };
  }
  if (results.status !== "succeeded") {
    return null;
  }

  // A dry run sends no packets, so its zero counts describe the preview, not
  // the network. This must precede the per-route copy below, whose "0 hosts
  // probed" wording would otherwise read as a real negative finding.
  if (summary.dry_run === true) {
    return {
      title: "Dry run complete — preview only",
      detail:
        "No packets were sent and no live results are expected. Run a real scan to populate results.",
    };
  }

  if (route === "ip-scanner") {
    const scanned = num("hosts_scanned");
    let detail: string;
    if (scanned === 0) {
      detail = "0 hosts were probed — check the target override or the imported IP register.";
    } else if (scanned !== undefined) {
      detail =
        `${scanned} host${scanned === 1 ? "" : "s"} probed — none answered on the scanned ports. ` +
        "No response is not proof a host is absent; it only means nothing accepted a TCP " +
        "connection on the probed ports.";
    } else {
      detail = "The scan completed, but no host answered on the scanned ports.";
    }
    return { title: "Scan complete — no responsive hosts found", detail };
  }

  if (route === "bacnet-discovery") {
    const title = "Discovery complete — no BACnet devices responded";

    // The engine authors empty_scan_hint from what the run ACTUALLY did: which
    // transport it used, whether the BBMD acknowledged the registration, and
    // how many directed Who-Is to register addresses went unanswered. None of
    // that is derivable here, so when the hint is present it is always the more
    // specific and more accurate sentence — render it VERBATIM rather than
    // paraphrasing or appending to it. One authored string, no second voice
    // that can contradict it.
    const hint = summaryText(summary, SUMMARY_EMPTY_SCAN_HINT);
    if (hint !== null) {
      return { title, detail: hint };
    }

    // Fallback for runs that predate the hint (v0.1.11 copy, unchanged).
    // Who-Is is a broadcast, so there is no probed-count analogue; the
    // configured instance range is the honest scope descriptor. It is
    // deliberately consistent with — not a contradiction of — the engine's
    // wording: both say a local broadcast cannot reach devices behind a BBMD or
    // on another subnet.
    const low = num("device_instance_low");
    const high = num("device_instance_high");
    const range = low !== undefined && high !== undefined ? ` (instance range ${low}–${high})` : "";
    return {
      title,
      detail:
        `No devices answered the Who-Is${range}. Devices behind a BBMD or on another subnet ` +
        "may not receive a local broadcast.",
    };
  }

  if (route === "mqtt-discovery") {
    // Defensive only: the engine currently stamps a zero-message capture as
    // FAILED (capture_window_empty, mqtt_discovery.py:447-461), so this arm is
    // unreachable today and must not be described as fixing an observable case.
    const secs = num("capture_seconds");
    const window = secs !== undefined ? ` during the ${secs}s capture window` : "";
    return {
      title: "Capture complete — no MQTT messages received",
      detail:
        `No messages arrived on the subscribed topic filters${window}. Zero traffic is a real ` +
        "observation — check that devices are publishing and the topic filter matches.",
    };
  }

  return null;
}
