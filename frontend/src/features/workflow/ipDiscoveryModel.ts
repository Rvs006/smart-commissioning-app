export type IpTargetRow = Readonly<{
  id: string;
  kind: "address" | "cidr" | "range";
  value: string;
  end?: string;
}>;

export type IpTargetExpression =
  | Readonly<{ kind: "address"; address: string }>
  | Readonly<{ kind: "cidr"; cidr: string }>
  | Readonly<{ kind: "range"; start: string; end: string }>;

export type ScanAuthorizationRecord = Readonly<{
  authorization_id: string;
  preview_run_id: string;
  packet_plan_sha256: string;
  not_before: string;
  not_after: string;
  max_uses: number;
  use_count: number;
  consumed_run_id: string | null;
  revoked_at: string | null;
}>;

export type ScanAuthorizationState =
  | "no_access"
  | "none_available"
  | "not_started"
  | "expired"
  | "revoked"
  | "exhausted"
  | "drift_invalidated"
  | "valid";

export const IP_HEADLINE_METRIC_HEADINGS = [
  "Expected Devices",
  "Reachable Devices",
  "Register Matches",
  "Unexpected / Unregistered Hosts",
] as const;

export type IpHeadlineMetricHeading = (typeof IP_HEADLINE_METRIC_HEADINGS)[number];

export type IpHeadlineMetricDisplay = Readonly<{
  heading: IpHeadlineMetricHeading;
  value: string;
  progress: string | null;
}>;

export const BACNET_HEADLINE_METRIC_HEADINGS = [
  "Expected Devices",
  "Discovered Devices",
  "Objects Discovered",
  "Unmatched / Unexpected",
] as const;

export type BacnetHeadlineMetricDisplay = Readonly<{
  heading: (typeof BACNET_HEADLINE_METRIC_HEADINGS)[number];
  value: string;
  progress: string | null;
}>;

export type BacnetRouterDisplay = Readonly<{
  address: string;
  networks: string;
}>;

function serializeTargetRow(row: IpTargetRow): IpTargetExpression {
  const value = row.value.trim();
  const end = row.end?.trim() ?? "";
  if (!value || (row.kind === "range" && !end)) {
    throw new Error("Complete every target and exclusion row before previewing the plan.");
  }
  if (row.kind === "address") {
    return { kind: "address", address: value };
  }
  if (row.kind === "cidr") {
    return { kind: "cidr", cidr: value };
  }
  return { kind: "range", start: value, end };
}

export function serializeIpTargetRows(
  targets: readonly IpTargetRow[],
  exclusions: readonly IpTargetRow[],
): Readonly<{
  target_expressions: IpTargetExpression[];
  exclusions: IpTargetExpression[];
}> {
  return {
    target_expressions: targets.map(serializeTargetRow),
    exclusions: exclusions.map(serializeTargetRow),
  };
}

export function classifyScanAuthorization(
  input: Readonly<{
    accessAllowed: boolean;
    authorization: ScanAuthorizationRecord | undefined;
    now: Date;
    previewRunId: string;
    packetPlanSha256: string;
  }>,
): ScanAuthorizationState {
  if (!input.accessAllowed) {
    return "no_access";
  }
  const authorization = input.authorization;
  if (!authorization) {
    return "none_available";
  }
  if (
    authorization.preview_run_id !== input.previewRunId ||
    authorization.packet_plan_sha256 !== input.packetPlanSha256
  ) {
    return "drift_invalidated";
  }
  if (authorization.revoked_at) {
    return "revoked";
  }
  if (authorization.use_count >= authorization.max_uses || authorization.consumed_run_id !== null) {
    return "exhausted";
  }
  const now = input.now.getTime();
  const starts = Date.parse(authorization.not_before);
  const ends = Date.parse(authorization.not_after);
  if (!Number.isFinite(now) || !Number.isFinite(starts) || !Number.isFinite(ends)) {
    return "drift_invalidated";
  }
  if (now < starts) {
    return "not_started";
  }
  if (now >= ends) {
    return "expired";
  }
  return "valid";
}

type MetricValue = Readonly<{
  schema_version: "1.0";
  heading: IpHeadlineMetricHeading;
  configured: boolean;
  value: number | null;
  denominator: number | null;
  percentage: number | null;
  pending_count: number | null;
  finalized_count: number | null;
}>;

function isNullableCount(value: unknown): value is number | null {
  return value === null || (Number.isInteger(value) && Number(value) >= 0);
}

function metricValue(value: unknown, heading: string): MetricValue {
  if (!value || typeof value !== "object") {
    throw new Error("Invalid IP headline metric snapshot.");
  }
  const candidate = value as Record<string, unknown>;
  if (
    candidate.schema_version !== "1.0" ||
    candidate.heading !== heading ||
    typeof candidate.configured !== "boolean" ||
    !isNullableCount(candidate.value) ||
    !isNullableCount(candidate.denominator) ||
    !isNullableCount(candidate.pending_count) ||
    !isNullableCount(candidate.finalized_count) ||
    !(
      candidate.percentage === null ||
      (typeof candidate.percentage === "number" &&
        Number.isFinite(candidate.percentage) &&
        candidate.percentage >= 0 &&
        candidate.percentage <= 100)
    )
  ) {
    throw new Error("Invalid IP headline metric snapshot.");
  }
  if (
    (!candidate.configured &&
      [
        candidate.value,
        candidate.denominator,
        candidate.percentage,
        candidate.pending_count,
        candidate.finalized_count,
      ].some((item) => item !== null)) ||
    (candidate.configured &&
      [
        candidate.value,
        candidate.denominator,
        candidate.pending_count,
        candidate.finalized_count,
      ].some((item) => item === null))
  ) {
    throw new Error("Invalid IP headline metric snapshot.");
  }
  return candidate as MetricValue;
}

function formatPercentage(value: number): string {
  return Number.isInteger(value)
    ? value.toFixed(0)
    : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

export function formatIpHeadlineMetrics(value: unknown): IpHeadlineMetricDisplay[] {
  if (!value || typeof value !== "object") {
    throw new Error("Invalid IP headline metric snapshot.");
  }
  const snapshot = value as Record<string, unknown>;
  if (
    snapshot.schema_version !== "1.0" ||
    !Array.isArray(snapshot.metrics) ||
    snapshot.metrics.length !== IP_HEADLINE_METRIC_HEADINGS.length
  ) {
    throw new Error("Invalid IP headline metric snapshot.");
  }
  const metrics = snapshot.metrics;
  return IP_HEADLINE_METRIC_HEADINGS.map((heading, index) => {
    const metric = metricValue(metrics[index], heading);
    if (!metric.configured) {
      return { heading, value: "Not configured", progress: null };
    }
    const valueText =
      metric.denominator === 0 || metric.percentage === null
        ? String(metric.value)
        : `${metric.value} / ${metric.denominator} (${formatPercentage(metric.percentage)}%)`;
    return {
      heading,
      value: valueText,
      progress: `${metric.finalized_count} finalized, ${metric.pending_count} pending`,
    };
  });
}

export type IpSidecarSummaryCard = Readonly<{ heading: string; value: string }>;

// GAP-C2: the summary strip for the native IP sidecar lane. The sidecar engine
// stamps its totals straight onto result_summary, not the sealed lane's
// ip_headline_metrics_v1 snapshot, so this reads them directly. All six of the
// sidecar's compare() counters are shown (expected / reachable / match /
// partial / missing / rogue) — Partial and Missing were stamped by the engine
// but never surfaced, so an operator could not see that expected devices had
// gone silent. Returns null when none is a number (a dry-run, an older run, or
// a failed scan has nothing to show, so the strip is omitted rather than
// faked); a present-but-null field renders "—", never an invented count.
// "Reachable" counts everything that answered, rogues included (the sidecar's
// summary.reachable), which is what this card has always shown.
const IP_SIDECAR_SUMMARY_FIELDS = [
  ["Expected", "register_expected"],
  ["Reachable", "hosts_scanned"],
  ["Match", "register_matches"],
  ["Partial", "register_partial"],
  ["Missing", "register_missing"],
  ["Rogue", "register_rogue"],
] as const;

export function formatIpSidecarSummaryCards(
  summary: Record<string, unknown> | null | undefined,
): IpSidecarSummaryCard[] | null {
  if (!summary || typeof summary !== "object") {
    return null;
  }
  const anyPresent = IP_SIDECAR_SUMMARY_FIELDS.some(
    ([, key]) => typeof summary[key] === "number",
  );
  if (!anyPresent) {
    return null;
  }
  return IP_SIDECAR_SUMMARY_FIELDS.map(([heading, key]) => {
    const value = summary[key];
    return { heading, value: typeof value === "number" ? String(value) : "—" };
  });
}

// GAP-C2 (BACnet): the summary strip for the native BACnet sidecar lane. The
// bacnet_scanner engine stamps these totals straight onto result_summary, so
// read them directly, exactly like the IP variant, and show the same six
// register counters so the two scanner screens read alike. Same
// null-when-none / "—"-for-null-field contract so a dry-run or an older run
// omits the strip rather than faking counts. "Reachable" is
// devices_discovered: every device that answered, rogues included.
const BACNET_SIDECAR_SUMMARY_FIELDS = [
  ["Expected", "register_expected"],
  ["Reachable", "devices_discovered"],
  ["Match", "register_matches"],
  ["Partial", "register_partial"],
  ["Missing", "register_missing"],
  ["Rogue", "register_rogue"],
] as const;

export function formatBacnetSidecarSummaryCards(
  summary: Record<string, unknown> | null | undefined,
): IpSidecarSummaryCard[] | null {
  if (!summary || typeof summary !== "object") {
    return null;
  }
  // points_exported is checked but NOT displayed. The engine computes it as a
  // sum, so it is a number on every real bacnet_scanner run even when every
  // register counter is null (a cancelled or empty scan); dropping it from the
  // strip must not also drop it from the "is this a scanner run at all?" test,
  // or such a run would lose its summary entirely instead of showing dashes.
  const anyPresent =
    typeof summary.points_exported === "number" ||
    BACNET_SIDECAR_SUMMARY_FIELDS.some(([, key]) => typeof summary[key] === "number");
  if (!anyPresent) {
    return null;
  }
  return BACNET_SIDECAR_SUMMARY_FIELDS.map(([heading, key]) => {
    const value = summary[key];
    return { heading, value: typeof value === "number" ? String(value) : "—" };
  });
}

// GAP-C2 (MQTT): the four-card summary strip for the native MQTT sidecar lane.
// The mqtt_scanner engine stamps these onto result_summary
// (topics_discovered / assets_discovered / register_matches / register_rogue),
// so read them directly like the IP/BACnet variants. Same null-when-none /
// "—"-for-null-field contract so a dry-run or an older run omits the strip.
const MQTT_SIDECAR_SUMMARY_FIELDS = [
  ["Topics", "topics_discovered"],
  ["Assets", "assets_discovered"],
  // "Match", not "Matches", so the same counter is named the same way on all
  // three scanner screens.
  ["Match", "register_matches"],
  ["Rogue", "register_rogue"],
] as const;

export function formatMqttSidecarSummaryCards(
  summary: Record<string, unknown> | null | undefined,
): IpSidecarSummaryCard[] | null {
  if (!summary || typeof summary !== "object") {
    return null;
  }
  const anyPresent = MQTT_SIDECAR_SUMMARY_FIELDS.some(
    ([, key]) => typeof summary[key] === "number",
  );
  if (!anyPresent) {
    return null;
  }
  return MQTT_SIDECAR_SUMMARY_FIELDS.map(([heading, key]) => {
    const value = summary[key];
    return { heading, value: typeof value === "number" ? String(value) : "—" };
  });
}

/**
 * Project `result_summary.routers` (stamped by the bacnet_scanner engine) into
 * display rows. Returns null when the key is absent or not a list — a pre-router
 * run, or the built-in engine, records nothing, so the UI renders no section;
 * an empty array means the scan heard no router (the UI shows the "none
 * responded" note). Malformed entries and blank addresses are skipped, never
 * faked; networks are joined into a display string ("" when none advertised).
 */
export function formatBacnetRouters(value: unknown): BacnetRouterDisplay[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const rows: BacnetRouterDisplay[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== "object") {
      continue;
    }
    const record = entry as Record<string, unknown>;
    const address = typeof record.address === "string" ? record.address.trim() : "";
    if (!address) {
      continue;
    }
    const networks = Array.isArray(record.networks)
      ? record.networks.filter((net): net is number => typeof net === "number").join(", ")
      : "";
    rows.push({ address, networks });
  }
  return rows;
}

export function formatBacnetHeadlineMetrics(value: unknown): BacnetHeadlineMetricDisplay[] {
  if (!value || typeof value !== "object") {
    throw new Error("Invalid BACnet headline metric snapshot.");
  }
  const snapshot = value as Record<string, unknown>;
  if (
    snapshot.schema_version !== "1.0" ||
    !Array.isArray(snapshot.metrics) ||
    snapshot.metrics.length !== BACNET_HEADLINE_METRIC_HEADINGS.length
  ) {
    throw new Error("Invalid BACnet headline metric snapshot.");
  }
  const metrics = snapshot.metrics;
  return BACNET_HEADLINE_METRIC_HEADINGS.map((heading, index) => {
    const metric = metricValue(metrics[index], heading);
    if (!metric.configured) {
      return { heading, value: "Not configured", progress: null };
    }
    const valueText =
      metric.denominator === 0 || metric.percentage === null
        ? String(metric.value)
        : `${metric.value} / ${metric.denominator} (${formatPercentage(metric.percentage)}%)`;
    return {
      heading,
      value: valueText,
      progress: `${metric.finalized_count} finalized, ${metric.pending_count} pending`,
    };
  });
}
