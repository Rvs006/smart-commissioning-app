import type { NmapProfileName, NmapProjectSiteScope } from "../../api/client";
import type { NmapPolicyText } from "./nmapPolicyDraft";

const PROFILE_OPTIONS: Array<{ label: string; value: NmapProfileName }> = [
  { label: "Host discovery", value: "host_discovery" },
  { label: "OS inventory", value: "os_inventory" },
  { label: "Reviewed script inventory", value: "reviewed_script_inventory" },
  { label: "Selected UDP", value: "selected_udp" },
  { label: "Service version inventory", value: "service_version_inventory" },
  { label: "TCP connect inventory", value: "tcp_connect_inventory" },
  { label: "TCP SYN inventory", value: "tcp_syn_inventory" },
  { label: "Traceroute inventory", value: "traceroute_inventory" },
];

export function NmapTextField({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <label>
      {label}
      <input maxLength={4096} onChange={(event) => onChange(event.target.value)} value={value} />
    </label>
  );
}

export function NmapScopeEditor({
  onChange,
  scopes,
}: {
  onChange: (scopes: NmapProjectSiteScope[]) => void;
  scopes: NmapProjectSiteScope[];
}) {
  const updateScope = (index: number, field: keyof NmapProjectSiteScope, value: string) => {
    onChange(
      scopes.map((scope, scopeIndex) =>
        scopeIndex === index ? { ...scope, [field]: value } : scope,
      ),
    );
  };

  return (
    <section aria-labelledby="nmap-scope-heading">
      <div className="section-heading">
        <h3 id="nmap-scope-heading">Permitted project sites</h3>
      </div>
      <div className="detail-list">
        {scopes.map((scope, index) => (
          <div className="field-grid" key={index}>
            <label>
              Permitted project {index + 1}
              <input
                onChange={(event) => updateScope(index, "project_id", event.target.value)}
                value={scope.project_id}
              />
            </label>
            <label>
              Permitted site {index + 1}
              <input
                onChange={(event) => updateScope(index, "site_id", event.target.value)}
                value={scope.site_id}
              />
            </label>
            {scopes.length > 1 && (
              <button
                className="link-button"
                onClick={() => onChange(scopes.filter((_, item) => item !== index))}
                type="button"
              >
                Remove site {index + 1}
              </button>
            )}
          </div>
        ))}
      </div>
      <button
        className="secondary-button compact"
        onClick={() => onChange(scopes.concat({ project_id: "", site_id: "" }))}
        type="button"
      >
        Add permitted site
      </button>
    </section>
  );
}

export function NmapProcessTrustFields({
  onField,
  onProfile,
  profiles,
  text,
}: {
  onField: (field: keyof NmapPolicyText, value: string) => void;
  onProfile: (profile: NmapProfileName) => void;
  profiles: NmapProfileName[];
  text: NmapPolicyText;
}) {
  return (
    <section aria-labelledby="nmap-trust-heading" id="nmap-process-policy-fields">
      <div className="section-heading">
        <div>
          <h3 id="nmap-trust-heading">Exact trust policy</h3>
          <p className="section-copy">
            Enter one permitted value per line. Local paths are never accepted or displayed.
          </p>
        </div>
      </div>
      <div className="field-grid">
        <ListField
          label="Permitted publisher"
          onChange={(value) => onField("publishers", value)}
          value={text.publishers}
        />
        <ListField
          label="Permitted Nmap version"
          onChange={(value) => onField("versions", value)}
          value={text.versions}
        />
        <ListField
          label="Signer SHA-256"
          onChange={(value) => onField("signerHashes", value)}
          value={text.signerHashes}
        />
        <ListField
          label="Executable SHA-256"
          onChange={(value) => onField("executableHashes", value)}
          value={text.executableHashes}
        />
        <ListField
          label="Data manifest SHA-256"
          onChange={(value) => onField("dataManifestHashes", value)}
          value={text.dataManifestHashes}
        />
        <ListField
          label="Licence SHA-256"
          onChange={(value) => onField("licenceHashes", value)}
          value={text.licenceHashes}
        />
        <ListField
          label="NPSL version"
          onChange={(value) => onField("npslVersions", value)}
          value={text.npslVersions}
        />
        <ListField
          label="Reviewed scripts"
          note="Optional. Use name, SHA-256 on each line."
          onChange={(value) => onField("reviewedScripts", value)}
          value={text.reviewedScripts}
        />
      </div>
      <div aria-labelledby="nmap-profile-heading" className="stack" role="group">
        <h3 id="nmap-profile-heading">Permitted fixed profiles</h3>
        <div className="field-grid">
          {PROFILE_OPTIONS.map((profile) => (
            <label className="confirm-row" key={profile.value}>
              <input
                checked={profiles.includes(profile.value)}
                onChange={() => onProfile(profile.value)}
                type="checkbox"
              />
              {profile.label}
            </label>
          ))}
        </div>
      </div>
    </section>
  );
}

function ListField({
  label,
  note,
  onChange,
  value,
}: {
  label: string;
  note?: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <label>
      {label}
      <textarea onChange={(event) => onChange(event.target.value)} rows={2} value={value} />
      {note && <span className="field-note">{note}</span>}
    </label>
  );
}
