import { useEffect, useMemo, useState } from "react";
import type { RunRecord } from "../../api/client";
import { formatRelativeTime, humanizeStage, toHealthState } from "./runFormat";

type Sample = Readonly<{ at: number; progress: number; issues: number; stage: string }>;

type LiveRunConsoleProps = Readonly<{
  elapsed: string;
  issueCount: number;
  progress: number;
  run: RunRecord;
  status: RunRecord["status"];
  stage: string;
}>;

const MAX_SAMPLES = 60;

function sampleKey(sample: Sample): string {
  return `${sample.progress}:${sample.issues}:${sample.stage}`;
}

/** Embedded operator view; heartbeat samples real SSE/poll state, not broker messages. */
export function LiveRunConsole({
  elapsed,
  issueCount,
  progress,
  run,
  status,
  stage,
}: LiveRunConsoleProps) {
  const [now, setNow] = useState(() => Date.now());
  const [samples, setSamples] = useState<readonly Sample[]>([]);

  useEffect(() => setSamples([]), [run.run_id]);
  useEffect(() => {
    const capture = () => {
      const next = { at: Date.now(), progress, issues: issueCount, stage };
      setNow(next.at);
      setSamples((previous) => [...previous, next].slice(-MAX_SAMPLES));
    };
    capture();
    const timer = window.setInterval(capture, 1000);
    return () => window.clearInterval(timer);
  }, [issueCount, progress, stage]);

  const progressPoints = useMemo(
    () =>
      samples
        .map((sample, index) => {
          const x = samples.length === 1 ? 0 : (index / (samples.length - 1)) * 100;
          return `${x},${100 - sample.progress}`;
        })
        .join(" "),
    [samples],
  );
  const recentChanges = useMemo(() => {
    const changes: Sample[] = [];
    let previous: Sample | undefined;
    for (const sample of samples) {
      if (!previous || sampleKey(sample) !== sampleKey(previous)) changes.push(sample);
      previous = sample;
    }
    return changes.slice(-4).reverse();
  }, [samples]);

  return (
    <section className="live-run-console" aria-label="Live run console">
      <div className="live-console-heading">
        <div>
          <span className="eyebrow">Live signal</span>
          <h3>Run console</h3>
          <p>Stream active · display heartbeat every second · evidence refreshes independently.</p>
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
            <span>Progress trace</span>
            <small>Last 60 seconds</small>
          </div>
          <svg
            aria-label="Run progress sampled every second"
            role="img"
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
          >
            <line x1="0" x2="100" y1="0" y2="0" />
            <line x1="0" x2="100" y1="50" y2="50" />
            <line x1="0" x2="100" y1="100" y2="100" />
            <polyline fill="none" points={progressPoints} />
          </svg>
          <small>Trace samples the actual run state; it is not a payload-rate chart.</small>
        </article>
        <article className="live-console-events" aria-live="polite">
          <div className="live-console-label">
            <span>Signal changes</span>
            <small>{humanizeStage(stage) || "Waiting"}</small>
          </div>
          {recentChanges.length ? (
            <ol>
              {recentChanges.map((sample) => (
                <li key={`${sample.at}-${sampleKey(sample)}`}>
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
    </section>
  );
}
