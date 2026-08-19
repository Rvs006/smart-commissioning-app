'use strict';
// Integration test over real UDP loopback sockets: a simulated BACnet device
// answers Who-Is (I-Am) and ReadProperty, and we drive the real client through
// discovery, identity read, and object-list enumeration.
const dgram = require('dgram');
const assert = require('assert');
const bacnet = require('./bacnet');
const I = bacnet._internal;

const SIM_PORT = 47810;
let passed = 0;
function ok(name, cond) { assert(cond, 'FAIL: ' + name); passed++; console.log('  ok  ' + name); }

// ---- helpers to build BACnet primitives (server-side of the wire) ----
function charString(s) {
  const body = Buffer.concat([Buffer.from([0x00]), Buffer.from(s, 'utf8')]);
  return Buffer.concat([Buffer.from([0x70 | 0x05, body.length]), body]); // char-string app tag, ext-len
}
function unsigned(n) {
  const b = I.encUnsigned(n);
  return Buffer.concat([Buffer.from([0x20 | b.length]), b]); // unsigned app tag
}
function enumerated(n) {
  const b = I.encUnsigned(n);
  return Buffer.concat([Buffer.from([0x90 | b.length]), b]); // enumerated app tag
}
function real(f) { const b = Buffer.alloc(4); b.writeFloatBE(f, 0); return Buffer.concat([Buffer.from([0x44]), b]); }
function objidTag(t, i) { return Buffer.concat([Buffer.from([0xc4]), I.encodeObjectId(t, i)]); }

function complexAck(invoke, objType, objInst, propId, arrayIndex, valueBuf) {
  const parts = [Buffer.from([0x30, invoke, 0x0c])];
  parts.push(Buffer.from([0x0c]), I.encodeObjectId(objType, objInst));
  const pid = I.encUnsigned(propId);
  parts.push(I.ctxTagHeader(1, pid.length), pid);
  if (arrayIndex != null) { const ai = I.encUnsigned(arrayIndex); parts.push(I.ctxTagHeader(2, ai.length), ai); }
  parts.push(Buffer.from([0x3e]), valueBuf, Buffer.from([0x3f]));
  const apdu = Buffer.concat(parts);
  const npdu = Buffer.from([0x01, 0x00]);
  return I.bvlc(0x0a, Buffer.concat([npdu, apdu]));
}
function errorPdu(invoke) {
  // error-class(device=0)/error-code(unknown-property=32) — minimal
  const apdu = Buffer.from([0x50, invoke, 0x0c, 0x91, 0x00, 0x91, 0x20]);
  return I.bvlc(0x0a, Buffer.concat([Buffer.from([0x01, 0x00]), apdu]));
}
function iAm(instance) {
  const apdu = Buffer.concat([
    Buffer.from([0x10, 0x00]),
    objidTag(8, instance),
    unsigned(1476), enumerated(3), unsigned(260),
  ]);
  const npdu = Buffer.from([0x01, 0x00]);
  return I.bvlc(0x0b, Buffer.concat([npdu, apdu]));
}

// ---- parse an incoming ReadProperty request (client → sim) ----
function parseRP(buf) {
  // BVLC(4) + NPDU(2, control 0x04) + APDU
  const a = 6;
  if (((buf[a] >> 4) & 0x0f) !== 0) return null; // not confirmed-request
  const invoke = buf[a + 2];
  const service = buf[a + 3];
  if (service !== 0x0c) return null;
  let p = a + 4;
  // ctx0 object id: tag 0x0c + 4 bytes
  p += 1; const oid = I.decodeObjectId(buf, p); p += 4;
  // ctx1 property id
  let h = readCtx(buf, p); const propId = buf.readUIntBE(h.dataOff, h.len); p = h.dataOff + h.len;
  let arrayIndex = null;
  if (p < buf.length) { // optional ctx2 index
    const h2 = readCtx(buf, p);
    if (h2.tagNum === 2) { arrayIndex = buf.readUIntBE(h2.dataOff, h2.len); p = h2.dataOff + h2.len; }
  }
  return { invoke, objType: oid.type, objInst: oid.instance, propId, arrayIndex };
}
function readCtx(buf, p) {
  const tag = buf[p];
  const tagNum = (tag >> 4) & 0x0f;
  const len = tag & 0x07;
  return { tagNum, len, dataOff: p + 1 };
}

// ---- the simulated device's property database ----
const P = { name: 77, list: 76, pv: 85, units: 117, vendorName: 121, model: 70, fw: 44, appSw: 12, loc: 58, desc: 28, protoRev: 139, sysStatus: 112 };
function answer(req) {
  const { invoke, objType, objInst, propId, arrayIndex } = req;
  // Device object 1001
  if (objType === 8 && objInst === 1001) {
    if (propId === P.name) return complexAck(invoke, 8, 1001, P.name, null, charString('AHU-01'));
    if (propId === P.vendorName) return complexAck(invoke, 8, 1001, P.vendorName, null, charString('Acme Controls'));
    if (propId === P.model) return complexAck(invoke, 8, 1001, P.model, null, charString('AC-9000'));
    if (propId === P.fw) return complexAck(invoke, 8, 1001, P.fw, null, charString('1.4.2'));
    if (propId === P.appSw) return complexAck(invoke, 8, 1001, P.appSw, null, charString('app-7'));
    if (propId === P.loc) return complexAck(invoke, 8, 1001, P.loc, null, charString('Plant Room'));
    if (propId === P.desc) return complexAck(invoke, 8, 1001, P.desc, null, charString('Air handling unit'));
    if (propId === P.protoRev) return complexAck(invoke, 8, 1001, P.protoRev, null, unsigned(14));
    if (propId === P.sysStatus) return complexAck(invoke, 8, 1001, P.sysStatus, null, enumerated(0));
    if (propId === P.list) {
      if (arrayIndex === 0) return complexAck(invoke, 8, 1001, P.list, 0, unsigned(2));
      if (arrayIndex === 1) return complexAck(invoke, 8, 1001, P.list, 1, objidTag(0, 5));
      if (arrayIndex === 2) return complexAck(invoke, 8, 1001, P.list, 2, objidTag(3, 3));
    }
  }
  // Analog-input 5
  if (objType === 0 && objInst === 5) {
    if (propId === P.name) return complexAck(invoke, 0, 5, P.name, null, charString('SpaceTemp'));
    if (propId === P.pv) return complexAck(invoke, 0, 5, P.pv, null, real(21.5));
    if (propId === P.units) return complexAck(invoke, 0, 5, P.units, null, enumerated(62));
  }
  // Binary-input 3
  if (objType === 3 && objInst === 3) {
    if (propId === P.name) return complexAck(invoke, 3, 3, P.name, null, charString('OccStatus'));
    if (propId === P.pv) return complexAck(invoke, 3, 3, P.pv, null, enumerated(1));
  }
  return errorPdu(invoke);
}

async function main() {
  const sim = dgram.createSocket({ type: 'udp4', reuseAddr: true });
  await new Promise((r) => sim.bind(SIM_PORT, r));
  sim.on('message', (msg, rinfo) => {
    // Respond to Who-Is with an I-Am; respond to ReadProperty with data.
    if (msg[0] === 0x81 && msg.length >= 8) {
      const a = 6;
      const pduType = (msg[a] >> 4) & 0x0f;
      if (pduType === 1 && msg[a + 1] === 0x08) {
        // Who-Is → I-Am back to sender
        sim.send(iAm(1001), rinfo.port, rinfo.address);
        return;
      }
      if (pduType === 0) {
        const req = parseRP(msg);
        if (req) sim.send(answer(req), rinfo.port, rinfo.address);
      }
    }
  });

  const client = bacnet.createClient();
  await client.bind();

  // --- discovery: point the "broadcast" at the sim's unicast address ---
  const devices = await client.discover({ broadcast: '127.0.0.1', timeoutMs: 700 });
  // Note: sendWhoIs targets port 47808 (the client). The sim listens on 47810,
  // so drive discovery explicitly by having the client send a directed Who-Is.
  // (Handled below via a manual read path instead.)

  // Directly exercise the confirmed path against the sim device.
  const target = { ip: '127.0.0.1', port: SIM_PORT };
  const name = await client.readOne(target, 8, 1001, 77, null, 1000);
  ok('reads device object-name over UDP', name === 'AHU-01');

  const count = await client.readOne(target, 8, 1001, 76, 0, 1000);
  ok('reads object-list count over UDP', count === 2);

  const dev = { instance: 1001, ip: '127.0.0.1', port: SIM_PORT, network: 0, mac: '', vendor: 'Acme Controls', vendorId: 260 };
  const id = await bacnet.readDeviceIdentity(client, dev, 1000);
  ok('identity: name', id.name === 'AHU-01');
  ok('identity: vendor-name', id.vendor === 'Acme Controls');
  ok('identity: model', id.model === 'AC-9000');
  ok('identity: firmware', id.firmware === '1.4.2');
  ok('identity: object count', id.objectCount === 2);
  ok('identity: protocol rev', id.protocolRevision === 14);
  ok('identity: system status', id.systemStatus === 'operational');

  const list = await bacnet.readObjectList(client, dev, { cap: 200, timeout: 1000 });
  ok('object-list length', list.objects.length === 2);
  ok('object-list not truncated', list.truncated === false);
  const ai = list.objects.find((o) => o.typeName === 'analog-input');
  ok('analog-input present', !!ai && ai.instance === 5);
  ok('analog-input name', ai.name === 'SpaceTemp');
  ok('analog-input present-value', ai.presentValue === '21.50');
  ok('analog-input units °C', ai.units === '°C');
  const bi = list.objects.find((o) => o.typeName === 'binary-input');
  ok('binary-input name', bi && bi.name === 'OccStatus');
  ok('binary-input pv active', bi && bi.presentValue === 'active');

  // discovery path over the real socket: sim sends an I-Am to the client port.
  const found = [];
  const disc = client.discover({ broadcast: '127.0.0.1', timeoutMs: 500, onDevice: (d) => found.push(d) });
  // Simulate a device broadcasting an unsolicited I-Am to the client's port.
  sim.send(iAm(2002), bacnet.BACNET_PORT, '127.0.0.1');
  await disc;
  ok('discovery receives I-Am over UDP', found.some((d) => d.instance === 2002));

  client.close();
  sim.close();
  console.log(`\n${passed} integration checks passed.`);
}

main().catch((e) => { console.error(e); process.exit(1); });
