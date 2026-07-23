// Per-line PRESENCE diff for the UDMI expected-vs-observed payload panels
// (ISSUE-8). The expected side is a TEMPLATE whose values are sentinels
// (present_value: null, "<device manufacturer>", a build timestamp, canonical
// placeholders, ...), so VALUE differences must never be marked — doing so would
// flag every healthy payload and fabricate findings the engine never made. Only
// key PRESENCE is compared, and only where BOTH sides are plain objects at the
// same path: a key present on just one side is highlighted on that side. The
// engine's issue cards (rendered above these panels) remain the authority on
// values.
//
// The serializer reproduces JSON.stringify(value, null, 2) byte-for-byte so the
// panels' visible text is unchanged; only per-line highlight classes are added.
// The join-equals-stringify invariant is pinned in payloadDiff.test.ts.
//
// Ceiling: arrays and type-mismatched nodes are treated as leaves (no descent).
// UDMI payloads are object-shaped, so per-index array diffing is out of scope.

export type DiffMark = "only-expected" | "only-observed" | null;

export type DiffLine = {
  text: string;
  mark: DiffMark;
};

export type PayloadDiff = {
  expected: DiffLine[];
  observed: DiffLine[];
};

const INDENT = "  ";

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Object property keys JSON.stringify actually serialises: it drops keys whose
// value is undefined, a function, or a symbol. Mirroring that keeps the
// join-equals-stringify invariant true for those (non-JSON) inputs too.
function serialisableKeys(obj: Record<string, unknown>): string[] {
  return Object.keys(obj).filter((key) => {
    const type = typeof obj[key];
    return type !== "undefined" && type !== "function" && type !== "symbol";
  });
}

function indentOf(depth: number): string {
  return INDENT.repeat(depth);
}

// Serialise `value` as a standalone JSON value at `depth`, marking object keys
// absent from `other` (the counterpart node on the OTHER side) with
// `markForMissing`. `forced`, when non-null, stamps every produced line so a
// whole subtree under a one-sided key inherits that key's mark.
function serializeValue(
  value: unknown,
  depth: number,
  other: unknown,
  markForMissing: DiffMark,
  forced: DiffMark,
): DiffLine[] {
  const indent = indentOf(depth);

  if (isPlainObject(value)) {
    const keys = serialisableKeys(value);
    if (keys.length === 0) {
      return [{ text: `${indent}{}`, mark: forced }];
    }
    // Keys are only diffable against another plain object at the same path;
    // a non-object counterpart (absent, primitive, array) is a type mismatch,
    // so no key is marked "missing" — presence simply can't be compared.
    const otherObject = isPlainObject(other) ? other : null;
    const lines: DiffLine[] = [{ text: `${indent}{`, mark: forced }];
    keys.forEach((key, index) => {
      const missing = otherObject !== null && !(key in otherObject);
      const keyForced = missing ? markForMissing : forced;
      const childOther = otherObject !== null ? otherObject[key] : undefined;
      const childLines = serializeValue(value[key], depth + 1, childOther, markForMissing, keyForced);
      // Merge the `"key": ` prefix into the child's first line, which starts
      // with exactly indentOf(depth + 1).
      const childIndent = indentOf(depth + 1);
      const firstBody = childLines[0].text.slice(childIndent.length);
      childLines[0] = {
        text: `${childIndent}${JSON.stringify(key)}: ${firstBody}`,
        mark: childLines[0].mark,
      };
      // Trailing comma on the last line of every entry except the final one.
      if (index < keys.length - 1) {
        const last = childLines[childLines.length - 1];
        childLines[childLines.length - 1] = { text: `${last.text},`, mark: last.mark };
      }
      lines.push(...childLines);
    });
    lines.push({ text: `${indent}}`, mark: forced });
    return lines;
  }

  if (Array.isArray(value)) {
    if (value.length === 0) {
      return [{ text: `${indent}[]`, mark: forced }];
    }
    // Arrays are leaves for diffing: items inherit `forced`, no per-index diff.
    const lines: DiffLine[] = [{ text: `${indent}[`, mark: forced }];
    value.forEach((item, index) => {
      const itemLines = serializeValue(item, depth + 1, undefined, markForMissing, forced);
      if (index < value.length - 1) {
        const last = itemLines[itemLines.length - 1];
        itemLines[itemLines.length - 1] = { text: `${last.text},`, mark: last.mark };
      }
      lines.push(...itemLines);
    });
    lines.push({ text: `${indent}]`, mark: forced });
    return lines;
  }

  // Primitive (string / number / boolean / null). JSON.stringify renders one
  // token; undefined/function/symbol items in an array serialise as "null".
  const token = JSON.stringify(value);
  return [{ text: `${indent}${token === undefined ? "null" : token}`, mark: forced }];
}

export function diffPayloadLines(expected: unknown, observed: unknown): PayloadDiff {
  return {
    expected: serializeValue(expected, 0, observed, "only-expected", null),
    observed: serializeValue(observed, 0, expected, "only-observed", null),
  };
}

// --- Aligned expected/observed diff (ITEM-8) --------------------------------
//
// alignPayloadDiff produces LINE-FOR-LINE aligned rows so the two panels sit on a
// shared grid and scroll together. Where diffPayloadLines serialises each side
// independently (byte-for-byte JSON.stringify), alignment normalises key order:
// at a paired object node it walks the UNION of keys (expected order, then
// observed-only keys) on BOTH sides, so a key present on one side never shoves
// the other side's lines out of step. Content is unchanged — only order — so the
// honesty invariant becomes PARSE-BACK equality rather than byte equality: each
// side's non-filler lines, joined and JSON.parsed, deep-equal the input. Pinned
// in payloadDiff.test.ts. Do NOT "restore" byte-for-byte here — that would defeat
// the alignment. VALUES are still never compared: the red row highlight is driven
// only by exact JSON Pointer paths from validation evidence, never a value diff
// (expected values are template sentinels). A unit mismatch can therefore mark
// only `/pointset/points/<point>/units`, without tinting the whole point object.
// Arrays and type-mismatched nodes stay leaves, exactly as in diffPayloadLines.

export type AlignedRow = {
  expected: DiffLine | null;
  observed: DiffLine | null;
  // Set on the first row at an exact path supplied by validation evidence.
  flagged: boolean;
};

function jsonPointer(path: string[]): string {
  return `/${path.map((part) => part.replace(/~/g, "~0").replace(/\//g, "~1")).join("/")}`;
}

// Clean (mark null) or fully-marked (one-sided) serialisation of one side. `other`
// is undefined so presence is never compared here — one-sidedness is the caller's
// decision, and a null mark yields ordinary lines for a paired leaf.
function serializeSide(value: unknown, depth: number, mark: DiffMark): DiffLine[] {
  return serializeValue(value, depth, undefined, mark, mark);
}

function prefixKeyOnSide(
  rows: AlignedRow[],
  key: string,
  side: "expected" | "observed",
  childDepth: number,
): void {
  const childIndent = indentOf(childDepth);
  for (const row of rows) {
    const line = row[side];
    if (line) {
      row[side] = {
        text: `${childIndent}${JSON.stringify(key)}: ${line.text.slice(childIndent.length)}`,
        mark: line.mark,
      };
      return;
    }
  }
}

function appendCommaOnSide(rows: AlignedRow[], side: "expected" | "observed"): void {
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const line = rows[index][side];
    if (line) {
      rows[index][side] = { text: `${line.text},`, mark: line.mark };
      return;
    }
  }
}

function alignPair(
  expected: unknown,
  observed: unknown,
  depth: number,
  flaggedPaths: ReadonlySet<string>,
  path: string[],
): AlignedRow[] {
  const indent = indentOf(depth);

  if (isPlainObject(expected) && isPlainObject(observed)) {
    const expectedKeys = serialisableKeys(expected);
    const observedKeys = serialisableKeys(observed);
    if (expectedKeys.length === 0 && observedKeys.length === 0) {
      return [
        {
          expected: { text: `${indent}{}`, mark: null },
          observed: { text: `${indent}{}`, mark: null },
          flagged: false,
        },
      ];
    }
    // Union output order: expected keys in their order, then observed-only keys.
    const observedOnly = observedKeys.filter((key) => !expectedKeys.includes(key));
    const unionOrder = [...expectedKeys, ...observedOnly];
    // Trailing commas are decided by OUTPUT order per side (not the input order):
    // the last key that appears on a side in the union order gets no comma.
    const observedInOutput = unionOrder.filter((key) => observedKeys.includes(key));
    const lastExpectedInOutput = expectedKeys[expectedKeys.length - 1];
    const lastObservedInOutput = observedInOutput[observedInOutput.length - 1];

    const rows: AlignedRow[] = [
      {
        expected: { text: `${indent}{`, mark: null },
        observed: { text: `${indent}{`, mark: null },
        flagged: false,
      },
    ];
    for (const key of unionOrder) {
      const childPath = [...path, key];
      const inExpected = expectedKeys.includes(key);
      const inObserved = observedKeys.includes(key);
      let childRows: AlignedRow[];
      if (inExpected && inObserved) {
        // Carry the exact object path through recursion. The caller decides
        // which path is authoritative; no key-name heuristic is applied here.
        childRows = alignPair(expected[key], observed[key], depth + 1, flaggedPaths, childPath);
        prefixKeyOnSide(childRows, key, "expected", depth + 1);
        prefixKeyOnSide(childRows, key, "observed", depth + 1);
      } else if (inExpected) {
        childRows = serializeSide(expected[key], depth + 1, "only-expected").map((line) => ({
          expected: line,
          observed: null,
          flagged: false,
        }));
        prefixKeyOnSide(childRows, key, "expected", depth + 1);
      } else {
        childRows = serializeSide(observed[key], depth + 1, "only-observed").map((line) => ({
          expected: null,
          observed: line,
          flagged: false,
        }));
        prefixKeyOnSide(childRows, key, "observed", depth + 1);
      }
      if (inExpected && key !== lastExpectedInOutput) {
        appendCommaOnSide(childRows, "expected");
      }
      if (inObserved && key !== lastObservedInOutput) {
        appendCommaOnSide(childRows, "observed");
      }
      if (flaggedPaths.has(jsonPointer(childPath)) && childRows.length > 0) {
        childRows[0].flagged = true;
      }
      rows.push(...childRows);
    }
    rows.push({
      expected: { text: `${indent}}`, mark: null },
      observed: { text: `${indent}}`, mark: null },
      flagged: false,
    });
    return rows;
  }

  // Leaves: both arrays, both primitives, or a type mismatch. Serialise each side
  // cleanly (no marks — presence cannot be compared) and pad the shorter side
  // with filler rows so the two panels stay row-aligned.
  const expectedLines = serializeSide(expected, depth, null);
  const observedLines = serializeSide(observed, depth, null);
  const count = Math.max(expectedLines.length, observedLines.length);
  const leafRows: AlignedRow[] = [];
  for (let index = 0; index < count; index += 1) {
    leafRows.push({
      expected: expectedLines[index] ?? null,
      observed: observedLines[index] ?? null,
      flagged: false,
    });
  }
  return leafRows;
}

export function alignPayloadDiff(
  expected: unknown,
  observed: unknown,
  flaggedPaths: ReadonlySet<string> = new Set(),
): AlignedRow[] {
  return alignPair(expected, observed, 0, flaggedPaths, []);
}

// --- JSON syntax colouring (ITEM-8) -----------------------------------------
//
// tokenizeJsonLine splits ONE serialised line (indent + optional "key": + value
// + trailing comma) into coloured spans. It only needs to handle the shapes this
// module emits, not arbitrary JSON. Covered in payloadDiff.test.ts.

export type JsonTokenKind = "key" | "string" | "number" | "literal" | "punct" | "plain";
export type JsonToken = { kind: JsonTokenKind; text: string };

const KEY_RE = /^("(?:[^"\\]|\\.)*")(\s*:\s*)/;
const STRING_RE = /^"(?:[^"\\]|\\.)*"/;
const NUMBER_RE = /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/;
const LITERAL_RE = /^(?:true|false|null)/;
const PUNCT_RE = /^[[\]{},:]/;

function nextJsonToken(rest: string): JsonToken {
  const string = STRING_RE.exec(rest);
  if (string) {
    return { kind: "string", text: string[0] };
  }
  const number = NUMBER_RE.exec(rest);
  if (number) {
    return { kind: "number", text: number[0] };
  }
  const literal = LITERAL_RE.exec(rest);
  if (literal) {
    return { kind: "literal", text: literal[0] };
  }
  const punct = PUNCT_RE.exec(rest);
  if (punct) {
    return { kind: "punct", text: punct[0] };
  }
  return { kind: "plain", text: rest[0] };
}

export function tokenizeJsonLine(text: string): JsonToken[] {
  const tokens: JsonToken[] = [];
  const indent = /^\s*/.exec(text)?.[0] ?? "";
  if (indent) {
    tokens.push({ kind: "punct", text: indent });
  }
  let rest = text.slice(indent.length);
  const keyMatch = KEY_RE.exec(rest);
  if (keyMatch) {
    tokens.push({ kind: "key", text: keyMatch[1] });
    tokens.push({ kind: "punct", text: keyMatch[2] });
    rest = rest.slice(keyMatch[0].length);
  }
  while (rest.length > 0) {
    const token = nextJsonToken(rest);
    tokens.push(token);
    rest = rest.slice(token.text.length);
  }
  return tokens;
}
