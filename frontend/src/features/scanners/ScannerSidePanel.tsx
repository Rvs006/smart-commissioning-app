import { useEffect, useRef, type ReactNode } from "react";
import {
  SCANNER_PANEL_MAX_WIDTH,
  SCANNER_PANEL_MIN_WIDTH,
  clampPanelWidth,
} from "./scannerRows";

export type ScannerSidePanelProps = {
  /** Heading shown in the panel head; also what the aside is labelled by. */
  title: string;
  width: number;
  onResize: (width: number) => void;
  onClose: () => void;
  /** Pop-out / collapse (BACnet only today). Omit to hide the control. */
  expanded?: boolean;
  onToggleExpand?: () => void;
  /** Chips under the head (status / register verdict). */
  badges?: ReactNode;
  /**
   * The heading's DOM id, which also labels the aside. Override it when two
   * panels can be on one page (the MQTT screen has a live one and a captured-row
   * one), so neither the id nor the accessible name is duplicated.
   */
  headingId?: string;
  children: ReactNode;
};

/**
 * The sticky, resizable side panel chrome from plan section 4.5: a drag handle on
 * the left edge, a head with the subject's name and a close control, and a
 * scrolling body. It holds a scanned device's evidence on the IP / BACnet
 * screens and the live focused-asset detail on the MQTT screen, so the two read
 * as the same object rather than two hand-built panels.
 */
export function ScannerSidePanel({
  title,
  width,
  onResize,
  onClose,
  expanded = false,
  onToggleExpand,
  badges,
  headingId = "scanner-detail-heading",
  children,
}: ScannerSidePanelProps) {
  const dragFromRef = useRef<{ x: number; width: number } | null>(null);
  // Whatever had focus when the panel opened (the results row, the rail's Focus
  // button). Closing hands focus back there instead of dropping it on <body>.
  const openerRef = useRef<Element | null>(
    typeof document === "undefined" ? null : document.activeElement,
  );
  const closeToOpener = () => {
    const opener = openerRef.current;
    if (opener instanceof HTMLElement && opener.isConnected) {
      opener.focus();
    }
    // After, so a caller with a better target (ScannerScreen's row map) wins.
    onClose();
  };

  // Esc collapses the expanded panel (the vendored tool's pop-out behaviour); a
  // second Esc is left to the browser so nothing traps the operator.
  useEffect(() => {
    if (!expanded || !onToggleExpand) {
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

  return (
    <aside
      aria-labelledby={headingId}
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
        <h3 id={headingId}>{title}</h3>
        <div className="scanner-detail-actions">
          {onToggleExpand && (
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
          <button className="scanner-icon-button" onClick={closeToOpener} type="button">
            <span aria-hidden="true">✕</span>
            <span className="visually-hidden">Close detail panel</span>
          </button>
        </div>
      </div>

      {badges && <div className="scanner-detail-badges">{badges}</div>}

      <div className="scanner-detail-body">{children}</div>
    </aside>
  );
}
