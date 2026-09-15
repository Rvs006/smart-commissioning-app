import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearApiKey, setApiKey } from "../../api/client";
import { IpScannerPage } from "./IpScannerPage";
import { createSessionScopeId } from "../../app/sessionScope";
import { createScannerQueryClient, scannerProviders } from "./scannerTestHarness";

const RUN_ID = "run-ip-scanner-1";

function jsonResponse(payload: unknown): Response {
  return { ok: true, status: 200, statusText: "OK", json: async () => payload } as unknown as Response;
}

const terminalRun = {
  run_id: RUN_ID,
  job_type: "ip_scanner",
  status: "succeeded",
  stage: "register_comparison",
  progress_percent: 100,
  created_at: "2026-09-14T09:00:00Z",
  updated_at: "2026-09-14T09:02:00Z",
  project_id: "demo-project",
  site_id: "demo-site",
  parameters: {},
  result_summary: {
    register_expected: 3,
    hosts_scanned: 2,
    register_matches: 1,
    register_partial: 0,
    register_missing: 1,
    register_rogue: 1,
  },
  error_message: null,
};

const results = {
  run_id: RUN_ID,
  job_type: "ip_scanner",
  status: "succeeded",
  result_summary: terminalRun.result_summary,
  discovered_assets: [
    {
      asset_id: null,
      ip_address: "10.0.10.12",
      mac_address: "00:80:F4:11:22:33",
      hostname: "ahu-01",
      observed_ports: [{ port: 443, protocol: "tcp" }],
      match_basis: "ip",
      status_detail: "reachable/match",
      last_seen_at: "2026-09-14T09:02:00Z",
      rag: "green",
      register: "match",
    },
    {
      asset_id: null,
      ip_address: "10.0.10.55",
      mac_address: null,
      hostname: "unknown-55",
      observed_ports: [{ port: 80, protocol: "tcp" }],
      match_basis: "ip",
      status_detail: "rogue/rogue",
      last_seen_at: "2026-09-14T09:02:00Z",
      rag: "red",
      register: "rogue",
    },
    {
      // A silent host: the engine leaves `hostname` null and carries the
      // register's expectation in expected_hostname.
      asset_id: null,
      ip_address: "10.0.10.20",
      mac_address: null,
      hostname: null,
      expected_hostname: "chiller-2",
      observed_ports: [],
      match_basis: "register",
      status_detail: "unreachable/missing",
      last_seen_at: null,
      rag: "red",
      register: "missing",
    },
  ],
  devices: [
    {
      address: "10.0.10.12",
      device_type: "controller",
      name: "ahu-01",
      vendor: "Example Controls",
      model: "AC-100",
      attributes: {
        rag: "green",
        register: "match",
        status: "reachable",
        hostname_status: "match",
        expected_hostname: "ahu-01",
        hostname: "ahu-01",
        hostname_src: "dns",
        mac_address: "00:80:F4:11:22:33",
        latency: 5,
        open_ports: [443],
        services: [
          {
            proto: "tcp",
            port: 443,
            name: "https",
            tls: true,
            product: "nginx",
            version: "1.24",
            title: "Plant controller",
            certCN: "ahu-01.local",
          },
        ],
        expected_ports: [443],
        missing_ports: [],
        extra_ports: [],
        banner: "Example BMS",
        discovered_by: "arp",
        project: "Demo",
        location: "Plant room",
        description: "Air handling unit controller",
      },
    },
    {
      address: "10.0.10.55",
      device_type: "ip_host",
      name: "unknown-55",
      vendor: null,
      attributes: { rag: "red", register: "rogue", status: "rogue", open_ports: [80] },
    },
  ],
  points: [],
  topics: [],
};

let startBody: Record<string, unknown> | null = null;

function stubFetch(
  overrides: {
    runs?: unknown[] | (() => unknown[]);
    onSaveRegister?: () => Promise<unknown>;
    latestImportFileName?: string;
    runStatus?: string;
    runError?: string | null;
  } = {},
) {
  startBody = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) {
        const base = { ...terminalRun, status: overrides.runStatus ?? terminalRun.status };
        void base;
        const runs =
          typeof overrides.runs === "function" ? overrides.runs() : (overrides.runs ?? [terminalRun]);
        return jsonResponse({ runs });
      }
      if (url.endsWith("/api/v1/reports") && init?.method === "POST") {
        return jsonResponse({
          report_id: "rep-run-1",
          report_type: "ip_discovery",
          output_format: "pdf",
          file_name: "ip-discovery.pdf",
          status: "succeeded",
          created_at: "2026-09-14T09:20:00Z",
        });
      }
      if (url.includes("/api/v1/imports") && init?.method === "POST") {
        return jsonResponse({
          import_id: "imp-upload-1",
          import_type: "ip_scanner_register",
          file_name: "register.csv",
          status: "accepted",
          total_rows: 1,
          accepted_rows: 1,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T09:10:00Z",
        });
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse([
          {
            import_type: "ip_scanner_register",
            description: "Expected IP assets.",
            required_columns: ["asset_id", "ip_address"],
            optional_columns: ["expected_ports"],
            duplicate_key_fields: ["asset_id"],
          },
        ]);
      }
      if (url.includes("/api/v1/imports/latest")) {
        return jsonResponse({
          import_id: "imp-1",
          import_type: "ip_scanner_register",
          file_name: overrides.latestImportFileName ?? "ip-register.csv",
          status: "accepted",
          total_rows: 3,
          accepted_rows: 3,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T08:00:00Z",
        });
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse({
          Network: { values: { "Source Interface": "10.0.10.5/24" } },
        });
      }
      if (url.endsWith("/api/v1/system/interfaces")) {
        return jsonResponse([]);
      }
      const runMatch = new RegExp("/api/v1/discovery/runs/([^/?]+)").exec(url);
      if (runMatch) {
        const runId = runMatch[1];
        if (url.includes("/results")) {
          return jsonResponse({
            ...results,
            run_id: runId,
            status: overrides.runStatus ?? results.status,
            ...(overrides.runStatus && overrides.runStatus !== "succeeded"
              ? { discovered_assets: [], devices: [] }
              : {}),
          });
        }
        if (url.includes("/save-as-register")) {
          // handled below
        } else {
          return jsonResponse({
            ...terminalRun,
            run_id: runId,
            status: overrides.runStatus ?? terminalRun.status,
            error_message: overrides.runError ?? terminalRun.error_message,
          });
        }
      }
      if (url.endsWith("/api/v1/discovery/ip_sidecar/runs") && init?.method === "POST") {
        startBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse({
          run_id: RUN_ID,
          job_type: "ip_scanner",
          status: "queued",
          message: "IP scan accepted.",
        });
      }
      if (url.includes("/save-as-register")) {
        if (overrides.onSaveRegister) {
          return jsonResponse(await overrides.onSaveRegister());
        }
        return jsonResponse({
          import_id: "imp-saved-1",
          import_type: "ip_scanner_register",
          file_name: "scan-register-run-ip-scanner-1.csv",
          status: "accepted",
          total_rows: 2,
          accepted_rows: 2,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T09:05:00Z",
        });
      }
      // The SSE stream: no streaming body, so useRunEvents falls back to polling.
      if (url.includes("/events")) {
        return jsonResponse({});
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );
}

beforeEach(() => {
  setApiKey("engineer-key");
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearApiKey();
  window.localStorage.clear();
});

describe("IpScannerPage", () => {
  it("renders the setup card, six summary pills and the RAG-coloured rows", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));

    // The page owns its title (the shell suppresses its own on this route), so
    // the head must name the lane exactly as the menu entry does.
    expect(screen.getByRole("heading", { level: 1, name: "IP Discovery" })).toBeInTheDocument();
    expect(
      screen.getByText("Find reachable, missing and unexpected hosts — native ip_scanner run."),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Scan setup" })).toBeInTheDocument();
    // Auto-subnet prefill from the configured Source Interface cidr.
    await waitFor(() =>
      expect(screen.getByLabelText(/Start IP/i)).toHaveValue("10.0.10.1"),
    );
    expect(screen.getByLabelText(/End IP/i)).toHaveValue("10.0.10.254");
    expect(screen.getByLabelText(/Per-probe timeout/i)).toHaveValue("1000");

    // All six register counters, from result_summary only.
    const results = await screen.findByRole("heading", { name: "Results" });
    const card = results.closest("section") as HTMLElement;
    // The card renders before the run's evidence barrier clears, so wait for the
    // first counter rather than asserting straight away.
    await within(card).findByText("3 Expected");
    for (const pill of ["2 Reachable", "1 Match", "0 Partial", "1 Missing", "1 Rogue"]) {
      expect(within(card).getByText(pill)).toBeInTheDocument();
    }

    // One row per observation, including the expected-but-silent host.
    expect(await screen.findByText("10.0.10.12")).toBeInTheDocument();
    expect(screen.getByText("10.0.10.55")).toBeInTheDocument();
    const missingCell = screen.getByText("10.0.10.20");
    const missingRow = missingCell.closest("tr") as HTMLElement;
    expect(missingRow.className).toContain("row-fail");
    expect(within(missingRow).getByText("Missing")).toBeInTheDocument();
  });

  it("narrows the table with the RAG chip filter", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    await screen.findByText("10.0.10.12");
    expect(screen.getByText(/Showing 3 of 3 rows/)).toBeInTheDocument();

    const matchChip = screen.getByRole("button", { name: "Match" });
    fireEvent.click(matchChip);

    expect(matchChip).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/Showing 1 of 3 rows/)).toBeInTheDocument();
    expect(screen.getByText("10.0.10.12")).toBeInTheDocument();
    expect(screen.queryByText("10.0.10.55")).not.toBeInTheDocument();
  });

  it("fills the side panel from the clicked row, banner, latency and found-via included", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    const cell = await screen.findByText("10.0.10.12");
    fireEvent.click(cell.closest("tr") as HTMLElement);

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "ahu-01" })).toBeInTheDocument();
    expect(within(panel).getAllByText("Example BMS").length).toBeGreaterThan(0);
    expect(within(panel).getByText("5 ms")).toBeInTheDocument();
    expect(within(panel).getByText("ARP")).toBeInTheDocument();
    // The vendored svcDescr() formatting, reproduced for the services list.
    expect(within(panel).getByText("tcp/443 https 🔒")).toBeInTheDocument();
    expect(
      within(panel).getByText('nginx 1.24 · “Plant controller” · cert: ahu-01.local'),
    ).toBeInTheDocument();
  });

  it("returns focus to the row that opened the panel when it closes", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    const cell = await screen.findByText("10.0.10.12");
    const row = cell.closest("tr") as HTMLElement;
    row.focus();
    fireEvent.click(row);

    const panel = await screen.findByRole("complementary");
    fireEvent.click(within(panel).getByRole("button", { name: "Close detail panel" }));

    await waitFor(() => expect(document.activeElement).toBe(row));
  });

  it("says a missing device was expected and shows no observed evidence", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    const cell = await screen.findByText("10.0.10.20");
    fireEvent.click(cell.closest("tr") as HTMLElement);

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "Expected, no response" })).toBeInTheDocument();
    expect(within(panel).queryByRole("heading", { name: "Live health" })).not.toBeInTheDocument();
    // The register's expectation is named as an expectation, never as a discovery.
    expect(within(panel).getByText("expected · chiller-2")).toBeInTheDocument();
    expect(
      within(panel).getByText("Expected, not resolved on the network"),
    ).toBeInTheDocument();
  });

  it("posts the same run parameters ModulePage posts today", async () => {
    stubFetch({ runs: [] });
    render(scannerProviders(<IpScannerPage />));
    await waitFor(() => expect(screen.getByLabelText(/Start IP/i)).toHaveValue("10.0.10.1"));

    fireEvent.click(screen.getByRole("button", { name: "Start scan" }));

    await waitFor(() => expect(startBody).not.toBeNull());
    expect(startBody).toMatchObject({
      job_type: "ip_scanner",
      parameters: {
        authorized: true,
        start_ip: "10.0.10.1",
        end_ip: "10.0.10.254",
        timeout: 1000,
      },
    });
    // No register opt-out was ticked, so the key must not reach the wire.
    expect((startBody?.parameters as Record<string, unknown>).ignore_register).toBeUndefined();
  });

  it("saves the scan as a register and offers the CSV download", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    const save = await screen.findByRole("button", {
      name: /Save scan as register \(applies to the next scan\)/,
    });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    expect(await screen.findByText(/Saved as register/)).toBeInTheDocument();
    expect(
      screen.getByText(/It is stored here and applies automatically to the next IP scan/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download register CSV" })).toBeInTheDocument();
  });

  it("drops a save that lands after the operator switched runs", async () => {
    const runB = { ...terminalRun, run_id: "run-ip-scanner-2" };
    let currentRun: Record<string, unknown> = terminalRun;
    let releaseSave!: () => void;
    const savePending = new Promise<void>((resolve) => {
      releaseSave = resolve;
    });
    stubFetch({
      runs: () => [currentRun],
      onSaveRegister: async () => {
        await savePending;
        return {
          import_id: "imp-saved-1",
          import_type: "ip_scanner_register",
          file_name: `scan-register-${RUN_ID}.csv`,
          status: "accepted",
          total_rows: 2,
          accepted_rows: 2,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T09:05:00Z",
        };
      },
    });
    render(scannerProviders(<IpScannerPage />));

    const save = await screen.findByRole("button", {
      name: /Save scan as register/,
    });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await screen.findByRole("button", { name: "Saving register..." });

    // The operator moves to another run while run 1 save is still in flight.
    currentRun = runB;
    window.dispatchEvent(new Event("visibilitychange"));
    // The refetch this event triggers is a real round trip through the query
    // cache; the default 1s window is tight on a loaded machine.
    await waitFor(() => expect(document.body.textContent).toContain("run-ip-scanner-2"), {
      timeout: 5000,
    });

    releaseSave();

    // Run 1 summary must not repopulate the panel over run 2: the note would
    // name run 1 file while the download URL pointed at run 2.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Saving register..." })).toBeNull(),
    );
    expect(screen.queryByText("Saved as register")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download register CSV" })).toBeNull();
  });

  it("offers the register CSV for a register on file that came from a scan", async () => {
    stubFetch({ latestImportFileName: `scan-register-${RUN_ID}.csv` });
    render(scannerProviders(<IpScannerPage />));

    const note = await screen.findByText(/Register already imported/);
    const panel = note.closest(".state-panel") as HTMLElement;
    fireEvent.click(within(panel).getByRole("button", { name: "Download register CSV" }));

    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(([input]) =>
            String(input).includes(`/ip_sidecar/runs/${RUN_ID}/register.csv`),
          ),
      ).toBe(true),
    );
  });

  it("offers no register CSV when the register on file was uploaded", async () => {
    stubFetch({ latestImportFileName: "site_ip_register.csv" });
    render(scannerProviders(<IpScannerPage />));

    const note = await screen.findByText(/Register already imported/);
    const panel = note.closest(".state-panel") as HTMLElement;
    expect(within(panel).queryByRole("button", { name: "Download register CSV" })).toBeNull();
  });

  it("refreshes the register-on-file note after an upload", async () => {
    stubFetch();
    const queryClient = createScannerQueryClient();
    render(scannerProviders(<IpScannerPage />, { queryClient }));
    await screen.findByText(/Register already imported/);

    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const file = new File(["asset_id,ip_address"], "register.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText(/CSV or XLSX file/i), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload and validate" }));

    // The ROOT key, not the one ending in an empty import-type slot, is what the
    // "Register already imported" query is actually stored under.
    await waitFor(() =>
      expect(
        invalidate.mock.calls.some(
          ([options]) =>
            Array.isArray(options?.queryKey) &&
            options.queryKey[options.queryKey.length - 1] === "latest-import",
        ),
      ).toBe(true),
    );
  });

  it("says a failed run failed instead of claiming it found nothing", async () => {
    stubFetch({ runStatus: "failed", runError: "sidecar refused the scan range" });
    render(scannerProviders(<IpScannerPage />));

    expect(await screen.findByText("Run failed — no results recorded")).toBeInTheDocument();
    expect(screen.getAllByText("sidecar refused the scan range").length).toBeGreaterThan(0);
    expect(screen.queryByText("No results yet")).not.toBeInTheDocument();
  });

  it("says a cancelled run was stopped", async () => {
    stubFetch({ runStatus: "cancelled" });
    render(scannerProviders(<IpScannerPage />));

    expect(await screen.findByText("Run stopped")).toBeInTheDocument();
    expect(
      screen.getByText("The run was stopped before any results were recorded."),
    ).toBeInTheDocument();
  });

  it("withdraws scan authorization when the workspace changes", async () => {
    stubFetch({ runs: [] });
    const sessionScopeId = createSessionScopeId();
    const queryClient = createScannerQueryClient();
    const view = render(
      scannerProviders(<IpScannerPage />, {
        authorizationEnforced: true,
        queryClient,
        sessionScopeId,
      }),
    );

    const consent = await screen.findByLabelText(/I am authorized to scan this network/i);
    fireEvent.click(consent);
    expect(consent).toBeChecked();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Start scan" })).not.toBeDisabled(),
    );

    // Same page, different project/site: the tick said "this network".
    view.rerender(
      scannerProviders(<IpScannerPage />, {
        authorizationEnforced: true,
        queryClient,
        sessionScopeId,
        workspace: { projectId: "other-project", siteId: "other-site" },
      }),
    );

    await waitFor(() =>
      expect(screen.getByLabelText(/I am authorized to scan this network/i)).not.toBeChecked(),
    );
    expect(screen.getByRole("button", { name: "Start scan" })).toBeDisabled();
  });

  it("clears a report confirmation when the run changes", async () => {
    const runB = { ...terminalRun, run_id: "run-ip-scanner-2" };
    let currentRun: Record<string, unknown> = terminalRun;
    stubFetch({ runs: () => [currentRun] });
    render(scannerProviders(<IpScannerPage />));

    const titleField = await screen.findByLabelText(/Report title/i);
    fireEvent.change(titleField, { target: { value: "Plant room sweep" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate report from this run" }));

    expect(await screen.findByText(/Report ID: rep-run-1./)).toBeInTheDocument();

    // A report describes ONE run; the confirmation must not follow the operator
    // onto the next one and claim run 1 report id belongs to run 2.
    currentRun = runB;
    window.dispatchEvent(new Event("visibilitychange"));
    // The refetch this event triggers is a real round trip through the query
    // cache; the default 1s window is tight on a loaded machine.
    await waitFor(() => expect(document.body.textContent).toContain("run-ip-scanner-2"), {
      timeout: 5000,
    });

    await waitFor(() => expect(screen.queryByText(/Report ID: rep-run-1./)).toBeNull());
    expect(screen.queryByText("Report generated")).not.toBeInTheDocument();
  });

  it("names the run in the footer and links Run History and Reports", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    await screen.findByText(/View in Run History/);
    expect(screen.getAllByText(RUN_ID, { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "View in Run History" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in Reports" })).toBeInTheDocument();
  });
});
