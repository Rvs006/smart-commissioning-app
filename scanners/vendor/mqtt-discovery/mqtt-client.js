'use strict';
/*
 * mqtt-client.js — a persistent, dependency-free MQTT client.
 *
 * Hand-rolls the MQTT 3.1.1 (protocol level 4) wire protocol over Node's
 * built-in `net` / `tls`, so the whole app stays a zero-runtime-dependency
 * codebase that packages cleanly into a single SEA executable (no `mqtt` npm
 * package).
 *
 * Unlike a one-shot probe, this holds a live session: it CONNECTs (anonymous,
 * username/password, or with client certificates), SUBSCRIBEs to a root topic
 * filter, and emits every incoming PUBLISH as a 'message' event in real time.
 * It can PUBLISH (QoS 0/1, retain), keeps the session alive with PINGREQ, and
 * reconnects on drop.
 *
 *   const c = new MqttClient({ host, port, tls, username, password, ... });
 *   c.on('connect', (info) => ...);      // CONNACK accepted
 *   c.on('message', (m) => ...);         // { topic, payload(Buffer), qos, retain }
 *   c.on('error', (e) => ...);           // connection / protocol error
 *   c.on('close', () => ...);            // socket closed
 *   c.connect();
 *   c.publish(topic, payload, { qos, retain });
 *   c.end();
 */

const net = require('net');
const tls = require('tls');
const { EventEmitter } = require('events');

// ---------------------------------------------------------------------------
// Wire encoding helpers
// ---------------------------------------------------------------------------
function encodeVarInt(n) {
  const bytes = [];
  do { let b = n % 128; n = Math.floor(n / 128); if (n > 0) b |= 0x80; bytes.push(b); } while (n > 0);
  return Buffer.from(bytes);
}
function decodeVarInt(buf, offset) {
  let multiplier = 1, value = 0, bytes = 0;
  while (true) {
    if (offset + bytes >= buf.length) return null;
    const b = buf[offset + bytes];
    value += (b & 0x7f) * multiplier;
    bytes++;
    if ((b & 0x80) === 0) break;
    multiplier *= 128;
    if (bytes > 4) return { value: -1, bytes };
  }
  return { value, bytes };
}
function encodeString(s) {
  const body = Buffer.from(s, 'utf8');
  const len = Buffer.alloc(2);
  len.writeUInt16BE(body.length, 0);
  return Buffer.concat([len, body]);
}

const CONNACK_TEXT = {
  0: 'Connection accepted',
  1: 'Unacceptable protocol version',
  2: 'Client identifier rejected',
  3: 'Server unavailable',
  4: 'Bad username or password',
  5: 'Not authorised',
};

// ---------------------------------------------------------------------------
// Packet builders
// ---------------------------------------------------------------------------
function buildConnect(opts) {
  const clientId = opts.clientId || 'sca-mqtt';
  const keepAlive = opts.keepAlive || 60;
  let flags = 0x02; // clean session
  const payloadParts = [encodeString(clientId)];
  if (opts.username) {
    flags |= 0x80;
    payloadParts.push(encodeString(opts.username));
    if (opts.password != null && opts.password !== '') {
      flags |= 0x40;
      payloadParts.push(encodeString(String(opts.password)));
    }
  }
  const ka = Buffer.alloc(2); ka.writeUInt16BE(keepAlive, 0);
  const varHeader = Buffer.concat([encodeString('MQTT'), Buffer.from([0x04]), Buffer.from([flags]), ka]);
  const body = Buffer.concat([varHeader, ...payloadParts]);
  return Buffer.concat([Buffer.from([0x10]), encodeVarInt(body.length), body]);
}
function buildSubscribe(packetId, filters, qos) {
  const pid = Buffer.alloc(2); pid.writeUInt16BE(packetId, 0);
  const parts = [pid];
  for (const f of filters) { parts.push(encodeString(f)); parts.push(Buffer.from([qos & 0x03])); }
  const body = Buffer.concat(parts);
  return Buffer.concat([Buffer.from([0x82]), encodeVarInt(body.length), body]);
}
function buildUnsubscribe(packetId, filters) {
  const pid = Buffer.alloc(2); pid.writeUInt16BE(packetId, 0);
  const parts = [pid];
  for (const f of filters) parts.push(encodeString(f));
  const body = Buffer.concat(parts);
  return Buffer.concat([Buffer.from([0xa2]), encodeVarInt(body.length), body]);
}
function buildPublish(topic, payload, opts) {
  const qos = (opts.qos || 0) & 0x03;
  const retain = opts.retain ? 0x01 : 0x00;
  const dup = 0x00;
  const first = 0x30 | dup | (qos << 1) | retain;
  const parts = [encodeString(topic)];
  if (qos > 0) { const pid = Buffer.alloc(2); pid.writeUInt16BE(opts.packetId || 1, 0); parts.push(pid); }
  parts.push(Buffer.isBuffer(payload) ? payload : Buffer.from(String(payload), 'utf8'));
  const body = Buffer.concat(parts);
  return Buffer.concat([Buffer.from([first]), encodeVarInt(body.length), body]);
}
function buildPubAck(packetId) {
  const pid = Buffer.alloc(2); pid.writeUInt16BE(packetId, 0);
  return Buffer.concat([Buffer.from([0x40, 0x02]), pid]);
}
function buildPingReq() { return Buffer.from([0xc0, 0x00]); }
function buildDisconnect() { return Buffer.from([0xe0, 0x00]); }

// ---------------------------------------------------------------------------
// Incoming stream parser
// ---------------------------------------------------------------------------
function parsePackets(buf) {
  const packets = [];
  let pos = 0;
  while (pos < buf.length) {
    const first = buf[pos];
    const type = (first >> 4) & 0x0f;
    const flags = first & 0x0f;
    const rl = decodeVarInt(buf, pos + 1);
    if (!rl) break;
    if (rl.value < 0) { pos = buf.length; break; }
    const headerLen = 1 + rl.bytes;
    const total = headerLen + rl.value;
    if (pos + total > buf.length) break;
    packets.push({ type, flags, payload: buf.slice(pos + headerLen, pos + total) });
    pos += total;
  }
  return { packets, consumed: pos };
}
function parsePublish(payload, flags) {
  const retain = (flags & 0x01) === 1;
  const qos = (flags >> 1) & 0x03;
  const dup = (flags & 0x08) === 8;
  if (payload.length < 2) return null;
  const topicLen = payload.readUInt16BE(0);
  let pos = 2;
  if (pos + topicLen > payload.length) return null;
  const topic = payload.slice(pos, pos + topicLen).toString('utf8');
  pos += topicLen;
  let packetId = null;
  if (qos > 0) { packetId = payload.readUInt16BE(pos); pos += 2; }
  const message = payload.slice(pos);
  return { topic, retain, qos, dup, packetId, message };
}

// ---------------------------------------------------------------------------
// The client
// ---------------------------------------------------------------------------
class MqttClient extends EventEmitter {
  /*
   * opts = {
   *   host, port,
   *   tls: false,                 // wrap in TLS
   *   rejectUnauthorized: true,   // validate broker certificate (TLS only)
   *   ca, cert, key,              // PEM strings for CA / client cert / client key
   *   username, password,
   *   clientId,
   *   keepAlive: 60,
   *   connectTimeout: 8000,
   *   reconnectPeriod: 4000,      // 0 to disable auto-reconnect
   * }
   */
  constructor(opts) {
    super();
    this.opts = Object.assign({ port: 1883, keepAlive: 60, connectTimeout: 8000, reconnectPeriod: 4000 }, opts);
    this.sock = null;
    this.buf = Buffer.alloc(0);
    this.connected = false;
    this.ended = false;
    this._packetId = 1;
    this._pingTimer = null;
    this._connectTimer = null;
    this._reconnectTimer = null;
    this._subscriptions = []; // [{ filter, qos }]
  }

  nextPacketId() { this._packetId = (this._packetId % 65535) + 1; return this._packetId; }

  connect() {
    this.ended = false;
    this._open();
    return this;
  }

  _open() {
    const o = this.opts;
    this.buf = Buffer.alloc(0);
    const onSecureOrConnect = () => { this._sendConnect(); };
    try {
      if (o.tls) {
        const tlsOpts = { host: o.host, port: o.port, servername: o.host,
          rejectUnauthorized: o.rejectUnauthorized !== false };
        if (o.ca) tlsOpts.ca = o.ca;
        if (o.cert) tlsOpts.cert = o.cert;
        if (o.key) tlsOpts.key = o.key;
        this.sock = tls.connect(tlsOpts, onSecureOrConnect);
      } else {
        this.sock = new net.Socket();
        this.sock.once('connect', onSecureOrConnect);
        this.sock.connect(o.port, o.host);
      }
    } catch (e) { return this._fail(e); }

    this._connectTimer = setTimeout(() => {
      if (!this.connected) this._fail(new Error('Connection timed out'));
    }, o.connectTimeout);

    this.sock.on('data', (chunk) => this._onData(chunk));
    this.sock.once('error', (e) => this._fail(e));
    this.sock.once('close', () => this._onClose());
  }

  _sendConnect() {
    this._write(buildConnect(this.opts));
  }

  _onData(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    const { packets, consumed } = parsePackets(this.buf);
    if (consumed > 0) this.buf = this.buf.slice(consumed);
    for (const p of packets) this._handle(p);
  }

  _handle(pkt) {
    switch (pkt.type) {
      case 2: { // CONNACK
        clearTimeout(this._connectTimer);
        const code = pkt.payload.length >= 2 ? pkt.payload[1] : 255;
        const sessionPresent = pkt.payload.length >= 1 && (pkt.payload[0] & 0x01) === 1;
        if (code === 0) {
          this.connected = true;
          this._startPing();
          this._resubscribe();
          this.emit('connect', { sessionPresent, code, text: CONNACK_TEXT[code] || 'Connected' });
        } else {
          const err = new Error(CONNACK_TEXT[code] || `CONNACK refused (code ${code})`);
          err.code = code;
          err.connack = true;
          // A credential/authorisation refusal should not auto-reconnect-loop.
          this.opts.reconnectPeriod = 0;
          this._fail(err);
        }
        break;
      }
      case 3: { // PUBLISH
        const m = parsePublish(pkt.payload, pkt.flags);
        if (m) {
          if (m.qos === 1 && m.packetId != null) this._write(buildPubAck(m.packetId));
          this.emit('message', m);
        }
        break;
      }
      case 4: // PUBACK (our QoS1 publish acked)
        this.emit('puback', pkt.payload.length >= 2 ? pkt.payload.readUInt16BE(0) : null);
        break;
      case 9: // SUBACK
        this.emit('suback');
        break;
      case 11: // UNSUBACK
        this.emit('unsuback');
        break;
      case 13: // PINGRESP
        break;
      default:
        break;
    }
  }

  subscribe(filter, qos = 0) {
    const filters = Array.isArray(filter) ? filter : [filter];
    for (const f of filters) {
      if (!this._subscriptions.find((s) => s.filter === f)) this._subscriptions.push({ filter: f, qos });
    }
    if (this.connected) this._write(buildSubscribe(this.nextPacketId(), filters, qos));
    return this;
  }
  unsubscribe(filter) {
    const filters = Array.isArray(filter) ? filter : [filter];
    this._subscriptions = this._subscriptions.filter((s) => !filters.includes(s.filter));
    if (this.connected) this._write(buildUnsubscribe(this.nextPacketId(), filters));
    return this;
  }
  _resubscribe() {
    // Group by qos to minimise packets.
    const byQos = {};
    for (const s of this._subscriptions) (byQos[s.qos] = byQos[s.qos] || []).push(s.filter);
    for (const qos of Object.keys(byQos)) this._write(buildSubscribe(this.nextPacketId(), byQos[qos], Number(qos)));
  }

  publish(topic, payload, opts = {}) {
    if (!this.connected) return false;
    const qos = opts.qos || 0;
    const packetId = qos > 0 ? this.nextPacketId() : undefined;
    this._write(buildPublish(topic, payload, { qos, retain: !!opts.retain, packetId }));
    return true;
  }

  _startPing() {
    this._stopPing();
    const ms = Math.max(5, (this.opts.keepAlive || 60) * 0.75) * 1000;
    this._pingTimer = setInterval(() => { if (this.connected) this._write(buildPingReq()); }, ms);
    if (this._pingTimer.unref) this._pingTimer.unref();
  }
  _stopPing() { if (this._pingTimer) { clearInterval(this._pingTimer); this._pingTimer = null; } }

  _write(b) { try { if (this.sock && !this.sock.destroyed) this.sock.write(b); } catch (_) {} }

  _fail(err) {
    clearTimeout(this._connectTimer);
    this.emit('error', err);
    this._teardownSocket();
    this._maybeReconnect();
  }
  _onClose() {
    const wasConnected = this.connected;
    this.connected = false;
    this._stopPing();
    if (wasConnected) this.emit('close');
    this._maybeReconnect();
  }
  _teardownSocket() {
    this._stopPing();
    this.connected = false;
    try { if (this.sock) { this.sock.removeAllListeners(); this.sock.destroy(); } } catch (_) {}
    this.sock = null;
  }
  _maybeReconnect() {
    if (this.ended) return;
    const period = this.opts.reconnectPeriod;
    if (!period) return;
    if (this._reconnectTimer) return;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (!this.ended) { this.emit('reconnect'); this._open(); }
    }, period);
    if (this._reconnectTimer.unref) this._reconnectTimer.unref();
  }

  end(force) {
    this.ended = true;
    if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
    if (this.connected && !force) { try { this._write(buildDisconnect()); } catch (_) {} }
    this._teardownSocket();
    return this;
  }
}

module.exports = {
  MqttClient,
  // exported for tests
  encodeVarInt, decodeVarInt, buildConnect, buildSubscribe, buildPublish, parsePackets, parsePublish,
};
