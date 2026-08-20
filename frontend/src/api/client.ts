import type { SessionScopeId, WorkspaceRef } from "../app/sessionScope";
import { isPlainObject } from "../utils/isPlainObject";

export type HealthStatus = {
  status: string;
  version: string;
  timestamp: string;
};

// RBAC roles, ascending privilege. Mirrors smart_commissioning_core.rbac.Role
// (StrEnum: serialized as the lowercase string). Declaration order here matches
// the backend ROLE_ORDER so roleAtLeast() can compare by index.
export const ROLE_ORDER = ["viewer", "reviewer", "engineer", "admin"] as const;
export type Role = (typeof ROLE_ORDER)[number];

// True when `role` has at least `minimum` privilege. Unknown roles rank lowest
// (fail-closed): a principal with an unrecognised role is treated as below any
// real minimum, so gated actions stay hidden rather than wrongly enabled.
export function roleAtLeast(role: Role | string | undefined, minimum: Role): boolean {
  const roleRank = ROLE_ORDER.indexOf(role as Role);
  const minRank = ROLE_ORDER.indexOf(minimum);
  if (roleRank < 0) {
    return false;
  }
  return roleRank >= minRank;
}

// GET /api/v1/me — the current principal. source distinguishes a per-user key
// from the bootstrap shared/local admin.
export type EffectiveScope = {
  project_id: string;
  site_id: string;
};

export type MeResponse = {
  username: string;
  role: Role;
  source: "user_key" | "shared_key" | "local";
  global_scope: boolean;
  effective_scopes: EffectiveScope[];
};

// A user as returned by the admin /users endpoints (never includes key material).
export type UserRecord = {
  id: string;
  username: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
};

// POST /api/v1/users (create) and POST /api/v1/users/{id}/key (re-issue) return
// the user PLUS the plaintext key, displayed exactly once per issuance.
export type CreateUserResponse = {
  user: UserRecord;
  api_key: string;
};

export type ScopeGrantRecord = {
  grant_id: string;
  user_id: string;
  project_id: string;
  site_id: string;
  active: boolean;
  granted_by: string;
  reason: string;
  granted_at: string;
  revoked_by: string | null;
  revoke_reason: string | null;
  revoked_at: string | null;
};

export type ScopeActivationPreflight = {
  ready: boolean;
  active_named_admin_count: number;
  unscoped_active_non_admin_users: Array<{
    id: string;
    username: string;
    role: Role;
  }>;
};

export type ConfigurationSection = {
  values: Record<string, string>;
  status: string;
};

export type ConfigurationSnapshot = {
  device: ConfigurationSection;
  bacnet: ConfigurationSection;
  mqtt: ConfigurationSection;
  certificates: ConfigurationSection;
  time: ConfigurationSection;
  backups: ConfigurationSection;
  logging: ConfigurationSection;
};

export type ConfigurationSectionKey = keyof ConfigurationSnapshot;

export type ConfigurationValidationResult = {
  valid: boolean;
  errors: string[];
};

export type SecretMaterialResponse = {
  secret_ref: string;
  field: string;
  file_name: string | null;
  fingerprint: string;
  validity: string;
  expiry: string | null;
  masked: boolean;
};

export type ImportType =
  | "ip_register"
  | "ip_scanner_register"
  | "bacnet_scanner_register"
  | "mqtt_scanner_register"
  | "bacnet_register"
  | "mqtt_register"
  | "asset_validation"
  | "bacnet_points"
  | "mqtt_points"
  | "mapping"
  | "tolerances";

export type ImportStatus = "accepted" | "rejected" | "partial";

export type ImportProfileSummary = {
  import_type: ImportType;
  description: string;
  required_columns: string[];
  optional_columns?: string[];
  duplicate_key_fields: string[];
};

// Informational note about an ACCEPTED row (e.g. a UDP port entry the
// TCP-only IP scan can never verify). Same shape as a backend
// ImportErrorRecord but delivered on the summary's separate warnings list,
// so it never counts as a rejection.
export type ImportWarningRecord = {
  row_number: number | null;
  field: string | null;
  code: string;
  message: string;
};

// Reason a row (or the file) was REJECTED. Byte-identical mirror of the same
// backend model as the warning record (schemas/imports.py:28-32), so it is
// aliased rather than redefined. Both row_number and field are nullable and
// each nullability occurs in real data: missing_required_column records carry a
// field but no row_number, duplicate_row records a row_number but no field.
export type ImportErrorRecord = ImportWarningRecord;

export type ImportErrorReport = {
  import_id: string;
  errors: ImportErrorRecord[];
};

export type ImportBatchSummary = {
  import_id: string;
  import_type: ImportType;
  file_name: string;
  file_type: "csv" | "xlsx";
  project_id: string | null;
  site_id: string | null;
  total_rows: number;
  accepted_rows: number;
  rejected_rows: number;
  status: ImportStatus;
  missing_columns: string[];
  // Optional so summaries stored before the field existed remain valid.
  warnings?: ImportWarningRecord[];
  stored_file_name: string;
  created_at: string;
};

export type JobType =
  | "ip_discovery"
  | "ip_scanner"
  | "bacnet_scanner"
  | "mqtt_scanner"
  | "bacnet_discovery"
  | "mqtt_discovery"
  | "udmi_validation"
  | "mqtt_config_publish"
  | "bacnet_validation"
  | "mapping_validation"
  | "report_generation";

export type JobAcceptedResponse = {
  run_id: string;
  job_type: JobType;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  message: string;
};

export type JobStatus = JobAcceptedResponse["status"];

export type RunRecord = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  stage: string;
  progress_percent: number;
  created_at: string;
  updated_at: string;
  // Originating edge id; null for a run created on the local edge, populated for
  // runs ingested from another edge. Additive — mirrors the backend JobSummary.
  edge_id: string | null;
  project_id: string;
  site_id: string;
  parameters: Record<string, unknown>;
  result_summary: Record<string, unknown>;
  error_message: string | null;
};

export type ScanAuthorizationV1 = {
  authorization_id: string;
  preview_run_id: string;
  project_id: string;
  site_id: string;
  packet_plan_sha256: string;
  approved_by: string;
  ticket: string;
  purpose: string;
  not_before: string;
  not_after: string;
  max_uses: number;
  use_count: number;
  consumed_run_id: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  revoke_reason: string | null;
  created_at: string;
};

export type ValidationIssueRecord = {
  issue_id: string;
  asset_id: string | null;
  issue_type: string;
  severity: "low" | "medium" | "high" | "critical";
  description: string;
  status?: string | null;
  point_name?: string | null;
  topic?: string | null;
  expected_value?: string | null;
  observed_value?: string | null;
  match_basis?: string | null;
  // Exact JSON Pointer into the compared payload when the validation engine
  // can identify one. Older runs omit it; consumers may derive a conservative
  // path from match_basis + point_name instead.
  evidence_path?: string | null;
  suggested_action?: string | null;
  raw_evidence_uri?: string | null;
  status_detail?: string | null;
  last_seen_at?: string | null;
};

// Per-payload-type expected-vs-observed view emitted into a UDMI validation
// run's result_summary.payload_views (mq9m4bnv). Payload content is the real
// pasted/captured JSON; expected is the sliced expected-schedule facet. A type
// with observed_present=false has an expected facet but no observed payload.
export type UdmiPayloadType = {
  payload_type: "state" | "metadata" | "pointset";
  expected: unknown;
  observed: unknown;
  observed_present: boolean;
};

export type UdmiAssetPayloadView = {
  asset_id: string;
  // Register column B. New validation runs always stamp this; older runs omit
  // it, so consumers must fall back to "Unspecified" rather than guessing from
  // the asset id.
  system?: string | null;
  payload_types: UdmiPayloadType[];
};

export type UdmiAssetMetrics = {
  expected: number;
  observed: number;
  not_observed: number;
  with_issues: number;
  successfully_validated: number;
  // Added to the 1.0 snapshot contract after initial release. Optional keeps
  // stored pre-upgrade runs readable; the UI normalises an absent value to 0.
  unexpected?: number;
  // Registered assets observed on a topic root that differs from the register.
  // Optional keeps summaries stored before wrong-topic classification readable.
  wrong_topic?: number;
};

export type UdmiUnexpectedDevice = {
  id: string;
  topic_root: string;
  topics: string[];
  last_seen: string | null;
};

export type UdmiWrongTopicPayload = {
  payload_type: "state" | "metadata" | "pointset";
  expected_topic: string;
  actual_topic: string;
};

export type UdmiWrongTopicAsset = {
  asset_id: string;
  system: string;
  expected_topic_root: string;
  actual_topic_root: string;
  payloads: UdmiWrongTopicPayload[];
  last_seen: string | null;
};

// Optional, opt-in ledger for diagnosing where a registered asset id appeared
// within an approved MQTT topic scope. The ledger intentionally contains topic
// metadata only: payload bodies are never part of this diagnostic projection.
export type UdmiAssetTopicDiscoveryStatus =
  | "expected_topic_observed"
  | "alternate_topic_observed"
  | "no_matching_asset_id_topic_observed"
  | "capture_incomplete"
  | "ambiguous_asset_id"
  | "missing_asset_id"
  | "scope_unavailable"
  | "scope_configuration_error";

export type UdmiAssetTopicObservation = {
  topic: string;
  message_count: number;
  last_seen: string;
};

export type UdmiAssetTopicDiscoveryAssetResult = {
  asset_id: string;
  system: string;
  expected_topic_root: string;
  expected_topics: string[];
  observed_expected_topics: UdmiAssetTopicObservation[];
  observed_alternate_topics: UdmiAssetTopicObservation[];
  matched_message_count: number;
  topic_limit_reached: boolean;
  status: UdmiAssetTopicDiscoveryStatus;
};

export type UdmiAssetTopicDiscovery = {
  enabled: true;
  scope: string | null;
  scope_source: "register_common_ancestor" | "all" | "invalid" | "unavailable" | "disabled";
  scope_error: string | null;
  topic_limit_per_asset: number;
  capture_complete: boolean;
  capture_status: string;
  asset_results: UdmiAssetTopicDiscoveryAssetResult[];
  status_counts: Partial<Record<UdmiAssetTopicDiscoveryStatus, number>>;
};

export type UdmiPayloadMetrics = {
  expected: number;
  received: number;
  not_received?: number;
  with_issues: number;
  successfully_validated: number;
};

export type UdmiFaultMetrics = {
  payload_formatting_issues: number;
  missing_points: number;
  point_naming_issues: number;
  additional_points: number;
  stale_or_cadence: number;
  other_issues: number;
};

export type UdmiIssueMetrics = {
  blocking: number;
  warning: number;
};

export type UdmiSystemMetrics = {
  system: string;
  asset_metrics: UdmiAssetMetrics;
  payload_metrics: UdmiPayloadMetrics;
  fault_metrics: UdmiFaultMetrics;
  issue_metrics: UdmiIssueMetrics;
};

export type UdmiAssetPayloadResult = {
  payload_type: string;
  expected: boolean;
  received: boolean;
  has_issues: boolean;
  blocking_issue_count: number;
  successfully_validated: boolean;
  topic: string | null;
  received_at: string | null;
};

export type UdmiAssetResult = {
  asset_id: string;
  system: string;
  observed: boolean;
  expected_payloads: number;
  received_payloads: number;
  all_expected_payloads_received: boolean;
  all_received_payloads_successfully_validated: boolean;
  successfully_validated: boolean;
  issue_count: number;
  blocking_issue_count: number;
  last_observed_at: string | null;
  payload_results: UdmiAssetPayloadResult[];
};

export type UdmiFaultRow = {
  issue_id: string;
  asset_id: string | null;
  system: string;
  payload_type: string | null;
  category: string;
  severity: string;
  description: string;
  point_name: string | null;
  expected_value: string | null;
  observed_value: string | null;
  suggested_action: string | null;
  raw_evidence_uri: string | null;
};

// Versioned metric contract shared by the results UI and report exporters.
// It is nested under RunRecord.result_summary.validation_summary_v1. Keeping
// the outer result_summary open preserves old run snapshots and other job types.
export type UdmiValidationSummaryV1 = {
  schema_version: "1.0" | "1.1";
  asset_metrics: UdmiAssetMetrics;
  payload_metrics: UdmiPayloadMetrics;
  fault_metrics: UdmiFaultMetrics;
  issue_metrics: UdmiIssueMetrics;
  system_metrics: UdmiSystemMetrics[];
  asset_results: UdmiAssetResult[];
  fault_rows: UdmiFaultRow[];
  // Older snapshots pre-date unexpected-device measurement.
  unexpected_devices?: UdmiUnexpectedDevice[];
  unexpected_devices_measured?: boolean;
  unexpected_devices_measurement_scope?: string | null;
  // Registered assets found outside their expected topic roots remain expected
  // assets. They are separate from truly unexpected publishers.
  wrong_topic_assets?: UdmiWrongTopicAsset[];
};

export type UdmiReportScopeV1 = {
  schema_version: "1.0";
  selected_payloads: Array<{
    source_run_id: string;
    asset_id: string;
    payload_type: "state" | "metadata" | "pointset";
  }>;
  unexpected_device_ids: string[];
  filters: {
    text: string;
    verdict: "all" | "pass" | "pass-notes" | "fail" | "offline" | "none";
    topic_contains: string;
    system: string;
    observation: "all" | "observed" | "not-observed";
    category: "all" | "validation" | "unexpected-devices";
  };
};

export type ValidationIssuesResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  issues: ValidationIssueRecord[];
};

// The "*_sidecar" kinds run the vendored scanner sidecars through the plain
// discovery path. They are deliberately NOT in NetworkDiscoveryRunKind below, so
// they never reach the sealed-preview / scan-authorization flow the built-in
// "ip" / "bacnet" engines use.
export type DiscoveryRunKind =
  | "ip"
  | "ip_sidecar"
  | "bacnet_sidecar"
  | "mqtt_sidecar"
  | "bacnet"
  | "mqtt";
export type NmapProfileName =
  | "tcp_connect_inventory"
  | "host_discovery"
  | "tcp_syn_inventory"
  | "selected_udp"
  | "service_version_inventory"
  | "os_inventory"
  | "traceroute_inventory"
  | "reviewed_script_inventory";

export type NmapCapabilityResponse = {
  schema_version: "1.0";
  provider: "nmap";
  state: "disabled" | "xml_import_only" | "confirmation_required" | "unavailable" | "available";
  reason: string;
  provider_mode: "disabled" | "operator_xml_import" | "internal_operator_managed";
  policy_id: string | null;
  policy_revision: number | null;
  publisher: string | null;
  version: string | null;
  fingerprint_sha256: string | null;
  npcap_version: string | null;
  npcap_state: "not_checked" | "missing" | "admin_only" | "connect_only" | "raw_capable";
  raw_capable: boolean;
  process_selection_allowed: boolean;
  xml_import_allowed: boolean;
  permitted_profiles: NmapProfileName[];
};

export type NmapDeploymentLane = "internal_same_organization" | "external_customer";
export type NmapProviderMode = "disabled" | "operator_xml_import" | "internal_operator_managed";
export type NmapNpcapState =
  | "not_checked"
  | "missing"
  | "admin_only"
  | "connect_only"
  | "raw_capable";

export type NmapProjectSiteScope = {
  project_id: string;
  site_id: string;
};

export type NmapReviewedScript = {
  schema_version: "1.0";
  name: string;
  sha256: string;
};

export type NmapDeploymentPolicyCreateRequest = {
  deployment_lane: NmapDeploymentLane;
  provider_mode: NmapProviderMode;
  deployment_owner: string;
  operator_install_responsibility: string;
  permitted_project_sites: readonly NmapProjectSiteScope[];
  update_owner: string;
  reviewed_version_policy: string;
  permitted_publishers: readonly string[];
  permitted_versions: readonly string[];
  permitted_signer_sha256: readonly string[];
  permitted_executable_sha256: readonly string[];
  permitted_data_manifest_sha256: readonly string[];
  permitted_licence_sha256: readonly string[];
  permitted_npsl_versions: readonly string[];
  reviewed_scripts: readonly NmapReviewedScript[];
  max_data_files: number;
  max_file_bytes: number;
  max_manifest_bytes: number;
  profile_policy: {
    schema_version: "1.0";
    permitted_profiles: readonly NmapProfileName[];
  };
  acknowledged_no_redistribution: boolean;
  reason: string;
};

export type NmapDeploymentPolicyResponse = {
  schema_version: "1.0";
  policy_id: string;
  deployment_id: string;
  network_executor_id: string;
  revision: number;
  deployment_lane: NmapDeploymentLane;
  provider_mode: NmapProviderMode;
  organization_internal: boolean;
  deployment_owner: string;
  operator_install_responsibility: string;
  permitted_project_sites: NmapProjectSiteScope[];
  update_owner: string;
  reviewed_version_policy: string;
  permitted_publishers: string[];
  permitted_versions: string[];
  permitted_signer_sha256: string[];
  permitted_executable_sha256: string[];
  permitted_data_manifest_sha256: string[];
  permitted_licence_sha256: string[];
  permitted_npsl_versions: string[];
  reviewed_scripts: NmapReviewedScript[];
  max_data_files: number;
  max_file_bytes: number;
  max_manifest_bytes: number;
  profile_policy: {
    schema_version: "1.0";
    permitted_profiles: NmapProfileName[];
  };
  reviewed_at: string;
  acknowledged_no_redistribution: boolean;
  created_by: string;
  reason: string;
  created_at: string;
  supersedes_policy_id: string | null;
};

export type NmapDetectedInstallationResponse = {
  schema_version: "1.0";
  provider: "nmap";
  state: "disabled" | "xml_import_only" | "confirmation_required" | "unavailable" | "available";
  reason: string;
  publisher: string | null;
  version: string | null;
  registry_view: "32" | "64" | null;
  display_name: string | null;
  fingerprint_sha256: string | null;
  signer_sha256: string | null;
  executable_sha256: string | null;
  data_manifest_sha256: string | null;
  data_file_count: number | null;
  data_total_bytes: number | null;
  licence_sha256: string | null;
  npsl_version: string | null;
  reviewed_scripts: NmapReviewedScript[];
  npcap_version: string | null;
  npcap_state: NmapNpcapState;
  raw_capable: boolean;
};

export type NmapInstallationConfirmationResponse = {
  schema_version: "1.0";
  confirmation_id: string;
  policy_id: string;
  deployment_id: string;
  network_executor_id: string;
  policy_revision: number;
  provider: "nmap";
  state: "disabled" | "xml_import_only" | "confirmation_required" | "unavailable" | "available";
  reason: string;
  publisher: string;
  version: string;
  fingerprint_sha256: string;
  signer_sha256: string;
  executable_sha256: string;
  data_manifest_sha256: string;
  data_file_count: number;
  data_total_bytes: number;
  licence_sha256: string;
  npsl_version: string;
  reviewed_scripts: NmapReviewedScript[];
  npcap_version: string | null;
  npcap_state: NmapNpcapState;
  raw_capable: boolean;
  confirmed_by: string;
  confirmed_at: string;
};
export type ValidationRunKind = "udmi" | "bacnet" | "mapping";
export type ImportTemplateFormat = "csv" | "xlsx";
export type ReportFormat = "zip" | "xlsx" | "docx" | "pdf";
export type UdmiReportVariant = "client" | "technical";

export type ReportType =
  | "ip_discovery"
  | "bacnet_discovery"
  | "mqtt_discovery"
  | "udmi_validation"
  | "data_validation"
  | "issue_report"
  | "evidence_pack";

export type ReportSummary = {
  report_id: string;
  report_type: string;
  output_format: ReportFormat;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  file_name: string;
  // Mirrors the backend projection: created_at is the report run's stored
  // creation instant (FastAPI serializes datetime as ISO 8601), source_run_ids
  // the runs the report was scoped to. Both are required server-side, but
  // render them defensively — a response from an older backend, or a cached
  // query payload, carries neither.
  created_at: string;
  source_run_ids: string[];
  evidence_set_id?: string | null;
  report_title?: string | null;
  udmi_report_variant?: UdmiReportVariant;
};

export type ReportListResponse = {
  reports: ReportSummary[];
  total?: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
};

export type DeleteReportsResponse = {
  deleted_report_ids: string[];
  deleted_count: number;
  artifact_cleanup_warnings: string[];
};

// Mirrors backend app.schemas.jobs.JobSummary. Run lists return summaries only;
// the full RunRecord (parameters/result_summary/issues) comes from a per-run GET.
export type JobSummary = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  stage: string;
  progress_percent: number;
  created_at: string;
  updated_at: string;
  // Originating edge id; null for a run created on the local edge, populated for
  // runs ingested from another edge. Additive field — see RunRecord.
  edge_id: string | null;
  validation_incomplete?: boolean | null;
  capture_mode?: string | null;
  capture_window_seconds?: number | null;
  capture_started_at?: string | null;
  capture_ended_at?: string | null;
  capture_duration_seconds?: number | null;
  window_completed?: boolean | null;
  termination_reason?: string | null;
  acceptance_eligible?: boolean | null;
};

export type RunListResponse = {
  runs: JobSummary[];
};

export type ObservedPort = {
  port: number;
  protocol: "tcp" | "udp";
  service?: string | null;
};

// Mirrors backend DiscoveryAssetObservation (extra="allow"): engines attach
// per-protocol fields (device_instance, vendor, point_count, ...) beyond the
// modelled keys, so the index signature keeps those reachable.
export type DiscoveryAssetObservation = {
  asset_id?: string | null;
  ip_address?: string | null;
  mac_address?: string | null;
  hostname?: string | null;
  observed_ports?: ObservedPort[];
  match_basis?: string;
  last_seen_at?: string | null;
  status_detail?: string | null;
  [key: string]: unknown;
};

// Devices/points/topics come back as plain dicts so per-engine attributes
// survive without a rigid model; consumers read known keys defensively.
export type DiscoveryRowRecord = Record<string, unknown>;

// MQTT-only: the whole-broker scan compared against the uploaded register.
// register_available false means no register was imported (the banner prompts an
// upload); the counts describe how many observed topics matched, and
// unobserved_filters lists register topics that no observed topic matched.
export type RegisterComparison = {
  register_available: boolean;
  import_filename?: string | null;
  matched_count?: number;
  unmatched_count?: number;
  expected_filter_count?: number;
  unobserved_filters?: { asset_id?: string; filter: string }[];
};

export type DiscoveryResultsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  result_summary: Record<string, unknown>;
  discovered_assets: DiscoveryAssetObservation[];
  devices: DiscoveryRowRecord[];
  points: DiscoveryRowRecord[];
  topics: DiscoveryRowRecord[];
  register_comparison?: RegisterComparison | null;
};

export type DiscoveryComparisonResponse = {
  baseline_run_id: string;
  candidate_run_id: string;
  job_type: JobType | null;
  compatible: boolean;
  reason: string | null;
  additions: Array<Record<string, unknown>>;
  removals: Array<Record<string, unknown>>;
  changes: Array<Record<string, unknown>>;
};

export type DiscoveryTopicsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  topics: DiscoveryRowRecord[];
  register_comparison?: RegisterComparison | null;
};

export type DiscoveryPointsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  points: DiscoveryRowRecord[];
  total: number;
  next_cursor: string | null;
  has_more: boolean;
};

export type DiscoveryObservationProtocol = "ip" | "bacnet";
export type DiscoveryObservationEntityKind =
  | "lane"
  | "host"
  | "port"
  | "device"
  | "object"
  | "property"
  | "diagnostic";
export type DiscoveryObservationPhase =
  | "planned"
  | "reachability"
  | "enrichment"
  | "comparison"
  | "finalize";

export type DiscoveryObservationRecord = {
  cursor: number;
  run_id: string;
  attempt: number;
  protocol: DiscoveryObservationProtocol;
  entity_kind: DiscoveryObservationEntityKind;
  entity_key: string;
  entity_version: number;
  event_key: string;
  phase: DiscoveryObservationPhase;
  outcome: string;
  payload_schema_version: string;
  payload: Record<string, unknown>;
  payload_sha256: string;
  observed_at: string | null;
  created_at: string;
};

export type DiscoveryObservationTerminal = {
  status: Extract<JobStatus, "succeeded" | "failed" | "cancelled">;
  terminal_cursor: number;
};

export type DiscoveryObservationPage = {
  run_id: string;
  attempt: number;
  observations: DiscoveryObservationRecord[];
  next_cursor: number;
  // Informational high-water mark. A reducer must acknowledge next_cursor only
  // after the complete page has folded successfully.
  latest_cursor: number;
  has_more: boolean;
  terminal: DiscoveryObservationTerminal | null;
  // A sealed run can outlive the 30-day provisional-event retention window.
  // When true, the terminal result is authoritative and the missing cursor
  // prefix must not be reconstructed or represented as observed in-browser.
  observations_pruned?: boolean;
  // Integrity quarantine is distinct from ordinary retention expiry. The
  // provisional stream must be discarded, while sealed evidence still crosses
  // the same terminal synchronization barrier before it is rendered.
  observations_quarantined?: boolean;
};

const rawApiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";
const apiBaseUrl = rawApiBaseUrl.replace(/\/$/, "");

const API_KEY_STORAGE_KEY = "sc.apiKey";

export const AUTH_REQUIRED_MESSAGE = "Authentication required — set an API key";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// True when the server itself REJECTED the credentials (401/403): the presented
// key is bad, inactive, or under-privileged. Network failures, timeouts, and
// 5xx are NOT auth rejections — the key may be perfectly valid while the server
// is unreachable or restarting, so callers must never treat those as a bad key
// (e.g. by prompting the operator to clear a key that is shown only once).
export function isAuthRejection(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

// Scoped run endpoints deliberately conceal revocation and cross-scope access
// behind the same 404 used for a missing run. Consumers of those endpoints
// must close the evidence surface for all three statuses instead of retrying.
export function isRunAccessClosedError(error: unknown): error is ApiError {
  return (
    error instanceof ApiError &&
    (error.status === 401 || error.status === 403 || error.status === 404)
  );
}

function readStoredApiKey(): string | null {
  try {
    return window.localStorage.getItem(API_KEY_STORAGE_KEY);
  } catch {
    // localStorage can be unavailable (e.g. restrictive embedded contexts).
    return null;
  }
}

export function getApiKey(): string | null {
  const stored = readStoredApiKey();
  if (stored) {
    return stored;
  }
  const envKey: unknown = import.meta.env.VITE_API_KEY;
  return typeof envKey === "string" && envKey.length > 0 ? envKey : null;
}

export function setApiKey(key: string): void {
  window.localStorage.setItem(API_KEY_STORAGE_KEY, key);
}

export function clearApiKey(): void {
  window.localStorage.removeItem(API_KEY_STORAGE_KEY);
}

function withApiKey(init?: RequestInit, apiKeyOverride?: string | null): RequestInit | undefined {
  const apiKey = apiKeyOverride === undefined ? getApiKey() : apiKeyOverride;
  if (!apiKey) {
    return init;
  }
  const headers = new Headers(init?.headers);
  headers.set("X-API-Key", apiKey);
  return { ...init, headers };
}

export type SessionBoundApiClient = Readonly<{
  sessionScopeId: SessionScopeId;
  workspace: WorkspaceRef;
  signal: AbortSignal;
  abort: () => void;
  fetchRaw: (path: string, init?: RequestInit) => Promise<Response>;
  request: <T>(path: string, init?: RequestInit) => Promise<T>;
  downloadFile: (path: string, init?: RequestInit) => Promise<DownloadedFile>;
}>;

export type ApiRequestContext = Readonly<{
  client?: SessionBoundApiClient;
  signal?: AbortSignal;
}>;

function combineSignals(
  ...signals: Array<AbortSignal | null | undefined>
): AbortSignal | undefined {
  const present = signals.filter((signal): signal is AbortSignal => Boolean(signal));
  if (present.length === 0) {
    return undefined;
  }
  if (present.length === 1) {
    return present[0];
  }
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any(present);
  }
  const controller = new AbortController();
  for (const signal of present) {
    if (signal.aborted) {
      controller.abort(signal.reason);
      break;
    }
    signal.addEventListener("abort", () => controller.abort(signal.reason), { once: true });
  }
  return controller.signal;
}

function withSignal(
  init: RequestInit | undefined,
  signal: AbortSignal | undefined,
): RequestInit | undefined {
  return signal ? { ...init, signal } : init;
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (response.status === 401) {
    throw new ApiError(AUTH_REQUIRED_MESSAGE, response.status);
  }

  if (!response.ok) {
    throw new ApiError(await parseApiError(response), response.status);
  }

  // 204 No Content (e.g. DELETE /udmi/schemas/{label}) carries no body to parse.
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export function createSessionBoundApiClient(
  sessionScopeId: SessionScopeId,
  workspace: WorkspaceRef,
  apiKey: string | null = getApiKey(),
): SessionBoundApiClient {
  const controller = new AbortController();
  const fetchRaw = (path: string, init?: RequestInit) => {
    const signal = combineSignals(controller.signal, init?.signal);
    return fetch(`${apiBaseUrl}${path}`, withApiKey({ ...init, signal }, apiKey));
  };
  const client: SessionBoundApiClient = {
    sessionScopeId,
    workspace,
    signal: controller.signal,
    abort: () => controller.abort(),
    fetchRaw,
    request: async <T>(path: string, init?: RequestInit) =>
      parseJsonResponse<T>(await fetchRaw(path, init)),
    downloadFile: async (path: string, init?: RequestInit) => {
      const response = await fetchRaw(path, init);
      return parseDownloadResponse(response);
    },
  };
  return Object.freeze(client);
}

async function request<T>(
  path: string,
  init?: RequestInit,
  context?: ApiRequestContext,
): Promise<T> {
  if (context?.client) {
    return context.client.request<T>(path, {
      ...init,
      signal: combineSignals(init?.signal, context.signal),
    });
  }
  const response = await fetch(
    `${apiBaseUrl}${path}`,
    withApiKey(withSignal(init, combineSignals(init?.signal, context?.signal))),
  );
  return parseJsonResponse<T>(response);
}

export type DownloadedFile = {
  blob: Blob;
  filename: string | null;
};

/**
 * Fetches a binary endpoint with the same auth handling as request().
 * Direct-navigation anchors cannot attach the X-API-Key header, so all
 * file downloads must go through this helper in hosted deployments. `init`
 * lets a caller POST a JSON body (e.g. the multi-report export) instead of a
 * bare GET; withApiKey merges the X-API-Key header in either way.
 */
export async function downloadFile(
  path: string,
  init?: RequestInit,
  context?: ApiRequestContext,
): Promise<DownloadedFile> {
  if (context?.client) {
    return context.client.downloadFile(path, {
      ...init,
      signal: combineSignals(init?.signal, context.signal),
    });
  }
  const response = await fetch(
    `${apiBaseUrl}${path}`,
    withApiKey(withSignal(init, combineSignals(init?.signal, context?.signal))),
  );
  return parseDownloadResponse(response);
}

async function parseDownloadResponse(response: Response): Promise<DownloadedFile> {
  if (response.status === 401) {
    throw new ApiError(AUTH_REQUIRED_MESSAGE, response.status);
  }

  if (!response.ok) {
    throw new ApiError(await parseApiError(response), response.status);
  }

  return {
    blob: await response.blob(),
    filename: parseContentDispositionFilename(response.headers.get("Content-Disposition")),
  };
}

function parseContentDispositionFilename(header: string | null): string | null {
  if (!header) {
    return null;
  }
  // RFC 5987 extended parameter (filename*=UTF-8''...) takes priority.
  const encodedMatch = /filename\*\s*=\s*utf-8''([^;]+)/i.exec(header);
  if (encodedMatch) {
    try {
      const decoded = decodeURIComponent(encodedMatch[1].trim());
      if (decoded) {
        return decoded;
      }
    } catch {
      // Malformed percent-encoding: fall back to the plain filename parameter.
    }
  }
  const quotedMatch = /filename\s*=\s*"([^"]*)"/i.exec(header);
  if (quotedMatch) {
    return quotedMatch[1] || null;
  }
  const bareMatch = /filename\s*=\s*([^;]+)/i.exec(header);
  const bareFilename = bareMatch?.[1].trim();
  return bareFilename ? bareFilename : null;
}

async function parseApiError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (Array.isArray(payload.detail)) {
      return payload.detail.map(formatApiDetail).join(" ");
    }
    if (payload.detail) {
      return formatApiDetail(payload.detail);
    }
  } catch {
    return `${response.status} ${response.statusText}`;
  }

  return `${response.status} ${response.statusText}`;
}

export function formatApiDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (typeof detail === "number" || typeof detail === "boolean") {
    return String(detail);
  }
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    const location = Array.isArray(record.loc)
      ? record.loc.filter((item) => item !== "body").join(".")
      : "";
    const message = typeof record.msg === "string" ? record.msg : JSON.stringify(record);
    return location ? `${location}: ${message}` : message;
  }
  return "Unknown API error.";
}

export function getHealth(context?: ApiRequestContext): Promise<HealthStatus> {
  return request<HealthStatus>("/health", undefined, context);
}

// Best-effort adapter classification from the backend. The server DOES return
// "virtual" adapters (ranked last, since 2026-07-14): on Hyper-V vSwitch /
// NIC-team hosts they can carry the machine's only routable IPv4, so the
// frontend labels them pick-with-care instead of filtering them out — and
// never auto-selects them.
export type AdapterType = "ethernet" | "wifi" | "usb_ethernet" | "virtual" | "unknown";

// A usable local network interface as enumerated by GET /system/interfaces.
// `cidr` ("192.168.1.10/24") is what the Source Interface selector stores; the
// bare `ipv4` and `prefix_length` are carried so the backend can bind sockets
// (IP/MQTT want the bare IP; BACnet wants ip/prefix). Gateway and DNS are shown
// read-only by product decision so engineers can confirm the tool reads the
// NIC correctly; MAC/driver strings remain deliberately omitted.
export interface SystemInterface {
  name: string;
  ipv4: string;
  prefix_length: number;
  cidr: string;
  is_up: boolean;
  adapter_type: AdapterType;
  subnet_mask: string;
  gateway: string | null;
  dns_servers: string[];
}

// GET /api/v1/system/interfaces — enumerates the host's usable NICs so the
// Source Interface selector can offer them. Viewer-gated and read-only.
export function getSystemInterfaces(context?: ApiRequestContext): Promise<SystemInterface[]> {
  return request<SystemInterface[]>("/system/interfaces", undefined, context);
}

export function getNmapCapability(input: {
  projectId: string;
  siteId: string;
  context?: ApiRequestContext;
}): Promise<NmapCapabilityResponse> {
  const query = new URLSearchParams({
    project_id: input.projectId,
    site_id: input.siteId,
  });
  return request<NmapCapabilityResponse>(`/nmap/capability?${query}`, undefined, input.context);
}

export function approveDetectedNmap(input: {
  projectId: string;
  siteId: string;
  context?: ApiRequestContext;
}): Promise<NmapCapabilityResponse> {
  return request<NmapCapabilityResponse>(
    "/nmap/approve-detected",
    {
      body: JSON.stringify({ project_id: input.projectId, site_id: input.siteId }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function listNmapDeploymentPolicies(
  context?: ApiRequestContext,
): Promise<NmapDeploymentPolicyResponse[]> {
  return request<NmapDeploymentPolicyResponse[]>("/nmap/policies", undefined, context);
}

export function createNmapDeploymentPolicy(
  policy: NmapDeploymentPolicyCreateRequest,
  context?: ApiRequestContext,
): Promise<NmapDeploymentPolicyResponse> {
  return request<NmapDeploymentPolicyResponse>(
    "/nmap/policies",
    {
      body: JSON.stringify(policy),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    context,
  );
}

export function detectNmapInstallations(
  context?: ApiRequestContext,
): Promise<NmapDetectedInstallationResponse[]> {
  return request<NmapDetectedInstallationResponse[]>(
    "/nmap/installations/detected",
    undefined,
    context,
  );
}

export function confirmNmapInstallation(input: {
  fingerprintSha256: string;
  reason: string;
  context?: ApiRequestContext;
}): Promise<NmapInstallationConfirmationResponse> {
  return request<NmapInstallationConfirmationResponse>(
    "/nmap/installations/confirm",
    {
      body: JSON.stringify({
        fingerprint_sha256: input.fingerprintSha256,
        reason: input.reason,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

// ---------------------------------------------------------------------------
// Identity + RBAC (Phase 4b).
//
// getMe resolves the current principal so the UI can gate engineer/admin
// actions. The user-admin calls are admin-only and surface the optional
// user-management view; non-admins never reach them (the entry is hidden).
// ---------------------------------------------------------------------------

export function getMe(context?: ApiRequestContext): Promise<MeResponse> {
  return request<MeResponse>("/me", undefined, context);
}

export function listUsers(context?: ApiRequestContext): Promise<UserRecord[]> {
  return request<UserRecord[]>("/users", undefined, context);
}

export function createUser(input: {
  username: string;
  role: Role;
  context?: ApiRequestContext;
}): Promise<CreateUserResponse> {
  return request<CreateUserResponse>(
    "/users",
    {
      body: JSON.stringify({ role: input.role, username: input.username }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function deactivateUser(userId: string, context?: ApiRequestContext): Promise<UserRecord> {
  return request<UserRecord>(
    `/users/${encodeURIComponent(userId)}/deactivate`,
    {
      method: "POST",
    },
    context,
  );
}

// Admin-only lost-key recovery: invalidates the user's current key immediately
// and returns a fresh plaintext key, displayed exactly once (same shape as
// createUser). The backend refuses (409) for deactivated users.
export function reissueUserKey(
  userId: string,
  context?: ApiRequestContext,
): Promise<CreateUserResponse> {
  return request<CreateUserResponse>(
    `/users/${encodeURIComponent(userId)}/key`,
    {
      method: "POST",
    },
    context,
  );
}

export function updateUserRole(
  userId: string,
  role: Role,
  context?: ApiRequestContext,
): Promise<UserRecord> {
  return request<UserRecord>(
    `/users/${encodeURIComponent(userId)}/role`,
    {
      body: JSON.stringify({ role }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    context,
  );
}

export function getScopeActivationPreflight(
  context?: ApiRequestContext,
): Promise<ScopeActivationPreflight> {
  return request<ScopeActivationPreflight>("/users/scope-activation-preflight", undefined, context);
}

export function listUserScopeGrants(
  userId: string,
  input: { includeRevoked?: boolean; context?: ApiRequestContext } = {},
): Promise<ScopeGrantRecord[]> {
  const query = input.includeRevoked ? "?include_revoked=true" : "";
  return request<ScopeGrantRecord[]>(
    `/users/${encodeURIComponent(userId)}/scope-grants${query}`,
    undefined,
    input.context,
  );
}

export function createUserScopeGrant(input: {
  userId: string;
  projectId: string;
  siteId: string;
  reason: string;
  context?: ApiRequestContext;
}): Promise<ScopeGrantRecord> {
  return request<ScopeGrantRecord>(
    `/users/${encodeURIComponent(input.userId)}/scope-grants`,
    {
      body: JSON.stringify({
        project_id: input.projectId,
        reason: input.reason,
        site_id: input.siteId,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function revokeUserScopeGrant(input: {
  userId: string;
  grantId: string;
  reason: string;
  context?: ApiRequestContext;
}): Promise<ScopeGrantRecord> {
  return request<ScopeGrantRecord>(
    `/users/${encodeURIComponent(input.userId)}/scope-grants/${encodeURIComponent(input.grantId)}/revoke`,
    {
      body: JSON.stringify({ reason: input.reason }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function getConfiguration(context?: ApiRequestContext): Promise<ConfigurationSnapshot> {
  return request<ConfigurationSnapshot>("/configuration", undefined, context);
}

export function validateConfiguration(
  configuration: ConfigurationSnapshot,
  context?: ApiRequestContext,
): Promise<ConfigurationValidationResult> {
  return request<ConfigurationValidationResult>(
    "/configuration/validate",
    {
      body: JSON.stringify(configuration),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    context,
  );
}

export function updateConfiguration(
  configuration: ConfigurationSnapshot,
  context?: ApiRequestContext,
): Promise<ConfigurationSnapshot> {
  return request<ConfigurationSnapshot>(
    "/configuration",
    {
      body: JSON.stringify(configuration),
      headers: { "Content-Type": "application/json" },
      method: "PUT",
    },
    context,
  );
}

// Query string for the optional project/site scoping the configuration
// endpoints accept (GET/PUT /configuration take project_id + site_id). Kept
// internal so export/import can target a specific project's snapshot without
// changing the default getConfiguration/updateConfiguration behaviour.
function buildConfigurationQuery(projectId?: string, siteId?: string): string {
  const search = new URLSearchParams();
  if (projectId) {
    search.set("project_id", projectId);
  }
  if (siteId) {
    search.set("site_id", siteId);
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

// The exportable configuration envelope written to / read from a JSON file. The
// snapshot is wrapped with provenance so an import can sanity-check what it is
// loading; only `configuration` is sent back to the API on import.
export type ConfigurationExport = {
  kind: "smart-commissioning-configuration";
  version: 1;
  exported_at: string;
  project_id: string | null;
  site_id: string | null;
  configuration: ConfigurationSnapshot;
};

// Reads the current configuration snapshot for a JSON file download. Optionally
// scoped to a specific project/site so a project-specific config can be reused
// across systems. Additive: reuses GET /configuration, does not alter
// getConfiguration. Returns the wrapped envelope the UI serialises to a file.
export async function exportConfiguration(
  projectId?: string,
  siteId?: string,
  context?: ApiRequestContext,
): Promise<ConfigurationExport> {
  const configuration = await request<ConfigurationSnapshot>(
    `/configuration${buildConfigurationQuery(projectId, siteId)}`,
    undefined,
    context,
  );
  return {
    configuration,
    exported_at: new Date().toISOString(),
    kind: "smart-commissioning-configuration",
    project_id: projectId ?? null,
    site_id: siteId ?? null,
    version: 1,
  };
}

// Accepts a previously exported envelope (or a bare snapshot) and saves it via
// the existing PUT /configuration path, which validates server-side before
// persisting. Optionally targets a specific project/site so a reusable config
// can be applied to another project/system. Throws ApiError on a 400 validation
// rejection, exactly like updateConfiguration. Additive: does not change
// updateConfiguration.
export function importConfiguration(
  payload: ConfigurationExport | ConfigurationSnapshot,
  projectId?: string,
  siteId?: string,
  context?: ApiRequestContext,
): Promise<ConfigurationSnapshot> {
  const configuration =
    "configuration" in payload && payload.configuration
      ? payload.configuration
      : (payload as ConfigurationSnapshot);
  return request<ConfigurationSnapshot>(
    `/configuration${buildConfigurationQuery(projectId, siteId)}`,
    {
      body: JSON.stringify(configuration),
      headers: { "Content-Type": "application/json" },
      method: "PUT",
    },
    context,
  );
}

// Legacy certificate material accepted on import for one compatibility release.
// v0.1.26 never returns `content` from an API response.
export type ConfigurationSecretMaterial = {
  secret_ref: string;
  content: string;
  file_name?: string | null;
};

// v2 compatibility envelope. Secret values are excluded from responses.
export type ConfigurationExportEnvelope = {
  kind: "smart-commissioning-configuration";
  version: 2;
  exported_at: string;
  project_id: string | null;
  site_id: string | null;
  secrets_included: boolean;
  configuration: ConfigurationSnapshot;
  secret_material: Record<string, ConfigurationSecretMaterial>;
};

// Compatibility endpoint retained for older clients. Its response is masked.
export function exportConfigurationWithSecrets(
  projectId?: string,
  siteId?: string,
  context?: ApiRequestContext,
): Promise<ConfigurationExportEnvelope> {
  return request<ConfigurationExportEnvelope>(
    `/configuration/export-with-secrets${buildConfigurationQuery(projectId, siteId)}`,
    undefined,
    context,
  );
}

// Imports a v2 envelope (configuration + secret_material) via POST
// /configuration/import, which restores the certificate material into the
// receiving machine's secret store, validates, and saves — returning the MASKED
// snapshot. Throws ApiError on a 400 validation rejection.
export function importConfigurationWithSecrets(
  envelope: ConfigurationExportEnvelope,
  projectId?: string,
  siteId?: string,
  context?: ApiRequestContext,
): Promise<ConfigurationSnapshot> {
  return request<ConfigurationSnapshot>(
    `/configuration/import${buildConfigurationQuery(projectId, siteId)}`,
    {
      body: JSON.stringify({
        configuration: envelope.configuration,
        secret_material: envelope.secret_material,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    context,
  );
}

// Outcome of POST /logs/upload. `outcome` is the honest terminal result: the
// server accepted the bundle ("uploaded"), rejected it ("rejected", with the
// HTTP status), or did not respond at all ("no_response") — never a fabricated
// success. `detail` never contains the upload token.
export type LogUploadResult = {
  outcome: "uploaded" | "rejected" | "no_response";
  status_code: number | null;
  detail: string;
  bundle_bytes: number;
  files: string[];
};

// Uploads the masked local log bundle to the configured Log Upload URL. The
// server reads the URL/token from the stored configuration; nothing is sent in
// the request body here.
export function uploadLogs(context?: ApiRequestContext): Promise<LogUploadResult> {
  return request<LogUploadResult>("/logs/upload", { method: "POST" }, context);
}

export function storeSecretMaterial(input: {
  field: string;
  content: string;
  fileName?: string | null;
  context?: ApiRequestContext;
}): Promise<SecretMaterialResponse> {
  return request<SecretMaterialResponse>(
    "/configuration/secrets",
    {
      body: JSON.stringify({
        content: input.content,
        field: input.field,
        file_name: input.fileName ?? null,
        section: "certificates",
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function listImportProfiles(context?: ApiRequestContext): Promise<ImportProfileSummary[]> {
  return request<ImportProfileSummary[]>("/imports/profiles", undefined, context);
}

export function createImport(input: {
  importType: ImportType;
  file: File;
  projectId?: string;
  siteId?: string;
  context?: ApiRequestContext;
}): Promise<ImportBatchSummary> {
  const body = new FormData();
  body.append("import_type", input.importType);
  body.append("project_id", input.projectId ?? "demo-project");
  body.append("site_id", input.siteId ?? "demo-site");
  body.append("file", input.file);

  return request<ImportBatchSummary>(
    "/imports",
    {
      body,
      method: "POST",
    },
    input.context,
  );
}

// Per-row rejection reasons for one import. The POST above returns only the
// accepted/rejected counts; the reasons are persisted separately and read back
// from here, so an operator can see WHY rows were rejected.
export function getImportErrors(
  importId: string,
  context?: ApiRequestContext,
): Promise<ImportErrorReport> {
  return request<ImportErrorReport>(
    `/imports/${encodeURIComponent(importId)}/errors`,
    undefined,
    context,
  );
}

// Newest usable (non-empty) import of a given type for the current project/site.
// The Setup card reads this so it can tell the operator a register is already
// imported and stored server-side — surviving a restart — instead of the native
// file input always reading "No file chosen" (ISSUE-5). Sends the SAME
// project/site defaults createImport sends, or the lookup would miss the upload.
// Returns null on a 404 (none on file) so the caller renders nothing rather than
// a false "register on file" claim; other errors propagate so the note never
// masks a genuine failure with a false negative.
export function getLatestImport(
  importType: ImportType,
  projectId = "demo-project",
  siteId = "demo-site",
  context?: ApiRequestContext,
): Promise<ImportBatchSummary | null> {
  const query = new URLSearchParams({
    import_type: importType,
    project_id: projectId,
    site_id: siteId,
  });
  return request<ImportBatchSummary>(
    `/imports/latest?${query.toString()}`,
    undefined,
    context,
  ).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  });
}

// One uploaded non-published UDMI schema set: payloads that declare this
// version label (e.g. "nonpub.1") are validated against these schema files
// instead of a published canonical UDMI release.
export type UdmiSchemaSet = {
  version_label: string;
  filenames: string[];
  uploaded_at: string;
};

export function listUdmiSchemaSets(context?: ApiRequestContext): Promise<UdmiSchemaSet[]> {
  return request<UdmiSchemaSet[]>("/udmi/schemas", undefined, context);
}

// Multipart upload mirroring createImport: version_label plus one or more
// .json schema files under the repeated "files" field. The backend 400s with
// an actionable detail on a bad label, missing schema roots, or invalid JSON.
export function uploadUdmiSchemaSet(input: {
  versionLabel: string;
  files: File[];
  context?: ApiRequestContext;
}): Promise<UdmiSchemaSet> {
  const body = new FormData();
  body.append("version_label", input.versionLabel);
  for (const file of input.files) {
    body.append("files", file);
  }
  return request<UdmiSchemaSet>(
    "/udmi/schemas",
    {
      body,
      method: "POST",
    },
    input.context,
  );
}

export function deleteUdmiSchemaSet(
  versionLabel: string,
  context?: ApiRequestContext,
): Promise<void> {
  return request<void>(
    `/udmi/schemas/${encodeURIComponent(versionLabel)}`,
    {
      method: "DELETE",
    },
    context,
  );
}

export function getImportTemplatePath(
  importType: ImportType,
  format: ImportTemplateFormat,
): string {
  return `/imports/templates/${encodeURIComponent(importType)}.${format}`;
}

// Public zip of the vendored UDMI 1.5.2 schema set (roots + full $ref closure +
// README + LICENSE): a starting point an engineer edits and re-uploads under a
// nonpub label. Unauthenticated, no side effects.
export function getUdmiSchemaTemplatePath(): string {
  return "/udmi/schemas/template";
}

export function getReportDownloadPath(reportId: string): string {
  return `/reports/${encodeURIComponent(reportId)}/download`;
}

export function getValidationJsonExportPath(runId: string): string {
  return `/validation/runs/${encodeURIComponent(runId)}/export.json`;
}

// Bundle multiple reports into one zip. One gesture, one fetch, one download —
// so the browser's per-gesture download throttle never drops files (mqatcqb3).
// The ids POST in a JSON body (built at the call site) rather than a query
// string so an unbounded selection never overruns request-line limits.
export const REPORTS_EXPORT_PATH = "/reports/export";

export function startDiscoveryRun(input: {
  runKind: DiscoveryRunKind;
  jobType: JobType;
  parameters?: Record<string, unknown>;
  workspace?: WorkspaceRef;
  context?: ApiRequestContext;
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(
    `/discovery/${input.runKind}/runs`,
    {
      body: JSON.stringify({
        job_type: input.jobType,
        parameters: { requested_from: "frontend-review", ...(input.parameters ?? {}) },
        project_id: input.workspace?.projectId ?? "demo-project",
        site_id: input.workspace?.siteId ?? "demo-site",
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

type NetworkDiscoveryRunKind = Exclude<
  DiscoveryRunKind,
  "mqtt" | "ip_sidecar" | "bacnet_sidecar" | "mqtt_sidecar"
>;
type NetworkDiscoveryJobType = Extract<JobType, "ip_discovery" | "bacnet_discovery">;

export function startDiscoveryPreview(input: {
  runKind: NetworkDiscoveryRunKind;
  jobType: NetworkDiscoveryJobType;
  parameters: Record<string, unknown> & { dry_run: true };
  workspace: WorkspaceRef;
  idempotencyKey?: string;
  context?: ApiRequestContext;
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(
    `/discovery/${input.runKind}/runs`,
    {
      body: JSON.stringify({
        job_type: input.jobType,
        parameters: input.parameters,
        project_id: input.workspace.projectId,
        site_id: input.workspace.siteId,
      }),
      headers: {
        "Content-Type": "application/json",
        ...(input.runKind === "ip" && input.idempotencyKey
          ? { "Idempotency-Key": input.idempotencyKey }
          : {}),
      },
      method: "POST",
    },
    input.context,
  );
}

export function startAuthorizedDiscoveryRun(input: {
  runKind: NetworkDiscoveryRunKind;
  jobType: NetworkDiscoveryJobType;
  previewRunId: string;
  scanAuthorizationId: string;
  workspace: WorkspaceRef;
  idempotencyKey?: string;
  context?: ApiRequestContext;
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(
    `/discovery/${input.runKind}/runs`,
    {
      body: JSON.stringify({
        job_type: input.jobType,
        preview_run_id: input.previewRunId,
        scan_authorization_id: input.scanAuthorizationId,
        parameters: {},
        project_id: input.workspace.projectId,
        site_id: input.workspace.siteId,
      }),
      headers: {
        "Content-Type": "application/json",
        ...(input.runKind === "ip" && input.idempotencyKey
          ? { "Idempotency-Key": input.idempotencyKey }
          : {}),
      },
      method: "POST",
    },
    input.context,
  );
}

export type BacnetPropertyName =
  | "object_name"
  | "present_value"
  | "units"
  | "status_flags"
  | "reliability"
  | "out_of_service"
  | "description";

export function startBacnetPropertyRun(input: {
  parentRunId: string;
  deviceInstance: number;
  destination?: string;
  requestedReadSet: BacnetPropertyName[];
  previewRunId?: string;
  scanAuthorizationId?: string;
  workspace: WorkspaceRef;
  context?: ApiRequestContext;
}): Promise<JobAcceptedResponse> {
  const live = Boolean(input.previewRunId || input.scanAuthorizationId);
  return request<JobAcceptedResponse>(
    "/discovery/bacnet/property-runs",
    {
      body: JSON.stringify({
        project_id: input.workspace.projectId,
        site_id: input.workspace.siteId,
        parent_run_id: input.parentRunId,
        device_instance: input.deviceInstance,
        destination: input.destination,
        requested_read_set: input.requestedReadSet,
        ...(live
          ? {
              preview_run_id: input.previewRunId,
              scan_authorization_id: input.scanAuthorizationId,
              parameters: {},
            }
          : { parameters: { dry_run: true } }),
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export type BacnetBrowsedObject = {
  type: number;
  type_name: string;
  instance: number;
  name: string;
  present_value: string;
  units: string;
};

export type BacnetObjectBrowseResponse = {
  run_id: string;
  device_instance: number;
  address: string;
  objects: BacnetBrowsedObject[];
  count: number;
  truncated: boolean;
  error: string | null;
};

// On-demand live object browse for one device in a succeeded bacnet_scanner run.
// Ephemeral read: the backend drives the sidecar and returns JSON; nothing is
// persisted and the sealed scan results are unchanged.
export function browseBacnetScannerObjects(input: {
  runId: string;
  deviceInstance: number;
  authorized: boolean;
  cap?: number;
  readTimeoutMs?: number;
  context?: ApiRequestContext;
}): Promise<BacnetObjectBrowseResponse> {
  return request<BacnetObjectBrowseResponse>(
    `/discovery/bacnet_sidecar/runs/${encodeURIComponent(input.runId)}/object-browse`,
    {
      body: JSON.stringify({
        device_instance: input.deviceInstance,
        authorized: input.authorized,
        ...(input.cap !== undefined ? { cap: input.cap } : {}),
        ...(input.readTimeoutMs !== undefined ? { read_timeout_ms: input.readTimeoutMs } : {}),
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

// Turn a succeeded IP scanner run's responding devices into an accepted
// ip_scanner_register import (server-side, from the run's own evidence). Returns
// the same ImportBatchSummary an upload returns.
export function saveIpScanRunAsRegister(input: {
  runId: string;
  context?: ApiRequestContext;
}): Promise<ImportBatchSummary> {
  return request<ImportBatchSummary>(
    `/discovery/ip_sidecar/runs/${encodeURIComponent(input.runId)}/save-as-register`,
    { method: "POST" },
    input.context,
  );
}

export function createScanAuthorization(input: {
  previewRunId: string;
  ticket: string;
  purpose: string;
  notBefore: string;
  notAfter: string;
  context?: ApiRequestContext;
}): Promise<ScanAuthorizationV1> {
  return request<ScanAuthorizationV1>(
    "/discovery/scan-authorizations",
    {
      body: JSON.stringify({
        preview_run_id: input.previewRunId,
        ticket: input.ticket,
        purpose: input.purpose,
        not_before: input.notBefore,
        not_after: input.notAfter,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function listScanAuthorizations(input: {
  workspace: WorkspaceRef;
  previewRunId?: string;
  context?: ApiRequestContext;
}): Promise<ScanAuthorizationV1[]> {
  const query = new URLSearchParams({
    project_id: input.workspace.projectId,
    site_id: input.workspace.siteId,
  });
  if (input.previewRunId) {
    query.set("preview_run_id", input.previewRunId);
  }
  return request<ScanAuthorizationV1[]>(
    `/discovery/scan-authorizations?${query.toString()}`,
    undefined,
    input.context,
  );
}

export function getScanAuthorization(
  authorizationId: string,
  context?: ApiRequestContext,
): Promise<ScanAuthorizationV1> {
  return request<ScanAuthorizationV1>(
    `/discovery/scan-authorizations/${encodeURIComponent(authorizationId)}`,
    undefined,
    context,
  );
}

export function revokeScanAuthorization(input: {
  authorizationId: string;
  reason: string;
  context?: ApiRequestContext;
}): Promise<ScanAuthorizationV1> {
  return request<ScanAuthorizationV1>(
    `/discovery/scan-authorizations/${encodeURIComponent(input.authorizationId)}/revoke`,
    {
      body: JSON.stringify({ reason: input.reason }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function startValidationRun(input: {
  runKind: ValidationRunKind;
  jobType: JobType;
  parameters?: Record<string, unknown>;
  workspace?: WorkspaceRef;
  context?: ApiRequestContext;
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(
    `/validation/${input.runKind}/runs`,
    {
      body: JSON.stringify({
        job_type: input.jobType,
        parameters: { requested_from: "frontend-review", ...(input.parameters ?? {}) },
        project_id: input.workspace?.projectId ?? "demo-project",
        site_id: input.workspace?.siteId ?? "demo-site",
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export type ConfigPublishPoint = { point: string; value: string | number | boolean };

export function startMqttConfigPublishRun(input: {
  topic: string;
  payload: string;
  confirmed: boolean;
  expectedPoint?: string;
  expectedValue?: string | number | boolean;
  // Every point/value the publish should confirm back, primary + extras
  // (mq9n11wi). When omitted, falls back to the single primary point.
  expectedPoints?: ConfigPublishPoint[];
  useLiveBroker?: boolean;
  pointsetTopic?: string;
  waitSeconds?: number;
  workspace?: WorkspaceRef;
  context?: ApiRequestContext;
}): Promise<JobAcceptedResponse> {
  // Confirm-back must cover EVERY written point (mq9n11wi). Build the expected
  // list from expectedPoints, falling back to the single primary for
  // back-compat, and give the local-verify next_pointset_payload a present_value
  // for each expected point so the no-broker path can confirm them all (a fixed
  // backend would otherwise report the extras as "missing").
  const expectedPairs = (input.expectedPoints ?? []).filter((pair) => pair.point.trim() !== "");
  const allExpected: ConfigPublishPoint[] =
    expectedPairs.length > 0
      ? expectedPairs
      : input.expectedPoint
        ? [{ point: input.expectedPoint, value: input.expectedValue ?? "" }]
        : [];
  const points: Record<string, { present_value: string | number | boolean }> = {};
  for (const pair of allExpected) {
    points[pair.point.trim()] = { present_value: pair.value };
  }
  return request<JobAcceptedResponse>(
    "/validation/mqtt-config/runs",
    {
      body: JSON.stringify({
        job_type: "mqtt_config_publish",
        parameters: {
          // A live-broker publish is a real network write, so the backend gates it
          // behind the authorization contract (403 without it). The operator's
          // explicit "publish through the broker" choice plus the confirm checkbox
          // IS that authorization; the backend still stamps the real principal.
          // Boolean shorthand only — the frontend never fabricates a
          // scan_authorization block. Validate-only (no broker) needs none.
          authorized: Boolean(input.useLiveBroker),
          confirmed: input.confirmed,
          expected_point: input.expectedPoint ?? allExpected[0]?.point ?? "",
          expected_value: input.expectedValue ?? allExpected[0]?.value ?? "",
          expected_points: allExpected.map((pair) => ({
            point: pair.point.trim(),
            value: pair.value,
          })),
          pointset_topic: input.pointsetTopic ?? "",
          next_pointset_payload: {
            pointset: {
              points,
            },
          },
          payload: input.payload,
          requested_from: "frontend-review",
          topic: input.topic,
          use_live_broker: Boolean(input.useLiveBroker),
          wait_seconds: input.waitSeconds ?? 5,
        },
        project_id: input.workspace?.projectId ?? "demo-project",
        site_id: input.workspace?.siteId ?? "demo-site",
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function getValidationRun(runId: string, context?: ApiRequestContext): Promise<RunRecord> {
  return request<RunRecord>(`/validation/runs/${runId}`, undefined, context);
}

export function getValidationIssues(
  runId: string,
  context?: ApiRequestContext,
): Promise<ValidationIssuesResponse> {
  return request<ValidationIssuesResponse>(`/validation/runs/${runId}/issues`, undefined, context);
}

export function createReport(input: {
  reportType: ReportType;
  format?: ReportFormat;
  sourceRunIds?: string[];
  reportTitle?: string;
  udmiScope?: UdmiReportScopeV1;
  udmiReportVariant?: UdmiReportVariant;
  workspace?: WorkspaceRef;
  context?: ApiRequestContext;
}): Promise<ReportSummary> {
  return request<ReportSummary>(
    "/reports",
    {
      body: JSON.stringify({
        output_format: input.format ?? "zip",
        project_id: input.workspace?.projectId ?? "demo-project",
        report_type: input.reportType,
        site_id: input.workspace?.siteId ?? "demo-site",
        source_run_ids: input.sourceRunIds ?? [],
        ...(input.reportTitle ? { report_title: input.reportTitle } : {}),
        ...(input.udmiScope ? { udmi_scope: input.udmiScope } : {}),
        ...(input.udmiReportVariant ? { udmi_report_variant: input.udmiReportVariant } : {}),
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function listReports(
  params?: { limit?: number; offset?: number },
  context?: ApiRequestContext,
): Promise<ReportListResponse> {
  const search = new URLSearchParams();
  if (typeof params?.limit === "number") {
    search.set("limit", String(params.limit));
  }
  if (typeof params?.offset === "number") {
    search.set("offset", String(params.offset));
  }
  const query = search.toString();
  return request<ReportListResponse>(`/reports${query ? `?${query}` : ""}`, undefined, context);
}

export function deleteReports(input: {
  reportIds: string[];
  context?: ApiRequestContext;
}): Promise<DeleteReportsResponse> {
  return request<DeleteReportsResponse>(
    "/reports/delete",
    {
      body: JSON.stringify({ report_ids: input.reportIds }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export type ListRunsParams = {
  projectId?: string;
  siteId?: string;
  jobType?: JobType;
  // New filters mirroring the backend GET /runs query params. edge_id is an
  // exact match on the originating edge; status filters by JobStatus.
  edgeId?: string;
  status?: JobStatus;
  limit?: number;
  offset?: number;
};

function buildRunsQuery(params?: ListRunsParams): string {
  const search = new URLSearchParams();
  if (params?.projectId) {
    search.set("project_id", params.projectId);
  }
  if (params?.siteId) {
    search.set("site_id", params.siteId);
  }
  if (params?.jobType) {
    search.set("job_type", params.jobType);
  }
  if (params?.edgeId) {
    search.set("edge_id", params.edgeId);
  }
  if (params?.status) {
    search.set("status", params.status);
  }
  if (typeof params?.limit === "number") {
    search.set("limit", String(params.limit));
  }
  if (typeof params?.offset === "number") {
    search.set("offset", String(params.offset));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

export function listRuns(
  params?: ListRunsParams,
  context?: ApiRequestContext,
): Promise<RunListResponse> {
  return request<RunListResponse>(`/runs${buildRunsQuery(params)}`, undefined, context);
}

export function cancelRun(runId: string, context?: ApiRequestContext): Promise<RunRecord> {
  return request<RunRecord>(
    `/runs/${encodeURIComponent(runId)}/cancel`,
    {
      method: "POST",
    },
    context,
  );
}

export function getDiscoveryRun(runId: string, context?: ApiRequestContext): Promise<RunRecord> {
  return request<RunRecord>(`/discovery/runs/${encodeURIComponent(runId)}`, undefined, context);
}

export function getDiscoveryResults(
  runId: string,
  context?: ApiRequestContext,
): Promise<DiscoveryResultsResponse> {
  return request<DiscoveryResultsResponse>(
    `/discovery/runs/${encodeURIComponent(runId)}/results`,
    undefined,
    context,
  );
}

export function getDiscoveryComparison(
  candidateRunId: string,
  baselineRunId: string,
  context?: ApiRequestContext,
): Promise<DiscoveryComparisonResponse> {
  const query = new URLSearchParams({ against: baselineRunId });
  return request<DiscoveryComparisonResponse>(
    `/discovery/runs/${encodeURIComponent(candidateRunId)}/comparison?${query.toString()}`,
    undefined,
    context,
  );
}

export function getDiscoveryObservations(
  runId: string,
  after: number,
  limit = 100,
  context?: ApiRequestContext,
): Promise<DiscoveryObservationPage> {
  const query = new URLSearchParams({
    after: String(after),
    limit: String(limit),
  });
  return request<DiscoveryObservationPage>(
    `/discovery/runs/${encodeURIComponent(runId)}/observations?${query.toString()}`,
    undefined,
    context,
  );
}

export function getDiscoveryTopics(
  runId: string,
  context?: ApiRequestContext,
): Promise<DiscoveryTopicsResponse> {
  return request<DiscoveryTopicsResponse>(
    `/discovery/runs/${encodeURIComponent(runId)}/topics`,
    undefined,
    context,
  );
}

export function getDiscoveryPoints(
  runId: string,
  input: {
    after?: string | null;
    limit?: number;
    search?: string | null;
    context?: ApiRequestContext;
  } = {},
): Promise<DiscoveryPointsResponse> {
  const query = new URLSearchParams();
  if (input.after) query.set("after", input.after);
  if (typeof input.limit === "number") query.set("limit", String(input.limit));
  if (input.search) query.set("search", input.search);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<DiscoveryPointsResponse>(
    `/discovery/runs/${encodeURIComponent(runId)}/points${suffix}`,
    undefined,
    input.context,
  );
}

// Path (display-only; download via downloadFile so the X-API-Key header rides)
// for the server-generated XLSX of captured topics (mq9nhbzu Excel export). An
// optional topic filter applies the same +/# wildcard semantics server-side.
export function getDiscoveryTopicsXlsxPath(runId: string, topicFilter?: string): string {
  const base = `/discovery/runs/${encodeURIComponent(runId)}/topics.xlsx`;
  return topicFilter ? `${base}?topic_filter=${encodeURIComponent(topicFilter)}` : base;
}

export function listValidationRuns(context?: ApiRequestContext): Promise<RunListResponse> {
  return request<RunListResponse>("/validation/runs", undefined, context);
}

export function rollbackMqttConfigPublish(
  runId: string,
  context?: ApiRequestContext,
): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(
    `/validation/mqtt-config/runs/${encodeURIComponent(runId)}/rollback`,
    { method: "POST" },
    context,
  );
}

// ---------------------------------------------------------------------------
// Server-Sent Events (SSE) run-progress streaming.
//
// The backend exposes GET /runs/{run_id}/events as a text/event-stream that
// emits status/stage/progress and closes when the run is terminal. The browser
// EventSource API CANNOT attach custom headers, so it cannot carry X-API-Key in
// api_key mode. We therefore consume the stream with fetch()+ReadableStream
// through the SAME withApiKey() path the rest of the client uses, and parse the
// SSE frames manually. In local/loopback mode no key is needed; this one code
// path covers both modes. On any error/unsupported environment the caller falls
// back to the existing 1.5s polling (see ModulePage / DashboardPage).
// ---------------------------------------------------------------------------

// The status/stage/progress slice emitted per progress frame. Mirrors the
// backend events._progress_payload shape.
export type ProgressiveCounts = Record<string, number>;

export type RunEvent = {
  run_id: string;
  job_type?: JobType;
  status: JobStatus;
  stage?: string;
  progress_percent?: number;
  updated_at?: string | null;
  error_message?: string | null;
  observation_attempt?: number | null;
  latest_observation_cursor?: number | null;
  progressive_counts?: ProgressiveCounts;
};

export type RunControlEvent =
  | { run_id: string; status: "closed" }
  | { run_id: string; status: "unavailable" };

export type RunEventPayload = RunEvent | RunControlEvent;

export type RunEventName = "message" | "terminal" | "timeout" | "closed" | "unavailable";

export type RunEventFrame =
  | { name: "message" | "terminal" | "timeout"; data: RunEvent | null }
  | { name: "closed"; data: Extract<RunControlEvent, { status: "closed" }> | null }
  | { name: "unavailable"; data: Extract<RunControlEvent, { status: "unavailable" }> | null };

export type RunEventCallbacks = {
  // Fired for every progress frame (the default "message" event) and for the
  // explicit "terminal" frame, so consumers always see the final state.
  onEvent: (event: RunEventPayload, name: RunEventName) => void;
  // Fired once when the stream ends (terminal/timeout/closed) or errors. The
  // boolean reports whether the run reached a terminal status over the stream.
  onClose?: (reachedTerminal: boolean) => void;
  onError?: (error: unknown) => void;
};

const TERMINAL_EVENT_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "succeeded",
  "failed",
  "cancelled",
]);

/**
 * Parses accumulated SSE text into complete frames, returning the parsed
 * events and the unconsumed trailing buffer (a partial frame).
 */
export function parseSseBuffer(buffer: string): { events: RunEventFrame[]; rest: string } {
  const events: RunEventFrame[] = [];
  // SSE frames are separated by a blank line. Normalise CRLF first.
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  // The last element is an incomplete frame (no trailing blank line yet).
  const rest = parts.pop() ?? "";
  for (const block of parts) {
    const trimmed = block.trim();
    if (!trimmed) {
      continue;
    }
    let rawName = "message";
    const dataLines: string[] = [];
    for (const line of trimmed.split("\n")) {
      if (line.startsWith("event:")) {
        rawName = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trim());
      }
    }
    const name = parseRunEventName(rawName);
    if (name === null) {
      continue;
    }
    let decoded: unknown = null;
    if (dataLines.length > 0) {
      try {
        decoded = JSON.parse(dataLines.join(""));
      } catch {
        // A malformed data frame is skipped rather than aborting the stream.
        decoded = null;
      }
    }
    if (name === "closed") {
      events.push({ data: parseControlEvent(decoded, "closed"), name });
    } else if (name === "unavailable") {
      events.push({ data: parseControlEvent(decoded, "unavailable"), name });
    } else {
      events.push({ data: parseProgressEvent(decoded), name });
    }
  }
  return { events, rest };
}

function parseRunEventName(value: string): RunEventName | null {
  return value === "message" ||
    value === "terminal" ||
    value === "timeout" ||
    value === "closed" ||
    value === "unavailable"
    ? value
    : null;
}

function isJobStatus(value: unknown): value is JobStatus {
  return (
    value === "queued" ||
    value === "running" ||
    value === "succeeded" ||
    value === "failed" ||
    value === "cancelled"
  );
}

export function isRunProgressEvent(value: RunEventPayload): value is RunEvent {
  return isJobStatus(value.status);
}

function parseProgressEvent(value: unknown): RunEvent | null {
  if (!isPlainObject(value) || typeof value.run_id !== "string" || !isJobStatus(value.status)) {
    return null;
  }
  return value as RunEvent;
}

function parseControlEvent<TStatus extends RunControlEvent["status"]>(
  value: unknown,
  status: TStatus,
): Extract<RunControlEvent, { status: TStatus }> | null {
  if (!isPlainObject(value) || typeof value.run_id !== "string" || value.status !== status) {
    return null;
  }
  return { run_id: value.run_id, status } as Extract<RunControlEvent, { status: TStatus }>;
}

/**
 * Opens the SSE run-progress stream and dispatches parsed events to callbacks.
 * Returns a disposer that aborts the underlying fetch (cancel-safe): calling it
 * stops the stream and is a no-op after the stream has already closed.
 *
 * Auth: routed through withApiKey() so X-API-Key (or the loopback path) applies
 * exactly like every other request. A 401 surfaces via onError as an ApiError,
 * letting the caller fall back to polling.
 */
export function streamRunEvents(
  runId: string,
  callbacks: RunEventCallbacks,
  context?: ApiRequestContext,
): () => void {
  const controller = new AbortController();
  let reachedTerminal = false;
  let closed = false;

  const finish = (error?: unknown) => {
    if (closed) {
      return;
    }
    closed = true;
    if (error !== undefined) {
      callbacks.onError?.(error);
    }
    callbacks.onClose?.(reachedTerminal);
  };

  void (async () => {
    try {
      const init = {
        headers: { Accept: "text/event-stream" },
        signal: combineSignals(controller.signal, context?.signal),
      };
      const path = `/runs/${encodeURIComponent(runId)}/events`;
      const response = context?.client
        ? await context.client.fetchRaw(path, init)
        : await fetch(`${apiBaseUrl}${path}`, withApiKey(init));

      if (response.status === 401) {
        throw new ApiError(AUTH_REQUIRED_MESSAGE, response.status);
      }
      if (!response.ok) {
        throw new ApiError(await parseApiError(response), response.status);
      }
      if (!response.body) {
        // No streaming body (e.g. a non-streaming fetch polyfill): the caller
        // must fall back to polling.
        throw new Error("Streaming response body is not available.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseSseBuffer(buffer);
        buffer = rest;
        for (const { name, data } of events) {
          if (data && data.run_id === runId) {
            if (
              name === "terminal" ||
              (isRunProgressEvent(data) && TERMINAL_EVENT_STATUSES.has(data.status))
            ) {
              reachedTerminal = true;
            }
            callbacks.onEvent(data, name);
          }
        }
      }
      finish();
    } catch (error) {
      // An aborted stream (caller disposed) is a clean close, not an error.
      if (controller.signal.aborted) {
        finish();
        return;
      }
      finish(error);
    }
  })();

  return () => {
    controller.abort();
  };
}

// ---------------------------------------------------------------------------
// MQTT live session (M4a): a held broker connection streamed to the browser.
// The frame types mirror the vendored sidecar's /api/stream shapes verbatim (a
// backend contract test pins them). The browser never sends broker secrets.
// ---------------------------------------------------------------------------

export type MqttLiveConnection = {
  status: "disconnected" | "connecting" | "connected" | "reconnecting" | "error";
  host: string;
  port: number;
  tls: boolean;
  rootFilter: string;
  qos: number;
  error: string;
  since: number;
};

export type MqttLiveStats = {
  expectedAssets: number;
  subscribedAssets: number;
  liveAssets: number;
  topicsDiscovered: number;
  issues: number;
  totalMessages: number;
};

export type MqttLiveTreeNode = {
  n: string;
  p: string;
  t: number;
  m: number;
  r: number;
  mt: 0 | 1;
  dev?: 1;
  a?: string;
  leaf?: 1;
  sc?: string;
  ret?: 1;
  c?: number;
  iss?: number;
  ch?: MqttLiveTreeNode[];
};

export type MqttLiveFocusedPoint = { name: string; value: unknown; unit: string; ts: number };

// The sidecar's buildFocused() object. Only the fields the live focus panel
// renders are typed; the sidecar sends more (per-topic history, comparison,
// register meta) which pass through untyped.
export type MqttLiveFocused = {
  asset: string;
  livePoints: MqttLiveFocusedPoint[];
  lastPayload: string;
  configTopic: string;
  configPayload: string;
};

export type MqttLiveSnapshot = {
  type: "snapshot";
  status: MqttLiveConnection;
  stats: MqttLiveStats;
  tree: MqttLiveTreeNode[];
  treeShown: number;
  totalTopics: number;
  filtered: boolean;
  focused: MqttLiveFocused | null;
};

export type MqttLiveActivity = { type: "activity"; paths: string[] };
export type MqttLiveFrame = MqttLiveSnapshot | MqttLiveActivity;
export type MqttLiveControlName = "closed" | "unavailable" | "timeout";
export type MqttLiveEventName = "message" | MqttLiveControlName;

export type MqttLiveSessionInfo = {
  session_id: string;
  owner: string;
  project_id: string;
  site_id: string;
  since: string;
};

export type MqttLiveConnectResponse = {
  ok: boolean;
  session: MqttLiveSessionInfo;
  connection: MqttLiveConnection;
};

export type MqttLiveStatusResponse = {
  session: MqttLiveSessionInfo | null;
  sidecar_available: boolean;
  connection: MqttLiveConnection | null;
  stats: MqttLiveStats | null;
  register: { assets: number; points: number } | null;
};

export type MqttLiveDisconnectResponse = { ok: boolean; released: boolean };

export function connectMqttLiveSession(input: {
  workspace: WorkspaceRef;
  authorized: boolean;
  rootFilter?: string;
  qos?: number;
  takeOver?: boolean;
  context?: ApiRequestContext;
}): Promise<MqttLiveConnectResponse> {
  return request<MqttLiveConnectResponse>(
    "/discovery/mqtt_sidecar/live/connect",
    {
      body: JSON.stringify({
        project_id: input.workspace.projectId,
        site_id: input.workspace.siteId,
        authorized: input.authorized,
        ...(input.rootFilter ? { root_filter: input.rootFilter } : {}),
        ...(input.qos !== undefined ? { qos: input.qos } : {}),
        ...(input.takeOver ? { take_over: true } : {}),
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function disconnectMqttLiveSession(input: {
  sessionId?: string;
  context?: ApiRequestContext;
}): Promise<MqttLiveDisconnectResponse> {
  return request<MqttLiveDisconnectResponse>(
    "/discovery/mqtt_sidecar/live/disconnect",
    {
      body: JSON.stringify(input.sessionId ? { session_id: input.sessionId } : {}),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export function getMqttLiveStatus(input: {
  context?: ApiRequestContext;
} = {}): Promise<MqttLiveStatusResponse> {
  return request<MqttLiveStatusResponse>("/discovery/mqtt_sidecar/live/status", undefined, input.context);
}

export type MqttLiveFocusResponse = { ok: boolean; focused: MqttLiveFocused | null };

// Focus one asset in the live session: its live points / config payload then ride
// the snapshot stream. Read-only; sets the sidecar's single-tenant focus.
export function focusMqttLiveAsset(input: {
  sessionId: string;
  asset: string;
  context?: ApiRequestContext;
}): Promise<MqttLiveFocusResponse> {
  return request<MqttLiveFocusResponse>(
    "/discovery/mqtt_sidecar/live/focus",
    {
      body: JSON.stringify({ session_id: input.sessionId, asset: input.asset }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

export type MqttLiveOk = { ok: boolean };

// Change the live subscription (filter and/or qos) on the held session.
export function subscribeMqttLive(input: {
  sessionId: string;
  rootFilter?: string;
  qos?: number;
  context?: ApiRequestContext;
}): Promise<MqttLiveOk> {
  return request<MqttLiveOk>(
    "/discovery/mqtt_sidecar/live/subscribe",
    {
      body: JSON.stringify({
        session_id: input.sessionId,
        ...(input.rootFilter !== undefined ? { root_filter: input.rootFilter } : {}),
        ...(input.qos !== undefined ? { qos: input.qos } : {}),
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

// Filter the live tree server-side (topic + asset + payload text). The filtered
// result rides the snapshot stream; blank clears the filter.
export function searchMqttLive(input: {
  sessionId: string;
  q: string;
  matchedOnly?: boolean;
  context?: ApiRequestContext;
}): Promise<MqttLiveOk> {
  return request<MqttLiveOk>(
    "/discovery/mqtt_sidecar/live/search",
    {
      body: JSON.stringify({ session_id: input.sessionId, q: input.q, matched_only: input.matchedOnly ?? false }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    input.context,
  );
}

function parseMqttLiveEventName(value: string): MqttLiveEventName | null {
  return value === "message" || value === "closed" || value === "unavailable" || value === "timeout"
    ? value
    : null;
}

function isMqttLiveFrame(value: unknown): value is MqttLiveFrame {
  return isPlainObject(value) && (value.type === "snapshot" || value.type === "activity");
}

export function parseMqttLiveSseBuffer(
  buffer: string,
): { events: Array<{ name: MqttLiveEventName; frame: MqttLiveFrame | null }>; rest: string } {
  const events: Array<{ name: MqttLiveEventName; frame: MqttLiveFrame | null }> = [];
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const block of parts) {
    const trimmed = block.trim();
    if (!trimmed) {
      continue;
    }
    let rawName = "message";
    const dataLines: string[] = [];
    for (const line of trimmed.split("\n")) {
      if (line.startsWith("event:")) {
        rawName = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trim());
      }
    }
    const name = parseMqttLiveEventName(rawName);
    if (name === null) {
      continue;
    }
    let decoded: unknown = null;
    if (dataLines.length > 0) {
      try {
        decoded = JSON.parse(dataLines.join(""));
      } catch {
        decoded = null;
      }
    }
    events.push({ name, frame: name === "message" && isMqttLiveFrame(decoded) ? decoded : null });
  }
  return { events, rest };
}

export type MqttLiveCallbacks = {
  onFrame: (frame: MqttLiveFrame) => void;
  onControl: (name: MqttLiveControlName) => void;
  onClose?: () => void;
  onError?: (error: unknown) => void;
};

/**
 * Opens the proxied MQTT live-session SSE stream and dispatches parsed frames.
 * Returns a disposer that aborts the fetch (cancel-safe). Mirrors
 * streamRunEvents; the backend never exposes the sidecar directly.
 */
export function streamMqttLiveEvents(
  sessionId: string,
  callbacks: MqttLiveCallbacks,
  context?: ApiRequestContext,
): () => void {
  const controller = new AbortController();
  let closed = false;

  const finish = (error?: unknown) => {
    if (closed) {
      return;
    }
    closed = true;
    if (error !== undefined) {
      callbacks.onError?.(error);
    }
    callbacks.onClose?.();
  };

  void (async () => {
    try {
      const init = {
        headers: { Accept: "text/event-stream" },
        signal: combineSignals(controller.signal, context?.signal),
      };
      const path = `/discovery/mqtt_sidecar/live/stream?session_id=${encodeURIComponent(sessionId)}`;
      const response = context?.client
        ? await context.client.fetchRaw(path, init)
        : await fetch(`${apiBaseUrl}${path}`, withApiKey(init));

      if (response.status === 401) {
        throw new ApiError(AUTH_REQUIRED_MESSAGE, response.status);
      }
      if (!response.ok) {
        throw new ApiError(await parseApiError(response), response.status);
      }
      if (!response.body) {
        throw new Error("Streaming response body is not available.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseMqttLiveSseBuffer(buffer);
        buffer = rest;
        for (const { name, frame } of events) {
          if (name === "message") {
            if (frame) {
              callbacks.onFrame(frame);
            }
          } else {
            callbacks.onControl(name);
          }
        }
      }
      finish();
    } catch (error) {
      if (controller.signal.aborted) {
        finish();
        return;
      }
      finish(error);
    }
  })();

  return () => {
    controller.abort();
  };
}
