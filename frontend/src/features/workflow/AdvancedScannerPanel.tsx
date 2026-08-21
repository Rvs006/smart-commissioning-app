import { useEffect, useState } from "react";

// The Advanced tab embeds the vendored standalone scanner app's OWN web UI via
// SCT's reverse proxy (/api/v1/scanners/{proto}/raw/). SCT stays on top: the
// proxy enforces role, blocks device writes, and records evidence; reads flow
// free. This is the raw tool, with its own look - that two-look UX is deliberate.

const PROTO_LABEL: Record<string, string> = { ip: "IP", bacnet: "BACnet", mqtt: "MQTT" };

export function AdvancedScannerPanel({
  proto,
  projectId,
  siteId,
}: {
  proto: string;
  projectId: string;
  siteId: string;
}) {
  // Open a panel session first so the proxy can attribute evidence to this
  // project/site (its cookie rides the iframe's subresource requests). Best
  // effort: render the tool even if it fails - reads still work, they just
  // don't record. Gating the iframe on the attempt avoids a cookie race.
  const [ready, setReady] = useState(false);
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

  const label = PROTO_LABEL[proto] ?? proto.toUpperCase();
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
        SCT: reads run freely, and device writes are handled through SCT&apos;s confirmation.
      </p>
      {/* Trailing slash so the app's relative asset/api URLs resolve under this path. */}
      <iframe className="advanced-panel-frame" src={`/api/v1/scanners/${proto}/raw/`} title={`${label} advanced scanner`} />
    </div>
  );
}
