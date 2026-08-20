'use strict';
/*
 * udmi.js — UDMI-style payload understanding + register comparison.
 *
 * MQTT Discovery subscribes to a broker and inspects the JSON payloads flowing
 * on each topic. For BMS/IoT estates these are typically UDMI-shaped: a JSON
 * envelope with version / timestamp / seq and a nested `data` object whose leaf
 * points look like  { "value": 14.2, "units": "degC" }.
 *
 * This module:
 *   - derives an ASSET id from a topic (falling back to the register mapping)
 *   - detects the payload SCHEMA (udmi-v2 / json / raw)
 *   - extracts POINTS (name, value, unit) from a parsed payload
 *   - flags SCHEMA / PAYLOAD ISSUES (missing envelope fields, bad point shape)
 *   - parses the MQTT Register CSV and compares expected vs live
 *     (Matched / Missing / Extra) at asset and point level
 *
 * Zero dependencies (pure JS) so it bundles into the SEA build.
 */

// ---------------------------------------------------------------------------
// Topic → device path / asset id
// ---------------------------------------------------------------------------
// UDMI message-type folders that trail a device's topic (state / config /
// metadata / events/pointset / …). Stripping these off a topic yields the
// device's base path; the last segment of that path is the device leaf.
const MESSAGE_FOLDERS = new Set([
  'event', 'events', 'pointset', 'state', 'config', 'metadata', 'system',
  'telemetry', 'discovery', 'update', 'errors', 'commands', 'command', 'points',
]);

// The device base path: the topic with any trailing message-folder segments
// removed. e.g. hv/sec/00/CAM-1000013/events/pointset → hv/sec/00/CAM-1000013
function topicDevicePath(topic) {
  let segs = String(topic || '').split('/').filter(Boolean);
  while (segs.length > 1 && MESSAGE_FOLDERS.has(String(segs[segs.length - 1]).toLowerCase())) segs = segs.slice(0, -1);
  return segs.join('/');
}

// The device leaf: the last segment of the device base path — the recognisable
// device id, matching how MQTT Explorer shows the tree leaf.
//   hv/sec/00/CAM-1000013/events/pointset → "CAM-1000013"
//   hv/ems/b1/EM-1099058/metadata         → "EM-1099058"
// The register (topic → asset) still overrides this; it is only the fallback
// used when a payload carries no identity block.
function assetFromTopic(topic) {
  if (!topic) return '';
  const path = topicDevicePath(topic);
  const segs = path.split('/').filter(Boolean);
  return segs.length ? segs[segs.length - 1] : String(topic);
}

// The config topic for a device: its base path + /config.
function configTopic(topic) {
  const path = topicDevicePath(topic);
  return path ? path + '/config' : 'config';
}

// The "site" segment if the topic follows udmi/site/<site>/…
function siteFromTopic(topic) {
  const segs = String(topic || '').split('/').filter(Boolean);
  if (segs[0] && segs[0].toLowerCase() === 'udmi' && segs[1] && segs[1].toLowerCase() === 'site') return segs[2] || '';
  return '';
}

// Safe nested getter.
function dig(obj, pathArr) {
  let n = obj;
  for (const k of pathArr) { if (n == null || typeof n !== 'object') return undefined; n = n[k]; }
  return n;
}

// UDMI carries the device identity IN the payload. Prefer the physical_tag
// asset name, then the gateway id, then an explicit device id. This is the
// authoritative asset id (the topic is only a fallback).
//   system.physical_tag.asset.name  →  "DDC-1021013"
function assetFromPayload(obj) {
  if (!obj || typeof obj !== 'object') return '';
  const name = dig(obj, ['system', 'physical_tag', 'asset', 'name']);
  if (name) return String(name);
  const gw = dig(obj, ['gateway', 'gateway_id']);
  if (gw) return String(gw);
  const did = obj.device_id || obj.deviceId || dig(obj, ['system', 'physical_tag', 'asset', 'guid']);
  return did ? String(did) : '';
}

/*
 * Parse a UDMI envelope into the fields the UI needs. Handles the real UDMI
 * shape (system.location, system.physical_tag, gateway, pointset.points) as
 * well as looser variants. Returns:
 *   { asset, site, room, guid, gatewayId, proxyIds[], version, points[] }
 */
function parseEnvelope(obj) {
  const out = { asset: '', site: '', room: '', guid: '', gatewayId: '', proxyIds: [], version: '', points: [] };
  if (!obj || typeof obj !== 'object') return out;
  out.version = obj.version != null ? String(obj.version) : '';
  out.asset = assetFromPayload(obj);
  out.site = dig(obj, ['system', 'location', 'site']) || '';
  out.room = dig(obj, ['system', 'location', 'room']) || dig(obj, ['system', 'location', 'section']) || '';
  out.guid = dig(obj, ['system', 'physical_tag', 'asset', 'guid']) || '';
  out.gatewayId = dig(obj, ['gateway', 'gateway_id']) || '';
  const px = dig(obj, ['gateway', 'proxy_ids']);
  if (Array.isArray(px)) out.proxyIds = px.map(String);
  out.points = extractPoints(obj);
  return out;
}

// ---------------------------------------------------------------------------
// Payload parsing
// ---------------------------------------------------------------------------
function tryParseJson(raw) {
  if (raw == null) return null;
  const s = Buffer.isBuffer(raw) ? raw.toString('utf8') : String(raw);
  const t = s.trim();
  if (!t || (t[0] !== '{' && t[0] !== '[')) return null;
  try { return JSON.parse(t); } catch (_) { return null; }
}

// udmi-v2  = JSON envelope with version + timestamp + data
// json     = valid JSON but not the UDMI envelope
// raw      = not JSON
function detectSchema(obj, raw) {
  if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
    const hasVer = 'version' in obj;
    const hasTs = 'timestamp' in obj;
    if (hasVer && hasTs && ('data' in obj || 'pointset' in obj || 'points' in obj)) return 'udmi-v2';
    if (hasVer && hasTs) return 'udmi-v2';
    return 'json';
  }
  return 'raw';
}

// Walk a UDMI payload and pull out leaf points. A point is a leaf object that
// carries a `value` (UDMI uses { value, units }; some variants use present_value
// or a bare scalar under `points`/`pointset`). Returns [{ name, value, unit, path }].
function extractPoints(obj) {
  const out = [];
  if (!obj || typeof obj !== 'object') return out;

  // Preferred UDMI locations first.
  const roots = [];
  if (obj.data && typeof obj.data === 'object') roots.push({ node: obj.data, base: '' });
  if (obj.pointset && obj.pointset.points && typeof obj.pointset.points === 'object') roots.push({ node: obj.pointset.points, base: '' });
  if (obj.points && typeof obj.points === 'object') roots.push({ node: obj.points, base: '' });
  if (!roots.length) roots.push({ node: obj, base: '' });

  const seen = new Set();
  const MAX = 500;
  function isPointLeaf(v) {
    return v && typeof v === 'object' && !Array.isArray(v) &&
      ('value' in v || 'present_value' in v || 'setValue' in v);
  }
  function pointValue(v) {
    if ('value' in v) return v.value;
    if ('present_value' in v) return v.present_value;
    if ('setValue' in v) return v.setValue;
    return null;
  }
  function walk(node, path, depth) {
    if (out.length >= MAX || depth > 8 || !node || typeof node !== 'object') return;
    for (const [k, v] of Object.entries(node)) {
      const p = path ? path + '.' + k : k;
      if (isPointLeaf(v)) {
        if (!seen.has(p)) {
          seen.add(p);
          out.push({ name: k, path: p, value: pointValue(v), unit: v.units || v.unit || '' });
        }
      } else if (v && typeof v === 'object' && !Array.isArray(v)) {
        walk(v, p, depth + 1);
      } else if ((typeof v === 'number' || typeof v === 'boolean') && /point|sensor|temp|value|speed|status|setpoint|humidity|pressure|flow|co2|kwh|power/i.test(k)) {
        // A bare scalar that reads like a telemetry point.
        if (!seen.has(p)) { seen.add(p); out.push({ name: k, path: p, value: v, unit: '' }); }
      }
    }
  }
  for (const r of roots) walk(r.node, r.base, 0);
  return out;
}

// Detect schema / payload issues worth flagging (drives the "Schema / Payload
// Issues" metric). Returns an array of short strings.
function schemaIssues(obj, schema) {
  const issues = [];
  if (schema === 'raw') { issues.push('Payload is not JSON'); return issues; }
  if (schema === 'json') { issues.push('Not a UDMI envelope (no version/timestamp)'); }
  if (obj && typeof obj === 'object') {
    if (schema === 'udmi-v2') {
      if (!('version' in obj)) issues.push('Missing "version"');
      if (!('timestamp' in obj)) issues.push('Missing "timestamp"');
      if ('timestamp' in obj && isNaN(Date.parse(obj.timestamp))) issues.push('Unparseable "timestamp"');
    }
    const pts = extractPoints(obj);
    for (const p of pts) {
      if (p.value == null || (typeof p.value === 'string' && p.value.trim() === '')) issues.push(`Point "${p.name}" has no value`);
      else if (typeof p.value === 'number' && !p.unit && /temp|speed|humidity|pressure|flow|co2|kwh|power/i.test(p.name)) issues.push(`Point "${p.name}" missing units`);
    }
  }
  return issues;
}

// ---------------------------------------------------------------------------
// Register CSV import
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

/*
 * MQTT Register CSV — one row per expected POINT:
 *   Asset, Topic, Type, Point, Unit, Data Type, Schema, Site, Location, Description
 * Rows are grouped into assets. An asset with no explicit points still counts
 * as an expected asset (presence-only match).
 */
function importRegister(csvText) {
  const rows = parseCsv(csvText || '');
  if (!rows.length) return { assets: [], points: 0 };
  const header = rows[0].map((h) => h.trim().toLowerCase());
  const idx = (names) => { for (const n of names) { const i = header.indexOf(n); if (i !== -1) return i; } return -1; };
  const col = {
    asset: idx(['asset', 'asset id', 'device', 'device id', 'equipment', 'name']),
    topic: idx(['topic', 'mqtt topic', 'topic pattern', 'topic filter']),
    type: idx(['type', 'asset type', 'category', 'equipment type']),
    point: idx(['point', 'point name', 'tag', 'telemetry', 'field']),
    unit: idx(['unit', 'units', 'uom']),
    dataType: idx(['data type', 'datatype', 'kind']),
    schema: idx(['schema', 'payload', 'payload schema', 'model']),
    site: idx(['site', 'project', 'building']),
    location: idx(['location', 'area', 'zone', 'level', 'floor']),
    description: idx(['description', 'notes', 'desc']),
  };
  const assetMap = new Map();
  let pointCount = 0;
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r];
    const get = (i) => (i >= 0 && i < row.length ? row[i].trim() : '');
    let asset = get(col.asset);
    const topic = get(col.topic);
    if (!asset && topic) asset = assetFromTopic(topic);
    if (!asset) continue;
    const key = asset.toUpperCase();
    if (!assetMap.has(key)) {
      assetMap.set(key, {
        asset, topic, type: get(col.type), schema: get(col.schema),
        site: get(col.site), location: get(col.location), description: get(col.description),
        points: [],
      });
    }
    const a = assetMap.get(key);
    if (topic && !a.topic) a.topic = topic;
    const point = get(col.point);
    if (point) {
      a.points.push({ name: point, unit: get(col.unit), dataType: get(col.dataType) });
      pointCount++;
    }
  }
  return { assets: Array.from(assetMap.values()), points: pointCount };
}

// Compare one asset's live points against its expected points.
// Returns { matched, missing, extra, total } as point-name arrays/counts.
function comparePoints(expectedPoints, livePointNames) {
  const exp = new Set((expectedPoints || []).map((p) => normPoint(p.name)));
  const live = new Set((livePointNames || []).map(normPoint));
  const matched = [], missing = [], extra = [];
  for (const e of exp) (live.has(e) ? matched : missing).push(e);
  for (const l of live) if (!exp.has(l)) extra.push(l);
  return {
    matched: matched.length, missing: missing.length, extra: extra.length,
    matchedNames: matched, missingNames: missing, extraNames: extra,
    expected: exp.size,
  };
}
function normPoint(s) { return String(s || '').trim().toLowerCase().replace(/[\s\-]+/g, '_'); }

module.exports = {
  assetFromTopic, siteFromTopic, assetFromPayload, parseEnvelope, dig,
  topicDevicePath, configTopic, MESSAGE_FOLDERS,
  tryParseJson, detectSchema, extractPoints,
  schemaIssues, importRegister, comparePoints, normPoint,
};
