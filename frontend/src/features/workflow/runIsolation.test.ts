import type {
  DiscoveryObservationPage,
  DiscoveryObservationRecord,
  JobSummary,
  UdmiReportScopeV1,
} from "../../api/client";
import { createSessionScopeId, DEFAULT_WORKSPACE } from "../../app/sessionScope";
import { queryKeys } from "../../api/queryKeys";
import {
  assetIdentity,
  createReportIntent,
  createObservationFoldState,
  evidenceRequirementsFor,
  initialRunControllerState,
  isObservationTerminalSynchronized,
  latestAttachableRun,
  foldObservationPage,
  payloadIdentity,
  resultIdentity,
  runControllerReducer,
  topicIdentity,
  toRunRef,
} from "./runIsolation";

const observation = (
  overrides: Partial<DiscoveryObservationRecord> = {},
): DiscoveryObservationRecord => ({
  cursor: 1,
  run_id: "run-1",
  attempt: 2,
  protocol: "ip",
  entity_kind: "host",
  entity_key: "192.0.2.8",
  entity_version: 1,
  event_key: "host:192.0.2.8:v1",
  phase: "reachability",
  outcome: "observed",
  payload_schema_version: "1.0",
  payload: { address: "192.0.2.8" },
  payload_sha256: "a".repeat(64),
  observed_at: "2026-08-11T02:00:00Z",
  created_at: "2026-08-11T02:00:01Z",
  ...overrides,
});

const observationPage = (
  observations: DiscoveryObservationRecord[],
  overrides: Partial<DiscoveryObservationPage> = {},
): DiscoveryObservationPage => {
  const lastCursor = observations.length > 0 ? observations[observations.length - 1].cursor : 0;
  return {
    run_id: "run-1",
    attempt: 2,
    observations,
    next_cursor: lastCursor,
    latest_cursor: lastCursor,
    has_more: false,
    terminal: null,
    ...overrides,
  };
};

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
  it("folds one run-attempt page atomically and keeps the highest entity version", () => {
    const state = createObservationFoldState("run-1", 2);
    const folded = foldObservationPage(
      state,
      observationPage([
        observation(),
        observation({
          cursor: 2,
          entity_version: 2,
          event_key: "host:192.0.2.8:v2",
          payload: { address: "192.0.2.8", hostname: "ahu-8" },
          payload_sha256: "b".repeat(64),
        }),
        observation({
          cursor: 3,
          entity_version: 1,
          event_key: "host:192.0.2.8:late-v1",
          payload_sha256: "c".repeat(64),
        }),
      ]),
    );

    expect(folded.acknowledgedCursor).toBe(3);
    expect([...folded.entities.values()]).toEqual([
      expect.objectContaining({ entity_version: 2, payload_sha256: "b".repeat(64) }),
    ]);
    expect(folded.events.size).toBe(3);
    expect(folded.resnapshotRequired).toBe(false);
  });

  it("treats an identical event replay as idempotent", () => {
    const first = foldObservationPage(
      createObservationFoldState("run-1", 2),
      observationPage([observation()]),
    );
    const replayed = foldObservationPage(
      first,
      observationPage(
        [
          observation({
            cursor: 2,
            entity_key: "192.0.2.99",
            entity_version: 99,
          }),
        ],
        {
          next_cursor: 2,
          latest_cursor: 2,
        },
      ),
    );

    expect(replayed.events.size).toBe(1);
    expect(replayed.entities.size).toBe(1);
    expect([...replayed.entities.values()][0]).toMatchObject({
      entity_key: "192.0.2.8",
      entity_version: 1,
    });
    expect(replayed.acknowledgedCursor).toBe(2);
    expect(replayed.resnapshotRequired).toBe(false);
  });

  it("preserves state identity for an unchanged observation page", () => {
    const first = foldObservationPage(
      createObservationFoldState("run-1", 2),
      observationPage([observation()]),
    );
    const unchanged = foldObservationPage(
      first,
      observationPage([], {
        next_cursor: first.acknowledgedCursor,
        latest_cursor: first.latestCursor,
        has_more: first.hasMore,
      }),
    );

    expect(unchanged).toBe(first);
  });

  it("requests a resnapshot without partially folding a conflicting page", () => {
    const first = foldObservationPage(
      createObservationFoldState("run-1", 2),
      observationPage([observation()]),
    );
    const conflicting = foldObservationPage(
      first,
      observationPage(
        [
          observation({
            cursor: 2,
            event_key: "new-event",
            entity_key: "192.0.2.9",
            payload_sha256: "b".repeat(64),
          }),
          observation({ cursor: 3, payload_sha256: "f".repeat(64) }),
        ],
        { next_cursor: 3, latest_cursor: 3 },
      ),
    );

    expect(conflicting).toMatchObject({
      acknowledgedCursor: 1,
      resnapshotRequired: true,
      resnapshotReason: "event_conflict",
    });
    expect(conflicting.events.size).toBe(1);
    expect(conflicting.entities.size).toBe(1);
    expect(
      foldObservationPage(
        conflicting,
        observationPage(
          [
            observation({
              cursor: 4,
              event_key: "ignored-after-conflict",
              payload_sha256: "d".repeat(64),
            }),
          ],
          { next_cursor: 4, latest_cursor: 4 },
        ),
      ),
    ).toBe(conflicting);
  });

  it.each([
    ["run mismatch", observationPage([], { run_id: "run-2" }), "run_mismatch"],
    ["page attempt mismatch", observationPage([], { attempt: 3 }), "attempt_mismatch"],
    [
      "row attempt mismatch",
      observationPage([observation({ attempt: 3, cursor: 5 })], {
        next_cursor: 5,
        latest_cursor: 5,
      }),
      "attempt_mismatch",
    ],
    [
      "cursor regression",
      observationPage([], { next_cursor: 3, latest_cursor: 9 }),
      "cursor_regression",
    ],
  ] as const)("requests a resnapshot on %s", (_label, page, reason) => {
    const state = createObservationFoldState("run-1", 2, 4);
    const folded = foldObservationPage(state, page);

    expect(folded).toMatchObject({
      acknowledgedCursor: 4,
      resnapshotRequired: true,
      resnapshotReason: reason,
    });
    expect(folded.events.size).toBe(0);
    expect(folded.entities.size).toBe(0);
  });

  it("waits for the acknowledged page cursor to reach the sealed terminal cursor", () => {
    const waiting = foldObservationPage(
      createObservationFoldState("run-1", 2),
      observationPage(
        [
          observation({ cursor: 1 }),
          observation({ cursor: 2, event_key: "event-2", payload_sha256: "b".repeat(64) }),
          observation({ cursor: 3, event_key: "event-3", payload_sha256: "c".repeat(64) }),
        ],
        {
          next_cursor: 3,
          latest_cursor: 5,
          has_more: true,
          terminal: { status: "succeeded", terminal_cursor: 5 },
        },
      ),
    );
    const caughtUp = foldObservationPage(
      waiting,
      observationPage(
        [
          observation({ cursor: 4, event_key: "event-4", payload_sha256: "d".repeat(64) }),
          observation({ cursor: 5, event_key: "event-5", payload_sha256: "e".repeat(64) }),
        ],
        {
          next_cursor: 5,
          latest_cursor: 5,
        },
      ),
    );

    expect(isObservationTerminalSynchronized(waiting)).toBe(false);
    expect(isObservationTerminalSynchronized(caughtUp)).toBe(true);
    expect(caughtUp.acknowledgedCursor).toBe(5);
  });

  it("crosses a pruned terminal stream to sealed authority without retaining provisional rows", () => {
    const provisional = foldObservationPage(
      createObservationFoldState("run-1", 2),
      observationPage([observation()]),
    );

    const pruned = foldObservationPage(provisional, {
      ...observationPage([], {
        latest_cursor: 8,
        next_cursor: 1,
        terminal: { status: "succeeded", terminal_cursor: 8 },
      }),
      observations_pruned: true,
    });

    expect(pruned).toMatchObject({
      acknowledgedCursor: 8,
      observationsPruned: true,
    });
    expect(isObservationTerminalSynchronized(pruned)).toBe(true);
    expect(pruned.events.size).toBe(0);
    expect(pruned.entities.size).toBe(0);
  });

  it("quarantines provisional rows separately from retention pruning", () => {
    const provisional = foldObservationPage(
      createObservationFoldState("run-1", 2),
      observationPage([observation()]),
    );

    const quarantined = foldObservationPage(provisional, {
      ...observationPage([], {
        latest_cursor: 8,
        next_cursor: 1,
        terminal: { status: "succeeded", terminal_cursor: 8 },
      }),
      observations_quarantined: true,
    });

    expect(quarantined).toMatchObject({
      acknowledgedCursor: 8,
      observationsPruned: false,
      observationsQuarantined: true,
    });
    expect(isObservationTerminalSynchronized(quarantined)).toBe(true);
    expect(quarantined.events.size).toBe(0);
    expect(quarantined.entities.size).toBe(0);
  });

  it("separates route/run/session query identities and gives reports one shared family", () => {
    const first = createSessionScopeId();
    const second = createSessionScopeId();
    const ipRun = toRunRef(
      first,
      DEFAULT_WORKSPACE,
      "ip-scanner",
      run({
      job_type: "ip_discovery",
      run_id: "run-ip",
      }),
    );
    const mqttRun = toRunRef(
      first,
      DEFAULT_WORKSPACE,
      "mqtt-discovery",
      run({
      job_type: "mqtt_discovery",
      run_id: "run-mqtt",
      }),
    );

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
    const active = toRunRef(
      scope,
      DEFAULT_WORKSPACE,
      "mqtt-discovery",
      run({
      job_type: "mqtt_discovery",
      run_id: "run-b",
      status: "running",
      }),
    );
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
    const active = toRunRef(
      scope,
      DEFAULT_WORKSPACE,
      "mqtt-discovery",
      run({
      job_type: "mqtt_discovery",
      run_id: "run-mqtt",
      status: "running",
      }),
    );
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

  it("keeps terminal evidence syncing until the observation cutoff is acknowledged", () => {
    const scope = createSessionScopeId();
    const active = toRunRef(
      scope,
      DEFAULT_WORKSPACE,
      "ip-scanner",
      run({
        job_type: "ip_discovery",
        run_id: "run-ip",
        status: "running",
      }),
    );
    let state = runControllerReducer(initialRunControllerState, {
      type: "accepted",
      runRef: active,
    });
    state = runControllerReducer(state, {
      type: "terminal-observed",
      runId: "run-ip",
      terminalCursor: 5,
    });
    state = runControllerReducer(state, {
      type: "terminal-observed",
      runId: "run-ip",
    });
    state = runControllerReducer(state, {
      type: "evidence-succeeded",
      runId: "run-ip",
      requirements: ["run", "results"],
    });

    expect(state.phase).toBe("terminal-sync");
    state = runControllerReducer(state, {
      type: "observation-cursor-acknowledged",
      runId: "run-ip",
      cursor: 4,
    });
    expect(state.phase).toBe("terminal-sync");
    expect(
      runControllerReducer(state, {
        type: "observation-cursor-acknowledged",
        runId: "run-ip",
        cursor: 4,
      }),
    ).toBe(state);
    state = runControllerReducer(state, {
      type: "observation-cursor-acknowledged",
      runId: "run-ip",
      cursor: 5,
    });
    expect(state.phase).toBe("settled");
  });

  it("reopens a legacy settled controller when a later terminal cutoff arrives", () => {
    const scope = createSessionScopeId();
    const active = toRunRef(
      scope,
      DEFAULT_WORKSPACE,
      "ip-scanner",
      run({
        job_type: "ip_discovery",
        run_id: "run-ip-late-cutoff",
        status: "running",
      }),
    );
    let state = runControllerReducer(initialRunControllerState, {
      type: "accepted",
      runRef: active,
    });
    state = runControllerReducer(state, {
      type: "terminal-observed",
      runId: active.runId,
    });
    state = runControllerReducer(state, {
      type: "evidence-succeeded",
      runId: active.runId,
      requirements: ["run", "results"],
    });
    expect(state.phase).toBe("settled");

    state = runControllerReducer(state, {
      type: "terminal-observed",
      runId: active.runId,
      terminalCursor: 6,
    });
    expect(state).toMatchObject({
      phase: "terminal-sync",
      terminalObservationCursor: 6,
    });
  });

  it("freezes report run, format, filters, and asset scope when the dialog opens", () => {
    const scope: UdmiReportScopeV1 = {
      schema_version: "1.0",
      selected_payloads: [{ source_run_id: "run-1", asset_id: "asset-a", payload_type: "state" }],
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
    const before = [{ Asset: "asset-a", Payload: "UDMI state", __payloadType: "state" }, selected];
    const after = [selected, before[0]];
    const id = resultIdentity("udmi-validation", before[1]);

    expect(after.find((row) => resultIdentity("udmi-validation", row) === id)).toBe(selected);
  });

  it("uses content-based identities for assets, payloads, and topics", () => {
    expect(assetIdentity("ip-scanner", { Asset: "ahu-1", Address: "192.0.2.8" })).toBe(
      assetIdentity("ip-scanner", { Asset: "ahu-1", Address: "192.0.2.8" }),
    );
    expect(payloadIdentity({ Asset: "ahu-1", __payloadType: "state" })).not.toBe(
      payloadIdentity({ Asset: "ahu-1", __payloadType: "pointset" }),
    );
    expect(topicIdentity({ Topic: "/devices/ahu-1/state" })).not.toBe(
      topicIdentity({ Topic: "/devices/ahu-2/state" }),
    );
    expect(assetIdentity("ip-scanner", { "Observed IP": "192.0.2.8" })).not.toBe(
      assetIdentity("ip-scanner", { "Observed IP": "192.0.2.9" }),
    );
    expect(
      assetIdentity("bacnet-discovery", { Address: "192.0.2.8:47808", Instance: "1001" }),
    ).not.toBe(assetIdentity("bacnet-discovery", { Address: "192.0.2.8:47808", Instance: "1002" }));
  });

  it("keeps progressive discovery selection on the server entity key as display fields change", () => {
    const before = resultIdentity("ip-scanner", {
      Asset: "Pending host",
      "Observed IP": "192.0.2.8",
      __entityKey: "host:192.0.2.8",
    });
    const after = resultIdentity("ip-scanner", {
      Asset: "AHU-8",
      "Observed IP": "192.0.2.8",
      __entityKey: "host:192.0.2.8",
    });

    expect(after).toBe(before);
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
