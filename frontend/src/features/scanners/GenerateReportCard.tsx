import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  createReport,
  type ReportFormat,
  type ReportSummary,
  type ReportType,
} from "../../api/client";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { ENGINEER_REQUIRED_TOOLTIP } from "../../app/sessionContext";
import type { RunEpochOwner, ScannerRunController } from "./useScannerRun";

const ALL_FORMATS = ["pdf", "docx", "xlsx", "zip"] as const satisfies readonly ReportFormat[];
type FormatSelection = ReportFormat | "all";

/**
 * "Generate report from this run", carried over from the v0.1.58 module page so
 * a finished scan can still be turned into a titled, run-scoped report without
 * leaving the page. The report is scoped through source_run_ids, exactly as
 * before, so it traces back to the run that produced it.
 *
 * Simplification against ModulePage: the title is collected inline instead of in
 * a modal dialog, and the UDMI variant / filtered-scope freeze are absent —
 * neither applies to a scanner run.
 */
export function GenerateReportCard({ run }: { run: ScannerRunController }) {
  const {
    activeRun,
    activeRunAuthoritativelyTerminal,
    activeRunOwner,
    apiClient,
    canEngineer,
    lane,
    ownsActiveRun,
    runAccessClosed,
    sessionScopeId,
    workspaceRef,
  } = run;
  const queryClient = useQueryClient();
  const [format, setFormat] = useState<FormatSelection>("pdf");
  const [title, setTitle] = useState("");
  const [toast, setToast] = useState<{ text: string; warning: boolean } | null>(null);

  const reportType: ReportType = lane === "bacnet" ? "bacnet_discovery" : "ip_discovery";

  const mutation = useMutation({
    mutationKey: mutationKeys.reports(sessionScopeId, workspaceRef),
    mutationFn: async ({
      owner,
      runId,
      reportTitle,
    }: {
      owner: RunEpochOwner;
      runId: string;
      reportTitle: string;
    }) => {
      const formats: readonly ReportFormat[] = format === "all" ? ALL_FORMATS : [format];
      const reports: ReportSummary[] = [];
      const failedFormats: ReportFormat[] = [];
      let firstFailure: unknown;
      for (const entry of formats) {
        // Generate-All is a loop of requests; if the operator starts another run
        // partway through, the remaining formats belong to a run that is no
        // longer on screen. Stop and say nothing rather than report success.
        if (!ownsActiveRun(owner)) {
          return { failedFormats, ownerLost: true, reports, requestedCount: formats.length };
        }
        try {
          reports.push(
            await createReport({
              context: { client: apiClient },
              format: entry,
              reportTitle,
              reportType,
              sourceRunIds: [runId],
              workspace: workspaceRef,
            }),
          );
        } catch (error) {
          firstFailure ??= error;
          failedFormats.push(entry);
        }
      }
      if (reports.length === 0) {
        throw firstFailure instanceof Error ? firstFailure : new Error("Report generation failed.");
      }
      return { failedFormats, ownerLost: false, reports, requestedCount: formats.length };
    },
    onSuccess: ({ failedFormats, ownerLost, reports, requestedCount }, { owner }) => {
      if (ownerLost || !ownsActiveRun(owner)) {
        return;
      }
      setToast(
        failedFormats.length > 0
          ? {
              warning: true,
              text: `${reports.length} of ${requestedCount} reports were generated. Failed formats: ${failedFormats
                .map((entry) => entry.toUpperCase())
                .join(", ")}. The completed reports are in the Reports tab.`,
            }
          : {
              warning: false,
              text:
                reports.length === 1
                  ? `Report generated from this run. Report ID: ${reports[0].report_id}.`
                  : `${reports.length} reports generated from this run. See the Reports tab.`,
            },
      );
      void queryClient.invalidateQueries({
        queryKey: queryKeys.reports(sessionScopeId, workspaceRef),
      });
    },
  });

  // A report describes ONE run. Carrying its confirmation across to the next run
  // would show run A report id under run B heading the moment B went terminal,
  // and the same is true across a workspace switch. Reset on the run identity
  // (id AND epoch, so a re-run of the same id still clears) and on the workspace.
  const mutationReset = mutation.reset;
  useEffect(() => {
    setToast(null);
    mutationReset();
  }, [
    activeRun?.epoch,
    activeRun?.runId,
    mutationReset,
    sessionScopeId,
    workspaceRef.projectId,
    workspaceRef.siteId,
  ]);

  if (!activeRun || !activeRunAuthoritativelyTerminal || !canEngineer || runAccessClosed) {
    return null;
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = title.trim();
    if (!trimmed || trimmed.length > 160 || mutation.isPending) {
      return;
    }
    if (!activeRunOwner) {
      return;
    }
    setToast(null);
    mutation.mutate({ owner: activeRunOwner, reportTitle: trimmed, runId: activeRun.runId });
  };

  return (
    <section className="scanner-card" aria-labelledby="scanner-report-heading">
      <div className="scanner-card-head">
        <h2 id="scanner-report-heading">Generate report</h2>
        <Link className="scanner-card-link" to="/reports">
          Open Reports <span aria-hidden="true">→</span>
        </Link>
      </div>
      <form className="scanner-card-body scanner-report-form" onSubmit={handleSubmit}>
        <label>
          Report title
          <input
            maxLength={160}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Name this report for the Reports list"
            required
            value={title}
          />
        </label>
        <label>
          Report format
          <select
            aria-label="Report format"
            onChange={(event) => setFormat(event.target.value as FormatSelection)}
            value={format}
          >
            <option value="pdf">PDF (.pdf)</option>
            <option value="docx">Word (.docx)</option>
            <option value="xlsx">Excel (.xlsx)</option>
            <option value="zip">Evidence pack (.zip)</option>
            <option value="all">Generate All</option>
          </select>
        </label>
        <button
          className="secondary-button compact"
          disabled={mutation.isPending || title.trim().length === 0}
          title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
          type="submit"
        >
          {mutation.isPending ? "Generating..." : "Generate report from this run"}
        </button>
      </form>
      {toast && (
        <div className={`state-panel ${toast.warning ? "warning" : "success"}`} role="status">
          <strong>{toast.warning ? "Report generation incomplete" : "Report generated"}</strong>
          <span>{toast.text}</span>
        </div>
      )}
      {mutation.isError && (
        <div className="state-panel error" role="alert">
          <strong>Report generation failed</strong>
          <span>{mutation.error.message}</span>
        </div>
      )}
    </section>
  );
}
