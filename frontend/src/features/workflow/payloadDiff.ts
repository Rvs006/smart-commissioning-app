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
