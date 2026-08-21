import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import {
  clearApiKey,
  createSessionBoundApiClient,
  setApiKey,
  type DiscoveryObservationRecord,
  type ReportFormat,
} from "../../api/client";
import { queryKeys } from "../../api/queryKeys";
import { SessionProvider } from "../../app/session";
import { SessionContext, type SessionContextValue } from "../../app/sessionContext";
import type { RunRef, SessionScopeId, WorkspaceRef } from "../../app/sessionScope";
import { ModulePage } from "./ModulePage";
import { resolvePermittedNmapProfile } from "./nmapProfileSelection";

// The engineer-gated controls (Queue, Upload, Publish, Cancel) require a known
// engineer+ role. These wiring tests set a key and stub /me as engineer so the
// existing engineer behaviour is exercised. A separate role test below covers
// the viewer (gated) and engineer (enabled) paths explicitly.
const mePayload = { username: "engineer-1", role: "engineer", source: "user_key" };

const profilesPayload = [
  {
    import_type: "ip_register",
    description: "Expected IP-addressable assets.",
    required_columns: ["asset_id", "ip_address"],
    duplicate_key_fields: ["asset_id"],
  },
];

const acceptedRun = {
  run_id: "run-ip-1",
  job_type: "ip_discovery",
  status: "queued",
  message: "IP discovery accepted.",
};

const previewAuthorization = {
  authorization_id: "auth-ip-1",
  preview_run_id: "run-ip-1",
  project_id: "demo-project",
  site_id: "demo-site",
  packet_plan_sha256: "a".repeat(64),
  approved_by: "admin-1",
  ticket: "CHG-1001",
  purpose: "ModulePage contract test",
  not_before: "2020-06-11T08:00:00Z",
  not_after: "2099-06-11T18:00:00Z",
  max_uses: 1,
  use_count: 0,
  consumed_run_id: null,
  revoked_at: null,
  revoked_by: null,
  revoke_reason: null,
  created_at: "2026-06-11T08:00:00Z",
};

const terminalRun = {
  run_id: "run-ip-1",
  job_type: "ip_discovery",
  status: "succeeded",
  stage: "register_comparison",
  progress_percent: 100,
  created_at: "2026-06-11T09:00:00Z",
  updated_at: "2026-06-11T09:05:00Z",
  project_id: "demo-project",
  site_id: "demo-site",
  parameters: {},
  result_summary: {
    hosts_responsive: 1,
    hosts_scanned: 3,
    ip_headline_metrics_v1: {
      schema_version: "1.0",
      metrics: [
        {
          schema_version: "1.0",
          heading: "Expected Devices",
          configured: true,
          value: 2,
          denominator: 2,
          percentage: 100,
          pending_count: 0,
          finalized_count: 3,
        },
        {
          schema_version: "1.0",
          heading: "Reachable Devices",
          configured: true,
          value: 1,
          denominator: 3,
          percentage: 33.33,
          pending_count: 0,
          finalized_count: 3,
        },
        {
          schema_version: "1.0",
          heading: "Register Matches",
          configured: true,
          value: 1,
          denominator: 2,
          percentage: 50,
          pending_count: 0,
          finalized_count: 3,
        },
        {
          schema_version: "1.0",
          heading: "Unexpected / Unregistered Hosts",
          configured: true,
          value: 0,
          denominator: 3,
          percentage: 0,
          pending_count: 0,
          finalized_count: 3,
        },
      ],
    },
  },
  error_message: null,
};

const resultsPayload = {
  run_id: "run-ip-1",
  job_type: "ip_discovery",
  status: "succeeded",
  result_summary: {
    ...terminalRun.result_summary,
    hosts_responsive: 1,
    hosts_scanned: 3,
  },
  discovered_assets: [
    {
      asset_id: null,
      ip_address: "192.0.2.214",
      mac_address: "02:00:00:00:00:03",
      hostname: "plant-controller",
      observed_ports: [{ port: 443, protocol: "tcp", service: "https" }],
      match_basis: "ip",
      last_seen_at: "2026-06-11T09:05:00Z",
      status_detail: "responsive: 443",
    },
  ],
  devices: [],
  points: [],
  topics: [],
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as unknown as Response;
}

function errorResponse(payload: unknown, status: number): Response {
  return {
    ok: false,
    status,
    statusText: "Conflict",
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  } as unknown as Response;
}

function controlledSseStream() {
  const encoder = new TextEncoder();
  const queued: Array<{ done: boolean; value?: Uint8Array }> = [];
  let pending: ((value: { done: boolean; value?: Uint8Array }) => void) | null = null;
  const deliver = (value: { done: boolean; value?: Uint8Array }) => {
    if (pending) {
      const resolve = pending;
      pending = null;
      resolve(value);
      return;
    }
    queued.push(value);
  };
  return {
    close: () => deliver({ done: true }),
    push: (frame: string) => deliver({ done: false, value: encoder.encode(frame) }),
    response: {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "Content-Type": "text/event-stream" }),
      body: {
        getReader: () => ({
          read: () => {
            const next = queued.shift();
            if (next) {
              return Promise.resolve(next);
            }
            return new Promise<{ done: boolean; value?: Uint8Array }>((resolve) => {
              pending = resolve;
            });
          },
        }),
      },
      json: async () => ({}),
    } as unknown as Response,
  };
}

function LocationProbe() {
  const location = useLocation();
  return (
    <span data-testid="test-location" hidden>
      {location.pathname}
      {location.search}
    </span>
  );
}

function renderModule(
  route: string,
  initialEntry = "/",
  scanAuthorizations: ReadonlyArray<typeof previewAuthorization> = [previewAuthorization],
  useScanAuthorizationFallback = true,
) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  // A key is set so the SessionProvider fetches /me; the stubs below return an
  // engineer role, matching the pre-RBAC behaviour these wiring tests assert.
  setApiKey("engineer-key");
  if (useScanAuthorizationFallback) {
    stubScanAuthorizationFallback(scanAuthorizations);
  }
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter initialEntries={[initialEntry]}>
            <LocationProbe />
            <ModulePage moduleRoute={route} />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

function stubScanAuthorizationFallback(
  scanAuthorizations: ReadonlyArray<typeof previewAuthorization> = [previewAuthorization],
) {
  const currentFetch = globalThis.fetch;
  if (typeof currentFetch !== "function") return;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/discovery/scan-authorizations?")) {
        return jsonResponse(scanAuthorizations);
      }
      return currentFetch(input, init);
    }),
  );
}

it("lets an admin approve a sealed IP preview from Run Controls", async () => {
  const previewRunId = "run-ip-preview-approval-1";
  const authorizations: typeof previewAuthorization[] = [];
  let approvalBody: Record<string, unknown> | null = null;
  let liveBody: Record<string, unknown> | null = null;

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
      if (url.endsWith("/api/v1/me")) return jsonResponse({ ...mePayload, role: "admin" });
      if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
      if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        if ((body.parameters as Record<string, unknown>).dry_run === true) {
          return jsonResponse({
            run_id: previewRunId,
            job_type: "ip_discovery",
            status: "queued",
            message: "IP preview accepted.",
          });
        }
        liveBody = body;
        return jsonResponse({
          run_id: "run-ip-live-approval-1",
          job_type: "ip_discovery",
          status: "queued",
          message: "IP discovery accepted.",
        });
      }
      if (url.endsWith("/api/v1/discovery/scan-authorizations") && init?.method === "POST") {
        approvalBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        const authorization = {
          ...previewAuthorization,
          preview_run_id: previewRunId,
          ticket: String(approvalBody.ticket),
          purpose: String(approvalBody.purpose),
          not_before: String(approvalBody.not_before),
          not_after: String(approvalBody.not_after),
        };
        return jsonResponse(authorization);
      }
      if (url.endsWith(`/api/v1/discovery/runs/${previewRunId}/results`)) {
        return jsonResponse({ ...resultsPayload, run_id: previewRunId, result_summary: { dry_run: true } });
      }
      if (url.endsWith(`/api/v1/discovery/runs/${previewRunId}`)) {
        return jsonResponse({ ...terminalRun, run_id: previewRunId, result_summary: { dry_run: true } });
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );

  renderModule("ip-scanner-sct", "/", authorizations);

  fireEvent.click(await screen.findByLabelText(/Dry run/i));
  fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
  await waitFor(() => expect(screen.getByText(new RegExp(`Run ID: ${previewRunId}`))).toBeInTheDocument());

  fireEvent.click(screen.getByLabelText(/Dry run/i));
  fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
  fireEvent.change(await screen.findByLabelText(/Change ticket/i), { target: { value: "CHG-2048" } });
  fireEvent.change(await screen.findByLabelText(/Approval purpose/i), {
    target: { value: "Commission the selected IP segment" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Approve preview" }));

  await waitFor(() => expect(approvalBody).not.toBeNull());
  expect(approvalBody).toMatchObject({
    preview_run_id: previewRunId,
    ticket: "CHG-2048",
    purpose: "Commission the selected IP segment",
  });

  const authorization = await screen.findByRole("combobox", {
    name: /Sealed preview authorization/i,
  });
  await waitFor(() => expect(authorization).toHaveValue(previewAuthorization.authorization_id));

  const runButton = screen.getByRole("button", { name: "Run" });
  await waitFor(() => expect(runButton).toBeEnabled());
  fireEvent.click(runButton);
  await waitFor(() => expect(liveBody).not.toBeNull());
  expect(liveBody).toMatchObject({
    parameters: {},
    preview_run_id: previewRunId,
    scan_authorization_id: previewAuthorization.authorization_id,
  });
});

it("frictionless deployment hides the authorization ceremony and runs a live scan directly", async () => {
  let liveBody: Record<string, unknown> | null = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
      if (url.endsWith("/api/v1/me")) return jsonResponse({ ...mePayload, authorization_enforced: false });
      if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
      if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
        liveBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse(acceptedRun);
      }
      if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) return jsonResponse(resultsPayload);
      if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) return jsonResponse(terminalRun);
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );

  renderModule("ip-scanner-sct");

  // The "I am authorized" checkbox and the sealed-preview authorization control
  // are both gone in a frictionless deployment. waitFor lets /me resolve first
  // (Target override renders regardless of the mode).
  await screen.findByLabelText(/Target override/i);
  await waitFor(() =>
    expect(screen.queryByLabelText(/I am authorized to scan this network/i)).toBeNull(),
  );
  expect(screen.queryByRole("combobox", { name: /Sealed preview authorization/i })).toBeNull();

  fireEvent.change(screen.getByLabelText(/Target override/i), { target: { value: "10.20.0.0/24" } });
  const runButton = await screen.findByRole("button", { name: "Run" });
  await waitFor(() => expect(runButton).toBeEnabled());
  fireEvent.click(runButton);

  // The live scan posts its real parameters directly, with no sealed ids.
  await waitFor(() => expect(liveBody).not.toBeNull());
  const body = liveBody as unknown as { parameters: Record<string, unknown> };
  expect(body.parameters).toMatchObject({ cidr: "10.20.0.0/24" });
  expect(body.parameters).not.toHaveProperty("dry_run");
  expect(body).not.toHaveProperty("preview_run_id");
  expect(body).not.toHaveProperty("scan_authorization_id");
});

async function prepareAuthorizedIpRun(): Promise<HTMLElement> {
  const dryRun = await screen.findByLabelText(/Dry run/i);
  fireEvent.click(dryRun);
  const previewButton = await screen.findByRole("button", { name: "Preview" });
  await waitFor(() => expect(previewButton).toBeEnabled());
  fireEvent.click(previewButton);
  await waitFor(() => expect(screen.getByText(/Run ID: run-ip-1/i)).toBeInTheDocument());

  fireEvent.click(screen.getByLabelText(/Dry run/i));
  fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
  const authorization = await screen.findByRole("combobox", {
    name: /Sealed preview authorization/i,
  });
  await waitFor(() => expect(authorization).toBeEnabled());
  fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });

  const runButton = await screen.findByRole("button", { name: "Run" });
  await waitFor(() => expect(runButton).toBeEnabled());
  return runButton;
}

async function submitReportDialog(opener: HTMLElement, title?: string) {
  fireEvent.click(opener);
  const dialog = await screen.findByRole("dialog", { name: "Name this validation report" });
  const titleInput = within(dialog).getByLabelText("Report title");
  if (title !== undefined) {
    fireEvent.change(titleInput, { target: { value: title } });
  }
  fireEvent.click(within(dialog).getByRole("button", { name: "Generate report" }));
}

describe("ModulePage discovery wiring", () => {
  it("resets a stale Nmap profile to the first approved profile", () => {
    expect(
      resolvePermittedNmapProfile("selected_udp", ["tcp_connect_inventory"]),
    ).toBe("tcp_connect_inventory");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    clearApiKey();
  });

  it("blocks a real scan until authorization is confirmed, then queues and renders live results", async () => {
    // A sweep that now reports every scanned host: the responder plus an
    // unregistered silent host (neutral) and a register-expected silent host
    // (amber/inconclusive). The engine emits "no response on scanned ports" —
    // a TCP-connect miss, never proof a host is absent.
    const liveResultsPayload = {
      ...resultsPayload,
      result_summary: { ...resultsPayload.result_summary, hosts_responsive: 1, hosts_scanned: 3 },
      discovered_assets: [
        ...resultsPayload.discovered_assets,
        {
          asset_id: null,
          ip_address: "192.0.2.9",
          mac_address: null,
          hostname: null,
          observed_ports: [],
          match_basis: "none",
          last_seen_at: null,
          status_detail: "no response on scanned ports (4 probed)",
        },
        {
          asset_id: "AHU-7",
          ip_address: "192.0.2.11",
          mac_address: null,
          hostname: null,
          observed_ports: [],
          match_basis: "none",
          last_seen_at: null,
          status_detail:
            "no response on scanned ports (2 probed) | EXPECTED BY REGISTER: expected from the " +
            "register import but did not answer this scan — inconclusive, not proof the host is offline",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(liveResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    // The real-scan run button is disabled until the operator confirms.
    const queueButton = await screen.findByRole("button", { name: "Run" });
    expect(queueButton).toBeDisabled();

    const authorizedRunButton = await prepareAuthorizedIpRun();
    fireEvent.click(authorizedRunButton);

    // Run monitor appears and live discovered hosts render from the results payload.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    await waitFor(
      () => expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
      { timeout: 5000 },
    );
    // hostname is unique to the live results payload (not present in sample rows);
    // it now appears in both the results table and the selected-result detail aside.
    expect((await screen.findAllByText("plant-controller")).length).toBeGreaterThan(0);
    // Live banner is shown (its ip-scanner-sct copy still opens with this phrase).
    expect(screen.getByText(/Live discovery observations/i)).toBeInTheDocument();
    // The Result column now reports each host's scan verdict.
    expect(screen.getByRole("columnheader", { name: "Result" })).toBeInTheDocument();
    // Both silent hosts surface the honest "no response" copy.
    expect((await screen.findAllByText("No response on scanned ports")).length).toBeGreaterThan(0);
    // jsdom cannot see theme CSS, so assert on classNames only: the register-
    // expected silent host shades amber (warn); the plain responder and the
    // unregistered silent host carry no pass/fail shading.
    expect(document.querySelector("tr.row-warn")).not.toBeNull();
    expect(document.querySelector("tr.row-pass, tr.row-fail")).toBeNull();

    // Headline metric now reflects the real run (hosts_responsive: 1), never the
    // old hardcoded "118" sample.
    expect(await screen.findByText("responsive hosts")).toBeInTheDocument();
    expect(screen.queryByText("118")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "IP scan headline metrics" })).toBeInTheDocument();
    expect(screen.getByText("Expected Devices")).toBeInTheDocument();
    expect(screen.getByText("Reachable Devices")).toBeInTheDocument();
    expect(screen.getByText("Register Matches")).toBeInTheDocument();
    expect(screen.getByText("Unexpected / Unregistered Hosts")).toBeInTheDocument();

    // A run the operator started here auto-advances to Results on success. Only
    // a *restored* run is exempt (see the run retention suite below).
    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
  });

  it("fences a terminal preview when an adapter reuses its id for the accepted live run", async () => {
    const sharedRunId = "run-ip-reused-events";
    const previewStream = controlledSseStream();
    const liveStream = controlledSseStream();
    let eventStreamRequests = 0;
    let liveSubmissionAccepted = false;
    let previewTerminalSignalReceived = false;
    let liveTerminal = false;
    let liveRunStatusRequests = 0;
    let terminalBarrierStatusRequests = 0;
    let liveTerminalSignalReceived = false;
    let liveResultsRequests = 0;
    let resolvePreviewEvidence: (() => void) | null = null;
    let resolveFirstTerminalBarrier: (() => void) | null = null;
    const previewRun = {
      ...terminalRun,
      run_id: sharedRunId,
      status: "running",
      progress_percent: 25,
      result_summary: { dry_run: true },
    };
    const liveRun = {
      ...terminalRun,
      run_id: sharedRunId,
      status: "running",
      progress_percent: 25,
      result_summary: {},
    };
    const terminalPreviewRun = { ...previewRun, status: "succeeded" };
    const terminalLiveRun = {
      ...terminalRun,
      run_id: sharedRunId,
      result_summary: {
        observation_evidence_v1: {
          attempt: 1,
          observation_count: 0,
          terminal_cursor: 0,
        },
      },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { parameters?: { dry_run?: boolean } };
          if (body.parameters?.dry_run) {
            return jsonResponse({ ...acceptedRun, run_id: sharedRunId, message: "IP preview accepted." });
          }
          liveSubmissionAccepted = true;
          return jsonResponse({ ...acceptedRun, run_id: sharedRunId, message: "IP discovery accepted." });
        }
        if (url.endsWith(`/api/v1/runs/${sharedRunId}/events`)) {
          eventStreamRequests += 1;
          return eventStreamRequests === 1 ? previewStream.response : liveStream.response;
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          if (!liveSubmissionAccepted) {
            return new Promise<Response>((resolve) => {
              resolvePreviewEvidence = () =>
                resolve(
                  jsonResponse({
                    ...resultsPayload,
                    run_id: sharedRunId,
                    result_summary: { dry_run: true },
                  }),
                );
            });
          }
          liveResultsRequests += 1;
          return jsonResponse({
            ...resultsPayload,
            run_id: sharedRunId,
            discovered_assets: [
              { ...resultsPayload.discovered_assets[0], hostname: "live-event-controller" },
            ],
          });
        }
        if (url.includes(`/api/v1/discovery/runs/${sharedRunId}/observations?`)) {
          if (!liveSubmissionAccepted) {
            return jsonResponse({
              run_id: sharedRunId,
              attempt: 1,
              observations: [],
              next_cursor: 7,
              latest_cursor: 7,
              has_more: false,
              terminal: { status: "succeeded", terminal_cursor: 7 },
              observations_pruned: false,
            });
          }
          return jsonResponse({
            run_id: sharedRunId,
            attempt: 1,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: liveTerminal ? { status: "succeeded", terminal_cursor: 0 } : null,
            observations_pruned: false,
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          if (!liveSubmissionAccepted) {
            return jsonResponse(previewTerminalSignalReceived ? terminalPreviewRun : previewRun);
          }
          liveRunStatusRequests += 1;
          if (!liveTerminalSignalReceived) {
            return jsonResponse(liveRun);
          }
          terminalBarrierStatusRequests += 1;
          if (terminalBarrierStatusRequests === 1) {
            return new Promise<Response>((resolve) => {
              resolveFirstTerminalBarrier = () => resolve(jsonResponse(liveRun));
            });
          }
          if (terminalBarrierStatusRequests < 4) {
            return jsonResponse(liveRun);
          }
          liveTerminal = true;
          return jsonResponse(terminalLiveRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct", "/", [{ ...previewAuthorization, preview_run_id: sharedRunId }]);

    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await waitFor(() =>
      expect(screen.getByText(new RegExp(`Run ID: ${sharedRunId}`))).toBeInTheDocument(),
    );

    previewTerminalSignalReceived = true;
    await act(async () => {
      previewStream.push(
        `event: terminal\ndata: ${JSON.stringify({ run_id: sharedRunId, status: "succeeded" })}\n\n`,
      );
    });
    await waitFor(() => expect(resolvePreviewEvidence).not.toBeNull());

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", {
      name: /Sealed preview authorization/i,
    });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    const runButton = screen.getByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await waitFor(() =>
      expect(screen.getByText(new RegExp(`Run ID: ${sharedRunId}`))).toBeInTheDocument(),
    );
    await waitFor(() => expect(liveRunStatusRequests).toBe(1));
    expect(liveResultsRequests).toBe(0);

    // The preview evidence request resolves after the new submission has been
    // accepted. It must not settle the reused live run's controller.
    await act(async () => {
      resolvePreviewEvidence?.();
    });
    expect(liveResultsRequests).toBe(0);

    // The old stream may still complete after the live run is accepted. That
    // stale terminal must neither drive live status nor fetch live results.
    await act(async () => {
      previewStream.push(
        `event: terminal\ndata: ${JSON.stringify({ run_id: sharedRunId, status: "succeeded" })}\n\n`,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(liveResultsRequests).toBe(0);

    liveTerminalSignalReceived = true;
    await act(async () => {
      liveStream.push(
        `event: terminal\ndata: ${JSON.stringify({ run_id: sharedRunId, status: "succeeded" })}\n\n`,
      );
    });

    // The live stream can report terminal slightly ahead of the durable record.
    // The bounded status barrier gets all four attempts (initial plus three
    // delays) before the terminal record allows any live results request.
    await waitFor(() => expect(resolveFirstTerminalBarrier).not.toBeNull());
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
    expect(liveResultsRequests).toBe(0);
    await act(async () => {
      resolveFirstTerminalBarrier?.();
    });
    await waitFor(() => expect(liveResultsRequests).toBeGreaterThan(0), { timeout: 4_000 });
    expect(
      await screen.findAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(2);
    expect(terminalBarrierStatusRequests).toBe(4);
    expect(liveRunStatusRequests).toBeGreaterThanOrEqual(5);
  });

  it("renders import warnings as a non-blocking amber panel distinct from errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/imports") && init?.method === "POST") {
          return jsonResponse({
            import_id: "import-ip-1",
            import_type: "ip_register",
            file_name: "ip_register.csv",
            file_type: "csv",
            project_id: "demo-project",
            site_id: "demo-site",
            total_rows: 1,
            accepted_rows: 1,
            rejected_rows: 0,
            status: "accepted",
            missing_columns: [],
            warnings: [
              {
                row_number: 2,
                field: "Expected services/ports",
                code: "udp_port_not_verified",
                message:
                  "47808/udp is a UDP service — the IP scan verifies TCP ports only. UDP 47808 (BACnet/IP) is verified by the BACnet discovery run.",
              },
            ],
            stored_file_name: "import-ip-1.csv",
            created_at: "2026-07-14T09:00:00Z",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    fireEvent.change(await screen.findByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["reg"], "ip_register.csv")] },
    });
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);

    // The import itself stays ACCEPTED; the UDP note arrives as a warning.
    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
    const warningPanel = screen
      .getByText(/UDP 47808 \(BACnet\/IP\) is verified by the BACnet discovery run/i)
      .closest(".state-panel");
    expect(warningPanel).toHaveClass("warning");
    expect(warningPanel).not.toHaveClass("error");
    expect(warningPanel).not.toHaveClass("rejected");
    expect(screen.getByText(/Row 2:/)).toBeInTheDocument();
    expect(screen.getByText(/affected rows are still accepted/i)).toBeInTheDocument();
  });

  const latestImportSummary = {
    import_id: "import-ip-9",
    import_type: "ip_register",
    file_name: "ip_register.csv",
    file_type: "csv",
    project_id: "demo-project",
    site_id: "demo-site",
    total_rows: 12,
    accepted_rows: 12,
    rejected_rows: 0,
    status: "accepted",
    missing_columns: [],
    stored_file_name: "import-ip-9.csv",
    created_at: "2026-07-16T09:00:00Z",
  };

  function stubLatestImportFetch(latest: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/imports/latest")) return jsonResponse(latest);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("shows a server-truth 'already imported' note when no file is staged (ISSUE-5)", async () => {
    stubLatestImportFetch(latestImportSummary);

    renderModule("ip-scanner-sct");

    // The empty file input no longer implies nothing was uploaded: the note names
    // the stored register and states it is persisted and used by runs here.
    expect(await screen.findByText("Register already imported")).toBeInTheDocument();
    expect(screen.getByText(/12 of 12 rows accepted/i)).toBeInTheDocument();
    expect(screen.getByText(/stored[\s\S]*used by runs on this page/i)).toBeInTheDocument();
  });

  it("hides the 'already imported' note while a new file is staged (ISSUE-5)", async () => {
    stubLatestImportFetch(latestImportSummary);

    renderModule("ip-scanner-sct");

    expect(await screen.findByText("Register already imported")).toBeInTheDocument();
    // Staging a file replaces the server-truth note with the in-session
    // "Selected: ..." line, so the two never both claim the current state.
    fireEvent.change(await screen.findByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["reg"], "new_ip_register.csv")] },
    });
    expect(screen.queryByText("Register already imported")).not.toBeInTheDocument();
    expect(screen.getByText(/Selected: new_ip_register\.csv/i)).toBeInTheDocument();
  });

  it("renders nothing extra when the server reports no prior import (ISSUE-5)", async () => {
    // getLatestImport maps a 404 to null; the note must not render on an empty
    // result — an empty file input is the honest state when nothing is on file.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/imports/latest")) {
          return {
            ok: false,
            status: 404,
            statusText: "Not Found",
            json: async () => ({ detail: "none" }),
          } as unknown as Response;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    // The profiles load resolves the card; the note is absent.
    expect(await screen.findByRole("button", { name: "Upload and validate" })).toBeInTheDocument();
    expect(screen.queryByText("Register already imported")).not.toBeInTheDocument();
  });

  it("sends a CIDR target override as parameters.cidr with no addresses key and no fabricated authorization principal", async () => {
    let previewBody: { parameters: Record<string, unknown> } | null = null;
    let liveBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          if (body.parameters.dry_run === true) previewBody = body;
          else liveBody = body;
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    // Type an ad-hoc CIDR override and authorize the real scan, mirroring the
    // authorized-scan test above.
    fireEvent.change(screen.getByLabelText(/Target override/i), {
      target: { value: "10.20.0.0/24" },
    });
    const queueButton = await prepareAuthorizedIpRun();
    fireEvent.click(queueButton);

    await waitFor(() => expect(previewBody).not.toBeNull());
    const parameters = (previewBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    // CIDR override belongs to the sealed preview. The authorized live request
    // carries only the preview and authorization references.
    expect(parameters.cidr).toBe("10.20.0.0/24");
    expect(parameters).not.toHaveProperty("addresses");
    expect(parameters).not.toHaveProperty("start");
    expect(parameters).not.toHaveProperty("end");
    expect(parameters.dry_run).toBe(true);
    expect(liveBody).toEqual({
      job_type: "ip_discovery",
      parameters: {},
      preview_run_id: "run-ip-1",
      scan_authorization_id: previewAuthorization.authorization_id,
      project_id: "demo-project",
      site_id: "demo-site",
    });
  });

  it("uses the accepted IP register when no target override is supplied", async () => {
    let previewBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          previewBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) return jsonResponse(terminalRun);
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) return jsonResponse(resultsPayload);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    await waitFor(() => expect(previewBody).not.toBeNull());
    const parameters = (previewBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.use_register_addresses).toBe(true);
    expect(parameters).not.toHaveProperty("target_expressions");
  });

  it("shows only confirmed Nmap profiles and submits the exact fixed profile contract", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/imports/latest")) {
          return new Response(JSON.stringify({ detail: "none" }), { status: 404 });
        }
        if (url.includes("/api/v1/nmap/capability?")) {
          return jsonResponse({
            schema_version: "1.0",
            provider: "nmap",
            state: "available",
            reason: "available",
            provider_mode: "internal_operator_managed",
            policy_id: "policy-1",
            policy_revision: 2,
            publisher: "Insecure.Com LLC",
            version: "7.98",
            fingerprint_sha256: "a".repeat(64),
            npcap_version: "1.83",
            npcap_state: "raw_capable",
            raw_capable: true,
            process_selection_allowed: true,
            xml_import_allowed: false,
            permitted_profiles: ["selected_udp", "host_discovery"],
          });
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    expect(await screen.findByText(/Nmap 7\.98 is confirmed for this site/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Discovery provider"), {
      target: { value: "operator_managed_nmap" },
    });
    const profile = screen.getByLabelText("Fixed Nmap profile");
    expect(within(profile).queryByRole("option", { name: "OS inventory" })).not.toBeInTheDocument();
    expect((profile as HTMLSelectElement).value).toBe("selected_udp");
    expect((screen.getAllByLabelText("Protocol")[0] as HTMLSelectElement).value).toBe("udp");
    fireEvent.change(profile, { target: { value: "host_discovery" } });
    expect(screen.queryByRole("button", { name: "Add port" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Dry run/));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.provider).toBe("operator_managed_nmap");
    expect(parameters.nmap_profile).toBe("host_discovery");
    expect(parameters).not.toHaveProperty("port_specification");
  });

  it("lets a global administrator approve the detected local Nmap installation once", async () => {
    let approvalBody: Record<string, string> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({
            ...mePayload,
            effective_scopes: [],
            global_scope: true,
            role: "admin",
          });
        }
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/imports/latest")) {
          return new Response(JSON.stringify({ detail: "none" }), { status: 404 });
        }
        if (url.includes("/api/v1/nmap/capability?")) {
          return jsonResponse({
            schema_version: "1.0",
            provider: "nmap",
            state: "disabled",
            reason: "policy_not_configured",
            provider_mode: "disabled",
            policy_id: null,
            policy_revision: null,
            publisher: null,
            version: null,
            fingerprint_sha256: null,
            npcap_version: null,
            npcap_state: "not_checked",
            raw_capable: false,
            process_selection_allowed: false,
            xml_import_allowed: false,
            permitted_profiles: ["selected_udp"],
          });
        }
        if (url.endsWith("/api/v1/nmap/approve-detected") && init?.method === "POST") {
          approvalBody = JSON.parse(String(init.body)) as Record<string, string>;
          return jsonResponse({
            schema_version: "1.0",
            provider: "nmap",
            state: "available",
            reason: "available",
            provider_mode: "internal_operator_managed",
            policy_id: "policy-1",
            policy_revision: 1,
            publisher: "Insecure.Com LLC",
            version: "7.98",
            fingerprint_sha256: "a".repeat(64),
            npcap_version: "1.83",
            npcap_state: "raw_capable",
            raw_capable: true,
            process_selection_allowed: true,
            xml_import_allowed: false,
            permitted_profiles: ["tcp_connect_inventory"],
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    const approve = await screen.findByRole("button", { name: "Approve detected Nmap" });
    fireEvent.click(approve);

    await waitFor(() => expect(approvalBody).not.toBeNull());
    expect(approvalBody).toEqual({ project_id: "demo-project", site_id: "demo-site" });
    expect(await screen.findByText(/Nmap 7\.98 is confirmed for this site/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve detected Nmap" })).not.toBeInTheDocument();
  });

  const mqttAccepted = {
    run_id: "run-mqtt-1",
    job_type: "mqtt_discovery",
    status: "queued",
    message: "MQTT discovery accepted.",
  };

  const mqttTerminal = {
    run_id: "run-mqtt-1",
    job_type: "mqtt_discovery",
    status: "succeeded",
    stage: "capture",
    progress_percent: 100,
    created_at: "2026-07-15T09:00:00Z",
    updated_at: "2026-07-15T09:05:00Z",
    project_id: "demo-project",
    site_id: "demo-site",
    parameters: {},
    result_summary: { topics_discovered: 0, messages_captured: 0 },
    error_message: null,
  };

  function stubMqttRunFetch(onPost: (body: { parameters: Record<string, unknown> }) => void) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          onPost(JSON.parse(String(init.body)) as { parameters: Record<string, unknown> });
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse({ ...resultsPayload, run_id: "run-mqtt-1", discovered_assets: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse(mqttTerminal);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  function stubMqttSidecarRunFetch(onPost: (body: { parameters: Record<string, unknown> }) => void) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt_sidecar/runs") && init?.method === "POST") {
          onPost(JSON.parse(String(init.body)) as { parameters: Record<string, unknown> });
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse({ ...resultsPayload, run_id: "run-mqtt-1", discovered_assets: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse(mqttTerminal);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  function stubIpSidecarRunFetch(onPost: (body: { parameters: Record<string, unknown> }) => void) {
    const ipAccepted = {
      run_id: "run-ip-1",
      job_type: "ip_scanner",
      status: "queued",
      message: "IP scanner accepted.",
    };
    const ipTerminal = {
      run_id: "run-ip-1",
      job_type: "ip_scanner",
      status: "succeeded",
      stage: "scan",
      progress_percent: 100,
      created_at: "2026-07-15T09:00:00Z",
      updated_at: "2026-07-15T09:05:00Z",
      project_id: "demo-project",
      site_id: "demo-site",
      parameters: {},
      result_summary: { hosts_scanned: 0 },
      error_message: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/ip_sidecar/runs") && init?.method === "POST") {
          onPost(JSON.parse(String(init.body)) as { parameters: Record<string, unknown> });
          return jsonResponse(ipAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse({ ...resultsPayload, run_id: "run-ip-1", devices: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(ipTerminal);
        }
        // Permissive fallback so the IP module's incidental polling never throws;
        // this test only asserts the run-submission parameters.
        if (url.includes("/api/v1/")) return jsonResponse({});
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("sends the IP scanner (sidecar) target range as start_ip/end_ip to the run", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubIpSidecarRunFetch((body) => {
      postedBody = body;
    });

    renderModule("ip-scanner");

    fireEvent.change(await screen.findByLabelText(/Start IP/i), {
      target: { value: "10.0.10.1" },
    });
    fireEvent.change(await screen.findByLabelText(/End IP/i), {
      target: { value: "10.0.10.254" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.start_ip).toBe("10.0.10.1");
    expect(parameters.end_ip).toBe("10.0.10.254");
  });

  it("hides the dry-run toggle on the IP scanner sidecar lane", async () => {
    stubIpSidecarRunFetch(() => {});
    renderModule("ip-scanner");
    // The vendored sidecar lanes drop the dry-run preview step the standalone
    // scanner apps never had; the range input is present, the dry-run checkbox is not.
    await screen.findByLabelText(/Start IP/i);
    expect(screen.queryByLabelText(/Dry run/i)).toBeNull();
  });

  it("sends the MQTT scanner (sidecar) topic filter and bounded run time to the run (M3)", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttSidecarRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-scanner");

    fireEvent.change(await screen.findByLabelText(/Topic filter/i), {
      target: { value: "site/asset-1/#" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "5" } });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "minutes" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.topic_filter).toBe("site/asset-1/#");
    expect(parameters.capture_seconds).toBe(300);
  });

  it("omits both MQTT scanner capture keys when filter and run time are blank (M3)", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttSidecarRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-scanner");

    // Blank filter + blank run time -> neither key on the wire; the adapter's own
    // defaults (# / 60s) apply, never a literal "#" or 0-sentinel.
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "" } });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters).not.toHaveProperty("topic_filter");
    expect(parameters).not.toHaveProperty("capture_seconds");
  });

  it("refuses an MQTT scanner run time over the 15-minute cap without posting (M3)", async () => {
    let posted = false;
    stubMqttSidecarRunFetch(() => {
      posted = true;
    });

    renderModule("mqtt-scanner");

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "16" } });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "minutes" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    expect(await screen.findByText(/15-minute scanner capture limit/i)).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    fireEvent.click(runButton);
    expect(posted).toBe(false);
  });

  it("blocks a non-numeric MQTT scanner run time and posts nothing (M3)", async () => {
    let posted = false;
    stubMqttSidecarRunFetch(() => {
      posted = true;
    });

    renderModule("mqtt-scanner");

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "45s" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(await screen.findByText(/Run time must be a positive number/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("sends an MQTT dry-run preview instead of an unauthorized live capture", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery-sct");

    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(
      (postedBody as unknown as { parameters: Record<string, unknown> }).parameters.dry_run,
    ).toBe(true);
  });

  it("fences same-ID preview MQTT topics from the accepted live epoch", async () => {
    const sharedRunId = "run-mqtt-reused";
    let liveSubmissionStarted = false;
    let liveResponseResolved = false;
    let previewTopicsSignal: AbortSignal | null = null;
    let previewExportSignal: AbortSignal | null = null;
    let previewTopicsRequests = 0;
    let liveTopicsRequests = 0;
    let resolveLivePost!: (response: Response) => void;
    const livePost = new Promise<Response>((resolve) => {
      resolveLivePost = resolve;
    });
    const terminal = { ...mqttTerminal, run_id: sharedRunId };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { parameters: { dry_run?: boolean } };
          if (!body.parameters.dry_run) {
            liveSubmissionStarted = true;
            return livePost;
          }
          return jsonResponse({ ...mqttAccepted, run_id: sharedRunId });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) return jsonResponse(terminal);
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          return jsonResponse({ ...resultsPayload, run_id: sharedRunId, discovered_assets: [] });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/topics.xlsx`)) {
          previewExportSignal = init?.signal ?? null;
          return new Promise<Response>(() => {});
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/topics`)) {
          if (!liveSubmissionStarted) {
            previewTopicsRequests += 1;
            if (previewTopicsRequests === 1) {
              return jsonResponse({
                run_id: sharedRunId,
                topics: [{ topic: "preview/site/device/events", last_payload: { preview: true } }],
              });
            }
            previewTopicsSignal = init?.signal ?? null;
            return new Promise<Response>(() => {});
          }
          if (!liveResponseResolved) {
            throw new Error("Live MQTT topics were requested before the submission response resolved.");
          }
          liveTopicsRequests += 1;
          return jsonResponse({
            run_id: sharedRunId,
            topics: [{ topic: "live/site/device/events", last_payload: { live: true } }],
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const view = renderModule("mqtt-discovery-sct");
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByText("preview/site/device/events")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Export to XLSX" }));
    await waitFor(() => expect(previewExportSignal).not.toBeNull());
    const previewTopicsKey = view.queryClient
      .getQueryCache()
      .findAll()
      .find(
        (query) =>
          Array.isArray(query.queryKey) &&
          query.queryKey.includes("topics") &&
          query.queryKey.includes(sharedRunId),
      )?.queryKey;
    expect(previewTopicsKey).toBeDefined();
    void view.queryClient.invalidateQueries({ queryKey: previewTopicsKey });
    await waitFor(() => expect(previewTopicsSignal).not.toBeNull());
    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await waitFor(() => expect(previewTopicsSignal?.aborted).toBe(true));
    await waitFor(() => expect(previewExportSignal?.aborted).toBe(true));
    expect(liveTopicsRequests).toBe(0);
    liveResponseResolved = true;
    resolveLivePost(jsonResponse({ ...mqttAccepted, run_id: sharedRunId }));
    await waitFor(() => expect(liveTopicsRequests).toBeGreaterThan(0));
    expect(screen.queryByText("preview/site/device/events")).not.toBeInTheDocument();
    expect(await screen.findByText("live/site/device/events")).toBeInTheDocument();
  });

  it.each([
    ["transport", () => Promise.reject(new Error("live MQTT response lost"))],
    ["HTTP 408", () => Promise.resolve(errorResponse({ detail: "Request timed out." }, 408))],
    ["HTTP 429", () => Promise.resolve(errorResponse({ detail: "Too many requests." }, 429))],
    ["HTTP 500", () => Promise.resolve(errorResponse({ detail: "Server error." }, 500))],
  ])("re-reserves a definitively rejected same-ID MQTT live retry before an ambiguous %s failure", async (_kind, ambiguousResponse) => {
    const sharedRunId = "run-mqtt-retry-fence";
    let liveAttempts = 0;
    let statusRequests = 0;
    let streamRequests = 0;
    let liveTopicRequests = 0;
    let resultsRequests = 0;
    let exportRequests = 0;
    const stream = controlledSseStream();
    const terminal = { ...mqttTerminal, run_id: sharedRunId };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { parameters: { dry_run?: boolean } };
          if (body.parameters.dry_run) {
            return jsonResponse({ ...mqttAccepted, run_id: sharedRunId });
          }
          liveAttempts += 1;
          if (liveAttempts === 1) {
            return errorResponse({ detail: "Authorization expired." }, 409);
          }
          return ambiguousResponse();
        }
        if (url.endsWith(`/api/v1/runs/${sharedRunId}/events`)) {
          streamRequests += 1;
          return stream.response;
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          statusRequests += 1;
          return jsonResponse(terminal);
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          resultsRequests += 1;
          return jsonResponse({ ...resultsPayload, run_id: sharedRunId, discovered_assets: [] });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/topics.xlsx`)) {
          exportRequests += 1;
          return new Promise<Response>(() => {});
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/topics`)) {
          liveTopicRequests += 1;
          return jsonResponse({
            run_id: sharedRunId,
            topics: [{ topic: "preview/site/device/events", last_payload: { preview: true } }],
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByText("preview/site/device/events")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Export to XLSX" }));
    await waitFor(() => expect(exportRequests).toBe(1));
    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await screen.findByText(/Run request failed/i);
    await waitFor(() => expect(runButton).toBeEnabled());
    expect(liveAttempts).toBe(1);
    expect(screen.queryByText("preview/site/device/events")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export to XLSX" })).toBeDisabled();
    const evidenceRequestsAfterRejection = {
      exportRequests,
      liveTopicRequests,
      resultsRequests,
      statusRequests,
      streamRequests,
    };
    expect(evidenceRequestsAfterRejection.statusRequests).toBeGreaterThan(0);
    expect(evidenceRequestsAfterRejection.streamRequests).toBeGreaterThan(0);
    expect(evidenceRequestsAfterRejection.liveTopicRequests).toBeGreaterThan(0);
    expect(evidenceRequestsAfterRejection.resultsRequests).toBeGreaterThan(0);

    // Install the fake clock while the definitive-rejection state is quiet so
    // any polling a regression starts for the second submission is owned by it.
    vi.useFakeTimers();
    fireEvent.click(runButton);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(liveAttempts).toBe(2);
    expect(runButton).toBeDisabled();
    expect(screen.queryByText("preview/site/device/events")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export to XLSX" })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    expect({ exportRequests, liveTopicRequests, resultsRequests, statusRequests, streamRequests }).toEqual(
      evidenceRequestsAfterRejection,
    );
    expect(runButton).toBeDisabled();
    expect(screen.queryByText("preview/site/device/events")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export to XLSX" })).toBeDisabled();
  });

  it("lets a viewer download MQTT XLSX evidence", async () => {
    const runId = "run-mqtt-viewer-export";
    const terminal = {
      ...mqttTerminal,
      run_id: runId,
      result_summary: { topics_discovered: 1, messages_captured: 1 },
    };
    const createObjectURL = vi.fn(() => "blob:mqtt-viewer-export");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: url.includes("job_type=mqtt_discovery") ? [{ ...terminal, edge_id: null }] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({ username: "viewer-1", role: "viewer", source: "user_key" });
        }
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith(`/api/v1/runs/${runId}/events`)) return controlledSseStream().response;
        if (url.endsWith(`/api/v1/discovery/runs/${runId}/topics`)) {
          return jsonResponse({
            run_id: runId,
            topics: [{ topic: "viewer/site/device/events", last_payload: { temperature: 22 } }],
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${runId}/results`)) {
          return jsonResponse({
            ...resultsPayload,
            run_id: runId,
            job_type: "mqtt_discovery",
            result_summary: terminal.result_summary,
            discovered_assets: [],
            topics: [],
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${runId}`)) return jsonResponse(terminal);
        if (url.endsWith(`/api/v1/discovery/runs/${runId}/topics.xlsx`)) {
          return {
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["viewer MQTT export"]),
            headers: { get: () => 'attachment; filename="viewer-topics.xlsx"' },
          } as unknown as Response;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");
    const downloadButton = await screen.findByRole("button", { name: "Export to XLSX" });
    await waitFor(() => expect(downloadButton).toBeEnabled());
    fireEvent.click(downloadButton);
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(anchorClick).toHaveBeenCalledTimes(1);
    anchorClick.mockRestore();
  });

  it.each(["resolve", "reject"])(
    "aborts a deferred MQTT XLSX export on access closure (%s late response)",
    async (settlement) => {
      const stream = controlledSseStream();
      const runId = "run-mqtt-xlsx-closed";
      const terminal = {
        ...mqttTerminal,
        run_id: runId,
        result_summary: { topics_discovered: 1, messages_captured: 1 },
      };
      let downloadSignal: AbortSignal | null = null;
      let resolveDownload!: (response: Response) => void;
      let rejectDownload!: (error: Error) => void;
      const deferredDownload = new Promise<Response>((resolve, reject) => {
        resolveDownload = resolve;
        rejectDownload = reject;
      });
      const createObjectURL = vi.fn(() => "blob:stale-mqtt-xlsx");
      vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
      const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes("/api/v1/runs?")) {
            return jsonResponse({
              runs: url.includes("job_type=mqtt_discovery") ? [{ ...terminal, edge_id: null }] : [],
            });
          }
          if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
          if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
          if (url.endsWith(`/api/v1/runs/${runId}/events`)) return stream.response;
          if (url.endsWith(`/api/v1/discovery/runs/${runId}/topics.xlsx`)) {
            downloadSignal = init?.signal ?? null;
            return deferredDownload;
          }
          if (url.endsWith(`/api/v1/discovery/runs/${runId}/topics`)) {
            return jsonResponse({
              run_id: runId,
              topics: [{ topic: "live/site/device/events", last_payload: { temperature: 22 } }],
            });
          }
          if (url.endsWith(`/api/v1/discovery/runs/${runId}/results`)) {
            return jsonResponse({
              ...resultsPayload,
              run_id: runId,
              job_type: "mqtt_discovery",
              result_summary: terminal.result_summary,
              discovered_assets: [],
              topics: [],
            });
          }
          if (url.endsWith(`/api/v1/discovery/runs/${runId}`)) return jsonResponse(terminal);
          throw new Error(`Unexpected fetch in test: ${url}`);
        }),
      );

      renderModule("mqtt-discovery-sct");
      const downloadButton = await screen.findByRole("button", { name: "Export to XLSX" });
      await waitFor(() => expect(downloadButton).toBeEnabled());
      fireEvent.click(downloadButton);
      await waitFor(() => expect(downloadSignal).not.toBeNull());
      expect(screen.getByRole("button", { name: "Exporting..." })).toBeDisabled();

      stream.push(`event: closed\ndata: ${JSON.stringify({ run_id: runId, status: "closed" })}\n\n`);
      await waitFor(() => expect(downloadSignal?.aborted).toBe(true));
      await waitFor(() => expect(screen.getByRole("button", { name: "Export to XLSX" })).toBeDisabled());
      await act(async () => {
        if (settlement === "resolve") {
          resolveDownload({
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["stale MQTT export"]),
            headers: { get: () => 'attachment; filename="stale.xlsx"' },
          } as unknown as Response);
        } else {
          rejectDownload(new Error("late MQTT export failure"));
        }
      });
      expect(createObjectURL).not.toHaveBeenCalled();
      expect(anchorClick).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Export to XLSX" })).not.toHaveTextContent(
        "Exporting...",
      );
      anchorClick.mockRestore();
      stream.close();
    },
  );

  it("converts an hours capture duration to seconds on the MQTT discovery wire", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery-sct");

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "2" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.capture_seconds).toBe(7200);
  });

  it("refuses an MQTT capture duration over the 48-hour cap without posting", async () => {
    let posted = false;
    stubMqttRunFetch(() => {
      posted = true;
    });

    renderModule("mqtt-discovery-sct");

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "49" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    expect(await screen.findByText(/exceeds the 48-hour capture limit/i)).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    fireEvent.click(runButton);
    expect(posted).toBe(false);
  });

  it("keeps a blank MQTT capture duration as the 0 indefinite sentinel regardless of unit", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery-sct");

    // Clear the default "10" so the duration is blank, then pick an hours unit:
    // the unit multiplier must not turn a blank (indefinite) into a bounded 0.
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.capture_seconds).toBe(0);
  });

  it("blocks a non-numeric MQTT run time with a validation error and posts nothing", async () => {
    let posted = false;
    stubMqttRunFetch(() => {
      posted = true;
    });

    renderModule("mqtt-discovery-sct");

    // "45s" must NOT silently coerce to the 0 = indefinite sentinel: that would
    // turn an intended bounded window into an unbounded background capture. It is
    // rejected client-side with a visible error and no parameters are posted —
    // mirroring the UDMI run-time path.
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "45s" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(await screen.findByText(/Run time must be a positive number/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("omits topic_filter from the MQTT run when the filter is left blank so the engine captures every topic (#) (2026-07-20 walkthrough ITEM-2)", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery-sct");

    // Do NOT touch the topic filter: it defaults to blank. Root Topic was removed
    // from Configuration, so a blank filter is omitted from the run parameters
    // entirely and the engine falls back to its own "#" default (capture-all) —
    // never a literal "#" on the wire.
    fireEvent.click(await screen.findByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters).not.toHaveProperty("topic_filter");
  });

  it("sends an explicit MQTT topic filter verbatim when the operator types one (ISSUE-3)", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery-sct");

    // An operator who wants a full-wildcard or scoped capture types it explicitly;
    // it flows through unchanged as the run's topic_filter override.
    fireEvent.change(await screen.findByLabelText(/Topic filter/i), {
      target: { value: "site/asset-1/#" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.topic_filter).toBe("site/asset-1/#");
  });

  it("selects an MQTT topic row to inspect its real payload with honest metadata and no fabricated issues", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      // subscribe_qos 0 is the delivery-QoS cap the run requested.
      result_summary: { topics_discovered: 2, messages_captured: 5, subscribe_qos: 0 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "udmi/AHU-1/state",
          message_count: 3,
          last_payload: { online: true, firmware: "1.2.3" },
          created_at: "2026-07-15T09:05:00Z",
          attributes: {
            device_ref: "AHU-1",
            position: 0,
            last_retained: true,
            last_qos: 1,
            last_received_at: "2026-07-15T10:00:00+00:00",
          },
        },
        {
          topic: "sensors/raw/blob",
          message_count: 1,
          // Engine presence marker for a non-JSON payload — no raw bytes stored.
          last_payload: { _raw_present: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: {
            device_ref: null,
            position: 1,
            last_retained: false,
            last_qos: 0,
            last_received_at: "2026-07-15T10:01:00+00:00",
          },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({
            ...mqttTerminal,
            result_summary: mqttResultsPayload.result_summary,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // Table populates; the inspector defaults to the first row (AHU-1, JSON),
    // so its payload panel (unique heading) and JSON tree are shown up front.
    expect(await screen.findByText(/Last payload on udmi\/AHU-1\/state/)).toBeInTheDocument();
    expect(screen.getByText("Explore JSON tree")).toBeInTheDocument();

    // The fabricated sample MQTT issue must NOT render on this live discovery
    // route. The operatorData issueRows fixture and the workspace?.issues
    // fallback are deleted outright now; this stays as the end-state guard.
    expect(
      screen.queryByText(/Telemetry interval exceeds configured tolerance/i),
    ).not.toBeInTheDocument();

    // A data cell is inert. The explicit Select evidence control moves the
    // inspector to the second topic without relying on a pointer-only row click.
    const rawCell = screen.getByText("sensors/raw/blob");
    const rawRow = rawCell.closest("tr");
    fireEvent.click(rawCell);
    expect(rawRow?.className).not.toContain("row-selected");
    fireEvent.click(
      within(rawRow as HTMLElement).getByRole("button", { name: /Select evidence/i }),
    );
    expect(await screen.findByText(/Non-JSON payload observed/i)).toBeInTheDocument();
    expect(screen.queryByText("Explore JSON tree")).not.toBeInTheDocument();

    // The explicitly selected row carries the selection class without opening
    // the View dialog.
    expect(rawRow?.className).toContain("row-selected");

    // Metadata detail items are present with honesty-rule labels; a timestamp is
    // NEVER labelled "Published" (MQTT 3.1.1 carries no publish time on the wire).
    expect(screen.getByText("Retained")).toBeInTheDocument();
    expect(screen.getByText("Delivery QoS")).toBeInTheDocument();
    expect(screen.getByText("Received at")).toBeInTheDocument();
    expect(screen.queryByText("Published")).not.toBeInTheDocument();
  });

  it("opens MQTT results from the sealed result snapshot when the live topics refresh fails", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 1, messages_captured: 1 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "site/AHU-1/state",
          message_count: 1,
          last_payload: { retained: true, temperature: 21.5 },
          created_at: "2026-08-13T09:05:00Z",
          attributes: { last_retained: true, last_qos: 1 },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return {
            ok: false,
            status: 503,
            statusText: "Service Unavailable",
            json: async () => ({ detail: "broker snapshot temporarily unavailable" }),
          } as unknown as Response;
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: mqttResultsPayload.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(await screen.findByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect((await screen.findAllByText("site/AHU-1/state")).length).toBeGreaterThan(0);
    expect(document.querySelector('[data-step="results"]')).toBeInTheDocument();
  });

  it("filters the results table by text, preserves selection, and never shows the scan empty state for a filter miss (ISSUE-4)", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 2, messages_captured: 5 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "udmi/AHU-1/state",
          message_count: 3,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: "AHU-1", position: 0 },
        },
        {
          topic: "sensors/raw/blob",
          message_count: 1,
          last_payload: { _raw_present: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 1 },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({
            ...mqttTerminal,
            result_summary: mqttResultsPayload.result_summary,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // Both topic rows land and the count line reports the full set.
    expect(await screen.findByText("sensors/raw/blob")).toBeInTheDocument();
    expect(screen.getByText(/Showing 2 of 2 rows/)).toBeInTheDocument();

    // Filter to the blob topic: the AHU row leaves the table, the count updates,
    // and selection follows to the only visible row (its inspector heading).
    fireEvent.change(screen.getByLabelText(/Filter results/i), { target: { value: "sensors" } });
    expect(screen.getByText(/Showing 1 of 2 rows/)).toBeInTheDocument();
    // Selection follows to the only visible row before asserting the AHU row is
    // fully gone (table cell AND its inspector echo).
    expect(await screen.findByText(/Last payload on sensors\/raw\/blob/)).toBeInTheDocument();
    expect(screen.queryByText("udmi/AHU-1/state")).not.toBeInTheDocument();

    // A filter that matches nothing shows the filter-specific note — NEVER the
    // scan empty state, whose copy would assert something about the network.
    fireEvent.change(screen.getByLabelText(/Filter results/i), { target: { value: "zzz-none" } });
    expect(screen.getByText("No rows match the current filters")).toBeInTheDocument();
    expect(screen.queryByText(/Capture complete/i)).not.toBeInTheDocument();
    expect(screen.queryByText("No results yet")).not.toBeInTheDocument();
    expect(screen.getByText(/Showing 0 of 2 rows/)).toBeInTheDocument();
    // The Inspector must not keep the previously-selected (now hidden) topic's
    // payload on screen while the table reports zero matches: with nothing
    // visible the selection is null and the aside falls back to its own empty
    // state (ISSUE-4).
    expect(screen.queryByText(/Last payload on sensors\/raw\/blob/)).not.toBeInTheDocument();
    expect(screen.getByText("No topic selected")).toBeInTheDocument();

    // Clearing restores every row.
    fireEvent.click(screen.getByRole("button", { name: /Clear filters/i }));
    expect(screen.getByText(/Showing 2 of 2 rows/)).toBeInTheDocument();
    expect(screen.getByText("udmi/AHU-1/state")).toBeInTheDocument();
  });

  it("filters the results table by verdict tone on the MQTT route (ISSUE-4)", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 2, messages_captured: 5 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "site/asset-1/state",
          message_count: 3,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: "AHU-1", position: 0, register_match: "matched" },
        },
        {
          topic: "rogue/asset-9/state",
          message_count: 1,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 1, register_match: "unmatched" },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({
            ...mqttTerminal,
            result_summary: mqttResultsPayload.result_summary,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(await screen.findByText("rogue/asset-9/state")).toBeInTheDocument();

    // "Not in register" keeps only the unmatched (fail-tone) row.
    fireEvent.change(screen.getByLabelText(/Verdict/i), { target: { value: "fail" } });
    expect(screen.getByText(/Showing 1 of 2 rows/)).toBeInTheDocument();
    expect(screen.queryByText("site/asset-1/state")).not.toBeInTheDocument();
    // Present in the table cell (and, since selection follows, the inspector).
    expect(screen.getAllByText("rogue/asset-9/state").length).toBeGreaterThan(0);
  });

  it("shows 'Not recorded' MQTT metadata for a run that predates metadata capture", async () => {
    // An old persisted run: topics carry no last_retained/last_qos/last_received_at
    // and the summary has no subscribe_qos. The inspector must render without
    // crashing and never fabricate values.
    const legacyResults = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 1, messages_captured: 2 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "udmi/AHU-1/state",
          message_count: 2,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: "AHU-1", position: 0 },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(legacyResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: legacyResults.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // The topic appears in both the table cell and the inspector — wait for it.
    expect((await screen.findAllByText("udmi/AHU-1/state")).length).toBeGreaterThan(0);
    // The metadata labels render, and the values honestly read "Not recorded".
    expect(screen.getByText("Retained")).toBeInTheDocument();
    expect(screen.getAllByText(/Not recorded/).length).toBeGreaterThan(0);
  });

  it("shades register-matched and register-foreign MQTT rows and shows the compare banner", async () => {
    const comparedResults = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 2, messages_captured: 4 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "demo-site/b1/ahu-1/state",
          message_count: 3,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: {
            device_ref: "AHU-1",
            position: 0,
            register_match: "matched",
            register_matched_filter: "demo-site/b1/ahu-1/#",
            register_asset_id: "AHU-1",
          },
        },
        {
          topic: "demo-site/rogue/x/state",
          message_count: 1,
          last_payload: { present_value: 1 },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 1, register_match: "unmatched" },
        },
      ],
      register_comparison: {
        register_available: true,
        import_filename: "register.csv",
        matched_count: 1,
        unmatched_count: 1,
        expected_filter_count: 2,
        unobserved_filters: [{ asset_id: "FCU-2", filter: "demo-site/b1/fcu-2/state" }],
      },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(comparedResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: comparedResults.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // Banner switches to the register-comparison copy (route-aware).
    expect(
      await screen.findByText(/Green rows match a topic in the uploaded MQTT register/),
    ).toBeInTheDocument();
    // The counts note is present on its own line.
    expect(screen.getByText(/1 topic matches the register/)).toBeInTheDocument();
    expect(screen.getByText(/demo-site\/b1\/fcu-2\/state/)).toBeInTheDocument();

    // The matched row shades green, the foreign row red. Assert on classes only
    // (jsdom cannot see the theme CSS that hides/reveals rows).
    const passRow = document.querySelector("tr.row-pass");
    const failRow = document.querySelector("tr.row-fail");
    expect(passRow?.textContent).toContain("demo-site/b1/ahu-1/state");
    expect(failRow?.textContent).toContain("demo-site/rogue/x/state");
  });

  it("prompts to upload a register when no MQTT register import exists", async () => {
    const noRegisterResults = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 1, messages_captured: 1 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "demo-site/rogue/x/state",
          message_count: 1,
          last_payload: { present_value: 1 },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 0 },
        },
      ],
      register_comparison: { register_available: false },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(noRegisterResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({
            ...mqttTerminal,
            result_summary: noRegisterResults.result_summary,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(
      await screen.findByText(/No accepted MQTT register import for this project\/site/),
    ).toBeInTheDocument();
    // No register means NO verdicts — never all-red.
    expect(document.querySelector("tr.row-pass, tr.row-fail")).toBeNull();
  });

  it("renders the MAC column and keeps the discovery per-host detail dialog", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    const authorizedRunButton = await prepareAuthorizedIpRun();
    fireEvent.click(authorizedRunButton);

    // The now-populated MAC column renders (header + the live cell value), proving
    // the engine's mac_address flows through to the table.
    expect(await screen.findByRole("columnheader", { name: "MAC Address" })).toBeInTheDocument();
    expect((await screen.findAllByText("02:00:00:00:00:03")).length).toBeGreaterThan(0);

    // Clicking the per-row "View" opens the existing discovery detail dialog.
    // UDMI alone routes View Issues into the persistent Inspector.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const viewButton = screen.getAllByRole("button", { name: "View" })[0];
    fireEvent.click(viewButton);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.tagName).toBe("DIALOG");
    await waitFor(() => expect(dialog).toHaveFocus());
    expect(within(dialog).getByText("MAC Address")).toBeInTheDocument();
    expect(within(dialog).getByText("02:00:00:00:00:03")).toBeInTheDocument();
    expect(within(dialog).getByText("Hostname")).toBeInTheDocument();
    expect(within(dialog).getByText("plant-controller")).toBeInTheDocument();

    // Close returns focus to the row action that opened the dialog.
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(viewButton).toHaveFocus());

    // The native cancel event is the browser Escape path and restores the same opener.
    fireEvent.click(viewButton);
    const escapeDialog = await screen.findByRole("dialog");
    fireEvent(escapeDialog, new Event("cancel", { cancelable: true }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(viewButton).toHaveFocus());
  });

  it("shows a neutral empty-state metric (no hardcoded sample) before any run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        return jsonResponse({});
      }),
    );

    renderModule("ip-scanner-sct");

    // Before any run the headline metric is a neutral empty state, NOT the old
    // hardcoded sample ("118" / "reachable hosts") that looked like a real scan.
    expect(await screen.findByText("No run yet")).toBeInTheDocument();
    expect(screen.queryByText("118")).not.toBeInTheDocument();
    expect(screen.queryByText("reachable hosts")).not.toBeInTheDocument();
  });

  it("shows a dry-run preview button that needs no authorization", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    // Enabled once the engineer role resolves (no scan-auth needed for dry run).
    await waitFor(() => expect(previewButton).toBeEnabled());
  });

  // A scan that completed and genuinely found nothing used to land on the same
  // "No results yet" as a head that had never run (field engineer 2026-07-15). These are
  // text-content assertions on the always-in-DOM results section: jsdom applies
  // no theme CSS, so step-gating visibility is not assertable here.
  it("states what was probed when a scan completes and finds nothing", async () => {
    const emptySummary = { hosts_responsive: 0, hosts_scanned: 254 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse({
            ...resultsPayload,
            result_summary: emptySummary,
            discovered_assets: [],
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse({ ...terminalRun, result_summary: emptySummary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    const queueButton = await prepareAuthorizedIpRun();
    fireEvent.click(queueButton);

    expect(
      await screen.findByText(/Scan complete — no responsive hosts found/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/254 hosts probed/i)).toBeInTheDocument();
    expect(screen.queryByText("No results yet")).not.toBeInTheDocument();
    // Honesty: a succeeded run that observed nothing is a real observation and
    // must never be dressed up as a failure.
    expect(document.querySelector(".empty-workspace")?.textContent).not.toMatch(/fail/i);
  });

  it("labels an empty dry-run preview as a preview, not a negative finding", async () => {
    // The engine stamps hosts_scanned: 0 on a dry run because it sends no
    // packets; without the dry_run gate this would read as "0 hosts were
    // probed" — a network claim about a run that never touched the network.
    const dryRunSummary = { dry_run: true, hosts_responsive: 0, hosts_scanned: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse({
            ...resultsPayload,
            result_summary: dryRunSummary,
            discovered_assets: [],
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse({ ...terminalRun, result_summary: dryRunSummary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    expect(await screen.findByText(/Dry run complete — preview only/i)).toBeInTheDocument();
    expect(screen.queryByText(/no responsive hosts found/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/0 hosts were probed/i)).not.toBeInTheDocument();
  });
});

describe("ModulePage BACnet backend provenance", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("uses a sealed BACnet preview before the authorized live request", async () => {
    let previewBody: Record<string, unknown> | null = null;
    let liveBody: Record<string, unknown> | null = null;
    const previewRunId = "run-bacnet-preview-1";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          if ((body.parameters as Record<string, unknown>).dry_run === true) {
            previewBody = body;
            return jsonResponse({
              run_id: previewRunId,
              job_type: "bacnet_discovery",
              status: "queued",
              message: "BACnet preview accepted.",
            });
          }
          liveBody = body;
          return jsonResponse({
            run_id: "run-bacnet-live-1",
            job_type: "bacnet_discovery",
            status: "queued",
            message: "BACnet discovery accepted.",
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${previewRunId}/results`)) {
          return jsonResponse({
            run_id: previewRunId,
            job_type: "bacnet_discovery",
            status: "succeeded",
            result_summary: { dry_run: true },
            discovered_assets: [],
            devices: [],
            points: [],
            topics: [],
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${previewRunId}`)) {
          return jsonResponse({
            ...terminalRun,
            run_id: previewRunId,
            job_type: "bacnet_discovery",
            result_summary: { dry_run: true },
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-live-1")) {
          return jsonResponse({
            ...terminalRun,
            run_id: "run-bacnet-live-1",
            job_type: "bacnet_discovery",
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-live-1/results")) {
          return jsonResponse({
            ...resultsPayload,
            run_id: "run-bacnet-live-1",
            job_type: "bacnet_discovery",
            devices: [],
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("bacnet-discovery-sct");

    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    await waitFor(() => expect(previewBody).not.toBeNull());
    expect((previewBody as unknown as { parameters: Record<string, unknown> }).parameters).toMatchObject({
      dry_run: true,
    });

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", {
      name: /Sealed preview authorization/i,
    });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });

    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(liveBody).not.toBeNull());
    expect(liveBody).toMatchObject({
      job_type: "bacnet_discovery",
      parameters: {},
      preview_run_id: previewRunId,
      scan_authorization_id: previewAuthorization.authorization_id,
    });
  });

  it("fences same-ID preview BACnet points and comparison evidence from the live epoch", async () => {
    const sharedRunId = "run-bacnet-reused";
    let liveSubmissionStarted = false;
    let liveResponseResolved = false;
    let previewPointsSignal: AbortSignal | null = null;
    let previewComparisonSignal: AbortSignal | null = null;
    let livePointsRequests = 0;
    let liveComparisonRequests = 0;
    let statusRequests = 0;
    let streamRequests = 0;
    let statusRequestsAtLivePost = 0;
    let streamRequestsAtLivePost = 0;
    const runStream = controlledSseStream();
    let resolveLivePost!: (response: Response) => void;
    const livePost = new Promise<Response>((resolve) => {
      resolveLivePost = resolve;
    });
    const terminal = {
      ...terminalRun,
      run_id: sharedRunId,
      job_type: "bacnet_discovery",
      result_summary: {},
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { parameters: { dry_run?: boolean } };
          if (!body.parameters.dry_run) {
            liveSubmissionStarted = true;
            statusRequestsAtLivePost = statusRequests;
            streamRequestsAtLivePost = streamRequests;
            return livePost;
          }
          return jsonResponse({ run_id: sharedRunId, job_type: "bacnet_discovery", status: "queued" });
        }
        if (url.endsWith(`/api/v1/runs/${sharedRunId}/events`)) {
          streamRequests += 1;
          return runStream.response;
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          statusRequests += 1;
          return jsonResponse(terminal);
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) return jsonResponse({ ...resultsPayload, run_id: sharedRunId, devices: [] });
        if (url.includes(`/api/v1/discovery/runs/${sharedRunId}/points`)) {
          if (!liveSubmissionStarted) {
            previewPointsSignal = init?.signal ?? null;
            return new Promise<Response>(() => {});
          }
          if (!liveResponseResolved) {
            throw new Error("Live BACnet points were requested before the submission response resolved.");
          }
          livePointsRequests += 1;
          return jsonResponse({ run_id: sharedRunId, points: [{ point_name: "live-point" }], total: 1, has_more: false, next_cursor: null });
        }
        if (url.includes(`/api/v1/discovery/runs/${sharedRunId}/comparison?`)) {
          if (!liveSubmissionStarted) {
            previewComparisonSignal = init?.signal ?? null;
            return new Promise<Response>(() => {});
          }
          if (!liveResponseResolved) {
            throw new Error("Live BACnet comparison was requested before the submission response resolved.");
          }
          liveComparisonRequests += 1;
          return jsonResponse({ compatible: true, additions: [], removals: [], changes: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("bacnet-discovery-sct", "/?compare=prior-run", [{ ...previewAuthorization, preview_run_id: sharedRunId }]);
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await waitFor(() => expect(previewPointsSignal).not.toBeNull());
    await waitFor(() => expect(previewComparisonSignal).not.toBeNull());
    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", { name: /Sealed preview authorization/i });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    await waitFor(() => expect(liveSubmissionStarted).toBe(true));
    await waitFor(() => expect(previewPointsSignal?.aborted).toBe(true));
    await waitFor(() => expect(previewComparisonSignal?.aborted).toBe(true));
    expect(livePointsRequests).toBe(0);
    expect(liveComparisonRequests).toBe(0);
    expect(statusRequests).toBe(statusRequestsAtLivePost);
    expect(streamRequests).toBe(streamRequestsAtLivePost);
    liveResponseResolved = true;
    resolveLivePost(jsonResponse({ run_id: sharedRunId, job_type: "bacnet_discovery", status: "queued" }));
    await waitFor(() => expect(livePointsRequests).toBeGreaterThan(0));
    await waitFor(() => expect(liveComparisonRequests).toBeGreaterThan(0));
    expect(statusRequests).toBeGreaterThan(statusRequestsAtLivePost);
    expect(streamRequests).toBeGreaterThan(streamRequestsAtLivePost);
    expect(screen.queryByText("preview-point")).not.toBeInTheDocument();
    expect(await screen.findByText("live-point")).toBeInTheDocument();
  });

  it("keeps a failed same-ID BACnet live submission fenced from terminal preview evidence", async () => {
    const sharedRunId = "run-bacnet-rejected-live";
    let statusRequests = 0;
    let resultsRequests = 0;
    let streamRequests = 0;
    let statusRequestsAtLivePost = 0;
    let resultsRequestsAtLivePost = 0;
    let streamRequestsAtLivePost = 0;
    const previewStream = controlledSseStream();
    const terminal = { ...terminalRun, run_id: sharedRunId, job_type: "bacnet_discovery" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { preview_run_id?: string };
          if (body.preview_run_id) {
            statusRequestsAtLivePost = statusRequests;
            resultsRequestsAtLivePost = resultsRequests;
            streamRequestsAtLivePost = streamRequests;
            throw new Error("live response lost");
          }
          return jsonResponse({ run_id: sharedRunId, job_type: "bacnet_discovery", status: "queued" });
        }
        if (url.endsWith(`/api/v1/runs/${sharedRunId}/events`)) {
          streamRequests += 1;
          return previewStream.response;
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          statusRequests += 1;
          return jsonResponse(terminal);
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          resultsRequests += 1;
          return jsonResponse({ ...resultsPayload, run_id: sharedRunId, devices: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("bacnet-discovery-sct", "/", [{ ...previewAuthorization, preview_run_id: sharedRunId }]);
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await waitFor(() => expect(resultsRequests).toBeGreaterThan(0));
    await screen.findAllByRole("button", { name: /Generate report from this run/i });

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", { name: /Sealed preview authorization/i });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

    expect(await screen.findByText(/Run request failed/i)).toBeInTheDocument();
    expect(statusRequests).toBe(statusRequestsAtLivePost);
    expect(resultsRequests).toBe(resultsRequestsAtLivePost);
    expect(streamRequests).toBe(streamRequestsAtLivePost);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.queryAllByRole("button", { name: /Generate report from this run/i })).toHaveLength(0);
  });

  it("releases a definitively rejected same-ID BACnet live submission for retry without reusing preview evidence", async () => {
    const sharedRunId = "run-bacnet-definitive-rejection";
    let livePosts = 0;
    let statusRequests = 0;
    const terminal = { ...terminalRun, run_id: sharedRunId, job_type: "bacnet_discovery" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { preview_run_id?: string };
          if (body.preview_run_id) {
            livePosts += 1;
            return errorResponse({ detail: "Authorization has already been consumed." }, 409);
          }
          return jsonResponse({ run_id: sharedRunId, job_type: "bacnet_discovery", status: "queued" });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          statusRequests += 1;
          return jsonResponse(terminal);
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          return jsonResponse({ ...resultsPayload, run_id: sharedRunId, devices: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("bacnet-discovery-sct", "/", [{ ...previewAuthorization, preview_run_id: sharedRunId }]);
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await screen.findAllByRole("button", { name: /Generate report from this run/i });
    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", { name: /Sealed preview authorization/i });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    const runButton = await screen.findByRole("button", { name: "Run" });
    fireEvent.click(runButton);

    await screen.findByText(/Run request failed/i);
    await waitFor(() => expect(runButton).toBeEnabled());
    expect(livePosts).toBe(1);
    expect(screen.queryAllByRole("button", { name: /Generate report from this run/i })).toHaveLength(0);
    expect(statusRequests).toBeGreaterThan(0);
    fireEvent.click(runButton);
    await waitFor(() => expect(livePosts).toBe(2));
  });

  it("discards a held authorized BACnet response after its active owner is replaced", async () => {
    const sharedRunId = "run-bacnet-late-owner";
    let resolveLivePost!: (response: Response) => void;
    const livePost = new Promise<Response>((resolve) => {
      resolveLivePost = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { preview_run_id?: string };
          if (body.preview_run_id) return livePost;
          return jsonResponse({ run_id: sharedRunId, job_type: "bacnet_discovery", status: "queued" });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          return jsonResponse({ ...terminalRun, run_id: sharedRunId, job_type: "bacnet_discovery" });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          return jsonResponse({ ...resultsPayload, run_id: sharedRunId, devices: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const view = renderModule("bacnet-discovery-sct", "/", [{ ...previewAuthorization, preview_run_id: sharedRunId }]);
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await screen.findAllByRole("button", { name: /Generate report from this run/i });
    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", { name: /Sealed preview authorization/i });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        <SessionProvider>
          <MemoryRouter initialEntries={["/"]}>
            <LocationProbe />
            <ModulePage moduleRoute="mqtt-discovery-sct" />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>,
    );
    await screen.findByRole("heading", { name: "MQTT Discovery" });
    await act(async () => {
      resolveLivePost(jsonResponse({ run_id: "run-bacnet-foreign", job_type: "bacnet_discovery", status: "queued" }));
    });
    expect(screen.queryByText(/run-bacnet-foreign/)).not.toBeInTheDocument();
  });

  it("fences a delayed preview property mutation when the same run ID enters a new epoch", async () => {
    const sharedRunId = "run-bacnet-property-reused";
    let liveAccepted = false;
    let propertyPosts = 0;
    let propertyAuthorizationSignal: AbortSignal | null = null;
    let resolveStaleProperty!: (response: Response) => void;
    const staleProperty = new Promise<Response>((resolve) => {
      resolveStaleProperty = resolve;
    });
    const previewDevice = {
      name: "Preview controller",
      address: "10.0.0.11",
      vendor: "Preview vendor",
      attributes: { device_instance: 1101, ip_address: "10.0.0.11" },
    };
    const liveDevice = {
      name: "Live controller",
      address: "10.0.0.12",
      vendor: "Live vendor",
      attributes: { device_instance: 1201, ip_address: "10.0.0.12" },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { parameters: { dry_run?: boolean } };
          liveAccepted ||= !body.parameters.dry_run;
          return jsonResponse({ run_id: sharedRunId, job_type: "bacnet_discovery", status: "queued" });
        }
        if (url.endsWith("/api/v1/discovery/bacnet/property-runs") && init?.method === "POST") {
          propertyPosts += 1;
          return propertyPosts === 1
            ? jsonResponse({
                run_id: "preview-property-child",
                job_type: "bacnet_property",
                status: "queued",
              })
            : staleProperty;
        }
        if (url.includes("/api/v1/discovery/scan-authorizations?")) {
          if (url.includes("preview_run_id=preview-property-child")) {
            propertyAuthorizationSignal = init?.signal ?? null;
            return new Promise<Response>(() => {});
          }
          return jsonResponse([{ ...previewAuthorization, preview_run_id: sharedRunId }]);
        }
        if (url.endsWith("/api/v1/discovery/runs/preview-property-child")) {
          return jsonResponse({
            ...terminalRun,
            run_id: "preview-property-child",
            job_type: "bacnet_property",
            result_summary: {},
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          return jsonResponse({
            ...terminalRun,
            run_id: sharedRunId,
            job_type: "bacnet_discovery",
            parameters: {
              scan_contract_v1: { bacnet: { authorized_property_ceiling: ["object_name"] } },
            },
            result_summary: {},
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          return jsonResponse({
            ...resultsPayload,
            run_id: sharedRunId,
            job_type: "bacnet_discovery",
            result_summary: {},
            discovered_assets: [],
            devices: [liveAccepted ? liveDevice : previewDevice],
            points: [],
            topics: [],
          });
        }
        if (url.includes(`/api/v1/discovery/runs/${sharedRunId}/points`)) {
          return jsonResponse({ run_id: sharedRunId, points: [], total: 0, has_more: false, next_cursor: null });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const { queryClient } = renderModule(
      "bacnet-discovery-sct",
      "/",
      [{ ...previewAuthorization, preview_run_id: sharedRunId }],
      false,
    );
    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    fireEvent.click(await screen.findByRole("button", { name: "View" }));
    const detail = await screen.findByRole("dialog", { name: "Result detail" });
    fireEvent.click(within(detail).getByLabelText("object_name"));
    fireEvent.click(within(detail).getByRole("button", { name: "Read more properties" }));
    await waitFor(() => expect(within(detail).getByText(/Preview: preview-property-child/i)).toBeInTheDocument());
    await waitFor(() => expect(propertyAuthorizationSignal).not.toBeNull());
    fireEvent.click(within(detail).getByRole("button", { name: "Read more properties" }));

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", { name: /Sealed preview authorization/i });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect((await screen.findAllByText("Live controller")).length).toBeGreaterThan(0);
    await waitFor(() => expect(propertyAuthorizationSignal?.aborted).toBe(true));
    expect(screen.queryByRole("dialog", { name: "Result detail" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "View" }));
    const liveDetail = await screen.findByRole("dialog", { name: "Result detail" });
    expect(within(liveDetail).getByRole("button", { name: "Read more properties" })).toBeEnabled();

    await act(async () => {
      resolveStaleProperty(
        jsonResponse({ run_id: "preview-property-child", job_type: "bacnet_property", status: "queued" }),
      );
    });

    await waitFor(() =>
      expect(
        queryClient.getQueryCache().findAll({
          predicate: (query) =>
            query.queryKey.includes("bacnet-property-run") && query.queryKey.includes(sharedRunId),
        }),
      ).toHaveLength(0),
    );
    expect(screen.queryByText(/Property preview preview-property-child was created/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Preview controller")).not.toBeInTheDocument();
  });

  // Drives a BACnet discovery run to a terminal, succeeded state whose results
  // carry result_summary.backend, then returns once the live table is showing.
  async function runBacnetWithBackend(backend: string) {
    const bacnetResults = {
      run_id: "run-bacnet-1",
      job_type: "bacnet_discovery",
      status: "succeeded",
      result_summary: { device_count: 1, point_count: 0, backend },
      discovered_assets: [],
      devices: [
        {
          name: "Acme Controls",
          address: "10.0.0.5",
          vendor: "Acme",
          attributes: { device_instance: 1001 },
        },
      ],
      points: [],
      topics: [],
    };
    const bacnetTerminalRun = {
      run_id: "run-bacnet-1",
      job_type: "bacnet_discovery",
      status: "succeeded",
      stage: "discovery",
      progress_percent: 100,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:05:00Z",
      project_id: "demo-project",
      site_id: "demo-site",
      parameters: {},
      result_summary: { device_count: 1, backend },
      error_message: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          return jsonResponse({
            run_id: "run-bacnet-1",
            job_type: "bacnet_discovery",
            status: "queued",
            message: "BACnet discovery accepted.",
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-1/results")) {
          return jsonResponse(bacnetResults);
        }
        if (url.includes("/api/v1/discovery/runs/run-bacnet-1/points")) {
          return jsonResponse({
            run_id: "run-bacnet-1",
            job_type: "bacnet_discovery",
            status: "succeeded",
            points: [
              {
                device_ref: "device-1001",
                point_id: "analog-input,1",
                point_name: "Supply air temperature",
                observed_value: { value: 21.5 },
                units: "degreesCelsius",
                attributes: { device_instance: 1001, object_type: "analog-input" },
              },
            ],
            total: 1,
            next_cursor: null,
            has_more: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-1")) {
          return jsonResponse(bacnetTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("bacnet-discovery-sct");

    fireEvent.click(await screen.findByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);
    await waitFor(() => expect(screen.getByText(/Run ID: run-bacnet-1/i)).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const authorization = await screen.findByRole("combobox", {
      name: /Sealed preview authorization/i,
    });
    await waitFor(() => expect(authorization).toBeEnabled());
    fireEvent.change(authorization, { target: { value: previewAuthorization.authorization_id } });
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // The live table renders (Acme Controls only exists in the results payload).
    expect((await screen.findAllByText("Acme Controls")).length).toBeGreaterThan(0);
  }

  it("shows a prominent SIMULATED warning for a simulated backend", async () => {
    await runBacnetWithBackend("simulated");
    const warning = await screen.findByText(/SIMULATED — demo data, not a real BACnet scan\./i);
    expect(warning).toBeInTheDocument();
    // Honesty-critical: it is an assertive alert, styled distinctly (not the
    // neutral amber note), so simulated data cannot pass for a real scan.
    expect(warning).toHaveAttribute("role", "alert");
    expect(warning).toHaveClass("warning");
    expect(screen.queryByText(/Live bacpypes3 scan\./i)).not.toBeInTheDocument();
  });

  it("shows a subtle Live confirmation for a real bacpypes3 backend", async () => {
    await runBacnetWithBackend("bacpypes3");
    expect(await screen.findByText(/Live bacpypes3 scan\./i)).toBeInTheDocument();
    // The alarming simulated warning must NOT appear for a real scan.
    expect(
      screen.queryByText(/SIMULATED — demo data, not a real BACnet scan\./i),
    ).not.toBeInTheDocument();
  });

  // Lives here rather than with the label tests because the results table only
  // exists once a real run has produced rows — there is no sample table to read
  // the columns off any more.
  it("shows IP Address and Network Number columns on the live BACnet results table", async () => {
    await runBacnetWithBackend("bacpypes3");
    expect(await screen.findByRole("columnheader", { name: "IP Address" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Network Number" })).toBeInTheDocument();
  });

  it("renders the persisted BACnet point schema in Points / Live Data", async () => {
    await runBacnetWithBackend("bacpypes3");
    expect(await screen.findByRole("columnheader", { name: "Units" })).toBeInTheDocument();
    const pointRow = screen.getByText("Supply air temperature").closest("tr") as HTMLTableRowElement;
    expect(within(pointRow).getByText("1001")).toBeInTheDocument();
    expect(within(pointRow).getByText("degreesCelsius")).toBeInTheDocument();
    expect(within(pointRow).getByText("21.5")).toBeInTheDocument();
    expect(within(pointRow).getByText("Read")).toBeInTheDocument();
  });
});

describe("ModulePage reports wiring", () => {
  // Mirrors the API projection: created_at + source_run_ids come back on every
  // report (GET /reports), which is what the Generated / Source runs columns read.
  const reportsPayload = {
    reports: [
      {
        report_id: "rep-1",
        report_type: "issue_report",
        output_format: "xlsx",
        status: "succeeded",
        file_name: "issue_report.xlsx",
        created_at: "2026-07-15T10:00:00Z",
        source_run_ids: ["run-1"],
      },
      {
        report_id: "rep-2",
        report_type: "evidence_pack",
        output_format: "docx",
        status: "queued",
        file_name: "evidence_pack.docx",
        created_at: "2026-07-15T11:30:00Z",
        source_run_ids: [],
      },
      // A SECOND succeeded report, deliberately not first in the list: the
      // per-row Download tests drive this one, so a row map that ignores its own
      // row and re-downloads liveReports[0] fails instead of passing by accident.
      {
        report_id: "rep-3",
        report_type: "evidence_pack",
        output_format: "zip",
        status: "succeeded",
        file_name: "handover_pack.zip",
        created_at: "2026-07-15T12:45:00Z",
        source_run_ids: ["run-2", "run-3"],
      },
    ],
  };

  // downloadFile() reads .blob() and the Content-Disposition header; the file's
  // jsonResponse helper models neither. Same hand-rolled-Response style.
  function blobResponse(filename: string): Response {
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      blob: async () => new Blob(["x"]),
      headers: {
        get: (name: string) =>
          name.toLowerCase() === "content-disposition"
            ? `attachment; filename="${filename}"`
            : null,
      },
    } as unknown as Response;
  }

  beforeEach(() => {
    // jsdom implements no object-URL APIs; triggerBlobDownload uses them.
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("keeps report exports selection-scoped when no reports are available", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Export selected" })).toBeDisabled();
    });
    expect(
      screen.queryByRole("button", { name: "Generate Excel Report" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Generate Word Report" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/Every report generated here is stored against its source run/),
    ).toBeInTheDocument();
  });

  it("lists generated reports with per-report selection and an Export selected action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse(reportsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    // Any report can be selected for deletion. Export still enables only when
    // the selection contains a succeeded report with downloadable bytes.
    const succeededCheckbox = await screen.findByLabelText(/Select report issue_report\.xlsx/i);
    const queuedCheckbox = screen.getByLabelText(/Select report evidence_pack\.docx/i);
    expect(queuedCheckbox).toBeEnabled();

    const exportSelected = screen.getByRole("button", { name: "Export selected" });
    expect(exportSelected).toBeDisabled();

    fireEvent.click(queuedCheckbox);
    expect(exportSelected).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeEnabled();

    fireEvent.click(succeededCheckbox);
    await waitFor(() => expect(exportSelected).toBeEnabled());
  });

  function stubReports(onDownload?: (url: string) => void, payload: unknown = reportsPayload) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (/\/api\/v1\/reports\/[^/]+\/download$/.test(url)) {
          onDownload?.(url);
          return blobResponse("issue_report.xlsx");
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse(payload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  // A report is only traceable evidence if the operator can see WHEN it was cut
  // and WHICH runs fed it — that is the whole point for an ITP handover pack.
  it("shows a Generated timestamp and the source run ids for each report", async () => {
    stubReports();
    renderModule("reports");

    expect(await screen.findByRole("columnheader", { name: "Generated" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Source runs" })).toBeInTheDocument();

    // Assert against the app's own formatter so the check is locale/timezone
    // agnostic (same approach as RunHistoryPage.test.tsx).
    const generated = new Date("2026-07-15T10:00:00Z").toLocaleString();
    expect(screen.getByRole("cell", { name: generated })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "run-1" })).toBeInTheDocument();

    // A report scoped to no runs says so honestly rather than inventing a source.
    const queuedRow = screen.getByLabelText(/Select report evidence_pack\.docx/i).closest("tr")!;
    expect(within(queuedRow).getByRole("cell", { name: "—" })).toBeInTheDocument();
  });

  // Drives rep-3 (succeeded, but NOT the first row) so the click has to resolve
  // its own row's report id rather than falling back to the first one.
  it("downloads a single completed report from its own row", async () => {
    const downloaded: string[] = [];
    stubReports((url) => downloaded.push(url));
    renderModule("reports");

    const row = (await screen.findByLabelText(/Select report handover_pack\.zip/i)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Download" }));

    await waitFor(() => expect(downloaded).toHaveLength(1));
    expect(downloaded[0]).toMatch(/\/api\/v1\/reports\/rep-3\/download$/);
    // The blob actually reached the browser's download path.
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  // Records every reports fetch so the export tests can assert exactly which
  // endpoint (bundle zip vs per-report download) each gesture hit.
  function stubReportsExport(hits: { download: string[]; export: string[] }) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports/export")) {
          // The selected ids POST in the JSON body now (not the query string),
          // so record the body — that is what carries the selection.
          hits.export.push(String(init?.body ?? ""));
          return blobResponse("reports_export.zip");
        }
        if (/\/api\/v1\/reports\/[^/]+\/download$/.test(url)) {
          hits.download.push(url);
          return blobResponse("issue_report.xlsx");
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse(reportsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  // Field bug (2026-07-20 item 13): a per-file download loop tripped the
  // browser's per-gesture throttle and kept only the last file. Several ticked
  // rows must now be ONE bundle request, not one download each.
  it("bundles multiple selected reports into a single export zip request", async () => {
    const hits = { download: [] as string[], export: [] as string[] };
    stubReportsExport(hits);
    renderModule("reports");

    fireEvent.click(await screen.findByLabelText(/Select report issue_report\.xlsx/i));
    fireEvent.click(screen.getByLabelText(/Select report handover_pack\.zip/i));
    fireEvent.click(screen.getByRole("button", { name: "Export selected" }));

    await waitFor(() => expect(hits.export).toHaveLength(1));
    // One request whose JSON body carries every selected id, and zero per-report
    // downloads.
    const body = JSON.parse(hits.export[0]) as { report_ids: string[] };
    expect(body.report_ids).toHaveLength(2);
    expect(body.report_ids).toContain("rep-1");
    expect(body.report_ids).toContain("rep-3");
    expect(hits.download).toHaveLength(0);
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  // Exactly one ticked report keeps the direct per-report download — a zip of
  // one is needless (their words: "a zip for multiples, direct for one is fine").
  it("downloads a single selected report directly, not through the export zip", async () => {
    const hits = { download: [] as string[], export: [] as string[] };
    stubReportsExport(hits);
    renderModule("reports");

    fireEvent.click(await screen.findByLabelText(/Select report handover_pack\.zip/i));
    fireEvent.click(screen.getByRole("button", { name: "Export selected" }));

    await waitFor(() => expect(hits.download).toHaveLength(1));
    expect(hits.download[0]).toMatch(/\/api\/v1\/reports\/rep-3\/download$/);
    expect(hits.export).toHaveLength(0);
  });

  // Only a succeeded report has real bytes behind it; offering a download for a
  // queued one would hand the operator an error, not a file.
  it("disables the row Download for a report that has not completed", async () => {
    stubReports();
    renderModule("reports");

    const queuedRow = (await screen.findByLabelText(/Select report evidence_pack\.docx/i)).closest(
      "tr",
    )!;
    expect(within(queuedRow).getByRole("button", { name: "Download" })).toBeDisabled();

    const succeededRow = screen.getByLabelText(/Select report issue_report\.xlsx/i).closest("tr")!;
    expect(within(succeededRow).getByRole("button", { name: "Download" })).toBeEnabled();
  });

  function stubReportDeletion(
    deletionBodies: Array<{ report_ids: string[] }>,
    options: { failReconciliation?: boolean } = {},
  ) {
    const deletedReportIds = new Set<string>();
    let deletionCompleted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/reports/delete") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { report_ids: string[] };
          deletionBodies.push(body);
          body.report_ids.forEach((reportId) => deletedReportIds.add(reportId));
          deletionCompleted = true;
          return jsonResponse({
            deleted_report_ids: body.report_ids,
            deleted_count: body.report_ids.length,
            artifact_cleanup_warnings: [],
          });
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          if (deletionCompleted && options.failReconciliation) {
            throw new Error("Report list reconciliation failed.");
          }
          return jsonResponse({
            reports: reportsPayload.reports.filter(
              (report) => !deletedReportIds.has(report.report_id),
            ),
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("confirms and deletes one report from its own row", async () => {
    const deletionBodies: Array<{ report_ids: string[] }> = [];
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    stubReportDeletion(deletionBodies);
    renderModule("reports");

    fireEvent.click(
      await screen.findByRole("button", { name: "Delete report evidence_pack.docx" }),
    );

    await waitFor(() => expect(deletionBodies).toEqual([{ report_ids: ["rep-2"] }]));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("Source runs remain intact"));
    expect(await screen.findByText("Deleted 1 report.")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete report handover_pack.zip" })).toHaveFocus(),
    );
  });

  it("keeps a successfully deleted row out of the cache when reconciliation fails", async () => {
    const deletionBodies: Array<{ report_ids: string[] }> = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    stubReportDeletion(deletionBodies, { failReconciliation: true });
    renderModule("reports");

    fireEvent.click(
      await screen.findByRole("button", { name: "Delete report evidence_pack.docx" }),
    );

    expect(await screen.findByText("Deleted 1 report.")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Delete report evidence_pack.docx" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Delete report issue_report.xlsx" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete report handover_pack.zip" })).toBeEnabled();
  });

  it("focuses the previous row action when the deleted report was last", async () => {
    const deletionBodies: Array<{ report_ids: string[] }> = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    stubReportDeletion(deletionBodies);
    renderModule("reports");

    fireEvent.click(await screen.findByRole("button", { name: "Delete report handover_pack.zip" }));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Delete report evidence_pack.docx" }),
      ).toHaveFocus(),
    );
  });

  it("deletes queued and completed reports together and clears their selection", async () => {
    const deletionBodies: Array<{ report_ids: string[] }> = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    stubReportDeletion(deletionBodies);
    renderModule("reports");

    const completed = await screen.findByLabelText(/Select report issue_report\.xlsx/i);
    const queued = screen.getByLabelText(/Select report evidence_pack\.docx/i);
    fireEvent.click(completed);
    fireEvent.click(queued);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    await waitFor(() =>
      expect(deletionBodies).toEqual([{ report_ids: expect.arrayContaining(["rep-1", "rep-2"]) }]),
    );
    expect(deletionBodies[0].report_ids).toHaveLength(2);
    await waitFor(() => {
      expect(screen.queryByLabelText(/Select report issue_report\.xlsx/i)).not.toBeInTheDocument();
      expect(screen.queryByLabelText(/Select report evidence_pack\.docx/i)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Delete selected" })).toBeDisabled();
      expect(screen.getByRole("heading", { name: "Generated Reports" })).toHaveFocus();
    });
  });

  it("hides report deletion controls from a viewer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({ username: "viewer-1", role: "viewer", source: "user_key" });
        }
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.split("?")[0].endsWith("/api/v1/reports")) return jsonResponse(reportsPayload);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("reports");

    expect(await screen.findByText("issue_report.xlsx")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete selected" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /Delete report / })).not.toBeInTheDocument();
  });

  // End-state guard, NOT a guard on the fixture itself: item 8 already removed
  // the `workspace?.rows` fallback, so this passes even with the fabricated rows
  // present (verified by mutation). It earns its place by pinning the OUTCOME —
  // no invented report reaches the reports page by ANY route, including a
  // re-introduced fallback. operatorData.test.ts pins the source side: the
  // fixture rows/issues fields are deleted from moduleWorkspaces entirely.
  it("shows an honest empty state, with no fabricated report rows, when no report exists", async () => {
    stubReports(undefined, { reports: [] });
    renderModule("reports");

    expect(await screen.findByText("No reports yet")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Generate a scoped report from a completed discovery or validation run; it will appear here for selection and export.",
      ),
    ).toBeInTheDocument();
    for (const fabricated of [
      "Excel issue report",
      "Word handover report",
      "Blocked report",
      "commissioning_handover.docx",
      "Awaiting validation",
    ]) {
      expect(screen.queryByText(fabricated)).toBeNull();
    }
  });

  // An older backend (or a stale cached payload) carries neither new field; the
  // row must still render rather than throwing on .join of undefined.
  it("renders a report that carries neither created_at nor source_run_ids", async () => {
    stubReports(undefined, {
      reports: [
        {
          report_id: "rep-legacy",
          report_type: "issue_report",
          output_format: "xlsx",
          status: "succeeded",
          file_name: "legacy.xlsx",
        },
      ],
    });
    renderModule("reports");

    const row = (await screen.findByLabelText(/Select report legacy\.xlsx/i)).closest("tr")!;
    // Both new cells degrade to the em-dash rather than crashing the table.
    expect(within(row).getAllByRole("cell", { name: "—" })).toHaveLength(2);
    expect(within(row).getByRole("button", { name: "Download" })).toBeEnabled();
  });
});

describe("ModulePage labels and templates", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  function stubBasic(reports: unknown[] = []) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse({ reports });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("renames the discovery run action from Queue to Run", async () => {
    stubBasic();
    renderModule("ip-scanner-sct");
    expect(await screen.findByText("Run IP Discovery")).toBeInTheDocument();
  });

  it("keeps Reports as a generated-artifact list without generic report controls", async () => {
    stubBasic([
      {
        created_at: "2026-08-07T13:20:00Z",
        file_name: "handover.pdf",
        output_format: "pdf",
        report_id: "report-1",
        report_type: "udmi_validation",
        source_run_ids: ["run-1"],
        status: "succeeded",
        udmi_report_variant: "client",
      },
    ]);
    renderModule("reports");
    expect(await screen.findByRole("heading", { name: "Generated Reports" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Generate Excel Report/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Generate Word Report/i })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(
        document.querySelector('[aria-label="Generated report list"] > table.report-list-table'),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Generate a scoped report from a completed discovery or validation run\./i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/format actions above/i)).not.toBeInTheDocument();
    expect(document.querySelector(".inspector")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Register Import" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Run Controls" })).not.toBeInTheDocument();
  });

  it("drops the duplicate all-templates section but keeps template downloads in Register Import (2026-07-20 walkthrough ITEM-3)", async () => {
    stubBasic();
    renderModule("data-validation");
    // The duplicate "Import Templates for This Page" section is removed.
    expect(await screen.findByText("Register Import")).toBeInTheDocument();
    expect(screen.queryByText("Import Templates for This Page")).not.toBeInTheDocument();
    // Templates remain downloadable from the Default import template card inside
    // Register Import — pick the import profile, then XLSX or CSV.
    expect(screen.getByText("Default import template")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download XLSX" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
  });

  // The module hero renders `workspace?.title ?? module.title`, so the
  // operatorData workspace title shadows the moduleData one on every head that
  // has a workspace — i.e. all five. These assert the string an operator
  // actually reads, whichever layer supplies it.
  it("titles the ip-scanner-sct hero 'IP Discovery', matching its menu entry", async () => {
    stubBasic();
    renderModule("ip-scanner-sct");
    expect(
      await screen.findByRole("heading", { level: 2, name: "IP Discovery" }),
    ).toBeInTheDocument();
  });

  it("titles the bacnet-discovery hero 'BACnet Discovery'", async () => {
    stubBasic();
    renderModule("bacnet-discovery-sct");
    expect(
      await screen.findByRole("heading", { level: 2, name: "BACnet Discovery" }),
    ).toBeInTheDocument();
  });
});

describe("ModulePage UDMI workbench live results", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const udmiAccepted = {
    run_id: "run-udmi-1",
    job_type: "udmi_validation",
    status: "queued",
    message: "UDMI validation accepted.",
  };

  const udmiTerminalRun = {
    run_id: "run-udmi-1",
    job_type: "udmi_validation",
    status: "succeeded",
    stage: "udmi_fixture_validation_complete",
    progress_percent: 100,
    created_at: "2026-07-09T09:00:00Z",
    updated_at: "2026-07-09T09:05:00Z",
    project_id: "demo-project",
    site_id: "demo-site",
    parameters: {},
    result_summary: {
      expected_devices: 1,
      publishing_seen: 1,
      not_publishing: 0,
      issue_count: 1,
      message_count: 3,
      source: "schedule_payload_inputs",
      payload_view_source: "direct_inputs",
      capture_mode: "bounded",
      capture_window_seconds: 120,
      // False = genuinely bounded; true renders the inline-cap wording
      // ("capped at N s (indefinite requested; inline run)") instead.
      indefinite_bounded_inline: false,
      payload_views: [
        {
          asset_id: "EM-1",
          payload_types: [
            {
              payload_type: "pointset",
              expected: {
                timestamp: "<RFC 3339 timestamp>",
                version: "1.5.2",
                points: {
                  energy_sensor: { present_value: "<device-reported value>" },
                  // Expected-only point: the device published a near-identical
                  // (typo'd) name, so this spelling has no observed counterpart —
                  // highlighted amber on the expected side (ISSUE-8).
                  supply_temp_sensor: { present_value: "<device-reported value>" },
                },
              },
              observed: {
                version: "1.4.0",
                points: {
                  energy_sensor: { present_value: 12.5 },
                  // Observed-only point (the typo) — highlighted red on the
                  // observed side. Values (present_value/version) are never marked.
                  suply_temp_sensor: { present_value: 21.4 },
                },
              },
              observed_present: true,
            },
            {
              payload_type: "metadata",
              expected: {
                timestamp: "<RFC 3339 timestamp>",
                version: "1.5.2",
                pointset: { points: { energy_sensor: { units: "kwh" } } },
              },
              observed: {
                version: "1.5.2",
                pointset: { points: { energy_sensor: { units: "kilowatt_hours" } } },
              },
              observed_present: true,
            },
          ],
        },
      ],
    },
    error_message: null,
  };

  const udmiIssuesPayload = {
    run_id: "run-udmi-1",
    issues: [
      {
        issue_id: "UDMI-PS-001",
        asset_id: "EM-1",
        issue_type: "pointset_validation",
        severity: "critical",
        description: "Expected schema version does not match the pointset payload version.",
        point_name: null,
        expected_value: "1.5.2",
        observed_value: "1.4.0",
        suggested_action:
          "Align the register's Expected schema version with the device's UDMI version.",
        status_detail: null,
        raw_evidence_uri: "runtime://udmi-validation/review-payloads",
      },
    ],
  };

  it("renders persisted provisional metrics and payload rows while capture is running", async () => {
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 0,
    };
    const runningRun = {
      ...udmiTerminalRun,
      status: "running",
      stage: "capturing_live_mqtt",
      progress_percent: 25,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        provisional: true,
        payload_view_source: "live_capture",
        payload_views: [
          {
            asset_id: "ACTIVE-1",
            system: "BMS",
            payload_types: [
              {
                payload_type: "state",
                expected: { version: "1.5.2" },
                observed: { version: "1.5.2", timestamp: "2026-07-09T09:01:00Z" },
                observed_present: true,
              },
            ],
          },
        ],
        validation_summary_v1: {
          schema_version: "1.0",
          asset_metrics: {
            expected: 2,
            observed: 1,
            not_observed: 1,
            with_issues: 0,
            successfully_validated: 0,
          },
          payload_metrics: {
            expected: 2,
            received: 1,
            with_issues: 0,
            successfully_validated: 1,
          },
          fault_metrics: zeroFaults,
          issue_metrics: { blocking: 0, warning: 0 },
          system_metrics: [],
          asset_results: [
            {
              asset_id: "ACTIVE-1",
              system: "BMS",
              observed: true,
              expected_payloads: 1,
              received_payloads: 1,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: true,
              successfully_validated: true,
              issue_count: 0,
              blocking_issue_count: 0,
              last_observed_at: "2026-07-09T09:01:00Z",
              payload_results: [
                {
                  payload_type: "state",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/active-1/state",
                  received_at: "2026-07-09T09:01:00Z",
                },
              ],
            },
          ],
          fault_rows: [],
        },
      },
    };
    let issuesFetches = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: [
              {
                run_id: "run-udmi-1",
                job_type: "udmi_validation",
                status: "running",
                stage: "capturing_live_mqtt",
                progress_percent: 25,
                created_at: runningRun.created_at,
                updated_at: runningRun.updated_at,
                edge_id: null,
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          issuesFetches += 1;
          return jsonResponse({ run_id: "run-udmi-1", issues: [] });
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(runningRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    expect(
      await screen.findByRole("heading", { name: "Provisional validation summary" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Provisional results below update as payloads arrive/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Provisional live validation results/i)).toBeInTheDocument();
    const liveConsole = screen.getByRole("region", { name: "Live run console" });
    expect(within(liveConsole).getByText("1 of 2 expected")).toBeInTheDocument();
    expect(within(liveConsole).getAllByText("Waiting for evidence")).toHaveLength(3);
    expect(screen.getAllByText("ACTIVE-1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Verdict pending").length).toBeGreaterThan(0);
    expect(screen.getByText(/Unexpected-device measurement was unavailable/i)).toBeInTheDocument();
    expect(issuesFetches).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Download raw JSON" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Generate report from this run" }),
    ).not.toBeInTheDocument();
  });

  it("keeps wrong-topic evidence separate from payload presence and validation", async () => {
    const wrongTopicRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        asset_topic_discovery: {
          enabled: true,
          scope: null,
          scope_source: "unavailable",
          scope_error: null,
          topic_limit_per_asset: 10,
          capture_complete: true,
          capture_status: "completed",
          status_counts: { scope_unavailable: 1 },
          asset_results: [
            {
              asset_id: "EM-1",
              system: "BMS",
              expected_topic_root: "site/registered/EM-1",
              expected_topics: ["site/registered/EM-1/metadata", "site/registered/EM-1/pointset"],
              observed_expected_topics: [],
              observed_alternate_topics: [],
              matched_message_count: 0,
              topic_limit_reached: false,
              status: "scope_unavailable",
            },
          ],
        },
        validation_summary_v1: {
          schema_version: "1.1",
          asset_metrics: {
            expected: 1,
            observed: 1,
            not_observed: 0,
            with_issues: 1,
            successfully_validated: 0,
            unexpected: 0,
            wrong_topic: 1,
          },
          payload_metrics: {
            expected: 2,
            received: 2,
            not_received: 0,
            with_issues: 1,
            successfully_validated: 1,
          },
          fault_metrics: {
            payload_formatting_issues: 0,
            missing_points: 0,
            point_naming_issues: 0,
            additional_points: 0,
            stale_or_cadence: 0,
            other_issues: 1,
          },
          issue_metrics: { blocking: 1, warning: 0 },
          system_metrics: [],
          asset_results: [
            {
              asset_id: "EM-1",
              system: "BMS",
              observed: true,
              expected_payloads: 2,
              received_payloads: 2,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: false,
              successfully_validated: false,
              issue_count: 1,
              blocking_issue_count: 1,
              last_observed_at: "2026-07-09T09:04:00Z",
              payload_results: [
                {
                  payload_type: "pointset",
                  expected: true,
                  received: true,
                  has_issues: true,
                  blocking_issue_count: 1,
                  successfully_validated: false,
                  topic: "site/wrong/EM-1/pointset",
                  received_at: "2026-07-09T09:04:00Z",
                },
                {
                  payload_type: "metadata",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/registered/EM-1/metadata",
                  received_at: "2026-07-09T09:03:00Z",
                },
              ],
            },
          ],
          fault_rows: [
            {
              issue_id: "UDMI-TOPIC-001",
              asset_id: "EM-1",
              system: "BMS",
              payload_type: "pointset",
              category: "topic_mismatch",
              severity: "high",
              description: "Registered asset published under a different topic root.",
              point_name: null,
              expected_value: "site/registered/EM-1/pointset",
              observed_value: "site/wrong/EM-1/pointset",
              suggested_action: "Correct the publisher topic or update the register.",
              raw_evidence_uri: null,
            },
          ],
          unexpected_devices: [],
          unexpected_devices_measured: true,
          unexpected_devices_measurement_scope: "site/#",
          wrong_topic_assets: [
            {
              asset_id: "EM-1",
              system: "BMS",
              expected_topic_root: "site/registered/EM-1",
              actual_topic_root: "site/wrong/EM-1",
              payloads: [
                {
                  payload_type: "pointset",
                  expected_topic: "site/registered/EM-1/pointset",
                  actual_topic: "site/wrong/EM-1/pointset",
                },
              ],
              last_seen: "2026-07-09T09:04:00Z",
            },
          ],
        },
      },
    };
    const wrongTopicIssues = {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "UDMI-TOPIC-001",
          asset_id: "EM-1",
          issue_type: "pointset_topic_mismatch",
          severity: "high",
          description: "Registered asset published under a different topic root.",
          point_name: null,
          expected_value: "site/registered/EM-1/pointset",
          observed_value: "site/wrong/EM-1/pointset",
          suggested_action: "Correct the publisher topic or update the register.",
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    };
    stubUdmiRunFetch(wrongTopicIssues, undefined, wrongTopicRun);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    const summary = (await screen.findByRole("heading", { name: "Validation summary" })).closest(
      ".udmi-summary",
    ) as HTMLElement;
    const wrongTopicMetric = within(summary)
      .getByText("Wrong-topic assets")
      .closest("div") as HTMLElement;
    expect(within(wrongTopicMetric).getByText("1")).toBeInTheDocument();
    const detail = screen
      .getByRole("heading", { name: "Registered assets on wrong topics" })
      .closest(".udmi-wrong-topic-summary") as HTMLElement;
    expect(within(detail).getByText("site/registered/EM-1")).toBeInTheDocument();
    expect(within(detail).getByText("site/wrong/EM-1")).toBeInTheDocument();
    expect(detail.querySelector(".udmi-wrong-topic-scroll")).toBeInTheDocument();

    const discovery = screen
      .getByRole("heading", { name: "Asset topic discovery" })
      .closest(".udmi-asset-topic-discovery") as HTMLElement;
    expect(within(discovery).getByText("Not available")).toBeInTheDocument();
    expect(within(discovery).getByText("Bounded scope unavailable")).toBeInTheDocument();
    expect(within(discovery).getByText("Discovery scope unavailable")).toBeInTheDocument();
    expect(within(discovery).getByText(/Payload content is not inspected/i)).toBeInTheDocument();

    const resultsTable = document.querySelector(".results-scroll table") as HTMLTableElement;
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(
      resultsTable.compareDocumentPosition(inspector) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      inspector.compareDocumentPosition(discovery) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      discovery.compareDocumentPosition(detail) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      within(resultsTable).getByRole("columnheader", { name: "Topic status" }),
    ).toBeInTheDocument();
    const pointsetRow = (await within(resultsTable).findByText("UDMI pointset")).closest(
      "tr",
    ) as HTMLTableRowElement;
    expect(within(pointsetRow).getByText("Wrong topic")).toBeInTheDocument();
    expect(within(pointsetRow).getByText("site/wrong/EM-1/pointset")).toBeInTheDocument();
    expect(within(pointsetRow).getByText("Yes")).toBeInTheDocument();
    expect(within(pointsetRow).getByText(/Non-compliant/)).toBeInTheDocument();
    const metadataRow = within(resultsTable)
      .getByText("UDMI metadata")
      .closest("tr") as HTMLTableRowElement;
    expect(within(metadataRow).getByText("Expected topic")).toBeInTheDocument();
  });

  it("keeps topic discovery opt-in and requires acknowledgement before widening to all topics", async () => {
    const postedRequest: { body: { parameters: Record<string, unknown> } | null } = { body: null };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedRequest.body = JSON.parse(String(init.body)) as {
            parameters: Record<string, unknown>;
          };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1"))
          return jsonResponse(udmiTerminalRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    expect(
      screen.queryByLabelText(/Diagnose where registered asset IDs appear/i),
    ).not.toBeInTheDocument();
    fireEvent.click(await screen.findByLabelText(/Validate against the imported MQTT register/i));
    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.click(await screen.findByLabelText(/Diagnose where registered asset IDs appear/i));

    const boundedScope = screen.getByLabelText(/Use the register's bounded topic scope/i);
    const allScope = screen.getByLabelText(/Search all authorised broker topics/i);
    expect(boundedScope).toBeChecked();
    expect(allScope).toBeDisabled();

    fireEvent.click(
      screen.getByLabelText(
        /I understand that an all-topic search can receive every broker topic/i,
      ),
    );
    expect(allScope).toBeEnabled();
    fireEvent.click(allScope);
    fireEvent.click(screen.getByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedRequest.body).not.toBeNull());
    if (!postedRequest.body) {
      throw new Error("Expected the UDMI run request to be submitted.");
    }
    expect(postedRequest.body.parameters).toMatchObject({
      topic_discovery_all_scope_confirmed: true,
      topic_discovery_enabled: true,
      topic_discovery_scope: "all",
      use_live_broker: true,
      use_register: true,
    });
  });

  it("keeps topic discovery out of manual live-payload requests", async () => {
    const postedRequest: { body: { parameters: Record<string, unknown> } | null } = { body: null };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedRequest.body = JSON.parse(String(init.body)) as {
            parameters: Record<string, unknown>;
          };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1"))
          return jsonResponse(udmiTerminalRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    expect(
      screen.queryByLabelText(/Diagnose where registered asset IDs appear/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        /Topic discovery is available for live captures that validate against the imported MQTT register/i,
      ),
    ).toBeInTheDocument();

    const runButton = screen.getByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedRequest.body).not.toBeNull());
    if (!postedRequest.body) {
      throw new Error("Expected the UDMI run request to be submitted.");
    }
    expect(postedRequest.body.parameters).toMatchObject({ use_live_broker: true });
    expect(postedRequest.body.parameters).not.toHaveProperty("topic_discovery_enabled");
    expect(postedRequest.body.parameters).not.toHaveProperty("topic_discovery_scope");
    expect(postedRequest.body.parameters).not.toHaveProperty("topic_discovery_all_scope_confirmed");
  });

  it("shows no rows until a terminal run, then real per-asset payload rows", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // Before a run there are no result rows at all — no sample preview, just
    // the honest empty state. Fabricated rows here read as real findings.
    // DEMO-00-044-BLR-2 is unique to the old sample rows (the sample *issues*
    // fallback in the inspector is a separate surface and still stands).
    expect(await screen.findByText("No validation run yet")).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    expect(screen.queryByText("DEMO-00-044-BLR-2")).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    // After the terminal run the table shows REAL per-asset payload rows —
    // the version-mismatch verdict and the run's asset id, not the sample rows.
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    // The run monitor shows the capture window the run ACTUALLY used.
    expect(screen.getByText("120 s (bounded)")).toBeInTheDocument();
    // Wait for the issues query to merge so the verdict lands on the row (it can
    // render a beat after the banner, which comes from payload views alone).
    await screen.findAllByText("Non-compliant: 1 issue (1 critical)");
    expect(screen.getAllByText("EM-1").length).toBeGreaterThan(0);
    // The single asset's summary row auto-expands (it is the selected asset), so
    // its per-payload-type rows are visible (ITEM-7 grouping).
    expect(screen.getAllByText("UDMI pointset").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Pass").length).toBeGreaterThan(0);
    // The old illustrative sample asset never appears as a live result.
    expect(screen.queryByText("DEMO-BLR-001")).not.toBeInTheDocument();

    // Expand the asset in the INSPECTOR drill-down (a separate toggle from the
    // results-table summary row, so scope the query to the inspector aside).
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(within(inspector).queryByText("Evidence outputs")).not.toBeInTheDocument();
    fireEvent.click(within(inspector).getByRole("button", { name: /EM-1.*issue/i }));
    fireEvent.click(
      screen.getAllByRole("button", { name: /Show expected vs observed payload/i })[0],
    );
    expect(screen.getByText("Expected UDMI template")).toBeInTheDocument();
    expect(
      screen.getByText(
        /expected timestamp is a schema-valid template value created when this result view was built/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/freshness checks use the observed payload timestamp/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/expected side keeps its own build value and never borrows broker data/i),
    ).toBeInTheDocument();

    // Presence diff (ISSUE-8): the expected-only point spelling shades amber on
    // the expected side, the observed-only (typo'd) spelling shades red on the
    // observed side. jsdom cannot see theme CSS, so assert on the mark classes.
    // The expected-only line names the correct spelling; the observed-only line
    // names the typo — proving each is marked on its own side.
    const expectedOnly = Array.from(document.querySelectorAll(".payload-diff-line.only-expected"));
    const observedOnly = Array.from(document.querySelectorAll(".payload-diff-line.only-observed"));
    expect(expectedOnly.some((el) => el.textContent?.includes("supply_temp_sensor"))).toBe(true);
    expect(observedOnly.some((el) => el.textContent?.includes("suply_temp_sensor"))).toBe(true);
    // A value difference (version 1.5.2 vs 1.4.0) is NEVER highlighted — expected
    // values are template sentinels; the engine's issue cards own value checks.
    expect(expectedOnly.some((el) => el.textContent?.includes("1.5.2"))).toBe(false);
    expect(observedOnly.some((el) => el.textContent?.includes("1.4.0"))).toBe(false);
    // The legend explaining the highlight is present.
    expect(screen.getByText(/Values are not compared here/i)).toBeInTheDocument();
  });

  // Shared stub for the verdict-focused tests below — the same endpoints as the
  // live-results test above, parameterised on the issues payload. An optional
  // issuesResponse factory overrides the whole issues Response (never-settling
  // or failing fetches for the verdict-gating tests).
  function stubUdmiRunFetch(
    issuesPayload: unknown,
    issuesResponse?: () => Response | Promise<Response>,
    runPayload = udmiTerminalRun,
  ) {
    const captured = { issuesRequests: 0, requests: [] as string[], runStatusRequests: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          captured.issuesRequests += 1;
          captured.requests.push("issues");
          return issuesResponse ? issuesResponse() : jsonResponse(issuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          captured.runStatusRequests += 1;
          captured.requests.push("run");
          return jsonResponse(runPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    return captured;
  }

  it("integrates the Workbench title and compact metric cards in one page header", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    const title = await screen.findByRole("heading", {
      level: 1,
      name: "UDMI Payload Workbench",
    });
    const hero = title.closest(".module-hero") as HTMLElement;
    expect(hero).toHaveClass("module-hero-workbench");
    expect(within(hero).getByText(/Inspect state, metadata, pointset/i)).toBeInTheDocument();

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await screen.findByText(/Live validation results/i);
    expect(await within(hero).findByText("Issues")).toBeInTheDocument();
  });

  it("requires matching validation issues after authoritative terminal status before settling", async () => {
    const captured = stubUdmiRunFetch({ run_id: "wrong-run", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText("Final evidence unavailable")).toBeInTheDocument();
    expect(captured.runStatusRequests).toBeGreaterThan(0);
    expect(captured.issuesRequests).toBeGreaterThan(0);
    expect(captured.requests.lastIndexOf("issues")).toBeGreaterThan(
      captured.requests.lastIndexOf("run"),
    );
  });

  it("shades live UDMI rows amber on non-compliant and green on pass (RAG)", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    // Nothing has run, so there are no rows to shade — and no sample rows
    // masquerading as results either.
    expect(await screen.findByText("No validation run yet")).toBeInTheDocument();
    expect(document.querySelector("tr.row-pass, tr.row-fail, tr.row-warn")).toBeNull();

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // Wait for the issues query to merge in (the live banner can render from
    // payload views alone, a beat before the verdict lands on the row).
    await screen.findAllByText("Non-compliant: 1 issue (1 critical)");

    // Under the RAG scheme a PUBLISHING device with a critical issue is amber
    // (row-warn), not red — red is reserved for offline / not-publishing. The
    // clean observed metadata row stays green. The payload-type text also
    // renders in the aside detail, so pick the occurrence inside a table row.
    const pointsetRow = screen
      .getAllByText("UDMI pointset")
      .map((cell) => cell.closest("tr"))
      .find((row) => row !== null);
    expect(pointsetRow).toHaveClass("row-warn");
    const metadataRow = screen
      .getAllByText("UDMI metadata")
      .map((cell) => cell.closest("tr"))
      .find((row) => row !== null);
    expect(metadataRow).toHaveClass("row-pass");
  });

  it("highlights only the units line for a unit mismatch", async () => {
    stubUdmiRunFetch({
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "UDMI-META-UNIT-1",
          asset_id: "EM-1",
          issue_type: "metadata_validation",
          severity: "medium",
          description: "The registered unit does not match the metadata payload.",
          point_name: "energy_sensor",
          match_basis: "units",
          expected_value: "kwh",
          observed_value: "kilowatt_hours",
          suggested_action: "Align the point unit.",
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await screen.findAllByText(/Non-compliant.*1 issue/i);

    const metadataRow = screen
      .getAllByText("UDMI metadata")
      .map((cell) => cell.closest("tr"))
      .find((row): row is HTMLTableRowElement => row !== null);
    expect(metadataRow).toBeDefined();
    fireEvent.click(within(metadataRow!).getByRole("button", { name: "EM-1" }));
    const inspector = document.querySelector(".inspector") as HTMLElement;
    const metadataGroup = within(inspector)
      .getByRole("heading", { name: "metadata" })
      .closest(".payload-type-group") as HTMLElement;
    fireEvent.click(
      within(metadataGroup).getByRole("button", { name: /Show expected vs observed payload/i }),
    );

    const flagged = Array.from(metadataGroup.querySelectorAll(".payload-diff-line.flagged"));
    expect(flagged).toHaveLength(2);
    expect(flagged.every((line) => line.textContent?.includes('"units"'))).toBe(true);
    expect(flagged.some((line) => line.textContent?.includes('"energy_sensor"'))).toBe(false);
  });

  it("uses listed unexpected devices when the provisional scalar is stale at zero", async () => {
    const runWithUnexpected = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        validation_summary_v1: {
          schema_version: "1.1",
          asset_metrics: {
            expected: 1,
            observed: 1,
            not_observed: 0,
            with_issues: 0,
            successfully_validated: 1,
            unexpected: 0,
          },
          payload_metrics: {
            expected: 2,
            received: 2,
            not_received: 0,
            with_issues: 0,
            successfully_validated: 2,
          },
          fault_metrics: {
            payload_formatting_issues: 0,
            missing_points: 0,
            point_naming_issues: 0,
            additional_points: 0,
            stale_or_cadence: 0,
            other_issues: 0,
          },
          issue_metrics: { blocking: 0, warning: 0 },
          system_metrics: [],
          asset_results: [
            {
              asset_id: "EM-1",
              system: "Unspecified",
              observed: true,
              expected_payloads: 2,
              received_payloads: 2,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: true,
              successfully_validated: true,
              issue_count: 0,
              blocking_issue_count: 0,
              last_observed_at: "2026-07-23T11:10:00Z",
              payload_results: [
                {
                  payload_type: "pointset",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/em-1/pointset",
                  received_at: "2026-07-23T11:10:00Z",
                },
                {
                  payload_type: "metadata",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/em-1/metadata",
                  received_at: "2026-07-23T11:10:00Z",
                },
              ],
            },
          ],
          fault_rows: [],
          unexpected_devices: [
            {
              id: "unexpected-9",
              topic_root: "site/unregistered/device-9",
              topics: ["site/unregistered/device-9/state"],
              last_seen: "2026-07-23T11:09:30Z",
            },
          ],
          unexpected_devices_measured: true,
          unexpected_devices_measurement_scope: "the MQTT capture window",
        },
      },
    };
    stubUdmiRunFetch({ run_id: "run-udmi-1", issues: [] }, undefined, runWithUnexpected);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(await screen.findByText(/plus 1 unexpected device/i)).toBeInTheDocument();
    const summaryPanel = screen
      .getByRole("heading", { name: "Validation summary" })
      .closest(".udmi-summary") as HTMLElement;
    const unexpectedMetric = within(summaryPanel)
      .getByText("Unexpected devices")
      .closest("div") as HTMLElement;
    expect(within(unexpectedMetric).getByText("1")).toBeInTheDocument();
    expect(
      within(summaryPanel).getByText(/available for the MQTT capture window/i),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Category"), {
      target: { value: "unexpected-devices" },
    });
    const resultsTable = document.querySelector(".results-scroll table") as HTMLTableElement;
    expect(
      await within(resultsTable).findByRole("button", { expanded: true, name: /device-9/i }),
    ).toBeInTheDocument();
    expect(within(resultsTable).queryByRole("button", { name: /EM-1/i })).not.toBeInTheDocument();
    const expectedMetric = within(summaryPanel)
      .getByText("Expected assets")
      .closest("div") as HTMLElement;
    expect(within(expectedMetric).getByText("0")).toBeInTheDocument();
    expect(within(unexpectedMetric).getByText("1")).toBeInTheDocument();
    expect(await screen.findByText("Observed outside the expected register")).toBeInTheDocument();
  });

  it("shows the actual issue text in the Inspector when View issues is clicked", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    // Ensure the issues query has merged before opening the detail.
    await screen.findAllByText("Non-compliant: 1 issue (1 critical)");

    // The pointset row carries the run's single critical issue. View selects and
    // focuses the persistent Inspector instead of opening a second detail view.
    fireEvent.click(screen.getByRole("button", { name: "View 1 issue" }));
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(within(inspector).getByText("UDMI-PS-001")).toBeInTheDocument();
    expect(
      within(inspector).getAllByText(
        /Expected schema version does not match the pointset payload version/i,
      ),
    ).toHaveLength(2);
    const assetToggle = within(inspector).getByRole("button", {
      expanded: true,
      name: /EM-1/i,
    });
    const assetGroup = assetToggle.closest(".asset-group");
    expect(assetGroup).toHaveClass("open");
    fireEvent.click(assetToggle);
    expect(assetGroup).not.toHaveClass("open");
    fireEvent.click(assetToggle);
    expect(assetGroup).toHaveClass("open");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps 3+ issue details in the expanded Inspector without a modal", async () => {
    const manyIssues = {
      run_id: "run-udmi-1",
      issues: [1, 2, 3].map((n) => ({
        issue_id: `UDMI-PS-00${n}`,
        asset_id: "EM-1",
        issue_type: "pointset_validation",
        severity: n === 1 ? "critical" : "medium",
        description: `Pointset problem ${n}.`,
        point_name: null,
        expected_value: null,
        observed_value: null,
        suggested_action: null,
        status_detail: null,
        raw_evidence_uri: null,
      })),
    };
    stubUdmiRunFetch(manyIssues);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    // Ensure the issues query has merged before opening the detail.
    await screen.findAllByText("Non-compliant: 3 issues (1 critical)");

    fireEvent.click(screen.getByRole("button", { name: "View 3 issues" }));
    /* Removed modal assertions retained here as historical context.
    expect(
      within(dialog).getByText("3 issues — see the issue details below the table."),
    ).toBeInTheDocument();
    // The full message text stays in the issue panel below, not the modal.
    expect(within(dialog).queryByText(/Pointset problem 1\./)).not.toBeInTheDocument();
    */
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(within(inspector).getByText("Pointset problem 1.")).toBeInTheDocument();
    expect(within(inspector).getByText("Pointset problem 2.")).toBeInTheDocument();
    expect(within(inspector).getByText("Pointset problem 3.")).toBeInTheDocument();
    expect(
      within(inspector).queryByRole("button", {
        name: /Jump to pointset expected versus observed/i,
      }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("jumps from an eight-issue payload heading to its comparison control", async () => {
    const scrollSpy = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
    let reduceMotion = false;
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: reduceMotion && query === "(prefers-reduced-motion: reduce)",
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    const longIssues = {
      run_id: "run-udmi-1",
      issues: Array.from({ length: 8 }, (_, index) => ({
        issue_id: `UDMI-PS-${index + 1}`,
        asset_id: "EM-1",
        issue_type: "pointset_validation",
        severity: index === 0 ? "critical" : "medium",
        description: `Pointset problem ${index + 1}.`,
        point_name: null,
        expected_value: null,
        observed_value: null,
        suggested_action: null,
        status_detail: null,
        raw_evidence_uri: null,
      })),
    };
    stubUdmiRunFetch(longIssues);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await screen.findAllByText("Non-compliant: 8 issues (1 critical)");
    fireEvent.click(screen.getByRole("button", { name: "View 8 issues" }));

    const inspector = document.querySelector(".inspector") as HTMLElement;
    const pointsetGroup = within(inspector)
      .getByRole("heading", { name: "pointset" })
      .closest(".payload-type-group") as HTMLElement;
    await waitFor(() => expect(pointsetGroup).toHaveFocus());
    const comparisonControl = within(pointsetGroup).getByRole("button", {
      name: /Show expected vs observed payload/i,
    });
    const jumpControl = within(pointsetGroup).getByRole("button", {
      name: "Jump to pointset expected versus observed comparison",
    });
    scrollSpy.mockClear();
    fireEvent.click(jumpControl);

    expect(comparisonControl).toHaveFocus();
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    reduceMotion = true;
    scrollSpy.mockClear();
    fireEvent.click(jumpControl);
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "center" });
    scrollSpy.mockRestore();
  });

  it("names a row's issue count on its View button and drives the inspector to those issues (ITEM-D)", async () => {
    const scrollSpy = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(prefers-reduced-motion: reduce)",
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    await screen.findAllByText("Non-compliant: 1 issue (1 critical)");

    // (C) The View affordance names the count it carries: the pointset row holds
    // the single critical issue; the clean metadata row keeps the bare label.
    const table = screen.getByRole("table");
    const pointsetView = within(table).getByRole("button", { name: "View 1 issue" });
    expect(within(table).getByRole("button", { name: "View" })).toBeInTheDocument();

    // (A) The inspector's asset group is collapsed on its own (independent of the
    // table's auto-expand), so the per-payload issue verdict is not shown yet.
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(within(inspector).queryByText(/see details below/i)).not.toBeInTheDocument();

    // Selecting the row expands its asset in the inspector — surfacing exactly the
    // issues that flagged it — and scrolls straight to that payload's group.
    scrollSpy.mockClear();
    fireEvent.click(pointsetView);
    expect(within(inspector).getByText(/see details below/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "nearest" }),
    );
    const focusedPayloadGroup = within(inspector)
      .getByRole("heading", { name: "pointset" })
      .closest(".payload-type-group");
    await waitFor(() => expect(focusedPayloadGroup).toHaveFocus());

    scrollSpy.mockRestore();
  });

  it("stamps the shared RAG verdict on the per-asset payload sections", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    // Ensure the issues query has merged before expanding the asset group.
    await screen.findAllByText("Non-compliant: 1 issue (1 critical)");

    const inspectorStamp = document.querySelector(".inspector") as HTMLElement;
    fireEvent.click(within(inspectorStamp).getByRole("button", { name: /EM-1.*issue/i }));
    // Publishing device with a critical issue → amber "NON-COMPLIANT" section.
    const nonCompliant = await screen.findByText("NON-COMPLIANT: please see details below");
    expect(nonCompliant).toHaveClass("payload-verdict", "warn");
    expect(nonCompliant.closest(".payload-type-group")).toHaveClass("section-warn");
    const pass = screen.getByText("PASS: UDMI Compliant");
    expect(pass).toHaveClass("payload-verdict", "pass");
    expect(pass.closest(".payload-type-group")).toHaveClass("section-pass");
  });

  it("flags a present-but-empty observed value as 'empty' in the issue detail (ISSUE-10)", async () => {
    const emptyValueIssue = {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "UDMI-PS-009",
          asset_id: "EM-1",
          issue_type: "pointset_validation",
          severity: "critical",
          description: "Pointset payload version is blank.",
          point_name: null,
          expected_value: "1.5.2",
          // Present but EMPTY (not null): previously rendered as a bare blank
          // ("observed " with nothing after it); now it must read "empty".
          observed_value: "",
          suggested_action: "Populate the version field.",
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    };
    stubUdmiRunFetch(emptyValueIssue);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    await screen.findAllByText("Non-compliant: 1 issue (1 critical)");

    const inspectorEmpty = document.querySelector(".inspector") as HTMLElement;
    fireEvent.click(within(inspectorEmpty).getByRole("button", { name: /EM-1.*issue/i }));
    // Structured issue card (ITEM-9): the expected/observed comparison and the
    // suggested action render as their OWN lines, not one run-on string. The
    // empty observed value is named "empty" rather than dropped or blank.
    expect((await screen.findAllByText("Expected 1.5.2, observed empty")).length).toBeGreaterThan(
      0,
    );
    expect((await screen.findAllByText("Populate the version field.")).length).toBeGreaterThan(0);
    expect(
      (await screen.findAllByText("Pointset payload version is blank.")).length,
    ).toBeGreaterThan(0);
  });

  it("shades assets not observed during the run red on a succeeded run (RAG)", async () => {
    // EM-1 publishes with one major issue → amber. EM-2 was CAPTURED (a real
    // attempt) but stayed silent: pointset + state observed_present false, an
    // engine "not_publishing" issue, and the run summary's not_publishing_devices
    // list. Every EM-2 row must read red "Offline — did not publish", even
    // though the RUN itself SUCCEEDED — the ask as field engineer experiences it.
    const silentRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        expected_devices: 2,
        publishing_seen: 1,
        not_publishing: 1,
        not_publishing_devices: ["EM-2"],
        payload_views: [
          {
            asset_id: "EM-1",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: { energy_sensor: { present_value: 12.5 } } },
                observed_present: true,
              },
            ],
          },
          {
            asset_id: "EM-2",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: null,
                observed_present: false,
              },
              {
                payload_type: "state",
                expected: { version: "1.5.2" },
                observed: null,
                observed_present: false,
              },
            ],
          },
        ],
      },
    };
    const silentIssues = {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "UDMI-PS-010",
          asset_id: "EM-1",
          issue_type: "pointset_validation",
          severity: "medium",
          description: "Units mismatch on the pointset payload.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
        {
          issue_id: "UDMI-NP-001",
          asset_id: "EM-2",
          issue_type: "not_publishing",
          severity: "high",
          description: "No UDMI messages received from this device during the capture window.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST")
          return jsonResponse(udmiAccepted);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues"))
          return jsonResponse(silentIssues);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(silentRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // EM-1 is the first (selected) asset, so it auto-expands; EM-2 is collapsed
    // under ITEM-7 grouping. Expand EM-2's summary row (scope to the table so the
    // query does not also match the inspector's EM-2 drill-down toggle).
    const resultsTable = screen.getByRole("table");
    fireEvent.click(within(resultsTable).getByRole("button", { expanded: false, name: /EM-2/ }));

    // Wait for the offline verdict to land (issues merged) on the now-visible
    // EM-2 rows.
    await screen.findAllByText("Not observed this run");

    // Every EM-2 data row (the rows carrying the offline verdict) is red offline.
    const em2Rows = screen
      .getAllByText("Not observed this run")
      .map((cell) => cell.closest("tr"))
      .filter((row): row is HTMLTableRowElement => row !== null);
    expect(em2Rows.length).toBeGreaterThan(0);
    for (const row of em2Rows) {
      expect(row).toHaveClass("row-fail");
    }
    // EM-1 is amber (publishing but non-compliant): a summary/data row reads warn.
    const em1Rows = screen
      .getAllByText("EM-1")
      .map((cell) => cell.closest("tr"))
      .filter((row): row is HTMLTableRowElement => row !== null);
    expect(em1Rows.some((row) => row.classList.contains("row-warn"))).toBe(true);

    // Selecting an EM-2 payload row replaces the inspector's EM-1 evidence with
    // EM-2 only; expanding that selected asset reveals its run-observation line.
    fireEvent.click(within(em2Rows[0]).getByRole("button", { name: /View/i }));
    const inspectorEm2 = document.querySelector(".inspector") as HTMLElement;
    const inspectorToggle = await within(inspectorEm2).findByRole("button", {
      name: /EM-2.*issue/i,
    });
    if (inspectorToggle.getAttribute("aria-expanded") !== "true") {
      fireEvent.click(inspectorToggle);
    }
    expect(within(inspectorEm2).queryByRole("button", { name: /EM-1/ })).not.toBeInTheDocument();
    expect(
      (
        await screen.findAllByText(
          "NOT OBSERVED THIS RUN: no payload arrived during the capture window",
        )
      ).length,
    ).toBeGreaterThan(0);
  });

  // Shared two-asset stub for the grouping/facet tests: EM-1 published, AHU-9
  // published, EM-2 (facet test) silent — overridable per test.
  function stubTwoAssetUdmi(
    run: unknown,
    issues: unknown,
    reportBodies?: Record<string, unknown>[],
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST")
          return jsonResponse(udmiAccepted);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) return jsonResponse(issues);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(run);
        if (
          url.split("?")[0].endsWith("/api/v1/reports") &&
          init?.method === "POST" &&
          reportBodies
        ) {
          reportBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
          return jsonResponse({
            file_name: "udmi_validation_rep-subset.pdf",
            output_format: "pdf",
            report_id: "rep-subset",
            report_type: "udmi_validation",
            status: "succeeded",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("groups results by asset: collapsed summary rows expand to per-payload rows (ITEM-7)", async () => {
    const cleanPayload = (asset: string, types: string[]) => ({
      asset_id: asset,
      payload_types: types.map((type) => ({
        payload_type: type,
        expected: { version: "1.5.2", points: {} },
        observed: { version: "1.5.2", points: {} },
        observed_present: true,
      })),
    });
    const twoAssetRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [
          cleanPayload("EM-1", ["pointset", "state"]),
          cleanPayload("AHU-9", ["pointset"]),
        ],
      },
    };
    stubTwoAssetUdmi(twoAssetRun, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    const table = screen.getByRole("table");
    // One collapsible summary row per asset.
    expect(
      await within(table).findByRole("button", { expanded: true, name: /EM-1/ }),
    ).toBeInTheDocument();
    expect(
      within(table).getByRole("button", { expanded: false, name: /AHU-9/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/across 2 expected assets/i)).toBeInTheDocument();

    // EM-1 is the first (selected) asset, so it auto-expands (2 child rows).
    // AHU-9 is collapsed until clicked.
    expect(await screen.findAllByRole("button", { name: "View" })).toHaveLength(2);
    fireEvent.click(within(table).getByRole("button", { expanded: false, name: /AHU-9/ }));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "View" })).toHaveLength(3));

    // The Asset cell itself is a keyboard-accessible selection control. It
    // replaces the Inspector immediately and never opens a duplicate modal.
    const ahuAssetButtons = screen.getAllByRole("button", { name: "AHU-9" });
    fireEvent.click(ahuAssetButtons[ahuAssetButtons.length - 1]);
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: /AHU-9.*issue/i })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("filters the results and inspector by observed/not-observed state (ITEM-10)", async () => {
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 0,
    };
    const summaryAsset = (assetId: string, summaryObserved: boolean, received: boolean) => ({
      asset_id: assetId,
      system: "Legacy",
      observed: summaryObserved,
      expected_payloads: 1,
      received_payloads: received ? 1 : 0,
      all_expected_payloads_received: received,
      all_received_payloads_successfully_validated: received,
      successfully_validated: false,
      issue_count: 0,
      blocking_issue_count: 0,
      last_observed_at: received ? "2026-07-23T01:00:00Z" : null,
      payload_results: [
        {
          payload_type: "pointset",
          expected: true,
          received,
          // EM-1 arrived on its expected topic but is invalid. Observation is
          // traffic presence, not JSON/schema validity.
          has_issues: assetId === "EM-1",
          blocking_issue_count: assetId === "EM-1" ? 1 : 0,
          successfully_validated: false,
          topic: `${assetId}/pointset`,
          received_at: received ? "2026-07-23T01:00:00Z" : null,
        },
      ],
    });
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        expected_devices: 2,
        publishing_seen: 1,
        not_publishing_devices: ["EM-2"],
        payload_views: [
          {
            asset_id: "EM-1",
            system: "BMS",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: {} },
                observed_present: true,
              },
            ],
          },
          {
            asset_id: "EM-2",
            system: "Lighting",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: null,
                observed_present: false,
              },
            ],
          },
        ],
        validation_summary_v1: {
          schema_version: "1.0",
          asset_metrics: {
            expected: 2,
            observed: 1,
            not_observed: 1,
            with_issues: 0,
            successfully_validated: 0,
          },
          payload_metrics: {
            expected: 2,
            received: 1,
            with_issues: 0,
            successfully_validated: 1,
          },
          fault_metrics: zeroFaults,
          issue_metrics: { blocking: 0, warning: 0 },
          system_metrics: [],
          // Deliberately stale observation/system facts. Direct payload views
          // above must reconcile both the table and summary-card filters.
          asset_results: [summaryAsset("EM-1", false, true), summaryAsset("EM-2", true, false)],
          fault_rows: [],
        },
      },
    };
    stubTwoAssetUdmi(run, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/across 2 expected assets/i)).toBeInTheDocument();

    const table = document.querySelector(".results-scroll table") as HTMLTableElement;
    expect(
      await within(table).findByRole("button", { expanded: true, name: /EM-1/ }),
    ).toBeInTheDocument();

    // Observation = not observed hides EM-1 and keeps only the silent EM-2.
    fireEvent.change(screen.getByLabelText("Observation"), { target: { value: "not-observed" } });
    expect(
      within(table).queryByRole("button", { expanded: true, name: /EM-1/ }),
    ).not.toBeInTheDocument();
    expect(
      await within(table).findByRole("button", { expanded: true, name: /EM-2/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/across 1 expected asset\b/i)).toBeInTheDocument();
    const summaryPanel = screen
      .getByRole("heading", { name: "Validation summary" })
      .closest(".udmi-summary") as HTMLElement;
    const expectedAssets = within(summaryPanel)
      .getByText("Expected assets")
      .closest("div") as HTMLElement;
    const observedAssets = within(summaryPanel)
      .getByText("Observed assets")
      .closest("div") as HTMLElement;
    expect(within(expectedAssets).getByText("1")).toBeInTheDocument();
    expect(within(observedAssets).getByText("0")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "BMS" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Lighting" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Legacy" })).not.toBeInTheDocument();

    // Observed selects the invalid-but-retained expected-topic payload. The
    // missing EM-2 (and separate unexpected publishers) are not promoted.
    fireEvent.change(screen.getByLabelText("Observation"), { target: { value: "observed" } });
    expect(
      await within(table).findByRole("button", { expanded: true, name: /EM-1/ }),
    ).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: /EM-2/ })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Observation"), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText("System"), { target: { value: "BMS" } });
    expect(
      await within(table).findByRole("button", { expanded: true, name: /EM-1/ }),
    ).toBeInTheDocument();
    expect(
      within(table).queryByRole("button", { expanded: false, name: /EM-2/ }),
    ).not.toBeInTheDocument();
    expect(within(expectedAssets).getByText("1")).toBeInTheDocument();
    expect(within(observedAssets).getByText("1")).toBeInTheDocument();
  });

  it("uses the stable summary for observation filters when a fixture run has no payload views", async () => {
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 0,
    };
    const assetResult = (assetId: string, system: string, observed: boolean) => ({
      asset_id: assetId,
      system,
      observed,
      expected_payloads: 1,
      received_payloads: observed ? 1 : 0,
      all_expected_payloads_received: observed,
      all_received_payloads_successfully_validated: observed,
      successfully_validated: false,
      issue_count: 1,
      blocking_issue_count: 0,
      last_observed_at: observed ? "2026-07-23T01:00:00Z" : null,
      payload_results: [
        {
          payload_type: "pointset",
          expected: true,
          received: observed,
          has_issues: true,
          blocking_issue_count: 0,
          successfully_validated: observed,
          topic: null,
          received_at: observed ? "2026-07-23T01:00:00Z" : null,
        },
      ],
    });
    const fixtureRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: null,
        validation_summary_v1: {
          schema_version: "1.0",
          asset_metrics: {
            expected: 2,
            observed: 1,
            not_observed: 1,
            with_issues: 2,
            successfully_validated: 0,
          },
          payload_metrics: {
            expected: 2,
            received: 1,
            with_issues: 2,
            successfully_validated: 0,
          },
          fault_metrics: { ...zeroFaults, other_issues: 2 },
          issue_metrics: { blocking: 0, warning: 2 },
          system_metrics: [],
          asset_results: [
            assetResult("EM-OBSERVED", "SEC", true),
            assetResult("EM-SILENT", "BMS", false),
          ],
          fault_rows: [],
        },
      },
    };
    const fixtureIssues = {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "fixture-observed",
          asset_id: "EM-OBSERVED",
          issue_type: "pointset_validation",
          severity: "medium",
          description: "Observed fixture payload needs review.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
        {
          issue_id: "fixture-silent",
          asset_id: "EM-SILENT",
          issue_type: "not_publishing",
          severity: "medium",
          description: "No fixture payload was observed.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    };

    stubTwoAssetUdmi(fixtureRun, fixtureIssues);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/across 2 expected assets/i)).toBeInTheDocument();

    const table = screen.getByRole("table");
    fireEvent.change(screen.getByLabelText("Observation"), { target: { value: "observed" } });
    expect(
      await within(table).findByRole("button", { expanded: true, name: /EM-OBSERVED/ }),
    ).toBeInTheDocument();
    expect(
      within(table).queryByRole("button", { expanded: false, name: /EM-SILENT/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/across 1 expected asset\b/i)).toBeInTheDocument();
    const summaryPanel = screen
      .getByRole("heading", { name: "Validation summary" })
      .closest(".udmi-summary") as HTMLElement;
    const expectedAssets = within(summaryPanel)
      .getByText("Expected assets")
      .closest("div") as HTMLElement;
    const observedAssets = within(summaryPanel)
      .getByText("Observed assets")
      .closest("div") as HTMLElement;
    expect(within(expectedAssets).getByText("1")).toBeInTheDocument();
    expect(within(observedAssets).getByText("1")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "SEC" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "BMS" })).toBeInTheDocument();
  });

  it("shows versioned metrics and filters the table and inspector by a partial topic", async () => {
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 0,
    };
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [
          {
            asset_id: "EM-1",
            system: "SEC",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: {} },
                observed_present: true,
              },
            ],
          },
          {
            asset_id: "EM-2",
            system: "BMS",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: {} },
                observed_present: true,
              },
            ],
          },
        ],
        validation_summary_v1: {
          schema_version: "1.0",
          asset_metrics: {
            expected: 5,
            observed: 5,
            not_observed: 0,
            with_issues: 1,
            successfully_validated: 4,
          },
          payload_metrics: {
            expected: 10,
            received: 10,
            with_issues: 1,
            successfully_validated: 9,
          },
          fault_metrics: { ...zeroFaults, missing_points: 1 },
          issue_metrics: { blocking: 1, warning: 2 },
          system_metrics: [
            {
              system: "BMS",
              asset_metrics: {
                expected: 5,
                observed: 5,
                not_observed: 0,
                with_issues: 1,
                successfully_validated: 4,
              },
              payload_metrics: {
                expected: 10,
                received: 10,
                with_issues: 1,
                successfully_validated: 9,
              },
              fault_metrics: { ...zeroFaults, missing_points: 1 },
              issue_metrics: { blocking: 1, warning: 2 },
            },
            {
              system: "Lighting",
              asset_metrics: {
                expected: 0,
                observed: 0,
                not_observed: 0,
                with_issues: 0,
                successfully_validated: 0,
              },
              payload_metrics: {
                expected: 0,
                received: 0,
                with_issues: 0,
                successfully_validated: 0,
              },
              fault_metrics: zeroFaults,
              issue_metrics: { blocking: 0, warning: 0 },
            },
          ],
          asset_results: [
            {
              asset_id: "EM-1",
              system: "SEC",
              observed: true,
              expected_payloads: 1,
              received_payloads: 1,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: true,
              successfully_validated: false,
              issue_count: 2,
              blocking_issue_count: 1,
              last_observed_at: "2026-06-11T09:04:00Z",
              payload_results: [
                {
                  payload_type: "pointset",
                  expected: true,
                  received: true,
                  has_issues: true,
                  blocking_issue_count: 1,
                  successfully_validated: false,
                  topic: "HV/SEC/02/MTS-100/pointset",
                  received_at: "2026-06-11T09:04:00Z",
                },
                {
                  payload_type: "unexpected_event",
                  expected: false,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "HV/SEC/02/MTS-100/unexpected_event",
                  received_at: "2026-06-11T09:04:10Z",
                },
              ],
            },
            {
              asset_id: "EM-2",
              system: "BMS",
              observed: true,
              expected_payloads: 1,
              received_payloads: 1,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: true,
              successfully_validated: true,
              issue_count: 0,
              blocking_issue_count: 0,
              last_observed_at: "2026-06-11T09:04:30Z",
              payload_results: [
                {
                  payload_type: "pointset",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "hv/bms/00/MTS-200/pointset",
                  received_at: "2026-06-11T09:04:30Z",
                },
              ],
            },
          ],
          fault_rows: [
            {
              issue_id: "issue-pointset",
              asset_id: "EM-1",
              system: "SEC",
              payload_type: "pointset",
              category: "missing_points",
              severity: "critical",
              description: "Expected point missing.",
              point_name: "supply_temp",
              expected_value: "present",
              observed_value: null,
              suggested_action: "Publish the registered point.",
              raw_evidence_uri: null,
            },
            {
              issue_id: "issue-other-payload",
              asset_id: "EM-1",
              system: "SEC",
              payload_type: "metadata",
              category: "schema",
              severity: "warning",
              description: "Metadata warning.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
          ],
        },
      },
    };
    stubTwoAssetUdmi(run, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    const summaryHeading = await screen.findByRole(
      "heading",
      { name: "Validation summary" },
      { timeout: 5_000 },
    );
    const summaryPanelBeforeFilter = summaryHeading.closest(".udmi-summary") as HTMLElement;
    const overallMetric = within(summaryPanelBeforeFilter)
      .getByText("Overall compliance")
      .closest("div") as HTMLElement;
    expect(within(overallMetric).getByText("80%")).toBeInTheDocument();
    expect(within(overallMetric).getByText("(4 / 5 assets)")).toBeInTheDocument();
    const initialPayloadGroup = within(summaryPanelBeforeFilter)
      .getByRole("heading", { name: "Payload metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    const initiallyCorrect = within(initialPayloadGroup)
      .getByText("Payloads correct")
      .closest("div") as HTMLElement;
    const initialAssetGroup = within(summaryPanelBeforeFilter)
      .getByRole("heading", { name: "Asset metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    const initialFaultGroup = within(summaryPanelBeforeFilter)
      .getByRole("heading", { name: "Fault metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    expect(initialAssetGroup).toHaveClass("udmi-metric-assets");
    expect(initialPayloadGroup).toHaveClass("udmi-metric-payloads");
    expect(initialFaultGroup).toHaveClass("udmi-metric-faults");
    expect(initialAssetGroup.querySelector(".udmi-metric-table")).toBeInTheDocument();
    expect(initialPayloadGroup.querySelector(".udmi-metric-table")).toBeInTheDocument();
    expect(initialPayloadGroup.querySelector(".udmi-payload-rates")).toBeInTheDocument();
    expect(initialFaultGroup.querySelector(".udmi-metric-table")).toBeInTheDocument();
    // Wide-grid rules are presentation-only. Empty cells must not enter the
    // description lists or create extra rows at smaller container widths.
    expect(initialAssetGroup.querySelectorAll(".udmi-metric-table > div")).toHaveLength(7);
    expect(initialPayloadGroup.querySelectorAll(".udmi-metric-table > div")).toHaveLength(5);
    expect(initialFaultGroup.querySelectorAll(".udmi-metric-table > div")).toHaveLength(6);

    const initiallyIncorrect = within(initialPayloadGroup)
      .getByText("Payloads incorrect")
      .closest("div") as HTMLElement;
    expect(within(initiallyCorrect).getByText("90%")).toBeInTheDocument();
    expect(within(initiallyCorrect).getByText("9 / 10 expected")).toBeInTheDocument();
    expect(within(initiallyIncorrect).getByText("10%")).toBeInTheDocument();
    expect(within(initiallyIncorrect).getByText("1 / 10 expected")).toBeInTheDocument();
    const systemSection = screen
      .getByRole("heading", { name: "Completion by system" })
      .closest(".udmi-system-summary") as HTMLElement;
    const bmsRow = within(systemSection).getByText("BMS").closest("tr") as HTMLTableRowElement;
    expect(within(bmsRow).getByText("80%")).toBeInTheDocument();
    expect(within(bmsRow).getByText("(4 / 5 assets)")).toBeInTheDocument();
    const lightingRow = within(systemSection)
      .getByText("Lighting")
      .closest("tr") as HTMLTableRowElement;
    expect(within(lightingRow).getByText("N/A")).toBeInTheDocument();
    expect(within(lightingRow).getByText("(0 / 0 assets)")).toBeInTheDocument();

    const resultsTable = document.querySelector(".results-scroll table") as HTMLTableElement;
    const resultsGrid = resultsTable.closest(".app-grid") as HTMLElement;
    expect(resultsGrid).not.toHaveClass("two-col");
    expect(resultsGrid.children[1]).toHaveClass("inspector");
    expect(within(resultsTable).getByRole("columnheader", { name: "Topic" })).toBeInTheDocument();
    expect(await within(resultsTable).findByText("HV/SEC/02/MTS-100/pointset")).toBeInTheDocument();
    expect(
      await within(resultsTable).findByRole("button", { expanded: true, name: /EM-1/ }),
    ).toBeInTheDocument();
    expect(
      within(resultsTable).getByRole("button", { expanded: false, name: /EM-2/ }),
    ).toBeInTheDocument();
    const inspector = document.querySelector(".inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: /EM-1/ })).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: /EM-2/ })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Topic contains"), { target: { value: "hv/sec" } });
    expect(
      await within(resultsTable).findByRole("button", { expanded: true, name: /EM-1/ }),
    ).toBeInTheDocument();
    expect(
      within(resultsTable).queryByRole("button", { expanded: false, name: /EM-2/ }),
    ).not.toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: /EM-1/ })).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: /EM-2/ })).not.toBeInTheDocument();
    const summaryPanel = screen
      .getByRole("heading", { name: "Validation summary" })
      .closest(".udmi-summary") as HTMLElement;
    const expectedAssetsMetric = within(summaryPanel)
      .getByText("Expected assets")
      .closest("div") as HTMLElement;
    expect(within(expectedAssetsMetric).getByText("1")).toBeInTheDocument();
    const payloadGroup = within(summaryPanel)
      .getByRole("heading", { name: "Payload metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    expect(
      within(
        within(payloadGroup).getByText("Expected payloads").closest("div") as HTMLElement,
      ).getByText("1"),
    ).toBeInTheDocument();
    expect(
      within(
        within(payloadGroup).getByText("Received payloads").closest("div") as HTMLElement,
      ).getByText("1"),
    ).toBeInTheDocument();
    expect(
      within(
        within(payloadGroup).getByText("Successfully validated").closest("div") as HTMLElement,
      ).getByText("0"),
    ).toBeInTheDocument();
    const filteredCorrect = within(payloadGroup)
      .getByText("Payloads correct")
      .closest("div") as HTMLElement;
    const filteredIncorrect = within(payloadGroup)
      .getByText("Payloads incorrect")
      .closest("div") as HTMLElement;
    expect(within(filteredCorrect).getByText("0%")).toBeInTheDocument();
    expect(within(filteredIncorrect).getByText("100%")).toBeInTheDocument();
    expect(
      within(payloadGroup).getByText(
        "Expected payloads are the denominator. Unexpected received payloads are excluded.",
      ),
    ).toBeInTheDocument();
    const issueMetric = within(summaryPanel)
      .getByText("Issues", { selector: "dt" })
      .closest("div") as HTMLElement;
    expect(within(issueMetric).getByText("1")).toBeInTheDocument();
    expect(within(issueMetric).getByText("(0 warnings)")).toBeInTheDocument();
    expect(
      within(summaryPanel).getByText(/exact rows retained by every active result filter/i),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Topic contains"), { target: { value: "" } });
    expect(
      within(resultsTable).getByRole("button", { expanded: false, name: /EM-2/ }),
    ).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: /EM-2/ })).not.toBeInTheDocument();
    expect(within(expectedAssetsMetric).getByText("5")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter results"), { target: { value: "EM-2" } });
    expect(
      within(resultsTable).queryByRole("button", { expanded: true, name: /EM-1/ }),
    ).not.toBeInTheDocument();
    expect(
      await within(resultsTable).findByRole("button", { expanded: true, name: /EM-2/ }),
    ).toBeInTheDocument();
    expect(within(expectedAssetsMetric).getByText("1")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter results"), { target: { value: "" } });

    fireEvent.change(screen.getByLabelText("Verdict"), { target: { value: "pass" } });
    expect(within(expectedAssetsMetric).getByText("2")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Verdict"), { target: { value: "all" } });
    expect(within(expectedAssetsMetric).getByText("5")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Topic contains"), {
      target: { value: "no/such/topic" },
    });
    expect(within(overallMetric).getByText("N/A")).toBeInTheDocument();
    expect(within(overallMetric).getByText("(0 / 0 assets)")).toBeInTheDocument();
    expect(within(filteredCorrect).getByText("N/A")).toBeInTheDocument();
    expect(within(filteredIncorrect).getByText("N/A")).toBeInTheDocument();
    expect(
      within(inspector).getByText("No asset selected in the filtered results"),
    ).toBeInTheDocument();
  });

  it("matches report metrics for unreceived payload issues and asset-level faults in a partial scope", async () => {
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 0,
    };
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [
          {
            asset_id: "A-1",
            system: "BMS",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: {} },
                observed_present: true,
              },
              {
                payload_type: "metadata",
                expected: { version: "1.5.2" },
                observed: null,
                observed_present: false,
              },
            ],
          },
        ],
        validation_summary_v1: {
          schema_version: "1.1",
          asset_metrics: {
            expected: 1,
            observed: 1,
            not_observed: 0,
            with_issues: 1,
            successfully_validated: 0,
            unexpected: 0,
          },
          payload_metrics: {
            expected: 2,
            received: 1,
            not_received: 1,
            with_issues: 1,
            successfully_validated: 1,
          },
          fault_metrics: { ...zeroFaults, payload_formatting_issues: 1, other_issues: 3 },
          issue_metrics: { blocking: 2, warning: 2 },
          system_metrics: [],
          asset_results: [
            {
              asset_id: "A-1",
              system: "BMS",
              observed: true,
              expected_payloads: 2,
              received_payloads: 1,
              all_expected_payloads_received: false,
              all_received_payloads_successfully_validated: true,
              successfully_validated: false,
              issue_count: 3,
              blocking_issue_count: 1,
              last_observed_at: "2026-07-23T10:00:00Z",
              payload_results: [
                {
                  payload_type: "pointset",
                  expected: true,
                  received: true,
                  has_issues: true,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/A-1/pointset",
                  received_at: "2026-07-23T10:00:00Z",
                },
                {
                  payload_type: "metadata",
                  expected: true,
                  received: false,
                  has_issues: true,
                  blocking_issue_count: 1,
                  successfully_validated: false,
                  topic: "site/A-1/metadata",
                  received_at: null,
                },
              ],
            },
          ],
          fault_rows: [
            {
              issue_id: "pointset-warning",
              asset_id: "A-1",
              system: "BMS",
              payload_type: "pointset",
              category: "other_issues",
              severity: "warning",
              description: "Pointset note.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
            {
              issue_id: "metadata-blocking",
              asset_id: "A-1",
              system: "BMS",
              payload_type: "metadata",
              category: "payload_formatting_issues",
              severity: "high",
              description: "Metadata did not arrive.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
            {
              issue_id: "asset-warning",
              asset_id: "A-1",
              system: "BMS",
              payload_type: null,
              category: "other_issues",
              severity: "warning",
              description: "Asset-level note.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
            {
              issue_id: "run-blocking",
              asset_id: null,
              system: "Unspecified",
              payload_type: null,
              category: "other_issues",
              severity: "high",
              description: "Run-wide broker finding.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
          ],
          unexpected_devices: [],
          unexpected_devices_measured: true,
          unexpected_devices_measurement_scope: "site/A-1/#",
        },
      },
    };
    stubTwoAssetUdmi(run, {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "pointset-warning",
          asset_id: "A-1",
          issue_type: "pointset_validation",
          severity: "minor",
          description: "Pointset note.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
        {
          issue_id: "metadata-blocking",
          asset_id: "A-1",
          issue_type: "metadata_validation",
          severity: "major",
          description: "Metadata did not arrive.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await screen.findByText(/Live validation results/i);
    const liveResultsTable = document.querySelector(".results-scroll table") as HTMLTableElement;
    const metadataRow = (await within(liveResultsTable).findByText("UDMI metadata")).closest(
      "tr",
    ) as HTMLTableRowElement;
    // Missing metadata carries a blocking finding, but no payload was present to
    // validate. Presence wins before severity, so this stays neutral.
    expect(within(metadataRow).getByText("Not received")).toBeInTheDocument();
    expect(metadataRow).not.toHaveClass("row-warn");
    expect(metadataRow).not.toHaveClass("row-fail");
    const inspector = document.querySelector(".inspector") as HTMLElement;
    const assetToggle = within(inspector).getByRole("button", { name: /A-1.*issue/i });
    if (assetToggle.getAttribute("aria-expanded") !== "true") {
      fireEvent.click(assetToggle);
    }
    const metadataGroup = within(inspector)
      .getByRole("heading", { name: "metadata" })
      .closest(".payload-type-group") as HTMLElement;
    expect(
      within(metadataGroup).getByText("NOT RECEIVED: no payload arrived for this payload type"),
    ).toHaveClass("payload-verdict", "neutral");
    fireEvent.change(await screen.findByLabelText("Filter results"), {
      target: { value: "A-1" },
    });

    const summaryPanel = screen
      .getByRole("heading", { name: "Validation summary" })
      .closest(".udmi-summary") as HTMLElement;
    const payloadGroup = within(summaryPanel)
      .getByRole("heading", { name: "Payload metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    const assetGroup = within(summaryPanel)
      .getByRole("heading", { name: "Asset metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    const payloadsWithIssues = within(payloadGroup)
      .getByText("Payloads with issues")
      .closest("div") as HTMLElement;
    const issueMetric = within(summaryPanel)
      .getByText("Issues", { selector: "dt" })
      .closest("div") as HTMLElement;

    // The missing metadata payload has an issue, but report metrics only count
    // issues on expected payloads that were actually received.
    expect(within(payloadsWithIssues).getByText("1")).toBeInTheDocument();
    // Both payloads are selected, so the asset-wide warning and run-wide
    // finding remain in scope with the two payload findings.
    expect(within(issueMetric).getByText("4")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Topic contains"), {
      target: { value: "pointset" },
    });
    // A partial payload selection retains its pointset warning, but drops the
    // asset-wide warning until every available payload for A-1 is selected.
    expect(within(issueMetric).getByText("1")).toBeInTheDocument();
    const successfullyValidatedAssets = within(assetGroup)
      .getByText("Successfully validated")
      .closest("div") as HTMLElement;
    // Backend compliance is blocked only by a missing expected payload or a
    // blocking fault. This selected pointset was received, so its warning alone
    // does not fail the asset.
    expect(within(successfullyValidatedAssets).getByText("1")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Topic contains"), {
      target: { value: "metadata" },
    });
    const observedAssets = within(assetGroup)
      .getByText("Observed assets")
      .closest("div") as HTMLElement;
    const notObservedAssetsMetric = within(assetGroup)
      .getByText("Not observed")
      .closest("div") as HTMLElement;
    expect(within(observedAssets).getByText("0")).toBeInTheDocument();
    expect(within(notObservedAssetsMetric).getByText("1")).toBeInTheDocument();
  });

  it("normalizes schema 1.0 missing-payload issue counts to the received-only definition", async () => {
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 1,
    };
    const legacyRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        validation_summary_v1: {
          schema_version: "1.0",
          asset_metrics: {
            expected: 1,
            observed: 0,
            not_observed: 1,
            with_issues: 1,
            successfully_validated: 0,
          },
          payload_metrics: {
            expected: 1,
            received: 0,
            with_issues: 1,
            successfully_validated: 0,
          },
          fault_metrics: zeroFaults,
          issue_metrics: { blocking: 1, warning: 0 },
          system_metrics: [
            {
              system: "BMS",
              asset_metrics: {
                expected: 1,
                observed: 0,
                not_observed: 1,
                with_issues: 1,
                successfully_validated: 0,
              },
              payload_metrics: {
                expected: 1,
                received: 0,
                with_issues: 1,
                successfully_validated: 0,
              },
              fault_metrics: zeroFaults,
              issue_metrics: { blocking: 1, warning: 0 },
            },
          ],
          asset_results: [
            {
              asset_id: "A-1",
              system: "BMS",
              observed: false,
              expected_payloads: 1,
              received_payloads: 0,
              all_expected_payloads_received: false,
              all_received_payloads_successfully_validated: false,
              successfully_validated: false,
              issue_count: 1,
              blocking_issue_count: 1,
              last_observed_at: null,
              payload_results: [
                {
                  payload_type: "state",
                  expected: true,
                  received: false,
                  has_issues: true,
                  blocking_issue_count: 1,
                  successfully_validated: false,
                  topic: "site/A-1/state",
                  received_at: null,
                },
              ],
            },
          ],
          fault_rows: [
            {
              issue_id: "state-not-received",
              asset_id: "A-1",
              system: "BMS",
              payload_type: "state",
              category: "other_issues",
              severity: "high",
              description: "State was not received.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
          ],
        },
      },
    };
    stubTwoAssetUdmi(legacyRun, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    const summaryPanel = await screen.findByRole("heading", { name: "Validation summary" });
    const payloadGroup = within(summaryPanel.closest(".udmi-summary") as HTMLElement)
      .getByRole("heading", { name: "Payload metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    const withIssues = within(payloadGroup)
      .getByText("Payloads with issues")
      .closest("div") as HTMLElement;
    const notReceived = within(payloadGroup)
      .getByText("Not received")
      .closest("div") as HTMLElement;

    expect(within(withIssues).getByText("0")).toBeInTheDocument();
    expect(within(notReceived).getByText("1")).toBeInTheDocument();
  });

  it("retires legacy unexpected-device faults by persisted issue ID", async () => {
    const legacyRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        validation_summary_v1: {
          schema_version: "1.0",
          asset_metrics: {
            expected: 1,
            observed: 1,
            not_observed: 0,
            with_issues: 0,
            successfully_validated: 1,
          },
          payload_metrics: {
            expected: 1,
            received: 1,
            with_issues: 0,
            successfully_validated: 1,
          },
          fault_metrics: {
            payload_formatting_issues: 0,
            missing_points: 0,
            point_naming_issues: 0,
            additional_points: 0,
            stale_or_cadence: 0,
            other_issues: 1,
          },
          issue_metrics: { blocking: 1, warning: 0 },
          system_metrics: [],
          asset_results: [
            {
              asset_id: "A-1",
              system: "BMS",
              observed: true,
              expected_payloads: 1,
              received_payloads: 1,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: true,
              successfully_validated: true,
              issue_count: 0,
              blocking_issue_count: 0,
              last_observed_at: "2026-07-23T10:00:00Z",
              payload_results: [
                {
                  payload_type: "state",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/A-1/state",
                  received_at: "2026-07-23T10:00:00Z",
                },
              ],
            },
          ],
          fault_rows: [
            {
              issue_id: "legacy-unexpected",
              asset_id: "rogue-legacy",
              system: "Unspecified",
              payload_type: null,
              category: "other_issues",
              severity: "high",
              description: "Legacy unexpected publisher finding.",
              point_name: null,
              expected_value: null,
              observed_value: null,
              suggested_action: null,
              raw_evidence_uri: null,
            },
          ],
          unexpected_devices: [
            {
              id: "rogue-legacy",
              topic_root: "site/rogue-legacy",
              topics: ["site/rogue-legacy/state"],
              last_seen: "2026-07-23T10:00:00Z",
            },
          ],
        },
      },
    };
    stubTwoAssetUdmi(legacyRun, {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "legacy-unexpected",
          asset_id: "rogue-legacy",
          issue_type: "unexpected_device",
          severity: "high",
          description: "Legacy unexpected publisher finding.",
        },
      ],
    });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    const summaryPanel = await screen.findByRole("heading", { name: "Validation summary" });
    const summary = summaryPanel.closest(".udmi-summary") as HTMLElement;
    const faultGroup = within(summary)
      .getByRole("heading", { name: "Fault metrics" })
      .closest(".udmi-metric-group") as HTMLElement;
    const otherIssues = within(faultGroup).getByText("Other issues").closest("div") as HTMLElement;
    const issueMetric = within(summary)
      .getByText("Issues", { selector: "dt" })
      .closest("div") as HTMLElement;

    expect(within(otherIssues).getByText("0")).toBeInTheDocument();
    expect(within(issueMetric).getByText("0")).toBeInTheDocument();
    expect(screen.queryByText("Legacy unexpected publisher finding.")).not.toBeInTheDocument();
    const unexpectedMetric = within(summary)
      .getByText("Unexpected devices")
      .closest("div") as HTMLElement;
    expect(within(unexpectedMetric).getByText("1")).toBeInTheDocument();
  });

  it("keeps non-expected payload evidence visible but excludes it from exact report scope", async () => {
    const reportBodies: Record<string, unknown>[] = [];
    const zeroFaults = {
      payload_formatting_issues: 0,
      missing_points: 0,
      point_naming_issues: 0,
      additional_points: 0,
      stale_or_cadence: 0,
      other_issues: 0,
    };
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [
          {
            asset_id: "SUBSET-1",
            system: "BMS",
            // The display projection can contain templates for all three UDMI
            // facets even when the report contract retained only one key.
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: {} },
                observed_present: true,
              },
              {
                payload_type: "metadata",
                expected: { version: "1.5.2" },
                observed: null,
                observed_present: false,
              },
              {
                payload_type: "state",
                expected: { version: "1.5.2" },
                observed: null,
                observed_present: false,
              },
            ],
          },
        ],
        validation_summary_v1: {
          schema_version: "1.1",
          asset_metrics: {
            expected: 1,
            observed: 1,
            not_observed: 0,
            with_issues: 0,
            successfully_validated: 1,
            unexpected: 0,
          },
          payload_metrics: {
            expected: 1,
            received: 1,
            not_received: 0,
            with_issues: 0,
            successfully_validated: 1,
          },
          fault_metrics: zeroFaults,
          issue_metrics: { blocking: 0, warning: 0 },
          system_metrics: [],
          asset_results: [
            {
              asset_id: "SUBSET-1",
              system: "BMS",
              observed: true,
              expected_payloads: 1,
              received_payloads: 1,
              all_expected_payloads_received: true,
              all_received_payloads_successfully_validated: true,
              successfully_validated: true,
              issue_count: 0,
              blocking_issue_count: 0,
              last_observed_at: "2026-07-23T10:05:00Z",
              payload_results: [
                {
                  payload_type: "pointset",
                  expected: true,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/SUBSET-1/pointset",
                  received_at: "2026-07-23T10:05:00Z",
                },
                {
                  payload_type: "metadata",
                  expected: false,
                  received: true,
                  has_issues: false,
                  blocking_issue_count: 0,
                  successfully_validated: true,
                  topic: "site/SUBSET-1/metadata",
                  received_at: "2026-07-23T10:05:30Z",
                },
              ],
            },
          ],
          fault_rows: [],
          unexpected_devices: [],
          unexpected_devices_measured: true,
          unexpected_devices_measurement_scope: "site/SUBSET-1/#",
        },
      },
    };
    stubTwoAssetUdmi(run, { run_id: "run-udmi-1", issues: [] }, reportBodies);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await screen.findByText(/Live validation results/i);

    const resultsTable = document.querySelector(".results-scroll table") as HTMLTableElement;
    const assetToggle = await within(resultsTable).findByRole("button", {
      expanded: true,
      name: /SUBSET-1/,
    });
    if (assetToggle.getAttribute("aria-expanded") !== "true") {
      fireEvent.click(assetToggle);
    }
    expect(within(resultsTable).getByText("UDMI pointset")).toBeInTheDocument();
    expect(within(resultsTable).getByText("UDMI metadata")).toBeInTheDocument();
    expect(within(resultsTable).queryByText("UDMI state")).not.toBeInTheDocument();

    const inspector = document.querySelector(".inspector") as HTMLElement;
    const inspectorToggle = within(inspector).getByRole("button", { name: /SUBSET-1.*issue/i });
    if (inspectorToggle.getAttribute("aria-expanded") !== "true") {
      fireEvent.click(inspectorToggle);
    }
    expect(within(inspector).getByRole("heading", { name: "pointset" })).toBeInTheDocument();
    expect(within(inspector).getByRole("heading", { name: "metadata" })).toBeInTheDocument();
    expect(within(inspector).queryByRole("heading", { name: "state" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter results"), {
      target: { value: "SUBSET-1" },
    });
    const generateButtons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(generateButtons[generateButtons.length - 1], "Subset payload report");
    await waitFor(() => expect(reportBodies).toHaveLength(1));
    expect(reportBodies[0].udmi_scope).toMatchObject({
      schema_version: "1.0",
      selected_payloads: [
        { source_run_id: "run-udmi-1", asset_id: "SUBSET-1", payload_type: "pointset" },
      ],
    });
  });

  it("re-attaches a still-running run with Stop retained and Execute locked", async () => {
    const runningRun = {
      run_id: "run-udmi-1",
      job_type: "udmi_validation",
      status: "running",
      stage: "capturing",
      progress_percent: 15,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:00:30Z",
      project_id: "demo-project",
      site_id: "demo-site",
      parameters: { capture_seconds: 0 },
      // Mid-run device progress the engine writes as it enriches each device, so
      // the monitor can show "X of Y devices" (hung vs working).
      result_summary: { progress: { devices_done: 3, devices_total: 41, points_read: 128 } },
      error_message: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // The rehydration query finds a still-running run of this head's job type.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: [
              {
                run_id: "run-udmi-1",
                job_type: "udmi_validation",
                status: "running",
                stage: "capturing",
                progress_percent: 15,
                created_at: "2026-06-11T09:00:00Z",
                updated_at: "2026-06-11T09:00:30Z",
                edge_id: null,
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues"))
          return jsonResponse({ run_id: "run-udmi-1", issues: [] });
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(runningRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    // The confirmed live run re-attaches its monitor with a Stop run control
    // and blocks a second capture, including after a reload.
    expect(await screen.findByRole("button", { name: "Stop run" })).toBeInTheDocument();
    expect(screen.getByText(/Stop run keeps the data collected so far/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Execute capture" })).toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: "Execute capture" })).toHaveAttribute(
      "title",
      "A run is already in progress. Stop it before starting another.",
    );

    // Progress + elapsed (ITEM-6): the monitor shows an Elapsed entry, and an
    // indefinite-window run (capture_seconds 0) shows the indeterminate sweep.
    expect(screen.getByText("Elapsed", { selector: "dt" })).toBeInTheDocument();
    expect(document.querySelector(".progress-track.indeterminate")).not.toBeNull();
    // Mid-run device progress from result_summary.progress lights up the monitor.
    expect(screen.getByText("3 of 41 devices · 128 points read")).toBeInTheDocument();
    // The monitor links to the persistent run history (naming/reachability).
    expect(screen.getByRole("link", { name: "Run history" })).toHaveAttribute(
      "href",
      "/run-history",
    );
  });

  it("does not submit or cancel through a locked restored-run Execute control", async () => {
    const runningRun = {
      run_id: "run-udmi-1",
      job_type: "udmi_validation",
      status: "running",
      stage: "capturing",
      progress_percent: 15,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:00:30Z",
      project_id: "demo-project",
      site_id: "demo-site",
      parameters: { capture_seconds: 0 },
      result_summary: {},
      error_message: null,
    };
    let cancelledRunId: string | null = null;
    let submitAttempted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: [
              {
                run_id: "run-udmi-1",
                job_type: "udmi_validation",
                status: "running",
                stage: "capturing",
                progress_percent: 15,
                created_at: "2026-06-11T09:00:00Z",
                updated_at: "2026-06-11T09:00:30Z",
                edge_id: null,
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        const cancelMatch = url.match(/\/api\/v1\/runs\/([^/]+)\/cancel$/);
        if (cancelMatch && init?.method === "POST") {
          cancelledRunId = cancelMatch[1];
          return jsonResponse({ ...runningRun, status: "cancelled" });
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          submitAttempted = true;
          return jsonResponse({ ...udmiAccepted, run_id: "run-udmi-2" });
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-2/issues"))
          return jsonResponse({ run_id: "run-udmi-2", issues: [] });
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues"))
          return jsonResponse({ run_id: "run-udmi-1", issues: [] });
        if (url.endsWith("/api/v1/validation/runs/run-udmi-2"))
          return jsonResponse({ ...runningRun, run_id: "run-udmi-2" });
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(runningRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    // The restored run keeps its reachable Stop action and the disabled control
    // cannot dispatch a replacement or implicitly cancel the original.
    await screen.findByRole("button", { name: "Stop run" });
    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeDisabled());

    fireEvent.click(runButton);
    expect(submitAttempted).toBe(false);
    expect(cancelledRunId).toBeNull();
    expect(screen.getByRole("button", { name: "Stop run" })).toBeInTheDocument();
  });

  it("shows the observed payload in the compare fallback when the expected template facet is null", async () => {
    // A payload can be observed while the expected side is null/empty (e.g. an
    // empty Expected schedule), so the aligned diff can't be built. The observed
    // side must still show the captured JSON — never the false claim "not
    // captured", which would contradict the row's Observed: Yes.
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [
          {
            asset_id: "EM-1",
            payload_types: [
              {
                payload_type: "pointset",
                expected: null,
                observed: { version: "1.5.2", points: { widget_sensor: { present_value: 99.9 } } },
                observed_present: true,
              },
            ],
          },
        ],
      },
    };
    stubTwoAssetUdmi(run, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    const inspector = document.querySelector(".inspector") as HTMLElement;
    const assetToggle = await within(inspector).findByRole("button", { name: /EM-1.*issue/i });
    if (assetToggle.getAttribute("aria-expanded") !== "true") {
      fireEvent.click(assetToggle);
    }
    const compareToggle = await within(inspector).findByRole("button", {
      name: /Show expected vs observed payload/i,
    });
    fireEvent.click(compareToggle);

    // The observed JSON is shown (real captured evidence), not "not captured".
    // Scoped to the inspector so the table's Raw Payload cell is not matched.
    expect(await within(inspector).findByText(/widget_sensor/)).toBeInTheDocument();
    expect(within(inspector).queryByText("not captured")).not.toBeInTheDocument();
  });

  it("hides the inspector detail when the selected asset's group is collapsed (ISSUE-4)", async () => {
    const cleanPayload = (asset: string, types: string[]) => ({
      asset_id: asset,
      payload_types: types.map((type) => ({
        payload_type: type,
        expected: { version: "1.5.2", points: {} },
        observed: { version: "1.5.2", points: {} },
        observed_present: true,
      })),
    });
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [cleanPayload("EM-1", ["pointset"]), cleanPayload("EM-2", ["pointset"])],
      },
    };
    stubTwoAssetUdmi(run, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // EM-1 is the first (selected) asset, so it auto-expands and the inspector
    // shows its selected-row detail ("Payload type" is unique to the inspector).
    const inspector = document.querySelector(".inspector") as HTMLElement;
    await waitFor(() => expect(within(inspector).getByText("Payload type")).toBeInTheDocument());

    // Collapse EM-1's group in the table: its child row unmounts, so the inspector
    // must stop showing that row's detail rather than describe a hidden row.
    const table = screen.getByRole("table");
    fireEvent.click(within(table).getByRole("button", { expanded: true, name: /EM-1/ }));
    await waitFor(() =>
      expect(
        within(document.querySelector(".inspector") as HTMLElement).queryByText("Payload type"),
      ).not.toBeInTheDocument(),
    );
  });

  it("keeps verdicts neutral (Verdict pending, no green Pass) while the issues query is loading", async () => {
    // The issues fetch never settles: the payload views land first (they ride
    // the run record), and an empty issues array must NOT read as a green
    // "Pass" — every verdict surface stays neutral until issues arrive.
    stubUdmiRunFetch(null, () => new Promise<Response>(() => {}));
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // Rows render from the payload views with a neutral pending verdict...
    expect((await screen.findAllByText("Verdict pending")).length).toBeGreaterThan(0);
    // ...and no pass/fail/warn shading or PASS text anywhere: a summary-derived
    // offline signal must not paint amber/red before the issues query settles.
    expect(document.querySelector("tr.row-pass, tr.row-fail, tr.row-warn")).toBeNull();
    // The verdict "Pass" never appears in a results-table row cell (the ISSUE-4
    // filter bar carries a "Pass" tone option, which is a control, not a verdict).
    expect(document.querySelector(".data-table")?.textContent).not.toContain("Pass");
    expect(screen.queryByText("PASS: UDMI Compliant")).not.toBeInTheDocument();
  });

  it("surfaces a visible error and keeps verdicts neutral when the issues fetch fails", async () => {
    // A failed issues fetch previously left the empty issues array in place —
    // PERMANENTLY rendering green Pass verdicts. It must instead surface the
    // failure near the results and keep every verdict surface neutral.
    stubUdmiRunFetch(
      null,
      () =>
        ({
          ok: false,
          status: 500,
          statusText: "Internal Server Error",
          json: async () => ({ detail: "issues backend unavailable" }),
        }) as unknown as Response,
    );
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    expect(
      await screen.findByText(/Could not load validation issues.*issues backend unavailable/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Verdict pending").length).toBeGreaterThan(0);
    expect(document.querySelector("tr.row-pass, tr.row-fail, tr.row-warn")).toBeNull();
    // The verdict "Pass" never appears in a results-table row cell (the ISSUE-4
    // filter bar carries a "Pass" tone option, which is a control, not a verdict).
    expect(document.querySelector(".data-table")?.textContent).not.toContain("Pass");
    expect(screen.queryByText("PASS: UDMI Compliant")).not.toBeInTheDocument();
  });

  it("register-driven mode sends no pasted schedule or payloads so the backend uses the imported register", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Validate against the imported MQTT register/i));
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    // No pasted expectation/payloads: the backend fans out one asset per
    // imported mqtt_register row (topic, points, units, schema version).
    expect(parameters).not.toHaveProperty("expected_schedule");
    expect(parameters).not.toHaveProperty("state_payload");
    expect(parameters).not.toHaveProperty("metadata_payload");
    expect(parameters).not.toHaveProperty("pointset_payload");
    expect(parameters).not.toHaveProperty("state_topic");
    // Blank run time (the default) => 0, the backend's indefinite sentinel:
    // run until every expected topic reports a payload or the run is stopped.
    expect(parameters.capture_seconds).toBe(0);
  });

  it("an accepted MQTT register enables register validation and live capture while keeping both reversible", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse([
            {
              import_type: "mqtt_register",
              description: "Expected MQTT assets.",
              required_columns: ["asset_id", "topic"],
              duplicate_key_fields: ["asset_id"],
            },
          ]);
        }
        if (url.endsWith("/api/v1/imports") && init?.method === "POST") {
          return jsonResponse({
            import_id: "import-mqtt-1",
            import_type: "mqtt_register",
            file_name: "mqtt_register.csv",
            file_type: "csv",
            project_id: "demo-project",
            site_id: "demo-site",
            total_rows: 1,
            accepted_rows: 1,
            rejected_rows: 0,
            status: "accepted",
            missing_columns: [],
            stored_file_name: "import-mqtt-1.csv",
            created_at: "2026-07-10T09:00:00Z",
          });
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    const registerMode = await screen.findByLabelText(
      /Validate against the imported MQTT register/i,
    );
    const liveCapture = screen.getByLabelText(
      /Capture latest state, metadata, and pointset payloads/i,
    );
    expect(registerMode).not.toBeChecked();
    expect(liveCapture).not.toBeChecked();

    fireEvent.change(screen.getByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["asset_id,topic\nEM-1,site/device"], "mqtt_register.csv")] },
    });
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);

    await waitFor(() => expect(registerMode).toBeChecked());
    expect(liveCapture).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Execute capture" }));
    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.use_register).toBe(true);
    expect(parameters.use_live_broker).toBe(true);
    expect(parameters.capture_seconds).toBe(0);
    expect(parameters).not.toHaveProperty("expected_schedule");
    expect(parameters).not.toHaveProperty("state_payload");
    expect(parameters).not.toHaveProperty("metadata_payload");
    expect(parameters).not.toHaveProperty("pointset_payload");

    fireEvent.click(registerMode);
    fireEvent.click(liveCapture);
    expect(registerMode).not.toBeChecked();
    expect(liveCapture).not.toBeChecked();
  });

  it("explains blank run time and every capture safety limit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );

    expect(
      screen.getByText(
        /Blank runs until every expected asset\/topic has reported or you press Stop run/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /48-hour safety limit.*500 distinct\s*concrete topics.*Closing the app ends the run/i,
      ),
    ).toBeInTheDocument();
  });

  it("a positive run time bounds the capture window sent with the run", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // The run-time input renders once live broker capture is ticked.
    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "45" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.capture_seconds).toBe(45);
  });

  it("a non-numeric run time blocks the submit with a validation error and posts nothing", async () => {
    let posted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          posted = true;
          return jsonResponse(udmiAccepted);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "45s" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    // "45s" must not silently coerce to the 0 = indefinite sentinel: the run is
    // rejected client-side with a visible error and no parameters are posted.
    expect(
      await screen.findByText(/Run time must be a positive number of seconds/i),
    ).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("converts an hours run time to seconds on the wire", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> })
      .parameters;
    expect(parameters.capture_seconds).toBe(7200);
  });

  it("refuses a run time over the 48-hour worker cap without posting", async () => {
    let posted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          posted = true;
          return jsonResponse(udmiAccepted);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "49" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });

    expect(await screen.findByText(/exceeds the 48-hour capture limit/i)).toBeInTheDocument();
    // Execute capture is now the ONLY trigger of the UDMI run action — the Run
    // Controls card is hidden — and it refuses the over-cap window. Hiding the
    // card must not lose the cap gate, so assert both the absence and the guard.
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    const executeButton = screen.getByRole("button", { name: "Execute capture" });
    expect(executeButton).toBeDisabled();
    fireEvent.click(executeButton);
    expect(posted).toBe(false);
  });

  it("clears a stale over-cap capture window when navigating to another module", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    stubScanAuthorizationFallback();
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    setApiKey("engineer-key");
    const tree = (route: string) => (
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute={route} />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    );
    const view = render(tree("udmi-validation"));

    // Type an hours-scale window over the 48h cap on the UDMI workbench.
    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "49" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    expect(await screen.findByText(/exceeds the 48-hour capture limit/i)).toBeInTheDocument();

    // Navigate to data-validation: the run-time control does not render there,
    // so a leaked over-cap window would disable its UDMI run action with no
    // visible input or error. The module-change reset must clear it.
    view.rerender(tree("data-validation"));
    const runButtons = await screen.findAllByRole("button", { name: "Run" });
    expect(runButtons).toHaveLength(3);
    for (const button of runButtons) {
      await waitFor(() => expect(button).toBeEnabled());
    }
  });

  it("generates a report from the run in the chosen format (PDF default)", async () => {
    const reportBodies: Record<string, unknown>[] = [];
    const threeAssetRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: ["EM-1", "EM-2", "EM-3"].map((assetId) => ({
          asset_id: assetId,
          system: "BMS",
          payload_types: [
            {
              payload_type: "state",
              expected: { timestamp: "<RFC 3339 timestamp>", version: "1.5.2" },
              observed: { timestamp: "2026-07-23T10:00:00Z", version: "1.5.2" },
              observed_present: true,
            },
          ],
        })),
      },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse({ run_id: "run-udmi-1", issues: [] });
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(threeAssetRun);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          reportBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
          return jsonResponse({
            file_name: "udmi_validation_rep-9.pdf",
            output_format: "pdf",
            report_id: "rep-9",
            report_type: "udmi_validation",
            status: "succeeded",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(
      await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));
    fireEvent.change(await screen.findByLabelText("Filter results"), {
      target: { value: "EM-" },
    });

    // Terminal run -> the report affordance appears with the PDF default, once
    // in the run monitor and once at the end of Results. Drive the Results one:
    // that is the copy the operator actually lands on when a run finishes.
    const generateButtons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    expect(generateButtons).toHaveLength(2);
    fireEvent.click(generateButtons[1]);
    const dialog = await screen.findByRole("dialog", { name: "Name this validation report" });
    expect(
      within(dialog).getByText(
        /Filtered scope locked when this dialog opened: 3 expected assets, 3 expected payloads/i,
      ),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter results"), {
      target: { value: "no matching publisher" },
    });
    fireEvent.change(within(dialog).getByLabelText("Report title"), {
      target: { value: "Demo Campus Smart Validation" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Generate report" }));
    await waitFor(() => expect(reportBodies).toHaveLength(1));
    const reportBody = reportBodies[0];
    expect(reportBody.output_format).toBe("pdf");
    expect(reportBody.report_title).toBe("Demo Campus Smart Validation");
    expect(reportBody.report_type).toBe("udmi_validation");
    expect(reportBody.udmi_scope).toEqual({
      schema_version: "1.0",
      selected_payloads: [
        { source_run_id: "run-udmi-1", asset_id: "EM-1", payload_type: "state" },
        { source_run_id: "run-udmi-1", asset_id: "EM-2", payload_type: "state" },
        { source_run_id: "run-udmi-1", asset_id: "EM-3", payload_type: "state" },
      ],
      unexpected_device_ids: [],
      filters: {
        text: "EM-",
        verdict: "all",
        topic_contains: "",
        system: "all",
        observation: "all",
        category: "all",
      },
    });

    // An active filter with no matches remains an explicit empty scope. Omitting
    // the scope here would silently generate an unfiltered report.
    fireEvent.change(screen.getByLabelText("Filter results"), {
      target: { value: "no matching publisher" },
    });
    await submitReportDialog(generateButtons[1], "No-match validation scope");
    await waitFor(() => expect(reportBodies).toHaveLength(2));
    expect(reportBodies[1].udmi_scope).toEqual({
      schema_version: "1.0",
      selected_payloads: [],
      unexpected_device_ids: [],
      filters: {
        text: "no matching publisher",
        verdict: "all",
        topic_contains: "",
        system: "all",
        observation: "all",
        category: "all",
      },
    });
  });

  // THE BREAKAGE-CATCHER. The Run Controls card for this action is hidden, which
  // makes its moduleData entry look unused — but Execute capture resolves it by
  // ARRAY INDEX. Delete the entry and mutationFn throws "Unknown run action."
  // before any fetch, so no POST lands and this test fails.
  it("Execute capture still resolves the hidden UDMI run action by index (do not delete the moduleData entry)", async () => {
    let postedUrl: string | null = null;
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedUrl = url;
          postedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");
    // Execute capture is engineer-gated, so it stays disabled until /me resolves.
    const executeButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(executeButton).toBeEnabled());
    fireEvent.click(executeButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    // The URL pins runKind and job_type pins jobType, so dispatching the WRONG
    // action (index drift) fails here rather than silently running something else.
    expect(postedUrl as unknown as string).toContain("/api/v1/validation/udmi/runs");
    expect((postedBody as unknown as Record<string, unknown>).job_type).toBe("udmi_validation");
    // onSuccess resolved the same action by index and attached the run.
    expect(await screen.findByText(/Validation run monitor/i)).toBeInTheDocument();
  });

  it("hides the Run UDMI Validation card from Run Controls and signposts Execute capture", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // field engineer 2026-07-15: the run control belongs at the bottom, after the options.
    // The card is genuinely absent from the DOM — a render assertion, not a CSS
    // one, so the jsdom step-gating caveat does not apply here.
    expect(await screen.findByRole("button", { name: "Execute capture" })).toBeInTheDocument();
    expect(screen.queryByText("Run UDMI Validation")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();

    // An empty Execution card would be its own confusion: point the operator at
    // the real trigger instead.
    expect(screen.getByText(/Run controls are at the bottom of Setup/i)).toBeInTheDocument();
    // The all-hidden branch must stay distinct from the no-actions branch — this
    // head DOES need a worker, so the synchronous copy would be a lie.
    expect(screen.queryByText("Saved synchronously")).not.toBeInTheDocument();
  });

  // Index-integrity pin. The fix maps the FULL runActions array and skips hidden
  // entries in place; a refactor to `visibleRunActions.map(...)` would renumber
  // cards and silently dispatch the wrong action the moment any earlier entry on
  // a multi-action head gets flagged. Card N must run runActions[N].
  it("dispatches the run action matching each card's own index on a multi-action head", async () => {
    let postedUrl: string | null = null;
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (
          url.includes("/api/v1/validation/") &&
          url.endsWith("/runs") &&
          init?.method === "POST"
        ) {
          postedUrl = url;
          postedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse({
            run_id: "run-bacnet-1",
            job_type: "bacnet_validation",
            status: "queued",
            message: "BACnet validation accepted.",
          });
        }
        if (url.endsWith("/api/v1/validation/runs/run-bacnet-1")) {
          return jsonResponse({
            ...udmiTerminalRun,
            run_id: "run-bacnet-1",
            job_type: "bacnet_validation",
          });
        }
        if (url.endsWith("/api/v1/validation/runs/run-bacnet-1/issues")) {
          return jsonResponse({ run_id: "run-bacnet-1", issues: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("data-validation");

    // data-validation renders all three cards (none hidden); the SECOND is the
    // BACnet Point Check at runActions[1].
    const runButtons = await screen.findAllByRole("button", { name: "Run" });
    expect(runButtons).toHaveLength(3);
    await waitFor(() => expect(runButtons[1]).toBeEnabled());
    fireEvent.click(runButtons[1]);

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedUrl as unknown as string).toContain("/api/v1/validation/bacnet/runs");
    expect((postedBody as unknown as Record<string, unknown>).job_type).toBe("bacnet_validation");
  });

  it("lets a viewer download terminal validation JSON evidence", async () => {
    const createObjectURL = vi.fn(() => "blob:validation-viewer-export");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: url.includes("job_type=udmi_validation") ? [{ ...udmiTerminalRun, edge_id: null }] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({ username: "viewer-1", role: "viewer", source: "user_key" });
        }
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/runs/run-udmi-1/events")) return controlledSseStream().response;
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(udmiTerminalRun);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/export.json")) {
          return {
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["viewer validation export"]),
            headers: { get: () => 'attachment; filename="viewer-validation.json"' },
          } as unknown as Response;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");
    const [downloadButton] = await screen.findAllByRole("button", { name: "Download raw JSON" });
    await waitFor(() => expect(downloadButton).toBeEnabled());
    fireEvent.click(downloadButton);
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(anchorClick).toHaveBeenCalledTimes(1);
    anchorClick.mockRestore();
  });

  it("aborts a validation JSON download when a same-ID submission enters a new epoch", async () => {
    const runId = "run-udmi-reused-export";
    const terminal = { ...udmiTerminalRun, run_id: runId };
    const firstStream = controlledSseStream();
    const secondStream = controlledSseStream();
    let eventStreamRequests = 0;
    let downloadSignal: AbortSignal | null = null;
    let resolveDownload!: (response: Response) => void;
    const deferredDownload = new Promise<Response>((resolve) => {
      resolveDownload = resolve;
    });
    const createObjectURL = vi.fn(() => "blob:stale-validation-epoch");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: url.includes("job_type=udmi_validation") ? [{ ...terminal, edge_id: null }] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith(`/api/v1/runs/${runId}/events`)) {
          eventStreamRequests += 1;
          return eventStreamRequests === 1 ? firstStream.response : secondStream.response;
        }
        if (url.endsWith(`/api/v1/validation/runs/${runId}/issues`)) {
          return jsonResponse({ ...udmiIssuesPayload, run_id: runId });
        }
        if (url.endsWith(`/api/v1/validation/runs/${runId}`)) return jsonResponse(terminal);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse({ ...udmiAccepted, run_id: runId });
        }
        if (url.endsWith(`/api/v1/validation/runs/${runId}/export.json`)) {
          downloadSignal = init?.signal ?? null;
          return deferredDownload;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");
    const [downloadButton] = await screen.findAllByRole("button", { name: "Download raw JSON" });
    await waitFor(() => expect(downloadButton).toBeEnabled());
    fireEvent.click(downloadButton);
    await waitFor(() => expect(downloadSignal).not.toBeNull());

    const executeButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(executeButton).toBeEnabled());
    fireEvent.click(executeButton);
    await waitFor(() => expect(downloadSignal?.aborted).toBe(true));
    await waitFor(() =>
      expect(
        screen
          .getAllByRole("button", { name: "Download raw JSON" })
          .some((button) => !(button as HTMLButtonElement).disabled),
      ).toBe(true),
    );

    await act(async () => {
      resolveDownload({
        ok: true,
        status: 200,
        statusText: "OK",
        blob: async () => new Blob(["stale validation epoch export"]),
        headers: { get: () => 'attachment; filename="stale.json"' },
      } as unknown as Response);
    });
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
    expect(screen.queryByText(/Raw JSON download failed/i)).not.toBeInTheDocument();
    expect(
      screen
        .getAllByRole("button", { name: "Download raw JSON" })
        .some((button) => !(button as HTMLButtonElement).disabled),
    ).toBe(true);
    anchorClick.mockRestore();
    await act(async () => {
      firstStream.close();
      secondStream.close();
    });
  });

  it.each(["resolve", "reject"])(
    "aborts a deferred validation JSON download on access closure (%s late response)",
    async (settlement) => {
      const stream = controlledSseStream();
      let downloadSignal: AbortSignal | null = null;
      let resolveDownload!: (response: Response) => void;
      let rejectDownload!: (error: Error) => void;
      const deferredDownload = new Promise<Response>((resolve, reject) => {
        resolveDownload = resolve;
        rejectDownload = reject;
      });
      const createObjectURL = vi.fn(() => "blob:stale-validation-json");
      vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
      const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes("/api/v1/runs?")) {
            return jsonResponse({
              runs: url.includes("job_type=udmi_validation") ? [{ ...udmiTerminalRun, edge_id: null }] : [],
            });
          }
          if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
          if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
          if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
          if (url.endsWith("/api/v1/runs/run-udmi-1/events")) return stream.response;
          if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) return jsonResponse(udmiIssuesPayload);
          if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(udmiTerminalRun);
          if (url.endsWith("/api/v1/validation/runs/run-udmi-1/export.json")) {
            downloadSignal = init?.signal ?? null;
            return deferredDownload;
          }
          throw new Error(`Unexpected fetch in test: ${url}`);
        }),
      );

      renderModule("udmi-validation");
      const [downloadButton] = await screen.findAllByRole("button", { name: "Download raw JSON" });
      fireEvent.click(downloadButton);
      await waitFor(() => expect(downloadSignal).not.toBeNull());
      stream.push(`event: closed\ndata: ${JSON.stringify({ run_id: "run-udmi-1", status: "closed" })}\n\n`);
      await waitFor(() => expect(downloadSignal?.aborted).toBe(true));
      await act(async () => {
        if (settlement === "resolve") {
          resolveDownload({
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["stale validation"]),
            headers: { get: () => 'attachment; filename="stale.json"' },
          } as unknown as Response);
        } else {
          rejectDownload(new Error("late validation download failure"));
        }
      });
      expect(createObjectURL).not.toHaveBeenCalled();
      expect(anchorClick).not.toHaveBeenCalled();
      expect(screen.queryByText(/Raw JSON download failed/i)).not.toBeInTheDocument();
      anchorClick.mockRestore();
      stream.close();
    },
  );
});

describe("ModulePage UDMI schema set uploads", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const existingSet = {
    version_label: "nonpub.1",
    filenames: ["state.json", "pointset.json"],
    uploaded_at: "2026-07-14T09:00:00Z",
  };

  // 204 No Content (DELETE success) carries no JSON body; request() must not
  // try to parse one.
  function noContentResponse(): Response {
    return {
      ok: true,
      status: 204,
      statusText: "No Content",
      json: async () => {
        throw new Error("204 has no body");
      },
    } as unknown as Response;
  }

  it("lists uploaded sets and uploads a new one as multipart FormData", async () => {
    const posted: FormData[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas") && init?.method === "POST") {
          posted.push(init.body as FormData);
          return jsonResponse({
            version_label: "nonpub.2",
            filenames: ["state.json"],
            uploaded_at: "2026-07-14T10:00:00Z",
          });
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([existingSet]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // The card renders on the UDMI route with the GET-backed list of sets.
    expect(await screen.findByText("Non-Published UDMI Schema Sets")).toBeInTheDocument();
    expect(await screen.findByText("nonpub.1")).toBeInTheDocument();
    expect(screen.getByText("state.json, pointset.json")).toBeInTheDocument();

    // Upload needs both a version label and at least one .json file.
    const uploadButton = screen.getByRole("button", { name: "Upload schema set" });
    expect(uploadButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Version label/i), { target: { value: "nonpub.2" } });
    fireEvent.change(screen.getByLabelText(/Schema JSON files/i), {
      target: {
        files: [new File(['{"title":"state"}'], "state.json", { type: "application/json" })],
      },
    });
    await waitFor(() => expect(uploadButton).toBeEnabled());
    fireEvent.click(uploadButton);

    // The POST is multipart FormData carrying the label plus the file.
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].get("version_label")).toBe("nonpub.2");
    expect((posted[0].get("files") as File).name).toBe("state.json");
    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
  });

  it("deletes an uploaded set via DELETE on its version label", async () => {
    let deletedUrl: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas/nonpub.1") && init?.method === "DELETE") {
          deletedUrl = url;
          return noContentResponse();
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([existingSet]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    const deleteButton = await screen.findByRole("button", { name: "Delete" });
    await waitFor(() => expect(deleteButton).toBeEnabled());
    fireEvent.click(deleteButton);
    await waitFor(() => expect(deletedUrl).toMatch(/\/api\/v1\/udmi\/schemas\/nonpub\.1$/));
  });

  it("downloads the schema-set template zip from the public template endpoint", async () => {
    // jsdom implements no object-URL APIs; triggerBlobDownload uses them.
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
    let templateUrl: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas/template")) {
          templateUrl = url;
          // downloadFile() reads .blob() and the Content-Disposition header.
          return {
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["zip"]),
            headers: {
              get: (name: string) =>
                name.toLowerCase() === "content-disposition"
                  ? 'attachment; filename="udmi-schema-template-1.5.2.zip"'
                  : null,
            },
          } as unknown as Response;
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    const downloadButton = await screen.findByRole("button", {
      name: "Download schema template (1.5.2)",
    });
    fireEvent.click(downloadButton);

    await waitFor(() => expect(templateUrl).toMatch(/\/udmi\/schemas\/template$/));
    // The blob actually reached the browser's download path.
    expect(URL.createObjectURL).toHaveBeenCalled();
  });
});

// Step visibility is CSS-driven (.module-steps > [data-stepgroup] { display:none }
// in the theme), and jsdom does not load that stylesheet. So these tests assert
// on the data-step / data-stepgroup attributes that drive it, never on
// toBeVisible() — which would pass vacuously here.
function stepOf() {
  return document.querySelector(".module-steps")?.getAttribute("data-step");
}

describe("ModulePage run retention", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  // What GET /runs?job_type=ip_discovery&status=succeeded&limit=1 returns: a
  // JobSummary, not the full RunRecord.
  const ipRunSummary = {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    stage: "register_comparison",
    progress_percent: 100,
    created_at: "2026-06-11T09:00:00Z",
    updated_at: "2026-06-11T09:05:00Z",
    edge_id: null,
  };

  // Serves the last-succeeded-run lookup per job type, so an ip-scanner-sct render
  // rehydrates run-ip-1 while every other head finds nothing of its own.
  function stubWithLastRun() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: url.includes("job_type=ip_discovery") ? [ipRunSummary] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("re-attaches the last succeeded run on arrival without the operator running anything", async () => {
    stubWithLastRun();
    renderModule("ip-scanner-sct");

    // No Run click anywhere in this test: the monitor comes back on its own.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    expect((await screen.findAllByText("run-ip-1")).length).toBeGreaterThan(0);
  });

  it("leaves a restored run on the Setup step instead of hijacking it to Results", async () => {
    stubWithLastRun();
    renderModule("ip-scanner-sct");

    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    // The operator came here to set something up. A run they did not just start
    // must not yank them to Results, even though it is terminal-succeeded.
    expect(stepOf()).toBe("setup");
    // Give the terminal-success effect a chance to fire before trusting that.
    await waitFor(() => expect(screen.getAllByText("run-ip-1").length).toBeGreaterThan(0));
    expect(stepOf()).toBe("setup");
  });

  it("shows the restored run's live results once the operator clicks through to Results", async () => {
    stubWithLastRun();
    renderModule("ip-scanner-sct");
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();

    // Results is one click away and holds the real rows from the restored run.
    fireEvent.click(screen.getByRole("button", { name: /Results/i }));
    await waitFor(() => expect(stepOf()).toBe("results"));
    expect((await screen.findAllByText("plant-controller")).length).toBeGreaterThan(0);
  });

  it("offers Generate report from this run for a restored terminal run", async () => {
    stubWithLastRun();
    renderModule("ip-scanner-sct");

    // A restored run satisfies the engineer + terminal gates just like a fresh
    // one, so the report affordance survives navigating away and back — both
    // the run-monitor copy and the end-of-Results copy.
    expect(
      await screen.findAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(2);
  });

  it("never bleeds one head's restored run into another head", async () => {
    stubWithLastRun();
    setApiKey("engineer-key");
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    const tree = (route: string) => (
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute={route} />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    );
    const view = render(tree("ip-scanner-sct"));
    expect((await screen.findAllByText("run-ip-1")).length).toBeGreaterThan(0);

    // Sibling ModulePage routes share one component instance (no key prop), so
    // this rerender — not a remount — is the real cross-head bleed vector.
    // MQTT has no succeeded run of its own, so nothing may be re-attached.
    view.rerender(tree("mqtt-discovery-sct"));
    await waitFor(() => expect(screen.queryByText("run-ip-1")).not.toBeInTheDocument());
    expect(screen.queryByText(/Discovery run monitor/i)).not.toBeInTheDocument();
    expect(stepOf()).toBe("setup");
  });

  it("replaces a cached terminal seed when the server returns a newer terminal run", async () => {
    let latestIpRun = ipRunSummary;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: url.includes("job_type=ip_discovery") ? [latestIpRun] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.includes("/api/v1/discovery/runs/") && url.endsWith("/results")) {
          const runId = url.includes("run-ip-2") ? "run-ip-2" : "run-ip-1";
          return jsonResponse({ ...resultsPayload, run_id: runId });
        }
        if (url.includes("/api/v1/discovery/runs/")) {
          const runId = url.includes("run-ip-2") ? "run-ip-2" : "run-ip-1";
          return jsonResponse({ ...terminalRun, run_id: runId });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    setApiKey("engineer-key");
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    const tree = (route: string) => (
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute={route} />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    );
    const view = render(tree("ip-scanner-sct"));
    expect((await screen.findAllByText("run-ip-1")).length).toBeGreaterThan(0);

    latestIpRun = {
      ...ipRunSummary,
      run_id: "run-ip-2",
      created_at: "2026-06-11T10:00:00Z",
      updated_at: "2026-06-11T10:05:00Z",
    };
    view.rerender(tree("mqtt-discovery-sct"));
    await waitFor(() => expect(screen.queryByText("run-ip-1")).not.toBeInTheDocument());
    view.rerender(tree("ip-scanner-sct"));

    expect((await screen.findAllByText("run-ip-2")).length).toBeGreaterThan(0);
    expect(screen.queryByText("run-ip-1")).not.toBeInTheDocument();
  });

  it("still advances an operator-started run to the Run step", async () => {
    // This run stays non-terminal, isolating the queued -> Run advance from the
    // succeeded -> Results one the discovery wiring suite covers.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse({ ...terminalRun, status: "running", progress_percent: 40 });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    expect(stepOf()).toBe("setup");

    const runButton = await prepareAuthorizedIpRun();
    fireEvent.click(runButton);

    await waitFor(() => expect(stepOf()).toBe("run"));
  });

  it("shows no results and no sample rows on a head that has never run", async () => {
    stubWithLastRun();
    renderModule("mqtt-discovery-sct");

    // "Boiler 1 Controller" is an old fixture row; nothing fabricated may stand
    // in for a run that never happened.
    expect(await screen.findByText("No results yet")).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Boiler 1 Controller")).not.toBeInTheDocument();
  });
});

describe("ModulePage progressive discovery observations", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    clearApiKey();
  });

  it("pauses the observation interval while SSE is driving and resumes it on fallback", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-sse-poll",
      status: "running",
      stage: "probing",
      progress_percent: 30,
      result_summary: {},
    };
    const activeStream = controlledSseStream();
    const fallbackStream = controlledSseStream();
    let streamRequests = 0;
    let observationRequests = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-sse-poll/events")) {
          streamRequests += 1;
          return streamRequests === 1 ? activeStream.response : fallbackStream.response;
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-sse-poll/observations?")) {
          observationRequests += 1;
          return jsonResponse({
            run_id: "run-ip-sse-poll",
            attempt: 8,
            observations: [
              {
                cursor: 1,
                run_id: "run-ip-sse-poll",
                attempt: 8,
                protocol: "ip",
                entity_kind: "host",
                entity_key: "host:192.0.2.80",
                entity_version: 1,
                event_key: "host:192.0.2.80:v1",
                phase: "reachability",
                outcome: "observed",
                payload_schema_version: "1.0",
                payload: {
                  projection_v1: {
                    collection: "devices",
                    record: {
                      hostname: "sse-poll-controller",
                      ip_address: "192.0.2.80",
                      observed_ports: [],
                    },
                  },
                },
                payload_sha256: "8".repeat(64),
                observed_at: "2026-08-11T06:00:00Z",
                created_at: "2026-08-11T06:00:01Z",
              },
            ],
            next_cursor: 1,
            latest_cursor: 1,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-sse-poll")) {
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    await waitFor(() => expect(observationRequests).toBeGreaterThan(0));

    await act(async () => {
      activeStream.push(
        `data: ${JSON.stringify({
          run_id: "run-ip-sse-poll",
          status: "running",
          latest_observation_cursor: 1,
        })}\n\n`,
      );
    });
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
        "Live updates are connected",
      ),
    );
    const requestsWhileSseConnected = observationRequests;
    await act(async () => new Promise((resolve) => setTimeout(resolve, 1_750)));
    expect(observationRequests).toBe(requestsWhileSseConnected);

    await act(async () => {
      activeStream.push(
        `event: timeout\ndata: ${JSON.stringify({
          run_id: "run-ip-sse-poll",
          status: "running",
          latest_observation_cursor: 1,
        })}\n\n`,
      );
    });
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
        "Connection interrupted",
      ),
    );
    await waitFor(() => expect(observationRequests).toBeGreaterThan(requestsWhileSseConnected), {
      timeout: 2_500,
    });
  });

  it("scopes a newly accepted discovery run into the URL for exact reload reattachment", async () => {
    const runningRun = {
      ...terminalRun,
      status: "running",
      stage: "probing",
      progress_percent: 5,
      result_summary: {},
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(runningRun);
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-1/observations?")) {
          return jsonResponse({
            run_id: "run-ip-1",
            attempt: 1,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct", "/ip-scanner-sct");
    const runButton = await prepareAuthorizedIpRun();
    fireEvent.click(runButton);

    await waitFor(() =>
      expect(screen.getByTestId("test-location")).toHaveTextContent("/ip-scanner-sct?run=run-ip-1"),
    );
  });

  it("drains acknowledged cursor pages for a restored active IP run and keeps start disabled", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-live",
      status: "running",
      stage: "probing",
      progress_percent: 35,
      result_summary: {},
    };
    const observation = (
      cursor: number,
      address: string,
      hostname: string,
    ): DiscoveryObservationRecord => ({
      cursor,
      run_id: "run-ip-live",
      attempt: 4,
      protocol: "ip",
      entity_kind: "host",
      entity_key: `host:${address}`,
      entity_version: 1,
      event_key: `host:${address}:v1`,
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            asset_id: null,
            hostname,
            ip_address: address,
            last_seen_at: "2026-08-11T05:00:00Z",
            match_basis: "ip",
            observed_ports: [],
            status_detail: "responsive",
          },
        },
      },
      payload_sha256: cursor === 1 ? "a".repeat(64) : "b".repeat(64),
      observed_at: "2026-08-11T05:00:00Z",
      created_at: "2026-08-11T05:00:01Z",
    });
    const observationRequests: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: [
              {
                ...runningRun,
                edge_id: null,
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/discovery/runs/run-ip-live/observations?")) {
          observationRequests.push(url);
          if (url.includes("after=0")) {
            return jsonResponse({
              run_id: "run-ip-live",
              attempt: 4,
              observations: [observation(1, "192.0.2.8", "ahu-eight")],
              next_cursor: 1,
              latest_cursor: 9,
              has_more: true,
              terminal: null,
              observations_pruned: false,
            });
          }
          if (url.includes("after=1")) {
            return jsonResponse({
              run_id: "run-ip-live",
              attempt: 4,
              observations: [observation(2, "192.0.2.9", "ahu-nine")],
              next_cursor: 2,
              latest_cursor: 9,
              has_more: false,
              terminal: null,
              observations_pruned: false,
            });
          }
          return jsonResponse({
            run_id: "run-ip-live",
            attempt: 4,
            observations: [],
            next_cursor: 2,
            latest_cursor: 9,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-live")) {
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    expect((await screen.findAllByText("ahu-eight")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("ahu-nine")).length).toBeGreaterThan(0);
    expect(observationRequests[0]).toContain("after=0&limit=100");
    expect(observationRequests[1]).toContain("after=1&limit=100");
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      "2 observations loaded. Catching up.",
    );
    expect(screen.getByRole("button", { name: "Stop run" })).toBeEnabled();
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Run" })).toHaveAttribute(
      "title",
      "A run is already in progress. Stop it before starting another.",
    );
  });

  it("refetches terminal metadata when the terminal cursor equals the acknowledged cursor", async () => {
    const stream = controlledSseStream();
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-equal-terminal",
      status: "running",
      stage: "probing",
      progress_percent: 60,
      result_summary: {},
    };
    const sealedRun = {
      ...terminalRun,
      run_id: "run-ip-equal-terminal",
      result_summary: {
        observation_evidence_v1: {
          attempt: 2,
          observation_count: 1,
          terminal_cursor: 1,
        },
      },
    };
    const observation: DiscoveryObservationRecord = {
      cursor: 1,
      run_id: "run-ip-equal-terminal",
      attempt: 2,
      protocol: "ip",
      entity_kind: "host",
      entity_key: "host:192.0.2.61",
      entity_version: 1,
      event_key: "host:192.0.2.61:v1",
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            hostname: "equal-cursor-controller",
            ip_address: "192.0.2.61",
            observed_ports: [],
          },
        },
      },
      payload_sha256: "6".repeat(64),
      observed_at: "2026-08-11T06:00:00Z",
      created_at: "2026-08-11T06:00:01Z",
    };
    let terminal = false;
    let observationRequests = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-equal-terminal/events")) return stream.response;
        if (url.endsWith("/api/v1/discovery/runs/run-ip-equal-terminal/results")) {
          return jsonResponse({ ...resultsPayload, run_id: "run-ip-equal-terminal" });
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-equal-terminal/observations?")) {
          observationRequests += 1;
          const after = new URL(url, "http://localhost").searchParams.get("after");
          if (after === "0") {
            return jsonResponse({
              run_id: "run-ip-equal-terminal",
              attempt: 2,
              observations: [observation],
              next_cursor: 1,
              latest_cursor: 1,
              has_more: false,
              terminal: null,
              observations_pruned: false,
            });
          }
          return jsonResponse({
            run_id: "run-ip-equal-terminal",
            attempt: 2,
            observations: [],
            next_cursor: 1,
            latest_cursor: 1,
            has_more: false,
            terminal: terminal ? { status: "succeeded", terminal_cursor: 1 } : null,
            observations_pruned: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-equal-terminal")) {
          return jsonResponse(terminal ? sealedRun : runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    expect((await screen.findAllByText("equal-cursor-controller")).length).toBeGreaterThan(0);
    await waitFor(() => expect(observationRequests).toBe(2));

    terminal = true;
    stream.push(
      `event: terminal\ndata: ${JSON.stringify({
        run_id: "run-ip-equal-terminal",
        status: "succeeded",
        latest_observation_cursor: 1,
      })}\n\n`,
    );

    await waitFor(() => expect(observationRequests).toBe(3));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
        "Sealed results loaded",
      ),
    );
    await act(async () => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(observationRequests).toBe(3);
    stream.close();
  });

  it("loads terminal metadata once for a zero-observation run", async () => {
    const stream = controlledSseStream();
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-zero-terminal",
      status: "running",
      stage: "probing",
      progress_percent: 80,
      result_summary: {},
    };
    const sealedRun = {
      ...terminalRun,
      run_id: "run-ip-zero-terminal",
      result_summary: {
        observation_evidence_v1: {
          attempt: 5,
          observation_count: 0,
          terminal_cursor: 0,
        },
      },
    };
    let terminal = false;
    let observationRequests = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-zero-terminal/events")) return stream.response;
        if (url.endsWith("/api/v1/discovery/runs/run-ip-zero-terminal/results")) {
          return jsonResponse({
            ...resultsPayload,
            run_id: "run-ip-zero-terminal",
            discovered_assets: [],
          });
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-zero-terminal/observations?")) {
          observationRequests += 1;
          return jsonResponse({
            run_id: "run-ip-zero-terminal",
            attempt: 5,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: terminal ? { status: "succeeded", terminal_cursor: 0 } : null,
            observations_pruned: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-zero-terminal")) {
          return jsonResponse(terminal ? sealedRun : runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    await waitFor(() => expect(observationRequests).toBe(1));
    terminal = true;
    stream.push(
      `event: terminal\ndata: ${JSON.stringify({
        run_id: "run-ip-zero-terminal",
        status: "succeeded",
        latest_observation_cursor: 0,
      })}\n\n`,
    );

    await waitFor(() => expect(observationRequests).toBe(2));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
        "Sealed results loaded",
      ),
    );
    await act(async () => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(observationRequests).toBe(2);
    stream.close();
  });

  it("reattaches the exact scoped run from the URL before asking for the latest run", async () => {
    const requestedRun = {
      ...terminalRun,
      run_id: "run-ip-requested",
      status: "running",
      stage: "probing",
      progress_percent: 25,
      result_summary: {},
    };
    let latestRequests = 0;
    let exactRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          latestRequests += 1;
          return jsonResponse({
            runs: [{ ...requestedRun, run_id: "run-ip-newer", edge_id: null }],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/runs/run-ip-requested")) {
          exactRequests += 1;
          return jsonResponse(requestedRun);
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-requested/observations?")) {
          return jsonResponse({
            run_id: "run-ip-requested",
            attempt: 2,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct", "/ip-scanner-sct?run=run-ip-requested");

    expect((await screen.findAllByText("run-ip-requested")).length).toBeGreaterThan(0);
    expect(screen.queryByText("run-ip-newer")).not.toBeInTheDocument();
    expect(exactRequests).toBeGreaterThan(0);
    expect(latestRequests).toBe(0);
  });

  it("clears an inaccessible run link and falls back without revealing whether that run exists", async () => {
    const accessibleRun = {
      ...terminalRun,
      run_id: "run-ip-accessible",
      status: "running",
      stage: "probing",
      progress_percent: 10,
      result_summary: {},
    };
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        requests.push(url);
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/runs/run-ip-hidden")) {
          return {
            ok: false,
            status: 404,
            statusText: "Not Found",
            json: async () => ({ detail: "Run not found" }),
          } as unknown as Response;
        }
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...accessibleRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-accessible")) {
          return jsonResponse(accessibleRun);
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-accessible/observations?")) {
          return jsonResponse({
            run_id: "run-ip-accessible",
            attempt: 1,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct", "/ip-scanner-sct?run=run-ip-hidden");

    expect((await screen.findAllByText("run-ip-accessible")).length).toBeGreaterThan(0);
    expect(screen.queryByText("run-ip-hidden")).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "The requested run is not available in this workspace. Showing the latest accessible run.",
    );
    const exactIndex = requests.findIndex((url) =>
      url.endsWith("/api/v1/discovery/runs/run-ip-hidden"),
    );
    const latestIndex = requests.findIndex((url) => url.includes("/api/v1/runs?"));
    expect(exactIndex).toBeGreaterThanOrEqual(0);
    expect(latestIndex).toBeGreaterThan(exactIndex);
  });

  it("keeps provisional rows until the whole terminal cursor is folded", async () => {
    const sealedRun = {
      ...terminalRun,
      run_id: "run-ip-sealed",
      result_summary: {
        observation_evidence_v1: {
          attempt: 3,
          observation_count: 3,
          terminal_cursor: 3,
        },
      },
    };
    const firstObservation: DiscoveryObservationRecord = {
      cursor: 1,
      run_id: "run-ip-sealed",
      attempt: 3,
      protocol: "ip",
      entity_kind: "host",
      entity_key: "host:192.0.2.20",
      entity_version: 1,
      event_key: "host:192.0.2.20:v1",
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            asset_id: null,
            hostname: "provisional-controller",
            ip_address: "192.0.2.20",
            observed_ports: [],
            status_detail: "responsive",
          },
        },
      },
      payload_sha256: "c".repeat(64),
      observed_at: "2026-08-11T05:00:00Z",
      created_at: "2026-08-11T05:00:01Z",
    };
    const selectedObservation: DiscoveryObservationRecord = {
      ...firstObservation,
      cursor: 2,
      entity_key: "host:192.0.2.21",
      event_key: "host:192.0.2.21:v1",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            asset_id: null,
            hostname: "provisional-selected-controller",
            ip_address: "192.0.2.21",
            observed_ports: [],
            status_detail: "responsive",
          },
        },
      },
      payload_sha256: "1".repeat(64),
    };
    let releaseFinalPage!: (response: Response) => void;
    const finalPage = new Promise<Response>((resolve) => {
      releaseFinalPage = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...sealedRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/runs/run-ip-sealed/results")) {
          return jsonResponse({
            ...resultsPayload,
            run_id: "run-ip-sealed",
            discovered_assets: [
              {
                ...resultsPayload.discovered_assets[0],
                hostname: "sealed-controller",
                ip_address: "192.0.2.20",
              },
              {
                ...resultsPayload.discovered_assets[0],
                hostname: "sealed-selected-controller",
                ip_address: "192.0.2.21",
              },
            ],
          });
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-sealed/observations?")) {
          if (url.includes("after=0")) {
            return jsonResponse({
              run_id: "run-ip-sealed",
              attempt: 3,
              observations: [firstObservation, selectedObservation],
              next_cursor: 2,
              latest_cursor: 3,
              has_more: true,
              terminal: { status: "succeeded", terminal_cursor: 3 },
              observations_pruned: false,
            });
          }
          return finalPage;
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-sealed")) {
          return jsonResponse(sealedRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    expect((await screen.findAllByText("provisional-controller")).length).toBeGreaterThan(0);
    const provisionalSelected = (
      await screen.findAllByText("provisional-selected-controller")
    ).find((element) => element.closest("tr"));
    const provisionalSelectedRow = provisionalSelected?.closest("tr") ?? null;
    expect(provisionalSelectedRow).not.toBeNull();
    fireEvent.click(
      within(provisionalSelectedRow as HTMLTableRowElement).getByRole("button", {
        name: "Select evidence",
      }),
    );
    expect(screen.queryByText("sealed-controller")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      "2 terminal observations loaded. Catching up before sealed results are shown.",
    );

    releaseFinalPage(
      jsonResponse({
        run_id: "run-ip-sealed",
        attempt: 3,
        observations: [
          {
            ...firstObservation,
            cursor: 3,
            entity_kind: "diagnostic",
            entity_key: "diagnostic:complete",
            event_key: "diagnostic:complete:v1",
            payload: { message: "complete" },
            payload_sha256: "d".repeat(64),
          },
        ],
        next_cursor: 3,
        latest_cursor: 3,
        has_more: false,
        terminal: { status: "succeeded", terminal_cursor: 3 },
        observations_pruned: false,
      }),
    );

    expect((await screen.findAllByText("sealed-controller")).length).toBeGreaterThan(0);
    expect(screen.queryByText("provisional-controller")).not.toBeInTheDocument();
    const sealedSelected = (await screen.findAllByText("sealed-selected-controller")).find(
      (element) => element.closest("tr"),
    );
    const sealedSelectedRow = sealedSelected?.closest("tr") ?? null;
    expect(sealedSelectedRow).not.toBeNull();
    expect(
      within(sealedSelectedRow as HTMLTableRowElement).getByRole("button", {
        name: "Select evidence",
      }),
    ).toHaveAttribute("aria-pressed", "true");
    const sealedFirst = (await screen.findAllByText("sealed-controller")).find((element) =>
      element.closest("tr"),
    );
    const sealedFirstRow = sealedFirst?.closest("tr") ?? null;
    expect(sealedFirstRow).not.toBeNull();
    expect(
      within(sealedFirstRow as HTMLTableRowElement).getByRole("button", {
        name: "Select evidence",
      }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      "Sealed results loaded",
    );
  });

  it("drops expired provisional history and crosses directly to sealed results", async () => {
    const prunedRun = {
      ...terminalRun,
      run_id: "run-ip-pruned",
      result_summary: {
        observation_evidence_v1: {
          attempt: 5,
          observation_count: 3,
          terminal_cursor: 3,
        },
      },
    };
    const provisional: DiscoveryObservationRecord = {
      cursor: 1,
      run_id: "run-ip-pruned",
      attempt: 5,
      protocol: "ip",
      entity_kind: "host",
      entity_key: "host:192.0.2.30",
      entity_version: 1,
      event_key: "host:192.0.2.30:v1",
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            hostname: "expired-provisional-controller",
            ip_address: "192.0.2.30",
            observed_ports: [],
          },
        },
      },
      payload_sha256: "e".repeat(64),
      observed_at: "2026-07-01T05:00:00Z",
      created_at: "2026-07-01T05:00:01Z",
    };
    let releasePrunedMarker!: (response: Response) => void;
    const prunedMarker = new Promise<Response>((resolve) => {
      releasePrunedMarker = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...prunedRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/runs/run-ip-pruned/results")) {
          return jsonResponse({
            ...resultsPayload,
            run_id: "run-ip-pruned",
            discovered_assets: [
              {
                ...resultsPayload.discovered_assets[0],
                hostname: "retained-sealed-controller",
                ip_address: "192.0.2.30",
              },
            ],
          });
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-pruned/observations?")) {
          if (url.includes("after=0")) {
            return jsonResponse({
              run_id: "run-ip-pruned",
              attempt: 5,
              observations: [provisional],
              next_cursor: 1,
              latest_cursor: 3,
              has_more: true,
              terminal: { status: "succeeded", terminal_cursor: 3 },
              observations_pruned: false,
            });
          }
          return prunedMarker;
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-pruned")) {
          return jsonResponse(prunedRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    expect((await screen.findAllByText("expired-provisional-controller")).length).toBeGreaterThan(
      0,
    );

    releasePrunedMarker(
      jsonResponse({
        run_id: "run-ip-pruned",
        attempt: 5,
        observations: [],
        next_cursor: 1,
        latest_cursor: 3,
        has_more: false,
        terminal: { status: "succeeded", terminal_cursor: 3 },
        observations_pruned: true,
      }),
    );

    expect((await screen.findAllByText("retained-sealed-controller")).length).toBeGreaterThan(0);
    expect(screen.queryByText("expired-provisional-controller")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      /^Provisional history expired; sealed results loaded\.$/,
    );
    for (const storage of [window.localStorage, window.sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const value = storage.getItem(storage.key(index) ?? "") ?? "";
        expect(value).not.toContain("expired-provisional-controller");
        expect(value).not.toContain("run-ip-pruned");
      }
    }
  });

  it("shows integrity quarantine copy instead of retention-expiry copy", async () => {
    const quarantinedRun = {
      ...terminalRun,
      run_id: "run-ip-quarantined",
      result_summary: {
        observation_evidence_v1: {
          attempt: 6,
          observation_count: 2,
          terminal_cursor: 2,
        },
      },
    };
    const provisional: DiscoveryObservationRecord = {
      cursor: 1,
      run_id: "run-ip-quarantined",
      attempt: 6,
      protocol: "ip",
      entity_kind: "host",
      entity_key: "host:192.0.2.31",
      entity_version: 1,
      event_key: "host:192.0.2.31:v1",
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            hostname: "quarantined-provisional-controller",
            ip_address: "192.0.2.31",
            observed_ports: [],
          },
        },
      },
      payload_sha256: "3".repeat(64),
      observed_at: "2026-08-11T05:00:00Z",
      created_at: "2026-08-11T05:00:01Z",
    };
    let releaseQuarantineMarker!: (response: Response) => void;
    const quarantineMarker = new Promise<Response>((resolve) => {
      releaseQuarantineMarker = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...quarantinedRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/runs/run-ip-quarantined/results")) {
          return jsonResponse({
            ...resultsPayload,
            run_id: "run-ip-quarantined",
            discovered_assets: [
              {
                ...resultsPayload.discovered_assets[0],
                hostname: "quarantined-sealed-controller",
                ip_address: "192.0.2.31",
              },
            ],
          });
        }
        if (url.includes("/api/v1/discovery/runs/run-ip-quarantined/observations?")) {
          if (url.includes("after=0")) {
            return jsonResponse({
              run_id: "run-ip-quarantined",
              attempt: 6,
              observations: [provisional],
              next_cursor: 1,
              latest_cursor: 2,
              has_more: true,
              terminal: { status: "succeeded", terminal_cursor: 2 },
              observations_pruned: false,
              observations_quarantined: false,
            });
          }
          return quarantineMarker;
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-quarantined")) {
          return jsonResponse(quarantinedRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    expect(
      (await screen.findAllByText("quarantined-provisional-controller")).length,
    ).toBeGreaterThan(0);

    releaseQuarantineMarker(
      jsonResponse({
        run_id: "run-ip-quarantined",
        attempt: 6,
        observations: [],
        next_cursor: 1,
        latest_cursor: 2,
        has_more: false,
        terminal: { status: "succeeded", terminal_cursor: 2 },
        observations_pruned: false,
        observations_quarantined: true,
      }),
    );

    expect((await screen.findAllByText("quarantined-sealed-controller")).length).toBeGreaterThan(0);
    expect(screen.queryByText("quarantined-provisional-controller")).not.toBeInTheDocument();
    const connectionStatus = screen.getByRole("status", { name: "Discovery connection" });
    expect(connectionStatus).toHaveTextContent(
      /^Provisional observations were quarantined after an integrity check; sealed results loaded\.$/,
    );
    expect(connectionStatus).not.toHaveTextContent("expired");
  });

  it("keeps the selected entity attached when a later page replaces its projection", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-selection",
      status: "running",
      stage: "probing",
      progress_percent: 45,
      result_summary: {},
    };
    const observation = (
      cursor: number,
      entityKey: string,
      entityVersion: number,
      address: string,
      hostname: string,
    ): DiscoveryObservationRecord => ({
      cursor,
      run_id: "run-ip-selection",
      attempt: 6,
      protocol: "ip",
      entity_kind: "host",
      entity_key: entityKey,
      entity_version: entityVersion,
      event_key: `${entityKey}:v${entityVersion}`,
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            hostname,
            ip_address: address,
            observed_ports: [],
            status_detail: "responsive",
          },
        },
      },
      payload_sha256: String(cursor).repeat(64),
      observed_at: "2026-08-11T05:00:00Z",
      created_at: "2026-08-11T05:00:01Z",
    });
    let releaseReplacement!: (response: Response) => void;
    const replacementPage = new Promise<Response>((resolve) => {
      releaseReplacement = resolve;
    });
    const stream = controlledSseStream();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-selection/events")) return stream.response;
        if (url.includes("/api/v1/discovery/runs/run-ip-selection/observations?")) {
          if (url.includes("after=0")) {
            return jsonResponse({
              run_id: "run-ip-selection",
              attempt: 6,
              observations: [
                observation(1, "host:first", 1, "192.0.2.40", "first-controller"),
                observation(2, "host:selected", 1, "192.0.2.41", "selected-before"),
              ],
              next_cursor: 2,
              latest_cursor: 3,
              has_more: true,
              terminal: null,
              observations_pruned: false,
            });
          }
          return replacementPage;
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-selection")) {
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    const selectedBefore = await screen.findByText("selected-before");
    const selectedBeforeRow = selectedBefore.closest("tr");
    expect(selectedBeforeRow).not.toBeNull();
    const selectedBeforeButton = within(selectedBeforeRow as HTMLTableRowElement).getByRole(
      "button",
      { name: "Select evidence" },
    );
    fireEvent.click(selectedBeforeButton);
    await waitFor(() => expect(selectedBeforeButton).toHaveAttribute("aria-pressed", "true"));

    releaseReplacement(
      jsonResponse({
        run_id: "run-ip-selection",
        attempt: 6,
        observations: [observation(3, "host:selected", 2, "192.0.2.41", "selected-after")],
        next_cursor: 3,
        latest_cursor: 3,
        has_more: false,
        terminal: null,
        observations_pruned: false,
      }),
    );

    const selectedAfter = (await screen.findAllByText("selected-after")).find((element) =>
      element.closest("tr"),
    );
    const selectedAfterRow = selectedAfter?.closest("tr") ?? null;
    expect(selectedAfterRow).not.toBeNull();
    await waitFor(() =>
      expect(
        within(selectedAfterRow as HTMLTableRowElement).getByRole("button", {
          name: "Select evidence",
        }),
      ).toHaveAttribute("aria-pressed", "true"),
    );
    const firstRow = screen.getByText("first-controller").closest("tr");
    expect(firstRow).not.toBeNull();
    expect(
      within(firstRow as HTMLTableRowElement).getByRole("button", { name: "Select evidence" }),
    ).toHaveAttribute("aria-pressed", "false");
    stream.close();
  });

  it("clears provisional evidence and focuses the page heading when scoped access closes", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-closed",
      status: "running",
      stage: "probing",
      progress_percent: 45,
      result_summary: {},
    };
    const stream = controlledSseStream();
    const provisional: DiscoveryObservationRecord = {
      cursor: 1,
      run_id: "run-ip-closed",
      attempt: 7,
      protocol: "ip",
      entity_kind: "host",
      entity_key: "host:closed",
      entity_version: 1,
      event_key: "host:closed:v1",
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            hostname: "private-closed-controller",
            ip_address: "192.0.2.50",
            observed_ports: [],
          },
        },
      },
      payload_sha256: "f".repeat(64),
      observed_at: "2026-08-11T05:00:00Z",
      created_at: "2026-08-11T05:00:01Z",
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-closed/events")) return stream.response;
        if (url.includes("/api/v1/discovery/runs/run-ip-closed/observations?")) {
          return jsonResponse({
            run_id: "run-ip-closed",
            attempt: 7,
            observations: [provisional],
            next_cursor: 1,
            latest_cursor: 1,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-closed")) {
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");

    expect((await screen.findAllByText("private-closed-controller")).length).toBeGreaterThan(0);
    stream.push(
      `event: closed\ndata: ${JSON.stringify({ run_id: "run-ip-closed", status: "closed" })}\n\n`,
    );
    stream.close();

    await waitFor(() =>
      expect(screen.queryByText("private-closed-controller")).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      "Access changed. Live run evidence is no longer available in this workspace.",
    );
    expect(screen.getAllByRole("status", { name: "Discovery connection" })).toHaveLength(1);
    const heading = screen.getByRole("heading", { name: "IP Discovery", level: 2 });
    expect(heading).toHaveFocus();
    fireEvent.click(screen.getByLabelText(/Dry run/i));
    const previewButton = screen.getByRole("button", { name: "Preview" });
    expect(previewButton).toBeDisabled();
    expect(previewButton).toHaveAttribute(
      "title",
      "Run access for this workspace is closed. Reopen the module before starting another run.",
    );
  });

  it("preserves colliding evidence when moving away from a closed workspace scope", async () => {
    const sessionScopeId = "session-workspace-transition" as SessionScopeId;
    const firstWorkspace: WorkspaceRef = { projectId: "project-a", siteId: "site-a" };
    const secondWorkspace: WorkspaceRef = { projectId: "project-b", siteId: "site-b" };
    const sharedRunId = "run-ip-workspace-collision";
    const runningRun = {
      ...terminalRun,
      run_id: sharedRunId,
      status: "running",
      stage: "probing",
      progress_percent: 30,
      result_summary: {},
    };
    const stream = controlledSseStream();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith(`/api/v1/runs/${sharedRunId}/events`)) return stream.response;
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) {
          return jsonResponse(runningRun);
        }
        if (url.includes(`/api/v1/discovery/runs/${sharedRunId}/observations?`)) {
          return jsonResponse({
            run_id: sharedRunId,
            attempt: 1,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    const sessionValue = (workspace: WorkspaceRef): SessionContextValue => ({
      apiClient: createSessionBoundApiClient(sessionScopeId, workspace, "engineer-key"),
      authorizationEnforced: true,
      canAdmin: false,
      canEngineer: true,
      error: null,
      hasApiKey: true,
      isLoading: false,
      me: {
        effective_scopes: [],
        global_scope: true,
        role: "engineer",
        source: "user_key",
        username: "engineer-1",
      },
      role: "engineer",
      sessionScopeId,
      signIn: vi.fn(),
      signOut: vi.fn(),
      workspace,
    });
    const tree = (workspace: WorkspaceRef) => (
      <QueryClientProvider client={queryClient}>
        <SessionContext.Provider value={sessionValue(workspace)}>
          <MemoryRouter initialEntries={["/"]}>
            <ModulePage moduleRoute="ip-scanner-sct" />
          </MemoryRouter>
        </SessionContext.Provider>
      </QueryClientProvider>
    );
    const view = render(tree(firstWorkspace));
    await screen.findByText(/Discovery run monitor/i);
    await waitFor(() => expect(screen.getAllByText(sharedRunId).length).toBeGreaterThan(0));
    stream.push(
      `event: closed\ndata: ${JSON.stringify({ run_id: sharedRunId, status: "closed" })}\n\n`,
    );
    stream.close();
    await screen.findByRole("status", { name: "Discovery connection" });

    const collidingRunRef: RunRef = {
      family: "discovery",
      jobType: "ip_discovery",
      module: "ip-scanner-sct",
      origin: "restored",
      runId: sharedRunId,
      sessionScopeId,
      workspace: secondWorkspace,
    };
    const collidingKey = [
      ...queryKeys.run(sessionScopeId, secondWorkspace, collidingRunRef),
      "epoch",
      777,
    ] as const;
    const collidingEvidence = { marker: "new-workspace-evidence" };
    queryClient.setQueryData(collidingKey, collidingEvidence);

    view.rerender(tree(secondWorkspace));
    await waitFor(() =>
      expect(queryClient.getQueryData(collidingKey)).toEqual(collidingEvidence),
    );
  });

  it("fences a delayed observation page after scoped access closes", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-delayed-closed",
      status: "running",
      stage: "probing",
      progress_percent: 45,
      result_summary: {},
    };
    const stream = controlledSseStream();
    const delayedObservation: DiscoveryObservationRecord = {
      cursor: 1,
      run_id: "run-ip-delayed-closed",
      attempt: 9,
      protocol: "ip",
      entity_kind: "host",
      entity_key: "host:delayed-closed",
      entity_version: 1,
      event_key: "host:delayed-closed:v1",
      phase: "reachability",
      outcome: "observed",
      payload_schema_version: "1.0",
      payload: {
        projection_v1: {
          collection: "devices",
          record: {
            hostname: "late-private-controller",
            ip_address: "192.0.2.71",
            observed_ports: [],
          },
        },
      },
      payload_sha256: "7".repeat(64),
      observed_at: "2026-08-11T05:00:00Z",
      created_at: "2026-08-11T05:00:01Z",
    };
    let observationRequested = false;
    let releaseObservation!: (response: Response) => void;
    const delayedPage = new Promise<Response>((resolve) => {
      releaseObservation = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-delayed-closed/events")) return stream.response;
        if (url.includes("/api/v1/discovery/runs/run-ip-delayed-closed/observations?")) {
          observationRequested = true;
          return delayedPage;
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-delayed-closed")) {
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    await waitFor(() => expect(observationRequested).toBe(true));

    stream.push(
      `event: closed\ndata: ${JSON.stringify({
        run_id: "run-ip-delayed-closed",
        status: "closed",
      })}\n\n`,
    );
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
        "Access changed",
      ),
    );

    await act(async () => {
      releaseObservation(
        jsonResponse({
          run_id: "run-ip-delayed-closed",
          attempt: 9,
          observations: [delayedObservation],
          next_cursor: 1,
          latest_cursor: 1,
          has_more: false,
          terminal: null,
          observations_pruned: false,
        }),
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(screen.queryByText("late-private-controller")).not.toBeInTheDocument();
    stream.close();
  });

  it("removes terminal sealed results when scoped access closes", async () => {
    const sealedRun = {
      ...terminalRun,
      run_id: "run-ip-terminal-closed",
      result_summary: {},
    };
    const stream = controlledSseStream();
    let resultsSignal: AbortSignal | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...sealedRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-terminal-closed/events")) return stream.response;
        if (url.endsWith("/api/v1/discovery/runs/run-ip-terminal-closed/results")) {
          resultsSignal = init?.signal ?? null;
          return new Promise<Response>(() => {
            // Access closure must abort and evict this epoch-scoped request.
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-terminal-closed")) {
          return jsonResponse(sealedRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const { queryClient } = renderModule("ip-scanner-sct");
    await waitFor(() => expect(resultsSignal).not.toBeNull());
    let activeResultsQueryKey: readonly unknown[] = [];
    await waitFor(() =>
      expect(
        queryClient
          .getQueryCache()
          .findAll({
            predicate: (query) =>
              query.queryKey.includes("results") && query.queryKey.includes("epoch"),
          }),
      ).toHaveLength(1),
    );
    activeResultsQueryKey = queryClient
      .getQueryCache()
      .findAll({
        predicate: (query) =>
          query.queryKey.includes("results") && query.queryKey.includes("epoch"),
      })[0].queryKey;
    const otherScopeResultsQueryKey = activeResultsQueryKey.map((part, index) => {
      if (index === 1) return "other-session";
      if (index === 4) return "other-project";
      if (index === 5) return "other-site";
      return part;
    });
    queryClient.setQueryData(otherScopeResultsQueryKey, { source: "other-workspace" });

    stream.push(
      `event: closed\ndata: ${JSON.stringify({
        run_id: "run-ip-terminal-closed",
        status: "closed",
      })}\n\n`,
    );

    await waitFor(() => expect(resultsSignal?.aborted).toBe(true));
    await waitFor(() =>
      expect(
        queryClient
          .getQueryCache()
          .find({ exact: true, queryKey: activeResultsQueryKey }),
      ).toBeUndefined(),
    );
    expect(
      queryClient.getQueryData(otherScopeResultsQueryKey),
    ).toEqual({ source: "other-workspace" });
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      "Access changed. Live run evidence is no longer available in this workspace.",
    );
    stream.close();
  });

  it("evicts and hides active BACnet points and comparison evidence on scoped closure only", async () => {
    const run = {
      ...terminalRun,
      run_id: "run-bacnet-closed",
      job_type: "bacnet_discovery",
      result_summary: {},
    };
    const stream = controlledSseStream();
    let pointsSignal: AbortSignal | null = null;
    let comparisonSignal: AbortSignal | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: url.includes("job_type=bacnet_discovery") ? [run] : [] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-bacnet-closed/events")) return stream.response;
        if (url.includes("/api/v1/discovery/runs/run-bacnet-closed/points")) {
          pointsSignal = init?.signal ?? null;
          return new Promise<Response>(() => {});
        }
        if (url.includes("/api/v1/discovery/runs/run-bacnet-closed/comparison?")) {
          comparisonSignal = init?.signal ?? null;
          return new Promise<Response>(() => {});
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-closed/results")) {
          return jsonResponse({ ...resultsPayload, run_id: "run-bacnet-closed", points: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-closed")) return jsonResponse(run);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const { queryClient } = renderModule("bacnet-discovery-sct", "/?compare=prior-run");
    await waitFor(() => expect(pointsSignal).not.toBeNull());
    await waitFor(() => expect(comparisonSignal).not.toBeNull());
    const activeAuxiliaryKeys = queryClient
      .getQueryCache()
      .findAll({ predicate: (query) => query.queryKey.includes("epoch") && query.queryKey.includes("run-bacnet-closed") })
      .map((query) => query.queryKey)
      .filter((key) => key.includes("bacnet-points") || key.includes("discovery-comparison"));
    expect(activeAuxiliaryKeys).toHaveLength(2);
    const foreignKey = activeAuxiliaryKeys[0].map((part, index) =>
      index === 1 ? "foreign-session" : index === 4 ? "foreign-project" : index === 5 ? "foreign-site" : part,
    );
    queryClient.setQueryData(foreignKey, { source: "foreign" });

    stream.push(`event: closed\ndata: ${JSON.stringify({ run_id: run.run_id, status: "closed" })}\n\n`);
    await waitFor(() => expect(pointsSignal?.aborted).toBe(true));
    await waitFor(() => expect(comparisonSignal?.aborted).toBe(true));
    await waitFor(() =>
      expect(
        queryClient.getQueryCache().findAll({ predicate: (query) => activeAuxiliaryKeys.some((key) => JSON.stringify(key) === JSON.stringify(query.queryKey)) }),
      ).toHaveLength(0),
    );
    expect(queryClient.getQueryData(foreignKey)).toEqual({ source: "foreign" });
    expect(screen.queryByRole("heading", { name: "Points / Live Data" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Sealed run against prior-run/i })).not.toBeInTheDocument();
    stream.close();
  });

  it("aborts and evicts epoch-scoped validation issues when scoped access closes", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-udmi-issues-closed",
      job_type: "udmi_validation",
      status: "running",
      stage: "capturing_live_mqtt",
      progress_percent: 25,
      result_summary: {},
    };
    const stream = controlledSseStream();
    let issuesSignal: AbortSignal | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: url.includes("job_type=udmi_validation") ? [runningRun] : [] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/runs/run-udmi-issues-closed/events")) return stream.response;
        if (url.endsWith("/api/v1/validation/runs/run-udmi-issues-closed/issues")) {
          issuesSignal = init?.signal ?? null;
          return new Promise<Response>(() => {
            // Access closure must cancel this epoch-specific issues request.
          });
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-issues-closed")) {
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const { queryClient } = renderModule("udmi-validation");
    await waitFor(() => expect(issuesSignal).not.toBeNull());
    stream.push(
      `event: closed\ndata: ${JSON.stringify({
        run_id: "run-udmi-issues-closed",
        status: "closed",
      })}\n\n`,
    );

    await waitFor(() => expect(issuesSignal?.aborted).toBe(true));
    await waitFor(() =>
      expect(
        queryClient
          .getQueryCache()
          .findAll({ predicate: (query) => query.queryKey.includes("issues") && query.queryKey.includes("epoch") }),
      ).toHaveLength(0),
    );
    stream.close();
  });

  it("stops run and observation polling after scoped access closes", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-ip-polling-closed",
      status: "running",
      stage: "probing",
      progress_percent: 45,
      result_summary: {},
    };
    const stream = controlledSseStream();
    let runRequests = 0;
    let observationRequests = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-ip-polling-closed/events")) return stream.response;
        if (url.includes("/api/v1/discovery/runs/run-ip-polling-closed/observations?")) {
          observationRequests += 1;
          return jsonResponse({
            run_id: "run-ip-polling-closed",
            attempt: 3,
            observations: [],
            next_cursor: 0,
            latest_cursor: 0,
            has_more: false,
            terminal: null,
            observations_pruned: false,
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-polling-closed")) {
          runRequests += 1;
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    await waitFor(() => {
      expect(runRequests).toBeGreaterThan(0);
      expect(observationRequests).toBeGreaterThan(0);
    });

    stream.push(
      `event: closed\ndata: ${JSON.stringify({
        run_id: "run-ip-polling-closed",
        status: "closed",
      })}\n\n`,
    );
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
        "Access changed",
      ),
    );
    const closedRunRequests = runRequests;
    const closedObservationRequests = observationRequests;

    await act(async () => new Promise((resolve) => setTimeout(resolve, 1_750)));
    expect(runRequests).toBe(closedRunRequests);
    expect(observationRequests).toBe(closedObservationRequests);
    stream.close();
  });

  it("stops MQTT topic polling after scoped access closes", async () => {
    const runningRun = {
      ...terminalRun,
      run_id: "run-mqtt-polling-closed",
      job_type: "mqtt_discovery",
      status: "running",
      stage: "capture",
      progress_percent: 45,
      result_summary: {},
    };
    const stream = controlledSseStream();
    let runRequests = 0;
    let topicRequests = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [{ ...runningRun, edge_id: null }] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-mqtt-polling-closed/events")) return stream.response;
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-polling-closed/topics")) {
          topicRequests += 1;
          return jsonResponse({ run_id: "run-mqtt-polling-closed", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-polling-closed")) {
          runRequests += 1;
          return jsonResponse(runningRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");
    await waitFor(() => {
      expect(runRequests).toBeGreaterThan(0);
      expect(topicRequests).toBeGreaterThan(0);
    });

    stream.push(
      `event: closed\ndata: ${JSON.stringify({
        run_id: "run-mqtt-polling-closed",
        status: "closed",
      })}\n\n`,
    );
    const heading = screen.getByRole("heading", { name: "MQTT Discovery", level: 2 });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getAllByRole("status", { name: "Discovery connection" })).toHaveLength(1);
    expect(screen.getByRole("status", { name: "Discovery connection" })).toHaveTextContent(
      "Access changed. Live run evidence is no longer available in this workspace.",
    );
    const closedRunRequests = runRequests;
    const closedTopicRequests = topicRequests;

    await act(async () => new Promise((resolve) => setTimeout(resolve, 2_250)));
    expect(runRequests).toBe(closedRunRequests);
    expect(topicRequests).toBe(closedTopicRequests);
    stream.close();
  });

  it("clears a report dialog intent and blocks a held submission after access closes", async () => {
    const run = { ...terminalRun, run_id: "run-report-closed", result_summary: {} };
    const stream = controlledSseStream();
    let reportPosts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: url.includes("job_type=ip_discovery") ? [{ ...run, edge_id: null }] : [] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-report-closed/events")) return stream.response;
        if (url.endsWith("/api/v1/discovery/runs/run-report-closed")) return jsonResponse(run);
        if (url.endsWith("/api/v1/discovery/runs/run-report-closed/results")) {
          return jsonResponse({ ...resultsPayload, run_id: run.run_id, result_summary: {} });
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          reportPosts += 1;
          return jsonResponse({ report_id: "unexpected", status: "succeeded" });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner-sct");
    const [reportButton] = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    fireEvent.click(reportButton);
    const dialog = await screen.findByRole("dialog", { name: "Name this validation report" });
    const form = dialog.querySelector("form");
    expect(form).not.toBeNull();

    stream.push(
      `event: closed\ndata: ${JSON.stringify({ run_id: run.run_id, status: "closed" })}\n\n`,
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Name this validation report" })).not.toBeInTheDocument());
    fireEvent.submit(form as HTMLFormElement);
    expect(reportPosts).toBe(0);
    stream.close();
  });

  it("stops a deferred Generate All mutation when access closes", async () => {
    const run = { ...terminalRun, run_id: "run-report-deferred-closed", result_summary: {} };
    const stream = controlledSseStream();
    let reportPosts = 0;
    let releaseFirstReport!: (response: Response) => void;
    const firstReport = new Promise<Response>((resolve) => {
      releaseFirstReport = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: url.includes("job_type=ip_discovery") ? [{ ...run, edge_id: null }] : [] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/runs/run-report-deferred-closed/events")) return stream.response;
        if (url.endsWith("/api/v1/discovery/runs/run-report-deferred-closed")) return jsonResponse(run);
        if (url.endsWith("/api/v1/discovery/runs/run-report-deferred-closed/results")) {
          return jsonResponse({ ...resultsPayload, run_id: run.run_id, result_summary: {} });
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          reportPosts += 1;
          return firstReport;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const { queryClient } = renderModule("ip-scanner-sct");
    const invalidateReports = vi.spyOn(queryClient, "invalidateQueries");
    const [formatPicker] = await screen.findAllByLabelText("Report format");
    fireEvent.change(formatPicker, { target: { value: "all" } });
    const [reportButton] = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    fireEvent.click(reportButton);
    const dialog = await screen.findByRole("dialog", { name: "Name this validation report" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Generate report" }));
    await waitFor(() => expect(reportPosts).toBe(1));

    stream.push(
      `event: closed\ndata: ${JSON.stringify({ run_id: run.run_id, status: "closed" })}\n\n`,
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Name this validation report" })).not.toBeInTheDocument());
    await act(async () => {
      releaseFirstReport(
        jsonResponse({
          file_name: "stale.pdf",
          output_format: "pdf",
          report_id: "stale-report",
          report_type: "ip_discovery",
          status: "succeeded",
        }),
      );
    });
    await waitFor(() => expect(reportPosts).toBe(1));
    expect(invalidateReports).not.toHaveBeenCalled();
    expect(screen.queryByText(/reports generated from this run|Report generated from this run/i)).not.toBeInTheDocument();
    stream.close();
  });
});

describe("ModulePage reports visibility", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const reportsPayload = {
    reports: [
      {
        report_id: "rep-1",
        report_type: "udmi_validation",
        output_format: "xlsx",
        status: "succeeded",
        file_name: "canihazcheezeburger_udmi_validation_rep-1.xlsx",
        report_title: "canihazcheezeburger",
        created_at: "2026-07-15T10:00:00Z",
        source_run_ids: ["run-1"],
      },
    ],
  };

  it("shows the Generated Reports table on arrival, before any step click", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse(reportsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");
    expect(
      await screen.findByLabelText(
        /Select report canihazcheezeburger_udmi_validation_rep-1\.xlsx/i,
      ),
    ).toBeInTheDocument();
    const reportName = screen
      .getByText("canihazcheezeburger")
      .closest(".report-name-cell") as HTMLElement;
    expect(within(reportName).getByText("rep-1")).toBeInTheDocument();

    expect(
      screen.getByTitle("Download canihazcheezeburger_udmi_validation_rep-1.xlsx"),
    ).toBeInTheDocument();
    // The page lands on Setup and stays there, so the reports table has to be
    // in the Setup step group or the CSS hides it — which is the bug this fixes.
    expect(stepOf()).toBe("setup");
    const section = screen.getByRole("heading", { name: "Generated Reports" }).closest("section");
    expect(section?.getAttribute("data-stepgroup")).toContain("setup");
  });

  it("says Loading reports while the list is in flight, not No reports yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          // Never resolves: the list is still loading.
          return new Promise<Response>(() => {});
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    // Scoped to the hero metric: the results table already handled the loading
    // case, so an unscoped query would match that instead and pass regardless.
    // "No reports yet" while we are still asking is a claim we cannot make.
    await waitFor(() =>
      expect(document.querySelector(".module-metrics-empty")).toHaveTextContent(
        "Loading reports...",
      ),
    );
    expect(document.querySelector(".module-metrics-empty")).not.toHaveTextContent("No reports yet");
  });

  it("invalidates the reports list after generating a report from a run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          return jsonResponse({
            file_name: "ip_discovery_rep-7.pdf",
            output_format: "pdf",
            report_id: "rep-7",
            report_type: "ip_discovery",
            status: "succeeded",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    setApiKey("engineer-key");
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    stubScanAuthorizationFallback();
    render(
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute="ip-scanner-sct" />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>,
    );

    const runButton = await prepareAuthorizedIpRun();
    fireEvent.click(runButton);

    const generateButtons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(generateButtons[1]);

    // The toast tells the operator to look in the Reports tab, so the cached
    // list behind that tab must not still be the pre-report one.
    await waitFor(() => {
      const reportInvalidation = invalidateSpy.mock.calls.find(([filters]) => {
        const key = filters?.queryKey;
        return Array.isArray(key) && key[key.length - 1] === "reports";
      });
      expect(reportInvalidation).toBeDefined();
    });
  });
});

describe("ModulePage report controls placement", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const ipRunSummary = {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    stage: "register_comparison",
    progress_percent: 100,
    created_at: "2026-06-11T09:00:00Z",
    updated_at: "2026-06-11T09:05:00Z",
    edge_id: null,
  };

  // Run rehydration hands us a terminal run with no clicks at all, which is the
  // only way to put a *viewer* in front of one — viewers cannot start runs.
  function stubTerminalRun(
    options: {
      role?: string;
      lastRun?: boolean;
      finalEvidenceFails?: boolean;
      finalRun?: typeof terminalRun;
      runStatusError?: number;
      failReportFormats?: readonly ReportFormat[];
    } = {},
  ) {
    const {
      role = "engineer",
      lastRun = true,
      finalEvidenceFails = false,
      finalRun = terminalRun,
      runStatusError,
      failReportFormats = [],
    } = options;
    const failedReportFormats = new Set(failReportFormats);
    const captured: {
      reportBodies: Record<string, unknown>[];
      reportBody: Record<string, unknown> | null;
      exportBodies: Array<{ report_ids: string[] }>;
      activeReportRequests: number;
      maxActiveReportRequests: number;
      discoveryResultsRequests: number;
      discoveryRunStatusRequests: number;
    } = {
      reportBodies: [],
      reportBody: null,
      exportBodies: [],
      activeReportRequests: 0,
      maxActiveReportRequests: 0,
      discoveryResultsRequests: 0,
      discoveryRunStatusRequests: 0,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: lastRun && url.includes("job_type=ip_discovery") ? [ipRunSummary] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({ ...mePayload, role });
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          captured.discoveryResultsRequests += 1;
          if (finalEvidenceFails) {
            return {
              ok: false,
              status: 500,
              statusText: "Internal Server Error",
              json: async () => ({ detail: "final discovery evidence unavailable" }),
            } as unknown as Response;
          }
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          captured.discoveryRunStatusRequests += 1;
          if (runStatusError) {
            return {
              ok: false,
              status: runStatusError,
              statusText: "Rejected",
              json: async () => ({ detail: "final run status rejected" }),
            } as unknown as Response;
          }
          return jsonResponse(finalRun);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          const reportBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          captured.reportBody = reportBody;
          const reportNumber = captured.reportBodies.push(reportBody);
          const format = String(reportBody.output_format) as ReportFormat;
          captured.activeReportRequests += 1;
          captured.maxActiveReportRequests = Math.max(
            captured.maxActiveReportRequests,
            captured.activeReportRequests,
          );
          try {
            await Promise.resolve();
            if (failedReportFormats.has(format)) {
              return {
                ok: false,
                status: 500,
                statusText: "Internal Server Error",
                json: async () => ({ detail: `forced ${format} failure` }),
              } as unknown as Response;
            }
            return jsonResponse({
              file_name: `ip_discovery_rep-${reportNumber}.${format}`,
              output_format: format,
              report_id: `rep-${reportNumber}`,
              report_type: "ip_discovery",
              status: "succeeded",
            });
          } finally {
            captured.activeReportRequests -= 1;
          }
        }
        if (url.endsWith("/api/v1/reports/export") && init?.method === "POST") {
          captured.exportBodies.push(JSON.parse(String(init.body)) as { report_ids: string[] });
          return {
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["report bundle"]),
            headers: {
              get: (name: string) =>
                name.toLowerCase() === "content-disposition"
                  ? 'attachment; filename="reports_export.zip"'
                  : null,
            },
          } as unknown as Response;
        }
        // The reports route lists them on arrival.
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    return captured;
  }

  // field engineer's walkthrough bug: a finished run auto-advances to Results, and the
  // report controls — which live in the "setup run" group — go with it.
  it("renders the report controls in both the run-monitor and the results step group", async () => {
    stubTerminalRun();
    renderModule("ip-scanner-sct");

    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    expect(buttons).toHaveLength(2);

    // jsdom does not apply the theme CSS, so both copies are always in the DOM
    // and visibility assertions would be meaningless. What is assertable — and
    // what the CSS gate actually keys on — is the step group each one sits in.
    expect(buttons[0].closest("[data-stepgroup]")).toHaveAttribute("data-stepgroup", "setup run");
    expect(buttons[1].closest("[data-stepgroup]")).toHaveAttribute("data-stepgroup", "results");
  });

  it("keeps report generation available when final evidence refresh fails", async () => {
    stubTerminalRun({ finalEvidenceFails: true });
    renderModule("ip-scanner-sct");

    expect(await screen.findByText("Final evidence unavailable")).toBeInTheDocument();
    expect(
      await screen.findAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(2);
  });

  it("rejects a mismatched final run before requesting discovery results", async () => {
    const captured = stubTerminalRun({ finalRun: { ...terminalRun, run_id: "wrong-run" } });
    renderModule("ip-scanner-sct");

    expect(await screen.findByText("Final evidence unavailable")).toBeInTheDocument();
    expect(captured.discoveryResultsRequests).toBe(0);
    expect(captured.discoveryRunStatusRequests).toBeLessThanOrEqual(2);
  });

  it("does not retry a nontransient final-run status error or request discovery results", async () => {
    const captured = stubTerminalRun({ runStatusError: 400 });
    renderModule("ip-scanner-sct");

    expect(await screen.findByText("Final evidence unavailable")).toBeInTheDocument();
    expect(captured.discoveryResultsRequests).toBe(0);
    expect(captured.discoveryRunStatusRequests).toBeLessThanOrEqual(2);
  });

  it("fails terminal sync after the four bounded nonterminal status attempts without fetching results", async () => {
    const captured = stubTerminalRun({ finalRun: { ...terminalRun, status: "running" } });
    renderModule("ip-scanner-sct");

    expect(
      await screen.findByText("Final evidence unavailable", {}, { timeout: 4_000 }),
    ).toBeInTheDocument();
    expect(captured.discoveryRunStatusRequests).toBeGreaterThanOrEqual(4);
    expect(captured.discoveryResultsRequests).toBe(0);
  });

  // The gate is `.module-steps > [data-stepgroup]` — a DIRECT child selector. A
  // results section nested one level deeper still looks right in jsdom and in
  // the test above, but would render on every step in a real browser.
  it("hangs the results-step report section directly off .module-steps so the CSS gate applies", async () => {
    stubTerminalRun();
    renderModule("ip-scanner-sct");

    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    const resultsSection = buttons[1].closest("[data-stepgroup]");
    expect(resultsSection?.parentElement).toHaveClass("module-steps");
  });

  it("shares one format selection between both report control instances", async () => {
    const captured = stubTerminalRun();
    renderModule("ip-scanner-sct");

    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    expect(pickers).toHaveLength(2);

    // Change the results-step picker; the run-monitor one must follow it. Give
    // the extracted component its own useState and the two drift apart — you
    // get a picker that lies about the format it is going to generate.
    fireEvent.change(pickers[1], { target: { value: "docx" } });
    expect(pickers[0].value).toBe("docx");

    // ...and the POST reads the shared state, whichever button you press.
    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(buttons[0]);
    await waitFor(() => expect(captured.reportBody).not.toBeNull());
    expect(captured.reportBody?.output_format).toBe("docx");
  });

  it("generates PDF, Word, Excel, and evidence pack reports from Generate All", async () => {
    const captured = stubTerminalRun();
    renderModule("ip-scanner-sct");

    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    fireEvent.change(pickers[1], { target: { value: "all" } });
    expect(pickers[0].value).toBe("all");

    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(buttons[1], "Building A commissioning");

    await waitFor(() => expect(captured.reportBodies).toHaveLength(4));
    expect(captured.reportBodies.map((body) => body.output_format)).toEqual([
      "pdf",
      "docx",
      "xlsx",
      "zip",
    ]);
    expect(captured.maxActiveReportRequests).toBe(1);
    expect(new Set(captured.reportBodies.map((body) => body.report_title))).toEqual(
      new Set(["Building A commissioning"]),
    );
    expect(
      new Set(captured.reportBodies.map((body) => JSON.stringify(body.source_run_ids))),
    ).toEqual(new Set([JSON.stringify(["run-ip-1"])]));
    expect(
      await screen.findAllByText(
        /4 reports generated from this run: PDF, Word, Excel, and evidence pack/i,
      ),
    ).toHaveLength(2);
  });

  it("continues serial Generate All after one format fails", async () => {
    const captured = stubTerminalRun({ failReportFormats: ["docx"] });
    renderModule("ip-scanner-sct");

    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    fireEvent.change(pickers[1], { target: { value: "all" } });
    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(buttons[1]);

    expect(
      await screen.findAllByText(
        /3 of 4 reports were generated\. Failed formats: DOCX\. The completed reports are in the Reports tab\./i,
      ),
    ).toHaveLength(2);
    expect(captured.reportBodies.map((body) => body.output_format)).toEqual([
      "pdf",
      "docx",
      "xlsx",
      "zip",
    ]);
    expect(captured.maxActiveReportRequests).toBe(1);
  });

  it("keeps Generate All retryable after every format fails", async () => {
    const captured = stubTerminalRun({
      failReportFormats: ["pdf", "docx", "xlsx", "zip"],
    });
    renderModule("ip-scanner-sct");

    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    fireEvent.change(pickers[1], { target: { value: "all" } });
    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(buttons[1]);

    const dialog = await screen.findByRole("dialog", { name: "Name this validation report" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("forced pdf failure");
    expect(captured.reportBodies.map((body) => body.output_format)).toEqual([
      "pdf",
      "docx",
      "xlsx",
      "zip",
    ]);
    expect(captured.maxActiveReportRequests).toBe(1);
    expect(screen.queryByText("Report generation incomplete")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download all reports/i })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Generate report" })).toBeEnabled();
  });

  it("offers one combined report download after Generate All", async () => {
    const captured = stubTerminalRun();
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
    renderModule("ip-scanner-sct");

    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    fireEvent.change(pickers[1], { target: { value: "all" } });
    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(buttons[1], "Building A commissioning");

    await waitFor(() => expect(captured.reportBodies).toHaveLength(4));
    const downloadButtons = await screen.findAllByRole("button", {
      name: "Download all reports (.zip)",
    });
    expect(downloadButtons).toHaveLength(2);
    expect(downloadButtons[0].closest("[data-stepgroup]")).toHaveAttribute(
      "data-stepgroup",
      "setup run",
    );
    expect(downloadButtons[1].closest("[data-stepgroup]")).toHaveAttribute(
      "data-stepgroup",
      "results",
    );
    expect(captured.exportBodies).toHaveLength(0);

    fireEvent.click(downloadButtons[1]);

    await waitFor(() =>
      expect(captured.exportBodies).toEqual([{ report_ids: ["rep-1", "rep-2", "rep-3", "rep-4"] }]),
    );
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it.each(["resolve", "reject"])(
    "clears completed Generate All feedback when the same run ID enters a new epoch (%s late export)",
    async (settlement) => {
    const sharedRunId = "run-report-feedback-reused";
    const terminal = {
      ...terminalRun,
      run_id: sharedRunId,
      job_type: "mqtt_discovery",
      stage: "capture",
      result_summary: {},
    };
    const reportBodies: Record<string, unknown>[] = [];
    let exportRequests = 0;
    let exportSignal: AbortSignal | null = null;
    let releaseExport!: (response: Response) => void;
    let rejectExport!: (error: Error) => void;
    const deferredExport = new Promise<Response>((resolve, reject) => {
      releaseExport = resolve;
      rejectExport = reject;
    });
    const createObjectURL = vi.fn(() => "blob:stale-report-bundle");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: url.includes("job_type=mqtt_discovery") ? [{ ...terminal, edge_id: null }] : [] });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse({
            run_id: sharedRunId,
            job_type: "mqtt_discovery",
            status: "queued",
            message: "MQTT discovery accepted.",
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}`)) return jsonResponse(terminal);
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/results`)) {
          return jsonResponse({
            ...resultsPayload,
            run_id: sharedRunId,
            job_type: "mqtt_discovery",
            discovered_assets: [],
            devices: [],
            points: [],
            topics: [],
          });
        }
        if (url.endsWith(`/api/v1/discovery/runs/${sharedRunId}/topics`)) {
          return jsonResponse({ run_id: sharedRunId, topics: [] });
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          const reportNumber = reportBodies.push(body);
          return jsonResponse({
            file_name: `report-${reportNumber}.${body.output_format}`,
            output_format: body.output_format,
            report_id: `report-${reportNumber}`,
            report_type: "mqtt_discovery",
            status: "succeeded",
          });
        }
        if (url.endsWith("/api/v1/reports/export") && init?.method === "POST") {
          exportRequests += 1;
          exportSignal = init.signal ?? null;
          return deferredExport;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery-sct");
    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    fireEvent.change(pickers[0], { target: { value: "all" } });
    const [reportButton] = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    await submitReportDialog(reportButton, "Stale feedback regression");
    await waitFor(() => expect(reportBodies).toHaveLength(4));
    const staleDownloadButtons = await screen.findAllByRole("button", {
      name: "Download all reports (.zip)",
    });
    expect(staleDownloadButtons).toHaveLength(2);
    expect(await screen.findAllByText(/4 reports generated from this run/i)).toHaveLength(2);
    fireEvent.click(staleDownloadButtons[0]);
    await waitFor(() => expect(exportSignal).not.toBeNull());

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() =>
      expect(screen.queryAllByRole("button", { name: "Download all reports (.zip)" })).toHaveLength(0),
    );
    await waitFor(() => expect(exportSignal?.aborted).toBe(true));
    expect(screen.queryByText(/4 reports generated from this run/i)).not.toBeInTheDocument();
    await act(async () => {
      if (settlement === "resolve") {
        releaseExport({
          ok: true,
          status: 200,
          statusText: "OK",
          blob: async () => new Blob(["stale export"]),
          headers: { get: () => 'attachment; filename="stale-reports.zip"' },
        } as unknown as Response);
      } else {
        rejectExport(new Error("late export failure"));
      }
    });
    await waitFor(() => expect(exportRequests).toBe(1));
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
    expect(screen.queryByText(/Combined report download failed/i)).not.toBeInTheDocument();
    anchorClick.mockRestore();
    },
  );

  it("renders no report controls until a run exists", async () => {
    stubTerminalRun({ lastRun: false });
    renderModule("ip-scanner-sct");

    // Wait for the page to settle before trusting an absence assertion.
    expect(await screen.findByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
  });

  it("renders no report controls for a viewer, even with a terminal run attached", async () => {
    stubTerminalRun({ role: "viewer" });
    renderModule("ip-scanner-sct");

    // The monitor proves the terminal run really is attached — so the absence
    // below is the engineer gate doing its job in the new section, not a page
    // that simply has no run.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
  });

  // Pins the reports route's shape, NOT the `route !== "reports"` clause in the
  // new section's guard: report actions never set activeRun, so that clause is
  // unreachable and this test passes with or without it. Deleting it here would
  // be a silent no-op — it earns its place upstream as defence, not as a fix.
  it("leaves the reports route with a single set of report controls", async () => {
    stubTerminalRun({ lastRun: false });
    renderModule("reports");

    expect(await screen.findByText(/Generated Reports/i)).toBeInTheDocument();
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
  });
});

describe("ModulePage snap-to-top when results open", () => {
  afterEach(() => {
    // Restores the setup.ts no-op that the spy wrapped.
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearApiKey();
  });

  // jsdom has no layout, so scrollIntoView only exists because src/test/setup.ts
  // installs a no-op — vi.spyOn would throw on an undefined property. The spy
  // calls through to that no-op, so nothing here depends on real scrolling.
  function spyOnScroll() {
    return vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
  }

  // The element the page scrolled to, recorded by the spy as its `this`.
  function scrollTarget(spy: ReturnType<typeof spyOnScroll>, call = 0) {
    return spy.mock.contexts[call] as HTMLElement;
  }

  const ipRunSummary = {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    stage: "register_comparison",
    progress_percent: 100,
    created_at: "2026-06-11T09:00:00Z",
    updated_at: "2026-06-11T09:05:00Z",
    edge_id: null,
  };

  // `lastRun` decides whether this head has a previous succeeded run to
  // rehydrate — the difference between a run the operator just started and one
  // restored on arrival, which must NOT snap.
  function stubIpScanner(options: { lastRun?: boolean } = {}) {
    const { lastRun = false } = options;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: lastRun && url.includes("job_type=ip_discovery") ? [ipRunSummary] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  // field engineer's walkthrough ask: after a run finishes, the page stays where the
  // operator left it mid-Run, so the headline results land off-screen.
  it("snaps to the hero when a succeeded run advances to Results", async () => {
    const scrollSpy = spyOnScroll();
    stubIpScanner();
    renderModule("ip-scanner-sct");

    const queueButton = await prepareAuthorizedIpRun();

    // Nothing has opened Results yet, so setting up must not move the page.
    expect(scrollSpy).not.toHaveBeenCalled();

    fireEvent.click(queueButton);

    // jsdom does not apply the step-gating CSS, so `data-step` is the assertable
    // signal that Results actually opened.
    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
    // The snap lives in a passive effect and the step update arrives from a
    // react-query poll outside act(), so the waitFor above can observe the
    // commit BEFORE React flushes the effect — seen flaking on the slower
    // windows-2022 runner. Poll for the spy; the assertion itself is unchanged.
    await waitFor(() =>
      expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "start" }),
    );
    // ...and it scrolled the hero, not some arbitrary element.
    expect(scrollTarget(scrollSpy)).toHaveClass("module-hero");
  });

  it("does not expose generic report generation on Reports", async () => {
    const scrollSpy = spyOnScroll();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.split("?")[0].endsWith("/api/v1/reports") && init?.method === "POST") {
          return jsonResponse({
            file_name: "issue_report.xlsx",
            output_format: "xlsx",
            report_id: "rep-1",
            report_type: "issue_report",
            status: "succeeded",
          });
        }
        if (url.split("?")[0].endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    await screen.findByRole("heading", { name: "Generated Reports" });
    expect(screen.queryByRole("button", { name: /Generate .* Report/i })).not.toBeInTheDocument();
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  // A run restored on arrival never advances the step, so it must never snap
  // either — the operator came here to set something up, and yanking the page to
  // the top for a run they did not just start would be exactly the hijack the
  // step-retention work took care to avoid.
  it("does not snap for a run rehydrated on arrival", async () => {
    const scrollSpy = spyOnScroll();
    stubIpScanner({ lastRun: true });
    renderModule("ip-scanner-sct");

    // The monitor proves the restored run really did attach, so the absence
    // below is the restored guard holding, not a page with nothing on it.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("plant-controller").length).toBeGreaterThan(0));

    expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "setup");
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  // The snap follows the step, not the run: a manual jump to Results must move
  // the page too, or clicking "3 Results" from a scrolled-down Run step leaves
  // the operator staring at the middle of the results they asked to see.
  it("snaps to the hero on a manual step click to Results", async () => {
    const scrollSpy = spyOnScroll();
    stubIpScanner({ lastRun: true });
    renderModule("ip-scanner-sct");

    const resultsStep = await screen.findByRole("button", { name: /Results/i });
    await waitFor(() => expect(resultsStep).toBeEnabled());
    expect(scrollSpy).not.toHaveBeenCalled();

    fireEvent.click(resultsStep);

    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "start" });
    expect(scrollTarget(scrollSpy)).toHaveClass("module-hero");
  });
});

// A rejected import used to report only "N accepted / M rejected" — the reasons
// were produced and persisted by the backend but never fetched. These cover the
// reasons panel plus the same-filename re-pick fix on the file input.
describe("ModulePage import rejection reasons", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  function rejectedSummary(overrides: Record<string, unknown> = {}) {
    return {
      import_id: "import-ip-2",
      import_type: "ip_register",
      file_name: "ip_register.csv",
      file_type: "csv",
      project_id: "demo-project",
      site_id: "demo-site",
      total_rows: 4,
      accepted_rows: 0,
      rejected_rows: 4,
      status: "rejected",
      missing_columns: [],
      warnings: [],
      stored_file_name: "import-ip-2.csv",
      created_at: "2026-07-15T09:00:00Z",
      ...overrides,
    };
  }

  // Stubs /me + /imports/profiles + POST /imports, and routes the errors GET to
  // `errors`. `onErrors` (when given) replaces the default success response.
  function stubImport(options: {
    summary?: Record<string, unknown>;
    errors?: unknown;
    onErrorsUrl?: (url: string) => void;
    errorsFails?: boolean;
  }) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) {
        return jsonResponse({ runs: [] });
      }
      if (url.endsWith("/api/v1/me")) {
        return jsonResponse(mePayload);
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse(profilesPayload);
      }
      if (url.endsWith("/api/v1/imports") && init?.method === "POST") {
        return jsonResponse(options.summary ?? rejectedSummary());
      }
      if (url.includes("/errors")) {
        options.onErrorsUrl?.(url);
        if (options.errorsFails) {
          return {
            ok: false,
            status: 404,
            statusText: "Not Found",
            json: async () => ({ detail: "Import errors for 'import-ip-2' were not found." }),
          } as unknown as Response;
        }
        return jsonResponse(options.errors ?? { import_id: "import-ip-2", errors: [] });
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  async function uploadFile(name = "ip_register.csv") {
    fireEvent.change(await screen.findByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["reg"], name)] },
    });
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);
  }

  it("fetches and renders per-row rejection reasons in a red panel", async () => {
    const errorUrls: string[] = [];
    stubImport({
      onErrorsUrl: (url) => errorUrls.push(url),
      errors: {
        import_id: "import-ip-2",
        errors: [
          {
            row_number: 3,
            field: "Expected topic",
            code: "invalid_topic",
            message: "Topic must not contain wildcards.",
          },
          {
            row_number: 5,
            field: null,
            code: "duplicate_row",
            message: "Duplicate record detected for asset_id AHU-01.",
          },
        ],
      },
    });

    renderModule("ip-scanner-sct");
    await uploadFile();

    // Row + field + message + code, with the reason the operator has to act on.
    expect(
      await screen.findByText(/Row 3 — Expected topic: Topic must not contain wildcards\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/\(invalid_topic\)/)).toBeInTheDocument();

    // field is null on duplicate_row records, so no stray ": " prefix.
    const duplicate = screen.getByText(/Duplicate record detected for asset_id AHU-01\./);
    expect(duplicate.textContent).toContain("Row 5 — Duplicate record detected");
    expect(duplicate.textContent).not.toContain("null");

    // Red rejection styling, never the amber warning panel (whose rows are kept).
    const panel = duplicate.closest(".state-panel");
    expect(panel).toHaveClass("error");
    expect(panel).toHaveClass("import-errors");
    expect(panel).not.toHaveClass("warning");

    // The reasons came from the real endpoint, keyed by this import's id.
    await waitFor(() => expect(errorUrls).toHaveLength(1));
    expect(errorUrls[0]).toMatch(/\/api\/v1\/imports\/import-ip-2\/errors$/);
  });

  it("names the missing columns without repeating them as bullets", async () => {
    stubImport({
      summary: rejectedSummary({
        total_rows: 0,
        rejected_rows: 0,
        missing_columns: ["Asset ID", "Expected topic"],
      }),
      errors: {
        import_id: "import-ip-2",
        errors: [
          {
            row_number: null,
            field: "Asset ID",
            code: "missing_required_column",
            message: "Required column 'Asset ID' is missing.",
          },
          {
            row_number: null,
            field: "Expected topic",
            code: "missing_required_column",
            message: "Required column 'Expected topic' is missing.",
          },
        ],
      },
    });

    renderModule("ip-scanner-sct");
    await uploadFile();

    // The summary already carries the columns, so this line needs no fetch...
    expect(
      await screen.findByText("Missing required columns: Asset ID, Expected topic"),
    ).toBeInTheDocument();
    // ...and the per-column records that would repeat it verbatim are filtered out.
    expect(screen.queryByText(/Required column 'Asset ID' is missing\./)).not.toBeInTheDocument();

    // rejected_rows is 0 for a missing-columns file (_status() still says
    // "rejected"), so the panel must not be gated on rejected_rows > 0.
    expect(screen.getByText("Import rejected — reasons below")).toBeInTheDocument();
  });

  it("does not fetch reasons for an accepted import", async () => {
    const errorUrls: string[] = [];
    stubImport({
      summary: rejectedSummary({
        import_id: "import-ip-3",
        total_rows: 2,
        accepted_rows: 2,
        rejected_rows: 0,
        status: "accepted",
      }),
      onErrorsUrl: (url) => errorUrls.push(url),
    });

    renderModule("ip-scanner-sct");
    await uploadFile();

    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
    expect(screen.queryByText(/reasons below/)).not.toBeInTheDocument();
    expect(errorUrls).toEqual([]);
  });

  it("says so honestly when the reasons cannot be loaded", async () => {
    stubImport({ errorsFails: true });

    renderModule("ip-scanner-sct");
    await uploadFile();

    // An empty list must never masquerade as "no reasons" when the fetch failed.
    expect(await screen.findByText(/Could not load rejection reasons:/)).toBeInTheDocument();
  });

  it("caps the rendered rows and states the honest remainder", async () => {
    stubImport({
      // A partial import: 5 rows landed, 60 did not. Exercises the partial
      // headline as well as the cap.
      summary: rejectedSummary({
        total_rows: 65,
        accepted_rows: 5,
        rejected_rows: 60,
        status: "partial",
      }),
      errors: {
        import_id: "import-ip-2",
        errors: Array.from({ length: 60 }, (_, index) => ({
          row_number: index + 2,
          field: "Expected topic",
          code: "invalid_topic",
          message: `Row ${index + 2} topic is malformed.`,
        })),
      },
    });

    renderModule("ip-scanner-sct");
    await uploadFile();

    const panel = (await screen.findByText("60 of 65 rows rejected — reasons below")).closest(
      ".state-panel",
    ) as HTMLElement;
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(50));
    expect(within(panel).getByText(/and 10 more rejected rows not shown/)).toBeInTheDocument();
  });

  it("clears the file input's value on selection so a re-picked same-name file is re-read", async () => {
    stubImport({ summary: rejectedSummary({ status: "accepted", accepted_rows: 4 }) });
    renderModule("ip-scanner-sct");

    const input = (await screen.findByLabelText(/CSV or XLSX file/i)) as HTMLInputElement;

    // jsdom cannot reproduce Chromium's real behaviour here (no change event
    // when the same path is re-picked), and neither `input.value` nor
    // `input.files` can witness the clear: fireEvent installs `files` as an own
    // property that shadows jsdom's getter and never fills jsdom's internal
    // selected-file list, so `value` reads "" before the handler even runs and
    // `files` keeps its array afterwards. Both would-be assertions are
    // therefore vacuous. Spy on the assignment itself instead — that IS the fix.
    const descriptor =
      Object.getOwnPropertyDescriptor(input, "value") ??
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!;
    const valueAssignments: string[] = [];
    Object.defineProperty(input, "value", {
      configurable: true,
      get: () => descriptor.get!.call(input),
      set: (next: string) => {
        valueAssignments.push(next);
        descriptor.set!.call(input, next);
      },
    });

    fireEvent.change(input, { target: { files: [new File(["reg"], "ip_register.csv")] } });

    expect(valueAssignments).toContain("");

    // The File lives in state, so clearing the DOM input costs nothing: the
    // staged name is still shown and the upload still goes through.
    expect(await screen.findByText("Selected: ip_register.csv")).toBeInTheDocument();
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);
    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
  });
});
