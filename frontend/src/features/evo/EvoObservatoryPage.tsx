import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { getEvoOverview, type EvoExperiment } from "../../api/client";
import { EvoOpenUiExplanation } from "./evoOpenUi";
import "./evoObservatory.css";

function value(value: number | null, suffix = ""): string {
  return value == null ? "Awaiting run" : `${value.toFixed(2)}${suffix}`;
}

function ExperimentRow({ experiment, active, onSelect }: { experiment: EvoExperiment; active: boolean; onSelect: () => void }) {
  return (
    <button className={`evo-run${active ? " active" : ""}`} onClick={onSelect} type="button">
      <span><strong>{experiment.id}</strong><small>{experiment.change || "Protected baseline"}</small></span>
      <span className={`evo-run-state state-${experiment.status}`}>{experiment.status}</span>
      <span className="evo-run-score">{experiment.score == null ? "No score" : experiment.score.toFixed(4)}</span>
    </button>
  );
}

export function EvoObservatoryPage() {
  const query = useQuery({ queryKey: ["evo", "overview"], queryFn: () => getEvoOverview(), refetchInterval: 5000 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const overview = query.data;
  const experiments = useMemo(() => overview ? [overview.baseline, ...overview.experiments].filter((item): item is EvoExperiment => Boolean(item)) : [], [overview]);
  const selected = experiments.find((item) => item.id === (selectedId ?? overview?.selected_experiment_id)) ?? experiments[experiments.length - 1] ?? null;

  if (query.isLoading) return <div className="evo-state" aria-busy="true">Reading the local Evo workspace…</div>;
  if (query.isError || !overview) return <div className="evo-state error"><strong>Evo data is unavailable</strong><span>{query.error instanceof Error ? query.error.message : "The API did not return an experiment snapshot."}</span><button onClick={() => query.refetch()} type="button">Try again</button></div>;

  return (
    <section className="evo-observatory">
      <header className="evo-masthead">
        <div><p className="evo-context">UDMI validation test bench</p><h1>Evo Observatory</h1><p>See what changed, what the benchmark measured, and why a candidate is safe enough to keep.</p></div>
        <div className="evo-protected"><span>Protected reference</span><strong>{overview.protected_release}</strong><code>{overview.protected_commit.slice(0, 12)}</code></div>
      </header>

      <div className="evo-safety" role="status"><strong>Release isolation is active.</strong><span>Experiments branch from the protected commit. No result is merged or published from this screen.</span></div>

      <div className="evo-bench">
        <section className="evo-lineage" aria-labelledby="lineage-title">
          <div className="evo-section-head"><h2 id="lineage-title">Experiment lineage</h2><button onClick={() => query.refetch()} disabled={query.isFetching} type="button">{query.isFetching ? "Refreshing…" : "Refresh data"}</button></div>
          {experiments.length ? <div className="evo-runs">{experiments.map((experiment) => <ExperimentRow key={experiment.id} experiment={experiment} active={selected?.id === experiment.id} onSelect={() => setSelectedId(experiment.id)} />)}</div> : <div className="evo-empty"><strong>No experiment has run yet</strong><span>The first row will appear after Evo verifies and scores exp_0000.</span></div>}
        </section>

        <section className="evo-measurements" aria-labelledby="measure-title">
          <h2 id="measure-title">Measured result</h2>
          <div className="evo-primary-measure"><span>Correct findings</span><strong>{selected?.correctness == null ? "Awaiting baseline" : `${(selected.correctness * 100).toFixed(1)}%`}</strong><small>This must remain exact before speed counts.</small></div>
          <dl className="evo-metrics"><div><dt>Composite score</dt><dd>{value(selected?.score ?? null)}</dd></div><div><dt>Duration</dt><dd>{value(selected?.duration_seconds ?? null, "s")}</dd></div><div><dt>Peak memory</dt><dd>{value(selected?.peak_memory_mb ?? null, " MB")}</dd></div></dl>
          <p className="evo-target"><strong>Target</strong><code>{overview.target}</code></p>
        </section>
      </div>

      <EvoOpenUiExplanation overview={overview} experiment={selected} />
    </section>
  );
}
