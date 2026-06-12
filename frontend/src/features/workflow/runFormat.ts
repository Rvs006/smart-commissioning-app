import type { JobStatus, JobType } from "../../api/client";
import type { HealthState } from "./operatorData";

// Terminal run states. Polling stops once a run reaches one of these; issues
// and results are only fetched after a run is terminal. Shared so Dashboard and
// ModulePage use the same definition as the original UDMI monitor.
export const terminalStatuses: JobStatus[] = ["succeeded", "failed", "cancelled"];

export function isTerminalStatus(status: JobStatus | undefined): boolean {
  return Boolean(status) && terminalStatuses.includes(status as JobStatus);
}

// Maps a backend JobStatus onto the frontend HealthState used for status tokens.
// succeeded -> ready, cancelled -> warning; queued/running/failed pass through.
export function toHealthState(status: JobStatus): HealthState {
  if (status === "succeeded") {
    return "ready";
  }
  if (status === "cancelled") {
    return "warning";
  }
  return status;
}

const jobTypeLabels: Record<JobType, string> = {
  ip_discovery: "IP discovery",
  bacnet_discovery: "BACnet discovery",
  mqtt_discovery: "MQTT discovery",
  udmi_validation: "UDMI validation",
  mqtt_config_publish: "MQTT config publish",
  bacnet_validation: "BACnet validation",
  mapping_validation: "Mapping validation",
  report_generation: "Report generation",
};

export function humanizeJobType(jobType: JobType): string {
  return jobTypeLabels[jobType] ?? jobType.replace(/_/g, " ");
}

export function humanizeStage(stage: string | undefined): string {
  if (!stage) {
    return "";
  }
  return stage.replace(/_/g, " ");
}

// Compact relative-time formatter for run timestamps. Returns "Just now" for
// very recent updates and falls back to a date for older ones. Tolerant of
// unparseable input (returns the raw string).
export function formatRelativeTime(iso: string | undefined, now: number = Date.now()): string {
  if (!iso) {
    return "Unknown";
  }
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) {
    return iso;
  }
  const diffSeconds = Math.round((now - parsed) / 1000);
  if (diffSeconds < 5) {
    return "Just now";
  }
  if (diffSeconds < 60) {
    return `${diffSeconds} sec ago`;
  }
  const diffMinutes = Math.round(diffSeconds / 60);
  if (diffMinutes < 60) {
    return `${diffMinutes} min ago`;
  }
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours} hr ago`;
  }
  const diffDays = Math.round(diffHours / 24);
  if (diffDays < 30) {
    return `${diffDays} day${diffDays === 1 ? "" : "s"} ago`;
  }
  return new Date(parsed).toLocaleDateString();
}

export const statusTokenLabels: Record<HealthState, string> = {
  failed: "Fail",
  queued: "Queued",
  ready: "Ready",
  running: "Running",
  warning: "Cancelled",
};
