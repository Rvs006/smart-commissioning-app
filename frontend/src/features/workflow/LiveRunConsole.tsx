import { useEffect, useMemo, useState } from "react";
import type {
  RunRecord,
  UdmiAssetTopicDiscovery,
  UdmiValidationSummaryV1,
} from "../../api/client";
import { formatRelativeTime, humanizeStage, toHealthState } from "./runFormat";

type StateSample = Readonly<{ at: number; progress: number; issues: number; stage: string }>;
type AssetObservation = Readonly<{ expected: number; observed: number }>;
type TrackedTopicStatus =
  | "expected_topic_observed"
  | "alternate_topic_observed"
  | "no_matching_asset_id_topic_observed";

type LiveRunConsoleProps = Readonly<{
  assetTopicDiscovery: UdmiAssetTopicDiscovery | null;
  elapsed: string;
  issueCount: number;
  progress: number;
  run: RunRecord;
  status: RunRecord["status"];
  stage: string;
  validationSummary: UdmiValidationSummaryV1 | null;
}>;

const MAX_ASSET_SAMPLES = 60;
const MAX_STATE_SAMPLES = 60;
const TRACKED_TOPIC_STATUSES: readonly TrackedTopicStatus[] = [
  "expected_topic_observed",
  "alternate_topic_observed",
  "no_matching_asset_id_topic_observed",
];

function stateSampleKey(sample: StateSample): string {
  return `${sample.progress}:${sample.issues}:${sample.stage}`;
}

function isMetricCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function hasUsableTopicLedger(
  discovery: UdmiAssetTopicDiscovery | null,
): discovery is UdmiAssetTopicDiscovery {
  return Boolean(
    discovery &&
      typeof discovery.scope === "string" &&
      discovery.scope.trim() !== "" &&
      discovery.scope_error === null &&
      (discovery.scope_source === "register_common_ancestor" || discovery.scope_source === "all"),
  );
}

function isTrackedTopicStatus(status: string): status is TrackedTopicStatus {
  return TRACKED_TOPIC_STATUSES.includes(status as TrackedTopicStatus);
}

function readAssetObservation(summary: UdmiValidationSummaryV1 | null): AssetObservation | null {
  const metrics = summary?.asset_metrics;
  if (!metrics || !isMetricCount(metrics.expected) || !isMetricCount(metrics.observed)) {
    return null;
  }
  return { expected: metrics.expected, observed: metrics.observed };
}

function traceCoordinate(index: number, length: number, value: number, scale: number) {
  return {
    x: length === 1 ? 0 : (index / (length - 1)) * 100,
    y: 100 - (value / scale) * 100,
  };
}

function tracePoints(
  samples: readonly AssetObservation[],
  scale: number,
  metric: (sample: AssetObservation) => number,
): string {
  return samples
    .map((sample, index) => {
      const point = traceCoordinate(index, samples.length, metric(sample), scale);
      return `${point.x},${point.y}`;
    })
    .join(" ");
}

function buildTopicStatusCounts(
  discovery: UdmiAssetTopicDiscovery | null,
): Partial<Record<TrackedTopicStatus, number>> | null {
  if (!hasUsableTopicLedger(discovery)) {
    return null;
  }
  const counts: Partial<Record<TrackedTopicStatus, number>> = {};
  const missing = new Set<TrackedTopicStatus>();
  for (const status of TRACKED_TOPIC_STATUSES) {
    const direct = discovery.status_counts[status];
    if (isMetricCount(direct)) {
      counts[status] = direct;
    } else {
      missing.add(status);
    }
  }
  if (missing.size === 0 || discovery.asset_results.length === 0) {
    return counts;
  }
  for (const asset of discovery.asset_results) {
    if (isTrackedTopicStatus(asset.status) && missing.has(asset.status)) {
      counts[asset.status] = (counts[asset.status] ?? 0) + 1;
    }
  }
  return counts;
}

function formatTopicStatus(
  discovery: UdmiAssetTopicDiscovery | null,
  counts: Partial<Record<TrackedTopicStatus, number>> | null,
  status: TrackedTopicStatus,
  expectedAssets: number | null,
): string {
  if (!hasUsableTopicLedger(discovery)) {
    return "Waiting for evidence";
  }
  if (status === "no_matching_asset_id_topic_observed" && !discovery.capture_complete) {
    return "Waiting for capture outcome";
  }
  const count = counts?.[status];
  if (!isMetricCount(count)) {
    return "Waiting for evidence";
  }
  return expectedAssets === null ? `${count} assets` : `${count} / ${expectedAssets} assets`;
}

/** Embedded operator view; evidence points are persisted run-summary snapshots, never message rates. */
export function LiveRunConsole({
  assetTopicDiscovery,
  elapsed,
  issueCount,
  progress,
  run,
  status,
  stage,
  validationSummary,
}: LiveRunConsoleProps) {
  const [now, setNow] = useState(() => Date.now());
  const [stateSamples, setStateSamples] = useState<readonly StateSample[]>([]);
  const [assetSamples, setAssetSamples] = useState<readonly AssetObservation[]>([]);
  const assetObservation = readAssetObservation(validationSummary);
  const assetExpected = assetObservation?.expected;
  const assetObserved = assetObservation?.observed;

  useEffect(() => {
    setStateSamples([]);
    setAssetSamples([]);
  }, [run.run_id]);

  useEffect(() => {
    const capture = () => {
      const next = { at: Date.now(), progress, issues: issueCount, stage };
      setNow(next.at);
      setStateSamples((previous) => {
        const last = previous[previous.length - 1];
        if (last && stateSampleKey(last) === stateSampleKey(next)) {
          return previous;
        }
        return [...previous, next].slice(-MAX_STATE_SAMPLES);
      });
    };
    capture();
    const timer = window.setInterval(capture, 1000);
    return () => window.clearInterval(timer);
  }, [issueCount, progress, stage]);

  useEffect(() => {
    if (assetExpected === undefined || assetObserved === undefined) {
      return;
    }
    setAssetSamples((previous) => {
      const last = previous[previous.length - 1];
      if (last?.expected === assetExpected && last.observed === assetObserved) {
        return previous;
      }
      return [
        ...previous,
        { expected: assetExpected, observed: assetObserved },
      ].slice(-MAX_ASSET_SAMPLES);
    });
  }, [assetExpected, assetObserved, run.run_id]);

  const recentStateChanges = useMemo(() => {
    const changes: StateSample[] = [];
    let previous: StateSample | undefined;
    for (const sample of stateSamples) {
      if (!previous || stateSampleKey(sample) !== stateSampleKey(previous)) {
        changes.push(sample);
      }
      previous = sample;
    }
    return changes.slice(-4).reverse();
  }, [stateSamples]);
  const assetTraceScale = useMemo(
    () => Math.max(1, ...assetSamples.flatMap((sample) => [sample.expected, sample.observed])),
    [assetSamples],
  );
  const observedAssetPoints = useMemo(
    () => tracePoints(assetSamples, assetTraceScale, (sample) => sample.observed),
    [assetSamples, assetTraceScale],
  );
  const expectedAssetPoints = useMemo(
    () => tracePoints(assetSamples, assetTraceScale, (sample) => sample.expected),
    [assetSamples, assetTraceScale],
  );
  const latestAssetSample = assetSamples[assetSamples.length - 1];
  const latestObservedPoint = latestAssetSample
    ? traceCoordinate(
        assetSamples.length - 1,
        assetSamples.length,
        latestAssetSample.observed,
        assetTraceScale,
      )
    : null;
  const expectedAssets = assetObservation?.expected ?? assetTopicDiscovery?.asset_results.length ?? null;
  const topicLedgerUsable = hasUsableTopicLedger(assetTopicDiscovery);
  const topicStatusCounts = useMemo(
    () => buildTopicStatusCounts(assetTopicDiscovery),
    [assetTopicDiscovery],
  );
  const topicStatus = {
    expected: formatTopicStatus(
      assetTopicDiscovery,
      topicStatusCounts,
      "expected_topic_observed",
      expectedAssets,
    ),
    alternate: formatTopicStatus(
      assetTopicDiscovery,
      topicStatusCounts,
      "alternate_topic_observed",
      expectedAssets,
    ),
    noMatch: formatTopicStatus(
      assetTopicDiscovery,
      topicStatusCounts,
      "no_matching_asset_id_topic_observed",
      expectedAssets,
    ),
  };

  return (
    <section className="live-run-console" aria-label="Live run console">
      <div className="live-console-heading">
        <div>
          <span className="eyebrow">Live signal</span>
          <h3>Run console</h3>
          <p>Evidence snapshots refresh independently of the display heartbeat.</p>
        </div>
        <div className="live-console-status">
          <span aria-hidden="true" className="live-console-pulse" />
          <strong className={`status-token ${toHealthState(status)}`}>{status}</strong>
        </div>
      </div>
      <div className="live-console-kpis">
        <div>
          <span>Elapsed</span>
          <strong>{elapsed}</strong>
        </div>
        <div>
          <span>Progress</span>
          <strong>{progress}%</strong>
        </div>
        <div>
          <span>Open issues</span>
          <strong>{issueCount}</strong>
        </div>
        <div>
          <span>Last run update</span>
          <strong>{formatRelativeTime(run.updated_at, now)}</strong>
        </div>
      </div>
      <div className="live-console-grid">
        <article className="live-console-chart">
          <div className="live-console-label">
            <span>Registered assets observed</span>
            <small>
              {assetSamples.length
                ? `${assetSamples.length} recent evidence snapshot${assetSamples.length === 1 ? "" : "s"}`
                : "Waiting for evidence"}
            </small>
          </div>
          {assetObservation === null ? (
            <p className="live-console-empty">Waiting for evidence.</p>
          ) : assetObservation.expected === 0 ? (
            <p className="live-console-empty">No expected assets were recorded.</p>
          ) : (
            <>
              <div className="live-console-evidence-count">
                <strong>
                  {assetObservation.observed} of {assetObservation.expected} expected
                </strong>
                <span>observed registered assets</span>
              </div>
              <svg
                aria-label={`Registered assets observed: ${assetObservation.observed} of ${assetObservation.expected} expected.`}
                role="img"
                viewBox="0 0 100 100"
                preserveAspectRatio="none"
              >
                <line x1="0" x2="100" y1="0" y2="0" />
                <line x1="0" x2="100" y1="50" y2="50" />
                <line x1="0" x2="100" y1="100" y2="100" />
                <polyline className="live-console-expected-trace" fill="none" points={expectedAssetPoints} />
                <polyline className="live-console-observed-trace" fill="none" points={observedAssetPoints} />
                {latestObservedPoint && (
                  <circle
                    className="live-console-observed-point"
                    cx={latestObservedPoint.x}
                    cy={latestObservedPoint.y}
                    r="2.5"
                  />
                )}
              </svg>
              <small>Recent evidence snapshots, not a broker message rate.</small>
            </>
          )}
        </article>
        <article className="live-console-events" aria-live="polite">
          <div className="live-console-label">
            <span>UI/run-state sampling every second</span>
            <small>{humanizeStage(stage) || "Waiting"}</small>
          </div>
          <p className="live-console-heartbeat-note">
            This display heartbeat is not broker traffic or a message rate.
          </p>
          {recentStateChanges.length ? (
            <ol>
              {recentStateChanges.map((sample) => (
                <li key={`${sample.at}-${stateSampleKey(sample)}`}>
                  <time dateTime={new Date(sample.at).toISOString()}>
                    {new Date(sample.at).toLocaleTimeString()}
                  </time>
                  <span>
                    {humanizeStage(sample.stage) || "Run state"} · {sample.progress}% ·{" "}
                    {sample.issues} issue{sample.issues === 1 ? "" : "s"}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="live-console-empty">Waiting for the first run state.</p>
          )}
        </article>
      </div>
      <section className="live-console-topic-evidence" aria-label="Topic observation breakdown">
        <div className="live-console-label">
          <span>Topic observation breakdown</span>
          <small>
            {topicLedgerUsable && assetTopicDiscovery?.capture_complete
              ? "Capture outcome recorded"
              : "Topic evidence pending"}
          </small>
        </div>
        <dl className="live-console-topic-statuses">
          <div className="topic-status-expected">
            <dt>Expected-topic observed</dt>
            <dd>{topicStatus.expected}</dd>
          </div>
          <div className="topic-status-alternate">
            <dt>Alternate-topic observed</dt>
            <dd>{topicStatus.alternate}</dd>
          </div>
          <div className="topic-status-no-match">
            <dt>No matching asset-ID topic observed</dt>
            <dd>{topicStatus.noMatch}</dd>
          </div>
        </dl>
        <p className="live-console-topic-note">
          Topic placement comes only from the topic-discovery ledger; it is never inferred from
          payload observation.
        </p>
      </section>
    </section>
  );
}
