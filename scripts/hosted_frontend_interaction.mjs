#!/usr/bin/env node
/** Exercise one real React control on each hosted route through Chrome CDP. */

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

const toggleSelector = `Array.from(document.querySelectorAll("button")).find(
  (element) => element.textContent?.includes("Review Comments")
)`;

async function exerciseRoute(session, baseUrl, route, expectedVersion) {
  const destination = new URL(route, baseUrl).href;
  await session.send("Page.navigate", { url: destination });
  await waitFor(
    session,
    `(() => {
      const root = document.getElementById("root");
      const toggle = ${toggleSelector};
      return document.readyState === "complete" && Boolean(root?.textContent?.trim()) && Boolean(toggle);
    })()`,
    `populated React route ${route}`,
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
  const versionState = await session.evaluate(`(() => {
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

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const target = await findPageTarget(options.debugUrl);
  const session = new DevToolsSession(target.webSocketDebuggerUrl);
  await session.open();
  try {
    const routes = [];
    for (const route of options.routes) {
      routes.push(
        await exerciseRoute(session, options.baseUrl, route, options.expectedVersion),
      );
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
