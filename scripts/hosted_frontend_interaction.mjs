#!/usr/bin/env node
/** Exercise real React controls and guidance routes through Chrome CDP. */

import { writeFile } from "node:fs/promises";

function parseArguments(argv) {
  const options = {
    baseUrl: "http://127.0.0.1:8080",
    debugUrl: "http://127.0.0.1:9222",
    expectedVersion: null,
    output: "build/browser/interaction.json",
    routes: [],
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    const next = argv[index + 1];
    if (value === "--base-url" && next) {
      options.baseUrl = next;
      index += 1;
    } else if (value === "--debug-url" && next) {
      options.debugUrl = next;
      index += 1;
    } else if (value === "--output" && next) {
      options.output = next;
      index += 1;
    } else if (value === "--expected-version" && next) {
      options.expectedVersion = next;
      index += 1;
    } else if (value === "--route" && next) {
      options.routes.push(next.startsWith("/") ? next : `/${next}`);
      index += 1;
    } else {
      throw new Error(`Unknown or incomplete argument: ${value}`);
    }
  }
  if (options.routes.length === 0) {
    options.routes = ["/", "/configuration", "/run-history"];
  }
  if (!options.expectedVersion || !/^v\d+\.\d+\.\d+$/.test(options.expectedVersion)) {
    throw new Error("--expected-version must be an explicit vMAJOR.MINOR.PATCH value");
  }
  return options;
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const chromeTargetReadyTimeoutMs = 30_000;
const chromeTargetPollIntervalMs = 100;
const guidanceViewports = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
];

async function findPageTarget(debugUrl) {
  let lastError;
  const deadline = Date.now() + chromeTargetReadyTimeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${debugUrl}/json/list`);
      if (!response.ok) {
        throw new Error(`Chrome target discovery returned HTTP ${response.status}`);
      }
      const targets = await response.json();
      const target = targets.find(
        (candidate) => candidate.type === "page" && candidate.webSocketDebuggerUrl,
      );
      if (target) {
        return target;
      }
      lastError = new Error("Chrome exposed no debuggable page target");
    } catch (error) {
      lastError = error;
    }
    await delay(chromeTargetPollIntervalMs);
  }
  const reason = lastError instanceof Error ? lastError.message : "no diagnostic was returned";
  throw new Error(
    `Chrome DevTools at ${debugUrl} did not expose a page target within ${chromeTargetReadyTimeoutMs / 1000} seconds: ${reason}`,
  );
}

class DevToolsSession {
  constructor(webSocketUrl) {
    this.socket = new WebSocket(webSocketUrl);
    this.nextId = 1;
    this.pending = new Map();
    this.browserErrors = [];
    this.socket.addEventListener("message", (event) => this.onMessage(event));
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener(
        "error",
        () => reject(new Error("Chrome DevTools WebSocket failed to open")),
        { once: true },
      );
    });
    await this.send("Page.enable");
    await this.send("Runtime.enable");
  }

  onMessage(event) {
    const message = JSON.parse(String(event.data));
    if (message.id) {
      const request = this.pending.get(message.id);
      if (!request) return;
      this.pending.delete(message.id);
      if (message.error) {
        request.reject(new Error(message.error.message ?? "Chrome DevTools command failed"));
      } else {
        request.resolve(message.result ?? {});
      }
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      this.browserErrors.push("uncaught runtime exception");
    }
    if (message.method === "Runtime.consoleAPICalled" && message.params?.type === "error") {
      this.browserErrors.push("console.error call");
    }
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const response = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (response.exceptionDetails) {
      throw new Error("Browser evaluation raised an exception");
    }
    return response.result?.value;
  }

  close() {
    this.socket.close();
  }
}

async function waitFor(session, expression, description) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (await session.evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function navigateToRoute(session, baseUrl, route, readyExpression) {
  const destination = new URL(route, baseUrl).href;
  await session.send("Page.navigate", { url: destination });
  await waitFor(
    session,
    `(() => {
      const root = document.getElementById("root");
      return document.readyState === "complete" && Boolean(root?.textContent?.trim()) && (${readyExpression});
    })()`,
    `populated React route ${route}`,
  );
}

async function visibleVersionState(session) {
  return session.evaluate(`(() => {
    const label = document.querySelector('[title="App version"]');
    if (!label) return { text: null, visible: false };
    const style = getComputedStyle(label);
    const bounds = label.getBoundingClientRect();
    return {
      text: label.textContent?.trim() ?? null,
      visible:
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity) > 0 &&
        bounds.width > 0 &&
        bounds.height > 0,
    };
  })()`);
}

async function measureGuidanceLayout(session) {
  return session.evaluate(`(() => {
    const clippedElements = Array.from(
      document.querySelectorAll(
        "h1,h2,h3,h4,h5,h6,p,li,label,button,a,input,select,textarea,code,pre",
      ),
    ).flatMap((element) => {
      const style = getComputedStyle(element);
      const bounds = element.getBoundingClientRect();
      const clippingValues = new Set(["hidden", "clip"]);
      const contentBounds = {
        left: bounds.left,
        right: bounds.left + Math.max(bounds.width, element.scrollWidth),
        top: bounds.top,
        bottom: bounds.top + Math.max(bounds.height, element.scrollHeight),
      };
      const visible =
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity) > 0 &&
        bounds.width > 0 &&
        bounds.height > 0;
      const clippedByViewport =
        contentBounds.left < -0.5 ||
        contentBounds.right > document.documentElement.clientWidth + 0.5;
      const clippedByOwnBox =
        (clippingValues.has(style.overflowX) && element.scrollWidth > element.clientWidth + 1) ||
        (clippingValues.has(style.overflowY) && element.scrollHeight > element.clientHeight + 1);
      let clippedByAncestor = false;
      for (
        let ancestor = element.parentElement;
        ancestor && ancestor !== document.documentElement;
        ancestor = ancestor.parentElement
      ) {
        const ancestorStyle = getComputedStyle(ancestor);
        const ancestorBounds = ancestor.getBoundingClientRect();
        const clippedHorizontally =
          clippingValues.has(ancestorStyle.overflowX) &&
          (contentBounds.left < ancestorBounds.left - 0.5 ||
            contentBounds.right > ancestorBounds.right + 0.5);
        const clippedVertically =
          clippingValues.has(ancestorStyle.overflowY) &&
          (contentBounds.top < ancestorBounds.top - 0.5 ||
            contentBounds.bottom > ancestorBounds.bottom + 0.5);
        if (clippedHorizontally || clippedVertically) {
          clippedByAncestor = true;
          break;
        }
      }
      if (!visible || (!clippedByViewport && !clippedByOwnBox && !clippedByAncestor)) {
        return [];
      }
      return [{
        id: element.id,
        tag: element.tagName,
        text: (element.textContent ?? element.getAttribute("aria-label") ?? "")
          .trim()
          .slice(0, 80),
        left: Math.round(bounds.left * 10) / 10,
        right: Math.round(bounds.right * 10) / 10,
        clippedByViewport,
        clippedByOwnBox,
        clippedByAncestor,
      }];
    });
    return {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      clippedElements,
    };
  })()`);
}

async function assertNoHorizontalOverflow(session, route, viewport) {
  const dimensions = await measureGuidanceLayout(session);
  if (
    !dimensions ||
    dimensions.scrollWidth > dimensions.clientWidth + 1 ||
    dimensions.clippedElements.length > 0
  ) {
    throw new Error(
      `Horizontal overflow or clipped content on ${route} at ${viewport.name}: ${JSON.stringify(dimensions)}`,
    );
  }
  return {
    clientWidth: dimensions.clientWidth,
    scrollWidth: dimensions.scrollWidth,
    clippedElementCount: dimensions.clippedElements.length,
  };
}

async function assertClippingDetectorCatchesFixture(session, route, viewport) {
  const fixtureId = "release-clipping-detector-fixture";
  const fixtureContentId = `${fixtureId}-content`;
  await session.evaluate(`(() => {
    document.getElementById(${JSON.stringify(fixtureId)})?.remove();
    const fixture = document.createElement("div");
    fixture.id = ${JSON.stringify(fixtureId)};
    Object.assign(fixture.style, {
      position: "fixed",
      left: "8px",
      top: "8px",
      width: "80px",
      height: "12px",
      overflow: "hidden",
      pointerEvents: "none",
      zIndex: "2147483647",
    });
    const content = document.createElement("p");
    content.id = ${JSON.stringify(fixtureContentId)};
    content.textContent = "Clipping detector negative fixture must overflow this ancestor";
    Object.assign(content.style, {
      width: "80px",
      margin: "0",
      overflow: "visible",
      whiteSpace: "nowrap",
    });
    fixture.append(content);
    document.body.append(fixture);
  })()`);
  try {
    const dimensions = await measureGuidanceLayout(session);
    const fixtureDetected = dimensions?.clippedElements?.some(
      (element) =>
        element.id === fixtureContentId && element.clippedByAncestor === true,
    );
    if (!fixtureDetected) {
      throw new Error(
        `Clipping detector missed its DOM fixture on ${route} at ${viewport.name}: ${JSON.stringify(dimensions)}`,
      );
    }
    return true;
  } finally {
    await session.evaluate(
      `document.getElementById(${JSON.stringify(fixtureId)})?.remove()`,
    );
  }
}

async function clickPressedButton(session, selector, label, route) {
  const clicked = await session.evaluate(`(() => {
    const button = Array.from(document.querySelectorAll(${JSON.stringify(selector)})).find(
      (candidate) => candidate.textContent?.includes(${JSON.stringify(label)}),
    );
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!clicked) throw new Error(`${label} control is absent on ${route}`);
  await waitFor(
    session,
    `(() => Array.from(document.querySelectorAll(${JSON.stringify(selector)})).some(
      (candidate) => candidate.textContent?.includes(${JSON.stringify(label)}) &&
        candidate.getAttribute("aria-pressed") === "true",
    ))()`,
    `${label} selected state on ${route}`,
  );
}

async function exerciseThemeToggle(session, route) {
  const previousThemeLabel = await session.evaluate(`(() => {
    const button = document.querySelector('button[aria-label*="colour theme"]');
    if (!button) return null;
    const before = button.getAttribute("aria-label");
    button.click();
    return before;
  })()`);
  if (!previousThemeLabel) throw new Error(`Theme control is absent on ${route}`);
  await waitFor(
    session,
    `document.querySelector('button[aria-label*="colour theme"]')?.getAttribute("aria-label") !== ${JSON.stringify(previousThemeLabel)}`,
    `Theme control response on ${route}`,
  );
}

const toggleSelector = `Array.from(document.querySelectorAll("button")).find(
  (element) => element.textContent?.includes("Review Comments")
)`;

async function exerciseRoute(session, baseUrl, route, expectedVersion) {
  await session.send("Emulation.clearDeviceMetricsOverride");
  await navigateToRoute(
    session,
    baseUrl,
    route,
    `Boolean(${toggleSelector})`,
  );
  const clicked = await session.evaluate(`(() => {
    const toggle = ${toggleSelector};
    if (!toggle) return false;
    toggle.click();
    return true;
  })()`);
  if (!clicked) throw new Error(`Review Comments control is absent on ${route}`);
  await waitFor(
    session,
    `(() => {
      const toggle = ${toggleSelector};
      const panel = document.getElementById("review-feedback-panel");
      return toggle?.getAttribute("aria-expanded") === "true" && panel?.hidden === false;
    })()`,
    `Review Comments open state on ${route}`,
  );
  const closed = await session.evaluate(`(() => {
    const close = document.querySelector('button[aria-label="Close review comments"]');
    if (!close) return false;
    close.click();
    return true;
  })()`);
  if (!closed) throw new Error(`Review Comments close control is absent on ${route}`);
  await waitFor(
    session,
    `(() => {
      const toggle = ${toggleSelector};
      const panel = document.getElementById("review-feedback-panel");
      return toggle?.getAttribute("aria-expanded") === "false" && panel?.hidden === true;
    })()`,
    `Review Comments closed state on ${route}`,
  );
  const versionState = await visibleVersionState(session);
  if (versionState?.text !== expectedVersion || versionState?.visible !== true) {
    throw new Error(
      `Visible app version label on ${route} is ${JSON.stringify(versionState)}, expected ${expectedVersion}`,
    );
  }
  return {
    route,
    control: "Review Comments",
    version_label: versionState.text,
    version_visible: versionState.visible,
    root_populated: true,
    opened: true,
    closed: true,
  };
}


async function exerciseBriefRoute(session, baseUrl, route, expectedVersion) {
  const tabHeadings = new Map([
    ["Basics", "What this tool is, and the job it does on site."],
    ["Key Features", "Nine modules, one commissioning workflow."],
    ["Section Reference", "Every section, what it is for, and how to use it."],
    ["Guided Tour", "Pick your role and walk a real job."],
  ]);
  const roleLabels = [
    "Commissioning Engineer",
    "BMS Designer",
    "Project Manager",
    "Integration Engineer",
  ];
  const viewports = [];
  for (const viewport of guidanceViewports) {
    await session.send("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.name === "mobile",
    });
    await navigateToRoute(
      session,
      baseUrl,
      route,
      `Boolean(document.querySelector('nav[aria-label="Product brief sections"]'))`,
    );
    let dimensions;
    for (const [label, heading] of tabHeadings) {
      await clickPressedButton(
        session,
        'nav[aria-label="Product brief sections"] button[aria-pressed]',
        label,
        route,
      );
      await waitFor(
        session,
        `document.querySelector("h1")?.textContent?.trim() === ${JSON.stringify(heading)}`,
        `${label} heading on ${route}`,
      );
      dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    }
    for (const label of roleLabels) {
      await clickPressedButton(session, '.dc-role-picker button[aria-pressed]', label, route);
      dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    }
    await exerciseThemeToggle(session, route);
    dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    const clippingNegativeFixture = await assertClippingDetectorCatchesFixture(
      session,
      route,
      viewport,
    );
    dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    viewports.push({
      ...viewport,
      heading: tabHeadings.get("Guided Tour"),
      clicked_controls: [...tabHeadings.keys(), "Theme", ...roleLabels],
      horizontal_overflow: dimensions.scrollWidth > dimensions.clientWidth + 1,
      clipped_content: dimensions.clippedElementCount > 0,
      clipping_negative_fixture: clippingNegativeFixture,
      dimensions,
    });
  }
  await session.send("Emulation.clearDeviceMetricsOverride");
  return {
    route,
    control: "Brief tabs",
    release_version: expectedVersion,
    root_populated: true,
    viewports,
  };
}


async function exerciseLearningRoute(session, baseUrl, route, expectedVersion) {
  const roleLabels = [
    "Commissioning Engineer",
    "BMS Designer",
    "Project Manager",
    "Integration Engineer",
  ];
  const viewports = [];
  for (const viewport of guidanceViewports) {
    await session.send("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.name === "mobile",
    });
    await navigateToRoute(
      session,
      baseUrl,
      route,
      `document.getElementById("installation-setup")?.textContent?.includes("Download and unzip") &&
        document.getElementById("operator-guides")?.textContent?.includes("Run an IP discovery")`,
    );
    let dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    for (const label of roleLabels) {
      await clickPressedButton(session, '.dc-role-picker button[aria-pressed]', label, route);
      dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    }
    await exerciseThemeToggle(session, route);
    dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    const clippingNegativeFixture = await assertClippingDetectorCatchesFixture(
      session,
      route,
      viewport,
    );
    dimensions = await assertNoHorizontalOverflow(session, route, viewport);
    viewports.push({
      ...viewport,
      heading: "Get the tool running first.",
      setup_checked: true,
      clicked_controls: ["Theme", ...roleLabels],
      horizontal_overflow: dimensions.scrollWidth > dimensions.clientWidth + 1,
      clipped_content: dimensions.clippedElementCount > 0,
      clipping_negative_fixture: clippingNegativeFixture,
      dimensions,
    });
  }
  await session.send("Emulation.clearDeviceMetricsOverride");
  return {
    route,
    control: "Learning setup and roles",
    release_version: expectedVersion,
    root_populated: true,
    viewports,
  };
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const target = await findPageTarget(options.debugUrl);
  const session = new DevToolsSession(target.webSocketDebuggerUrl);
  await session.open();
  try {
    const routes = [];
    for (const route of options.routes) {
      if (route === "/#/brief") {
        routes.push(await exerciseBriefRoute(session, options.baseUrl, route, options.expectedVersion));
      } else if (route === "/#/learning") {
        routes.push(await exerciseLearningRoute(session, options.baseUrl, route, options.expectedVersion));
      } else {
        routes.push(await exerciseRoute(session, options.baseUrl, route, options.expectedVersion));
      }
    }
    if (session.browserErrors.length > 0) {
      throw new Error(`Browser emitted ${session.browserErrors.length} uncaught or console errors`);
    }
    await writeFile(
      options.output,
      `${JSON.stringify(
        {
          schema_version: 2,
          app_version: options.expectedVersion,
          routes,
          browser_errors: [],
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
    process.stdout.write(`hosted frontend interaction: OK (${routes.length} routes)\n`);
  } finally {
    session.close();
  }
}

main().catch((error) => {
  process.stderr.write(`FAIL: ${error.message}\n`);
  process.exitCode = 1;
});
