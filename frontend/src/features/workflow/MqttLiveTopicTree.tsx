import { useCallback, useEffect, useRef, useState } from "react";
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
const COPIED_MS = 1200;

type SortMode = "name" | "rate";

// A-Z natural order. The sidecar emits children rate-desc (server.js
// `kids.sort((a,b) => b.r - a.r)`), so without a stable order the rows reshuffle
// on every ~1 Hz snapshot as message rates drift — a busy branch keeps leaping
// around while the operator is trying to read it. Name never changes, so this
// holds each row in place; the msg/s column still conveys rate. Default order.
function orderByName(nodes: MqttLiveTreeNode[]): MqttLiveTreeNode[] {
  return nodes.slice().sort((a, b) => a.n.localeCompare(b.n, undefined, { numeric: true }));
}

// GAP-M2 "Rate ↻": rank-once-then-hold (app.js computeOrder rate mode). A frozen
// rank map, captured on the last explicit rank press, orders each sibling group;
// nodes ranked earlier keep their slot as rates drift, and any topic that
// appeared since the last press (no frozen rank) falls to the end A-Z instead of
// leaping to the top. Pressing the button again re-ranks against current rates.
function orderByFrozenRate(
  nodes: MqttLiveTreeNode[],
  frozen: Map<string, number>,
): MqttLiveTreeNode[] {
  return nodes.slice().sort((a, b) => {
    const ra = frozen.get(a.p);
    const rb = frozen.get(b.p);
    if (ra !== undefined && rb !== undefined) {
      return ra - rb;
    }
    if (ra !== undefined) {
      return -1; // ranked nodes sit above never-ranked (fresh) ones
    }
    if (rb !== undefined) {
      return 1;
    }
    return a.n.localeCompare(b.n, undefined, { numeric: true }); // fresh: A-Z
  });
}

// Walk the current tree and assign each node a global slot number in rate-desc
// (then A-Z) order within its sibling group, so orderByFrozenRate holds exactly
// this ranking until the next press. Recorded into `frozen` in place.
function rankByRate(nodes: MqttLiveTreeNode[], frozen: Map<string, number>, counter: { n: number }): void {
  const ordered = nodes
    .slice()
    .sort((a, b) => b.r - a.r || a.n.localeCompare(b.n, undefined, { numeric: true }));
  for (const node of ordered) {
    frozen.set(node.p, counter.n);
    counter.n += 1;
    if (node.ch && node.ch.length > 0) {
      rankByRate(node.ch, frozen, counter);
    }
  }
}

type TreeProps = {
  tree: MqttLiveTreeNode[];
  treeShown: number;
  totalTopics: number;
  lastActivity: { paths: string[]; at: number } | null;
  onFocus?: (asset: string) => void;
};

function TreeNodeRows({
  node,
  depth,
  expanded,
  onToggle,
  flashing,
  onFocus,
  orderNodes,
  onCopy,
  copiedPath,
}: {
  node: MqttLiveTreeNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  flashing: Set<string>;
  onFocus?: (asset: string) => void;
  orderNodes: (nodes: MqttLiveTreeNode[]) => MqttLiveTreeNode[];
  onCopy: (path: string) => void;
  copiedPath: string | null;
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
          {/* GAP-M3: copy this node's full topic path. */}
          <button
            className="secondary-button compact"
            onClick={() => onCopy(node.p)}
            title={`Copy topic ${node.p}`}
            type="button"
          >
            {copiedPath === node.p ? "Copied" : "Copy topic"}
          </button>
          {node.a ? (
            onFocus ? (
              <button className="secondary-button compact" onClick={() => onFocus(node.a as string)} type="button">
                Focus {node.a}
              </button>
            ) : (
              <span className="results-filter-count"> {node.a}</span>
            )
          ) : null}
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
        ? orderNodes(node.ch ?? []).map((child) => (
            <TreeNodeRows
              copiedPath={copiedPath}
              expanded={expanded}
              flashing={flashing}
              depth={depth + 1}
              key={child.p}
              node={child}
              onCopy={onCopy}
              onFocus={onFocus}
              onToggle={onToggle}
              orderNodes={orderNodes}
            />
          ))
        : null}
    </>
  );
}

export function MqttLiveTopicTree({ tree, treeShown, totalTopics, lastActivity, onFocus }: TreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [flashing, setFlashing] = useState<Set<string>>(() => new Set());
  const [sortMode, setSortMode] = useState<SortMode>("name");
  const [copiedPath, setCopiedPath] = useState<string | null>(null);
  // The frozen rate ranking (GAP-M2). State, not a ref, so pressing Rate ↻ again
  // (sortMode already "rate") still re-renders with the fresh ranking — a new Map
  // object every press. Read during render for ordering.
  const [frozenRank, setFrozenRank] = useState<Map<string, number>>(() => new Map());
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const treeRef = useRef(tree);
  treeRef.current = tree;

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

  useEffect(
    () => () => {
      if (copyTimerRef.current !== null) {
        clearTimeout(copyTimerRef.current);
      }
    },
    [],
  );

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

  const onCopy = useCallback((path: string) => {
    try {
      void navigator.clipboard?.writeText(path);
    } catch {
      // Clipboard access can be denied; the copy button is a convenience only.
    }
    setCopiedPath(path);
    if (copyTimerRef.current !== null) {
      clearTimeout(copyTimerRef.current);
    }
    copyTimerRef.current = setTimeout(() => setCopiedPath(null), COPIED_MS);
  }, []);

  const rankByCurrentRates = useCallback(() => {
    const frozen = new Map<string, number>();
    rankByRate(treeRef.current, frozen, { n: 0 });
    setFrozenRank(frozen);
    setSortMode("rate");
  }, []);

  const orderNodes = useCallback(
    (nodes: MqttLiveTreeNode[]) =>
      sortMode === "rate" ? orderByFrozenRate(nodes, frozenRank) : orderByName(nodes),
    [sortMode, frozenRank],
  );

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
      <div className="inline-actions">
        <span className="results-filter-count">
          {treeShown} of {totalTopics} topic{totalTopics === 1 ? "" : "s"} shown
        </span>
        <button
          aria-pressed={sortMode === "name"}
          className="secondary-button compact"
          onClick={() => setSortMode("name")}
          title="Order the tree A-Z (stable while messages arrive)."
          type="button"
        >
          Name
        </button>
        <button
          aria-pressed={sortMode === "rate"}
          className="secondary-button compact"
          onClick={rankByCurrentRates}
          title="Rank the tree by current message rate, then hold that order. Press again to re-rank."
          type="button"
        >
          Rate ↻
        </button>
      </div>
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
            {orderNodes(tree).map((node) => (
              <TreeNodeRows
                copiedPath={copiedPath}
                expanded={expanded}
                flashing={flashing}
                depth={0}
                key={node.p}
                node={node}
                onCopy={onCopy}
                onFocus={onFocus}
                onToggle={onToggle}
                orderNodes={orderNodes}
              />
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
