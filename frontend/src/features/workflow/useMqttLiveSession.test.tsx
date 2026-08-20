import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api/client", () => ({
  connectMqttLiveSession: vi.fn(),
  disconnectMqttLiveSession: vi.fn(),
  focusMqttLiveAsset: vi.fn(),
  getMqttLiveStatus: vi.fn(),
  searchMqttLive: vi.fn(),
  streamMqttLiveEvents: vi.fn(() => () => {}),
  subscribeMqttLive: vi.fn(),
}));

import {
  connectMqttLiveSession,
  disconnectMqttLiveSession,
  focusMqttLiveAsset,
  getMqttLiveStatus,
  searchMqttLive,
  streamMqttLiveEvents,
  subscribeMqttLive,
  type MqttLiveCallbacks,
  type MqttLiveConnection,
  type MqttLiveSessionInfo,
  type MqttLiveStatusResponse,
} from "../../api/client";
import { useMqttLiveSession } from "./useMqttLiveSession";

const workspace = { projectId: "p", siteId: "s" };

function sessionInfo(owner: string): MqttLiveSessionInfo {
  return { session_id: "s1", owner, project_id: "p", site_id: "s", since: "2026-08-20T00:00:00Z" };
}

function connection(): MqttLiveConnection {
  return { status: "connected", host: "broker.example.local", port: 8883, tls: true, rootFilter: "#", qos: 0, error: "", since: 0 };
}

function statusResponse(session: MqttLiveSessionInfo | null): MqttLiveStatusResponse {
  return { session, sidecar_available: true, connection: null, stats: null, register: null };
}

describe("useMqttLiveSession", () => {
  beforeEach(() => {
    vi.mocked(getMqttLiveStatus).mockResolvedValue(statusResponse(null));
    vi.mocked(connectMqttLiveSession).mockReset();
    vi.mocked(disconnectMqttLiveSession).mockReset();
    vi.mocked(focusMqttLiveAsset).mockReset().mockResolvedValue({ ok: true, focused: null });
    vi.mocked(searchMqttLive).mockReset().mockResolvedValue({ ok: true });
    vi.mocked(subscribeMqttLive).mockReset().mockResolvedValue({ ok: true });
    vi.mocked(streamMqttLiveEvents).mockReset().mockReturnValue(() => {});
  });

  it("reads occupancy on enable and reports no_session when free", async () => {
    const { result } = renderHook(() => useMqttLiveSession(true, { workspace, authorized: true }));
    await waitFor(() => expect(result.current.phase).toBe("no_session"));
  });

  it("reports occupied when another operator holds the lease", async () => {
    vi.mocked(getMqttLiveStatus).mockResolvedValue(statusResponse(sessionInfo("alice")));
    const { result } = renderHook(() => useMqttLiveSession(true, { workspace, authorized: true }));
    await waitFor(() => expect(result.current.phase).toBe("occupied"));
    expect(result.current.status?.session?.owner).toBe("alice");
  });

  it("start opens a session and goes live on the first snapshot frame", async () => {
    vi.mocked(connectMqttLiveSession).mockResolvedValue({ ok: true, session: sessionInfo("me"), connection: connection() });
    let callbacks: MqttLiveCallbacks | undefined;
    vi.mocked(streamMqttLiveEvents).mockImplementation((_sessionId, cb) => {
      callbacks = cb;
      return () => {};
    });
    const { result } = renderHook(() => useMqttLiveSession(true, { workspace, authorized: true }));
    await waitFor(() => expect(result.current.phase).toBe("no_session"));
    await act(async () => {
      await result.current.start();
    });
    act(() => {
      callbacks?.onFrame({
        type: "snapshot",
        status: connection(),
        stats: { expectedAssets: 0, subscribedAssets: 1, liveAssets: 1, topicsDiscovered: 3, issues: 0, totalMessages: 5 },
        tree: [],
        treeShown: 0,
        totalTopics: 3,
        filtered: false,
        focused: null,
      });
    });
    await waitFor(() => expect(result.current.phase).toBe("live"));
    expect(result.current.snapshot?.stats.topicsDiscovered).toBe(3);
  });

  it("stop disconnects and returns to no_session", async () => {
    vi.mocked(connectMqttLiveSession).mockResolvedValue({ ok: true, session: sessionInfo("me"), connection: connection() });
    vi.mocked(disconnectMqttLiveSession).mockResolvedValue({ ok: true, released: true });
    const { result } = renderHook(() => useMqttLiveSession(true, { workspace, authorized: true }));
    await waitFor(() => expect(result.current.phase).toBe("no_session"));
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      await result.current.stop();
    });
    expect(disconnectMqttLiveSession).toHaveBeenCalled();
    await waitFor(() => expect(result.current.phase).toBe("no_session"));
  });

  it("focus sends the asset for the current session", async () => {
    vi.mocked(connectMqttLiveSession).mockResolvedValue({ ok: true, session: sessionInfo("me"), connection: connection() });
    const { result } = renderHook(() => useMqttLiveSession(true, { workspace, authorized: true }));
    await waitFor(() => expect(result.current.phase).toBe("no_session"));
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      await result.current.focus("AHU-1");
    });
    expect(focusMqttLiveAsset).toHaveBeenCalledWith(expect.objectContaining({ sessionId: "s1", asset: "AHU-1" }));
  });

  it("subscribe and search send for the current session", async () => {
    vi.mocked(connectMqttLiveSession).mockResolvedValue({ ok: true, session: sessionInfo("me"), connection: connection() });
    const { result } = renderHook(() => useMqttLiveSession(true, { workspace, authorized: true }));
    await waitFor(() => expect(result.current.phase).toBe("no_session"));
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      await result.current.subscribe("site/#", 1);
      await result.current.search("supply");
    });
    expect(subscribeMqttLive).toHaveBeenCalledWith(expect.objectContaining({ sessionId: "s1", rootFilter: "site/#", qos: 1 }));
    expect(searchMqttLive).toHaveBeenCalledWith(expect.objectContaining({ sessionId: "s1", q: "supply" }));
  });
});
