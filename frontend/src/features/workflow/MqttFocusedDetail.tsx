import { useEffect, useMemo, useRef, useState } from "react";
import type { MqttLiveFocused, MqttLiveTopicDetail } from "../../api/client";

// GAP-M1: the full focused-asset detail. All of it rides the live snapshot
// stream (the focus call is fire-and-forget), so this component is pure over the
// `focused` object — no fetches of its own. Four tabs mirror the vendored tool:
// Overview (identity + topics + register comparison), Live payload (history
// scrubber + pause + copy), Points (live points RAG-verdicted against the
// register) and Metadata (discovered UDMI identity vs register expected).
//
// Everything is visible by default: switching tabs toggles [hidden] on already
// rendered panels, it never gates content on an animation.

const COPIED_MS = 1200;

type Tab = "overview" | "payload" | "points" | "metadata";

// Point match key — mirrors udmi.js normPoint (case / separator insensitive),
// so the frontend RAG verdict matches the backend comparison exactly.
function normPoint(value: string): string {
  return value.trim().toLowerCase().replace(/[\s-]+/g, "_");
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function formatTs(ts: number): string {
  if (!ts || !Number.isFinite(ts)) {
    return "";
  }
  // The sidecar stamps millisecond epochs; tolerate a seconds epoch too.
  const millis = ts < 1e12 ? ts * 1000 : ts;
  const date = new Date(millis);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString();
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function freshestTopic(focused: MqttLiveFocused): MqttLiveTopicDetail | null {
  const detail = focused.topicsDetail ?? [];
  return detail.find((entry) => entry.topic === focused.lastTopic) ?? detail[0] ?? null;
}

function ComparisonBar({ matched, missing, extra }: { matched: number; missing: number; extra: number }) {
  const total = Math.max(1, matched + missing + extra);
  const pct = (part: number) => Math.round((part / total) * 100);
  return (
    <div>
      <div className="mqtt-cmp-bar" aria-hidden="true">
        <div className="mqtt-cmp-seg matched" style={{ width: `${(matched / total) * 100}%` }} />
        <div className="mqtt-cmp-seg missing" style={{ width: `${(missing / total) * 100}%` }} />
        <div className="mqtt-cmp-seg extra" style={{ width: `${(extra / total) * 100}%` }} />
      </div>
      <div className="mqtt-cmp-legend">
        <span>Matched {matched} ({pct(matched)}%)</span>
        <span>Missing {missing} ({pct(missing)}%)</span>
        <span>Extra {extra} ({pct(extra)}%)</span>
      </div>
    </div>
  );
}

export function MqttFocusedDetail({
  focused,
  canEngineer,
  onWriteConfig,
}: {
  focused: MqttLiveFocused;
  canEngineer: boolean;
  onWriteConfig: (topic: string, payload: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const [paused, setPaused] = useState(false);
  // null = live (freshest payload); a number indexes into the freshest topic's
  // history (oldest-first), matching the vendored history scrubber.
  const [histView, setHistView] = useState<number | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The live payload frozen at the moment Pause was pressed.
  const [frozenLive, setFrozenLive] = useState<string>(focused.lastPayload);

  // Reset the transient scrubber/pause when the operator focuses another asset.
  useEffect(() => {
    setHistView(null);
    setPaused(false);
  }, [focused.asset]);

  // Hold the newest live payload unless paused (then it freezes in place).
  useEffect(() => {
    if (!paused) {
      setFrozenLive(focused.lastPayload);
    }
  }, [focused.lastPayload, paused]);

  useEffect(
    () => () => {
      if (copyTimerRef.current !== null) {
        clearTimeout(copyTimerRef.current);
      }
    },
    [],
  );

  const copy = (key: string, text: string) => {
    try {
      void navigator.clipboard?.writeText(text);
    } catch {
      // Clipboard can be denied; copy is a convenience, not load-bearing.
    }
    setCopied(key);
    if (copyTimerRef.current !== null) {
      clearTimeout(copyTimerRef.current);
    }
    copyTimerRef.current = setTimeout(() => setCopied(null), COPIED_MS);
  };

  const fresh = freshestTopic(focused);
  const history = fresh?.history ?? [];
  const shownPayload =
    histView !== null && history[histView]
      ? history[histView].raw
      : paused
        ? frozenLive
        : focused.lastPayload;

  const comparison = focused.comparison;
  const meta = focused.meta;
  const udmi = focused.udmi ?? {};

  // Points table verdicts (udmi.js comparePoints semantics): a live point is
  // "extra" when its normalised name is not expected, else "matched"; expected
  // names never seen live are appended as "missing". Neutral when unregistered.
  const pointRows = useMemo(() => {
    const extra = new Set(comparison?.extraNames ?? []);
    const rows = (focused.livePoints ?? []).map((point) => ({
      name: point.name,
      value: formatValue(point.value),
      unit: point.unit || "—",
      ts: formatTs(point.ts),
      verdict: focused.matched ? (extra.has(normPoint(point.name)) ? "extra" : "matched") : "neutral",
    }));
    if (focused.matched) {
      for (const missing of comparison?.missingNames ?? []) {
        rows.push({ name: missing, value: "—", unit: "", ts: "", verdict: "missing" });
      }
    }
    return rows;
  }, [comparison, focused.livePoints, focused.matched]);

  const overviewKv: Array<[string, string]> = [
    ["Asset", focused.asset],
    ["Type", meta?.type || "—"],
    ["Schema", focused.schema ? focused.schema + (udmi.version ? ` · ${udmi.version}` : "") : "—"],
    ["Topics", String((focused.topics ?? []).length)],
    ["Message rate", `${(focused.rate ?? 0).toFixed(1)} msg/s`],
    ["Total messages", String(focused.count ?? 0)],
    ["Live points", String((focused.livePoints ?? []).length)],
    ["In register", focused.matched ? "Yes" : "No"],
    ["Site", meta?.site || udmi.site || "—"],
    ["Room / Location", meta?.location || udmi.room || "—"],
  ];
  if (udmi.gatewayId) {
    overviewKv.push(["Gateway", udmi.gatewayId]);
  }
  if (udmi.proxyIds && udmi.proxyIds.length > 0) {
    overviewKv.push(["Proxies", udmi.proxyIds.join(", ")]);
  }
  if (udmi.guid) {
    overviewKv.push(["GUID", udmi.guid]);
  }

  const tabs: Array<[Tab, string]> = [
    ["overview", "Overview"],
    ["payload", "Live payload"],
    ["points", "Points"],
    ["metadata", "Metadata"],
  ];

  return (
    <div className="property-expansion-panel" aria-live="polite">
      <div className="surface-heading">
        <div>
          <strong>Focused: {focused.asset}</strong>
          <p className="section-copy">Live detail for this asset. Read-only unless you write a config.</p>
        </div>
        {focused.configTopic ? (
          <button
            className="secondary-button compact"
            disabled={!canEngineer}
            onClick={() => onWriteConfig(focused.configTopic, focused.configPayload)}
            title={
              canEngineer
                ? "Open the publish dialog prefilled with this device's config topic and last-seen config payload (retain on)."
                : "Engineer role required."
            }
            type="button"
          >
            Write config…
          </button>
        ) : null}
      </div>

      <div className="mqtt-focus-tabs" role="tablist">
        {tabs.map(([id, label]) => (
          <button
            aria-selected={tab === id}
            className="mqtt-focus-tab"
            key={id}
            onClick={() => setTab(id)}
            role="tab"
            type="button"
          >
            {label}
          </button>
        ))}
      </div>

      {/* Overview */}
      <div hidden={tab !== "overview"} role="tabpanel">
        <dl className="mqtt-focus-kv">
          {overviewKv.map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <strong>Topics</strong>
        {(focused.topicsDetail ?? []).length > 0 ? (
          <div>
            {focused.topicsDetail.map((entry) => (
              <div className="mqtt-topic-line" key={entry.topic}>
                <span className="tl-path">{entry.topic}</span>
                <span className="tl-rate">{(entry.rate ?? 0).toFixed(1)} msg/s</span>
                <button
                  className="secondary-button compact"
                  onClick={() => copy(`topic:${entry.topic}`, entry.topic)}
                  type="button"
                >
                  {copied === `topic:${entry.topic}` ? "Copied" : "Copy"}
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="section-copy">No topics yet.</p>
        )}
        <strong>Register comparison</strong>
        {focused.matched ? (
          <ComparisonBar matched={comparison?.matched ?? 0} missing={comparison?.missing ?? 0} extra={comparison?.extra ?? 0} />
        ) : (
          <p className="section-copy">This asset is not in the imported register.</p>
        )}
      </div>

      {/* Live payload */}
      <div hidden={tab !== "payload"} role="tabpanel">
        <div className="mqtt-hist-chips">
          <button
            aria-pressed={histView === null}
            className="mqtt-hist-chip"
            onClick={() => setHistView(null)}
            type="button"
          >
            Live
          </button>
          {history
            .map((entry, index) => ({ entry, index }))
            .reverse()
            .map(({ entry, index }) => (
              <button
                aria-pressed={histView === index}
                className="mqtt-hist-chip"
                key={`${entry.ts}-${index}`}
                onClick={() => setHistView(index)}
                type="button"
              >
                {formatTs(entry.ts) || `#${index + 1}`}
              </button>
            ))}
        </div>
        <div className="inline-actions">
          <button
            aria-pressed={paused}
            className="secondary-button compact"
            disabled={histView !== null}
            onClick={() => setPaused((current) => !current)}
            title={histView !== null ? "Viewing history — return to Live to pause the stream." : undefined}
            type="button"
          >
            {paused ? "Resume" : "Pause"}
          </button>
          <button
            className="secondary-button compact"
            disabled={!shownPayload}
            onClick={() => copy("payload", shownPayload)}
            type="button"
          >
            {copied === "payload" ? "Copied" : "Copy payload"}
          </button>
        </div>
        {shownPayload ? (
          <pre className="mqtt-payload-view">{prettyJson(shownPayload)}</pre>
        ) : (
          <p className="section-copy">No payload seen yet for this asset.</p>
        )}
      </div>

      {/* Points */}
      <div hidden={tab !== "points"} role="tabpanel">
        {pointRows.length > 0 ? (
          <div className="data-table-wrap results-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Point</th>
                  <th scope="col">Value</th>
                  <th scope="col">Units</th>
                  <th scope="col">Register</th>
                  <th scope="col">Last seen</th>
                </tr>
              </thead>
              <tbody>
                {pointRows.map((row) => (
                  <tr key={`${row.name}-${row.verdict}`}>
                    <td>{row.name}</td>
                    <td>{row.value}</td>
                    <td>{row.unit}</td>
                    <td>
                      {row.verdict === "matched" ? (
                        <span className="status-token ready">matched</span>
                      ) : row.verdict === "extra" ? (
                        <span className="status-token warning">extra</span>
                      ) : row.verdict === "missing" ? (
                        <span className="status-token failed">missing</span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{row.ts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="section-copy">No points extracted yet.</p>
        )}
      </div>

      {/* Metadata */}
      <div hidden={tab !== "metadata"} role="tabpanel">
        {udmi.gatewayId || udmi.proxyIds?.length || udmi.site || udmi.room || udmi.guid || udmi.version ? (
          <>
            <strong>Discovered (from payload)</strong>
            <dl className="mqtt-focus-kv">
              {udmi.gatewayId ? (
                <div style={{ display: "contents" }}>
                  <dt>Gateway ID</dt>
                  <dd>{udmi.gatewayId}</dd>
                </div>
              ) : null}
              {udmi.proxyIds && udmi.proxyIds.length > 0 ? (
                <div style={{ display: "contents" }}>
                  <dt>Proxied devices</dt>
                  <dd>{udmi.proxyIds.join(", ")}</dd>
                </div>
              ) : null}
              {udmi.site ? (
                <div style={{ display: "contents" }}>
                  <dt>Site</dt>
                  <dd>{udmi.site}</dd>
                </div>
              ) : null}
              {udmi.room ? (
                <div style={{ display: "contents" }}>
                  <dt>Room</dt>
                  <dd>{udmi.room}</dd>
                </div>
              ) : null}
              {udmi.guid ? (
                <div style={{ display: "contents" }}>
                  <dt>GUID</dt>
                  <dd>{udmi.guid}</dd>
                </div>
              ) : null}
              {udmi.version ? (
                <div style={{ display: "contents" }}>
                  <dt>UDMI version</dt>
                  <dd>{udmi.version}</dd>
                </div>
              ) : null}
            </dl>
          </>
        ) : null}
        {meta ? (
          <>
            <strong>Register (expected)</strong>
            <dl className="mqtt-focus-kv">
              {(
                [
                  ["Asset", meta.asset],
                  ["Type", meta.type],
                  ["Topic", meta.topic],
                  ["Schema", meta.schema],
                  ["Site", meta.site],
                  ["Location", meta.location],
                  ["Description", meta.description],
                ] as Array<[string, string | undefined]>
              ).map(([key, value]) => (
                <div key={key} style={{ display: "contents" }}>
                  <dt>{key}</dt>
                  <dd>{value || "—"}</dd>
                </div>
              ))}
            </dl>
            <strong>Expected points ({(meta.points ?? []).length})</strong>
            {(meta.points ?? []).length > 0 ? (
              <p className="section-copy">
                {meta.points!.map((point) => point.name + (point.unit ? ` (${point.unit})` : "")).join(", ")}
              </p>
            ) : (
              <p className="section-copy">None.</p>
            )}
          </>
        ) : (
          <p className="section-copy">
            {udmi.gatewayId || udmi.site
              ? "Not in the imported register."
              : "No metadata yet — not in register and no UDMI identity block in the payloads."}
          </p>
        )}
      </div>
    </div>
  );
}
