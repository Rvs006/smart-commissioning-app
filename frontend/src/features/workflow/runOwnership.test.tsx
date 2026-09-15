import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { initialRunControllerState, type RunControllerAction } from "./runIsolation";
import { useTerminalEvidenceBarrier } from "./runOwnership";

const RUN = { runId: "run-1", epoch: 1 };

const TERMINAL_SYNC = {
  ...initialRunControllerState,
  phase: "terminal-sync" as const,
  runRef: { runId: "run-1" } as never,
  epoch: 1,
};

/**
 * Drives the barrier with callbacks whose identity changes on EVERY render, the
 * way an un-memoised caller would. Before the callbacks were latched in a ref
 * they were effect dependencies, so a re-render mid-flight disposed the running
 * sequence and the re-run then short-circuited on the evidence-sync guard: the
 * barrier never settled and the run hung at "terminal-sync" with no error.
 */
function Harness({
  dispatchRun,
  gate,
  onConfirm,
}: {
  dispatchRun: (action: RunControllerAction) => void;
  gate: Promise<void>;
  onConfirm: () => void;
}) {
  useTerminalEvidenceBarrier({
    activeRun: RUN,
    blocked: false,
    // Fresh closures every render, deliberately.
    confirmEvidence: async () => {
      onConfirm();
    },
    dispatchRun,
    refetchRunStatus: async () => {
      await gate;
      return { data: { run_id: "run-1", status: "succeeded" as const }, error: null, isError: false };
    },
    requirementsFor: () => ["run", "results"] as const,
    runController: TERMINAL_SYNC,
  });
  return null;
}

describe("useTerminalEvidenceBarrier", () => {
  it("settles even when the caller passes new callback identities mid-flight", async () => {
    const dispatchRun = vi.fn();
    const onConfirm = vi.fn();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });

    const { rerender } = render(
      <Harness dispatchRun={dispatchRun} gate={gate} onConfirm={onConfirm} />,
    );

    // The status re-read is still in flight; re-render so every callback gets a
    // new identity, exactly what an un-memoised caller does on any state change.
    rerender(<Harness dispatchRun={dispatchRun} gate={gate} onConfirm={onConfirm} />);
    rerender(<Harness dispatchRun={dispatchRun} gate={gate} onConfirm={onConfirm} />);

    await act(async () => {
      release();
      await gate;
    });

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(dispatchRun).toHaveBeenCalledWith({
      type: "evidence-succeeded",
      runId: "run-1",
      epoch: 1,
      requirements: ["run", "results"],
    });
  });

  it("fails the barrier when the run record names a different run", async () => {
    const dispatchRun = vi.fn();
    function Mismatch() {
      useTerminalEvidenceBarrier({
        activeRun: RUN,
        blocked: false,
        confirmEvidence: async () => {},
        dispatchRun,
        refetchRunStatus: async () => ({
          data: { run_id: "run-2", status: "succeeded" as const },
          error: null,
          isError: false,
        }),
        requirementsFor: () => ["run", "results"] as const,
        runController: TERMINAL_SYNC,
      });
      return null;
    }
    await act(async () => {
      render(<Mismatch />);
    });
    expect(dispatchRun).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "evidence-failed",
        error: "Final run evidence did not match the active run.",
      }),
    );
  });
});
