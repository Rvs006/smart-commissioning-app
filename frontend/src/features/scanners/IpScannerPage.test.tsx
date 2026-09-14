import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearApiKey, setApiKey } from "../../api/client";
import { IpScannerPage } from "./IpScannerPage";
import { scannerProviders } from "./scannerTestHarness";

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
      asset_id: null,
      ip_address: "10.0.10.20",
      mac_address: null,
      hostname: "chiller-2",
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

function stubFetch(overrides: { runs?: unknown[] } = {}) {
  startBody = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) {
        return jsonResponse({ runs: overrides.runs ?? [terminalRun] });
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
          file_name: "ip-register.csv",
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
      if (url.includes(`/api/v1/discovery/runs/${RUN_ID}/results`)) {
        return jsonResponse(results);
      }
      if (url.includes(`/api/v1/discovery/runs/${RUN_ID}`)) {
        return jsonResponse(terminalRun);
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
      if (url.includes(`/api/v1/discovery/ip_sidecar/runs/${RUN_ID}/save-as-register`)) {
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
    // Pete's svcDescr formatting, reproduced for the services list.
    expect(within(panel).getByText("tcp/443 https 🔒")).toBeInTheDocument();
    expect(
      within(panel).getByText('nginx 1.24 · “Plant controller” · cert: ahu-01.local'),
    ).toBeInTheDocument();
  });

  it("says a missing device was expected and shows no observed evidence", async () => {
    stubFetch();
    render(scannerProviders(<IpScannerPage />));
    const cell = await screen.findByText("10.0.10.20");
    fireEvent.click(cell.closest("tr") as HTMLElement);

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "Expected, no response" })).toBeInTheDocument();
    expect(within(panel).queryByRole("heading", { name: "Live health" })).not.toBeInTheDocument();
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
      screen.getByText(/The next IP scan for this project and site compares against it\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download register CSV" })).toBeInTheDocument();
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
