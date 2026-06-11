import { isSecretSentinel, maskSecretValue } from "./secretField";

describe("isSecretSentinel", () => {
  it("accepts all-asterisk placeholders returned by the API", () => {
    expect(isSecretSentinel("*")).toBe(true);
    expect(isSecretSentinel("********")).toBe(true);
  });

  it("rejects empty strings", () => {
    expect(isSecretSentinel("")).toBe(false);
  });

  it("rejects real values, even ones containing asterisks", () => {
    expect(isSecretSentinel("hunter2")).toBe(false);
    expect(isSecretSentinel("**partially*masked**")).toBe(false);
    expect(isSecretSentinel("**** ")).toBe(false);
  });
});

describe("maskSecretValue", () => {
  it("returns an empty string unchanged", () => {
    expect(maskSecretValue("")).toBe("");
  });

  it("keeps a short identifiable prefix for stored secret references", () => {
    const masked = maskSecretValue("secret://certificates/ca-certificate/abcdef0123456789");
    expect(masked).toBe("secret://certificates/ca...");
    expect(masked.length).toBe(27);
  });

  it("masks plain values character-for-character", () => {
    expect(maskSecretValue("hunter2")).toBe("*******");
    expect(maskSecretValue("ab")).toHaveLength(2);
  });
});
