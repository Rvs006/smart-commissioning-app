import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";

// jsdom implements no layout, so Element.prototype.scrollIntoView is undefined.
// ModulePage calls it whenever the Results step opens, which every test that
// drives a run to a successful terminal state does — so install a global no-op.
// The snap-to-top tests wrap this with vi.spyOn, which is only possible because
// the method exists here (spyOn throws on an undefined property). Delete this if
// jsdom ever ships the real API.
window.HTMLElement.prototype.scrollIntoView = () => {};

afterEach(() => {
  cleanup();
});
