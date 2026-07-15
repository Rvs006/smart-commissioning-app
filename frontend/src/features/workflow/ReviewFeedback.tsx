import { FormEvent, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";

type ReviewComment = {
  app: string;
  build: string;
  comment: string;
  createdAt: string;
  id: string;
  module: string;
  priority: string;
  reviewer: string;
  route: string;
  title: string;
  type: string;
  viewport: string;
};

const storageKey = "smartCommissioningEngineerReviewCommentsV1";
const reviewerKey = "smartCommissioningEngineerReviewerName";

const moduleLabels: Record<string, string> = {
  "/": "Homepage",
  "/bacnet-discovery": "BACnet Discovery",
  "/configuration": "Configuration",
  "/data-validation": "Validation",
  "/ip-scanner": "IP Discovery",
  "/mqtt-discovery": "MQTT Discovery",
  "/reports": "Reports",
  "/udmi-validation": "UDMI",
};

const modules = [
  "Homepage",
  "Configuration",
  "IP Discovery",
  "BACnet Discovery",
  "MQTT Discovery",
  "UDMI",
  "Validation",
  "Reports",
];

const commentTypes = ["Issue", "Question", "UX comment", "Missing requirement", "Positive note"];
const priorities = ["Medium", "High", "Low"];

export function ReviewFeedback() {
  const location = useLocation();
  const currentModule = useMemo(
    () => moduleLabels[location.pathname] ?? "Current page",
    [location.pathname],
  );
  const [isOpen, setIsOpen] = useState(false);
  const [comments, setComments] = useState<ReviewComment[]>(() => loadComments());
  const [reviewer, setReviewer] = useState(() => localStorage.getItem(reviewerKey) ?? "");
  const [module, setModule] = useState(currentModule);
  const [type, setType] = useState(commentTypes[0]);
  const [priority, setPriority] = useState(priorities[0]);
  const [title, setTitle] = useState("");
  const [comment, setComment] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    setModule(currentModule);
  }, [currentModule]);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(comments));
  }, [comments]);

  useEffect(() => {
    localStorage.setItem(reviewerKey, reviewer);
  }, [reviewer]);

  useEffect(() => {
    if (!status) {
      return;
    }
    const timer = window.setTimeout(() => setStatus(""), 3500);
    return () => window.clearTimeout(timer);
  }, [status]);

  const addComment = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cleanedComment = comment.trim();
    if (!cleanedComment) {
      setStatus("Add a comment before saving.");
      return;
    }

    const cleanedTitle = title.trim() || cleanedComment.slice(0, 80);
    setComments((current) => [
      ...current,
      {
        app: "Smart Commissioning App",
        build: "local-dry-run",
        comment: cleanedComment,
        createdAt: new Date().toISOString(),
        id: `review-${Date.now().toString(36)}`,
        module,
        priority,
        reviewer: reviewer.trim(),
        route: location.pathname,
        title: cleanedTitle,
        type,
        viewport: `${window.innerWidth}x${window.innerHeight}`,
      },
    ]);
    setTitle("");
    setComment("");
    setStatus("Comment saved locally.");
  };

  const removeComment = (id: string) => {
    setComments((current) => current.filter((item) => item.id !== id));
    setStatus("Comment removed.");
  };

  const exportJson = () => {
    download(
      "smart-commissioning-review-comments.json",
      "application/json",
      JSON.stringify(feedbackPayload(comments), null, 2),
    );
    setStatus("JSON feedback exported.");
  };

  const exportCsv = () => {
    download("smart-commissioning-review-comments.csv", "text/csv", asCsv(comments));
    setStatus("CSV feedback exported.");
  };

  const copySummary = async () => {
    const payload = JSON.stringify(feedbackPayload(comments), null, 2);
    try {
      await navigator.clipboard.writeText(payload);
      setStatus("Feedback copied to clipboard.");
    } catch {
      download("smart-commissioning-review-comments.json", "application/json", payload);
      setStatus("Clipboard blocked. JSON exported instead.");
    }
  };

  const clearComments = () => {
    if (!comments.length) {
      setStatus("There are no comments to clear.");
      return;
    }
    if (window.confirm("Clear all locally saved review comments?")) {
      setComments([]);
      setStatus("All comments cleared.");
    }
  };

  return (
    <aside className="review-feedback" aria-label="Engineer review comments">
      <section className="review-feedback-panel" hidden={!isOpen}>
        <div className="review-feedback-heading">
          <div>
            <h2>Engineer Review Comments</h2>
            <p>
              Capture UI, workflow, copy, and validation comments while testing. Export the file
              when the dry run is finished.
            </p>
          </div>
          <button
            aria-label="Close review comments"
            className="review-feedback-close"
            onClick={() => setIsOpen(false)}
            type="button"
          >
            x
          </button>
        </div>

        <form className="review-feedback-form" onSubmit={addComment}>
          <div className="review-feedback-row">
            <label>
              Reviewer name
              <input
                autoComplete="name"
                onChange={(event) => setReviewer(event.target.value)}
                placeholder="Your name"
                value={reviewer}
              />
            </label>
            <label>
              Module
              <select onChange={(event) => setModule(event.target.value)} value={module}>
                {modules.map((entry) => (
                  <option key={entry}>{entry}</option>
                ))}
              </select>
            </label>
          </div>

          <div className="review-feedback-row">
            <label>
              Comment type
              <select onChange={(event) => setType(event.target.value)} value={type}>
                {commentTypes.map((entry) => (
                  <option key={entry}>{entry}</option>
                ))}
              </select>
            </label>
            <label>
              Priority
              <select onChange={(event) => setPriority(event.target.value)} value={priority}>
                {priorities.map((entry) => (
                  <option key={entry}>{entry}</option>
                ))}
              </select>
            </label>
          </div>

          <label>
            Short title
            <input
              maxLength={120}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Example: MQTT publish button needs clearer state"
              value={title}
            />
          </label>

          <label>
            Comment
            <textarea
              onChange={(event) => setComment(event.target.value)}
              placeholder="What should be changed, clarified, or checked?"
              value={comment}
            />
          </label>

          <div className="review-feedback-actions">
            <button className="primary" type="submit">
              Add comment
            </button>
            <button onClick={exportJson} type="button">
              Export JSON
            </button>
            <button onClick={exportCsv} type="button">
              Export CSV
            </button>
            <button onClick={copySummary} type="button">
              Copy summary
            </button>
            <button onClick={clearComments} type="button">
              Clear comments
            </button>
          </div>

          <div className="review-feedback-status" aria-live="polite">
            {status}
          </div>
        </form>

        <div className="review-feedback-list">
          {comments.length === 0 ? (
            <div className="review-feedback-empty">
              No comments yet. Add findings as engineers move through the app.
            </div>
          ) : (
            [...comments].reverse().map((item) => (
              <article className="review-feedback-item" key={item.id}>
                <div className="review-feedback-meta">
                  <span>{item.module}</span>
                  <span>{item.type}</span>
                  <span>{item.priority}</span>
                </div>
                <strong>{item.title}</strong>
                <p>{item.comment}</p>
                <div className="review-feedback-meta">
                  <span>{item.reviewer || "Unnamed reviewer"}</span>
                  <span>{new Date(item.createdAt).toLocaleString()}</span>
                </div>
                <button
                  className="review-feedback-remove"
                  onClick={() => removeComment(item.id)}
                  type="button"
                >
                  Remove
                </button>
              </article>
            ))
          )}
        </div>
      </section>

      <button
        aria-controls="review-feedback-panel"
        aria-expanded={isOpen}
        className="review-feedback-toggle"
        onClick={() => setIsOpen((current) => !current)}
        type="button"
      >
        Review Comments
        <span className="review-feedback-count">{comments.length}</span>
      </button>
    </aside>
  );
}

function loadComments(): ReviewComment[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) ?? "[]") as unknown;
    return Array.isArray(parsed) ? (parsed as ReviewComment[]) : [];
  } catch {
    return [];
  }
}

function feedbackPayload(comments: ReviewComment[]) {
  return {
    app: "Smart Commissioning App",
    build: "local-dry-run",
    comments,
    exportedAt: new Date().toISOString(),
  };
}

function csvEscape(value: unknown): string {
  return `"${String(value ?? "").replace(/"/g, '""')}"`;
}

function asCsv(comments: ReviewComment[]): string {
  const headers: Array<keyof ReviewComment> = [
    "id",
    "createdAt",
    "reviewer",
    "module",
    "type",
    "priority",
    "route",
    "title",
    "comment",
    "viewport",
  ];
  const rows = comments.map((item) => headers.map((key) => csvEscape(item[key])).join(","));
  return `${headers.join(",")}\n${rows.join("\n")}`;
}

function download(filename: string, mime: string, contents: string) {
  const blob = new Blob([contents], { type: mime });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  window.setTimeout(() => {
    URL.revokeObjectURL(link.href);
    link.remove();
  }, 0);
}
