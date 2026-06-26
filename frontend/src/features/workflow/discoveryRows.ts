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
    const seen = num("publishing_seen") ?? 0;
    const conformance = expected > 0 ? Math.round((seen / expected) * 100) : 0;
    return {
      primary: `${conformance}%`,
      primaryLabel: "payload conformance",
      secondary: String(num("issue_count") ?? 0),
      secondaryLabel: "blocking issues",
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
