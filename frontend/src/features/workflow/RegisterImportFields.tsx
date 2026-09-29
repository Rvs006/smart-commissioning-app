import type { ChangeEvent } from "react";
import {
  getImportTemplatePath,
  type ImportBatchSummary,
  type ImportErrorReport,
  type ImportProfileSummary,
  type ImportType,
} from "../../api/client";
import { ENGINEER_REQUIRED_TOOLTIP } from "../../app/sessionContext";
import type { FileDownloadController } from "./fileDownload";
import { formatRelativeTime } from "./runFormat";

/**
 * A large register can reject hundreds of rows. Render the first N and state the
 * honest remainder count rather than building pagination for a pre-1.0 fix:
 * fixing the listed rows and re-uploading surfaces the rest.
 */
export const IMPORT_ERROR_DISPLAY_CAP = 50;

type ImportErrorsQuery = {
  data?: ImportErrorReport;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
};

export type RegisterImportFieldsProps = {
  canEngineer: boolean;
  /** The import type the upload posts. "" disables the upload button. */
  importType: ImportType | "";
  selectedFile: File | null;
  onFileSelected: (file: File | null) => void;
  onUpload: () => void;
  uploading: boolean;
  uploadError: Error | null;
  importOutcome: ImportBatchSummary | null;
  importErrorsQuery: ImportErrorsQuery;
  /** The server's record of the register already on file, if any. */
  latestImport: ImportBatchSummary | null | undefined;
  selectedProfile: ImportProfileSummary | null | undefined;
  templateDownload: FileDownloadController;
  /**
   * Rebuild-the-register-CSV affordance. Only the scan-register lanes have one;
   * `path` is null when the register on file was uploaded rather than saved from
   * a scan, which hides the button but still surfaces a download failure.
   */
  registerCsv?: {
    download: FileDownloadController;
    fallbackFilename: string;
    path: string | null;
  };
};

/**
 * The register import card's body, from the file picker down: shared by
 * ModulePage (built-in discovery lanes, UDMI, data validation) and the native
 * scanner pages so one register import cannot drift from the other.
 *
 * Deliberately NOT included: the wrapping card, the heading, and ModulePage's
 * import-profile <select>. ModulePage offers a profile choice because its
 * modules carry several import types; a scanner lane has exactly one and names
 * it in the card head, so a permanently single-option select there would invite
 * a click that can never do anything. Each caller renders its own wrapper (and,
 * for ModulePage, the select) above these fields.
 */
export function RegisterImportFields({
  canEngineer,
  importType,
  selectedFile,
  onFileSelected,
  onUpload,
  uploading,
  uploadError,
  importOutcome,
  importErrorsQuery,
  latestImport,
  selectedProfile,
  templateDownload,
  registerCsv,
}: RegisterImportFieldsProps) {
  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    onFileSelected(event.target.files?.[0] ?? null);
    // Chromium fires no change event when the same path is re-picked while the
    // input still holds it, so a corrected CSV saved over the original was
    // silently never re-read (the field engineer had to rename the file to get it
    // uploaded). Clearing the value makes every pick deliver a fresh File
    // snapshot. The File captured into state above stays valid for the upload,
    // and the staged name is rendered from state since the native input now
    // always reads "No file chosen".
    event.target.value = "";
  };

  // Rejection reasons for the red panel. When the summary already names the
  // missing columns on its own line, the per-column missing_required_column
  // records (import_service.py:698-706) would repeat it verbatim as bullets —
  // drop them there only, so the reasons stay complete but nothing is said twice.
  const importErrors = (importErrorsQuery.data?.errors ?? []).filter(
    (error) =>
      error.code !== "missing_required_column" ||
      (importOutcome?.missing_columns.length ?? 0) === 0,
  );
  const visibleImportErrors = importErrors.slice(0, IMPORT_ERROR_DISPLAY_CAP);
  const hiddenImportErrorCount = Math.max(importErrors.length - IMPORT_ERROR_DISPLAY_CAP, 0);
  // Import warnings are informational (their rows stay accepted), so they get
  // their own amber panel below the outcome — never the red error styling.
  const importWarnings = importOutcome?.warnings ?? [];

  return (
    <>
      <label>
        CSV or XLSX file
        <input accept=".csv,.xlsx" onChange={handleFileChange} type="file" />
      </label>
      {/* handleFileChange clears the input's value, so the native control always
          reads "No file chosen" — the staged file is named here from state. */}
      {selectedFile && <p className="field-note">Selected: {selectedFile.name}</p>}
      {/* When nothing is staged in this session, surface the server's own record
          of the last import so the empty file input does not imply nothing was
          ever uploaded (ISSUE-5). Only ever shown on a real hit — a 404/error
          leaves data undefined. */}
      {!selectedFile && latestImport && (
        <div className="state-panel success import-on-file">
          <strong>Register already imported</strong>
          <span>
            {latestImport.file_name} — {latestImport.accepted_rows} of {latestImport.total_rows}{" "}
            rows accepted, {formatRelativeTime(latestImport.created_at)}. This register is stored
            and used by runs on this page; upload again only if the file changed.
          </span>
          {/* A register saved from a scan has no file the operator ever held; the
              run it came from can still rebuild the same CSV. */}
          {registerCsv?.path && (
            <button
              className="secondary-button compact"
              disabled={registerCsv.download.pendingKey !== null}
              onClick={() => {
                void registerCsv.download.download({
                  fallbackFilename: registerCsv.fallbackFilename,
                  key: "latest-register-csv",
                  path: registerCsv.path as string,
                });
              }}
              type="button"
            >
              {registerCsv.download.pendingKey === "latest-register-csv"
                ? "Downloading..."
                : "Download register CSV"}
            </button>
          )}
          {/* The link is offered on a file-name match, so a 404 here is the honest
              answer that the guess was wrong, not a fault. */}
          {registerCsv?.download.error && (
            <span className="field-note" role="alert">
              {registerCsv.download.errorStatus === 404
                ? "This register was uploaded, so there is no scan behind it to rebuild the CSV from. Use your own copy of the file."
                : `Register CSV download failed: ${registerCsv.download.error}`}
            </span>
          )}
        </div>
      )}

      <button
        className="primary-button"
        disabled={!selectedFile || !importType || uploading || !canEngineer}
        onClick={onUpload}
        title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
        type="button"
      >
        {uploading ? "Validating..." : "Upload and validate"}
      </button>

      {importType && (
        <div className="schema-card template-card">
          <div>
            <strong>Default import template</strong>
            <p>
              Use this format as the normal project template. It includes the required columns and
              one realistic example row.
            </p>
          </div>
          <div className="inline-actions">
            <button
              className="secondary-button compact"
              disabled={templateDownload.pendingKey !== null}
              onClick={() =>
                void templateDownload.download({
                  fallbackFilename: `${importType}_template.xlsx`,
                  key: "template-xlsx",
                  path: getImportTemplatePath(importType, "xlsx"),
                })
              }
              type="button"
            >
              {templateDownload.pendingKey === "template-xlsx" ? "Downloading..." : "Download XLSX"}
            </button>
            <button
              className="secondary-button compact"
              disabled={templateDownload.pendingKey !== null}
              onClick={() =>
                void templateDownload.download({
                  fallbackFilename: `${importType}_template.csv`,
                  key: "template-csv",
                  path: getImportTemplatePath(importType, "csv"),
                })
              }
              type="button"
            >
              {templateDownload.pendingKey === "template-csv" ? "Downloading..." : "Download CSV"}
            </button>
          </div>
        </div>
      )}

      {templateDownload.error && (
        <div className="state-panel error">
          <strong>Template download failed</strong>
          <span>{templateDownload.error}</span>
        </div>
      )}

      {selectedProfile && (
        <div className="schema-card">
          <strong>Required columns</strong>
          <div className="tag-cloud">
            {selectedProfile.required_columns.slice(0, 8).map((column) => (
              <span key={column}>{column}</span>
            ))}
          </div>
          {(selectedProfile.optional_columns ?? []).length > 0 && (
            <>
              <strong>Optional columns</strong>
              <div className="tag-cloud">
                {(selectedProfile.optional_columns ?? []).slice(0, 8).map((column) => (
                  <span className="optional" key={column}>
                    {column}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {uploadError && (
        <div className="state-panel error">
          <strong>Import failed</strong>
          <span>{uploadError.message}</span>
        </div>
      )}

      {importOutcome && (
        <div className={`state-panel ${importOutcome.status}`}>
          <strong>{importOutcome.status.toUpperCase()}</strong>
          <span>
            {importOutcome.accepted_rows} accepted · {importOutcome.rejected_rows} rejected
          </span>
        </div>
      )}

      {importOutcome && importOutcome.status !== "accepted" && (
        <div className="state-panel error import-errors">
          <strong>
            {importOutcome.status === "rejected"
              ? "Import rejected — reasons below"
              : `${importOutcome.rejected_rows} of ${importOutcome.total_rows} rows rejected — reasons below`}
          </strong>
          {importOutcome.missing_columns.length > 0 && (
            <span>Missing required columns: {importOutcome.missing_columns.join(", ")}</span>
          )}
          {importErrorsQuery.isLoading && <span>Loading rejection reasons...</span>}
          {/* Never let a failed fetch look like "no reasons": say so. */}
          {importErrorsQuery.isError && (
            <span>Could not load rejection reasons: {importErrorsQuery.error?.message}</span>
          )}
          {visibleImportErrors.length > 0 && (
            <ul>
              {visibleImportErrors.map((error, index) => (
                <li key={`${error.row_number ?? "file"}-${error.field ?? ""}-${index}`}>
                  {error.row_number != null ? `Row ${error.row_number} — ` : ""}
                  {error.field ? `${error.field}: ` : ""}
                  {error.message} ({error.code})
                </li>
              ))}
            </ul>
          )}
          {hiddenImportErrorCount > 0 && (
            <span>
              ...and {hiddenImportErrorCount} more rejected rows not shown — fix the rows listed
              above and re-upload to see the rest.
            </span>
          )}
        </div>
      )}

      {importWarnings.length > 0 && (
        <div className="state-panel warning">
          <strong>{importWarnings.length} warning(s) — affected rows are still accepted</strong>
          <ul>
            {importWarnings.map((warning, index) => (
              <li key={`${warning.row_number ?? "file"}-${warning.field ?? ""}-${index}`}>
                {warning.row_number != null ? `Row ${warning.row_number}: ` : ""}
                {warning.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
