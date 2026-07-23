export type HealthStatus = {
  status: string;
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
export type MeResponse = {
  username: string;
  role: Role;
  source: "user_key" | "shared_key" | "local";
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
};

export type UdmiPayloadMetrics = {
  expected: number;
  received: number;
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
  schema_version: "1.0";
  asset_metrics: UdmiAssetMetrics;
  payload_metrics: UdmiPayloadMetrics;
  fault_metrics: UdmiFaultMetrics;
  issue_metrics: UdmiIssueMetrics;
  system_metrics: UdmiSystemMetrics[];
  asset_results: UdmiAssetResult[];
  fault_rows: UdmiFaultRow[];
};

export type ValidationIssuesResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  issues: ValidationIssueRecord[];
};

export type DiscoveryRunKind = "ip" | "bacnet" | "mqtt";
export type ValidationRunKind = "udmi" | "bacnet" | "mapping";
export type ImportTemplateFormat = "csv" | "xlsx";
export type ReportFormat = "zip" | "xlsx" | "docx" | "pdf";

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
  report_title?: string | null;
};

export type ReportListResponse = {
  reports: ReportSummary[];
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

export type DiscoveryTopicsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  topics: DiscoveryRowRecord[];
  register_comparison?: RegisterComparison | null;
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

function withApiKey(init?: RequestInit): RequestInit | undefined {
  const apiKey = getApiKey();
  if (!apiKey) {
    return init;
  }
  const headers = new Headers(init?.headers);
  headers.set("X-API-Key", apiKey);
  return { ...init, headers };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, withApiKey(init));

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
export async function downloadFile(path: string, init?: RequestInit): Promise<DownloadedFile> {
  const response = await fetch(`${apiBaseUrl}${path}`, withApiKey(init));

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

export function getHealth(): Promise<HealthStatus> {
  return request<HealthStatus>("/health");
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
export function getSystemInterfaces(): Promise<SystemInterface[]> {
  return request<SystemInterface[]>("/system/interfaces");
}

// ---------------------------------------------------------------------------
// Identity + RBAC (Phase 4b).
//
// getMe resolves the current principal so the UI can gate engineer/admin
// actions. The user-admin calls are admin-only and surface the optional
// user-management view; non-admins never reach them (the entry is hidden).
// ---------------------------------------------------------------------------

export function getMe(): Promise<MeResponse> {
  return request<MeResponse>("/me");
}

export function listUsers(): Promise<UserRecord[]> {
  return request<UserRecord[]>("/users");
}

export function createUser(input: { username: string; role: Role }): Promise<CreateUserResponse> {
  return request<CreateUserResponse>("/users", {
    body: JSON.stringify({ role: input.role, username: input.username }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function deactivateUser(userId: string): Promise<UserRecord> {
  return request<UserRecord>(`/users/${encodeURIComponent(userId)}/deactivate`, {
    method: "POST",
  });
}

// Admin-only lost-key recovery: invalidates the user's current key immediately
// and returns a fresh plaintext key, displayed exactly once (same shape as
// createUser). The backend refuses (409) for deactivated users.
export function reissueUserKey(userId: string): Promise<CreateUserResponse> {
  return request<CreateUserResponse>(`/users/${encodeURIComponent(userId)}/key`, {
    method: "POST",
  });
}

export function updateUserRole(userId: string, role: Role): Promise<UserRecord> {
  return request<UserRecord>(`/users/${encodeURIComponent(userId)}/role`, {
    body: JSON.stringify({ role }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function getConfiguration(): Promise<ConfigurationSnapshot> {
  return request<ConfigurationSnapshot>("/configuration");
}

export function validateConfiguration(
  configuration: ConfigurationSnapshot,
): Promise<ConfigurationValidationResult> {
  return request<ConfigurationValidationResult>("/configuration/validate", {
    body: JSON.stringify(configuration),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function updateConfiguration(
  configuration: ConfigurationSnapshot,
): Promise<ConfigurationSnapshot> {
  return request<ConfigurationSnapshot>("/configuration", {
    body: JSON.stringify(configuration),
    headers: { "Content-Type": "application/json" },
    method: "PUT",
  });
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
): Promise<ConfigurationExport> {
  const configuration = await request<ConfigurationSnapshot>(
    `/configuration${buildConfigurationQuery(projectId, siteId)}`,
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
  );
}

// Importable secret material for one certificate field (CA Certificate / Client
// Certificate / Private Key). `content` is the PEM text in plain text so another
// engineer can import it on their OWN machine — the receiving machine re-encrypts
// it into its own secret store. Field decision 2026-07-20: plain text is fine for
// now, encryption at a later date.
export type ConfigurationSecretMaterial = {
  secret_ref: string;
  content: string;
  file_name?: string | null;
};

// v2 export envelope that INCLUDES secret material so a shared config file
// actually works across machines (2026-07-20 walkthrough ITEM-1). The server
// sets exported_at and reads the UNMASKED snapshot, so password-kind values
// (MQTT Password, Key Password, Log Upload Token) ride in `configuration` in
// plain text; certificate material rides in `secret_material`, keyed by field
// name. Distinct from the default masked ConfigurationExport (version 1).
export type ConfigurationExportEnvelope = {
  kind: "smart-commissioning-configuration";
  version: 2;
  exported_at: string;
  project_id: string | null;
  site_id: string | null;
  secrets_included: true;
  configuration: ConfigurationSnapshot;
  secret_material: Record<string, ConfigurationSecretMaterial>;
};

// Engineer-gated export that carries secrets in plain text (GET
// /configuration/export-with-secrets). Separate from exportConfiguration, whose
// default export stays masked. The server builds the whole envelope (including
// exported_at); this just fetches it for the UI to serialise to a file.
export function exportConfigurationWithSecrets(
  projectId?: string,
  siteId?: string,
): Promise<ConfigurationExportEnvelope> {
  return request<ConfigurationExportEnvelope>(
    `/configuration/export-with-secrets${buildConfigurationQuery(projectId, siteId)}`,
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
export function uploadLogs(): Promise<LogUploadResult> {
  return request<LogUploadResult>("/logs/upload", { method: "POST" });
}

export function storeSecretMaterial(input: {
  field: string;
  content: string;
  fileName?: string | null;
}): Promise<SecretMaterialResponse> {
  return request<SecretMaterialResponse>("/configuration/secrets", {
    body: JSON.stringify({
      content: input.content,
      field: input.field,
      file_name: input.fileName ?? null,
      section: "certificates",
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function listImportProfiles(): Promise<ImportProfileSummary[]> {
  return request<ImportProfileSummary[]>("/imports/profiles");
}

export function createImport(input: {
  importType: ImportType;
  file: File;
  projectId?: string;
  siteId?: string;
}): Promise<ImportBatchSummary> {
  const body = new FormData();
  body.append("import_type", input.importType);
  body.append("project_id", input.projectId ?? "demo-project");
  body.append("site_id", input.siteId ?? "demo-site");
  body.append("file", input.file);

  return request<ImportBatchSummary>("/imports", {
    body,
    method: "POST",
  });
}

// Per-row rejection reasons for one import. The POST above returns only the
// accepted/rejected counts; the reasons are persisted separately and read back
// from here, so an operator can see WHY rows were rejected.
export function getImportErrors(importId: string): Promise<ImportErrorReport> {
  return request<ImportErrorReport>(`/imports/${encodeURIComponent(importId)}/errors`);
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
): Promise<ImportBatchSummary | null> {
  const query = new URLSearchParams({
    import_type: importType,
    project_id: projectId,
    site_id: siteId,
  });
  return request<ImportBatchSummary>(`/imports/latest?${query.toString()}`).catch(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 404) {
        return null;
      }
      throw error;
    },
  );
}

// One uploaded non-published UDMI schema set: payloads that declare this
// version label (e.g. "nonpub.1") are validated against these schema files
// instead of a published canonical UDMI release.
export type UdmiSchemaSet = {
  version_label: string;
  filenames: string[];
  uploaded_at: string;
};

export function listUdmiSchemaSets(): Promise<UdmiSchemaSet[]> {
  return request<UdmiSchemaSet[]>("/udmi/schemas");
}

// Multipart upload mirroring createImport: version_label plus one or more
// .json schema files under the repeated "files" field. The backend 400s with
// an actionable detail on a bad label, missing schema roots, or invalid JSON.
export function uploadUdmiSchemaSet(input: {
  versionLabel: string;
  files: File[];
}): Promise<UdmiSchemaSet> {
  const body = new FormData();
  body.append("version_label", input.versionLabel);
  for (const file of input.files) {
    body.append("files", file);
  }
  return request<UdmiSchemaSet>("/udmi/schemas", {
    body,
    method: "POST",
  });
}

export function deleteUdmiSchemaSet(versionLabel: string): Promise<void> {
  return request<void>(`/udmi/schemas/${encodeURIComponent(versionLabel)}`, {
    method: "DELETE",
  });
}

export function getImportTemplatePath(importType: ImportType, format: ImportTemplateFormat): string {
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
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(`/discovery/${input.runKind}/runs`, {
    body: JSON.stringify({
      job_type: input.jobType,
      parameters: { requested_from: "frontend-review", ...(input.parameters ?? {}) },
      project_id: "demo-project",
      site_id: "demo-site",
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function startValidationRun(input: {
  runKind: ValidationRunKind;
  jobType: JobType;
  parameters?: Record<string, unknown>;
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(`/validation/${input.runKind}/runs`, {
    body: JSON.stringify({
      job_type: input.jobType,
      parameters: { requested_from: "frontend-review", ...(input.parameters ?? {}) },
      project_id: "demo-project",
      site_id: "demo-site",
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
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
  return request<JobAcceptedResponse>("/validation/mqtt-config/runs", {
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
        expected_points: allExpected.map((pair) => ({ point: pair.point.trim(), value: pair.value })),
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
      project_id: "demo-project",
      site_id: "demo-site",
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function getValidationRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/validation/runs/${runId}`);
}

export function getValidationIssues(runId: string): Promise<ValidationIssuesResponse> {
  return request<ValidationIssuesResponse>(`/validation/runs/${runId}/issues`);
}

export function createReport(input: {
  reportType: ReportType;
  format?: ReportFormat;
  sourceRunIds?: string[];
  reportTitle?: string;
}): Promise<ReportSummary> {
  return request<ReportSummary>("/reports", {
    body: JSON.stringify({
      output_format: input.format ?? "zip",
      project_id: "demo-project",
      report_type: input.reportType,
      site_id: "demo-site",
      source_run_ids: input.sourceRunIds ?? [],
      ...(input.reportTitle ? { report_title: input.reportTitle } : {}),
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function listReports(): Promise<ReportListResponse> {
  return request<ReportListResponse>("/reports");
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

export function listRuns(params?: ListRunsParams): Promise<RunListResponse> {
  return request<RunListResponse>(`/runs${buildRunsQuery(params)}`);
}

export function cancelRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
}

export function getDiscoveryRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/discovery/runs/${encodeURIComponent(runId)}`);
}

export function getDiscoveryResults(runId: string): Promise<DiscoveryResultsResponse> {
  return request<DiscoveryResultsResponse>(`/discovery/runs/${encodeURIComponent(runId)}/results`);
}

export function getDiscoveryTopics(runId: string): Promise<DiscoveryTopicsResponse> {
  return request<DiscoveryTopicsResponse>(`/discovery/runs/${encodeURIComponent(runId)}/topics`);
}

// Path (display-only; download via downloadFile so the X-API-Key header rides)
// for the server-generated XLSX of captured topics (mq9nhbzu Excel export). An
// optional topic filter applies the same +/# wildcard semantics server-side.
export function getDiscoveryTopicsXlsxPath(runId: string, topicFilter?: string): string {
  const base = `/discovery/runs/${encodeURIComponent(runId)}/topics.xlsx`;
  return topicFilter ? `${base}?topic_filter=${encodeURIComponent(topicFilter)}` : base;
}

export function listValidationRuns(): Promise<RunListResponse> {
  return request<RunListResponse>("/validation/runs");
}

export function rollbackMqttConfigPublish(runId: string): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>(
    `/validation/mqtt-config/runs/${encodeURIComponent(runId)}/rollback`,
    { method: "POST" },
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
export type RunEvent = {
  run_id: string;
  job_type?: JobType;
  status: JobStatus;
  stage?: string;
  progress_percent?: number;
  updated_at?: string | null;
  error_message?: string | null;
};

export type RunEventName = "message" | "terminal" | "timeout" | "gone";

export type RunEventCallbacks = {
  // Fired for every progress frame (the default "message" event) and for the
  // explicit "terminal" frame, so consumers always see the final state.
  onEvent: (event: RunEvent, name: RunEventName) => void;
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
export function parseSseBuffer(buffer: string): { events: { name: RunEventName; data: RunEvent | null }[]; rest: string } {
  const events: { name: RunEventName; data: RunEvent | null }[] = [];
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
    let name: RunEventName = "message";
    const dataLines: string[] = [];
    for (const line of trimmed.split("\n")) {
      if (line.startsWith("event:")) {
        name = line.slice("event:".length).trim() as RunEventName;
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trim());
      }
    }
    let data: RunEvent | null = null;
    if (dataLines.length > 0) {
      try {
        data = JSON.parse(dataLines.join("")) as RunEvent;
      } catch {
        // A malformed data frame is skipped rather than aborting the stream.
        data = null;
      }
    }
    events.push({ data, name });
  }
  return { events, rest };
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
export function streamRunEvents(runId: string, callbacks: RunEventCallbacks): () => void {
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
      const init = withApiKey({
        headers: { Accept: "text/event-stream" },
        signal: controller.signal,
      });
      const response = await fetch(
        `${apiBaseUrl}/runs/${encodeURIComponent(runId)}/events`,
        init,
      );

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
          if (data) {
            if (name === "terminal" || TERMINAL_EVENT_STATUSES.has(data.status)) {
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
