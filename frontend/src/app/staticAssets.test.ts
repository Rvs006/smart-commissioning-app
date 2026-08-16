import { describe, expect, it } from "vitest";
import indexHtml from "../../index.html?raw";

describe("browser identity assets", () => {
  it("declares the packaged Electracom logo as the browser icon", () => {
    expect(indexHtml).toContain(
      '<link rel="icon" type="image/png" href="/electracom-logo.png" />',
    );
  });
});
