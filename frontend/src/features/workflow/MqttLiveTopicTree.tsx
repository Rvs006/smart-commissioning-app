import { useEffect, useRef, useState } from "react";
import type { MqttLiveTreeNode } from "../../api/client";

// A nested topic tree built from SCT's existing table/caret/token vocabulary
// (not a copy of the sidecar's own widget). Collapsed children are NOT mounted,
// which also bounds the DOM for a busy broker's 1 Hz snapshots. The activity
// flash is decorative and disabled under prefers-reduced-motion (the msg/s
// column still conveys liveness).
//
// ponytail: toggle rows match the shipped asset-summary rows — only the button is
// interactive and carries aria-expanded; a full roving-tabindex tree can come
// with the focus phase if screen-reader testing asks for it.

const FLASH_MS = 700;

type TreeProps = {
  tree: MqttLiveTreeNode[];
  treeShown: number;
  totalTopics: number;
  lastActivity: { paths: string[]; at: number } | null;
};

function TreeNodeRows({
  node,
  depth,
  expanded,
  onToggle,
  flashing,
}: {
  node: MqttLiveTreeNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  flashing: Set<string>;
}) {
  const hasChildren = Boolean(node.ch && node.ch.length > 0);
  const isOpen = expanded.has(node.p);
  return (
    <>
      <tr className={flashing.has(node.p) ? "mqtt-tree-flash" : undefined}>
        <td style={{ paddingLeft: depth * 16 + 8 }}>
          {hasChildren ? (
            <button
              aria-expanded={isOpen}
              className="asset-summary-toggle"
              onClick={() => onToggle(node.p)}
              type="button"
            >
              <span aria-hidden="true" className="asset-summary-caret">
                {isOpen ? "▾" : "▸"}
              </span>
              <strong>{node.n}</strong>
            </button>
          ) : (
            <strong>{node.n}</strong>
          )}
          {node.a ? <span className="results-filter-count"> {node.a}</span> : null}
        </td>
        <td>{node.t}</td>
        <td>{node.m}</td>
        <td>{node.r}</td>
        <td>{node.sc ?? "—"}</td>
        <td>
          {node.mt ? <span className="status-token ready">matched</span> : null}
          {node.ret ? <span> retained</span> : null}
          {node.iss ? (
            <span className="error-text">
              {" "}
              {node.iss} issue{node.iss === 1 ? "" : "s"}
            </span>
          ) : null}
        </td>
      </tr>
      {hasChildren && isOpen
        ? node.ch?.map((child) => (
            <TreeNodeRows
              expanded={expanded}
              flashing={flashing}
              depth={depth + 1}
              key={child.p}
              node={child}
              onToggle={onToggle}
            />
          ))
        : null}
    </>
  );
}

export function MqttLiveTopicTree({ tree, treeShown, totalTopics, lastActivity }: TreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [flashing, setFlashing] = useState<Set<string>>(() => new Set());
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!lastActivity || lastActivity.paths.length === 0) {
      return;
    }
    setFlashing(new Set(lastActivity.paths));
    if (flashTimerRef.current !== null) {
      clearTimeout(flashTimerRef.current);
    }
    flashTimerRef.current = setTimeout(() => setFlashing(new Set()), FLASH_MS);
    return () => {
      if (flashTimerRef.current !== null) {
        clearTimeout(flashTimerRef.current);
      }
    };
  }, [lastActivity]);

  const onToggle = (path: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });

  if (tree.length === 0) {
    return (
      <div className="empty-workspace">
        <strong>No topics seen yet</strong>
        <span>The tree fills as messages arrive on the broker.</span>
      </div>
    );
  }

  return (
    <>
      <span className="results-filter-count">
        {treeShown} of {totalTopics} topic{totalTopics === 1 ? "" : "s"} shown
      </span>
      <div className="data-table-wrap results-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Topic</th>
              <th scope="col">Topics</th>
              <th scope="col">Msgs</th>
              <th scope="col">msg/s</th>
              <th scope="col">Schema</th>
              <th scope="col">Flags</th>
            </tr>
          </thead>
          <tbody>
            {tree.map((node) => (
              <TreeNodeRows
                expanded={expanded}
                flashing={flashing}
                depth={0}
                key={node.p}
                node={node}
                onToggle={onToggle}
              />
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
