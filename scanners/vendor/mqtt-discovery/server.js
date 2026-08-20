'use strict';
/*
 * server.js — local HTTP server + REST/SSE API for the MQTT Discovery UI.
 *
 * The browser talks only to this loopback server; the server holds the live
 * MQTT connection to the broker, aggregates a topic TREE (assets nested within
 * their topic paths), keeps a short per-topic payload history, and streams
 * throttled snapshots + a fast "activity" pulse back to the browser over SSE
 * (so a broker doing thousands of messages a second never floods the UI).
 *
 * Zero external runtime dependencies (pure Node http + our own modules) so it
 * packages to a single SEA .exe cleanly. Binds to loopback only.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { MqttClient } = require('./mqtt-client');
const udmi = require('./udmi');
const { Zip } = require('./zip');

const PORT = Number(process.env.PORT) || 3799;
const HOST = '127.0.0.1';

const BASE = __dirname;
const EMBEDDED = typeof global.__ASSETS__ === 'object' ? global.__ASSETS__ : null;
const TEMPLATE_KEY = 'template/mqtt-register-template.csv';

function getAsset(key) {
  if (EMBEDDED) { const a = EMBEDDED[key]; return a ? Buffer.from(a.data, a.enc || 'base64') : null; }
  const abs = path.join(BASE, key);
  if (!abs.startsWith(BASE)) return null;
  try { return fs.readFileSync(abs); } catch (_) { return null; }
}

// ---------------------------------------------------------------------------
// Live discovery state
// ---------------------------------------------------------------------------
const HISTORY_PER_TOPIC = 8;    // rolling payload history kept per topic
const RAW_CAP = 32768;          // max stored characters per payload
const TREE_NODE_CAP = 20000;    // safety cap on serialized tree nodes

let client = null;
let connection = { status: 'disconnected', host: '', port: 0, tls: false, rootFilter: '', qos: 1, error: '', since: 0 };
let register = { assets: [], points: 0, byKey: new Map(), byTopic: new Map() };

const topics = new Map();       // topic → aggregate
const assets = new Map();       // asset key → aggregate
const pathAsset = new Map();    // device-path → asset id learned from a payload
const activeTopics = new Set(); // topics that saw traffic since the last activity pulse

let focusAsset = '';
let searchQuery = '';
let matchedOnly = false;

function resetState() {
  topics.clear();
  assets.clear();
  pathAsset.clear();
  activeTopics.clear();
  focusAsset = '';
}

function assetKey(a) { return String(a || '').toUpperCase(); }

function ensureAsset(assetId) {
  const key = assetKey(assetId);
  let a = assets.get(key);
  if (!a) {
    const reg = register.byKey.get(key);
    a = {
      asset: assetId, key,
      topics: new Set(),
      count: 0, rate: 0, _sec: 0,
      schema: '', lastTopic: '', lastTs: 0,
      livePoints: new Map(),   // name → { value, unit, ts }
      matched: register.byKey.has(key),
      meta: reg || null,
      udmi: null,
    };
    assets.set(key, a);
  }
  return a;
}

function onMessage(m) {
  const now = Date.now();
  let text = m.message.toString('utf8');
  if (text.length > RAW_CAP) text = text.slice(0, RAW_CAP);
  const obj = udmi.tryParseJson(m.message);
  const schema = udmi.detectSchema(obj, m.message);
  const env = obj ? udmi.parseEnvelope(obj) : null;

  const dpath = udmi.topicDevicePath(m.topic);
  if (env && env.asset) pathAsset.set(dpath, env.asset);
  const assetId = (env && env.asset) || pathAsset.get(dpath)
    || register.byTopic.get(m.topic) || udmi.assetFromTopic(m.topic);

  // topic aggregate + history ring
  let t = topics.get(m.topic);
  if (!t) {
    t = { topic: m.topic, asset: assetId, devicePath: dpath, count: 0, rate: 0, _sec: 0, schema, firstSeen: now, lastSeen: 0, lastText: '', retained: false, issues: [], history: [] };
    topics.set(m.topic, t);
  }
  t.count++; t._sec++; t.lastSeen = now; t.schema = schema; t.retained = m.retain; t.asset = assetId; t.lastText = text;
  t.history.push({ ts: now, raw: text });
  if (t.history.length > HISTORY_PER_TOPIC) t.history.shift();
  activeTopics.add(m.topic);

  // asset aggregate
  const a = ensureAsset(assetId);
  a.topics.add(m.topic);
  a.count++; a._sec++; a.lastTopic = m.topic; a.lastTs = now; a.schema = schema;

  if (env) {
    a.udmi = a.udmi || {};
    for (const k of ['site', 'room', 'guid', 'gatewayId', 'version']) if (env[k]) a.udmi[k] = env[k];
    if (env.proxyIds && env.proxyIds.length) a.udmi.proxyIds = env.proxyIds;
  }

  const pts = env ? env.points : [];
  for (const p of pts) a.livePoints.set(udmi.normPoint(p.name), { name: p.name, value: p.value, unit: p.unit, ts: now });

  t.issues = udmi.schemaIssues(obj, schema);
}

// 1-second aggregation tick: smooth per-second counters into msg/s.
const ALPHA = 0.5;
setInterval(() => {
  for (const t of topics.values()) { t.rate = t.rate * (1 - ALPHA) + t._sec * ALPHA; t._sec = 0; }
  for (const a of assets.values()) { a.rate = a.rate * (1 - ALPHA) + a._sec * ALPHA; a._sec = 0; }
}, 1000).unref();

// ---------------------------------------------------------------------------
// Tree building (assets nested within their topic paths)
// ---------------------------------------------------------------------------
function topicMatches(t, q, matchOnly) {
  if (matchOnly && !register.byKey.has(assetKey(t.asset))) return false;
  if (q) {
    const hay = (t.topic + ' ' + t.asset + ' ' + (t.lastText || '')).toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function buildTree(q, matchOnly) {
  const root = { children: new Map() };
  let shown = 0;
  for (const t of topics.values()) {
    if (!topicMatches(t, q, matchOnly)) continue;
    shown++;
    const segs = t.topic.split('/').filter(Boolean);
    let node = root, acc = '';
    const isMatched = register.byKey.has(assetKey(t.asset));
    for (let i = 0; i < segs.length; i++) {
      acc = acc ? acc + '/' + segs[i] : segs[i];
      let child = node.children.get(segs[i]);
      if (!child) { child = { name: segs[i], path: acc, children: new Map(), t: 0, m: 0, r: 0, matched: false }; node.children.set(segs[i], child); }
      child.t += 1; child.m += t.count; child.r += t.rate; child.matched = child.matched || isMatched;
      if (acc === t.devicePath) { child.asset = t.asset; child.device = true; }
      node = child;
    }
    node.leaf = true; node.asset = t.asset; node.schema = t.schema; node.retained = t.retained;
    node.count = t.count; node.rate = t.rate; node.issues = (t.issues && t.issues.length) || 0;
  }
  return { root, shown };
}

let _nodeBudget = 0;
function serializeChildren(node) {
  const kids = Array.from(node.children.values());
  kids.sort((a, b) => b.r - a.r || a.name.localeCompare(b.name));
  const out = [];
  for (const k of kids) {
    if (_nodeBudget-- <= 0) break;
    const o = { n: k.name, p: k.path, t: k.t, m: k.m, r: round1(k.r), mt: k.matched ? 1 : 0 };
    if (k.device) o.dev = 1;
    if (k.asset) o.a = k.asset;
    if (k.leaf) { o.leaf = 1; o.sc = k.schema; if (k.retained) o.ret = 1; o.c = k.count; if (k.issues) o.iss = k.issues; }
    if (k.children.size) o.ch = serializeChildren(k);
    out.push(o);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Snapshot
// ---------------------------------------------------------------------------
function matchedAssetCount() { let n = 0; for (const a of assets.values()) if (a.matched) n++; return n; }
function countIssueTopics() { let n = 0; for (const t of topics.values()) if (t.issues && t.issues.length) n += t.issues.length; return n; }
function sumMessages() { let n = 0; for (const t of topics.values()) n += t.count; return n; }
function round1(n) { return Math.round(n * 10) / 10; }

function buildSnapshot() {
  const { root, shown } = buildTree(searchQuery, matchedOnly);
  _nodeBudget = TREE_NODE_CAP;
  const tree = serializeChildren(root);

  const stats = {
    expectedAssets: register.assets.length,
    subscribedAssets: matchedAssetCount(),
    liveAssets: assets.size,
    topicsDiscovered: topics.size,
    issues: countIssueTopics(),
    totalMessages: sumMessages(),
  };

  let focused = null;
  const a = focusAsset ? assets.get(focusAsset) : null;
  if (a) focused = buildFocused(a);

  return { type: 'snapshot', status: connection, stats, tree, treeShown: shown, totalTopics: topics.size, filtered: !!(searchQuery || matchedOnly), focused };
}

function latestPayloadFor(a) {
  let raw = '', ts = 0, topic = '';
  for (const tp of a.topics) {
    const t = topics.get(tp);
    if (t && t.lastSeen > ts) { ts = t.lastSeen; raw = t.history.length ? t.history[t.history.length - 1].raw : t.lastText; topic = tp; }
  }
  return { raw, topic };
}

function configTopicFor(a) {
  if (!a.topics.size) return '';
  const dp = udmi.topicDevicePath(Array.from(a.topics)[0]);
  return dp ? dp + '/config' : '';
}

function buildFocused(a) {
  const livePoints = Array.from(a.livePoints.values()).map((p) => ({ name: p.name, value: p.value, unit: p.unit, ts: p.ts }));
  const reg = register.byKey.get(a.key);
  const comparison = udmi.comparePoints(reg ? reg.points : [], livePoints.map((p) => p.name));
  const latest = latestPayloadFor(a);

  const topicsDetail = Array.from(a.topics).map((tp) => {
    const t = topics.get(tp) || {};
    return { topic: tp, schema: t.schema || '', count: t.count || 0, rate: round1(t.rate || 0), retained: !!t.retained, history: (t.history || []).map((h) => ({ ts: h.ts, raw: h.raw })) };
  });

  const cfgTopic = configTopicFor(a);
  const cfgT = topics.get(cfgTopic);
  const configPayload = cfgT ? (cfgT.history.length ? cfgT.history[cfgT.history.length - 1].raw : cfgT.lastText) : '';

  let issues = 0;
  for (const tp of a.topics) { const t = topics.get(tp); if (t && t.issues) issues += t.issues.length; }

  return {
    asset: a.asset, key: a.key, matched: a.matched, schema: a.schema,
    rate: round1(a.rate), count: a.count,
    topics: Array.from(a.topics), topicsDetail, lastTopic: latest.topic || a.lastTopic,
    livePoints, lastPayload: latest.raw || '', issues, comparison,
    meta: reg || null, udmi: a.udmi || {},
    configTopic: cfgTopic, configPayload,
  };
}

// ---------------------------------------------------------------------------
// Register CSV generation (Save as Register)
// ---------------------------------------------------------------------------
function csvCell(v) { return `"${String(v == null ? '' : v).replace(/"/g, '""')}"`; }
function dataTypeOf(v) { if (typeof v === 'number') return 'analog'; if (typeof v === 'boolean') return 'binary'; if (v == null) return ''; return 'string'; }
function generateRegisterCsv() {
  const header = ['Asset', 'Topic', 'Type', 'Point', 'Unit', 'Data Type', 'Schema', 'Site', 'Location', 'Description'];
  const lines = [header.join(',')];
  const ordered = Array.from(assets.values()).sort((a, b) => a.asset.localeCompare(b.asset));
  for (const a of ordered) {
    const reg = register.byKey.get(a.key) || {};
    const um = a.udmi || {};
    const topic = a.lastTopic || (a.topics.size ? Array.from(a.topics)[0] : '') || (reg.topic || '');
    const site = reg.site || um.site || udmi.siteFromTopic(topic) || '';
    const location = reg.location || um.room || '';
    const pts = Array.from(a.livePoints.values());
    if (!pts.length) { lines.push([a.asset, topic, reg.type || '', '', '', '', a.schema || '', site, location, reg.description || ''].map(csvCell).join(',')); continue; }
    for (const pt of pts) lines.push([a.asset, topic, reg.type || '', pt.name, pt.unit || '', dataTypeOf(pt.value), a.schema || '', site, location, ''].map(csvCell).join(','));
  }
  return lines.join('\r\n') + '\r\n';
}

// ---------------------------------------------------------------------------
// Export archive (ZIP of every asset's live + historic payloads)
// ---------------------------------------------------------------------------
function buildExportZip(nowMs) {
  const z = new Zip(nowMs);
  const manifestAssets = [];
  for (const a of Array.from(assets.values()).sort((x, y) => x.asset.localeCompare(y.asset))) {
    const reg = register.byKey.get(a.key) || {};
    const um = a.udmi || {};
    for (const tp of a.topics) {
      const t = topics.get(tp);
      if (!t) continue;
      const base = tp.split('/').filter(Boolean).join('/'); // mirror topic path
      const hist = t.history || [];
      const live = hist.length ? hist[hist.length - 1].raw : (t.lastText || '');
      z.add(base + '.json', live || '{}');
      if (hist.length > 1) {
        hist.forEach((h, i) => z.add(base + '.history/' + String(i + 1).padStart(2, '0') + '.json', h.raw || '{}'));
      }
    }
    manifestAssets.push({
      asset: a.asset, matched: a.matched, schema: a.schema,
      site: reg.site || um.site || '', room: reg.location || um.room || '',
      gatewayId: um.gatewayId || '', topics: Array.from(a.topics),
      points: Array.from(a.livePoints.values()).map((p) => ({ name: p.name, value: p.value, unit: p.unit })),
    });
  }
  const manifest = {
    tool: 'SCA MQTT Discovery', version: '2.0.0',
    exportedAt: new Date(nowMs).toISOString(),
    broker: { host: connection.host, port: connection.port, tls: connection.tls, rootFilter: connection.rootFilter },
    assetCount: assets.size, topicCount: topics.size,
    assets: manifestAssets,
  };
  z.add('_manifest.json', JSON.stringify(manifest, null, 2));
  return z.build();
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------
const sseClients = new Set();
setInterval(() => {
  if (!sseClients.size) return;
  const data = `data: ${JSON.stringify(buildSnapshot())}\n\n`;
  for (const res of sseClients) { try { res.write(data); } catch (_) {} }
}, 1000).unref();

// Fast activity pulse for the tree flash.
setInterval(() => {
  if (!sseClients.size) { activeTopics.clear(); return; }
  if (!activeTopics.size) return;
  const paths = Array.from(activeTopics).slice(0, 500);
  activeTopics.clear();
  const data = `data: ${JSON.stringify({ type: 'activity', paths })}\n\n`;
  for (const res of sseClients) { try { res.write(data); } catch (_) {} }
}, 350).unref();

// ---------------------------------------------------------------------------
// Connection lifecycle
// ---------------------------------------------------------------------------
function splitFilters(s) { return String(s || '').split(',').map((x) => x.trim()).filter(Boolean); }

function doConnect(cfg) {
  return new Promise((resolve) => {
    if (client) { try { client.end(true); } catch (_) {} client = null; }
    resetState();

    const rootFilter = cfg.rootFilter || '#';
    const qos = Number(cfg.qos) || 0;
    connection = { status: 'connecting', host: cfg.host, port: Number(cfg.port) || (cfg.tls ? 8883 : 1883), tls: !!cfg.tls, rootFilter, qos, error: '', since: 0 };

    const opts = {
      host: cfg.host, port: connection.port, tls: !!cfg.tls,
      rejectUnauthorized: cfg.rejectUnauthorized !== false && cfg.validateCert !== false,
      username: cfg.username || undefined, password: cfg.password || undefined,
      clientId: cfg.clientId || ('sca-mqtt-' + Math.floor(process.pid % 100000)),
      keepAlive: 60, reconnectPeriod: 4000,
    };
    if (cfg.ca) opts.ca = cfg.ca;
    if (cfg.cert) opts.cert = cfg.cert;
    if (cfg.key) opts.key = cfg.key;

    let settled = false;
    client = new MqttClient(opts);
    client.on('connect', () => {
      connection.status = 'connected'; connection.error = ''; connection.since = Date.now();
      for (const f of splitFilters(rootFilter)) client.subscribe(f, qos);
      if (!settled) { settled = true; resolve({ ok: true, status: connection }); }
    });
    client.on('message', onMessage);
    client.on('error', (e) => {
      connection.error = String(e && e.message ? e.message : e);
      if (connection.status !== 'connected') connection.status = 'error';
      if (!settled) { settled = true; resolve({ ok: false, status: connection, error: connection.error }); }
    });
    client.on('close', () => { if (connection.status === 'connected') connection.status = 'reconnecting'; });
    client.on('reconnect', () => { if (connection.status !== 'connected') connection.status = 'reconnecting'; });
    client.connect();

    setTimeout(() => { if (!settled) { settled = true; resolve({ ok: connection.status === 'connected', status: connection, error: connection.error }); } }, 9000);
  });
}

function doDisconnect() { if (client) { try { client.end(); } catch (_) {} client = null; } connection.status = 'disconnected'; connection.since = 0; }

// Change the subscription live, no reconnect.
function changeSubscription(newFilter, qos, clear) {
  if (!client || connection.status !== 'connected') return { ok: false, error: 'Not connected' };
  const oldFilters = splitFilters(connection.rootFilter);
  const newFilters = splitFilters(newFilter || '#');
  for (const f of oldFilters) if (!newFilters.includes(f)) client.unsubscribe(f);
  const q = qos != null ? Number(qos) : connection.qos;
  for (const f of newFilters) client.subscribe(f, q);
  connection.rootFilter = newFilters.join(', ');
  connection.qos = q;
  if (clear) resetState();
  return { ok: true, status: connection };
}

function applyRegister(csvText) {
  const parsed = udmi.importRegister(csvText);
  const byKey = new Map(), byTopic = new Map();
  for (const a of parsed.assets) { byKey.set(assetKey(a.asset), a); if (a.topic) byTopic.set(a.topic, a.asset); }
  register = { assets: parsed.assets, points: parsed.points, byKey, byTopic };
  for (const a of assets.values()) { a.matched = byKey.has(a.key); a.meta = byKey.get(a.key) || null; }
  return { assets: parsed.assets.length, points: parsed.points };
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------
function send(res, code, body, headers = {}) { res.writeHead(code, { 'Cache-Control': 'no-store', ...headers }); res.end(body); }
function sendJson(res, code, obj) { send(res, code, JSON.stringify(obj), { 'Content-Type': 'application/json' }); }
function readBody(req) { return new Promise((resolve) => { let d = ''; req.on('data', (c) => (d += c)); req.on('end', () => resolve(d)); req.on('error', () => resolve(d)); }); }
async function readJson(req) { const b = await readBody(req); try { return JSON.parse(b || '{}'); } catch (_) { return {}; } }

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.png': 'image/png', '.json': 'application/json' };
function serveStatic(res, urlPath) {
  let rel = (urlPath === '/' ? '/index.html' : urlPath).split('?')[0];
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
    if (p === '/api/health' && req.method === 'GET') return sendJson(res, 200, { ok: true, version: '2.0.0' });

    if (p === '/api/status' && req.method === 'GET')
      return sendJson(res, 200, { status: connection, stats: buildSnapshot().stats, register: { assets: register.assets.length, points: register.points } });

    if (p === '/api/connect' && req.method === 'POST') {
      const cfg = await readJson(req);
      if (!cfg.host) return sendJson(res, 400, { ok: false, error: 'host is required' });
      const r = await doConnect(cfg);
      return sendJson(res, r.ok ? 200 : 502, r);
    }
    if (p === '/api/disconnect' && req.method === 'POST') { doDisconnect(); return sendJson(res, 200, { ok: true, status: connection }); }

    // Change subscription live (no reconnect).
    if (p === '/api/subscribe' && req.method === 'POST') {
      const b = await readJson(req);
      const r = changeSubscription(b.rootFilter, b.qos, !!b.clear);
      return sendJson(res, r.ok ? 200 : 409, r);
    }

    // Search / filter state (server-side, covers all topics incl. payload text).
    if (p === '/api/search' && req.method === 'POST') {
      const b = await readJson(req);
      searchQuery = String(b.q || '').trim().toLowerCase();
      matchedOnly = !!b.matchedOnly;
      return sendJson(res, 200, buildSnapshot());
    }

    if (p === '/api/focus' && req.method === 'POST') {
      const b = await readJson(req);
      focusAsset = assetKey(b.asset || (b.topic ? udmi.assetFromTopic(b.topic) : ''));
      const a = assets.get(focusAsset);
      return sendJson(res, 200, { ok: true, focused: a ? buildFocused(a) : null });
    }

    if (p === '/api/publish' && req.method === 'POST') {
      const b = await readJson(req);
      if (!client || connection.status !== 'connected') return sendJson(res, 409, { ok: false, error: 'Not connected' });
      if (!b.topic) return sendJson(res, 400, { ok: false, error: 'topic is required' });
      const ok = client.publish(b.topic, b.payload != null ? String(b.payload) : '', { qos: Number(b.qos) || 0, retain: !!b.retain });
      return sendJson(res, ok ? 200 : 500, { ok });
    }

    // Write a config payload to a device's /config topic.
    if (p === '/api/config' && req.method === 'POST') {
      const b = await readJson(req);
      if (!client || connection.status !== 'connected') return sendJson(res, 409, { ok: false, error: 'Not connected' });
      let topic = b.topic;
      if (!topic && b.asset) { const a = assets.get(assetKey(b.asset)); topic = a ? configTopicFor(a) : ''; }
      else if (topic) topic = udmi.configTopic(topic);
      if (!topic) return sendJson(res, 400, { ok: false, error: 'Could not resolve config topic' });
      const retain = b.retain !== false;
      const qos = b.qos != null ? Number(b.qos) : 1;
      const ok = client.publish(topic, b.payload != null ? String(b.payload) : '', { qos, retain });
      return sendJson(res, ok ? 200 : 500, { ok, topic });
    }

    if (p === '/api/register' && req.method === 'POST') { const b = await readJson(req); const r = applyRegister(b.csv || ''); return sendJson(res, 200, { imported: r.assets, points: r.points }); }
    if (p === '/api/register' && req.method === 'GET') return sendJson(res, 200, { assets: register.assets.length, points: register.points, register: register.assets });
    if (p === '/api/register' && req.method === 'DELETE') { register = { assets: [], points: 0, byKey: new Map(), byTopic: new Map() }; for (const a of assets.values()) { a.matched = false; a.meta = null; } return sendJson(res, 200, { imported: 0 }); }

    if (p === '/api/template' && req.method === 'GET') {
      const buf = getAsset(TEMPLATE_KEY);
      if (!buf) return send(res, 404, 'Template not found');
      return send(res, 200, buf, { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="mqtt-register-template.csv"' });
    }

    if (p === '/api/generate-register' && req.method === 'GET')
      return send(res, 200, generateRegisterCsv(), { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="mqtt-register-discovered.csv"' });

    // Export the currently focused asset's latest payload.
    if (p === '/api/export' && req.method === 'GET') {
      const key = assetKey(url.searchParams.get('asset') || focusAsset);
      const a = assets.get(key);
      const raw = a ? latestPayloadFor(a).raw : '';
      return send(res, 200, raw || '{}', { 'Content-Type': 'application/json', 'Content-Disposition': `attachment; filename="${key || 'payload'}.json"` });
    }

    // Export EVERYTHING as a ZIP archive mirroring the topic tree.
    if (p === '/api/export-archive' && req.method === 'GET') {
      if (!assets.size) return sendJson(res, 409, { error: 'Nothing discovered yet' });
      const now = Date.now();
      const buf = buildExportZip(now);
      const stamp = new Date(now).toISOString().replace(/[:.]/g, '-').slice(0, 19);
      return send(res, 200, buf, { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="mqtt-discovery-export-${stamp}.zip"` });
    }

    if (p === '/api/stream' && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
      res.write(`data: ${JSON.stringify(buildSnapshot())}\n\n`);
      sseClients.add(res);
      req.on('close', () => sseClients.delete(res));
      return;
    }

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
  console.log('   MQTT Discovery  —  running');
  console.log(`   Open your browser at:  ${uiUrl}`);
  console.log('   (Close this window to stop.)');
  console.log('  ==================================================');
  console.log('');
  if (process.platform === 'win32' && !process.env.NO_OPEN) {
    try { spawn('cmd', ['/c', 'start', '""', uiUrl], { detached: true, stdio: 'ignore', windowsHide: true }); } catch (_) {}
  }
});
