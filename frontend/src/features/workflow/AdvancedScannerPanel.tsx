import { useEffect, useRef, useState } from "react";

// The Advanced tab embeds the vendored standalone scanner app's OWN web UI via
// SCT's reverse proxy (/api/v1/scanners/{proto}/raw/). SCT stays on top: the
// proxy enforces role, records evidence, and gates device writes; reads flow
// free. A write inside the iframe pauses (via the injected bridge) and asks here
// for an explicit confirm before SCT mints the hash-bound token that lets it pass.

const PROTO_LABEL: Record<string, string> = { ip: "IP", bacnet: "BACnet", mqtt: "MQTT" };

type PendingWrite = { id: string; method: string; path: string; body: string };

function describeWrite(body: string): string {
  try {
    return JSON.stringify(JSON.parse(body), null, 2);
  } catch {
    return body || "(empty)";
  }
}

export function AdvancedScannerPanel({
  proto,
  projectId,
  siteId,
}: {
  proto: string;
  projectId: string;
  siteId: string;
}) {
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState<PendingWrite | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const label = PROTO_LABEL[proto] ?? proto.toUpperCase();

  // Open a panel session first so proxied actions attribute evidence to this
  // project/site. Best effort: render the tool even if it fails.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/v1/scanners/${proto}/raw/session`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ project_id: projectId, site_id: siteId }),
    })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [proto, projectId, siteId]);

  // The bridge inside the iframe posts a write request; hold it until the
  // operator confirms. Only accept messages from our own iframe.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const data = event.data as { type?: string; id?: string; method?: string; path?: string; body?: string };
      if (data?.type === "sct-write-request" && data.id && data.method && data.path) {
        setError(null);
        setPending({ id: data.id, method: data.method, path: data.path, body: data.body ?? "" });
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  function decide(pendingWrite: PendingWrite, token: string | null) {
    iframeRef.current?.contentWindow?.postMessage(
      { type: "sct-write-decision", id: pendingWrite.id, token },
      "*",
    );
    setPending(null);
  }

  async function confirmWrite(pendingWrite: PendingWrite) {
    try {
      const response = await fetch(`/api/v1/scanners/${proto}/raw/confirm-write`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ method: pendingWrite.method, path: pendingWrite.path, body: pendingWrite.body }),
      });
      if (!response.ok) throw new Error("SCT refused the write confirmation.");
      const { token } = (await response.json()) as { token: string };
      setAcknowledged(true);
      decide(pendingWrite, token);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Write confirmation failed.");
      decide(pendingWrite, null);
    }
  }

  if (!ready) {
    return (
      <div className="advanced-panel">
        <p className="advanced-panel-note">Preparing the {label} scanner...</p>
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
      />
      {error && (
        <p className="error-text" role="alert">
          {error}
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
