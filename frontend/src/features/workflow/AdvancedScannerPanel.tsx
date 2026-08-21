// The Advanced tab embeds the vendored standalone scanner app's OWN web UI via
// SCT's reverse proxy (/api/v1/scanners/{proto}/raw/). SCT stays on top: the
// proxy enforces role and blocks device writes; reads flow free. This is the raw
// tool, with its own look - that two-look UX is deliberate for the Advanced tab.

const PROTO_LABEL: Record<string, string> = { ip: "IP", bacnet: "BACnet", mqtt: "MQTT" };

export function AdvancedScannerPanel({ proto }: { proto: string }) {
  // Trailing slash so the app's relative asset/api URLs resolve under this path.
  const src = `/api/v1/scanners/${proto}/raw/`;
  const label = PROTO_LABEL[proto] ?? proto.toUpperCase();
  return (
    <div className="advanced-panel">
      <p className="advanced-panel-note">
        The full standalone {label} scanner, embedded in SCT. Everything the tool does runs through
        SCT: reads run freely, and device writes are handled through SCT&apos;s confirmation.
      </p>
      <iframe className="advanced-panel-frame" src={src} title={`${label} advanced scanner`} />
    </div>
  );
}
