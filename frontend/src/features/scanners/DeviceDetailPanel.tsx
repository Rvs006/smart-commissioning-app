import { useEffect, useRef } from "react";
import type { BacnetObjectBrowseResponse } from "../../api/client";
import { bacnetDeviceDetailItems, ipDeviceDetailItems } from "../workflow/discoveryRows";
import {
  SCANNER_PANEL_MAX_WIDTH,
  SCANNER_PANEL_MIN_WIDTH,
  bacnetDetailSections,
  clampPanelWidth,
  ipDetailSections,
  registerChip,
  serviceLines,
  type DetailSection,
  type ScannerRow,
} from "./scannerRows";
import type { ScannerLane } from "./useScannerRun";

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
  return (
    <>
      {sections.map((section) => (
        <section className="scanner-detail-section" key={section.heading}>
          <h4>{section.heading}</h4>
          <dl className="scanner-kv">
            {section.items.map((item) => (
              <div key={item.label}>
                <dt>{item.label}</dt>
                <dd className={item.tone ? `scanner-kv-${item.tone}` : undefined}>{item.value}</dd>
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
  const dragFromRef = useRef<{ x: number; width: number } | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  // Esc collapses the expanded panel (the vendored tool's pop-out behaviour); a
  // second Esc is left to the browser so nothing traps the operator.
  useEffect(() => {
    if (!expanded) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onToggleExpand();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded, onToggleExpand]);

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      const from = dragFromRef.current;
      if (!from) {
        return;
      }
      // The panel sits on the right, so dragging left widens it.
      onResize(clampPanelWidth(from.width + (from.x - event.clientX)));
    };
    const onUp = () => {
      dragFromRef.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [onResize]);

  if (!row) {
    return (
      <aside aria-labelledby="scanner-detail-heading" className="scanner-detail empty" role="complementary">
        <h3 id="scanner-detail-heading">Device detail</h3>
        <p className="scanner-detail-empty">Select a device to view details.</p>
      </aside>
    );
  }

  const sections = row.missing
    ? []
    : lane === "bacnet"
      ? bacnetDetailSections(row)
      : ipDetailSections(row);
  const services = lane === "ip" && !row.missing ? serviceLines(row.attributes.services) : [];
  // The persisted attribute list the v0.1.58 dialog showed, kept verbatim so no
  // engine-recorded field is lost by the new section layout. A missing device has
  // no persisted device record at all, so there is nothing honest to list.
  const rawItems = row.missing
    ? []
    : lane === "bacnet"
      ? bacnetDeviceDetailItems(row.attributes)
      : ipDeviceDetailItems(row.attributes);
  const registerLabel = registerChip(row.register, row.tone);
  const browseResult =
    objectBrowse?.result && objectBrowse.result.device_instance === row.deviceInstance
      ? objectBrowse.result
      : null;

  return (
    <aside
      aria-labelledby="scanner-detail-heading"
      className={`scanner-detail${expanded ? " expanded" : ""}`}
      role="complementary"
    >
      <div
        aria-label="Resize detail panel"
        aria-orientation="vertical"
        aria-valuemax={SCANNER_PANEL_MAX_WIDTH}
        aria-valuemin={SCANNER_PANEL_MIN_WIDTH}
        aria-valuenow={width}
        className="scanner-detail-resizer"
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") {
            onResize(clampPanelWidth(width + 16));
          } else if (event.key === "ArrowRight") {
            onResize(clampPanelWidth(width - 16));
          }
        }}
        onPointerDown={(event) => {
          dragFromRef.current = { x: event.clientX, width };
        }}
        role="separator"
        tabIndex={0}
      />
      <div className="scanner-detail-head">
        <h3 id="scanner-detail-heading">{row.title}</h3>
        <div className="scanner-detail-actions">
          {lane === "bacnet" && (
            <button
              aria-pressed={expanded}
              className="scanner-icon-button"
              onClick={onToggleExpand}
              title={expanded ? "Collapse the panel (Esc)" : "Expand the panel"}
              type="button"
            >
              <span aria-hidden="true">⤢</span>
              <span className="visually-hidden">{expanded ? "Collapse" : "Expand"} detail panel</span>
            </button>
          )}
          <button className="scanner-icon-button" onClick={onClose} ref={closeRef} type="button">
            <span aria-hidden="true">✕</span>
            <span className="visually-hidden">Close detail panel</span>
          </button>
        </div>
      </div>

      <div className="scanner-detail-badges">
        <span className={`scanner-chip chip-${row.cells.Status?.chip ?? "neutral"}`}>
          {row.cells.Status?.text ?? "—"}
        </span>
        <span className={`scanner-chip chip-${registerLabel.chip ?? "neutral"}`}>
          {registerLabel.text}
        </span>
      </div>

      <div className="scanner-detail-body">
        {row.missing && (
          <section className="scanner-detail-section">
            <h4>Expected, no response</h4>
            <p className="scanner-detail-note tone-fail">
              This device is in the register but did not answer this scan. Nothing was observed, so
              there is no live evidence to show — only what the register expected.
            </p>
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
                <div>
                  <dt>Hostname check</dt>
                  {/* The engine deliberately leaves `hostname` null here, so the
                      panel must not imply the name was resolved. */}
                  <dd className="scanner-kv-fail">Expected, not resolved on the network</dd>
                </div>
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
      </div>
    </aside>
  );
}
