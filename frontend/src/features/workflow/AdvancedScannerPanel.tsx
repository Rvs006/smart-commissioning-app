import { useEffect, useRef, useState } from "react";

// The Advanced tab embeds the vendored standalone scanner app's OWN web UI via
// SCT's reverse proxy (/api/v1/scanners/{proto}/raw/). SCT stays on top: the
// proxy enforces role, records evidence, and gates device writes; reads flow
// free. A write inside the iframe pauses (via the injected bridge) and asks here
// for an explicit confirm before SCT mints the hash-bound token that lets it pass.

const PROTO_LABEL: Record<string, string> = { ip: "IP", bacnet: "BACnet", mqtt: "MQTT" };

type PendingWrite = { id: string; method: string; path: string; body: string };
type Phase = "loading" | "error" | "ready";

function describeWrite(body: string): string {
  try {
    return JSON.stringify(JSON.parse(body), null, 2);
  } catch {
    return body || "(empty)";
  }
}

// Throws if the sidecar does not answer its health probe. Shared by the preflight
// (before the tool is shown) and the iframe onLoad recheck (a sidecar that dies
// after the panel is ready).
async function assertSidecarHealthy(proto: string, label: string): Promise<void> {
  const health = await fetch(`/api/v1/scanners/${proto}/raw/api/health`, {
    credentials: "same-origin",
  });
  if (!health.ok) {
    throw new Error(`The ${label} scanner service is unavailable (HTTP ${health.status}).`);
  }
}

export function AdvancedScannerPanel({
  proto,
  projectId,
  siteId,
  sourceInterfaceCidr,
  mqttConfig,
}: {
  proto: string;
  projectId: string;
  siteId: string;
  sourceInterfaceCidr?: string;
  mqttConfig?: { host: string; port: string; tls: boolean; clientId: string; username: string; qos: string };
}) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [startError, setStartError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [bridgeReady, setBridgeReady] = useState(false);
  const [pending, setPending] = useState<PendingWrite | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // Bumped by the preflight effect whenever the panel switches scanner/context.
  // Because this component instance is reused across a switch (same iframe DOM
  // node, new proto), an async op started under the old context - a write confirm
  // or a health recheck - could otherwise apply its result to the new scanner.
  // Each op snapshots this at start and bails before its side effects if it
  // changed, so a stale IP result never posts a decision to, or errors out, the
  // BACnet/MQTT tool that replaced it.
  const contextIdRef = useRef(0);
  const label = PROTO_LABEL[proto] ?? proto.toUpperCase();

  // Preflight before showing the tool: open the panel session (so proxied actions
  // attribute evidence to this project/site) AND confirm the sidecar answers. Both
  // must succeed - a failed session (actions would not attribute) or an
  // unreachable sidecar (the iframe would load a 503) surfaces an error with Retry
  // instead of a ready-looking panel that does not work. Re-runs on Retry.
  useEffect(() => {
    // New context: invalidate any in-flight write confirm / health recheck from
    // the previous scanner so their late results are dropped, not applied here.
    contextIdRef.current += 1;
    let cancelled = false;
    setBridgeReady(false);
    setPhase("loading");
    // Context changed (different scanner / project / site) or Retry: drop any write
    // confirmation left over from the previous scanner. Otherwise a stale IP write
    // dialog lingers over - and could be confirmed against - the newly loaded
    // BACnet/MQTT tool, since this component instance is reused across the switch.
    setPending(null);
    setAcknowledged(false);
    setWriteError(null);
    (async () => {
      try {
        const session = await fetch(`/api/v1/scanners/${proto}/raw/session`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ project_id: projectId, site_id: siteId }),
        });
        if (!session.ok) {
          throw new Error(`The panel session could not be opened (HTTP ${session.status}).`);
        }
        await assertSidecarHealthy(proto, label);
        if (!cancelled) {
          setStartError(null);
          setPhase("ready");
        }
      } catch (caught) {
        if (!cancelled) {
          setStartError(
            caught instanceof Error ? caught.message : `The ${label} scanner could not be reached.`,
          );
          setPhase("error");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [proto, projectId, siteId, attempt, label]);

  // The bridge inside the iframe posts a write request; hold it until the
  // operator confirms. Only accept messages from our own iframe.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const data = event.data as { type?: string; id?: string; method?: string; path?: string; body?: string };
      if (data?.type === "sct-bridge-ready") setBridgeReady(true);
      if (data?.type === "sct-write-request" && data.id && data.method && data.path) {
        setWriteError(null);
        setPending({ id: data.id, method: data.method, path: data.path, body: data.body ?? "" });
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // Config-in (pipe 1): once the in-iframe bridge signals it is live, push the
  // saved Source Interface so the vendored tool pre-selects the matching NIC.
  // Re-sends if the cidr resolves after the bridge is ready; a no-op with no cidr.
  useEffect(() => {
    if (!bridgeReady || (!sourceInterfaceCidr && !mqttConfig)) return;
    iframeRef.current?.contentWindow?.postMessage(
      { type: "sct-config-in", sourceInterface: sourceInterfaceCidr, mqtt: mqttConfig },
      "*",
    );
  }, [bridgeReady, sourceInterfaceCidr, mqttConfig]);

  function decide(pendingWrite: PendingWrite, token: string | null) {
    iframeRef.current?.contentWindow?.postMessage(
      { type: "sct-write-decision", id: pendingWrite.id, token },
      "*",
    );
    setPending(null);
  }

  async function confirmWrite(pendingWrite: PendingWrite) {
    const contextId = contextIdRef.current;
    try {
      const response = await fetch(`/api/v1/scanners/${proto}/raw/confirm-write`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ method: pendingWrite.method, path: pendingWrite.path, body: pendingWrite.body }),
      });
      if (!response.ok) throw new Error("SCT refused the write confirmation.");
      const { token } = (await response.json()) as { token: string };
      // Scanner switched while the confirm was in flight: never post this IP
      // decision + token to the iframe now showing a different scanner.
      if (contextIdRef.current !== contextId) return;
      setAcknowledged(true);
      decide(pendingWrite, token);
    } catch (caught) {
      // Same guard on the deny path: a stale "denied" decision must not reach the
      // replacement scanner either.
      if (contextIdRef.current !== contextId) return;
      setWriteError(caught instanceof Error ? caught.message : "Write confirmation failed.");
      decide(pendingWrite, null);
    }
  }

  // A sidecar that dies AFTER preflight makes the iframe (re)load the proxy's 503
  // instead of the tool. An iframe's onError does not fire for an HTTP error
  // document, but onLoad does; re-verify health on each full (re)load and fall back
  // to the same Retry error state if the sidecar is gone.
  async function recheckHealth() {
    const contextId = contextIdRef.current;
    try {
      await assertSidecarHealthy(proto, label);
    } catch (caught) {
      // A late (re)load health failure from the previous scanner must not
      // overwrite the current scanner's ready state with a stale error.
      if (contextIdRef.current !== contextId) return;
      setStartError(
        caught instanceof Error ? caught.message : `The ${label} scanner could not be reached.`,
      );
      setPhase("error");
    }
  }

  if (phase === "loading") {
    return (
      <div className="advanced-panel">
        <p className="advanced-panel-note">Preparing the {label} scanner...</p>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="advanced-panel">
        <div className="state-panel error" role="alert">
          <strong>The {label} scanner could not start</strong>
          <span>{startError}</span>
          <div className="inline-actions">
            <button
              className="secondary-button compact"
              onClick={() => setAttempt((n) => n + 1)}
              type="button"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="advanced-panel">
      <p className="advanced-panel-note">
        The full standalone {label} scanner, embedded in SCT. Everything the tool does runs through
        SCT: reads run freely, and device writes ask for confirmation here.
      </p>
      {/* Trailing slash so the app's relative asset/api URLs resolve under this path. */}
      <iframe
        ref={iframeRef}
        className="advanced-panel-frame"
        src={`/api/v1/scanners/${proto}/raw/`}
        title={`${label} advanced scanner`}
        onLoad={recheckHealth}
      />
      {writeError && (
        <p className="error-text" role="alert">
          {writeError}
        </p>
      )}
      {pending && (
        <div className="advanced-write-confirm" role="dialog" aria-modal="true" aria-label="Confirm device write">
          <div className="advanced-write-confirm-card">
            <h3>Confirm device write</h3>
            {!acknowledged && (
              <p className="error-text">
                This sends a message to live equipment. Check the target and value before you continue.
              </p>
            )}
            <p>
              <strong>{pending.method}</strong> {pending.path}
            </p>
            <pre className="advanced-write-payload">{describeWrite(pending.body)}</pre>
            <div className="inline-actions">
              <button className="secondary-button compact" onClick={() => decide(pending, null)} type="button">
                Cancel
              </button>
              <button className="primary-button compact" onClick={() => confirmWrite(pending)} type="button">
                Send write
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
