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
  "Result",
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
  "Register Match",
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

// Engine marker spellings owned on the Python side by
// core/smart_commissioning_core/engines/ip_scan.py (NO_RESPONSE_DETAIL,
// MARKER_EXPECTED_BY_REGISTER). TypeScript cannot import that module, so they
// are mirrored here ONCE, by name — the same cross-language contract as
// SUMMARY_BACNET_MODE below. The vitest cases pin the current spellings so the
// mirror cannot drift silently.
const NO_RESPONSE_DETAIL = "no response on scanned ports";
const MARKER_EXPECTED_BY_REGISTER = "EXPECTED BY REGISTER";

// True when a silent host's status_detail carries the register-expected marker.
// Used by the results table to render an amber "expected by register" chip
// without re-spelling the marker at the call site.
export const expectedByRegisterSilent = (statusDetail: string | undefined | null): boolean =>
  (statusDetail ?? "").includes(MARKER_EXPECTED_BY_REGISTER);

// The "Result" column label and row tone for one IP discovery row, derived from
// the same status_detail markers the chips already read — no new engine field.
//
// Precedence is LOAD-BEARING (honesty rule): a silent host is checked FIRST so
// it can never fall through to the red "Missing expected ports" verdict — a host
// we never heard from must never be coloured as a hard failure. Amber is
// reserved for register-expected silence (mirrors the BACnet expected-but-silent
// semantics); an unregistered silent host stays neutral so a wide CIDR sweep
// does not drown the table in amber. A responsive host with a demonstrably
// closed expected port IS a real finding (fail), unlike full silence.
export function ipRowVerdict(asset: DiscoveryAssetObservation): {
  label: string;
  tone: "pass" | "fail" | "warn" | null;
} {
  const detail = asset.status_detail ?? "";
  if (detail.startsWith(NO_RESPONSE_DETAIL)) {
    return {
      label: "No response on scanned ports",
      tone: detail.includes(MARKER_EXPECTED_BY_REGISTER) ? "warn" : null,
    };
  }
  if (forbiddenOpenPorts(detail)) {
    return { label: "Forbidden ports open", tone: "fail" };
  }
  if (missingExpectedPorts(detail)) {
    return { label: "Missing expected ports", tone: "fail" };
  }
  if (unexpectedOpenPorts(detail)) {
    return { label: "Unexpected ports open", tone: "warn" };
  }
  if (/HOSTNAME MISMATCH/.test(detail)) {
    return { label: "Hostname mismatch", tone: "warn" };
  }
  if (expectedPortsOk(detail)) {
    return { label: "Expected ports OK", tone: "pass" };
  }
  return { label: "Responsive", tone: null };
}

// IP discovery rows come from discovered_assets (DiscoveryAssetObservation),
// which now includes a row for every scanned host — responders and silent hosts
// alike. Each row carries a "Result" verdict label and a __tone key for row
// shading; __tone is NOT in ipResultColumns, so it never renders as a cell.
export function ipRowsFromResults(results: DiscoveryResultsResponse): Record<string, string>[] {
  return results.discovered_assets.map((asset: DiscoveryAssetObservation) => {
    const verdict = ipRowVerdict(asset);
    return {
      Asset: str(asset.asset_id),
      Result: verdict.label,
      "Observed IP": str(asset.ip_address),
      "MAC Address": str(asset.mac_address),
      Hostname: str(asset.hostname),
      Ports: formatPorts(asset.observed_ports),
      "Match Basis": str(asset.match_basis ?? "none"),
      "Last Seen": asset.last_seen_at ? formatRelativeTime(asset.last_seen_at) : "—",
      "Detailed Status": str(asset.status_detail),
      __tone: verdict.tone ?? "",
    };
  });
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

// MQTT topic rows come from the structured topics[]. Per-message metadata
// (retained flag / delivery QoS / received-at) rides the free-form attributes
// column and is stamped onto HIDDEN keys (not in mqttResultColumns, so they
// never render as cells) for the inspector and the View detail — the same
// pattern as UDMI's __tone. Runs predating this capture carry no metadata keys,
// so every hidden key falls back to "" and the UI reads "Not recorded".
export function mqttRowsFromResults(results: DiscoveryResultsResponse): Record<string, string>[] {
  // Delivery-QoS cap (the subscription QoS this run requested), stamped once on
  // the whole run's result_summary rather than per topic.
  const subscribeQosRaw = results.result_summary?.subscribe_qos;
  const subscribeQos =
    typeof subscribeQosRaw === "number" || typeof subscribeQosRaw === "string"
      ? String(subscribeQosRaw)
      : "";
  return results.topics.map((topic: DiscoveryRowRecord) => {
    const attributes = (topic.attributes as Record<string, unknown> | undefined) ?? {};
    const lastPayload = topic.last_payload;
    // last_retained is a JSON boolean; check identity so an absent value (old
    // run) yields "" rather than String(undefined). Never str() a boolean here —
    // "true"/"false" strings would defeat the absent-vs-false distinction.
    const retained =
      attributes.last_retained === true ? "yes" : attributes.last_retained === false ? "no" : "";
    const qos =
      attributes.last_qos === undefined || attributes.last_qos === null
        ? ""
        : String(attributes.last_qos);
    const receivedAt =
      attributes.last_received_at === undefined || attributes.last_received_at === null
        ? ""
        : String(attributes.last_received_at);
    // Register-comparison verdict (stamped at read time by the backend, MQTT
    // only). "matched" shows the basis so a single "prefix/#" register row that
    // green-lights dozens of observed topics is visible on every one of them;
    // "unmatched" is a topic seen on the broker but absent from the register.
    // No register_match key => no register imported (or a dry/failed run) => the
    // row stays neutral (no __tone key at all), which keeps every existing
    // fixture that carries no annotation rendering exactly as before.
    const registerMatch = attributes.register_match;
    const matchedFilter =
      typeof attributes.register_matched_filter === "string"
        ? attributes.register_matched_filter
        : "";
    let registerCell = "—";
    let registerTone: "pass" | "fail" | null = null;
    if (registerMatch === "matched") {
      registerCell = matchedFilter.includes("#")
        ? `In register (wildcard ${matchedFilter})`
        : "In register";
      registerTone = "pass";
    } else if (registerMatch === "unmatched") {
      registerCell = "Not in register";
      registerTone = "fail";
    }
    const row: Record<string, string> = {
      Topic: str(topic.topic),
      Asset: str(attributes.device_ref),
      "Register Match": registerCell,
      "Message Count": str(topic.message_count),
      // Prefer the real last-message receive time over the DB row-insert time
      // (created_at is stamped when the run persists, NOT when the message
      // arrived); falls back to created_at for runs predating metadata capture.
      "Last Payload Seen": receivedAt
        ? formatRelativeTime(receivedAt)
        : topic.created_at
          ? formatRelativeTime(String(topic.created_at))
          : "—",
      "Detailed Status": str(attributes.status_detail ?? attributes.broker_status_detail),
      "Raw Payload":
        lastPayload && typeof lastPayload === "object" && Object.keys(lastPayload).length > 0
          ? JSON.stringify(lastPayload)
          : "",
      __retained: retained,
      __qos: qos,
      __receivedAt: receivedAt,
      __subscribeQos: subscribeQos,
    };
    // Only stamp __tone when there is a verdict; absent key = neutral row.
    if (registerTone !== null) {
      row.__tone = registerTone;
    }
    return row;
  });
}

// MQTT wildcard match used by the Explorer-like capture panel's topic filter
// (mq9nhbzu): '#' matches the rest of the topic, '+' matches exactly one level.
// Mirrors broker semantics so a filter like "demo-site/+/+/state" behaves as the
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

// Client-side results-table filter (ISSUE-4). `tone` filters on the hidden
// __tone verdict key; `text` is either an MQTT wildcard (when it carries a +/#
// AND a Topic column exists) matched with broker semantics against the Topic
// cell, or a case-insensitive substring across every VISIBLE cell. Hidden keys
// (prefixed "__", e.g. __tone/__qos/__receivedAt) never participate in text
// matching, so a filter can never key off data the operator cannot see.
export type ResultsFilter = {
  text: string;
  // The verdict-filter value. On discovery routes tone == verdict, so this keys
  // off __tone. On udmi-validation verdict and shading-tone deliberately diverge
  // (Non-compliant is amber, Offline is red), so those rows carry a __verdict key
  // holding the real verdict kind ("pass" | "pass-notes" | "fail" | "offline" |
  // "" for none) which is preferred here — otherwise a "Fail" filter would key
  // off the amber tone and hide the Non-compliant rows the label promises.
  tone: string;
};

export function resultRowMatchesFilter(
  row: Record<string, string>,
  filter: ResultsFilter,
  topicColumn?: string,
): boolean {
  if (filter.tone !== "all") {
    // Prefer the explicit verdict key when present (udmi), else the shading tone
    // (discovery, where the two coincide). "" means "no verdict" for both.
    const rowTone = row.__verdict ?? row.__tone ?? "";
    if (filter.tone === "none" ? rowTone !== "" : rowTone !== filter.tone) {
      return false;
    }
  }
  const text = filter.text.trim();
  if (text === "") {
    return true;
  }
  // A plain query (no wildcard) always uses substring matching, even on the MQTT
  // route: matchesTopicFilter's exact-level semantics would make an asset-name
  // query match nothing. Only a +/# query engages topic matching.
  if (/[+#]/.test(text) && topicColumn && row[topicColumn] !== undefined) {
    return matchesTopicFilter(row[topicColumn], text);
  }
  const lowerText = text.toLowerCase();
  return Object.entries(row).some(
    ([key, value]) => !key.startsWith("__") && value.toLowerCase().includes(lowerText),
  );
}

export function filterResultRows(
  rows: Record<string, string>[],
  filter: ResultsFilter,
  topicColumn?: string,
): Record<string, string>[] {
  return rows.filter((row) => resultRowMatchesFilter(row, filter, topicColumn));
}

// One asset's group of UDMI results rows (ITEM-7): the visible per-payload-type
// rows for that asset, folded into a single expandable summary row. This is a
// RENDER-ONLY layer over the unchanged flat resultRows — each child keeps its
// ORIGINAL index, so positional selection and the Asset+Payload detail joins
// (the ISSUE-4 invariant) are untouched.
export type UdmiRowGroup = {
  asset: string;
  rows: Array<{ row: Record<string, string>; index: number }>;
  // Aggregate RAG shade over VISIBLE children only — so the collapsed row's tone
  // honestly reflects what is shown (it can change as the filters change).
  worstTone: "pass" | "warn" | "fail" | "";
  issueTotal: number;
  observedCount: number;
};

const TONE_RANK: Record<string, number> = { fail: 3, warn: 2, pass: 1, "": 0 };

// Folds already-filtered visible rows (each {row, original index}) into per-asset
// groups. udmiLiveResults emits rows grouped by asset in order, so consecutive
// same-Asset rows form one group.
export function groupUdmiRowsByAsset(
  visible: Array<{ row: Record<string, string>; index: number }>,
): UdmiRowGroup[] {
  const groups: UdmiRowGroup[] = [];
  let current: UdmiRowGroup | null = null;
  for (const entry of visible) {
    const asset = entry.row.Asset ?? "";
    if (!current || current.asset !== asset) {
      current = { asset, issueTotal: 0, observedCount: 0, rows: [], worstTone: "" };
      groups.push(current);
    }
    current.rows.push(entry);
    const tone = entry.row.__tone ?? "";
    if ((TONE_RANK[tone] ?? 0) > (TONE_RANK[current.worstTone] ?? 0)) {
      current.worstTone = tone as UdmiRowGroup["worstTone"];
    }
    const issues = Number(entry.row.Issues ?? "0");
    if (Number.isFinite(issues)) {
      current.issueTotal += issues;
    }
    if (entry.row.Observed === "Yes") {
      current.observedCount += 1;
    }
  }
  return groups;
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
// network?" — the second question is what field engineer's zero-device run could not
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
    // New runs stamp hosts_responsive; pre-upgrade runs contained ONLY
    // responders in discovered_assets, so counting assets with an open port is
    // correct for both — and never miscounts the new silent (non-responder)
    // rows, which carry observed_ports: [], as live.
    const responsive =
      num("hosts_responsive") ??
      results.discovered_assets.filter((asset) => (asset.observed_ports?.length ?? 0) > 0).length;
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

// A one-line summary of the MQTT register comparison for the results banner.
// Returns null when there is no comparison, or when no register was imported
// (register_available false — the banner shows the upload hint for that case).
//
// HONESTY: an expected register topic that no observed topic matched is worded
// "had no matching topic observed", never "device absent" — a silent topic is
// not proof the device is gone (the capture window may simply not have caught
// its publish).
export function mqttRegisterCompareNote(results: DiscoveryResultsResponse): string | null {
  const comparison = results.register_comparison;
  if (!comparison || !comparison.register_available) {
    return null;
  }
  const matched = comparison.matched_count ?? 0;
  const unmatched = comparison.unmatched_count ?? 0;
  const unobservedFilters = comparison.unobserved_filters ?? [];
  const parts = [
    `${matched} ${matched === 1 ? "topic matches" : "topics match"} the register`,
    `${unmatched} not in register`,
    `${unobservedFilters.length} register ${
      unobservedFilters.length === 1 ? "topic" : "topics"
    } had no matching topic observed`,
  ];
  let note = parts.join(" · ");
  if (unobservedFilters.length > 0) {
    const shown = unobservedFilters.slice(0, 5).map((entry) => entry.filter);
    const remainder = unobservedFilters.length - shown.length;
    const list = remainder > 0 ? `${shown.join(", ")} (+${remainder} more)` : shown.join(", ");
    note += ` — unobserved: ${list}`;
  }
  return note;
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
    // Prefer the total issue count. Older runs expose only the severity-derived
    // blocking count, but the operator-facing label stays neutral: "issues".
    const issueCount = num("issue_count") ?? num("blocking_issue_count") ?? 0;
    return {
      primary: `${conformance}%`,
      primaryLabel: "payload conformance",
      secondary: String(issueCount),
      secondaryLabel: "issues found",
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
// yet" as a head that has never run (field engineer 2026-07-15: "it can't find anything,
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
