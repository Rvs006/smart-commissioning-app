'use strict';
/*
 * zip.js — a tiny, dependency-free ZIP archive writer.
 *
 * The app ships as a single SEA executable with ZERO runtime dependencies, and
 * Node has no built-in ZIP *writer*, so we hand-roll the minimal slice of the
 * ZIP (PKZIP / APPNOTE) format needed to emit a normal, Windows-double-clickable
 * archive: a local file header + DEFLATE-compressed data per entry, then a
 * central directory and end-of-central-directory record. Compression uses the
 * built-in zlib; the checksum is a hand-rolled CRC-32.
 *
 *   const zip = new Zip();
 *   zip.add('folder/file.json', Buffer.from('...'));
 *   const buf = zip.build();          // → Buffer (the .zip)
 */

const zlib = require('zlib');

// CRC-32 (IEEE 802.3) with a lazily-built lookup table.
let CRC_TABLE = null;
function crcTable() {
  if (CRC_TABLE) return CRC_TABLE;
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  CRC_TABLE = t;
  return t;
}
function crc32(buf) {
  const t = crcTable();
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = t[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// MS-DOS date/time from an epoch-ms value (both are required, no runtime clock).
function dosDateTime(ms) {
  const d = new Date(ms || 0);
  const time = ((d.getUTCHours() & 0x1f) << 11) | ((d.getUTCMinutes() & 0x3f) << 5) | ((Math.floor(d.getUTCSeconds() / 2)) & 0x1f);
  const year = Math.max(1980, d.getUTCFullYear()) - 1980;
  const date = ((year & 0x7f) << 9) | (((d.getUTCMonth() + 1) & 0x0f) << 5) | (d.getUTCDate() & 0x1f);
  return { time, date };
}

class Zip {
  constructor(mtimeMs) {
    this.entries = [];
    this.mtimeMs = mtimeMs || 0; // pass a timestamp in; do not read the clock here
  }

  // path uses forward slashes; data is a Buffer or string.
  add(path, data) {
    const nameBuf = Buffer.from(String(path).replace(/\\/g, '/'), 'utf8');
    const raw = Buffer.isBuffer(data) ? data : Buffer.from(String(data), 'utf8');
    const deflated = zlib.deflateRawSync(raw);
    // Only compress if it actually helps; else store (method 0).
    const useDeflate = deflated.length < raw.length;
    this.entries.push({
      nameBuf,
      crc: crc32(raw),
      compSize: useDeflate ? deflated.length : raw.length,
      uncompSize: raw.length,
      method: useDeflate ? 8 : 0,
      body: useDeflate ? deflated : raw,
    });
  }

  build() {
    const { time, date } = dosDateTime(this.mtimeMs);
    const chunks = [];
    const central = [];
    let offset = 0;

    for (const e of this.entries) {
      const local = Buffer.alloc(30);
      local.writeUInt32LE(0x04034b50, 0);      // local file header sig
      local.writeUInt16LE(20, 4);              // version needed
      local.writeUInt16LE(0x0800, 6);          // flags: UTF-8 filename
      local.writeUInt16LE(e.method, 8);        // compression method
      local.writeUInt16LE(time, 10);
      local.writeUInt16LE(date, 12);
      local.writeUInt32LE(e.crc, 14);
      local.writeUInt32LE(e.compSize, 18);
      local.writeUInt32LE(e.uncompSize, 22);
      local.writeUInt16LE(e.nameBuf.length, 26);
      local.writeUInt16LE(0, 28);              // extra field length
      chunks.push(local, e.nameBuf, e.body);

      const cd = Buffer.alloc(46);
      cd.writeUInt32LE(0x02014b50, 0);         // central dir header sig
      cd.writeUInt16LE(20, 4);                 // version made by
      cd.writeUInt16LE(20, 6);                 // version needed
      cd.writeUInt16LE(0x0800, 8);             // flags: UTF-8
      cd.writeUInt16LE(e.method, 10);
      cd.writeUInt16LE(time, 12);
      cd.writeUInt16LE(date, 14);
      cd.writeUInt32LE(e.crc, 16);
      cd.writeUInt32LE(e.compSize, 20);
      cd.writeUInt32LE(e.uncompSize, 24);
      cd.writeUInt16LE(e.nameBuf.length, 28);
      cd.writeUInt16LE(0, 30);                 // extra len
      cd.writeUInt16LE(0, 32);                 // comment len
      cd.writeUInt16LE(0, 34);                 // disk number
      cd.writeUInt16LE(0, 36);                 // internal attrs
      cd.writeUInt32LE(0, 38);                 // external attrs
      cd.writeUInt32LE(offset, 42);            // local header offset
      central.push(Buffer.concat([cd, e.nameBuf]));

      offset += local.length + e.nameBuf.length + e.body.length;
    }

    const centralBuf = Buffer.concat(central);
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);         // end of central directory sig
    eocd.writeUInt16LE(0, 4);                  // disk number
    eocd.writeUInt16LE(0, 6);                  // disk with central dir
    eocd.writeUInt16LE(this.entries.length, 8);
    eocd.writeUInt16LE(this.entries.length, 10);
    eocd.writeUInt32LE(centralBuf.length, 12);
    eocd.writeUInt32LE(offset, 16);            // offset of central dir
    eocd.writeUInt16LE(0, 20);                 // comment length

    return Buffer.concat([...chunks, centralBuf, eocd]);
  }
}

module.exports = { Zip, crc32 };
