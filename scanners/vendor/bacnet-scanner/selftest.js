'use strict';
// Offline self-test of the BACnet codec: build reference frames, parse them
// back through the real parser, and check the decoded fields.
const assert = require('assert');
const bacnet = require('./bacnet');
const I = bacnet._internal;

let passed = 0;
function ok(name, cond) { assert(cond, 'FAIL: ' + name); passed++; console.log('  ok  ' + name); }

// --- object-id encode/decode ---
const oid = I.encodeObjectId(8, 260001);
ok('object-id round-trips type', I.decodeObjectId(oid, 0).type === 8);
ok('object-id round-trips instance', I.decodeObjectId(oid, 0).instance === 260001);
const oidMax = I.encodeObjectId(1023, 4194303);
ok('object-id max type', I.decodeObjectId(oidMax, 0).type === 1023);
ok('object-id max instance', I.decodeObjectId(oidMax, 0).instance === 4194303);

// --- I-Am frame parse ---
// BVLC 81 0b <len> | NPDU 01 00 | APDU 10 00 | C4 <objid> | 22 05 c4 | 91 03 | 22 01 04
const iam = Buffer.concat([
  Buffer.from([0x81, 0x0b, 0x00, 0x00]),
  Buffer.from([0x01, 0x00]),
  Buffer.from([0x10, 0x00]),
  Buffer.from([0xc4]), I.encodeObjectId(8, 260001),
  Buffer.from([0x22, 0x05, 0xc4]),   // max-apdu 1476
  Buffer.from([0x91, 0x03]),         // segmentation 3
  Buffer.from([0x22, 0x01, 0x04]),   // vendor 260
]);
iam.writeUInt16BE(iam.length, 2);
const fr = I.parseFrame(iam, { address: '192.0.2.37', port: 47808 });
ok('frame kind = iam', fr.kind === 'iam');
const info = I.parseIAm(fr.buf, fr.apduOffset);
ok('I-Am instance', info.instance === 260001);
ok('I-Am maxApdu', info.maxApdu === 1476);
ok('I-Am segmentation', info.segmentation === 3);
ok('I-Am vendorId', info.vendorId === 260);
ok('I-Am carries source ip', fr.ip === '192.0.2.37');

// --- I-Am from a routed device (NPDU with SNET/SADR) ---
// NPDU control 0x08 = source present. SNET=2001, SLEN=1, SADR=0x0a
const iamRouted = Buffer.concat([
  Buffer.from([0x81, 0x0a, 0x00, 0x00]),
  Buffer.from([0x01, 0x08, 0x07, 0xd1, 0x01, 0x0a]), // ver, ctrl(src), SNET 2001, SLEN 1, SADR 0x0a
  Buffer.from([0x10, 0x00]),
  Buffer.from([0xc4]), I.encodeObjectId(8, 77),
  Buffer.from([0x21, 0x01]),         // max-apdu (unsigned 1 byte) = 1
  Buffer.from([0x91, 0x03]),
  Buffer.from([0x21, 0x18]),         // vendor 24 (Honeywell)
]);
iamRouted.writeUInt16BE(iamRouted.length, 2);
const fr2 = I.parseFrame(iamRouted, { address: '10.0.0.1', port: 47808 });
ok('routed frame kind = iam', fr2.kind === 'iam');
ok('routed SNET decoded', fr2.snet === 2001);
ok('routed SADR decoded', fr2.sadr && fr2.sadr[0] === 0x0a);
const info2 = I.parseIAm(fr2.buf, fr2.apduOffset);
ok('routed I-Am instance', info2.instance === 77);
ok('routed I-Am vendor 24', info2.vendorId === 24);

// --- ReadProperty complex-ack: object-name (char string) ---
// APDU: 30 <inv> 0c | 0c <objid> | 19 4d | 3e | 75 <len><enc><str> | 3f
const name = 'AHU-01';
const strBody = Buffer.concat([Buffer.from([0x00]), Buffer.from(name, 'utf8')]); // enc 0 + text
const strTag = Buffer.concat([Buffer.from([0x70 | 0x05, strBody.length]), strBody]); // char-string, ext-len
const ack = Buffer.concat([
  Buffer.from([0x81, 0x0a, 0x00, 0x00]),
  Buffer.from([0x01, 0x00]),
  Buffer.from([0x30, 0x01, 0x0c]),         // complex-ack, invoke 1, RP
  Buffer.from([0x0c]), I.encodeObjectId(8, 1001),
  Buffer.from([0x19, 0x4d]),               // ctx1 property = 77 (object-name)
  Buffer.from([0x3e]),                     // opening tag 3
  strTag,
  Buffer.from([0x3f]),                     // closing tag 3
]);
ack.writeUInt16BE(ack.length, 2);
const frAck = I.parseFrame(ack, { address: '10.0.10.15', port: 47808 });
ok('ack kind = complexAck', frAck.kind === 'complexAck');
ok('ack invokeId', frAck.invokeId === 1);
const vals = I.parseReadPropertyAck(frAck.buf, frAck.apduOffset);
ok('ack decodes name', vals.length === 1 && vals[0].value === 'AHU-01');

// --- ReadProperty complex-ack: object-list count (unsigned) ---
const ackCount = Buffer.concat([
  Buffer.from([0x81, 0x0a, 0x00, 0x00]),
  Buffer.from([0x01, 0x00]),
  Buffer.from([0x30, 0x02, 0x0c]),
  Buffer.from([0x0c]), I.encodeObjectId(8, 1001),
  Buffer.from([0x19, 0x4c]),               // property 76 (object-list)
  Buffer.from([0x29, 0x00]),               // ctx2 array index = 0
  Buffer.from([0x3e]),
  Buffer.from([0x22, 0x00, 0x2a]),         // unsigned = 42
  Buffer.from([0x3f]),
]);
ackCount.writeUInt16BE(ackCount.length, 2);
const frC = I.parseFrame(ackCount, { address: '10.0.10.15', port: 47808 });
const valsC = I.parseReadPropertyAck(frC.buf, frC.apduOffset);
ok('ack decodes object count 42', valsC.length === 1 && valsC[0].value === 42);

// --- ReadProperty complex-ack: object-list element (object id) ---
const ackElem = Buffer.concat([
  Buffer.from([0x81, 0x0a, 0x00, 0x00]),
  Buffer.from([0x01, 0x00]),
  Buffer.from([0x30, 0x03, 0x0c]),
  Buffer.from([0x0c]), I.encodeObjectId(8, 1001),
  Buffer.from([0x19, 0x4c]),
  Buffer.from([0x29, 0x01]),               // index 1
  Buffer.from([0x3e]),
  Buffer.from([0xc4]), I.encodeObjectId(0, 5), // analog-input:5
  Buffer.from([0x3f]),
]);
ackElem.writeUInt16BE(ackElem.length, 2);
const frE = I.parseFrame(ackElem, { address: '10.0.10.15', port: 47808 });
const valsE = I.parseReadPropertyAck(frE.buf, frE.apduOffset);
ok('ack decodes object id', valsE.length === 1 && valsE[0].value.type === 0 && valsE[0].value.instance === 5);

// --- present-value REAL ---
const pvBuf = Buffer.alloc(4); pvBuf.writeFloatBE(21.5, 0);
const ackReal = Buffer.concat([
  Buffer.from([0x81, 0x0a, 0x00, 0x00]),
  Buffer.from([0x01, 0x00]),
  Buffer.from([0x30, 0x04, 0x0c]),
  Buffer.from([0x0c]), I.encodeObjectId(0, 5),
  Buffer.from([0x19, 0x55]),               // property 85 present-value
  Buffer.from([0x3e]),
  Buffer.from([0x44]), pvBuf,              // real
  Buffer.from([0x3f]),
]);
ackReal.writeUInt16BE(ackReal.length, 2);
const frR = I.parseFrame(ackReal, { address: '10.0.10.15', port: 47808 });
const valsR = I.parseReadPropertyAck(frR.buf, frR.apduOffset);
ok('ack decodes real present-value', Math.abs(valsR[0].value - 21.5) < 0.001);

// --- I-Am-Router-To-Network ---
// NPDU control 0x80 (netmsg), msgType 0x01, then DNETs 2001, 2002
const router = Buffer.concat([
  Buffer.from([0x81, 0x0b, 0x00, 0x00]),
  Buffer.from([0x01, 0x80, 0x01]),         // ver, ctrl(netmsg), msgtype 1
  Buffer.from([0x07, 0xd1, 0x07, 0xd2]),   // 2001, 2002
]);
router.writeUInt16BE(router.length, 2);
const frRt = I.parseFrame(router, { address: '10.0.0.1', port: 47808 });
ok('router frame kind', frRt.kind === 'router');
ok('router networks', frRt.networks.length === 2 && frRt.networks[0] === 2001 && frRt.networks[1] === 2002);

// --- ReadProperty request builder (encUnsigned + ctx tags) ---
ok('encUnsigned 1 byte', I.encUnsigned(42).equals(Buffer.from([0x2a])));
ok('encUnsigned 2 byte', I.encUnsigned(300).equals(Buffer.from([0x01, 0x2c])));
ok('ctxTagHeader prop', I.ctxTagHeader(1, 1).equals(Buffer.from([0x19])));
ok('ctxTagHeader index', I.ctxTagHeader(2, 1).equals(Buffer.from([0x29])));

// --- lookups ---
ok('objectTypeName device', bacnet.objectTypeName(8) === 'device');
ok('unitName °C', bacnet.unitName(62) === '°C');
ok('vendorName JCI', /Johnson/.test(bacnet.vendorName(5)));
ok('ip round-trip', bacnet.intToIp(bacnet.ipToInt('192.0.2.37')) === '192.0.2.37');

console.log(`\n${passed} checks passed.`);
