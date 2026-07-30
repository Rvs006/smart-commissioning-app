import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RunRecord, UdmiAssetTopicDiscovery, UdmiValidationSummaryV1 } from "../../api/client";
import { LiveRunConsole } from "./LiveRunConsole";

const run: RunRecord = {
  run_id: "run-live-console",
  job_type: "udmi_validation",
  status: "running",
  stage: "capturing_live_mqtt",
  progress_percent: 35,
  created_at: "2026-07-30T09:00:00Z",
  updated_at: "2026-07-30T09:01:00Z",
  edge_id: null,
  project_id: "demo-project",
  site_id: "demo-site",
  parameters: {},
  result_summary: {},
  error_message: null,
};

const validationSummary: UdmiValidationSummaryV1 = {
  schema_version: "1.0",
  asset_metrics: {
    expected: 4,
    observed: 2,
    not_observed: 2,
    with_issues: 0,
    successfully_validated: 0,
  },
  payload_metrics: {
    expected: 4,
    received: 2,
    with_issues: 0,
    successfully_validated: 0,
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
  asset_results: [],
  fault_rows: [],
};

function consoleView(
  assetTopicDiscovery: UdmiAssetTopicDiscovery | null,
  runRecord: RunRecord = run,
  summary: UdmiValidationSummaryV1 = validationSummary,
) {
  return (
    <LiveRunConsole
      assetTopicDiscovery={assetTopicDiscovery}
      elapsed="1m 0s"
      issueCount={0}
      progress={35}
      run={runRecord}
      stage="capturing_live_mqtt"
      status="running"
      validationSummary={summary}
    />
  );
}

function renderConsole(assetTopicDiscovery: UdmiAssetTopicDiscovery | null) {
  return render(consoleView(assetTopicDiscovery));
}

function topicAssetResult(
  assetId: string,
  status: UdmiAssetTopicDiscovery["asset_results"][number]["status"],
): UdmiAssetTopicDiscovery["asset_results"][number] {
  return {
    asset_id: assetId,
    system: "Demo",
    expected_topic_root: "",
    expected_topics: [],
    observed_expected_topics: [],
    observed_alternate_topics: [],
    matched_message_count: 0,
    status,
    topic_limit_reached: false,
  };
}

describe("LiveRunConsole", () => {
  it("shows registered asset observations without inventing topic evidence", () => {
    renderConsole(null);

    const console = screen.getByRole("region", { name: "Live run console" });
    expect(within(console).getByText("Registered assets observed")).toBeInTheDocument();
    expect(within(console).getByText("2 of 4 expected")).toBeInTheDocument();
    expect(
      within(console).getByText(/Recent evidence snapshots, not a broker message rate/i),
    ).toBeInTheDocument();
    expect(within(console).getAllByText("Waiting for evidence")).toHaveLength(3);
    expect(
      within(console).getByText(/UI\/run-state sampling every second/i),
    ).toBeInTheDocument();
  });

  it("shows complete topic-observation counts separately from asset observations", () => {
    renderConsole({
      enabled: true,
      scope: "register/#",
      scope_source: "register_common_ancestor",
      scope_error: null,
      topic_limit_per_asset: 10,
      capture_complete: true,
      capture_status: "completed",
      asset_results: [],
      status_counts: {
        expected_topic_observed: 2,
        alternate_topic_observed: 1,
        no_matching_asset_id_topic_observed: 1,
      },
    });

    const breakdown = screen.getByRole("region", { name: "Topic observation breakdown" });
    expect(within(breakdown).getByText("Expected-topic observed")).toBeInTheDocument();
    expect(within(breakdown).getByText("2 / 4 assets")).toBeInTheDocument();
    const alternate = within(breakdown).getByText("Alternate-topic observed").parentElement;
    const noMatch = within(breakdown).getByText("No matching asset-ID topic observed").parentElement;
    expect(alternate).not.toBeNull();
    expect(noMatch).not.toBeNull();
    expect(within(alternate as HTMLElement).getByText("1 / 4 assets")).toBeInTheDocument();
    expect(within(noMatch as HTMLElement).getByText("1 / 4 assets")).toBeInTheDocument();
  });

  it("waits for a capture outcome before reporting no matching topics", () => {
    renderConsole({
      enabled: true,
      scope: "register/#",
      scope_source: "register_common_ancestor",
      scope_error: null,
      topic_limit_per_asset: 10,
      capture_complete: false,
      capture_status: "live_capture_in_progress",
      asset_results: [],
      status_counts: {
        expected_topic_observed: 1,
        alternate_topic_observed: 0,
        capture_incomplete: 3,
      },
    });

    const breakdown = screen.getByRole("region", { name: "Topic observation breakdown" });
    expect(within(breakdown).getByText("1 / 4 assets")).toBeInTheDocument();
    expect(within(breakdown).getByText("0 / 4 assets")).toBeInTheDocument();
    expect(within(breakdown).getByText("Waiting for capture outcome")).toBeInTheDocument();
  });

  it("waits for evidence when the topic ledger has no usable scope", () => {
    renderConsole({
      enabled: true,
      scope: null,
      scope_source: "unavailable",
      scope_error: null,
      topic_limit_per_asset: 10,
      capture_complete: true,
      capture_status: "completed",
      asset_results: [],
      status_counts: {
        expected_topic_observed: 0,
        alternate_topic_observed: 0,
        no_matching_asset_id_topic_observed: 0,
      },
    });

    const breakdown = screen.getByRole("region", { name: "Topic observation breakdown" });
    expect(within(breakdown).getAllByText("Waiting for evidence")).toHaveLength(3);
    expect(within(breakdown).getByText("Topic evidence pending")).toBeInTheDocument();
    expect(within(breakdown).queryByText("Capture outcome recorded")).not.toBeInTheDocument();
    expect(within(breakdown).queryByText("0 / 4 assets")).not.toBeInTheDocument();
  });

  it("seeds a fresh evidence snapshot when the run changes without new metrics", () => {
    const { rerender } = render(consoleView(null));

    expect(screen.getByText("1 recent evidence snapshot")).toBeInTheDocument();

    rerender(consoleView(null, { ...run, run_id: "run-live-console-next" }));

    expect(screen.getByText("1 recent evidence snapshot")).toBeInTheDocument();
    expect(screen.queryByText("2 recent evidence snapshots")).not.toBeInTheDocument();
  });

  it("reconstructs missing topic counts without overriding direct counts", () => {
    renderConsole({
      enabled: true,
      scope: "register/#",
      scope_source: "register_common_ancestor",
      scope_error: null,
      topic_limit_per_asset: 10,
      capture_complete: true,
      capture_status: "completed",
      asset_results: [
        topicAssetResult("DEMO-01", "expected_topic_observed"),
        topicAssetResult("DEMO-02", "expected_topic_observed"),
        topicAssetResult("DEMO-03", "alternate_topic_observed"),
        topicAssetResult("DEMO-04", "no_matching_asset_id_topic_observed"),
      ],
      status_counts: {
        expected_topic_observed: 1,
        alternate_topic_observed: 0,
      },
    });

    const breakdown = screen.getByRole("region", { name: "Topic observation breakdown" });
    const expected = within(breakdown).getByText("Expected-topic observed").parentElement;
    const alternate = within(breakdown).getByText("Alternate-topic observed").parentElement;
    const noMatch = within(breakdown).getByText("No matching asset-ID topic observed").parentElement;
    expect(expected).not.toBeNull();
    expect(alternate).not.toBeNull();
    expect(noMatch).not.toBeNull();
    expect(within(expected as HTMLElement).getByText("1 / 4 assets")).toBeInTheDocument();
    expect(within(alternate as HTMLElement).getByText("0 / 4 assets")).toBeInTheDocument();
    expect(within(noMatch as HTMLElement).getByText("1 / 4 assets")).toBeInTheDocument();
  });
});
