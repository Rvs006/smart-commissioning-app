'use strict';
/*
 * server.js — local HTTP server + REST/SSE API for the IP scanner UI.
 * Zero external dependencies (pure Node http) so it packages to a single .exe
 * cleanly. Binds to loopback only.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const scanner = require('./scanner');

const PORT = Number(process.env.PORT) || 3777;
const HOST = '127.0.0.1';

// Asset resolution. When built as a Single Executable Application, all web
// assets are embedded in global.__ASSETS__ (built by build-bundle.js). In dev
// they are loose files on disk under BASE. getAsset() hides the difference.
const BASE = __dirname;
const PUBLIC_DIR = path.join(BASE, 'public');
const TEMPLATE_FILE = path.join(BASE, 'template', 'ip-device-register-template.csv');
const EMBEDDED = typeof global.__ASSETS__ === 'object' ? global.__ASSETS__ : null;

// key is a forward-slash relative path, e.g. "public/index.html".
function getAsset(key) {
  if (EMBEDDED) {
    const a = EMBEDDED[key];
    if (!a) return null;
    return Buffer.from(a.data, a.enc || 'base64');
  }
  const abs = path.join(BASE, key);
  if (!abs.startsWith(BASE)) return null;
  try { return fs.readFileSync(abs); } catch (_) { return null; }
}

// In-memory imported register (single local operator).
let register = []; // [{ ip, hostname, type, vendor, model, expectedPorts:[], project, location, description }]

// ---------------------------------------------------------------------------
// CSV parsing (RFC4180-ish, handles quoted fields)
// ---------------------------------------------------------------------------
function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\r') { /* skip */ }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.length && r.some((v) => v.trim() !== ''));
}

function splitPorts(s) {
  if (!s) return [];
  return String(s)
    .split(/[;,\s|]+/)
    .map((p) => parseInt(p, 10))
    .filter((p) => Number.isFinite(p) && p > 0 && p <= 65535);
}

function importRegister(csvText) {
  const rows = parseCsv(csvText);
  if (!rows.length) return [];
  const header = rows[0].map((h) => h.trim().toLowerCase());
  const idx = (names) => {
    for (const n of names) {
      const i = header.indexOf(n);
      if (i !== -1) return i;
    }
    return -1;
  };
  const col = {
    ip: idx(['ip', 'ip address', 'ip_address', 'address']),
    hostname: idx(['hostname', 'host', 'name', 'device', 'device name']),
    type: idx(['type', 'device type', 'category']),
    vendor: idx(['vendor', 'manufacturer', 'make']),
    model: idx(['model']),
    ports: idx(['expected ports', 'expected_ports', 'ports', 'open ports', 'services ports']),
    project: idx(['project', 'site']),
    location: idx(['location', 'area', 'zone']),
    description: idx(['description', 'notes', 'desc']),
  };
  const out = [];
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r];
    const get = (i) => (i >= 0 && i < row.length ? row[i].trim() : '');
    const ip = get(col.ip);
    const hostname = get(col.hostname);
    if (!ip && !hostname) continue;
    out.push({
      ip,
      hostname,
      type: get(col.type),
      vendor: get(col.vendor),
      model: get(col.model),
      expectedPorts: splitPorts(get(col.ports)),
      project: get(col.project),
      location: get(col.location),
      description: get(col.description),
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// RAG comparison: merge discovered devices against imported register
// ---------------------------------------------------------------------------
/*
 * RAG rules:
 *   GREEN  (match)   — expected device reachable AND every expected port open
 *                      AND no unexpected extra ports.
 *   AMBER  (partial) — reachable, but some expected ports missing OR extra
 *                      unexpected ports present.
 *   RED    (missing) — expected device not found / unreachable.
 *   RED    (rogue)   — discovered device that is NOT in the register.
 * With no register imported, everything discovered is simply "unclassified".
 */
function normHost(h) {
  // Compare on the short name, case-insensitive (strip domain / trailing dot).
  return String(h || '').split('.')[0].trim().toLowerCase();
}

function compare(devices) {
  const devByIp = {};
  const devByHost = {};
  for (const d of devices) {
    devByIp[d.ip] = d;
    if (d.hostname) devByHost[d.hostname.toLowerCase()] = d;
  }
  const rows = [];
  const consumed = new Set();

  for (const exp of register) {
    let dev = (exp.ip && devByIp[exp.ip]) || (exp.hostname && devByHost[exp.hostname.toLowerCase()]);
    if (dev) consumed.add(dev.ip);

    if (!dev) {
      rows.push({
        ip: exp.ip || '—',
        hostname: exp.hostname || '',
        discoveredHostname: '',
        expectedHostname: exp.hostname || '',
        hostnameStatus: exp.hostname ? 'unverified' : 'na',
        hostnameSrc: '',
        status: 'unreachable',
        register: 'missing',
        rag: 'red',
        latency: null,
        openPorts: [],
        services: [],
        expectedPorts: exp.expectedPorts,
        missingPorts: exp.expectedPorts,
        extraPorts: [],
        vendor: exp.vendor,
        model: exp.model,
        type: exp.type,
        project: exp.project,
        location: exp.location,
        description: exp.description,
        expected: true,
      });
      continue;
    }

    const openSet = new Set(dev.openPorts);
    const expSet = new Set(exp.expectedPorts);
    const missingPorts = exp.expectedPorts.filter((p) => !openSet.has(p));
    const extraPorts = dev.openPorts.filter((p) => !expSet.has(p));
    let registerStatus, rag;
    if (exp.expectedPorts.length === 0) {
      // No expected ports specified: presence alone is a match.
      registerStatus = 'match';
      rag = 'green';
    } else if (missingPorts.length === 0 && extraPorts.length === 0) {
      registerStatus = 'match';
      rag = 'green';
    } else if (missingPorts.length === 0 && extraPorts.length > 0) {
      registerStatus = 'partial';
      rag = 'amber';
    } else if (missingPorts.length < exp.expectedPorts.length) {
      registerStatus = 'partial';
      rag = 'amber';
    } else {
      // Reachable but none of the expected ports are open.
      registerStatus = 'partial';
      rag = 'amber';
    }

    // Hostname validation: compare register hostname vs discovered hostname.
    let hostnameStatus = 'na';
    if (exp.hostname) {
      if (!dev.hostname) hostnameStatus = 'unverified';
      else hostnameStatus = normHost(dev.hostname) === normHost(exp.hostname) ? 'match' : 'mismatch';
    }
    // A hostname mismatch is a real discrepancy — never let it stay green.
    if (hostnameStatus === 'mismatch' && rag === 'green') {
      rag = 'amber';
      registerStatus = 'partial';
    }

    rows.push({
      ip: dev.ip,
      hostname: dev.hostname || exp.hostname || '',
      discoveredHostname: dev.hostname || '',
      expectedHostname: exp.hostname || '',
      hostnameStatus,
      hostnameSrc: dev.hostnameSrc || '',
      status: 'reachable',
      register: registerStatus,
      rag,
      latency: dev.latency,
      openPorts: dev.openPorts,
      services: dev.services,
      mac: dev.mac,
      vendor: dev.vendor || exp.vendor,
      model: exp.model,
      type: exp.type,
      banner: dev.banner,
      expectedPorts: exp.expectedPorts,
      missingPorts,
      extraPorts,
      project: exp.project,
      location: exp.location,
      description: exp.description,
      discoveredBy: dev.discoveredBy,
      expected: true,
    });
  }

  // Discovered hosts not in the register → rogue / unexpected.
  for (const dev of devices) {
    if (consumed.has(dev.ip)) continue;
    rows.push({
      ip: dev.ip,
      hostname: dev.hostname || '',
      discoveredHostname: dev.hostname || '',
      expectedHostname: '',
      hostnameStatus: 'na',
      hostnameSrc: dev.hostnameSrc || '',
      status: register.length ? 'rogue' : 'reachable',
      register: register.length ? 'rogue' : 'none',
      rag: register.length ? 'red' : 'none',
      latency: dev.latency,
      openPorts: dev.openPorts,
      services: dev.services,
      mac: dev.mac,
      vendor: dev.vendor,
      banner: dev.banner,
      expectedPorts: [],
      missingPorts: [],
      extraPorts: dev.openPorts,
      discoveredBy: dev.discoveredBy,
      expected: false,
    });
  }

  rows.sort((a, b) => {
    const ai = a.ip === '—' ? Infinity : scanner.ipToInt(a.ip);
    const bi = b.ip === '—' ? Infinity : scanner.ipToInt(b.ip);
    return ai - bi;
  });

  const summary = {
    expected: register.length,
    reachable: devices.length, // total live hosts found (incl. rogues)
    // How many of the EXPECTED devices are actually reachable — this, not the
    // raw live count, is the meaningful "% of expected" metric. Rogues are
    // counted in their own bucket and must not inflate this.
    expectedReachable: rows.filter((r) => r.expected && r.status === 'reachable').length,
    matches: rows.filter((r) => r.register === 'match').length,
    partial: rows.filter((r) => r.register === 'partial').length,
    missing: rows.filter((r) => r.register === 'missing').length,
    rogue: rows.filter((r) => r.register === 'rogue').length,
  };
  return { rows, summary };
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------
function send(res, code, body, headers = {}) {
  res.writeHead(code, { 'Cache-Control': 'no-store', ...headers });
  res.end(body);
}
function sendJson(res, code, obj) {
  send(res, code, JSON.stringify(obj), { 'Content-Type': 'application/json' });
}
function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => (data += c));
    req.on('end', () => resolve(data));
    req.on('error', () => resolve(data));
  });
}
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
  '.json': 'application/json',
};
function serveStatic(res, urlPath) {
  let rel = urlPath === '/' ? '/index.html' : urlPath;
  rel = rel.split('?')[0];
  // Normalise and prevent traversal, then look up under public/.
  const clean = path.posix.normalize(rel).replace(/^\/+/, '');
  if (clean.includes('..')) return send(res, 403, 'Forbidden');
  const buf = getAsset('public/' + clean);
  if (!buf) return send(res, 404, 'Not found');
  send(res, 200, buf, { 'Content-Type': MIME[path.extname(clean)] || 'application/octet-stream' });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  const p = url.pathname;

  try {
    if (p === '/api/adapters' && req.method === 'GET') {
      const adapters = await scanner.listAdapters();
      return sendJson(res, 200, { adapters });
    }

    if (p === '/api/template' && req.method === 'GET') {
      const buf = getAsset('template/ip-device-register-template.csv');
      if (!buf) return send(res, 404, 'Template not found');
      return send(res, 200, buf, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="ip-device-register-template.csv"',
      });
    }

    if (p === '/api/register' && req.method === 'POST') {
      const body = await readBody(req);
      let csvText = body;
      const ctype = req.headers['content-type'] || '';
      if (ctype.includes('application/json')) {
        try { csvText = JSON.parse(body).csv || ''; } catch (_) { csvText = ''; }
      }
      register = importRegister(csvText);
      return sendJson(res, 200, { imported: register.length, register });
    }

    if (p === '/api/register' && req.method === 'GET') {
      return sendJson(res, 200, { register });
    }

    if (p === '/api/register' && req.method === 'DELETE') {
      register = [];
      return sendJson(res, 200, { imported: 0 });
    }

    // SSE live scan
    if (p === '/api/scan' && req.method === 'GET') {
      const start = url.searchParams.get('start');
      const end = url.searchParams.get('end') || start;
      if (!start) return sendJson(res, 400, { error: 'start (and end) IP required' });
      const timeout = Math.max(200, Number(url.searchParams.get('timeout')) || 1000);
      const udpPorts = splitPorts(url.searchParams.get('udpPorts') || '');
      const hostConcurrency = Math.min(120, Math.max(4, Number(url.searchParams.get('concurrency')) || 24));
      // Always scan the full "all common services" set, unioned with every port
      // named by the imported register (so RAG is correct even for rare ports).
      const regPorts = [];
      for (const r of register) for (const p of r.expectedPorts) regPorts.push(p);
      const tcpPorts = Array.from(new Set([...scanner.DEFAULT_TCP_PORTS, ...regPorts])).sort((a, b) => a - b);

      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-store',
        Connection: 'keep-alive',
      });
      const emit = (event) => {
        try { res.write(`data: ${JSON.stringify(event)}\n\n`); } catch (_) {}
      };

      let cancelled = false;
      req.on('close', () => { cancelled = true; });

      const options = {
        range: { start, end },
        timeout,
        tcpPorts,
        udpPorts: udpPorts.length ? udpPorts : undefined,
        hostConcurrency,
      };

      scanner
        .scanNetwork(options, (event) => {
          if (cancelled) return;
          if (event.type === 'complete') {
            const result = compare(event.devices);
            emit({ type: 'result', ...result });
            emit({ type: 'complete' });
            res.end();
          } else {
            emit(event);
          }
        })
        .catch((e) => {
          emit({ type: 'error', message: String(e && e.message ? e.message : e) });
          try { res.end(); } catch (_) {}
        });
      return;
    }

    // Recompute RAG against current register without rescanning
    if (p === '/api/compare' && req.method === 'POST') {
      const body = await readBody(req);
      let devices = [];
      try { devices = JSON.parse(body).devices || []; } catch (_) {}
      return sendJson(res, 200, compare(devices));
    }

    if (p === '/api/health' && req.method === 'GET') {
      return sendJson(res, 200, { ok: true, version: '1.0.0' });
    }

    // Static files
    if (req.method === 'GET') return serveStatic(res, p);

    send(res, 405, 'Method not allowed');
  } catch (e) {
    sendJson(res, 500, { error: String(e && e.message ? e.message : e) });
  }
});

server.listen(PORT, HOST, () => {
  const uiUrl = `http://${HOST}:${PORT}/`;
  console.log('');
  console.log('  ==================================================');
  console.log('   Network IP Scanner  —  running');
  console.log(`   Open your browser at:  ${uiUrl}`);
  console.log('   (Close this window to stop the scanner.)');
  console.log('  ==================================================');
  console.log('');
  // Auto-open the default browser on Windows.
  if (process.platform === 'win32' && !process.env.NO_OPEN) {
    try { spawn('cmd', ['/c', 'start', '""', uiUrl], { detached: true, stdio: 'ignore', windowsHide: true }); } catch (_) {}
  }
});
