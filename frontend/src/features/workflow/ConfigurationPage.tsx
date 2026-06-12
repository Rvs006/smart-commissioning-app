import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ConfigurationSectionKey,
  ConfigurationSnapshot,
  getConfiguration,
  storeSecretMaterial,
  updateConfiguration,
  validateConfiguration,
} from "../../api/client";
import { isSecretSentinel, maskSecretValue } from "./secretField";

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
  device: "Gateway network identity used by discovery and validation services.",
  logging: "Runtime diagnostics, log retention, syslog, and current logging health.",
  mqtt: "Broker, client identity, topic, QoS, keep-alive, and optional Mosquitto-style credentials.",
  time: "Timezone and NTP settings used to timestamp evidence and validate stale data.",
};

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
  },
};

const secretFields = new Set(["CA Certificate", "Client Certificate", "Private Key"]);

export function ConfigurationPage() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ConfigurationSnapshot | null>(null);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({});
  const [secretFiles, setSecretFiles] = useState<Record<string, string | null>>({});
  const [secretMessage, setSecretMessage] = useState<string | null>(null);

  const configurationQuery = useQuery({
    queryFn: getConfiguration,
    queryKey: ["configuration"],
  });

  useEffect(() => {
    if (configurationQuery.data) {
      setDraft(normalizeConfigurationForLocks(configurationQuery.data));
      setValidationErrors([]);
    }
  }, [configurationQuery.data]);

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

  const changeStatus = (section: ConfigurationSectionKey, status: string) => {
    setDraft((current) => {
      if (!current) {
        return current;
      }

      return {
        ...current,
        [section]: {
          ...current[section],
          status,
        },
      };
    });
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
          <button className="primary-button" disabled={saveMutation.isPending} type="submit">
            {saveMutation.isPending ? "Saving..." : "Save Configuration"}
          </button>
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
        {sectionOrder.map((section) => (
          <article className="config-section" key={section}>
            <div className="section-heading">
              <div>
                <span>{sectionLabels[section]}</span>
                <h3>{draft[section].status}</h3>
              </div>
              <label>
                Section status
                <input
                  onChange={(event) => changeStatus(section, event.target.value)}
                  value={draft[section].status}
                />
              </label>
            </div>
            <p className="section-copy">{sectionDescriptions[section]}</p>
            <div className="field-grid">
              {Object.entries(draft[section].values).map(([field, value]) => (
                <FieldControl
                  disabled={section === "bacnet" && field === "Foreign Device" && isBbmdEnabled(draft)}
                  field={field}
                  hint={
                    section === "bacnet" && field === "Foreign Device" && isBbmdEnabled(draft)
                      ? "Locked because BBMD is enabled."
                      : undefined
                  }
                  kind={fieldDefinitions[section]?.[field]?.kind ?? "text"}
                  key={field}
                  onFileSelect={(file) => handleSecretFile(field, file)}
                  onSecretChange={(content) => setSecretDrafts((current) => ({ ...current, [field]: content }))}
                  onSecretStore={() => handleSecretStore(field)}
                  onValueChange={(nextValue) => changeValue(section, field, nextValue)}
                  options={fieldDefinitions[section]?.[field]?.options}
                  secretContent={secretDrafts[field] ?? ""}
                  secretFileName={secretFiles[field] ?? null}
                  secretPending={secretMutation.isPending}
                  value={value}
                />
              ))}
            </div>
          </article>
        ))}
      </section>
    </form>
  );
}

type FieldControlProps = {
  disabled?: boolean;
  field: string;
  hint?: string;
  kind: FieldKind;
  onFileSelect: (file: File | null) => void;
  onSecretChange: (content: string) => void;
  onSecretStore: () => void;
  onValueChange: (value: string) => void;
  options?: string[];
  secretContent: string;
  secretFileName: string | null;
  secretPending: boolean;
  value: string;
};

function FieldControl({
  disabled = false,
  field,
  hint,
  kind,
  onFileSelect,
  onSecretChange,
  onSecretStore,
  onValueChange,
  options = [],
  secretContent,
  secretFileName,
  secretPending,
  value,
}: FieldControlProps) {
  const [maskedSentinel, setMaskedSentinel] = useState<string | null>(null);

  if (kind === "secret" || secretFields.has(field)) {
    return (
      <label className="secret-field">
        {field}
        <input readOnly value={maskSecretValue(value)} />
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
            disabled={secretPending || !secretContent.trim()}
            onClick={onSecretStore}
            type="button"
          >
            {secretPending ? "Storing..." : "Store masked reference"}
          </button>
        </div>
        {secretFileName && <small>Loaded from {secretFileName}</small>}
        {hint && <small>{hint}</small>}
      </label>
    );
  }

  if (kind === "select") {
    return (
      <label>
        {field}
        <select disabled={disabled} onChange={(event) => onValueChange(event.target.value)} value={value}>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        {hint && <small>{hint}</small>}
      </label>
    );
  }

  if (kind === "textarea") {
    return (
      <label>
        {field}
        <textarea disabled={disabled} onChange={(event) => onValueChange(event.target.value)} rows={4} value={value} />
        {hint && <small>{hint}</small>}
      </label>
    );
  }

  return (
    <label>
      {field}
      <input
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
