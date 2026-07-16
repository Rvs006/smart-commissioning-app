import { getModuleByRoute } from "./moduleData";
import { moduleWorkspaces } from "./operatorData";

// Two data layers carry a title for the same head, and ModulePage renders
// `workspace?.title ?? module.title` — so operatorData silently shadows
// moduleData wherever a workspace exists. That is how /ip-scanner came to be
// called "IP Scanner" in one layer and "IP Discovery" in another without any
// test noticing. Keep them identical: whichever layer a future reader edits,
// the other must follow.
describe("module title layers", () => {
  // Iterate the workspaces, not the module list: "configuration" has a
  // moduleData entry with no workspace, and needs no counterpart here.
  it.each(Object.keys(moduleWorkspaces))(
    "%s has the same title in moduleData and operatorData",
    (route) => {
      expect(getModuleByRoute(route).title).toBe(moduleWorkspaces[route].title);
    },
  );
});
