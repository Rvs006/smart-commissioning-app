import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, downloadFile, type SessionBoundApiClient } from "../../api/client";

/**
 * Drives an authenticated file download. Plain `<a download href>` anchors
 * navigate outside fetch(), so they cannot carry the X-API-Key header and 401 in
 * hosted deployments; this routes downloads through downloadFile().
 *
 * One in-flight download per hook instance (a second call aborts the first), and
 * the whole thing aborts on unmount. Shared by ModulePage and the scanner pages
 * so a lazily-loaded scanner route does not import the 11k-line module component
 * just to fetch a file.
 */
export function useFileDownload(apiClient: SessionBoundApiClient) {
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The HTTP status behind `error`, so a caller can tell a real fault from an
  // expected miss (e.g. a 404 on a download path offered on a heuristic) without
  // pattern-matching the server's prose. null when the failure carried no status.
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const generationRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      generationRef.current += 1;
      controllerRef.current?.abort();
      controllerRef.current = null;
    },
    [],
  );

  const download = useCallback(
    async ({
      fallbackFilename,
      init,
      isCurrent = () => true,
      key,
      path,
    }: {
      fallbackFilename: string;
      init?: RequestInit;
      isCurrent?: () => boolean;
      key: string;
      path: string;
    }) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setPendingKey(key);
      setError(null);
      setErrorStatus(null);
      try {
        const { blob, filename } = await downloadFile(path, init, {
          client: apiClient,
          signal: controller.signal,
        });
        if (generation !== generationRef.current || !isCurrent()) {
          return;
        }
        triggerBlobDownload(blob, filename ?? fallbackFilename);
      } catch (cause) {
        if (generation === generationRef.current && !controller.signal.aborted && isCurrent()) {
          setError(cause instanceof Error ? cause.message : "Download failed.");
          setErrorStatus(cause instanceof ApiError ? cause.status : null);
        }
      } finally {
        if (generation === generationRef.current) {
          controllerRef.current = null;
          setPendingKey(null);
        }
      }
    },
    [apiClient],
  );

  const reset = useCallback(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setPendingKey(null);
    setError(null);
    setErrorStatus(null);
  }, []);

  return { download, error, errorStatus, pendingKey, reset };
}

export type FileDownloadController = ReturnType<typeof useFileDownload>;

/** Hand a client-built blob (a CSV assembled in the browser) to the browser. */
export function triggerBlobDownload(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}
