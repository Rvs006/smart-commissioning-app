export type HealthStatus = {
  status: string;
  timestamp: string;
};

export type Blueprint = {
  services: string[];
  modules: string[];
  jobs: string[];
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
  duplicate_key_fields: string[];
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
  stored_file_name: string;
  created_at: string;
};

export type ImportErrorRecord = {
  row_number: number | null;
  field: string | null;
  code: string;
  message: string;
};

export type ImportErrorReport = {
  import_id: string;
  errors: ImportErrorRecord[];
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

export type ValidationIssuesResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  issues: ValidationIssueRecord[];
};

export type DiscoveryRunKind = "ip" | "bacnet" | "mqtt";
export type ValidationRunKind = "udmi" | "bacnet" | "mapping";
export type ImportTemplateFormat = "csv" | "xlsx";
export type ReportFormat = "zip" | "xlsx" | "docx";

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

export type DiscoveryResultsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  result_summary: Record<string, unknown>;
  discovered_assets: DiscoveryAssetObservation[];
  devices: DiscoveryRowRecord[];
  points: DiscoveryRowRecord[];
  topics: DiscoveryRowRecord[];
};

export type DiscoveryPointsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  points: DiscoveryRowRecord[];
};

export type DiscoveryTopicsResponse = {
  run_id: string;
  job_type: JobType;
  status: JobStatus;
  topics: DiscoveryRowRecord[];
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

  return (await response.json()) as T;
}

export type DownloadedFile = {
  blob: Blob;
  filename: string | null;
};

/**
 * Fetches a binary endpoint with the same auth handling as request().
 * Direct-navigation anchors cannot attach the X-API-Key header, so all
 * file downloads must go through this helper in hosted deployments.
 */
export async function downloadFile(path: string): Promise<DownloadedFile> {
  const response = await fetch(`${apiBaseUrl}${path}`, withApiKey());

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

export function getBlueprint(): Promise<Blueprint> {
  return request<Blueprint>("/blueprint");
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

export function getImportErrors(importId: string): Promise<ImportErrorReport> {
  return request<ImportErrorReport>(`/imports/${importId}/errors`);
}

export function getImportTemplatePath(importType: ImportType, format: ImportTemplateFormat): string {
  return `/imports/templates/${encodeURIComponent(importType)}.${format}`;
}

// URL helpers are display-only. Downloads must use downloadFile() so the
// X-API-Key header is attached; bare anchors 401 in hosted deployments.
export function getImportTemplateUrl(importType: ImportType, format: ImportTemplateFormat): string {
  return `${apiBaseUrl}${getImportTemplatePath(importType, format)}`;
}

export function getReportDownloadPath(reportId: string): string {
  return `/reports/${encodeURIComponent(reportId)}/download`;
}

export function getReportDownloadUrl(reportId: string): string {
  return `${apiBaseUrl}${getReportDownloadPath(reportId)}`;
}

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

export function startMqttConfigPublishRun(input: {
  topic: string;
  payload: string;
  confirmed: boolean;
  expectedPoint?: string;
  expectedValue?: string | number | boolean;
  useLiveBroker?: boolean;
  pointsetTopic?: string;
  waitSeconds?: number;
}): Promise<JobAcceptedResponse> {
  return request<JobAcceptedResponse>("/validation/mqtt-config/runs", {
    body: JSON.stringify({
      job_type: "mqtt_config_publish",
      parameters: {
        confirmed: input.confirmed,
        expected_point: input.expectedPoint ?? "",
        expected_value: input.expectedValue ?? "",
        pointset_topic: input.pointsetTopic ?? "",
        next_pointset_payload: {
          pointset: {
            points: input.expectedPoint
              ? { [input.expectedPoint]: { present_value: input.expectedValue ?? "" } }
              : {},
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

export function createReport(input: { reportType: ReportType; format?: ReportFormat }): Promise<ReportSummary> {
  return request<ReportSummary>("/reports", {
    body: JSON.stringify({
      output_format: input.format ?? "zip",
      project_id: "demo-project",
      report_type: input.reportType,
      site_id: "demo-site",
      source_run_ids: [],
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function listReports(): Promise<ReportListResponse> {
  return request<ReportListResponse>("/reports");
}

export function getReport(reportId: string): Promise<ReportSummary> {
  return request<ReportSummary>(`/reports/${encodeURIComponent(reportId)}`);
}

export type ListRunsParams = {
  projectId?: string;
  siteId?: string;
  jobType?: JobType;
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

export function listDiscoveryRuns(): Promise<RunListResponse> {
  return request<RunListResponse>("/discovery/runs");
}

export function getDiscoveryRun(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/discovery/runs/${encodeURIComponent(runId)}`);
}

export function getDiscoveryResults(runId: string): Promise<DiscoveryResultsResponse> {
  return request<DiscoveryResultsResponse>(`/discovery/runs/${encodeURIComponent(runId)}/results`);
}

export function getDiscoveryPoints(runId: string): Promise<DiscoveryPointsResponse> {
  return request<DiscoveryPointsResponse>(`/discovery/runs/${encodeURIComponent(runId)}/points`);
}

export function getDiscoveryTopics(runId: string): Promise<DiscoveryTopicsResponse> {
  return request<DiscoveryTopicsResponse>(`/discovery/runs/${encodeURIComponent(runId)}/topics`);
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
