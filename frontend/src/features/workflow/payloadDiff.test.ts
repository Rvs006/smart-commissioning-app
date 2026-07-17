import { describe, expect, it } from "vitest";
import { diffPayloadLines, isPlainObject, type DiffLine } from "./payloadDiff";

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
