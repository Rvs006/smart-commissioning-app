import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearApiKey, setApiKey } from "../../api/client";
import { BacnetScannerPage } from "./BacnetScannerPage";
import { scannerRowsFromResults } from "./scannerRows";
import { scannerProviders } from "./scannerTestHarness";

const RUN_ID = "run-bacnet-scanner-1";

function jsonResponse(payload: unknown): Response {
  return { ok: true, status: 200, statusText: "OK", json: async () => payload } as unknown as Response;
}

const terminalRun = {
  run_id: RUN_ID,
  job_type: "bacnet_scanner",
  status: "succeeded",
  stage: "register_comparison",
  progress_percent: 100,
  created_at: "2026-09-14T09:00:00Z",
  updated_at: "2026-09-14T09:03:00Z",
  project_id: "demo-project",
  site_id: "demo-site",
  parameters: {},
  result_summary: {
    register_expected: 2,
    devices_discovered: 1,
    register_matches: 1,
    register_partial: 0,
    register_missing: 1,
    register_rogue: 0,
    points_exported: 54,
    backend: "bacpypes3",
    routers: [{ address: "10.0.10.2", networks: [2001, 2002] }],
  },
  error_message: null,
};

const results = {
  run_id: RUN_ID,
  job_type: "bacnet_scanner",
  status: "succeeded",
  result_summary: terminalRun.result_summary,
  discovered_assets: [
    {
      asset_id: "bacnet-device-2098101",
      device_instance: 2098101,
      address: "10.0.10.12",
      name: "AHU-01-CTRL",
      vendor: "Example Controls",
      model: "AC-100",
      firmware: "3.2.1",
      rag: "green",
      register_state: "match",
      last_seen_at: "2026-09-14T09:03:00Z",
    },
    {
      asset_id: "bacnet-device-2098140",
      device_instance: 2098140,
      address: "10.0.10.61",
      name: "VAV-3-14",
      rag: "red",
      register_state: "missing",
      last_seen_at: null,
    },
  ],
  devices: [
    {
      address: "10.0.10.12",
      device_type: "bacnet_device",
      name: "AHU-01-CTRL",
      vendor: "Example Controls",
      model: "AC-100",
      attributes: {
        asset_id: "bacnet-device-2098101",
        device_instance: 2098101,
        firmware: "3.2.1",
        rag: "green",
        register_state: "match",
        network: 2001,
        mac: "0a:1b",
        vendor_id: 10,
        system_status: "operational",
        object_count: 54,
        max_apdu: 1476,
        segmentation: "segmented-both",
        protocol_revision: 14,
        app_software: "BMS 5.4",
        name_status: "match",
        expected_name: "AHU-01-CTRL",
        location: "Plant room",
        description: "Air handling unit controller",
        points_truncated: false,
      },
    },
  ],
  points: [],
  topics: [],
};

const pointsPage = {
  run_id: RUN_ID,
  total: 2,
  has_more: false,
  next_cursor: null,
  points: [
    {
      id: "p1",
      position: 1,
      device_ref: "bacnet-device-2098101",
      point_name: "analogInput-1",
      units: "degC",
      observed_value: { value: "18.4" },
      attributes: { device_instance: 2098101 },
    },
    {
      id: "p2",
      position: 2,
      device_ref: "bacnet-device-2098101",
      point_name: "binaryValue-5",
      units: null,
      observed_value: { value: "active" },
      attributes: { device_instance: 2098101, read_error: "timeout" },
    },
  ],
};

let startBody: Record<string, unknown> | null = null;

function stubFetch(
  overrides: {
    runs?: () => unknown[];
    onObjectBrowse?: () => Promise<unknown>;
  } = {},
) {
  startBody = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) {
        return jsonResponse({ runs: overrides.runs ? overrides.runs() : [terminalRun] });
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse([
          {
            import_type: "bacnet_scanner_register",
            description: "Expected BACnet devices.",
            required_columns: ["asset_id", "device_instance"],
            duplicate_key_fields: ["asset_id"],
          },
        ]);
      }
      if (url.includes("/api/v1/imports/latest")) {
        return jsonResponse({
          import_id: "imp-b1",
          import_type: "bacnet_scanner_register",
          file_name: "bacnet-register.csv",
          status: "accepted",
          total_rows: 2,
          accepted_rows: 2,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T08:00:00Z",
        });
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse({ Network: { values: { "Source Interface": "10.0.10.5/24" } } });
      }
      if (url.endsWith("/api/v1/system/interfaces")) {
        return jsonResponse([]);
      }
      const runMatch = new RegExp("/api/v1/discovery/runs/([^/?]+)").exec(url);
      if (runMatch && !url.includes("/object-browse")) {
        const runId = runMatch[1];
        if (url.includes("/results")) {
          return jsonResponse({ ...results, run_id: runId });
        }
        if (url.includes("/points")) {
          return jsonResponse({ ...pointsPage, run_id: runId });
        }
        return jsonResponse({ ...terminalRun, run_id: runId });
      }
      if (url.endsWith("/api/v1/discovery/bacnet_sidecar/runs") && init?.method === "POST") {
        startBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse({
          run_id: RUN_ID,
          job_type: "bacnet_scanner",
          status: "queued",
          message: "BACnet scan accepted.",
        });
      }
      if (url.includes(`/object-browse`) && init?.method === "POST") {
        if (overrides.onObjectBrowse) {
          return jsonResponse(await overrides.onObjectBrowse());
        }
        return jsonResponse({
          run_id: RUN_ID,
          device_instance: 2098101,
          address: "10.0.10.12",
          count: 54,
          truncated: true,
          error: null,
          objects: [
            {
              type_name: "analogInput",
              instance: 1,
              name: "Supply Air Temp",
              present_value: "18.4",
              units: "degC",
            },
            {
              type_name: "binaryValue",
              instance: 5,
              name: "Fan Enable",
              present_value: "active",
              units: "",
            },
          ],
        });
      }
      if (url.includes(`/save-as-register`)) {
        return jsonResponse({
          import_id: "imp-saved-b1",
          import_type: "bacnet_scanner_register",
          file_name: "bacnet-scan-register-run-bacnet-scanner-1.csv",
          status: "accepted",
          total_rows: 1,
          accepted_rows: 1,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T09:06:00Z",
        });
      }
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

describe("BacnetScannerPage", () => {
  it("renders the setup card, six pills plus the objects count and the device columns", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />));

    expect(screen.getByRole("heading", { level: 1, name: "BACnet Discovery" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Scan setup" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Device instance range — low/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Device instance range — high/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Discovery window/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send Who-Is" })).toBeInTheDocument();

    const heading = await screen.findByRole("heading", { name: "Results" });
    const card = heading.closest("section") as HTMLElement;
    await within(card).findByText("2 Expected");
    for (const pill of ["1 Reachable", "1 Match", "0 Partial", "1 Missing", "0 Rogue"]) {
      expect(within(card).getByText(pill)).toBeInTheDocument();
    }
    expect(within(card).getByText("54 objects")).toBeInTheDocument();

    for (const column of ["Instance", "Name", "Address", "Net", "Vendor", "Model", "Firmware", "Objects", "Register"]) {
      expect(within(card).getByRole("columnheader", { name: column })).toBeInTheDocument();
    }
    expect(await within(card).findByText("2098101")).toBeInTheDocument();
    expect(within(card).getByText("3.2.1")).toBeInTheDocument();

    // The expected-but-silent device is a red row, not just an issue.
    const missingRow = within(card).getByText("2098140").closest("tr") as HTMLElement;
    expect(missingRow.className).toContain("row-fail");
    expect(within(missingRow).getByText("Missing")).toBeInTheDocument();
  });

  it("loads the live object list into the inline grid", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />));
    const cell = await screen.findByText("2098101");
    fireEvent.click(cell.closest("tr") as HTMLElement);

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "AHU-01-CTRL" })).toBeInTheDocument();
    expect(within(panel).getAllByText("1476").length).toBeGreaterThan(0);
    expect(within(panel).getAllByText("segmented-both").length).toBeGreaterThan(0);

    fireEvent.click(within(panel).getByRole("button", { name: "Load objects" }));

    expect(await within(panel).findByText("analogInput-1")).toBeInTheDocument();
    expect(within(panel).getByText("Supply Air Temp")).toBeInTheDocument();
    expect(within(panel).getByText("18.4")).toBeInTheDocument();
    expect(within(panel).getByText("degC")).toBeInTheDocument();
    expect(
      within(panel).getByText(/54 objects on device · showing 2 · list truncated at the read cap/),
    ).toBeInTheDocument();
  });

  it("discards an object-browse response that lands after the run changed", async () => {
    const runB = { ...terminalRun, run_id: "run-bacnet-scanner-2" };
    let currentRun: Record<string, unknown> = terminalRun;
    let releaseBrowse!: () => void;
    const browsePending = new Promise<void>((resolve) => {
      releaseBrowse = resolve;
    });
    stubFetch({
      runs: () => [currentRun],
      onObjectBrowse: async () => {
        await browsePending;
        // Run 1 values, for the same device instance run 2 also has.
        return {
          run_id: RUN_ID,
          device_instance: 2098101,
          address: "10.0.10.12",
          count: 1,
          truncated: false,
          error: null,
          objects: [
            {
              type_name: "analogInput",
              instance: 1,
              name: "Stale Supply Air Temp",
              present_value: "99.9",
              units: "degC",
            },
          ],
        };
      },
    });
    render(scannerProviders(<BacnetScannerPage />));

    const cell = await screen.findByText("2098101");
    fireEvent.click(cell.closest("tr") as HTMLElement);
    const panel = await screen.findByRole("complementary");
    fireEvent.click(within(panel).getByRole("button", { name: "Load objects" }));
    await within(panel).findByRole("button", { name: "Reading object list..." });

    // The operator moves to another run while the live read is still in flight.
    currentRun = runB;
    window.dispatchEvent(new Event("visibilitychange"));
    // The refetch this event triggers is a real round trip through the query
    // cache; the default 1s window is tight on a loaded machine.
    await waitFor(() => expect(document.body.textContent).toContain("run-bacnet-scanner-2"), {
      timeout: 5000,
    });

    releaseBrowse();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Reading object list..." })).toBeNull(),
    );

    // Re-open the SAME device instance under run 2. This is where the leak bit:
    // the stale result is still in state and the panel matches on
    // device_instance alone, so run 1 present values would render as run 2 live
    // read without ever pressing Load objects again.
    const resultsCard = (await screen.findByRole("heading", { name: "Results" })).closest(
      "section",
    ) as HTMLElement;
    const rowB = within(resultsCard).getByText("2098101");
    fireEvent.click(rowB.closest("tr") as HTMLElement);
    const panelB = await screen.findByRole("complementary");

    expect(within(panelB).queryByText("Stale Supply Air Temp")).not.toBeInTheDocument();
    expect(within(panelB).queryByText("99.9")).not.toBeInTheDocument();
    // The browse control is offered again, unread.
    expect(within(panelB).getByRole("button", { name: "Load objects" })).toBeInTheDocument();
  });

  it("blocks Send Who-Is on a half-filled instance range", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />));
    const low = await screen.findByLabelText(/Device instance range — low/i);
    fireEvent.change(low, { target: { value: "100" } });

    const start = screen.getByRole("button", { name: "Send Who-Is" });
    await waitFor(() => expect(start).toBeDisabled());
    expect(screen.getByRole("alert")).toHaveTextContent(/Enter both bounds or leave both blank/);
  });

  // Moved from ModulePage.test.tsx (P2b) when the sidecar branches were deleted:
  // this lane posts authorized=scanAuthorized and the server rejects an
  // unauthorized run, so Start must stay disabled until the operator confirms
  // rather than clicking through to a failure. Only an enforcing deployment
  // shows the checkbox; the portable default authorizes implicitly.
  it("holds Send Who-Is until scan authorization is confirmed where it is enforced", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />, { authorizationEnforced: true }));

    const authorize = await screen.findByLabelText(/I am authorized to scan this network/i);
    expect(authorize).not.toBeChecked();
    const start = screen.getByRole("button", { name: "Send Who-Is" });
    await waitFor(() => expect(start).toBeDisabled());

    fireEvent.click(authorize);
    await waitFor(() => expect(start).not.toBeDisabled());
  });

  it("posts the parameters the sidecar adapter reads", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />));
    fireEvent.change(await screen.findByLabelText(/Device instance range — low/i), {
      target: { value: "100" },
    });
    fireEvent.change(screen.getByLabelText(/Device instance range — high/i), {
      target: { value: "200" },
    });
    fireEvent.change(screen.getByLabelText(/Discovery window/i), { target: { value: "5000" } });
    fireEvent.click(screen.getByLabelText(/Ignore register for this run/i));

    const start = screen.getByRole("button", { name: "Send Who-Is" });
    await waitFor(() => expect(start).not.toBeDisabled());
    fireEvent.click(start);

    await waitFor(() => expect(startBody).not.toBeNull());
    expect(startBody).toMatchObject({
      job_type: "bacnet_scanner",
      parameters: {
        authorized: true,
        low: 100,
        high: 200,
        discoverMs: 5000,
        ignore_register: true,
      },
    });
  });

  it("keeps the routers table and the paged point rows from the module page", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />));

    const routers = await screen.findByRole("heading", { name: "Routers / BBMDs" });
    const routersCard = routers.closest("section") as HTMLElement;
    expect(within(routersCard).getByText("10.0.10.2")).toBeInTheDocument();
    expect(within(routersCard).getByText("1 router")).toBeInTheDocument();

    const points = await screen.findByRole("heading", { name: "Points / live data" });
    const pointsCard = points.closest("section") as HTMLElement;
    expect(await within(pointsCard).findByText("analogInput-1")).toBeInTheDocument();
    expect(within(pointsCard).getByText("18.4")).toBeInTheDocument();
    // A point whose read failed says so instead of reporting a value as read.
    expect(within(pointsCard).getByText("Read failed")).toBeInTheDocument();
    expect(within(pointsCard).getByLabelText("Search points")).toBeInTheDocument();
  });

  it("saves the scan as a BACnet register and offers export assets", async () => {
    stubFetch();
    render(scannerProviders(<BacnetScannerPage />));
    expect(
      await screen.findByRole("button", { name: "Export assets & points" }),
    ).toBeInTheDocument();

    const save = screen.getByRole("button", {
      name: /Save scan as register \(applies to the next scan\)/,
    });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    expect(await screen.findByText(/Saved as register/)).toBeInTheDocument();
    expect(
      screen.getByText(/It is stored here and applies automatically to the next BACnet scan/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download register CSV" })).toBeInTheDocument();
  });
});

describe("BACnet row projection", () => {
  it("keeps two instance-less silent devices as separate rows", () => {
    // The vendored compare() spells a blank/unparseable Device Instance "—", so
    // two such register rows share one instance. Keying a row on the instance
    // alone would collapse them and hide a device the operator must chase.
    const rows = scannerRowsFromResults("bacnet", {
      run_id: RUN_ID,
      job_type: "bacnet_scanner",
      status: "succeeded",
      result_summary: {},
      discovered_assets: [
        {
          asset_id: null,
          device_instance: "—",
          address: "10.0.10.71",
          name: "FCU-1",
          rag: "red",
          register_state: "missing",
          last_seen_at: null,
        },
        {
          asset_id: null,
          device_instance: "—",
          address: "10.0.10.72",
          name: "FCU-2",
          rag: "red",
          register_state: "missing",
          last_seen_at: null,
        },
      ],
      devices: [],
      points: [],
      topics: [],
    });

    expect(rows).toHaveLength(2);
    expect(new Set(rows.map((row) => row.id)).size).toBe(2);
    expect(rows.every((row) => row.missing && row.tone === "fail")).toBe(true);
    expect(rows.map((row) => row.cells.Name?.text)).toEqual([
      "expected · FCU-1",
      "expected · FCU-2",
    ]);
  });
});
