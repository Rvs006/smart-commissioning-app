'use strict';
/*
 * bacnet.js — a compact, dependency-free BACnet/IP client for Windows.
 *
 * It speaks just enough of the protocol to *discover and inventory* a BACnet
 * network from a single UDP socket (port 47808), with no native addons:
 *
 *   1. Who-Is (global broadcast, or a device-instance range) → collect I-Am.
 *      Each I-Am yields the device instance, max APDU, segmentation support,
 *      vendor id, the responding IP:port, and (for routed devices) the source
 *      BACnet network number + MAC.
 *   2. Who-Is-Router-To-Network → I-Am-Router-To-Network, to enumerate the
 *      remote networks/routers on a multi-network (BBMD) site.
 *   3. ReadProperty (confirmed request / complex-ack) for device identity:
 *      object-name, vendor-name, model-name, firmware-revision,
 *      application-software-version, location, description, protocol-revision,
 *      system-status, and the object-list length.
 *   4. Object-list enumeration by reading object-list[i] index-by-index (this
 *      sidesteps segmentation, which we do not implement), then reading each
 *      object's object-name / present-value / units.
 *
 * Wire format references: BVLC (Annex J), NPDU (Clause 6), APDU (Clause 20).
 */

const os = require('os');
const dgram = require('dgram');
const { execFile } = require('child_process');
const { VENDORS } = require('./vendors');

const BACNET_PORT = 47808;

// ---------------------------------------------------------------------------
// IP / adapter helpers
// ---------------------------------------------------------------------------
function ipToInt(ip) {
  const p = ip.split('.').map(Number);
  return (((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3]) >>> 0;
}
function intToIp(n) {
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.');
}
function maskToPrefix(mask) {
  return ipToInt(mask).toString(2).split('').filter((b) => b === '1').length;
}
function broadcastAddr(ip, netmask) {
  const network = (ipToInt(ip) & ipToInt(netmask)) >>> 0;
  return intToIp((network | (~ipToInt(netmask) >>> 0)) >>> 0);
}

function execFileP(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    execFile(cmd, args, { windowsHide: true, timeout: 15000, ...opts }, (err, stdout, stderr) => {
      resolve({ err, stdout: stdout || '', stderr: stderr || '' });
    });
  });
}

async function windowsAdapterMeta() {
  const meta = {};
  const ps =
    'Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,MacAddress | ConvertTo-Json -Compress';
  const { stdout } = await execFileP('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps]);
  try {
    let data = JSON.parse(stdout);
    if (!Array.isArray(data)) data = [data];
    for (const a of data) {
      meta[a.Name] = {
        description: a.InterfaceDescription,
        status: a.Status,
        linkSpeed: a.LinkSpeed,
        mac: a.MacAddress,
      };
    }
  } catch (_) {
    /* PowerShell unavailable — os module only */
  }
  return meta;
}

async function listAdapters() {
  const ifaces = os.networkInterfaces();
  const meta = await windowsAdapterMeta().catch(() => ({}));
  const out = [];
  for (const [name, addrs] of Object.entries(ifaces)) {
    for (const a of addrs || []) {
      if (a.family !== 'IPv4' || a.internal) continue;
      const m = meta[name] || {};
      out.push({
        name,
        description: m.description || name,
        status: m.status || 'Unknown',
        linkSpeed: m.linkSpeed || '',
        ip: a.address,
        netmask: a.netmask,
        mac: (a.mac || m.mac || '').toLowerCase(),
        prefix: maskToPrefix(a.netmask),
        cidr: `${a.address}/${maskToPrefix(a.netmask)}`,
        broadcast: broadcastAddr(a.address, a.netmask),
      });
    }
  }
  out.sort((x, y) => {
    const up = (v) => (String(v.status).toLowerCase() === 'up' ? 0 : 1);
    return up(x) - up(y);
  });
  return out;
}

// ---------------------------------------------------------------------------
// Enumerations (subset — unknown codes fall back to "type(n)")
// ---------------------------------------------------------------------------
const OBJECT_TYPES = {
  0: 'analog-input', 1: 'analog-output', 2: 'analog-value',
  3: 'binary-input', 4: 'binary-output', 5: 'binary-value',
  6: 'calendar', 7: 'command', 8: 'device', 9: 'event-enrollment',
  10: 'file', 11: 'group', 12: 'loop', 13: 'multi-state-input',
  14: 'multi-state-output', 15: 'notification-class', 16: 'program',
  17: 'schedule', 18: 'averaging', 19: 'multi-state-value', 20: 'trend-log',
  21: 'life-safety-point', 22: 'life-safety-zone', 23: 'accumulator',
  24: 'pulse-converter', 25: 'event-log', 26: 'global-group',
  27: 'trend-log-multiple', 28: 'load-control', 29: 'structured-view',
  30: 'access-door', 36: 'access-point', 37: 'access-zone',
  40: 'credential-data-input', 56: 'network-port',
};
const PROP = {
  objectIdentifier: 75, objectList: 76, objectName: 77, objectType: 79,
  presentValue: 85, units: 117, description: 28, vendorName: 121,
  vendorIdentifier: 120, modelName: 70, firmwareRevision: 44,
  appSoftwareVersion: 12, location: 58, systemStatus: 112,
  protocolVersion: 98, protocolRevision: 139, statusFlags: 111,
};
const SEGMENTATION = {
  0: 'segmented-both', 1: 'transmit-only', 2: 'receive-only', 3: 'no-segmentation',
};
const SYSTEM_STATUS = {
  0: 'operational', 1: 'operational-read-only', 2: 'download-required',
  3: 'download-in-progress', 4: 'non-operational', 5: 'backup-in-progress',
};
// Engineering units — the widely-used, confidently-known subset.
const UNITS = {
  0: 'm²', 1: 'ft²', 2: 'mA', 3: 'A', 4: 'Ω', 5: 'V', 6: 'kV', 8: 'VA',
  9: 'kVA', 16: 'J', 18: 'Wh', 19: 'kWh', 27: 'Hz', 29: '%RH', 30: 'mm',
  31: 'm', 47: 'W', 48: 'kW', 49: 'MW', 50: 'BTU/h', 53: 'Pa', 54: 'kPa',
  55: 'bar', 62: '°C', 63: 'K', 64: '°F', 71: 'h', 72: 'min', 73: 's',
  74: 'm/s', 82: 'L', 84: 'ft³/min', 85: 'm³/s', 87: 'L/s', 88: 'L/min',
  95: 'no-units', 96: 'ppm', 98: '%',
};
function objectTypeName(t) { return OBJECT_TYPES[t] || `type(${t})`; }
function unitName(u) { return u == null ? '' : (UNITS[u] || `units(${u})`); }
function vendorName(id) { return VENDORS[id] || ''; }

// ---------------------------------------------------------------------------
// APDU / tag encoding
// ---------------------------------------------------------------------------
function encodeObjectId(type, instance) {
  const v = (type & 0x3ff) * 0x400000 + (instance & 0x3fffff);
  const b = Buffer.alloc(4);
  b.writeUInt32BE(v >>> 0, 0);
  return b;
}
function decodeObjectId(buf, off) {
  const v = buf.readUInt32BE(off);
  return { type: Math.floor(v / 0x400000), instance: v & 0x3fffff };
}
function encUnsigned(n) {
  n = n >>> 0;
  if (n <= 0xff) return Buffer.from([n]);
  if (n <= 0xffff) return Buffer.from([(n >> 8) & 0xff, n & 0xff]);
  if (n <= 0xffffff) return Buffer.from([(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff]);
  return Buffer.from([(n >>> 24) & 0xff, (n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff]);
}
// Context tag with a primitive value of length `len` (len 0..4).
function ctxTagHeader(tagNum, len) {
  return Buffer.from([((tagNum & 0x0f) << 4) | 0x08 | (len & 0x07)]);
}

// ---------------------------------------------------------------------------
// NPDU / BVLC framing
// ---------------------------------------------------------------------------
function buildNPDU(opts = {}) {
  const { dnet = null, dadr = null, expectReply = false } = opts;
  const b = [0x01]; // version
  let control = 0;
  if (dnet != null) control |= 0x20;
  if (expectReply) control |= 0x04;
  b.push(control);
  if (dnet != null) {
    b.push((dnet >> 8) & 0xff, dnet & 0xff);
    if (dadr && dadr.length) {
      b.push(dadr.length);
      for (const x of dadr) b.push(x & 0xff);
    } else {
      b.push(0); // DLEN 0 → broadcast on that network
    }
    b.push(0xff); // hop count
  }
  return Buffer.from(b);
}
function bvlc(func, payload) {
  const len = payload.length + 4;
  return Buffer.concat([Buffer.from([0x81, func, (len >> 8) & 0xff, len & 0xff]), payload]);
}
const BVLC_UNICAST = 0x0a;
const BVLC_BROADCAST = 0x0b;

// ---------------------------------------------------------------------------
// Application-tag value decoding
// ---------------------------------------------------------------------------
// Reads the tag header at `off`. Returns {tagNum, isContext, isOpen, isClose,
// len, headerLen}. For primitive tags, the value bytes start at off+headerLen.
function readTagHeader(buf, off) {
  const tag = buf[off];
  let tagNum = (tag >> 4) & 0x0f;
  const isContext = ((tag >> 3) & 1) === 1;
  const lvt = tag & 0x07;
  let p = off + 1;
  if (tagNum === 0x0f) { tagNum = buf[p]; p++; }
  const isOpen = isContext && lvt === 6;
  const isClose = isContext && lvt === 7;
  let len = 0;
  if (!isOpen && !isClose) {
    if (lvt === 5) {
      len = buf[p]; p++;
      if (len === 254) { len = buf.readUInt16BE(p); p += 2; }
      else if (len === 255) { len = buf.readUInt32BE(p); p += 4; }
    } else {
      len = lvt;
    }
  }
  return { tagNum, isContext, isOpen, isClose, lvt, len, headerLen: p - off };
}

function decodeCharString(buf, off, len) {
  if (len <= 0) return '';
  const enc = buf[off];
  const body = buf.slice(off + 1, off + len);
  if (enc === 0) return body.toString('utf8');           // ANSI X3.4 / UTF-8
  if (enc === 4 || enc === 5) {                          // UCS-2 (UTF-16 BE)
    const swapped = Buffer.alloc(body.length);
    for (let i = 0; i + 1 < body.length; i += 2) { swapped[i] = body[i + 1]; swapped[i + 1] = body[i]; }
    return swapped.toString('utf16le').replace(/ +$/, '');
  }
  return body.toString('latin1');
}

// Decode one *application*-tagged primitive value at `off`.
// Returns { value, tagNum, next } or null if it's a context tag.
function decodeAppValue(buf, off) {
  const h = readTagHeader(buf, off);
  if (h.isContext) return null;
  const d = off + h.headerLen;
  let value;
  switch (h.tagNum) {
    case 0: value = null; break;                                   // Null
    case 1: value = h.len === 1; break;                            // Boolean (len carries value)
    case 2: value = h.len ? buf.readUIntBE(d, Math.min(h.len, 6)) : 0; break; // Unsigned
    case 3: value = h.len ? buf.readIntBE(d, Math.min(h.len, 6)) : 0; break;  // Signed
    case 4: value = buf.readFloatBE(d); break;                     // Real
    case 5: value = buf.readDoubleBE(d); break;                    // Double
    case 6: value = buf.slice(d, d + h.len).toString('hex'); break; // Octet string
    case 7: value = decodeCharString(buf, d, h.len); break;        // Character string
    case 8: value = buf.slice(d + 1, d + h.len).toString('hex'); break; // Bit string
    case 9: value = h.len ? buf.readUIntBE(d, Math.min(h.len, 6)) : 0; break; // Enumerated
    case 12: value = decodeObjectId(buf, d); break;                // Object identifier
    default: value = buf.slice(d, d + h.len).toString('hex');
  }
  const consumed = h.tagNum === 1 ? 0 : h.len; // boolean has no data bytes
  return { value, tagNum: h.tagNum, next: d + consumed };
}

// ---------------------------------------------------------------------------
// Frame parsing
// ---------------------------------------------------------------------------
function parseNPDU(buf, off) {
  const version = buf[off++];
  const control = buf[off++];
  let dnet = null, snet = null, sadr = null;
  if (control & 0x20) {
    dnet = buf.readUInt16BE(off); off += 2;
    const dlen = buf[off++];
    off += dlen;
  }
  if (control & 0x08) {
    snet = buf.readUInt16BE(off); off += 2;
    const slen = buf[off++];
    if (slen) { sadr = buf.slice(off, off + slen); off += slen; }
  }
  if (control & 0x20) off++; // hop count
  const isNetMsg = !!(control & 0x80);
  let msgType = null;
  if (isNetMsg) msgType = buf[off++];
  return { version, control, dnet, snet, sadr, isNetMsg, msgType, apduOffset: off };
}

// Parse a top-level BACnet/IP frame. Returns a normalised event object.
function parseFrame(buf, rinfo) {
  if (buf.length < 4 || buf[0] !== 0x81) return null;
  const func = buf[1];
  let off = 4;
  let originAddr = rinfo.address;
  let originPort = rinfo.port;
  if (func === 0x04) {
    // Forwarded-NPDU: 6-byte B/IP source (4 IP + 2 port) precedes the NPDU.
    if (buf.length >= 10) {
      originAddr = `${buf[4]}.${buf[5]}.${buf[6]}.${buf[7]}`;
      originPort = buf.readUInt16BE(8);
    }
    off = 10;
  } else if (func !== 0x0a && func !== 0x0b && func !== 0x00) {
    // Other BVLC functions (register-fd, etc.) carry an NPDU after 4-byte header.
  }
  const npdu = parseNPDU(buf, off);
  const base = {
    ip: originAddr, port: originPort,
    snet: npdu.snet, sadr: npdu.sadr,
  };
  if (npdu.isNetMsg) {
    if (npdu.msgType === 0x01) {
      // I-Am-Router-To-Network: list of 2-byte DNETs.
      const nets = [];
      let p = npdu.apduOffset;
      while (p + 1 < buf.length) { nets.push(buf.readUInt16BE(p)); p += 2; }
      return { kind: 'router', ...base, networks: nets };
    }
    return { kind: 'netmsg', ...base, msgType: npdu.msgType };
  }
  const a = npdu.apduOffset;
  const pduType = (buf[a] >> 4) & 0x0f;
  if (pduType === 1) {
    const service = buf[a + 1];
    if (service === 0x00) return { kind: 'iam', ...base, apduOffset: a, buf };
    return { kind: 'unconfirmed', ...base, service };
  }
  if (pduType === 3) {
    return { kind: 'complexAck', ...base, invokeId: buf[a + 1], service: buf[a + 2], apduOffset: a, buf };
  }
  if (pduType === 2) return { kind: 'simpleAck', ...base, invokeId: buf[a + 1] };
  if (pduType === 5) return { kind: 'error', ...base, invokeId: buf[a + 1], apduOffset: a, buf };
  if (pduType === 6) return { kind: 'reject', ...base, invokeId: buf[a + 1], reason: buf[a + 2] };
  if (pduType === 7) return { kind: 'abort', ...base, invokeId: buf[a + 1], reason: buf[a + 2] };
  return { kind: 'other', ...base, pduType };
}

// Parse an I-Am APDU: object-id (device), max-apdu, segmentation, vendor-id.
function parseIAm(buf, apduOffset) {
  let p = apduOffset + 2; // skip 0x10 0x00
  const oid = decodeAppValue(buf, p); if (!oid) return null; p = oid.next;
  const maxApdu = decodeAppValue(buf, p); if (!maxApdu) return null; p = maxApdu.next;
  const seg = decodeAppValue(buf, p); if (!seg) return null; p = seg.next;
  const vendor = decodeAppValue(buf, p); if (!vendor) return null;
  return {
    instance: oid.value.instance,
    objectType: oid.value.type,
    maxApdu: maxApdu.value,
    segmentation: seg.value,
    vendorId: vendor.value,
  };
}

// Extract the application values from a ReadProperty complex-ack: everything
// between the opening (0x3E) and closing (0x3F) context tag 3.
function parseReadPropertyAck(buf, apduOffset) {
  let p = apduOffset + 3; // skip PDU-type/invoke/service
  // Walk context tags until the opening tag 3.
  while (p < buf.length) {
    const h = readTagHeader(buf, p);
    if (h.isOpen && h.tagNum === 3) { p += h.headerLen; break; }
    if (h.isClose) { p += h.headerLen; continue; }
    p += h.headerLen + h.len;
  }
  const values = [];
  while (p < buf.length) {
    const h = readTagHeader(buf, p);
    if (h.isClose && h.tagNum === 3) break;
    if (h.isContext) { p += h.headerLen + h.len; continue; } // skip nested context values
    const v = decodeAppValue(buf, p);
    if (!v || v.next <= p) break;
    values.push({ value: v.value, tagNum: v.tagNum });
    p = v.next;
  }
  return values;
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function createClient() {
  // Two sockets, deliberately:
  //  * discoverySock is bound to UDP 47808 (reuseAddr) so it hears the broadcast
  //    I-Am replies. Because the responses to Who-Is are BROADCASTS, they fan out
  //    to every socket bound to 47808 — so this keeps working even when other
  //    BACnet software (e.g. YABE) also holds 47808.
  //  * readSock is bound to a private EPHEMERAL port and carries all confirmed
  //    ReadProperty traffic. A device replies to a confirmed request at the
  //    requester's source port, so those unicast responses come straight back
  //    to us and are never stolen by another app sharing 47808.
  let discoverySock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
  const readSock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
  const pending = new Map(); // invokeId -> { resolve, reject, ip, timer }
  const iamHandlers = new Set();
  const routerHandlers = new Set();
  let invokeCounter = 0;
  let bound = false;
  let discoveryDegraded = false;

  const onMessage = (msg, rinfo) => {
    let f;
    try { f = parseFrame(msg, rinfo); } catch (_) { return; }
    if (!f) return;
    if (f.kind === 'iam') {
      let info;
      try { info = parseIAm(f.buf, f.apduOffset); } catch (_) { info = null; }
      if (info) for (const h of iamHandlers) h({ ...info, ip: f.ip, port: f.port, snet: f.snet, sadr: f.sadr });
      return;
    }
    if (f.kind === 'router') {
      for (const h of routerHandlers) h({ ip: f.ip, port: f.port, networks: f.networks });
      return;
    }
    if (f.invokeId != null && pending.has(f.invokeId)) {
      const req = pending.get(f.invokeId);
      if (f.kind === 'complexAck') {
        let vals;
        try { vals = parseReadPropertyAck(f.buf, f.apduOffset); } catch (e) { req.reject(new Error('decode: ' + e.message)); cleanup(f.invokeId); return; }
        req.resolve(vals);
      } else if (f.kind === 'error') {
        req.reject(makeErr('BACnet-Error', false));
      } else if (f.kind === 'reject') {
        req.reject(makeErr('reject(' + f.reason + ')', true));
      } else if (f.kind === 'abort') {
        req.reject(makeErr('abort(' + f.reason + ')', true));
      } else if (f.kind === 'simpleAck') {
        req.resolve([]);
      }
      cleanup(f.invokeId);
    }
  };
  discoverySock.on('message', onMessage);
  readSock.on('message', onMessage);

  function makeErr(label, transient) {
    const e = new Error(label);
    e.bacnet = true;
    e.transient = !!transient; // reject/abort are transient (busy); Error is definitive
    return e;
  }
  function cleanup(id) {
    const r = pending.get(id);
    if (r && r.timer) clearTimeout(r.timer);
    pending.delete(id);
  }

  function bindSock(sock, port) {
    return new Promise((resolve, reject) => {
      const onErr = (e) => reject(e);
      sock.once('error', onErr);
      sock.bind(port, () => {
        try { sock.setBroadcast(true); } catch (_) {}
        sock.removeListener('error', onErr);
        resolve();
      });
    });
  }

  async function bind() {
    if (bound) return;
    // Private ephemeral socket for confirmed reads — always available.
    await bindSock(readSock, 0);
    // Shared 47808 socket for discovery. If it cannot bind even with reuseAddr,
    // degrade to the ephemeral socket (still finds devices that unicast their
    // I-Am; may miss ones that only broadcast it).
    try {
      await bindSock(discoverySock, BACNET_PORT);
    } catch (_) {
      discoveryDegraded = true;
      try { discoverySock.close(); } catch (_) {}
      discoverySock = readSock;
    }
    bound = true;
  }

  function nextInvoke() {
    for (let i = 0; i < 256; i++) {
      invokeCounter = (invokeCounter + 1) & 0xff;
      if (!pending.has(invokeCounter)) return invokeCounter;
    }
    return invokeCounter;
  }

  function sendRaw(sock, frame, ip, port) {
    return new Promise((resolve) => {
      sock.send(frame, 0, frame.length, port || BACNET_PORT, ip, () => resolve());
    });
  }

  // --- discovery ---
  function sendWhoIs(ip, port, low, high, broadcast) {
    let apdu;
    if (low != null && high != null) {
      const lo = encUnsigned(low), hi = encUnsigned(high);
      apdu = Buffer.concat([
        Buffer.from([0x10, 0x08]),
        ctxTagHeader(0, lo.length), lo,
        ctxTagHeader(1, hi.length), hi,
      ]);
    } else {
      apdu = Buffer.from([0x10, 0x08]);
    }
    // Global-broadcast NPDU (DNET 0xFFFF) so routers relay it to all networks.
    const npdu = buildNPDU({ dnet: 0xffff });
    return sendRaw(discoverySock, bvlc(broadcast ? BVLC_BROADCAST : BVLC_UNICAST, Buffer.concat([npdu, apdu])), ip, port);
  }
  function sendWhoIsRouter(ip, port) {
    // Network-layer message, type 0 (Who-Is-Router-To-Network), no DNET = all.
    const npdu = Buffer.from([0x01, 0x80, 0x00]);
    return sendRaw(discoverySock, bvlc(BVLC_BROADCAST, npdu), ip, port);
  }

  async function discover({ broadcast, timeoutMs = 4000, low = null, high = null, onDevice, onRouter }) {
    const devices = new Map(); // instance -> device
    const iamH = (iam) => {
      if (devices.has(iam.instance)) return;
      const dev = {
        instance: iam.instance,
        ip: iam.ip,
        port: iam.port,
        network: iam.snet || 0,
        mac: iam.sadr ? iam.sadr.toString('hex') : '',
        maxApdu: iam.maxApdu,
        segmentation: SEGMENTATION[iam.segmentation] || String(iam.segmentation),
        segmentationCode: iam.segmentation,
        vendorId: iam.vendorId,
        vendor: vendorName(iam.vendorId),
      };
      devices.set(iam.instance, dev);
      if (onDevice) onDevice(dev);
    };
    const routerH = (r) => { if (onRouter) onRouter(r); };
    iamHandlers.add(iamH);
    routerHandlers.add(routerH);
    try {
      await sendWhoIsRouter(broadcast);
      await sendWhoIs(broadcast, BACNET_PORT, low, high, true);
      // A second Who-Is mid-window catches devices that were slow / missed the first.
      await sleep(Math.min(1200, timeoutMs / 2));
      await sendWhoIs(broadcast, BACNET_PORT, low, high, true);
      await sleep(Math.max(0, timeoutMs - Math.min(1200, timeoutMs / 2)));
    } finally {
      iamHandlers.delete(iamH);
      routerHandlers.delete(routerH);
    }
    return [...devices.values()];
  }

  // --- confirmed ReadProperty ---
  function readProperty(target, objType, objInst, propId, arrayIndex, timeout = 1500) {
    const invokeId = nextInvoke();
    const parts = [
      Buffer.from([0x00, 0x05, invokeId, 0x0c]), // confirmed-req, max-apdu=1476, invoke, RP
      Buffer.from([0x0c]), encodeObjectId(objType, objInst), // ctx0: object id
    ];
    const pid = encUnsigned(propId);
    parts.push(ctxTagHeader(1, pid.length), pid);
    if (arrayIndex != null) {
      const ai = encUnsigned(arrayIndex);
      parts.push(ctxTagHeader(2, ai.length), ai);
    }
    const apdu = Buffer.concat(parts);
    const routed = target.dnet != null;
    const npdu = buildNPDU({ expectReply: true, dnet: routed ? target.dnet : null, dadr: routed ? target.dadr : null });
    const frame = bvlc(BVLC_UNICAST, Buffer.concat([npdu, apdu]));

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(invokeId); reject(Object.assign(new Error('timeout'), { timeout: true })); }, timeout);
      pending.set(invokeId, { resolve, reject, ip: target.ip, timer });
      sendRaw(readSock, frame, target.ip, target.port || BACNET_PORT);
    });
  }

  // Read a single property, returning the first value or null on any failure.
  // `retries` extra attempts on failure — many small controllers silently drop
  // (timeout) or transiently reject/abort a request when busy, and a repeat
  // attempt usually succeeds. A BACnet *Error* PDU (e.g. unknown-property) is a
  // definitive "no" and is not retried.
  async function readOne(target, objType, objInst, propId, arrayIndex, timeout, retries = 1) {
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const vals = await readProperty(target, objType, objInst, propId, arrayIndex, timeout);
        return vals.length ? vals[0].value : null;
      } catch (e) {
        // Retry on drops (timeout) and transient reject/abort; not on Error PDUs.
        const retryable = e && (e.timeout || e.transient);
        if (retryable && attempt < retries) continue;
        return null;
      }
    }
    return null;
  }

  function close() {
    for (const [, r] of pending) { if (r.timer) clearTimeout(r.timer); }
    pending.clear();
    try { readSock.close(); } catch (_) {}
    try { if (discoverySock !== readSock) discoverySock.close(); } catch (_) {}
  }

  return { bind, discover, readProperty, readOne, close, isDegraded: () => discoveryDegraded };
}

// ---------------------------------------------------------------------------
// High-level helpers used by the server
// ---------------------------------------------------------------------------
function deviceTarget(dev) {
  const t = { ip: dev.ip, port: dev.port || BACNET_PORT };
  if (dev.network && dev.mac) {
    t.dnet = dev.network;
    t.dadr = Buffer.from(dev.mac, 'hex');
  }
  return t;
}

// Read the standard identity properties for a discovered device (best-effort;
// every field degrades gracefully to null / '').
async function readDeviceIdentity(client, dev, timeout = 1500) {
  const t = deviceTarget(dev);
  const DEV = 8;
  // Sequential, not parallel: many controllers process one confirmed request at
  // a time and drop the rest, so firing all identity reads at once loses fields.
  const rd = (pid, idx) => client.readOne(t, DEV, dev.instance, pid, idx == null ? null : idx, timeout);
  const name = await rd(PROP.objectName);
  const vName = await rd(PROP.vendorName);
  const model = await rd(PROP.modelName);
  const fw = await rd(PROP.firmwareRevision);
  const appSw = await rd(PROP.appSoftwareVersion);
  const loc = await rd(PROP.location);
  const desc = await rd(PROP.description);
  const protoRev = await rd(PROP.protocolRevision);
  const sysStatus = await rd(PROP.systemStatus);
  const objCount = await rd(PROP.objectList, 0); // array index 0 = count
  return {
    name: name || '',
    vendor: vName || dev.vendor || '',
    model: model || '',
    firmware: fw || '',
    appSoftware: appSw || '',
    location: loc || '',
    description: desc || '',
    protocolRevision: protoRev != null ? protoRev : '',
    systemStatus: sysStatus != null ? (SYSTEM_STATUS[sysStatus] || String(sysStatus)) : '',
    objectCount: objCount != null ? objCount : null,
  };
}

// Enumerate a device's object list index-by-index, then read each object's
// name (and present-value/units for I/O + value objects). Bounded by `cap`.
async function readObjectList(client, dev, { cap = 200, timeout = 1500, onProgress } = {}) {
  const t = deviceTarget(dev);
  const DEV = 8;
  // Object-list length can be busy on first ask — allow a couple of retries.
  let count = await client.readOne(t, DEV, dev.instance, PROP.objectList, 0, timeout, 2);
  if (count == null) return { objects: [], count: 0, truncated: false, error: 'Device did not answer the object-list read (no response after retries).' };
  const total = count;
  const limit = Math.min(count, cap);
  const objects = [];
  const PV_TYPES = new Set([0, 1, 2, 3, 4, 5, 13, 14, 19, 23, 24]);
  const ANALOG = new Set([0, 1, 2]);

  // Fetch object identifiers first (sequential index reads are cheap & reliable).
  const ids = [];
  for (let i = 1; i <= limit; i++) {
    const oid = await client.readOne(t, DEV, dev.instance, PROP.objectList, i, timeout, 2);
    if (oid && typeof oid === 'object') ids.push(oid);
    if (onProgress) onProgress(ids.length, limit);
  }

  // Enrich each object with name / present-value / units — ONE object at a time.
  // Concurrent reads make small controllers silently drop requests, which is
  // what blanks out object names; reading serially (like YABE reads on click),
  // with retries on the name itself, resolves names reliably. object-name is a
  // required property, so it is retried on any transient failure.
  for (let i = 0; i < ids.length; i++) {
    const oid = ids[i];
    const name = await client.readOne(t, oid.type, oid.instance, PROP.objectName, null, timeout, 3);
    let presentValue = null, units = '';
    if (PV_TYPES.has(oid.type)) {
      presentValue = await client.readOne(t, oid.type, oid.instance, PROP.presentValue, null, timeout);
      if (ANALOG.has(oid.type)) {
        const u = await client.readOne(t, oid.type, oid.instance, PROP.units, null, timeout);
        units = unitName(u);
      }
    }
    objects[i] = {
      type: oid.type,
      typeName: objectTypeName(oid.type),
      instance: oid.instance,
      name: name || '',
      presentValue: formatPresentValue(presentValue, oid.type),
      units,
    };
    if (onProgress) onProgress(i + 1, ids.length);
  }

  return {
    objects: objects.filter(Boolean),
    count: total,
    truncated: total > limit,
    error: null,
  };
}

const BINARY_TYPES = new Set([3, 4, 5]); // binary in/out/value: present-value is BACnetBinaryPV
function formatPresentValue(v, objType) {
  if (v == null) return '';
  if (typeof v === 'boolean') return v ? 'active' : 'inactive';
  if (typeof v === 'number') {
    if (BINARY_TYPES.has(objType)) return v === 1 ? 'active' : v === 0 ? 'inactive' : String(v);
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }
  if (typeof v === 'object') return `${objectTypeName(v.type)}:${v.instance}`;
  return String(v);
}

module.exports = {
  createClient,
  listAdapters,
  readDeviceIdentity,
  readObjectList,
  deviceTarget,
  objectTypeName,
  unitName,
  vendorName,
  ipToInt,
  intToIp,
  BACNET_PORT,
  OBJECT_TYPES,
  // Exposed for offline codec tests (selftest.js).
  _internal: {
    parseFrame, parseIAm, parseReadPropertyAck, decodeAppValue,
    encodeObjectId, decodeObjectId, buildNPDU, bvlc, encUnsigned, ctxTagHeader,
  },
};
