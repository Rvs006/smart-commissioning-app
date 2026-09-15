import { useEffect, useRef, useState } from "react";
import type { BacnetObjectBrowseResponse } from "../../api/client";
import { bacnetDeviceDetailItems, ipDeviceDetailItems } from "../workflow/discoveryRows";
import { ScannerSidePanel } from "./ScannerSidePanel";
import {
  bacnetDetailSections,
  ipDetailSections,
  mqttDetailSections,
  registerChip,
  serviceLines,
  type DetailSection,
  type ScannerRow,
} from "./scannerRows";
import type { ScannerLane } from "./useScannerRun";

const COPIED_MS = 1200;

export type DeviceDetailPanelProps = {
  lane: ScannerLane;
  row: ScannerRow | null;
  expanded: boolean;
  width: number;
  onClose: () => void;
  onToggleExpand: () => void;
  onResize: (width: number) => void;
  // BACnet live object browse (ephemeral; persists nothing).
  objectBrowse?: {
    canBrowse: boolean;
    blockedReason: string | null;
    pending: boolean;
    error: string | null;
    result: BacnetObjectBrowseResponse | null;
    onLoad: (deviceInstance: number) => void;
  };
};

function SectionList({ sections }: { sections: DetailSection[] }) {
  const [copied, setCopied] = useState<string | null>(null);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (copyTimerRef.current !== null) {
        clearTimeout(copyTimerRef.current);
      }
    },
    [],
  );
  // Carried over from the v0.1.58 capture table's per-row "Copy payload": a long
  // recorded value (an MQTT payload, a banner) is worth taking out whole.
  const copy = (key: string, value: string) => {
    try {
      void navigator.clipboard?.writeText(value);
    } catch {
      // Clipboard access can be denied; copy is a convenience, not load-bearing.
    }
    setCopied(key);
    if (copyTimerRef.current !== null) {
      clearTimeout(copyTimerRef.current);
    }
    copyTimerRef.current = setTimeout(() => setCopied(null), COPIED_MS);
  };
  return (
    <>
      {sections.map((section) => (
        <section className="scanner-detail-section" key={section.heading}>
          <h4>{section.heading}</h4>
          <dl className="scanner-kv">
            {section.items.map((item) => (
              <div key={item.label}>
                <dt>{item.label}</dt>
                <dd className={item.tone ? `scanner-kv-${item.tone}` : undefined}>
                  {item.value}
                  {item.copyable && item.value !== "—" && (
                    <button
                      className="secondary-button compact inline-link-button"
                      onClick={() => copy(`${section.heading}:${item.label}`, item.value)}
                      type="button"
                    >
                      {copied === `${section.heading}:${item.label}` ? "Copied" : `Copy ${item.label.toLowerCase()}`}
                    </button>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {section.note && (
            <p className={`scanner-detail-note${section.noteTone ? ` tone-${section.noteTone}` : ""}`}>
              {section.note}
            </p>
          )}
        </section>
      ))}
    </>
  );
}

/**
 * The sticky side inspector: one device's full evidence, replacing the
 * scroll-to-bottom modal the v0.1.58 screens used. Sections follow the vendored
 * tools' own detail panels so the two apps read the same; every value comes from
 * the persisted run attributes, none is derived here.
 */
export function DeviceDetailPanel({
  lane,
  row,
  expanded,
  width,
  onClose,
  onToggleExpand,
  onResize,
  objectBrowse,
}: DeviceDetailPanelProps) {
  if (!row) {
    return (
      <aside aria-labelledby="scanner-detail-heading" className="scanner-detail empty" role="complementary">
        <h3 id="scanner-detail-heading">{lane === "mqtt" ? "Topic detail" : "Device detail"}</h3>
        <p className="scanner-detail-empty">
          {lane === "mqtt"
            ? "Select a captured topic to view its payload and metadata."
            : "Select a device to view details."}
        </p>
      </aside>
    );
  }

  const sections = row.missing
    ? []
    : lane === "bacnet"
      ? bacnetDetailSections(row)
      : lane === "mqtt"
        ? mqttDetailSections(row)
        : ipDetailSections(row);
  const services = lane === "ip" && !row.missing ? serviceLines(row.attributes.services) : [];
  // The persisted attribute list the v0.1.58 dialog showed, kept verbatim so no
  // engine-recorded field is lost by the new section layout. A missing device has
  // no persisted device record at all, so there is nothing honest to list.
  // MQTT rows carry no persisted device record; mqttDetailSections already lists
  // every field the capture stamped, so there is no second raw list to append.
  const rawItems =
    row.missing || lane === "mqtt"
      ? []
      : lane === "bacnet"
        ? bacnetDeviceDetailItems(row.attributes)
        : ipDeviceDetailItems(row.attributes);
  const registerLabel = registerChip(row.register, row.tone);
  // BACnet has no probe flag: a global Who-Is reaches the whole local segment,
  // so a silent device really was asked. Only the IP sweep can miss an address.
  const missingHeading =
    lane === "ip" && row.probed === false ? "Expected, not probed" : "Expected, no response";
  const missingNote =
    lane === "ip" && row.probed === false
      ? "This host is in the register but its address falls outside the range this scan swept, so the scan never reached it. Its silence is not evidence: widen Start/End to cover it and scan again."
      : lane === "ip" && row.probed === undefined
        ? "This host is in the register and was not seen. Whether the scan reached its address was not recorded for this run, so nothing here says the host is absent."
        : "This device is in the register but did not answer this scan. Nothing was observed, so there is no live evidence to show — only what the register expected.";
  const browseResult =
    objectBrowse?.result && objectBrowse.result.device_instance === row.deviceInstance
      ? objectBrowse.result
      : null;

  return (
    <ScannerSidePanel
      badges={
        // MQTT rows have no Status column; their one verdict is the register
        // match, and its own cell already carries the right wording and tone.
        lane === "mqtt" ? (
          <span className={`scanner-chip chip-${row.cells["Register Match"]?.chip ?? "neutral"}`}>
            {row.cells["Register Match"]?.text ?? "—"}
          </span>
        ) : (
          <>
            <span className={`scanner-chip chip-${row.cells.Status?.chip ?? "neutral"}`}>
              {row.cells.Status?.text ?? "—"}
            </span>
            <span className={`scanner-chip chip-${registerLabel.chip ?? "neutral"}`}>
              {registerLabel.text}
            </span>
          </>
        )
      }
      expanded={expanded}
      onClose={onClose}
      onResize={onResize}
      onToggleExpand={lane === "bacnet" ? onToggleExpand : undefined}
      title={row.title}
      width={width}
    >
        {row.missing && (
          <section className="scanner-detail-section">
            {/* Silence is only evidence if something was actually sent. A
                register host outside the scanned range was never contacted, so
                the panel must not report it as having failed to answer. */}
            <h4>{missingHeading}</h4>
            <p className="scanner-detail-note tone-fail">{missingNote}</p>
            <dl className="scanner-kv">
              <div>
                <dt>{lane === "bacnet" ? "Expected instance" : "Expected address"}</dt>
                <dd>
                  {lane === "bacnet"
                    ? (row.cells.Instance?.text ?? "—")
                    : (row.cells.Address?.text ?? "—")}
                </dd>
              </div>
              <div>
                <dt>{lane === "bacnet" ? "Expected name" : "Expected hostname"}</dt>
                <dd>{(lane === "bacnet" ? row.cells.Name?.text : row.cells.Hostname?.text) ?? "—"}</dd>
              </div>
              {lane === "ip" && (
                <>
                  <div>
                    <dt>Probe sent</dt>
                    <dd className={row.probed === true ? undefined : "scanner-kv-fail"}>
                      {row.probed === true
                        ? "Yes, inside the scanned range"
                        : row.probed === false
                          ? "No, address outside the scanned range"
                          : "Not recorded for this run"}
                    </dd>
                  </div>
                  <div>
                    <dt>Hostname check</dt>
                    {/* The engine deliberately leaves `hostname` null here, so
                        the panel must not imply the name was resolved — nor
                        that a lookup failed on a host nothing was sent to. */}
                    <dd className="scanner-kv-fail">
                      {row.probed === false
                        ? "Not attempted — the host was never contacted"
                        : "Expected, not resolved on the network"}
                    </dd>
                  </div>
                </>
              )}
            </dl>
          </section>
        )}

        <SectionList sections={sections} />

        {lane === "ip" && !row.missing && (
          <section className="scanner-detail-section">
            <h4>Services &amp; software</h4>
            {services.length === 0 ? (
              <p className="scanner-detail-empty">None observed</p>
            ) : (
              <ul className="scanner-service-list">
                {services.map((service, index) => (
                  <li key={`${service.head}-${index}`}>
                    <span className="scanner-service-head">{service.head}</span>
                    {service.detail && (
                      <span className="scanner-service-detail">{service.detail}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {/* A never-observed device has no live object list to read; offering the
            browse button there would invite a request that cannot succeed. */}
        {lane === "bacnet" && objectBrowse && !row.missing && (
          <section aria-live="polite" className="scanner-detail-section">
            <h4>Object list</h4>
            <p className="scanner-detail-note">
              Reads this device&apos;s object list and present values directly from the network.
              Nothing is persisted; the scan results are unchanged.
            </p>
            <button
              className="secondary-button compact"
              disabled={
                objectBrowse.pending ||
                !objectBrowse.canBrowse ||
                row.deviceInstance === undefined
              }
              onClick={() => {
                if (row.deviceInstance !== undefined) {
                  objectBrowse.onLoad(row.deviceInstance);
                }
              }}
              type="button"
            >
              {objectBrowse.pending ? "Reading object list..." : "Load objects"}
            </button>
            {objectBrowse.blockedReason && (
              <p className="scanner-detail-note">{objectBrowse.blockedReason}</p>
            )}
            {row.deviceInstance === undefined && (
              <p className="scanner-detail-note">This row has no device instance to read.</p>
            )}
            {objectBrowse.error && (
              <div className="state-panel error" role="alert">
                <strong>Object browse failed</strong>
                <span>{objectBrowse.error}</span>
              </div>
            )}
            {browseResult && (
              <>
                {browseResult.error && (
                  <div className="state-panel" role="status">
                    <strong>Device did not return a full object list</strong>
                    <span>{browseResult.error}</span>
                  </div>
                )}
                {browseResult.objects.length > 0 && (
                  <div className="scanner-object-grid" role="table">
                    <div className="scanner-object-row head" role="row">
                      <span role="columnheader">Object</span>
                      <span role="columnheader">Name</span>
                      <span role="columnheader">Present value</span>
                      <span role="columnheader">Units</span>
                    </div>
                    {browseResult.objects.map((object) => (
                      <div
                        className="scanner-object-row"
                        key={`${object.type_name}-${object.instance}`}
                        role="row"
                      >
                        <span className="mono" role="cell">{`${object.type_name}-${object.instance}`}</span>
                        <span role="cell">{object.name || "—"}</span>
                        <span className="mono" role="cell">
                          {object.present_value || "—"}
                        </span>
                        <span role="cell">{object.units || "—"}</span>
                      </div>
                    ))}
                  </div>
                )}
                <p className="scanner-detail-note">
                  {`${browseResult.count} object${browseResult.count === 1 ? "" : "s"} on device · showing ${browseResult.objects.length}`}
                  {browseResult.truncated ? " · list truncated at the read cap" : ""}
                </p>
              </>
            )}
          </section>
        )}

        {rawItems.length > 0 && (
          <section className="scanner-detail-section">
            <h4>All recorded attributes</h4>
            <dl className="scanner-kv">
              {rawItems.map((item) => (
                <div key={item.label}>
                  <dt>{item.label}</dt>
                  <dd>{item.value}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}
    </ScannerSidePanel>
  );
}
