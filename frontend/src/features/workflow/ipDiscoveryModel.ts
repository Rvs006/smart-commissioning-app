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
