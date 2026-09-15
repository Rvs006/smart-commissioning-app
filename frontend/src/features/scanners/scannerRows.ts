import type {
  DiscoveryAssetObservation,
  DiscoveryResultsResponse,
  DiscoveryRowRecord,
  ObservedPort,
} from "../../api/client";
import { bacnetRowVerdict, ipRowVerdict, mqttRowsFromResults } from "../workflow/discoveryRows";
import {
  formatBacnetSidecarSummaryCards,
  formatIpSidecarSummaryCards,
  formatMqttSidecarSummaryCards,
} from "../workflow/ipDiscoveryModel";
import type { ScannerLane } from "./useScannerRun";

export type ChipTone = "neutral" | "pass" | "warn" | "fail" | "info";
export type RowTone = "pass" | "warn" | "fail" | null;

export type ScannerCell = {
  text: string;
  mono?: boolean;
  sub?: string;
  chip?: ChipTone;
};

export type ScannerRow = {
  /** Stable identity: survives a response reorder, so selection cannot drift. */
  id: string;
  title: string;
  tone: RowTone;
  /** The register state as the engine reported it: match/partial/missing/rogue, or "". */
  register: string;
  status: string;
  /** True for an expected device that never answered (no observed evidence). */
  missing: boolean;
  deviceInstance?: number;
  /** The persisted device attributes, empty for a missing (never-observed) row. */
  attributes: Record<string, unknown>;
  cells: Record<string, ScannerCell>;
};

export const IP_COLUMNS = [
  "Address",
  "Status",
  "Hostname",
  "Response",
  "MAC / Vendor",
  "Open ports",
  "Register",
] as const;

export const BACNET_COLUMNS = [
  "Instance",
  "Name",
  "Address",
  "Net",
  "Vendor",
  "Model",
  "Firmware",
  "Objects",
  "Register",
] as const;

// The MQTT capture table from the full-app artboard. "Register Match" is added
// to the artboard's five so the capture keeps the register verdict the v0.1.58
// table carried; Asset / Message count / Last payload seen / Detailed status
// ride each row's cell sub-lines and the detail panel, so no evidence is lost.
export const MQTT_COLUMNS = [
  "Topic",
  "Ret",
  "QoS",
  "JSON size",
  "Last value",
  "Register Match",
] as const;

export function scannerColumns(lane: ScannerLane): readonly string[] {
  return lane === "bacnet" ? BACNET_COLUMNS : lane === "mqtt" ? MQTT_COLUMNS : IP_COLUMNS;
}

const DASH = "—";

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return DASH;
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function attributesOf(record: DiscoveryRowRecord | undefined): Record<string, unknown> {
  const attributes = record?.attributes;
  return attributes && typeof attributes === "object"
    ? (attributes as Record<string, unknown>)
    : {};
}

// ponytail: a local copy of discoveryRows' private registerStateOf. The two
// sidecars name the key differently (`register` for IP, `register_state` for
// BACnet) and both spell "no register bound" as "none", which must read as no
// verdict rather than as a state. Track A owns that file and keeps the helper
// private; fold this back in if it is ever exported.
function registerStateOf(source: Record<string, unknown>): string {
  const value = source.register ?? source.register_state;
  return typeof value === "string" && value !== "none" ? value : "";
}

function numberList(value: unknown): number[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is number => typeof entry === "number")
    : [];
}

/**
 * The register chip. The verdict TONE is Track A's shared mapping
 * (ipRowVerdict / bacnetRowVerdict in discoveryRows.ts) so the two screens can
 * never disagree about what green / amber / red mean; only the label is short
 * here because it has to fit a chip.
 */
export function registerChip(state: string, tone: RowTone): ScannerCell {
  switch (state.toLowerCase()) {
    case "match":
      return { text: "Match", chip: "pass" };
    case "partial":
      return { text: "Partial", chip: "warn" };
    case "missing":
      return { text: "Missing", chip: "fail" };
    case "rogue":
      return { text: "Rogue", chip: "fail" };
    default:
      // No register bound to this run: the row is an observation, not a verdict.
      return { text: "Discovered", chip: tone === "fail" ? "fail" : "info" };
  }
}

function statusChip(status: string): ScannerCell {
  switch (status) {
    case "reachable":
      return { text: "Reachable", chip: "pass" };
    case "rogue":
      return { text: "Rogue host", chip: "fail" };
    case "unreachable":
      return { text: "Unreachable", chip: "fail" };
    default:
      return { text: status ? status : DASH, chip: "neutral" };
  }
}

function formatPorts(ports: unknown): string {
  if (Array.isArray(ports) && ports.length > 0 && typeof ports[0] === "object") {
    return (ports as ObservedPort[]).map((port) => `${port.port}/${port.protocol}`).join(", ");
  }
  const numbers = numberList(ports);
  return numbers.length > 0 ? numbers.join(", ") : DASH;
}

/** "reachable/match" -> "reachable". Both sidecars stamp the same shape. */
function statusFromDetail(statusDetail: unknown, fallback: string): string {
  const detail = typeof statusDetail === "string" ? statusDetail : "";
  const head = detail.split("/")[0]?.trim();
  return head && head !== "unknown" ? head : fallback;
}

/**
 * Rows for the mockup's column sets. These are NOT discoveryRows'
 * ipRowsFromResults / bacnetRowsFromResults: those project the older evidence
 * columns (Asset / Match Basis / Detailed Status) that the built-in lanes and
 * Run History still render. The verdict logic is shared — tone comes from
 * ipRowVerdict / bacnetRowVerdict and the state from registerStateOf — so only
 * the presentation differs.
 *
 * discovered_assets is the primary source for both lanes: since Track A it holds
 * one entry per expected AND observed device, including the expected-but-silent
 * ones that never enter devices[]. devices[] supplies the richer persisted
 * attributes, joined by address (IP) or device instance (BACnet).
 */
export function scannerRowsFromResults(
  lane: ScannerLane,
  results: DiscoveryResultsResponse | null | undefined,
): ScannerRow[] {
  if (!results) {
    return [];
  }
  return lane === "bacnet" ? bacnetRows(results) : lane === "mqtt" ? mqttRows(results) : ipRows(results);
}

/**
 * UTF-8 size of the STORED JSON payload — the same text the "Last value" cell
 * shows, so the two cells always describe one thing. The engine records no wire
 * message length, so this is not it; the column is named "JSON size" and carries
 * that caveat as its title. Unknown reads "—", never 0: a run with no payload,
 * and a non-JSON payload kept only as a presence marker, have no honest size.
 */
function payloadBytes(stored: string | null): string {
  if (stored === null) {
    return DASH;
  }
  try {
    return String(new TextEncoder().encode(stored).length);
  } catch {
    return String(stored.length);
  }
}

/** Column headers that need a caveat the header itself cannot carry. */
export const COLUMN_TITLES: Record<string, string> = {
  "JSON size":
    "Size of the stored JSON payload; the engine does not record the wire message length.",
};

/**
 * The engine's stored payload shapes, unwrapped for display exactly as the
 * v0.1.58 payload inspector did: `_raw_present` marks a non-JSON payload whose
 * bytes were NOT stored, and `_value` wraps a payload that was not a JSON object.
 * Returns the pretty-printed payload, or null when there is nothing to show.
 */
function displayPayload(value: unknown): {
  text: string | null;
  compact: string | null;
  rawOnly: boolean;
} {
  if (value === null || value === undefined) {
    return { text: null, compact: null, rawOnly: false };
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (record._raw_present === true) {
      return { text: null, compact: null, rawOnly: true };
    }
    const unwrapped = "_value" in record ? record._value : value;
    return {
      text: JSON.stringify(unwrapped, null, 2),
      compact: JSON.stringify(unwrapped),
      rawOnly: false,
    };
  }
  return { text: String(value), compact: String(value), rawOnly: false };
}

/**
 * MQTT capture rows. The per-topic projection is discoveryRows'
 * mqttRowsFromResults, verbatim, so the capture table and Run History can never
 * disagree about a topic's register verdict or its retained / QoS metadata; only
 * the column set and the chips differ here.
 */
function mqttRows(results: DiscoveryResultsResponse): ScannerRow[] {
  // mqttRowsFromResults maps results.topics 1:1 and in order, so the index also
  // reaches the topic record's real last_payload OBJECT. The stringified cell is
  // never re-parsed to get it back.
  return mqttRowsFromResults(results).map((row, index) => {
    const topic = row.Topic ?? DASH;
    const asset = row.Asset ?? DASH;
    const verdictText = row["Register Match"] ?? DASH;
    const tone: RowTone = row.__tone === "pass" ? "pass" : row.__tone === "fail" ? "fail" : null;
    // ScannerRow.register drives the chip filter; keep the two register words the
    // MQTT engine actually reports ("matched" / "unmatched") in the shared
    // vocabulary the filters use.
    const register = tone === "pass" ? "match" : tone === "fail" ? "rogue" : "";
    const payload = displayPayload(results.topics[index]?.last_payload);
    const retained = row.__retained;
    const lastSeen = row["Last Payload Seen"] ?? DASH;
    const messageCount = row["Message Count"] ?? DASH;
    const statusDetail = row["Detailed Status"] ?? DASH;
    return {
      id: topic === DASH ? `mqtt:row-${index}` : `mqtt:${topic}`,
      title: topic,
      tone,
      register,
      status: statusDetail,
      missing: false,
      attributes: {
        topic,
        asset,
        message_count: messageCount,
        last_payload_seen: lastSeen,
        status_detail: statusDetail,
        last_retained: retained,
        last_qos: row.__qos,
        subscribe_qos: row.__subscribeQos,
        last_payload: payload.text,
        payload_raw_only: payload.rawOnly,
        register_match: verdictText,
      },
      cells: {
        Topic: { text: topic, mono: true, sub: asset === DASH ? undefined : asset },
        // "" = the run predates per-message metadata: unknown reads "—", which
        // must not be confused with an observed "not retained".
        Ret: { text: retained === "yes" ? "✓" : retained === "no" ? "✗" : DASH },
        QoS: { text: row.__qos ? row.__qos : DASH, mono: true },
        // Measured on the SAME text "Last value" renders (payload.compact), not
        // on the re-serialized wrapper: {"_value":42} would have read 13 bytes
        // beside a cell showing "42".
        "JSON size": { text: payloadBytes(payload.compact), mono: true },
        "Last value": {
          text: payload.rawOnly ? "non-JSON (not stored)" : (payload.compact ?? DASH),
          mono: true,
          sub: `${messageCount} msg · ${lastSeen}`,
        },
        "Register Match": {
          text: verdictText,
          chip: tone === "pass" ? "pass" : tone === "fail" ? "fail" : "neutral",
        },
      },
    };
  });
}

function ipRows(results: DiscoveryResultsResponse): ScannerRow[] {
  const deviceByAddress = new Map<string, DiscoveryRowRecord>();
  for (const device of results.devices) {
    const address = typeof device.address === "string" ? device.address : "";
    if (address) {
      deviceByAddress.set(address, device);
    }
  }

  return results.discovered_assets.map((asset: DiscoveryAssetObservation, index) => {
    const address = text(asset.ip_address);
    const device = deviceByAddress.get(address);
    const attributes = attributesOf(device);
    const state = registerStateOf(asset as Record<string, unknown>);
    const verdict = ipRowVerdict(asset);
    const status = statusFromDetail(asset.status_detail, state === "missing" ? "unreachable" : "reachable");
    const missing = state === "missing";
    // A silent host resolved no hostname: the engine keeps `hostname` null and
    // carries the register's expectation in `expected_hostname`, so an
    // expectation is never rendered as a discovery.
    const hostname = text(missing ? asset.expected_hostname : (asset.hostname ?? device?.name));
    const vendor = text(device?.vendor);
    const latency = attributes.latency;
    return {
      id: address === DASH ? `ip:row-${index}` : `ip:${address}`,
      title: hostname === DASH ? address : hostname,
      tone: verdict.tone,
      register: state,
      status,
      missing,
      // A silent host has no device record; carry the register's expectation so
      // the panel can name what was expected without claiming it was observed.
      attributes: missing ? { expected_hostname: asset.expected_hostname } : attributes,
      cells: {
        Address: { text: address, mono: true },
        Status: statusChip(status),
        Hostname: { text: missing && hostname !== DASH ? `expected · ${hostname}` : hostname },
        Response: {
          text: latency === null || latency === undefined ? DASH : `${String(latency)} ms`,
          mono: true,
        },
        "MAC / Vendor": {
          text: text(asset.mac_address ?? attributes.mac_address),
          mono: true,
          sub: vendor === DASH ? undefined : vendor,
        },
        "Open ports": { text: formatPorts(asset.observed_ports), mono: true },
        Register: registerChip(state, verdict.tone),
      },
    };
  });
}

function bacnetRows(results: DiscoveryResultsResponse): ScannerRow[] {
  const deviceByInstance = new Map<string, DiscoveryRowRecord>();
  for (const device of results.devices) {
    const instance = attributesOf(device).device_instance;
    if (instance !== undefined && instance !== null) {
      deviceByInstance.set(String(instance), device);
    }
  }

  return results.discovered_assets.map((asset: DiscoveryAssetObservation, index) => {
    const instance = text(asset.device_instance);
    const device = deviceByInstance.get(instance);
    const attributes = attributesOf(device);
    const state = registerStateOf(asset as Record<string, unknown>);
    const verdict = bacnetRowVerdict(asset as Record<string, unknown>);
    const missing = state === "missing";
    const status = missing ? "unreachable" : state === "rogue" ? "rogue" : "reachable";
    const name = text(asset.name ?? device?.name);
    const network = attributes.network;
    // The sidecar does not stamp point_count on its observations (only the
    // built-in engine does), so the device's own reported object count is the
    // honest source, with the per-asset count as a fallback.
    const objectCount = attributes.object_count ?? asset.point_count;
    return {
      // The vendored compare() emits "—" for every register row with a blank or
      // unparseable device instance, so those rows must not share one key.
      id: instance === DASH ? `bacnet:row-${index}` : `bacnet:${instance}`,
      title: name === DASH ? `Device ${instance}` : name,
      tone: verdict.tone,
      register: state,
      status,
      missing,
      deviceInstance: Number.isInteger(Number(instance)) ? Number(instance) : undefined,
      attributes,
      cells: {
        Instance: { text: instance, mono: true },
        Name: { text: missing && name !== DASH ? `expected · ${name}` : name },
        Address: { text: text(attributes.ip_address ?? device?.address ?? asset.address), mono: true },
        Net: { text: missing ? DASH : network ? text(network) : "local", mono: true },
        Vendor: { text: text(asset.vendor ?? device?.vendor) },
        Model: { text: text(asset.model ?? device?.model) },
        Firmware: { text: text(asset.firmware ?? attributes.firmware) },
        Objects: { text: text(objectCount), mono: true },
        Register: registerChip(state, verdict.tone),
      },
    };
  });
}

export type SummaryPill = {
  label: string;
  value: string;
  chip: ChipTone;
};

// Chip colour per counter heading. The counters themselves come from Track A's
// formatIpSidecarSummaryCards / formatBacnetSidecarSummaryCards, so nothing is
// derived here: a field the engine did not report already renders "—".
const PILL_TONES: Record<string, ChipTone> = {
  Expected: "neutral",
  Reachable: "neutral",
  Match: "pass",
  Partial: "warn",
  Missing: "fail",
  Rogue: "fail",
};

export function scannerSummaryPills(
  lane: ScannerLane,
  summary: Record<string, unknown> | null | undefined,
): SummaryPill[] {
  const cards =
    (lane === "bacnet"
      ? formatBacnetSidecarSummaryCards(summary)
      : lane === "mqtt"
        ? formatMqttSidecarSummaryCards(summary)
        : formatIpSidecarSummaryCards(summary)) ?? [];
  return cards.map((card) => ({
    label: card.heading,
    value: card.value,
    chip: PILL_TONES[card.heading] ?? "neutral",
  }));
}

/**
 * The BACnet screen also shows the exported-object count beside the six RAG
 * pills (the full-app artboard's "128 objects"). Null when the run reported no
 * count, so nothing is invented.
 */
export function bacnetObjectsPill(
  summary: Record<string, unknown> | null | undefined,
): SummaryPill | null {
  const value = summary?.points_exported;
  if (typeof value !== "number") {
    return null;
  }
  return { label: value === 1 ? "object" : "objects", value: String(value), chip: "neutral" };
}

export type RagFilter = "all" | "match" | "partial" | "missing-rogue";

export const RAG_FILTERS: ReadonlyArray<{ id: RagFilter; label: string; chip: ChipTone }> = [
  { id: "all", label: "All", chip: "neutral" },
  { id: "match", label: "Match", chip: "pass" },
  { id: "partial", label: "Partial", chip: "warn" },
  { id: "missing-rogue", label: "Missing / Rogue", chip: "fail" },
];

// The MQTT capture compares a topic against the register's filters, so it only
// ever reports matched / not-in-register: no Partial, and no Missing row (an
// expected topic the capture never saw is reported in the register-comparison
// note, never as a row, because nothing was observed).
export const MQTT_RAG_FILTERS: ReadonlyArray<{ id: RagFilter; label: string; chip: ChipTone }> = [
  { id: "all", label: "All", chip: "neutral" },
  { id: "match", label: "Matched", chip: "pass" },
  { id: "missing-rogue", label: "Not in register", chip: "fail" },
];

export function ragFiltersFor(lane: ScannerLane) {
  return lane === "mqtt" ? MQTT_RAG_FILTERS : RAG_FILTERS;
}

export function rowMatchesRagFilter(row: ScannerRow, filter: RagFilter): boolean {
  if (filter === "all") {
    return true;
  }
  const register = row.register.toLowerCase();
  if (filter === "missing-rogue") {
    return register === "missing" || register === "rogue";
  }
  return register === filter;
}

export function rowMatchesText(row: ScannerRow, needle: string): boolean {
  const query = needle.trim().toLocaleLowerCase();
  if (!query) {
    return true;
  }
  return Object.values(row.cells).some((cell) =>
    `${cell.text} ${cell.sub ?? ""}`.toLocaleLowerCase().includes(query),
  );
}

// Detail-panel geometry (plan 4.5): 380px default, dragged between 320 and 640,
// remembered per browser.
export const SCANNER_PANEL_MIN_WIDTH = 320;
export const SCANNER_PANEL_MAX_WIDTH = 640;
export const SCANNER_PANEL_DEFAULT_WIDTH = 380;
export const SCANNER_PANEL_WIDTH_STORAGE_KEY = "sct.scannerDetailPanelWidth";

export function clampPanelWidth(width: number): number {
  if (!Number.isFinite(width)) {
    return SCANNER_PANEL_DEFAULT_WIDTH;
  }
  return Math.min(SCANNER_PANEL_MAX_WIDTH, Math.max(SCANNER_PANEL_MIN_WIDTH, Math.round(width)));
}

export type DetailItem = {
  label: string;
  value: string;
  tone?: RowTone;
  /** Offer a copy control beside the value (a raw payload, a banner). */
  copyable?: boolean;
};
export type DetailSection = {
  heading: string;
  items: DetailItem[];
  note?: string;
  noteTone?: RowTone;
};

/** One "proto/port name 🔒" + "product version · “title” · cert: CN" service row. */
export type ServiceLine = { head: string; detail: string };

export function serviceLines(services: unknown): ServiceLine[] {
  if (!Array.isArray(services)) {
    return [];
  }
  return services.map((entry) => {
    if (typeof entry === "string") {
      return { head: entry, detail: "" };
    }
    const record = (entry ?? {}) as Record<string, unknown>;
    const proto = record.proto ? String(record.proto) : "";
    const port = record.port === undefined || record.port === null ? "" : String(record.port);
    const name = record.name ? String(record.name) : "";
    const head = [proto && port ? `${proto}/${port}` : proto || port, name, record.tls ? "🔒" : ""]
      .filter(Boolean)
      .join(" ");
    // The vendored svcDescr(), verbatim in order: product+version, quoted title,
    // certificate CN, then the raw info string only when nothing else is known.
    const parts: string[] = [];
    if (record.product) {
      parts.push(`${String(record.product)}${record.version ? ` ${String(record.version)}` : ""}`);
    }
    if (record.title) {
      parts.push(`“${String(record.title)}”`);
    }
    if (record.certCN) {
      parts.push(`cert: ${String(record.certCN)}`);
    }
    if (parts.length === 0 && record.info) {
      parts.push(String(record.info));
    }
    return { head: head || JSON.stringify(entry), detail: parts.join(" · ") };
  });
}

function checkTone(status: unknown): RowTone {
  switch (String(status ?? "").toLowerCase()) {
    case "match":
      return "pass";
    case "mismatch":
      return "warn";
    default:
      return null;
  }
}

function checkLabel(status: unknown): string {
  switch (String(status ?? "").toLowerCase()) {
    case "match":
      return "Matches register";
    case "mismatch":
      return "Does not match register";
    case "unverified":
      return "Could not be verified on the network";
    default:
      return text(status);
  }
}

/** IP detail panel sections, in the order of plan section 4.2. */
export function ipDetailSections(row: ScannerRow): DetailSection[] {
  const a = row.attributes;
  const sections: DetailSection[] = [
    {
      heading: "Device overview",
      items: [
        { label: "IP address", value: row.cells.Address?.text ?? DASH },
        { label: "Hostname", value: row.cells.Hostname?.text ?? DASH },
        { label: "Type", value: text(a.device_type ?? a.type) },
        { label: "Vendor", value: row.cells["MAC / Vendor"]?.sub ?? DASH },
        { label: "Model", value: text(a.model) },
        { label: "MAC", value: row.cells["MAC / Vendor"]?.text ?? DASH },
        { label: "Banner", value: text(a.banner) },
      ],
    },
    {
      heading: "Live health",
      items: [
        { label: "Status", value: row.cells.Status?.text ?? DASH },
        { label: "Latency", value: row.cells.Response?.text ?? DASH },
        {
          label: "Found via",
          value: a.discovered_by ? String(a.discovered_by).toUpperCase() : DASH,
        },
      ],
    },
  ];

  if (a.hostname_status && String(a.hostname_status) !== "na") {
    sections.push({
      heading: "Hostname check",
      items: [
        { label: "Expected", value: text(a.expected_hostname) },
        {
          label: "Discovered",
          value:
            text(a.hostname ?? row.cells.Hostname?.text) +
            (a.hostname_src ? ` (${String(a.hostname_src)})` : ""),
        },
        {
          label: "Result",
          value: checkLabel(a.hostname_status),
          tone: checkTone(a.hostname_status),
        },
      ],
    });
  }

  const missingPorts = numberList(a.missing_ports);
  const extraPorts = numberList(a.extra_ports);
  const portNote =
    missingPorts.length > 0 || extraPorts.length > 0
      ? [
          missingPorts.length > 0 ? `Missing expected: ${missingPorts.join(", ")}` : "",
          extraPorts.length > 0 ? `Unexpected open: ${extraPorts.join(", ")}` : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : row.status === "reachable" && numberList(a.expected_ports).length > 0
        ? "All expected ports present, no extras."
        : undefined;
  sections.push({
    heading: "Network / Register",
    items: [
      { label: "Project", value: text(a.project) },
      { label: "Location", value: text(a.location) },
      { label: "Expected ports", value: formatPorts(a.expected_ports) },
      { label: "Description", value: text(a.description) },
    ],
    note: portNote,
    noteTone: missingPorts.length > 0 ? "fail" : extraPorts.length > 0 ? "warn" : "pass",
  });
  return sections;
}

/** BACnet detail panel sections, in the order of plan section 4.3. */
export function bacnetDetailSections(row: ScannerRow): DetailSection[] {
  const a = row.attributes;
  const objectDiff = a.object_diff ? `Object count: ${String(a.object_diff)}` : undefined;
  const sections: DetailSection[] = [
    {
      heading: "Device identity",
      items: [
        { label: "Instance", value: row.cells.Instance?.text ?? DASH },
        { label: "Name", value: row.cells.Name?.text ?? DASH },
        {
          label: "Vendor",
          value:
            (row.cells.Vendor?.text ?? DASH) +
            (a.vendor_id === undefined || a.vendor_id === null
              ? ""
              : ` (id ${String(a.vendor_id)})`),
        },
        { label: "Model", value: row.cells.Model?.text ?? DASH },
        { label: "Firmware", value: row.cells.Firmware?.text ?? DASH },
        { label: "App SW", value: text(a.app_software) },
        { label: "Location", value: text(a.location) },
        { label: "Description", value: text(a.description) },
      ],
    },
    {
      heading: "BACnet / network",
      items: [
        { label: "Address", value: row.cells.Address?.text ?? DASH },
        { label: "Network", value: a.network ? text(a.network) : "0 (local)" },
        { label: "MAC", value: text(a.mac) },
        { label: "Max APDU", value: text(a.max_apdu) },
        { label: "Segmentation", value: text(a.segmentation) },
        { label: "Protocol rev", value: text(a.protocol_revision) },
        { label: "System status", value: text(a.system_status) },
        { label: "Object count", value: text(a.object_count) },
      ],
      note: objectDiff,
      noteTone: objectDiff ? "warn" : undefined,
    },
  ];

  if (a.name_status && String(a.name_status) !== "na") {
    sections.push({
      heading: "Name check",
      items: [
        { label: "Expected", value: text(a.expected_name) },
        { label: "Reported", value: row.cells.Name?.text ?? DASH },
        { label: "Result", value: checkLabel(a.name_status), tone: checkTone(a.name_status) },
      ],
    });
  }
  if (a.mismatch) {
    sections.push({
      heading: "Register mismatch",
      items: [{ label: "Detail", value: text(a.mismatch), tone: "warn" }],
    });
  }
  return sections;
}

/**
 * MQTT capture detail panel. Every field the v0.1.58 capture table carried in a
 * column (asset, message count, last payload seen, detailed status) lives here
 * alongside the per-message metadata the old inspector showed, so narrowing the
 * table to the artboard's columns loses nothing. A blank metadata value means
 * the run predates that capture, and is said so rather than shown as a zero.
 */
export function mqttDetailSections(row: ScannerRow): DetailSection[] {
  const a = row.attributes;
  const notRecorded = (value: unknown) =>
    value === undefined || value === null || value === "" ? "Not recorded" : String(value);
  return [
    {
      heading: "Topic",
      items: [
        { label: "Topic", value: text(a.topic) },
        { label: "Asset", value: text(a.asset) },
        { label: "Messages", value: text(a.message_count) },
        { label: "Last payload seen", value: text(a.last_payload_seen) },
        { label: "Detailed status", value: text(a.status_detail) },
      ],
    },
    {
      heading: "Message metadata",
      items: [
        {
          label: "Retained",
          value:
            a.last_retained === "yes" ? "Yes" : a.last_retained === "no" ? "No" : "Not recorded",
        },
        { label: "Delivery QoS", value: notRecorded(a.last_qos) },
        { label: "Subscription QoS cap", value: notRecorded(a.subscribe_qos) },
      ],
      note:
        "Delivery QoS is min(publisher QoS, this run's subscription QoS), not the publisher's own.",
    },
    {
      heading: "Register",
      items: [
        { label: "Verdict", value: text(a.register_match), tone: row.tone },
      ],
    },
    {
      heading: "Last payload",
      items: a.payload_raw_only
        ? []
        : [{ label: "Raw", value: text(a.last_payload), copyable: true }],
      note: a.payload_raw_only
        ? "Non-JSON payload observed. The engine stores a presence marker, not the raw bytes."
        : undefined,
    },
  ];
}
