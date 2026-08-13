import { describe, expect, it } from "vitest";
import { isPlainObject } from "./isPlainObject";

describe("isPlainObject", () => {
  it("accepts record-shaped values and rejects arrays, null, and primitives", () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject({ value: 1 })).toBe(true);
    expect(isPlainObject([])).toBe(false);
    expect(isPlainObject(null)).toBe(false);
    expect(isPlainObject("value")).toBe(false);
    expect(isPlainObject(1)).toBe(false);
  });
});
