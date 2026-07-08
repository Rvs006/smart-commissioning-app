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

// Absolute calendar date-time for the Run History Started/Finished columns.
// Unlike formatRelativeTime this renders a fixed timestamp via the platform Intl
// API (toLocaleString) — no date library. Tolerant of missing/unparseable input
// (returns "—"/the raw string) with the same Date.parse guard.
export function formatAbsoluteTime(iso: string | undefined): string {
  if (!iso) {
    return "—";
  }
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) {
    return iso;
  }
  return new Date(parsed).toLocaleString();
}

// Humanised elapsed time between two ISO timestamps (updated_at − created_at) for
// the Run History Duration column. updated_at is only an honest finish for a
// terminal run, so callers gate on isTerminalStatus; returns "—" for missing or
// unparseable input or a negative delta rather than fabricating a duration.
export function formatDuration(startIso: string | undefined, endIso: string | undefined): string {
  if (!startIso || !endIso) {
    return "—";
  }
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) {
    return "—";
  }
  const totalSeconds = Math.round((end - start) / 1000);
  if (totalSeconds < 60) {
    return `${totalSeconds}s`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) {
    const seconds = totalSeconds % 60;
    return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

export const statusTokenLabels: Record<HealthState, string> = {
  failed: "Fail",
  queued: "Queued",
  ready: "Ready",
  running: "Running",
  warning: "Cancelled",
};
