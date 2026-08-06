import type { JobSummary, UdmiReportScopeV1 } from "../../api/client";
import { createSessionScopeId, DEFAULT_WORKSPACE } from "../../app/sessionScope";
import { queryKeys } from "../../api/queryKeys";
import {
  assetIdentity,
  createReportIntent,
  evidenceRequirementsFor,
  initialRunControllerState,
  latestAttachableRun,
  payloadIdentity,
  resultIdentity,
  runControllerReducer,
  topicIdentity,
  toRunRef,
} from "./runIsolation";

const run = (overrides: Partial<JobSummary>): JobSummary => ({
  run_id: "run-1",
  job_type: "udmi_validation",
  status: "succeeded",
  stage: "complete",
  progress_percent: 100,
  created_at: "2026-07-26T10:00:00Z",
  updated_at: "2026-07-26T10:01:00Z",
  edge_id: null,
  ...overrides,
});

describe("frontend run and session isolation", () => {
  it("separates route/run/session query identities and gives reports one shared family", () => {
    const first = createSessionScopeId();
    const second = createSessionScopeId();
    const ipRun = toRunRef(first, DEFAULT_WORKSPACE, "ip-scanner", run({
      job_type: "ip_discovery",
      run_id: "run-ip",
    }));
    const mqttRun = toRunRef(first, DEFAULT_WORKSPACE, "mqtt-discovery", run({
      job_type: "mqtt_discovery",
      run_id: "run-mqtt",
    }));

    expect(queryKeys.run(first, DEFAULT_WORKSPACE, ipRun)).not.toEqual(
      queryKeys.run(first, DEFAULT_WORKSPACE, mqttRun),
    );
    expect(queryKeys.results(first, DEFAULT_WORKSPACE, ipRun)).not.toEqual(
      queryKeys.results(first, DEFAULT_WORKSPACE, mqttRun),
    );
    expect(queryKeys.run(first, DEFAULT_WORKSPACE, ipRun)).not.toEqual(
      queryKeys.run(second, DEFAULT_WORKSPACE, { ...ipRun, sessionScopeId: second }),
    );
    expect(queryKeys.reports(first, DEFAULT_WORKSPACE).slice(-1)).toEqual(["reports"]);
    expect(queryKeys.reports(first, DEFAULT_WORKSPACE)).toEqual(
      queryKeys.reportList(first, DEFAULT_WORKSPACE),
    );
  });

  it("ignores an SSE terminal event tagged for another run", () => {
    const scope = createSessionScopeId();
    const active = toRunRef(scope, DEFAULT_WORKSPACE, "mqtt-discovery", run({
      job_type: "mqtt_discovery",
      run_id: "run-b",
      status: "running",
    }));
    const started = runControllerReducer(initialRunControllerState, {
      type: "accepted",
      runRef: active,
    });
    const unchanged = runControllerReducer(started, {
      type: "terminal-observed",
      runId: "run-a",
    });

    expect(unchanged).toBe(started);
    expect(unchanged.phase).toBe("active");
  });

  it("settles only after every terminal evidence requirement succeeds", () => {
    const scope = createSessionScopeId();
    const active = toRunRef(scope, DEFAULT_WORKSPACE, "mqtt-discovery", run({
      job_type: "mqtt_discovery",
      run_id: "run-mqtt",
      status: "running",
    }));
    let state = runControllerReducer(initialRunControllerState, {
      type: "accepted",
      runRef: active,
    });
    state = runControllerReducer(state, { type: "terminal-observed", runId: "run-mqtt" });
    expect(state.phase).toBe("terminal-sync");
    expect(evidenceRequirementsFor(active)).toEqual(["run", "results", "topics"]);

    state = runControllerReducer(state, {
      type: "evidence-succeeded",
      runId: "run-mqtt",
      requirements: ["run", "results"],
    });
    expect(state.phase).toBe("terminal-sync");

    state = runControllerReducer(state, {
      type: "evidence-succeeded",
      runId: "run-mqtt",
      requirements: ["topics"],
    });
    expect(state.phase).toBe("settled");
  });

  it("freezes report run, format, filters, and asset scope when the dialog opens", () => {
    const scope: UdmiReportScopeV1 = {
      schema_version: "1.0",
      selected_payloads: [
        { source_run_id: "run-1", asset_id: "asset-a", payload_type: "state" },
      ],
      unexpected_device_ids: [],
      filters: {
        text: "ahu",
        verdict: "fail",
        topic_contains: "events",
        system: "air",
        observation: "observed",
        category: "validation",
      },
    };
    const intent = createReportIntent({
      format: "pdf",
      reportType: "udmi_validation",
      runId: "run-1",
      udmiReportVariant: "technical",
      udmiScope: scope,
    });

    scope.selected_payloads[0].asset_id = "asset-b";
    scope.filters.text = "changed";

    expect(intent).toMatchObject({
      format: "pdf",
      reportType: "udmi_validation",
      runId: "run-1",
      udmiReportVariant: "technical",
    });
    expect(intent.udmiScope?.selected_payloads[0].asset_id).toBe("asset-a");
    expect(intent.udmiScope?.filters.text).toBe("ahu");
    expect(Object.isFrozen(intent)).toBe(true);
  });

  it("keeps result selection attached to the same evidence after rows reorder", () => {
    const selected = { Asset: "asset-b", Payload: "UDMI pointset", __payloadType: "pointset" };
    const before = [
      { Asset: "asset-a", Payload: "UDMI state", __payloadType: "state" },
      selected,
    ];
    const after = [selected, before[0]];
    const id = resultIdentity("udmi-validation", before[1]);

    expect(after.find((row) => resultIdentity("udmi-validation", row) === id)).toBe(selected);
  });

  it("uses content-based identities for assets, payloads, and topics", () => {
    expect(assetIdentity("ip-scanner", { Asset: "ahu-1", Address: "10.0.0.8" })).toBe(
      assetIdentity("ip-scanner", { Asset: "ahu-1", Address: "10.0.0.8" }),
    );
    expect(payloadIdentity({ Asset: "ahu-1", __payloadType: "state" })).not.toBe(
      payloadIdentity({ Asset: "ahu-1", __payloadType: "pointset" }),
    );
    expect(topicIdentity({ Topic: "/devices/ahu-1/state" })).not.toBe(
      topicIdentity({ Topic: "/devices/ahu-2/state" }),
    );
  });

  it("restores the newest failed or cancelled run when no run remains active", () => {
    const failed = run({ run_id: "failed", status: "failed", created_at: "2026-07-26T11:00:00Z" });
    const cancelled = run({
      run_id: "cancelled",
      status: "cancelled",
      created_at: "2026-07-26T12:00:00Z",
    });

    expect(latestAttachableRun([failed, cancelled])?.run_id).toBe("cancelled");
  });
});
