import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeviceDetailPanel } from "./DeviceDetailPanel";
import {
  SCANNER_PANEL_DEFAULT_WIDTH,
  SCANNER_PANEL_MAX_WIDTH,
  SCANNER_PANEL_MIN_WIDTH,
  clampPanelWidth,
  scannerRowsFromResults,
  type ScannerRow,
} from "./scannerRows";

function ipRow(): ScannerRow {
  const [row] = scannerRowsFromResults("ip", {
    run_id: "run-1",
    job_type: "ip_scanner",
    status: "succeeded",
    result_summary: {},
    discovered_assets: [
      {
        asset_id: null,
        ip_address: "10.0.10.12",
        mac_address: "00:80:F4:11:22:33",
        hostname: "ahu-01",
        observed_ports: [{ port: 443, protocol: "tcp" }],
        match_basis: "ip",
        status_detail: "reachable/partial",
        last_seen_at: "2026-09-14T09:02:00Z",
        rag: "amber",
        register: "partial",
      },
    ],
    devices: [
      {
        address: "10.0.10.12",
        name: "ahu-01",
        vendor: "Example Controls",
        attributes: {
          rag: "amber",
          register: "partial",
          status: "reachable",
          latency: 5,
          hostname_status: "mismatch",
          expected_hostname: "ahu-1",
          hostname: "ahu-01",
          expected_ports: [443, 502],
          missing_ports: [502],
          extra_ports: [],
          services: [{ proto: "tcp", port: 443, name: "https", tls: true, info: "TLS 1.3" }],
        },
      },
    ],
    points: [],
    topics: [],
  });
  return row;
}

function bacnetMissingRow(): ScannerRow {
  const [row] = scannerRowsFromResults("bacnet", {
    run_id: "run-2",
    job_type: "bacnet_scanner",
    status: "succeeded",
    result_summary: {},
    discovered_assets: [
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
    devices: [],
    points: [],
    topics: [],
  });
  return row;
}

const noop = () => {};

describe("DeviceDetailPanel", () => {
  it("prompts for a selection when no row is chosen", () => {
    render(
      <DeviceDetailPanel
        expanded={false}
        lane="ip"
        onClose={noop}
        onResize={noop}
        onToggleExpand={noop}
        row={null}
        width={SCANNER_PANEL_DEFAULT_WIDTH}
      />,
    );
    const panel = screen.getByRole("complementary");
    expect(within(panel).getByText("Select a device to view details.")).toBeInTheDocument();
  });

  it("renders the IP sections, port diff and the service line formatting", () => {
    render(
      <DeviceDetailPanel
        expanded={false}
        lane="ip"
        onClose={noop}
        onResize={noop}
        onToggleExpand={noop}
        row={ipRow()}
        width={SCANNER_PANEL_DEFAULT_WIDTH}
      />,
    );
    const panel = screen.getByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "ahu-01" })).toBeInTheDocument();
    for (const section of ["Device overview", "Live health", "Hostname check", "Network / Register"]) {
      expect(within(panel).getByRole("heading", { name: section })).toBeInTheDocument();
    }
    expect(within(panel).getByText("Does not match register")).toBeInTheDocument();
    expect(within(panel).getByText("Missing expected: 502")).toBeInTheDocument();
    // svcDescr falls back to `info` when no product/title/cert is known.
    expect(within(panel).getByText("tcp/443 https 🔒")).toBeInTheDocument();
    expect(within(panel).getByText("TLS 1.3")).toBeInTheDocument();
    // Status and register chips both read off the row's own verdict.
    const badges = panel.querySelector(".scanner-detail-badges") as HTMLElement;
    expect(within(badges).getByText("Reachable")).toBeInTheDocument();
    expect(within(badges).getByText("Partial")).toBeInTheDocument();
  });

  it("says a never-observed device was expected and hides the object browse", () => {
    render(
      <DeviceDetailPanel
        expanded={false}
        lane="bacnet"
        objectBrowse={{
          canBrowse: true,
          blockedReason: null,
          pending: false,
          error: null,
          result: null,
          onLoad: noop,
        }}
        onClose={noop}
        onResize={noop}
        onToggleExpand={noop}
        row={bacnetMissingRow()}
        width={SCANNER_PANEL_DEFAULT_WIDTH}
      />,
    );
    const panel = screen.getByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "Expected, no response" })).toBeInTheDocument();
    expect(within(panel).getByText("2098140")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Load objects" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("heading", { name: "Device identity" })).not.toBeInTheDocument();
  });

  it("closes on the ✕ control and collapses an expanded panel on Escape", () => {
    const onClose = vi.fn();
    const onToggleExpand = vi.fn();
    render(
      <DeviceDetailPanel
        expanded
        lane="bacnet"
        onClose={onClose}
        onResize={noop}
        onToggleExpand={onToggleExpand}
        row={bacnetMissingRow()}
        width={SCANNER_PANEL_DEFAULT_WIDTH}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Close detail panel" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onToggleExpand).toHaveBeenCalledTimes(1);
  });

  it("resizes from the keyboard within the 320-640 bounds", () => {
    const onResize = vi.fn();
    render(
      <DeviceDetailPanel
        expanded={false}
        lane="ip"
        onClose={noop}
        onResize={onResize}
        onToggleExpand={noop}
        row={ipRow()}
        width={SCANNER_PANEL_DEFAULT_WIDTH}
      />,
    );
    const resizer = screen.getByRole("separator", { name: "Resize detail panel" });
    fireEvent.keyDown(resizer, { key: "ArrowLeft" });
    expect(onResize).toHaveBeenCalledWith(SCANNER_PANEL_DEFAULT_WIDTH + 16);

    expect(clampPanelWidth(10)).toBe(SCANNER_PANEL_MIN_WIDTH);
    expect(clampPanelWidth(10_000)).toBe(SCANNER_PANEL_MAX_WIDTH);
    expect(clampPanelWidth(Number.NaN)).toBe(SCANNER_PANEL_DEFAULT_WIDTH);
  });
});
