import type { NmapProfileName } from "../../api/client";
import type { ModuleRunAction } from "./moduleData";
import { serializeIpTargetRows, type IpTargetRow } from "./ipDiscoveryModel";

export type ScanPort = {
  port: string;
  protocol: "tcp" | "udp";
};

export type IPDiscoveryProvider = "builtin_tcp_connect" | "operator_managed_nmap";

export function scanPortSpecification(ports: ScanPort[]): string {
  return ports
    .map((entry) => ({ port: entry.port.trim(), protocol: entry.protocol }))
    .filter((entry) => entry.port)
    .map((entry) => `${entry.port}/${entry.protocol}`)
    .join(", ");
}

// BACnet device-instance bounds (0 .. 2^22-1). A device-instance range is
// pair-or-neither: the vendored scanner sends a bounded Who-Is only when BOTH
// low and high arrive; a lone bound falls through to a global Who-Is. So a
// half-filled or invalid range must never reach the wire (it would silently
// degrade to "scan everything" while the UI showed the operator's bound as
// accepted). One rule, shared by the builder and the Run gate.
const BACNET_INSTANCE_MIN = 0;
const BACNET_INSTANCE_MAX = 4_194_303;

export function resolveBacnetInstanceRange(
  lowRaw: string | undefined,
  highRaw: string | undefined,
): { low?: number; high?: number; error: string | null } {
  const lo = (lowRaw ?? "").trim();
  const hi = (highRaw ?? "").trim();
  if (lo === "" && hi === "") {
    return { error: null }; // both blank -> global Who-Is (the sidecar default)
  }
  const low = Number(lo);
  const high = Number(hi);
  const valid =
    lo !== "" &&
    hi !== "" &&
    Number.isInteger(low) &&
    Number.isInteger(high) &&
    low >= BACNET_INSTANCE_MIN &&
    high <= BACNET_INSTANCE_MAX &&
    low <= high;
  if (!valid) {
    return {
      error:
        "Enter both bounds or leave both blank. Low must be a whole number no greater than high, within 0 to 4194303.",
    };
  }
  return { low, high, error: null };
}

// Builds discovery run parameters, attaching the authorization contract for
// real scans and the dry_run flag for previews. IP scans also carry the port
// specification. Mirrors the backend safety contract (parameters.authorized).
// Pure builder — no component in this module, so no react-refresh disable is needed.
export function buildDiscoveryParameters(
  action: Extract<ModuleRunAction, { kind: "discovery" }>,
  options: {
    authorized: boolean;
    dryRun: boolean;
    scanPorts: ScanPort[];
    targetRows?: IpTargetRow[];
    exclusionRows?: IpTargetRow[];
    provider?: IPDiscoveryProvider;
    nmapProfile?: NmapProfileName;
    captureTopicFilter?: string;
    captureSeconds?: string;
    target?: string;
    scanRangeStart?: string;
    scanRangeEnd?: string;
    probeTimeout?: string;
    ignoreRegister?: boolean;
    bacnetInstanceLow?: string;
    bacnetInstanceHigh?: string;
    bacnetDiscoverMs?: string;
  },
): Record<string, unknown> {
  const parameters: Record<string, unknown> = {};
  if (options.dryRun) {
    parameters.dry_run = true;
  } else {
    // Boolean shorthand only — the backend stamps the real authenticated
    // principal, so the frontend never fabricates a scan_authorization block.
    parameters.authorized = options.authorized;
  }
  if (action.runKind === "ip") {
    parameters.provider = options.provider ?? "builtin_tcp_connect";
    if (parameters.provider === "operator_managed_nmap") {
      parameters.nmap_profile = options.nmapProfile ?? "tcp_connect_inventory";
    }
    if (
      parameters.provider !== "operator_managed_nmap" ||
      options.nmapProfile !== "host_discovery"
    ) {
      parameters.port_specification = scanPortSpecification(options.scanPorts);
    }
    const targetRows = options.targetRows ?? [];
    const exclusionRows = options.exclusionRows ?? [];
    if (targetRows.length > 0 || exclusionRows.length > 0) {
      const expressions = serializeIpTargetRows(targetRows, exclusionRows);
      parameters.target_expressions = expressions.target_expressions;
      parameters.exclusions = expressions.exclusions;
      // A register-driven scan may still carry exclusions. The backend requires
      // this explicit opt-in before it expands registered addresses, rather than
      // treating an empty target editor as permission to scan them.
      if (expressions.target_expressions.length === 0) {
        parameters.use_register_addresses = true;
      }
      return parameters;
    }
    // Compatibility fallback for existing deep links and saved drafts. A blank
    // target list deliberately scans the imported IP register, but the backend
    // requires that intent on the wire before it will expand those addresses.
    const target = options.target?.trim();
    if (target) {
      if (target.includes("/")) {
        parameters.cidr = target;
      } else if (target.includes("-")) {
        // Split once on the first "-" so the operator's input reaches the
        // backend intact (JS split(limit) would drop any trailing segment).
        const dash = target.indexOf("-");
        parameters.start = target.slice(0, dash).trim();
        parameters.end = target.slice(dash + 1).trim();
      } else {
        parameters.addresses = [target];
      }
    } else {
      parameters.use_register_addresses = true;
    }
  }
  // IP sidecar lane: forward the operator's target range as start_ip / end_ip
  // (the adapter's _scan_query accepts either start_ip/end_ip or start/end and
  // requires a start). A blank end scans from start; a blank start reaches the
  // adapter's honest "No scan range was provided" failure rather than a silent
  // register-only scan.
  if (action.runKind === "ip_sidecar") {
    const start = options.scanRangeStart?.trim();
    const end = options.scanRangeEnd?.trim();
    if (start) {
      parameters.start_ip = start;
    }
    if (end) {
      parameters.end_ip = end;
    }
    // GAP-IP1: per-probe timeout (ms). Only a positive finite value goes on the
    // wire; a blank or garbage field omits the key so the adapter's own default
    // applies rather than a bogus timeout.
    const timeout = Number((options.probeTimeout ?? "").trim());
    if (Number.isFinite(timeout) && timeout > 0) {
      parameters.timeout = timeout;
    }
    // GAP-C1: opt this run out of register RAG-comparison. The route's binder
    // reads this and skips freezing a register in.
    if (options.ignoreRegister) {
      parameters.ignore_register = true;
    }
  }
  // GAP-B1: BACnet sidecar lane. Forward the operator's device-instance range as
  // low/high and the discovery window as discoverMs (the adapter's _scan_query
  // reads exactly these keys). The range is a validated pair (see below); a blank
  // range omits both keys so the sidecar's global Who-Is default applies.
  // discoverMs is a duration (> 0) and stays per-key.
  if (action.runKind === "bacnet_sidecar") {
    // Pair-or-neither: emit low/high only as a validated range, otherwise omit
    // BOTH. A lone or inverted bound would fall through to a global Who-Is on the
    // sidecar while looking accepted. This also catches a saved draft or deep link
    // carrying a half-filled range that never passed through the live Run gate.
    const range = resolveBacnetInstanceRange(options.bacnetInstanceLow, options.bacnetInstanceHigh);
    if (range.low !== undefined && range.high !== undefined) {
      parameters.low = range.low;
      parameters.high = range.high;
    }
    const discoverMs = Number((options.bacnetDiscoverMs ?? "").trim());
    if (Number.isFinite(discoverMs) && discoverMs > 0) {
      parameters.discoverMs = discoverMs;
    }
    // GAP-C1: opt this run out of register RAG-comparison, same as ip_sidecar.
    if (options.ignoreRegister) {
      parameters.ignore_register = true;
    }
  }
  // MQTT discovery: forward the operator's topic filter and capture window so
  // the engine subscribes to the requested topics for the requested duration
  // (mq9nhbzu). The backend reads topic_filter + capture_seconds.
  if (action.runKind === "mqtt") {
    const filter = options.captureTopicFilter?.trim();
    if (filter) {
      parameters.topic_filter = filter;
    }
    // Blank => 0, the backend's "indefinite" sentinel: run until stopped (Stop
    // run) or the message cap. A positive value is a bounded capture window.
    // Anything else ("45s", "abc", "-5") is REJECTED at submit, mirroring the
    // UDMI run-time path — silently coercing it to 0 would turn an intended
    // bounded window into an unbounded background capture with no warning
    // (mq9nhbzu). The thrown Error surfaces through the runMutation error panel.
    const raw = (options.captureSeconds ?? "").trim();
    const seconds = Number(raw);
    if (raw !== "" && !(Number.isFinite(seconds) && seconds > 0)) {
      throw new Error(
        "Run time must be a positive number, or blank to capture until you press Stop run.",
      );
    }
    parameters.capture_seconds = raw === "" ? 0 : seconds;
  }
  if (action.runKind === "mqtt_sidecar") {
    // Sidecar capture lane: same operator inputs, bounded-capture semantics. The
    // adapter reads topic_filter (a root-filter alias) and capture_seconds; blank
    // omits the key so the engine's own defaults apply (# / 60s) — never a literal
    // "#" or a 0-sentinel on the wire (this lane has no indefinite mode; its
    // window is bounded 1-900s, clamped by the adapter).
    const filter = options.captureTopicFilter?.trim();
    if (filter) {
      parameters.topic_filter = filter;
    }
    const raw = (options.captureSeconds ?? "").trim();
    const seconds = Number(raw);
    if (raw !== "" && !(Number.isFinite(seconds) && seconds > 0)) {
      throw new Error(
        "Run time must be a positive number, or blank for the 60-second default window.",
      );
    }
    if (raw !== "") {
      parameters.capture_seconds = seconds;
    }
  }
  return parameters;
}
