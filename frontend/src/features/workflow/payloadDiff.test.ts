import { describe, expect, it } from "vitest";
import {
  alignPayloadDiff,
  diffPayloadLines,
  isPlainObject,
  tokenizeJsonLine,
  type AlignedRow,
  type DiffLine,
} from "./payloadDiff";

// Text of every produced line, joined, must equal JSON.stringify(value, null, 2)
// exactly — the panels' visible content is unchanged; only highlight classes are
// added. Marking depends on the OTHER side, so vary it to prove text is stable.
function assertStringifyInvariant(value: unknown, other: unknown): void {
  const diff = diffPayloadLines(value, other);
  expect(diff.expected.map((line) => line.text).join("\n")).toBe(JSON.stringify(value, null, 2));
  const reversed = diffPayloadLines(other, value);
  expect(reversed.observed.map((line) => line.text).join("\n")).toBe(JSON.stringify(value, null, 2));
}

function markedText(lines: DiffLine[], mark: DiffLine["mark"]): string[] {
  return lines.filter((line) => line.mark === mark).map((line) => line.text);
}

describe("diffPayloadLines — serialisation invariant", () => {
  it("reproduces JSON.stringify(value, null, 2) byte-for-byte", () => {
    const cases: Array<[unknown, unknown]> = [
      [{ version: "1.5.2", points: { supply_temp_sensor: { present_value: 22 } } }, {}],
      [{ empty_object: {}, empty_array: [], nested: { a: { b: 1 } } }, { nested: {} }],
      [{ quoted: 'a "tricky"\tstring', unicode: "café", slash: "a/b" }, {}],
      [{ list: [1, 2, { k: "v" }], flag: true, missing: null, count: 0 }, {}],
      [{}, { anything: 1 }],
      [[1, 2, 3], []],
      ["a bare string", { x: 1 }],
      [42, null],
    ];
    for (const [value, other] of cases) {
      assertStringifyInvariant(value, other);
    }
  });
});

describe("diffPayloadLines — presence marking", () => {
  it("marks a misnamed point on the side it appears (typo case)", () => {
    // A single misnamed point appears expected-only on one spelling and
    // observed-only on the other — the exact ISSUE-9 typo shape, shown as an
    // amber (expected-only) + red (observed-only) pair in the panels.
    const expected = {
      points: {
        supply_temp_sensor: { present_value: "<device-reported value>" },
        return_temp_sensor: { present_value: "<device-reported value>" },
      },
    };
    const observed = {
      points: {
        supply_temp_sensor: { present_value: 21.5 },
        retun_temp_sensor: { present_value: 19.0 },
      },
    };

    const diff = diffPayloadLines(expected, observed);

    // Expected side: the correctly spelled point that observed lacks is amber.
    const expectedOnly = markedText(diff.expected, "only-expected").join("\n");
    expect(expectedOnly).toContain('"return_temp_sensor"');
    expect(expectedOnly).not.toContain('"supply_temp_sensor"'); // present on both

    // Observed side: the typo'd point that expected lacks is red.
    const observedOnly = markedText(diff.observed, "only-observed").join("\n");
    expect(observedOnly).toContain('"retun_temp_sensor"');
    expect(observedOnly).not.toContain('"supply_temp_sensor"');
  });

  it("marks every line of a one-sided nested block, not just its opening line", () => {
    const expected = { config: { retries: 3, backoff: { base: 2 } } };
    const observed = { other: true };

    const diff = diffPayloadLines(expected, observed);
    const expectedOnly = markedText(diff.expected, "only-expected");
    // The whole `config` subtree (opening line, nested keys, closing brace) is
    // amber — it has no counterpart in observed at all.
    expect(expectedOnly.some((text) => text.includes('"config"'))).toBe(true);
    expect(expectedOnly.some((text) => text.includes('"retries"'))).toBe(true);
    expect(expectedOnly.some((text) => text.includes('"backoff"'))).toBe(true);
    expect(expectedOnly.some((text) => text.includes('"base"'))).toBe(true);
  });

  it("never marks a value difference when both sides carry the same key", () => {
    // version 1.5.2 vs 1.4.0 and unit spelling differences are NOT highlighted:
    // expected values are template sentinels, so only presence is compared.
    const expected = { version: "1.5.2", pointset: { points: { sensor: { units: "kwh" } } } };
    const observed = { version: "1.4.0", pointset: { points: { sensor: { units: "kilowatt_hours" } } } };

    const diff = diffPayloadLines(expected, observed);
    expect(markedText(diff.expected, "only-expected")).toEqual([]);
    expect(markedText(diff.observed, "only-observed")).toEqual([]);
  });

  it("treats arrays as leaves — no per-index diff marks", () => {
    const expected = { ports: [443, 80], only_here: 1 };
    const observed = { ports: [22], other: 2 };

    const diff = diffPayloadLines(expected, observed);
    const expectedOnly = markedText(diff.expected, "only-expected").join("\n");
    // The differing array contents are not marked; only the one-sided key is.
    expect(expectedOnly).toContain('"only_here"');
    expect(expectedOnly).not.toContain("443");
    expect(expectedOnly).not.toContain("80");
  });

  it("does not mark keys when the counterpart is a type mismatch (not an object)", () => {
    // expected.meta is an object; observed.meta is a string — a type mismatch,
    // so meta's keys are leaves and nothing inside is marked.
    const expected = { meta: { a: 1, b: 2 } };
    const observed = { meta: "n/a" };

    const diff = diffPayloadLines(expected, observed);
    expect(markedText(diff.expected, "only-expected")).toEqual([]);
  });
});

describe("isPlainObject", () => {
  it("accepts plain objects and rejects arrays, null, and primitives", () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject({ a: 1 })).toBe(true);
    expect(isPlainObject([])).toBe(false);
    expect(isPlainObject(null)).toBe(false);
    expect(isPlainObject("x")).toBe(false);
    expect(isPlainObject(3)).toBe(false);
  });
});

function joinSide(rows: AlignedRow[], side: "expected" | "observed"): string {
  return rows
    .map((row) => row[side])
    .filter((line): line is DiffLine => line !== null)
    .map((line) => line.text)
    .join("\n");
}

describe("alignPayloadDiff — parse-back invariant (ITEM-8)", () => {
  // Content is unchanged, only key order is normalised, so each side's non-filler
  // lines must JSON.parse back deep-equal to the input (the honesty trade-off
  // that replaces diffPayloadLines' byte-for-byte invariant).
  it("each side's non-filler lines parse back deep-equal to the input", () => {
    const cases: Array<[unknown, unknown]> = [
      [{ a: 1, b: { c: 2 } }, { b: { c: 9 }, a: 5 }], // reordered keys
      [{ version: "1.5.2", points: { x: { v: 1 } } }, { points: { y: { v: 2 } }, version: "1.4.0" }],
      [{ only_e: 1 }, { only_o: 2 }],
      [{}, { a: 1 }],
      [{ a: [1, 2], b: 3 }, { a: [3], b: 4 }], // arrays are leaves
      [{ meta: { a: 1 } }, { meta: "n/a" }], // type mismatch
    ];
    for (const [expected, observed] of cases) {
      const rows = alignPayloadDiff(expected, observed);
      expect(JSON.parse(joinSide(rows, "expected"))).toEqual(expected);
      expect(JSON.parse(joinSide(rows, "observed"))).toEqual(observed);
    }
  });

  it("marks a one-sided key on its own side and leaves shared-key values unmarked", () => {
    const rows = alignPayloadDiff(
      { points: { supply: 1, ret: 2 } },
      { points: { supply: 9, retun: 2 } },
    );
    const expectedOnly = rows
      .filter((row) => row.expected?.mark === "only-expected")
      .map((row) => row.expected!.text)
      .join("\n");
    const observedOnly = rows
      .filter((row) => row.observed?.mark === "only-observed")
      .map((row) => row.observed!.text)
      .join("\n");
    expect(expectedOnly).toContain('"ret"');
    expect(observedOnly).toContain('"retun"');
    // supply is on both sides — never marked, even though its value differs.
    expect(expectedOnly).not.toContain('"supply"');
    expect(observedOnly).not.toContain('"supply"');
  });

  it("flags the first row of a key named in flaggedKeys (both sides)", () => {
    const rows = alignPayloadDiff(
      { points: { supply: 1 } },
      { points: { supply: 2 } },
      new Set(["supply"]),
    );
    const flagged = rows.filter((row) => row.flagged);
    expect(flagged).toHaveLength(1);
    expect(flagged[0].expected?.text).toContain('"supply"');
    expect(flagged[0].observed?.text).toContain('"supply"');
  });

  it("never flags a structural key that collides with a point name (only points-level keys)", () => {
    // "version"/"timestamp" are top-level structural keys of every pointset
    // template. A device that also publishes a point literally named "version"
    // gets that point flagged — but the top-level "version" row must stay clean,
    // never misattributed to the engine's finding.
    const rows = alignPayloadDiff(
      { version: "1.5.2", points: { version: { present_value: null } } },
      { version: "1.5.2", points: { version: { present_value: 3 } } },
      new Set(["version"]),
    );
    const flagged = rows.filter((row) => row.flagged);
    // Exactly one flagged row — the point under "points", not the top-level key.
    expect(flagged).toHaveLength(1);
    // The flagged row opens the point object ("version": {), never the top-level
    // scalar version row (value "1.5.2").
    expect(flagged[0].expected?.text).toContain('"version"');
    expect(flagged[0].expected?.text).not.toContain('"1.5.2"');
    const topLevelVersion = rows.find((row) => row.expected?.text.includes('"1.5.2"'));
    expect(topLevelVersion?.flagged).toBe(false);
  });
});

describe("tokenizeJsonLine (ITEM-8)", () => {
  it("classifies key, string, and punctuation and loses no text", () => {
    const tokens = tokenizeJsonLine('  "version": "1.5.2",');
    expect(tokens.filter((token) => token.kind === "key").map((token) => token.text)).toEqual([
      '"version"',
    ]);
    expect(tokens.filter((token) => token.kind === "string").map((token) => token.text)).toEqual([
      '"1.5.2"',
    ]);
    // The concatenated token text always equals the input line, so colouring can
    // never mangle or drop a character.
    expect(tokens.map((token) => token.text).join("")).toBe('  "version": "1.5.2",');
  });

  it("classifies numbers and literals", () => {
    expect(tokenizeJsonLine("    22").some((t) => t.kind === "number" && t.text === "22")).toBe(true);
    expect(tokenizeJsonLine("    -3.5,").some((t) => t.kind === "number" && t.text === "-3.5")).toBe(
      true,
    );
    expect(tokenizeJsonLine("    true").some((t) => t.kind === "literal" && t.text === "true")).toBe(
      true,
    );
    expect(tokenizeJsonLine("    null").some((t) => t.kind === "literal" && t.text === "null")).toBe(
      true,
    );
  });

  it("round-trips every line shape this module emits", () => {
    for (const line of ["{", "  }", '  "a": {', "    -3.5,", "  []", '    "café"']) {
      expect(tokenizeJsonLine(line).map((token) => token.text).join("")).toBe(line);
    }
  });
});
