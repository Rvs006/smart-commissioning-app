import type { NmapDetectedInstallationResponse } from "../../api/client";

export function NmapInstallationIdentity({
  installation,
}: {
  installation: NmapDetectedInstallationResponse;
}) {
  const identity = [
    ["State", installation.state],
    ["Publisher", installation.publisher],
    ["Version", installation.version],
    ["Fingerprint SHA-256", installation.fingerprint_sha256],
    ["Signer SHA-256", installation.signer_sha256],
    ["Executable SHA-256", installation.executable_sha256],
    ["Data manifest SHA-256", installation.data_manifest_sha256],
    ["Licence SHA-256", installation.licence_sha256],
    ["NPSL version", installation.npsl_version],
    ["Npcap", installation.npcap_version],
  ].filter((entry): entry is [string, string] => Boolean(entry[1]));

  return (
    <div
      aria-label={`Detected Nmap identity ${installation.fingerprint_sha256 ?? installation.display_name ?? "unidentified"}`}
      className="detail-list"
      role="group"
    >
      {identity.map(([label, value]) => (
        <div className="detail-row" key={label}>
          <span>{label}</span>
          <strong>
            {label.includes("SHA-256") ? (
              <code style={{ overflowWrap: "anywhere" }}>{value}</code>
            ) : (
              value
            )}
          </strong>
        </div>
      ))}
      <div className="detail-row">
        <span>Npcap capability</span>
        <strong>{installation.npcap_state}</strong>
      </div>
    </div>
  );
}
