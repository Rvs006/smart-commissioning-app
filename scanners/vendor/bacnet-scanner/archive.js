'use strict';
/*
 * archive.js — zero-dependency ZIP and XLSX writers.
 *
 * A .xlsx file is itself a ZIP of OOXML parts, so the ZIP writer serves double
 * duty: it builds each per-device .xlsx AND the outer export archive. Entries
 * are STORED (no compression) — valid ZIP, valid XLSX, and needs no zlib.
 */

// ---- CRC-32 (needed for ZIP local/central headers) ----
const CRC_TABLE = (() => {
  const t = new Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// ---- ZIP writer (stored entries) ----
// entries: [{ name: 'path/in/zip', data: Buffer }]
function makeZip(entries) {
  const chunks = [];
  const central = [];
  let offset = 0;
  for (const e of entries) {
    const nameBuf = Buffer.from(e.name, 'utf8');
    const data = Buffer.isBuffer(e.data) ? e.data : Buffer.from(e.data);
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); // local file header sig
    local.writeUInt16LE(20, 4);         // version needed
    local.writeUInt16LE(0x0800, 6);     // flags: UTF-8 names
    local.writeUInt16LE(0, 8);          // method 0 = store
    local.writeUInt16LE(0, 10);         // mod time
    local.writeUInt16LE(0x21, 12);      // mod date (1980-01-01, arbitrary valid)
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    chunks.push(local, nameBuf, data);

    const cen = Buffer.alloc(46);
    cen.writeUInt32LE(0x02014b50, 0);   // central dir header sig
    cen.writeUInt16LE(20, 4);           // version made by
    cen.writeUInt16LE(20, 6);           // version needed
    cen.writeUInt16LE(0x0800, 8);       // flags
    cen.writeUInt16LE(0, 10);           // method
    cen.writeUInt16LE(0, 12);           // mod time
    cen.writeUInt16LE(0x21, 14);        // mod date
    cen.writeUInt32LE(crc, 16);
    cen.writeUInt32LE(data.length, 20);
    cen.writeUInt32LE(data.length, 24);
    cen.writeUInt16LE(nameBuf.length, 28);
    cen.writeUInt16LE(0, 30);           // extra len
    cen.writeUInt16LE(0, 32);           // comment len
    cen.writeUInt16LE(0, 34);           // disk #
    cen.writeUInt16LE(0, 36);           // internal attrs
    cen.writeUInt32LE(0, 38);           // external attrs
    cen.writeUInt32LE(offset, 42);      // local header offset
    central.push(cen, nameBuf);

    offset += 30 + nameBuf.length + data.length;
  }
  const centralBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);     // end of central dir sig
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...chunks, centralBuf, end]);
}

// ---- XLSX writer (single sheet, inline strings) ----
function xmlEscape(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));
}
function colLetter(n) {
  let s = '';
  n++;
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}
function isNumeric(v) {
  return v !== '' && v != null && /^-?\d+(\.\d+)?$/.test(String(v));
}
function sheetXml(rows2d) {
  let body = '';
  rows2d.forEach((row, ri) => {
    const r = ri + 1;
    let cells = '';
    row.forEach((val, ci) => {
      const ref = colLetter(ci) + r;
      if ((typeof val === 'number' && isFinite(val)) || isNumeric(val)) {
        cells += `<c r="${ref}"><v>${val}</v></c>`;
      } else {
        cells += `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xmlEscape(val)}</t></is></c>`;
      }
    });
    body += `<row r="${r}">${cells}</row>`;
  });
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
    `<sheetData>${body}</sheetData></worksheet>`;
}
const XLSX_CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
  '<Default Extension="xml" ContentType="application/xml"/>' +
  '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
  '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
  '</Types>';
const XLSX_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
  '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
  '</Relationships>';
const XLSX_WORKBOOK = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
  '<sheets><sheet name="Points" sheetId="1" r:id="rId1"/></sheets></workbook>';
const XLSX_WB_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
  '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
  '</Relationships>';

function buildXlsx(rows2d) {
  return makeZip([
    { name: '[Content_Types].xml', data: Buffer.from(XLSX_CONTENT_TYPES, 'utf8') },
    { name: '_rels/.rels', data: Buffer.from(XLSX_RELS, 'utf8') },
    { name: 'xl/workbook.xml', data: Buffer.from(XLSX_WORKBOOK, 'utf8') },
    { name: 'xl/_rels/workbook.xml.rels', data: Buffer.from(XLSX_WB_RELS, 'utf8') },
    { name: 'xl/worksheets/sheet1.xml', data: Buffer.from(sheetXml(rows2d), 'utf8') },
  ]);
}

// Strip characters illegal in Windows filenames / zip paths.
function sanitizeFilename(s) {
  return String(s || '')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 80) || 'device';
}

module.exports = { makeZip, buildXlsx, sanitizeFilename, crc32 };
