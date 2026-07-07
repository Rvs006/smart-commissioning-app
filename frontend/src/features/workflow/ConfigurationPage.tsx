import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ConfigurationExport,
  ConfigurationSectionKey,
  ConfigurationSnapshot,
  exportConfiguration,
  getConfiguration,
  getSystemInterfaces,
  importConfiguration,
  storeSecretMaterial,
  updateConfiguration,
  validateConfiguration,
} from "../../api/client";
import { isSecretSentinel, maskSecretValue } from "./secretField";
import { SourceInterfaceDetails } from "./SourceInterfaceDetails";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";

type FieldKind = "text" | "password" | "select" | "textarea" | "secret" | "readonly";

type FieldDefinition = {
  kind: FieldKind;
  options?: string[];
};

const sectionOrder: ConfigurationSectionKey[] = [
  "device",
  "bacnet",
  "mqtt",
  "certificates",
  "time",
  "backups",
  "logging",
];

// Connection-critical sections stay expanded by default so the settings needed
// before discovery/validation runs are visible without a click. The advanced
// sections collapse to reduce the wall of fields the original review flagged.
const defaultExpandedSections: Record<ConfigurationSectionKey, boolean> = {
  backups: false,
  bacnet: true,
  certificates: true,
  device: true,
  logging: false,
  mqtt: true,
  time: false,
};

const sectionLabels: Record<ConfigurationSectionKey, string> = {
  backups: "Backup & Restore",
  bacnet: "BACnet Discovery",
  certificates: "Certificates & Keys",
  device: "Network Basics",
  logging: "Logging & Diagnostics",
  mqtt: "MQTT Settings",
  time: "Time & NTP",
};

const sectionDescriptions: Record<ConfigurationSectionKey, string> = {
  backups: "Backup schedule, retention, encryption, storage location, and restore readiness.",
  bacnet: "BACnet/IP discovery settings including BBMD, foreign device mode, UDP ports, and TTL.",
  certificates: "TLS trust and client authentication material. Paste content or select local files; only masked server references are saved.",
  device:
    "Planned network identity of the gateway device being commissioned — reference values used by validation, not this laptop's settings. The adapter this laptop scans from is chosen under Source Interface.",
  logging: "Runtime diagnostics, log retention, syslog, and current logging health.",
  mqtt: "Broker, client identity, topic, QoS, keep-alive, and optional Mosquitto-style credentials.",
  time: "Timezone and NTP settings used to timestamp evidence and validate stale data.",
};

// A representative, comprehensive list of IANA timezones spanning every UTC
// offset region (not just Europe). The stored value is the raw IANA name, so it
// stays compatible with the configuration "Timezone" field the backend keeps.
const TIMEZONE_OPTIONS = [
  "UTC",
  "Pacific/Midway",
  "Pacific/Honolulu",
  "America/Anchorage",
  "America/Los_Angeles",
  "America/Denver",
  "America/Phoenix",
  "America/Chicago",
  "America/Mexico_City",
  "America/New_York",
  "America/Toronto",
  "America/Bogota",
  "America/Caracas",
  "America/Halifax",
  "America/Santiago",
  "America/Sao_Paulo",
  "America/Argentina/Buenos_Aires",
  "Atlantic/Azores",
  "Atlantic/Cape_Verde",
  "Europe/London",
  "Europe/Dublin",
  "Europe/Lisbon",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Rome",
  "Europe/Amsterdam",
  "Europe/Brussels",
  "Europe/Zurich",
  "Europe/Stockholm",
  "Europe/Warsaw",
  "Europe/Athens",
  "Europe/Helsinki",
  "Europe/Bucharest",
  "Europe/Kyiv",
  "Europe/Istanbul",
  "Europe/Moscow",
  "Africa/Casablanca",
  "Africa/Lagos",
  "Africa/Cairo",
  "Africa/Johannesburg",
  "Africa/Nairobi",
  "Asia/Jerusalem",
  "Asia/Riyadh",
  "Asia/Tehran",
  "Asia/Dubai",
  "Asia/Baku",
  "Asia/Karachi",
  "Asia/Kolkata",
  "Asia/Kathmandu",
  "Asia/Dhaka",
  "Asia/Yangon",
  "Asia/Bangkok",
  "Asia/Jakarta",
  "Asia/Singapore",
  "Asia/Hong_Kong",
  "Asia/Shanghai",
  "Asia/Taipei",
  "Asia/Seoul",
  "Asia/Tokyo",
  "Australia/Perth",
  "Australia/Adelaide",
  "Australia/Brisbane",
  "Australia/Sydney",
  "Pacific/Guam",
  "Pacific/Auckland",
  "Pacific/Fiji",
  "Pacific/Tongatapu",
];

const fieldDefinitions: Partial<Record<ConfigurationSectionKey, Record<string, FieldDefinition>>> = {
  bacnet: {
    BBMD: { kind: "select", options: ["Enabled", "Disabled"] },
    "Foreign Device": { kind: "select", options: ["Enabled", "Disabled"] },
  },
  backups: {
    "Encrypted Backups": { kind: "select", options: ["Enabled", "Disabled"] },
    "Last Backup Status": { kind: "readonly" },
    "Restore Action": { kind: "readonly" },
  },
  certificates: {
    "CA Certificate": { kind: "secret" },
    "Client Certificate": { kind: "secret" },
    "Private Key": { kind: "secret" },
    "Key Password": { kind: "password" },
    "Certificate Expiry": { kind: "readonly" },
  },
  device: {
    "IP Assignment": { kind: "select", options: ["Static IP", "DHCP"] },
  },
  logging: {
    "Diagnostics Mode": { kind: "select", options: ["Enabled", "Disabled"] },
  },
  mqtt: {
    "MQTT Password": { kind: "password" },
    QoS: { kind: "select", options: ["0 - At most once", "1 - At least once", "2 - Exactly once"] },
  },
  time: {
    Timezone: { kind: "select", options: TIMEZONE_OPTIONS },
  },
};

const secretFields = new Set(["CA Certificate", "Client Certificate", "Private Key"]);

// The device-section field whose options come from the live NIC enumeration
// (GET /system/interfaces) rather than the static fieldDefinitions map. The
// first option is the OS-default-route sentinel the backend treats as "bind
// nothing"; the rest are the enumerated interface CIDRs.
const SOURCE_INTERFACE_FIELD = "Source Interface";
const SOURCE_INTERFACE_AUTO = "Auto (OS default route)";

// The Auto sentinel is case-insensitive (with trimming) everywhere else in the
// stack — backend validation and dispatch both casefold it, and the sibling
// SourceInterfaceDetails lowercases it — so this page must treat a stored
// case-variant (e.g. from an imported configuration JSON) as Auto too.
function isAutoSourceInterface(value: string): boolean {
  const trimmed = value.trim();
  return trimmed === "" || trimmed.toLowerCase() === SOURCE_INTERFACE_AUTO.toLowerCase();
}

// Human-readable adapter-type tag appended to a Source Interface option label.
// Wi-Fi carries an explicit warning (commissioning traffic belongs on the
// wired adapter); "unknown" gets no tag rather than a guessed one.
const ADAPTER_TYPE_SUFFIX: Record<string, string> = {
  ethernet: "Ethernet",
  unknown: "",
  usb_ethernet: "USB Ethernet",
  wifi: "Wi-Fi — not recommended for commissioning traffic",
};

// Advisory hint shown while Source Interface is on Auto and more than one
// eligible adapter is up. A saved Auto is respected — the wired-first default
// below only fills a never-chosen (absent/empty) value — so this hint remains
// the nudge for engineers who explicitly stay on Auto.
const AUTO_MULTI_ADAPTER_HINT =
  "Multiple active adapters detected. Auto follows the Windows default route — often the internet adapter, not the site network. Pick your wired adapter to make sure scans use it.";

// The single certificate-expiry indicator field. It is engine/backend-derived
// (store_secret currently returns expiry:null, so real PEM parsing is on-site),
// surfaced here as a read-only status: we compare the displayed date to today
// and flag it red when expired rather than asking the operator to type it.
const CERT_EXPIRY_FIELD = "Certificate Expiry";

// Classifies a free-text section status into a colour band. Engines set values
// like "Healthy"/"Connected"/"Valid" (green), "Degraded"/"Stale"/"Warning"
// (amber), or "Error"/"Failed"/"Unreachable" (red). Unknown strings stay
// neutral-green so a long fault string still renders in a single pill.
function statusTone(status: string): "green" | "amber" | "red" {
  const normalized = status.trim().toLowerCase();
  if (/(fail|error|unreachable|invalid|expired|disconnected|critical|down)/.test(normalized)) {
    return "red";
  }
  if (/(warn|degrad|stale|pending|partial|retry|unknown|attention)/.test(normalized)) {
    return "amber";
  }
  return "green";
}

// Parses an expiry value (e.g. "2027-05-20") into a Date, or null when it is
// blank/unparseable. Kept lenient: the field is a status indicator, so an
// unparseable value is simply not flagged (rather than wrongly marked expired).
function parseExpiryDate(value: string): Date | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const parsed = new Date(trimmed);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// True when the parsed expiry date is strictly before the current day.
function isExpired(value: string): boolean {
  const expiry = parseExpiryDate(value);
  if (!expiry) {
    return false;
  }
  return expiry.getTime() < Date.now();
}

export function ConfigurationPage() {
  // Publishing configuration (PUT /configuration) and storing secrets are
  // engineer+ mutations. Viewing the snapshot and validating it stay viewer, so
  // only the Save, Import, and secret-store controls are role-gated here.
  const { canEngineer } = useSession();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ConfigurationSnapshot | null>(null);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({});
  const [secretFiles, setSecretFiles] = useState<Record<string, string | null>>({});
  const [secretMessage, setSecretMessage] = useState<string | null>(null);
  const [expandedSections, setExpandedSections] = useState<Record<ConfigurationSectionKey, boolean>>(
    defaultExpandedSections,
  );
  const [transferMessage, setTransferMessage] = useState<string | null>(null);
  const [transferError, setTransferError] = useState<string | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);

  const configurationQuery = useQuery({
    queryFn: getConfiguration,
    queryKey: ["configuration"],
  });

  // Live NIC enumeration for the Source Interface selector. Kept independent of
  // the configuration query so a failed/slow enumeration never blocks the page:
  // on loading or error the options fall back to just the Auto sentinel, and the
  // FieldControl !options.includes(value) escape hatch still surfaces a stored
  // non-enumerated value (e.g. an interface that is down or on another host).
  const systemInterfacesQuery = useQuery({
    queryFn: getSystemInterfaces,
    queryKey: ["system-interfaces"],
  });
  // Virtual adapters (Hyper-V vEthernet, VPN/TAP, docker bridges, ...) are
  // never offered as scan sources. The backend already filters them out; this
  // is a defensive re-filter that also tolerates an older backend where
  // adapter_type is undefined. API order is preserved verbatim (the server
  // sorts up-first, then ethernet -> usb_ethernet -> unknown -> wifi).
  const eligibleInterfaces = (systemInterfacesQuery.data ?? []).filter(
    (iface) => iface.adapter_type !== "virtual",
  );
  // Wired-first default (2026-07 product decision, reversing the earlier
  // "keep Auto, hint only" call): the first up wired adapter in API order.
  // Applied by the effect below only when Source Interface was never chosen.
  const defaultWiredCidr = eligibleInterfaces.find(
    (iface) =>
      iface.is_up && (iface.adapter_type === "ethernet" || iface.adapter_type === "usb_ethernet"),
  )?.cidr;
  const sourceInterfaceOptions = [
    SOURCE_INTERFACE_AUTO,
    ...eligibleInterfaces.map((iface) => iface.cidr),
  ];
  // Option labels carry the adapter name and type tag; option VALUES stay the
  // bare cidr so the stored config value remains parseable by the backend
  // source-interface resolver.
  const sourceInterfaceOptionLabels: Record<string, string> = {};
  for (const iface of eligibleInterfaces) {
    const suffix = ADAPTER_TYPE_SUFFIX[iface.adapter_type] ?? "";
    sourceInterfaceOptionLabels[iface.cidr] =
      `${iface.cidr} — ${iface.name}${suffix ? ` (${suffix})` : ""}`;
  }
  // Distinct up adapters by NAME, not per-IPv4 entries: the backend emits one
  // SystemInterface per AF_INET address, so a single NIC carrying a secondary
  // static IPv4 (a common field pattern) must not trigger the Auto hint.
  const upAdapterCount = new Set(
    eligibleInterfaces.filter((iface) => iface.is_up).map((iface) => iface.name),
  ).size;

  useEffect(() => {
    if (configurationQuery.data) {
      setDraft(normalizeConfigurationForLocks(configurationQuery.data));
      setValidationErrors([]);
    }
  }, [configurationQuery.data]);

  // Default a never-chosen Source Interface to the first up wired adapter so
  // scans on multi-homed laptops don't silently egress via Wi-Fi. "Never
  // chosen" means the SAVED value is absent or empty — the backend seeds and
  // backfills the empty string and the Auto sentinel is stored only when it
  // was picked in the dropdown, so a stored Auto is explicit and never
  // overridden. The default is ordinary select state on the draft — visible
  // in the dropdown and saved like a manual pick. With no wired adapter up,
  // the field stays on Auto exactly as before.
  useEffect(() => {
    if (!configurationQuery.data || !defaultWiredCidr) {
      return;
    }
    if ((configurationQuery.data.device.values[SOURCE_INTERFACE_FIELD] ?? "").trim() !== "") {
      return;
    }
    setDraft((current) => {
      // Re-check the live draft so an edit made before the NIC enumeration
      // resolved (e.g. explicitly picking Auto) is never clobbered.
      if (!current || (current.device.values[SOURCE_INTERFACE_FIELD] ?? "").trim() !== "") {
        return current;
      }
      return {
        ...current,
        device: {
          ...current.device,
          values: { ...current.device.values, [SOURCE_INTERFACE_FIELD]: defaultWiredCidr },
        },
      };
    });
  }, [configurationQuery.data, defaultWiredCidr]);

  const validationMutation = useMutation({
    mutationFn: validateConfiguration,
    onSuccess: (result) => {
      setValidationErrors(result.errors);
    },
  });

  const saveMutation = useMutation({
    mutationFn: updateConfiguration,
    onSuccess: (savedConfiguration) => {
      queryClient.setQueryData(["configuration"], savedConfiguration);
      setDraft(savedConfiguration);
      setValidationErrors([]);
    },
  });

  const secretMutation = useMutation({
    mutationFn: (input: { field: string; content: string; fileName?: string | null }) =>
      storeSecretMaterial(input),
    onSuccess: (response) => {
      setSecretMessage(
        `${response.field} stored as masked reference ${response.secret_ref} (fingerprint ${response.fingerprint}).`,
      );
      setSecretDrafts((current) => ({ ...current, [response.field]: "" }));
      setSecretFiles((current) => ({ ...current, [response.field]: response.file_name }));
      queryClient.invalidateQueries({ queryKey: ["configuration"] });
    },
  });

  // Export reads the current snapshot (via exportConfiguration -> GET
  // /configuration) and triggers a JSON file download. The API already returns
  // password fields masked (sentinel) and certificate material as secret://
  // references, so the exported envelope never carries raw secret values.
  const exportMutation = useMutation({
    mutationFn: () => exportConfiguration(),
    onSuccess: (envelope) => {
      downloadConfigurationEnvelope(envelope);
      setTransferError(null);
      setTransferMessage(
        "Exported the current configuration as JSON. Secret material is exported as masked references only, never raw values.",
      );
    },
    onError: (error: Error) => {
      setTransferMessage(null);
      setTransferError(error.message);
    },
  });

  // Import validates the parsed file client-side, then saves it via
  // importConfiguration -> PUT /configuration, which validates again
  // server-side before persisting (surfacing an ApiError on a 400).
  const importMutation = useMutation({
    mutationFn: (payload: ConfigurationExport | ConfigurationSnapshot) => importConfiguration(payload),
    onSuccess: (savedConfiguration) => {
      queryClient.setQueryData(["configuration"], savedConfiguration);
      setDraft(normalizeConfigurationForLocks(savedConfiguration));
      setValidationErrors([]);
      setTransferError(null);
      setTransferMessage("Imported configuration was validated by the API and saved as the new snapshot.");
    },
    onError: (error: Error) => {
      setTransferMessage(null);
      setTransferError(error.message);
    },
  });

  const changeValue = (section: ConfigurationSectionKey, field: string, value: string) => {
    setDraft((current) => {
      if (!current) {
        return current;
      }
      if (section === "bacnet" && field === "Foreign Device" && isBbmdEnabled(current)) {
        return current;
      }

      const nextValues = {
        ...current[section].values,
        [field]: value,
      };

      if (section === "bacnet" && field === "BBMD" && value === "Enabled") {
        nextValues["Foreign Device"] = "Disabled";
      }

      return {
        ...current,
        [section]: {
          ...current[section],
          values: nextValues,
        },
      };
    });
  };

  const toggleSection = (section: ConfigurationSectionKey) => {
    setExpandedSections((current) => ({ ...current, [section]: !current[section] }));
  };

  const handleValidate = () => {
    if (draft) {
      validationMutation.mutate(draft);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (draft) {
      saveMutation.mutate(draft);
    }
  };

  const handleSecretFile = (field: string, file: File | null) => {
    if (!file) {
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setSecretDrafts((current) => ({ ...current, [field]: String(reader.result ?? "") }));
      setSecretFiles((current) => ({ ...current, [field]: file.name }));
    };
    reader.readAsText(file);
  };

  const handleSecretStore = (field: string) => {
    const content = secretDrafts[field]?.trim();
    if (!content) {
      setSecretMessage(`${field} content is empty.`);
      return;
    }
    secretMutation.mutate({ content, field, fileName: secretFiles[field] });
  };

  const handleImportFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    // Reset the input so re-selecting the same file fires onChange again.
    event.target.value = "";
    if (!file) {
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const parsed = parseConfigurationFile(String(reader.result ?? ""));
      if (!parsed.ok) {
        setTransferMessage(null);
        setTransferError(parsed.error);
        return;
      }
      importMutation.mutate(parsed.payload);
    };
    reader.onerror = () => {
      setTransferMessage(null);
      setTransferError("Could not read the selected configuration file.");
    };
    reader.readAsText(file);
  };

  if (configurationQuery.isError) {
    return (
      <div className="state-panel error">
        <strong>Configuration API unavailable</strong>
        <span>{configurationQuery.error.message}</span>
      </div>
    );
  }

  if (configurationQuery.isLoading || !draft) {
    return (
      <div className="state-panel">
        <strong>Loading configuration</strong>
        <span>Reading the persisted API-backed configuration snapshot.</span>
      </div>
    );
  }

  const sectionCount = sectionOrder.length;
  const configuredFieldCount = sectionOrder.reduce(
    (total, section) => total + Object.keys(draft[section].values).length,
    0,
  );

  return (
    <form className="stack" onSubmit={handleSubmit}>
      <section className="hero">
        <div className="hero-banner">
          <h2>Configuration</h2>
          <p>
            Review only the connection settings needed before discovery and validation runs start.
            Advanced sections remain available below when a commissioning engineer needs them.
          </p>
          <div className="chip-row">
            <span className="chip green">Loaded from API</span>
            <span className="chip">{sectionCount} sections</span>
            <span className="chip amber">{configuredFieldCount} editable fields</span>
          </div>
        </div>
        <aside className="hero-side action-panel">
          <h2>Actions</h2>
          <p className="muted">Validate first when changing ports, addresses, topics, or certificates.</p>
          <button
            className="secondary-button"
            disabled={validationMutation.isPending}
            onClick={handleValidate}
            type="button"
          >
            {validationMutation.isPending ? "Validating..." : "Validate Snapshot"}
          </button>
          <p className="action-note">
            Validate Snapshot checks ports, IP/gateway addresses, MQTT topics, and certificate
            references for validity. It runs server-side checks only and does not save the snapshot.
          </p>
          <button
            className="primary-button"
            disabled={saveMutation.isPending || !canEngineer}
            title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
            type="submit"
          >
            {saveMutation.isPending ? "Saving..." : "Save Configuration"}
          </button>
          <p className="action-note">
            Save Configuration persists the edited snapshot as the new runtime configuration used by
            discovery and validation services.
          </p>
          <div className="config-toolbar">
            <button
              className="secondary-button compact"
              disabled={exportMutation.isPending}
              onClick={() => exportMutation.mutate()}
              type="button"
            >
              {exportMutation.isPending ? "Exporting..." : "Export JSON"}
            </button>
            <button
              className="secondary-button compact"
              disabled={importMutation.isPending || !canEngineer}
              onClick={() => importInputRef.current?.click()}
              title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
              type="button"
            >
              {importMutation.isPending ? "Importing..." : "Import JSON"}
            </button>
            <input
              accept="application/json,.json"
              aria-label="Import configuration JSON file"
              hidden
              onChange={handleImportFile}
              ref={importInputRef}
              type="file"
            />
          </div>
          <p className="action-note">
            Export downloads the current configuration as JSON (secrets stay masked) so it can be
            reused on another project; Import validates a JSON file and saves it as the new snapshot.
          </p>
        </aside>
      </section>

      {validationMutation.isError && (
        <div className="state-panel error">
          <strong>Validation request failed</strong>
          <span>{validationMutation.error.message}</span>
        </div>
      )}

      {saveMutation.isError && (
        <div className="state-panel error">
          <strong>Configuration was not saved</strong>
          <span>{saveMutation.error.message}</span>
        </div>
      )}

      {saveMutation.isSuccess && (
        <div className="state-panel success">
          <strong>Configuration saved</strong>
          <span>The persisted runtime snapshot has been updated.</span>
        </div>
      )}

      {transferMessage && (
        <div className="state-panel success">
          <strong>Configuration transfer</strong>
          <span>{transferMessage}</span>
        </div>
      )}

      {transferError && (
        <div className="state-panel error">
          <strong>Configuration import/export failed</strong>
          <span>{transferError}</span>
        </div>
      )}

      {secretMessage && (
        <div className="state-panel success">
          <strong>Secret material updated</strong>
          <span>{secretMessage}</span>
        </div>
      )}

      {secretMutation.isError && (
        <div className="state-panel error">
          <strong>Secret material was not saved</strong>
          <span>{secretMutation.error.message}</span>
        </div>
      )}

      {validationMutation.isSuccess && validationErrors.length === 0 && (
        <div className="state-panel success">
          <strong>Configuration is valid</strong>
          <span>The API accepted this snapshot with no validation errors.</span>
        </div>
      )}

      {validationErrors.length > 0 && (
        <div className="state-panel warning">
          <strong>{validationErrors.length} validation issue(s)</strong>
          <ul>
            {validationErrors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      )}

      <section className="config-grid">
        {sectionOrder.map((section) => {
          const expanded = expandedSections[section];
          const status = draft[section].status;
          const panelId = `config-section-${section}`;
          return (
            <article className="config-section" key={section}>
              <button
                aria-controls={panelId}
                aria-expanded={expanded}
                className="section-toggle"
                onClick={() => toggleSection(section)}
                type="button"
              >
                <span className="section-toggle-title">
                  <span className="section-toggle-caret" aria-hidden="true">
                    ▾
                  </span>
                  <span>{sectionLabels[section]}</span>
                </span>
                <span className={`section-status-pill ${statusTone(status)}`}>{status}</span>
              </button>
              {expanded && (
                <div id={panelId}>
                  <p className="section-copy">{sectionDescriptions[section]}</p>
                  {section === "backups" && (
                    <p className="field-note">
                      Backups bundle the runtime database, encrypted secrets, and uploaded import
                      files. The bundle is built from the app runtime (under the backend runtime
                      directory by default) and written to a path chosen at backup time via the
                      backup CLI&apos;s output option. The Backup Location field below records the
                      intended target for operators; the app does not pick a host directory itself.
                    </p>
                  )}
                  <div className="field-grid">
                    {Object.entries(draft[section].values).map(([field, value]) => {
                      // The device Source Interface field is a select whose options
                      // come from the live NIC enumeration query rather than the
                      // static fieldDefinitions map (proposal 3.4).
                      const isSourceInterface = section === "device" && field === SOURCE_INTERFACE_FIELD;
                      // On Auto (or unset) with several adapters up, nudge the
                      // engineer to pick the wired adapter explicitly — Auto
                      // itself is never silently rebound.
                      const showAutoHint =
                        isSourceInterface && isAutoSourceInterface(value) && upAdapterCount > 1;
                      // A stored case-variant of the Auto sentinel selects the
                      // canonical Auto option instead of rendering as a stray
                      // escape-hatch option (display only; the draft value is
                      // untouched until the engineer changes the select).
                      const displayValue =
                        isSourceInterface && value.trim() !== "" && isAutoSourceInterface(value)
                          ? SOURCE_INTERFACE_AUTO
                          : value;
                      return (
                      <FieldControl
                        canEngineer={canEngineer}
                        disabled={section === "bacnet" && field === "Foreign Device" && isBbmdEnabled(draft)}
                        expired={field === CERT_EXPIRY_FIELD && isExpired(value)}
                        field={field}
                        hint={showAutoHint ? AUTO_MULTI_ADAPTER_HINT : fieldHint(section, field, draft)}
                        kind={isSourceInterface ? "select" : (fieldDefinitions[section]?.[field]?.kind ?? "text")}
                        key={field}
                        onFileSelect={(file) => handleSecretFile(field, file)}
                        onSecretChange={(content) => setSecretDrafts((current) => ({ ...current, [field]: content }))}
                        onSecretStore={() => handleSecretStore(field)}
                        onValueChange={(nextValue) => changeValue(section, field, nextValue)}
                        optionLabels={isSourceInterface ? sourceInterfaceOptionLabels : undefined}
                        options={isSourceInterface ? sourceInterfaceOptions : fieldDefinitions[section]?.[field]?.options}
                        secretContent={secretDrafts[field] ?? ""}
                        secretFileName={secretFiles[field] ?? null}
                        secretPending={secretMutation.isPending}
                        value={displayValue}
                      />
                      );
                    })}
                  </div>
                  {section === "device" && (
                    <SourceInterfaceDetails
                      enumerationFailed={systemInterfacesQuery.isError}
                      enumerationPending={systemInterfacesQuery.isLoading}
                      interfaces={eligibleInterfaces}
                      value={draft.device.values[SOURCE_INTERFACE_FIELD] ?? ""}
                    />
                  )}
                </div>
              )}
            </article>
          );
        })}
      </section>
    </form>
  );
}

// Per-field helper text. Beyond the BBMD lock hint, the certificate-expiry
// field gets an honest status note explaining it is a derived indicator (red
// when the stored expiry date is in the past) rather than a value to type.
function fieldHint(
  section: ConfigurationSectionKey,
  field: string,
  draft: ConfigurationSnapshot,
): string | undefined {
  if (section === "bacnet" && field === "Foreign Device" && isBbmdEnabled(draft)) {
    return "Locked because BBMD is enabled.";
  }
  if (section === "certificates" && field === CERT_EXPIRY_FIELD) {
    const value = draft.certificates.values[field] ?? "";
    if (isExpired(value)) {
      return "Certificate expired: the stored expiry date is in the past.";
    }
    return "Status indicator derived from the stored certificate expiry date (read-only).";
  }
  return undefined;
}

// Short hover descriptions per configuration field, shown as the label's title
// (hover). Keeps the V1 "no inline info-icons" decision while still giving an
// operator a one-line "what is this" on demand. Keyed by field label; an
// unmapped field simply has no tooltip.
const FIELD_TOOLTIPS: Record<string, string> = {
  // Network Basics
  Hostname: "Gateway hostname that identifies this device on the network.",
  "Source Interface":
    "Which local network interface active scans send from. Leave on Auto to use the OS default route; pick a NIC on a multi-homed laptop to force IP/BACnet/MQTT scans out the right adapter. Windows manages adapter IP settings; the app never changes them.",
  "IP Assignment": "How the gateway gets its address — Static IP or DHCP.",
  "IP Address":
    "The gateway device's planned IPv4 address on the site network (reference for validation — not this laptop's address).",
  "Subnet Mask": "Planned subnet mask of the gateway device's network (reference — not this laptop's mask).",
  Gateway:
    "Planned default gateway for the gateway device (reference — this laptop's own gateway is shown read-only under Source Interface).",
  "DNS Servers": "Planned DNS resolvers for the gateway device, comma-separated (reference values).",
  "VLAN ID": "802.1Q VLAN tag for the gateway's network, if used.",
  // BACnet Discovery
  "BACnet Network Number": "Logical BACnet network this gateway lives on.",
  "UDP Port": "BACnet/IP UDP port (default 47808).",
  "Device Instance Range": "Range of BACnet device instance IDs to discover.",
  BBMD: "BACnet Broadcast Management Device — relays broadcasts across subnets.",
  "BBMD Address": "IP of the BBMD to register with.",
  "BBMD UDP Port": "UDP port of the BBMD.",
  "Foreign Device": "Register as a BBMD foreign device (locked when BBMD is enabled).",
  TTL: "Foreign-device registration time-to-live, in seconds.",
  // MQTT Settings
  "MQTT Broker FQDN or IP Address": "Hostname or IP of the MQTT broker to connect to.",
  Port: "MQTT broker TCP port (1883 plain, 8883 TLS).",
  "Client ID": "Unique client identifier this gateway connects with.",
  "Root Topic": "Base MQTT topic prefix for this site's messages.",
  QoS: "MQTT delivery guarantee — 0 at most once, 1 at least once, 2 exactly once.",
  "Keep Alive Interval": "Seconds between MQTT keep-alive pings.",
  "MQTT Username": "Broker username, if authentication is required.",
  "MQTT Password": "Broker password (stored masked).",
  // Certificates & Keys
  "CA Certificate": "Trusted CA cert used to verify the broker's TLS certificate.",
  "Client Certificate": "Client TLS certificate for mutual authentication.",
  "Private Key": "Private key paired with the client certificate.",
  "Key Password": "Passphrase protecting the private key, if any.",
  "Certificate Expiry": "Read-only status derived from the stored certificate's expiry date.",
  // Time & NTP
  Timezone: "Site timezone used to timestamp commissioning evidence.",
  "Primary NTP Server": "Main NTP source used to sync the gateway clock.",
  "Secondary NTP Server": "Fallback NTP source.",
  "NTP Sync Interval": "Seconds between NTP clock syncs.",
  // Backup & Restore
  "Backup Schedule": "How often automatic backups run.",
  "Backup Retention": "How long backups are kept before pruning.",
  "Encrypted Backups": "Whether backup bundles are encrypted at rest.",
  "Backup Location": "Intended target path for backups (operator reference).",
  "Last Backup Status": "Result of the most recent backup.",
  "Restore Action": "Restore readiness / available restore action.",
  // Logging & Diagnostics
  "Log Level": "Verbosity of runtime logs (Info, Debug, etc.).",
  "Log Retention": "How long log files are kept.",
  "Remote Syslog Target": "IP/host of a remote syslog collector, if used.",
  "Syslog Port": "Port of the remote syslog target.",
  "Diagnostics Mode": "Extra diagnostic logging toggle.",
};

type FieldControlProps = {
  canEngineer: boolean;
  disabled?: boolean;
  expired?: boolean;
  field: string;
  hint?: string;
  kind: FieldKind;
  onFileSelect: (file: File | null) => void;
  onSecretChange: (content: string) => void;
  onSecretStore: () => void;
  onValueChange: (value: string) => void;
  // Optional display label per option VALUE (select kind only). The option
  // value stays the raw string; only the visible text differs.
  optionLabels?: Record<string, string>;
  options?: string[];
  secretContent: string;
  secretFileName: string | null;
  secretPending: boolean;
  value: string;
};

function FieldControl({
  canEngineer,
  disabled = false,
  expired = false,
  field,
  hint,
  kind,
  onFileSelect,
  onSecretChange,
  onSecretStore,
  onValueChange,
  optionLabels,
  options = [],
  secretContent,
  secretFileName,
  secretPending,
  value,
}: FieldControlProps) {
  const [maskedSentinel, setMaskedSentinel] = useState<string | null>(null);
  // Secret material (CA cert, client cert, private key) is collapsed to the
  // masked value + a "Replace…" button by default, so the Certificates card
  // stays compact. The paste box + file picker only appear when replacing.
  const [showSecretEditor, setShowSecretEditor] = useState(false);

  if (kind === "secret" || secretFields.has(field)) {
    return (
      <label className="secret-field" title={FIELD_TOOLTIPS[field]}>
        {field}
        <input readOnly value={maskSecretValue(value)} />
        {value && !showSecretEditor && (
          <small className="secret-stored">✓ Uploaded — in use by the tool. Replace to change.</small>
        )}
        {showSecretEditor ? (
          <>
            <textarea
              onChange={(event) => onSecretChange(event.target.value)}
              placeholder={`Paste ${field.toLowerCase()} content`}
              rows={4}
              value={secretContent}
            />
            <div className="inline-actions">
              <input
                accept=".pem,.crt,.cer,.key,.p12,.pfx"
                onChange={(event) => onFileSelect(event.target.files?.[0] ?? null)}
                type="file"
              />
              <button
                className="secondary-button compact"
                disabled={secretPending || !secretContent.trim() || !canEngineer}
                onClick={onSecretStore}
                title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
                type="button"
              >
                {secretPending ? "Saving..." : "Save & use file"}
              </button>
              <button
                className="secondary-button compact"
                onClick={() => setShowSecretEditor(false)}
                type="button"
              >
                Cancel
              </button>
            </div>
          </>
        ) : (
          <button
            className="secondary-button compact inline-link-button"
            disabled={!canEngineer}
            onClick={() => setShowSecretEditor(true)}
            title={canEngineer ? "Paste or upload a new value" : ENGINEER_REQUIRED_TOOLTIP}
            type="button"
          >
            Replace…
          </button>
        )}
        {secretFileName && <small>Loaded from {secretFileName}</small>}
        {hint && <small>{hint}</small>}
      </label>
    );
  }

  if (kind === "select") {
    return (
      <label title={FIELD_TOOLTIPS[field]}>
        {field}
        <select disabled={disabled} onChange={(event) => onValueChange(event.target.value)} value={value}>
          {!options.includes(value) && value !== "" && <option value={value}>{value}</option>}
          {options.map((option) => (
            <option key={option} value={option}>
              {optionLabels?.[option] ?? option}
            </option>
          ))}
        </select>
        {hint && <small>{hint}</small>}
      </label>
    );
  }

  if (kind === "textarea") {
    return (
      <label title={FIELD_TOOLTIPS[field]}>
        {field}
        <textarea disabled={disabled} onChange={(event) => onValueChange(event.target.value)} rows={4} value={value} />
        {hint && <small>{hint}</small>}
      </label>
    );
  }

  return (
    <label className={expired ? "field-expired" : undefined} title={FIELD_TOOLTIPS[field]}>
      {field}
      <input
        className={expired ? "field-expired" : undefined}
        onBlur={() => {
          if (kind === "password" && maskedSentinel && !value) {
            onValueChange(maskedSentinel);
          }
        }}
        onChange={(event) => onValueChange(event.target.value)}
        onFocus={() => {
          if (kind === "password" && isSecretSentinel(value)) {
            setMaskedSentinel(value);
            onValueChange("");
          }
        }}
        readOnly={disabled || kind === "readonly"}
        type={kind === "password" ? "password" : "text"}
        value={value}
      />
      {hint && <small>{hint}</small>}
    </label>
  );
}

function normalizeConfigurationForLocks(configuration: ConfigurationSnapshot): ConfigurationSnapshot {
  if (configuration.bacnet.values.BBMD !== "Enabled") {
    return configuration;
  }
  return {
    ...configuration,
    bacnet: {
      ...configuration.bacnet,
      values: {
        ...configuration.bacnet.values,
        "Foreign Device": "Disabled",
      },
    },
  };
}

function isBbmdEnabled(configuration: ConfigurationSnapshot): boolean {
  return configuration.bacnet.values.BBMD === "Enabled";
}

// Serialises an exported envelope to a downloadable JSON file via a transient
// object-URL anchor. No secret values are present in the envelope: password
// fields are masked and certificate material is a secret:// reference.
function downloadConfigurationEnvelope(envelope: ConfigurationExport): void {
  const blob = new Blob([JSON.stringify(envelope, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const stamp = envelope.exported_at.replace(/[:.]/g, "-");
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `smart-commissioning-configuration-${stamp}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

type ParsedConfiguration =
  | { ok: true; payload: ConfigurationExport | ConfigurationSnapshot }
  | { ok: false; error: string };

const configurationSectionKeys: ConfigurationSectionKey[] = [...sectionOrder];

// Parses and shape-checks an imported JSON file. Accepts either the exported
// envelope ({kind, configuration, ...}) or a bare snapshot, and verifies every
// section is present with a values object before handing it to the API, so an
// obviously-wrong file is rejected client-side with a clear message.
function parseConfigurationFile(raw: string): ParsedConfiguration {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: "Selected file is not valid JSON.", ok: false };
  }
  if (!parsed || typeof parsed !== "object") {
    return { error: "Configuration file must be a JSON object.", ok: false };
  }

  const record = parsed as Record<string, unknown>;
  const candidate =
    record.configuration && typeof record.configuration === "object"
      ? (record.configuration as Record<string, unknown>)
      : record;

  for (const section of configurationSectionKeys) {
    const sectionValue = candidate[section];
    if (!sectionValue || typeof sectionValue !== "object") {
      return { error: `Configuration file is missing the "${section}" section.`, ok: false };
    }
    const values = (sectionValue as Record<string, unknown>).values;
    if (!values || typeof values !== "object") {
      return { error: `Section "${section}" is missing its values.`, ok: false };
    }
  }

  return { ok: true, payload: parsed as ConfigurationExport | ConfigurationSnapshot };
}
