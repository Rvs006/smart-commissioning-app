import { SystemInterface } from "../../api/client";

// Human-readable adapter-type captions for the details panel. "virtual" never
// reaches this component (eligible interfaces are pre-filtered), but the map
// stays total over AdapterType so an unexpected value still renders sensibly.
const ADAPTER_TYPE_LABELS: Record<string, string> = {
  ethernet: "Ethernet",
  usb_ethernet: "USB Ethernet",
  virtual: "Virtual",
  wifi: "Wi-Fi",
};

const AUTO_SENTINEL = "auto (os default route)";

type SourceInterfaceDetailsProps = {
  // The stored Source Interface value (cidr, bare IP, Auto sentinel, or "").
  value: string;
  // Eligible (non-virtual) interfaces from GET /system/interfaces, API order.
  interfaces: SystemInterface[];
  enumerationFailed: boolean;
  enumerationPending: boolean;
};

// Read-only details for the selected Source Interface adapter: IPv4, subnet
// mask, default gateway, and DNS — straight from the OS, shown by product
// decision so engineers can confirm the tool reads the NIC correctly. Purely
// presentational (no queries): the parent passes the enumeration state in.
// Windows owns adapter IP settings; this panel never edits anything.
export function SourceInterfaceDetails({
  value,
  interfaces,
  enumerationFailed,
  enumerationPending,
}: SourceInterfaceDetailsProps) {
  const trimmed = value.trim();
  const isAuto = trimmed === "" || trimmed.toLowerCase() === AUTO_SENTINEL;

  const managedNote = (
    <p className="field-note">
      Windows manages these adapter settings. The app never changes them — it only chooses which
      adapter scans send from.
    </p>
  );

  if (isAuto) {
    return (
      <div>
        {managedNote}
        <small className="muted">Auto: Windows picks the sending adapter via its default route.</small>
      </div>
    );
  }

  // Stored values are normally cidrs, but a bare-IP value (hand-entered or from
  // an older snapshot) still matches its adapter by ipv4.
  const selected =
    interfaces.find((iface) => iface.cidr === trimmed) ??
    interfaces.find((iface) => iface.ipv4 === trimmed.split("/")[0]);

  if (!selected) {
    return (
      <div>
        {managedNote}
        {enumerationFailed ? (
          <small className="muted">
            Adapter details unavailable (interface enumeration failed on the backend host).
          </small>
        ) : enumerationPending ? null : (
          // Honest about BOTH not-listed cases: the adapter may be genuinely
          // gone (dispatch then fails with a clear error), or it may still be
          // present but ineligible — e.g. a virtual adapter saved before
          // virtual NICs were excluded, which scans would still send from.
          // This component only sees the eligible list, so it cannot tell the
          // two apart and must not promise either outcome.
          <small>
            This interface is not in the list of eligible adapters on this machine — it may be
            unplugged or disabled, or it may be a virtual adapter, which is not a valid scan
            source. Pick a listed adapter, or set Source Interface back to Auto (OS default
            route).
          </small>
        )}
      </div>
    );
  }

  const typeLabel = ADAPTER_TYPE_LABELS[selected.adapter_type] ?? "Unknown";
  const rows: Array<[string, string]> = [
    ["Adapter", `${selected.name} (${typeLabel})`],
    ["IPv4 address", selected.ipv4],
    ["Subnet mask", selected.subnet_mask],
    ["Default gateway", selected.gateway ?? "—"],
    ["Primary DNS", selected.dns_servers[0] ?? "—"],
    ["Secondary DNS", selected.dns_servers[1] ?? "—"],
  ];

  return (
    <div>
      {managedNote}
      {/* Visible heading + group semantics: the section above holds the
          near-identically labelled EDITABLE planned-device fields (Subnet
          Mask / Gateway / DNS Servers), so these OS-read laptop values need
          an explicit group label for sighted and screen-reader users alike. */}
      <strong className="eyebrow">Selected adapter — this laptop, read-only</strong>
      <div
        aria-label="Selected adapter (this laptop, read-only)"
        className="field-grid"
        role="group"
      >
        {rows.map(([caption, detail]) => (
          <label key={caption}>
            {caption}
            <input readOnly value={detail} />
          </label>
        ))}
      </div>
      {!selected.is_up && (
        <small>This adapter is currently down — scans from it will fail until it is back up.</small>
      )}
    </div>
  );
}
