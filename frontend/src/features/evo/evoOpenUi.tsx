import { createLibrary, defineComponent, Renderer } from "@openuidev/react-lang";
import { z } from "zod/v4";
import type { EvoExperiment, EvoOverview } from "../../api/client";

const ExperimentExplanation = defineComponent({
  name: "ExperimentExplanation",
  description: "Explains one Evo experiment in plain commissioning language.",
  props: z.object({
    title: z.string(),
    summary: z.string(),
    evidence: z.string(),
    nextStep: z.string(),
  }),
  component: ({ props }) => (
    <article className="evo-explanation" aria-label="OpenUI experiment explanation">
      <p className="evo-explanation-source">Rendered with OpenUI</p>
      <h2>{props.title}</h2>
      <p>{props.summary}</p>
      <dl>
        <div><dt>Evidence</dt><dd>{props.evidence}</dd></div>
        <div><dt>Decision</dt><dd>{props.nextStep}</dd></div>
      </dl>
    </article>
  ),
});

const evoExplanationLibrary = createLibrary({
  root: "ExperimentExplanation",
  components: [ExperimentExplanation],
});

function q(value: string): string {
  return JSON.stringify(value);
}

function explanation(over: EvoOverview, experiment: EvoExperiment | null): string {
  if (!experiment) {
    return `root = ExperimentExplanation(${q("Baseline preparation")}, ${q("Evo has not scored the UDMI validator yet. The protected v0.1.40 commit remains the reference point.")}, ${q("The benchmark, held-out cases, and regression gates must pass before optimization starts.")}, ${q("Create and verify exp_0000, then record its correctness, duration, and memory.")})`;
  }
  const correctness = experiment.correctness == null ? "not recorded" : `${(experiment.correctness * 100).toFixed(1)}%`;
  const duration = experiment.duration_seconds == null ? "not recorded" : `${experiment.duration_seconds.toFixed(2)} seconds`;
  const summary = experiment.change || "Baseline implementation, with no production logic change.";
  const evidence = `Correctness ${correctness}; duration ${duration}. ${experiment.finding || "No additional finding recorded."}`;
  const decision = experiment.status === "committed" ? "Keep as evidence and compare it with the next isolated candidate." : "Wait for the run and all gates to finish before drawing a conclusion.";
  return `root = ExperimentExplanation(${q(experiment.id)}, ${q(summary)}, ${q(evidence)}, ${q(decision)})`;
}

export function EvoOpenUiExplanation({ overview, experiment }: { overview: EvoOverview; experiment: EvoExperiment | null }) {
  return <Renderer response={explanation(overview, experiment)} library={evoExplanationLibrary} />;
}
