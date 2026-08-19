'use strict';
/*
 * server.js — local HTTP server + REST/SSE API for the BACnet Scanner UI.
 * Zero external dependencies (pure Node http) so it packages to a single .exe
 * cleanly. Binds to loopback only. All BACnet traffic goes through one shared
 * UDP client (bacnet.js) bound to port 47808.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const bacnet = require('./bacnet');
const archive = require('./archive');

const PORT = Number(process.env.PORT) || 3778;
const HOST = '127.0.0.1';

// Asset resolution — embedded in the SEA build (global.__ASSETS__) or loose on
// disk in dev. getAsset() hides the difference.
const BASE = __dirname;
const EMBEDDED = typeof global.__ASSETS__ === 'object' ? global.__ASSETS__ : null;
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

// ---------------------------------------------------------------------------
// Shared BACnet client (lazy, one socket on UDP 47808 for the whole process)
// ---------------------------------------------------------------------------
let client = null;
let clientReady = null;
async function getClient() {
  if (client) { await clientReady; return client; }
  client = bacnet.createClient();
  clientReady = client.bind().catch((e) => {
    // Bind failed (port busy / blocked). Surface a helpful message downstream.
    const err = new Error(
      'Could not bind UDP port 47808 (' + (e && e.message ? e.message : e) +
      '). Close any other BACnet software using this port, then rescan.'
    );
    client = null;
    throw err;
  });
  await clientReady;
  return client;
}

// In-memory imported register (single local operator).
let register = []; // [{ instance, name, network, ip, vendor, model, location, expectedObjects, description }]

// ---------------------------------------------------------------------------
// CSV parsing (RFC4180-ish, handles quoted fields)
// ---------------------------------------------------------------------------
function parseCsv(text) {
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQuotes = false; }
      else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\r') { /* skip */ }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.length && r.some((v) => v.trim() !== ''));
}

function importRegister(csvText) {
  const rows = parseCsv(csvText);
  if (!rows.length) return [];
  const header = rows[0].map((h) => h.trim().toLowerCase());
  const idx = (names) => { for (const n of names) { const i = header.indexOf(n); if (i !== -1) return i; } return -1; };
  const col = {
    instance: idx(['device instance', 'instance', 'device id', 'device_id', 'deviceinstance']),
    name: idx(['device name', 'name', 'object name', 'hostname']),
    network: idx(['network', 'net', 'dnet']),
    ip: idx(['ip address', 'ip', 'address']),
    vendor: idx(['vendor', 'manufacturer', 'make']),
    model: idx(['model', 'model name']),
    location: idx(['location', 'area', 'zone']),
    objects: idx(['expected objects', 'expected_objects', 'objects', 'object count']),
    description: idx(['description', 'notes', 'desc']),
  };
  const out = [];
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r];
    const get = (i) => (i >= 0 && i < row.length ? row[i].trim() : '');
    const instRaw = get(col.instance);
    const name = get(col.name);
    if (!instRaw && !name) continue;
    const instance = instRaw === '' ? null : parseInt(instRaw, 10);
    const objRaw = get(col.objects);
    out.push({
      instance: Number.isFinite(instance) ? instance : null,
      name,
      network: get(col.network),
      ip: get(col.ip),
      vendor: get(col.vendor),
      model: get(col.model),
      location: get(col.location),
      expectedObjects: objRaw && Number.isFinite(parseInt(objRaw, 10)) ? parseInt(objRaw, 10) : null,
      description: get(col.description),
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// RAG comparison: discovered BACnet devices vs imported register (keyed on
// device instance — globally unique across a BACnet internetwork).
// ---------------------------------------------------------------------------
/*
 *   GREEN  (match)   — expected device present; object count matches (if given);
 *                      name/model match (if given).
 *   AMBER  (partial) — present, but object count differs OR name/model mismatch.
 *   RED    (missing) — expected device not discovered.
 *   RED    (rogue)   — discovered device not in the register.
 */
function norm(s) { return String(s || '').trim().toLowerCase(); }

function compare(devices) {
  const devByInst = {};
  for (const d of devices) devByInst[d.instance] = d;
  const rows = [];
  const consumed = new Set();

  for (const exp of register) {
    const dev = exp.instance != null ? devByInst[exp.instance] : null;
    if (dev) consumed.add(dev.instance);

    if (!dev) {
      rows.push({
        instance: exp.instance != null ? exp.instance : '—',
        name: exp.name || '',
        expectedName: exp.name || '',
        network: exp.network || '',
        ip: exp.ip || '—',
        vendor: exp.vendor || '',
        model: exp.model || '',
        firmware: '', appSoftware: '', location: exp.location || '',
        systemStatus: '', objectCount: null,
        status: 'unreachable', register: 'missing', rag: 'red',
        expectedObjects: exp.expectedObjects, objectDiff: '',
        nameStatus: 'na', description: exp.description || '', expected: true,
      });
      continue;
    }

    const notes = [];
    let rag = 'green', registerStatus = 'match';
    // Object-count check
    let objectDiff = '';
    if (exp.expectedObjects != null && dev.objectCount != null && dev.objectCount !== exp.expectedObjects) {
      rag = 'amber'; registerStatus = 'partial';
      objectDiff = `expected ${exp.expectedObjects}, found ${dev.objectCount}`;
      notes.push('object-count');
    }
    // Name check
    let nameStatus = 'na';
    if (exp.name) {
      nameStatus = norm(dev.name) === norm(exp.name) ? 'match' : (dev.name ? 'mismatch' : 'unverified');
      if (nameStatus === 'mismatch') { rag = 'amber'; registerStatus = 'partial'; notes.push('name'); }
    }
    // Model check
    if (exp.model && dev.model && norm(dev.model) !== norm(exp.model)) {
      rag = 'amber'; registerStatus = 'partial'; notes.push('model');
    }

    rows.push({
      instance: dev.instance,
      name: dev.name || exp.name || '',
      expectedName: exp.name || '',
      network: dev.network || '',
      ip: `${dev.ip}${dev.port && dev.port !== bacnet.BACNET_PORT ? ':' + dev.port : ''}`,
      vendor: dev.vendor || exp.vendor || '',
      model: dev.model || exp.model || '',
      firmware: dev.firmware || '',
      appSoftware: dev.appSoftware || '',
      location: dev.location || exp.location || '',
      systemStatus: dev.systemStatus || '',
      protocolRevision: dev.protocolRevision,
      maxApdu: dev.maxApdu,
      segmentation: dev.segmentation,
      vendorId: dev.vendorId,
      mac: dev.mac,
      objectCount: dev.objectCount,
      status: 'reachable', register: registerStatus, rag,
      expectedObjects: exp.expectedObjects, objectDiff,
      nameStatus, mismatch: notes.join(', '),
      description: dev.description || exp.description || '', expected: true,
    });
  }

  for (const dev of devices) {
    if (consumed.has(dev.instance)) continue;
    rows.push({
      instance: dev.instance,
      name: dev.name || '',
      expectedName: '',
      network: dev.network || '',
      ip: `${dev.ip}${dev.port && dev.port !== bacnet.BACNET_PORT ? ':' + dev.port : ''}`,
      vendor: dev.vendor || '',
      model: dev.model || '',
      firmware: dev.firmware || '',
      appSoftware: dev.appSoftware || '',
      location: dev.location || '',
      systemStatus: dev.systemStatus || '',
      protocolRevision: dev.protocolRevision,
      maxApdu: dev.maxApdu,
      segmentation: dev.segmentation,
      vendorId: dev.vendorId,
      mac: dev.mac,
      objectCount: dev.objectCount,
      status: register.length ? 'rogue' : 'reachable',
      register: register.length ? 'rogue' : 'none',
      rag: register.length ? 'red' : 'none',
      expectedObjects: null, objectDiff: '', nameStatus: 'na',
      description: dev.description || '', expected: false,
    });
  }

  rows.sort((a, b) => {
    const ai = a.instance === '—' ? Infinity : Number(a.instance);
    const bi = b.instance === '—' ? Infinity : Number(b.instance);
    return ai - bi;
  });

  const summary = {
    expected: register.length,
    discovered: devices.length,
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
function sendJson(res, code, obj) { send(res, code, JSON.stringify(obj), { 'Content-Type': 'application/json' }); }
function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => (data += c));
    req.on('end', () => resolve(data));
    req.on('error', () => resolve(data));
  });
}
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
  '.png': 'image/png', '.json': 'application/json',
};
function serveStatic(res, urlPath) {
  let rel = urlPath === '/' ? '/index.html' : urlPath;
  rel = rel.split('?')[0];
  const clean = path.posix.normalize(rel).replace(/^\/+/, '');
  if (clean.includes('..')) return send(res, 403, 'Forbidden');
  const buf = getAsset('public/' + clean);
  if (!buf) return send(res, 404, 'Not found');
  send(res, 200, buf, { 'Content-Type': MIME[path.extname(clean)] || 'application/octet-stream' });
}

// ---------------------------------------------------------------------------
// Concurrency helper
// ---------------------------------------------------------------------------
async function pool(items, limit, worker, onEach) {
  let idx = 0;
  async function run() {
    while (idx < items.length) {
      const cur = idx++;
      try { await worker(items[cur], cur); } catch (_) {}
      if (onEach) onEach(cur);
    }
  }
  const runners = [];
  for (let i = 0; i < Math.min(limit, items.length); i++) runners.push(run());
  await Promise.all(runners);
}

// ---------------------------------------------------------------------------
// Asset export: per device build a JSON + XLSX of its points and current
// values, arranged one folder per asset inside a single ZIP.
// ---------------------------------------------------------------------------
function buildDeviceFiles(dev, objResult, exportedAt) {
  const base = archive.sanitizeFilename((dev.name || ('device_' + dev.instance))) + '_' + dev.instance;
  const points = (objResult.objects || []).map((o) => ({
    objectType: o.typeName,
    objectInstance: o.instance,
    name: o.name,
    presentValue: o.presentValue,
    units: o.units,
  }));

  const json = {
    asset: dev.name || '',
    deviceInstance: dev.instance,
    address: dev.ip + (dev.port && dev.port !== bacnet.BACNET_PORT ? ':' + dev.port : ''),
    network: dev.network || 0,
    vendor: dev.vendor || '',
    model: dev.model || '',
    firmware: dev.firmware || '',
    objectCount: objResult.count,
    pointsExported: points.length,
    truncated: !!objResult.truncated,
    exportedAt,
    points,
  };

  const rows = [
    ['BACnet Asset Export'],
    ['Asset', dev.name || ''],
    ['Device Instance', dev.instance],
    ['Address', json.address],
    ['Network', dev.network || 0],
    ['Vendor', dev.vendor || ''],
    ['Model', dev.model || ''],
    ['Firmware', dev.firmware || ''],
    ['Object Count', objResult.count],
    ['Points Exported', points.length + (objResult.truncated ? ' (truncated)' : '')],
    ['Exported', exportedAt],
    [],
    ['Object Type', 'Instance', 'Object Name', 'Present Value', 'Units'],
    ...points.map((p) => [p.objectType, p.objectInstance, p.name, p.presentValue, p.units]),
  ];

  return {
    base,
    files: [
      { name: `${base}/${base}.json`, data: Buffer.from(JSON.stringify(json, null, 2), 'utf8') },
      { name: `${base}/${base}.xlsx`, data: archive.buildXlsx(rows) },
    ],
  };
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  const p = url.pathname;

  try {
    if (p === '/api/adapters' && req.method === 'GET') {
      const adapters = await bacnet.listAdapters();
      return sendJson(res, 200, { adapters });
    }

    if (p === '/api/template' && req.method === 'GET') {
      const buf = getAsset('template/bacnet-device-register-template.csv');
      if (!buf) return send(res, 404, 'Template not found');
      return send(res, 200, buf, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="bacnet-device-register-template.csv"',
      });
    }

    if (p === '/api/register' && req.method === 'POST') {
      const body = await readBody(req);
      let csvText = body;
      const ctype = req.headers['content-type'] || '';
      if (ctype.includes('application/json')) { try { csvText = JSON.parse(body).csv || ''; } catch (_) { csvText = ''; } }
      register = importRegister(csvText);
      return sendJson(res, 200, { imported: register.length, register });
    }
    if (p === '/api/register' && req.method === 'GET') return sendJson(res, 200, { register });
    if (p === '/api/register' && req.method === 'DELETE') { register = []; return sendJson(res, 200, { imported: 0 }); }

    // SSE live discovery
    if (p === '/api/scan' && req.method === 'GET') {
      const adapters = await bacnet.listAdapters();
      const adapterIdx = Number(url.searchParams.get('adapter'));
      const adapter = adapters[adapterIdx] || adapters[0];
      if (!adapter) return sendJson(res, 400, { error: 'No network adapter available' });
      const broadcast = url.searchParams.get('broadcast') || adapter.broadcast;
      const timeoutMs = Math.min(20000, Math.max(1000, Number(url.searchParams.get('discoverMs')) || 4000));
      const readTimeout = Math.min(6000, Math.max(500, Number(url.searchParams.get('timeout')) || 1500));
      const lowRaw = url.searchParams.get('low');
      const highRaw = url.searchParams.get('high');
      const low = lowRaw !== null && lowRaw !== '' ? parseInt(lowRaw, 10) : null;
      const high = highRaw !== null && highRaw !== '' ? parseInt(highRaw, 10) : null;

      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
      const emit = (event) => { try { res.write(`data: ${JSON.stringify(event)}\n\n`); } catch (_) {} };
      let cancelled = false;
      req.on('close', () => { cancelled = true; });

      (async () => {
        let cl;
        try { cl = await getClient(); }
        catch (e) { emit({ type: 'error', message: String(e.message || e) }); return res.end(); }

        emit({ type: 'start', adapter: adapter.description, broadcast, discoverMs: timeoutMs });

        const networks = [];
        const devices = await cl.discover({
          broadcast,
          timeoutMs,
          low: Number.isFinite(low) ? low : null,
          high: Number.isFinite(high) ? high : null,
          onDevice: (d) => { if (!cancelled) emit({ type: 'device', device: d }); },
          onRouter: (r) => {
            if (cancelled) return;
            for (const n of r.networks) if (!networks.includes(n)) networks.push(n);
            emit({ type: 'router', router: r.ip, networks: r.networks });
          },
        });
        if (cancelled) return;
        emit({ type: 'discovered', count: devices.length, networks });

        // Enrich each device with identity properties (bounded concurrency).
        let done = 0;
        await pool(devices, 4, async (dev) => {
          if (cancelled) return;
          try {
            const id = await bacnet.readDeviceIdentity(cl, dev, readTimeout);
            Object.assign(dev, id);
          } catch (_) {}
          if (!cancelled) emit({ type: 'device-update', device: dev });
        }, () => { done++; if (!cancelled) emit({ type: 'progress', phase: 'identify', done, total: devices.length }); });

        if (cancelled) return;
        const result = compare(devices);
        emit({ type: 'result', ...result });
        emit({ type: 'complete' });
        res.end();
      })().catch((e) => {
        emit({ type: 'error', message: String(e && e.message ? e.message : e) });
        try { res.end(); } catch (_) {}
      });
      return;
    }

    // On-demand object-list enumeration for one device (SSE — can be slow).
    if (p === '/api/objects' && req.method === 'GET') {
      const instance = parseInt(url.searchParams.get('instance'), 10);
      const ip = url.searchParams.get('ip');
      const port = Number(url.searchParams.get('port')) || bacnet.BACNET_PORT;
      const network = Number(url.searchParams.get('network')) || 0;
      const mac = url.searchParams.get('mac') || '';
      const cap = Math.min(1000, Math.max(1, Number(url.searchParams.get('cap')) || 200));
      const readTimeout = Math.min(6000, Math.max(500, Number(url.searchParams.get('timeout')) || 1500));
      if (!Number.isFinite(instance) || !ip) return sendJson(res, 400, { error: 'instance and ip required' });

      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
      const emit = (event) => { try { res.write(`data: ${JSON.stringify(event)}\n\n`); } catch (_) {} };
      let cancelled = false;
      req.on('close', () => { cancelled = true; });

      (async () => {
        let cl;
        try { cl = await getClient(); }
        catch (e) { emit({ type: 'error', message: String(e.message || e) }); return res.end(); }
        const dev = { instance, ip, port, network, mac };
        const result = await bacnet.readObjectList(cl, dev, {
          cap, timeout: readTimeout,
          onProgress: (n, total) => { if (!cancelled) emit({ type: 'progress', done: n, total }); },
        });
        if (cancelled) return;
        emit({ type: 'objects', ...result });
        emit({ type: 'complete' });
        res.end();
      })().catch((e) => { emit({ type: 'error', message: String(e && e.message ? e.message : e) }); try { res.end(); } catch (_) {} });
      return;
    }

    // Asset export (POST body {devices:[...]}) — streams SSE progress, final
    // event carries the built ZIP as base64. Reads each selected device's full
    // object list (names + present values) before packaging.
    if (p === '/api/export' && req.method === 'POST') {
      const body = await readBody(req);
      let devices = [];
      try { devices = JSON.parse(body).devices || []; } catch (_) {}

      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
      const emit = (event) => { try { res.write(`data: ${JSON.stringify(event)}\n\n`); } catch (_) {} };
      let cancelled = false;
      req.on('close', () => { cancelled = true; });

      (async () => {
        if (!devices.length) { emit({ type: 'error', message: 'No devices selected' }); return res.end(); }
        let cl;
        try { cl = await getClient(); }
        catch (e) { emit({ type: 'error', message: String(e.message || e) }); return res.end(); }

        const exportedAt = new Date().toISOString();
        const zipEntries = [];
        let totalPoints = 0;
        for (let i = 0; i < devices.length; i++) {
          if (cancelled) return;
          const dev = devices[i];
          emit({ type: 'device', index: i, total: devices.length, instance: dev.instance, name: dev.name || '' });
          let objResult;
          try {
            objResult = await bacnet.readObjectList(cl, dev, {
              cap: 2000, timeout: 1500,
              onProgress: (done, total) => { if (!cancelled) emit({ type: 'objprogress', index: i, done, total }); },
            });
          } catch (e) {
            objResult = { objects: [], count: 0, truncated: false, error: String(e && e.message ? e.message : e) };
          }
          const built = buildDeviceFiles(dev, objResult, exportedAt);
          zipEntries.push(...built.files);
          totalPoints += (objResult.objects || []).length;
          emit({ type: 'device-done', index: i, name: dev.name || '', points: (objResult.objects || []).length, error: objResult.error || null });
        }
        if (cancelled) return;

        emit({ type: 'packaging' });
        const zip = archive.makeZip(zipEntries);
        const stamp = exportedAt.replace(/[:T]/g, '-').replace(/\..+/, '');
        const filename = `bacnet-export-${stamp}.zip`;
        emit({ type: 'ready', filename, assets: devices.length, points: totalPoints, size: zip.length, base64: zip.toString('base64') });
        res.end();
      })().catch((e) => { emit({ type: 'error', message: String(e && e.message ? e.message : e) }); try { res.end(); } catch (_) {} });
      return;
    }

    if (p === '/api/compare' && req.method === 'POST') {
      const body = await readBody(req);
      let devices = [];
      try { devices = JSON.parse(body).devices || []; } catch (_) {}
      return sendJson(res, 200, compare(devices));
    }

    if (p === '/api/health' && req.method === 'GET') return sendJson(res, 200, { ok: true, version: '1.0.0' });

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
  console.log('   BACnet Scanner  —  running');
  console.log(`   Open your browser at:  ${uiUrl}`);
  console.log('   (Close this window to stop the scanner.)');
  console.log('  ==================================================');
  console.log('');
  if (process.platform === 'win32' && !process.env.NO_OPEN) {
    try { spawn('cmd', ['/c', 'start', '""', uiUrl], { detached: true, stdio: 'ignore', windowsHide: true }); } catch (_) {}
  }
});
