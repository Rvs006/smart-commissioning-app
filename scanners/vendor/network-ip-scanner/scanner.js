'use strict';
/*
 * scanner.js — real LAN discovery engine for Windows.
 *
 * Discovery strategy (mirrors what tools like Advanced IP Scanner do):
 *   1. ICMP ping every host in range (spawns the built-in ping.exe). This also
 *      forces the OS to ARP-resolve every live host on the local subnet.
 *   2. Read the ARP cache (arp -a). Any IP that resolved to a MAC is PRESENT on
 *      the wire even if it dropped our ping — this catches ping-silent devices.
 *   3. TCP connect-scan the configured ports on every candidate host. Open TCP
 *      ports also prove a host is up regardless of ICMP/ARP.
 *   4. UDP service probes for protocols that don't do TCP — notably BACnet/IP
 *      (UDP 47808) via a Who-Is, and SNMP (UDP 161).
 *   5. Enrich: reverse DNS + NetBIOS name, MAC vendor (OUI), service names,
 *      HTTP banner (Server header).
 *
 * Everything is concurrency-limited so a /24 (or larger) completes without
 * spawning thousands of processes or sockets at once.
 */

const os = require('os');
const net = require('net');
const tls = require('tls');
const dgram = require('dgram');
const dns = require('dns');
const { execFile } = require('child_process');
const { OUI } = require('./oui');

// ---------------------------------------------------------------------------
// IP helpers
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
function prefixToMask(prefix) {
  const m = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return intToIp(m);
}

// Build the list of host IPs to scan given a start/end (inclusive).
function expandRange(startIp, endIp) {
  const s = ipToInt(startIp);
  const e = ipToInt(endIp);
  const out = [];
  for (let i = s; i <= e; i++) out.push(intToIp(i));
  return out;
}

// Derive a sensible default scan range for an adapter (its whole subnet,
// excluding network + broadcast addresses).
function adapterRange(ip, netmask) {
  const prefix = maskToPrefix(netmask);
  const ipInt = ipToInt(ip);
  const maskInt = ipToInt(netmask);
  const network = (ipInt & maskInt) >>> 0;
  const broadcast = (network | (~maskInt >>> 0)) >>> 0;
  // For a /31 or /32 there are no "usable host" gaps; just scan what we can.
  const start = prefix >= 31 ? network : (network + 1) >>> 0;
  const end = prefix >= 31 ? broadcast : (broadcast - 1) >>> 0;
  return { start: intToIp(start), end: intToIp(end), prefix, count: end - start + 1 };
}

// ---------------------------------------------------------------------------
// Concurrency pool
// ---------------------------------------------------------------------------
async function pool(items, limit, worker, onEach) {
  const results = new Array(items.length);
  let idx = 0;
  async function run() {
    while (idx < items.length) {
      const cur = idx++;
      try {
        results[cur] = await worker(items[cur], cur);
      } catch (e) {
        results[cur] = { error: String(e) };
      }
      if (onEach) onEach(results[cur], cur);
    }
  }
  const runners = [];
  for (let i = 0; i < Math.min(limit, items.length); i++) runners.push(run());
  await Promise.all(runners);
  return results;
}

// ---------------------------------------------------------------------------
// Adapter discovery
// ---------------------------------------------------------------------------
function execFileP(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    execFile(cmd, args, { windowsHide: true, timeout: 15000, ...opts }, (err, stdout, stderr) => {
      resolve({ err, stdout: stdout || '', stderr: stderr || '' });
    });
  });
}

// Pull friendly descriptions + link status/speed from Windows so the operator
// can tell adapters apart (os.networkInterfaces only gives terse names).
async function windowsAdapterMeta() {
  const meta = {};
  const ps =
    'Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,MacAddress | ConvertTo-Json -Compress';
  const { stdout } = await execFileP(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', ps]
  );
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
    /* PowerShell unavailable or blocked — fall back to os module only */
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
      const range = adapterRange(a.address, a.netmask);
      const m = meta[name] || {};
      out.push({
        name,
        description: m.description || name,
        status: m.status || 'Unknown',
        linkSpeed: m.linkSpeed || '',
        ip: a.address,
        netmask: a.netmask,
        mac: (a.mac || m.mac || '').toLowerCase(),
        cidr: `${a.address}/${range.prefix}`,
        prefix: range.prefix,
        suggestedRange: { start: range.start, end: range.end },
        hostCount: range.count,
      });
    }
  }
  // Prefer connected, real (non-virtual) adapters at the top.
  out.sort((x, y) => {
    const up = (v) => (String(v.status).toLowerCase() === 'up' ? 0 : 1);
    return up(x) - up(y);
  });
  return out;
}

// ---------------------------------------------------------------------------
// ICMP ping (built-in ping.exe)
// ---------------------------------------------------------------------------
async function pingHost(ip, timeout) {
  const { stdout } = await execFileP('ping', ['-n', '1', '-w', String(timeout), ip]);
  const text = stdout.toLowerCase();
  // "reply from x" but NOT "destination host unreachable"/"timed out"
  const gotReply =
    /reply from/.test(text) &&
    !/unreachable/.test(text) &&
    !/timed out/.test(text) &&
    !/request timed out/.test(text);
  let latency = null;
  const m = stdout.match(/time[=<]\s*([0-9]+)\s*ms/i);
  if (m) latency = Number(m[1]);
  else if (/time<1ms/i.test(stdout)) latency = 0.5;
  return { alive: gotReply, latency };
}

// ---------------------------------------------------------------------------
// ARP cache
// ---------------------------------------------------------------------------
async function arpTable() {
  const { stdout } = await execFileP('arp', ['-a']);
  const map = {};
  for (const line of stdout.split(/\r?\n/)) {
    const m = line.match(/^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{11,17})\s+(\w+)/);
    if (m) {
      const ip = m[1];
      const mac = m[2].toLowerCase().replace(/-/g, ':');
      if (mac !== 'ff:ff:ff:ff:ff:ff' && !ip.startsWith('224.') && !ip.startsWith('239.')) {
        map[ip] = mac;
      }
    }
  }
  return map;
}

function ouiVendor(mac) {
  if (!mac) return '';
  const prefix = mac.toUpperCase().replace(/[:-]/g, '').slice(0, 6);
  return OUI[prefix] || '';
}

// ---------------------------------------------------------------------------
// TCP connect scan
// ---------------------------------------------------------------------------
function tcpConnect(ip, port, timeout) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let done = false;
    const finish = (open) => {
      if (done) return;
      done = true;
      sock.destroy();
      resolve(open);
    };
    sock.setTimeout(timeout);
    sock.once('connect', () => finish(true));
    sock.once('timeout', () => finish(false));
    sock.once('error', () => finish(false));
    sock.connect(port, ip);
  });
}

// ---------------------------------------------------------------------------
// Service fingerprinting — identify the software/product + version behind an
// open port (like Advanced IP Scanner surfaces "nginx 1.18.0" on HTTP). Uses:
//   * HTTP/HTTPS: GET / then read Server / X-Powered-By headers + <title>
//   * HTTPS/TLS:  also read the certificate subject CN (device identity)
//   * SSH/FTP/SMTP/POP3/IMAP/VNC etc.: read the server-first greeting banner
//   * anything else: capture the first printable line it sends
// ---------------------------------------------------------------------------
const TLS_PORTS = new Set([443, 8443, 9443, 4443, 10443, 993, 995, 465, 636, 990, 989, 5986, 4911, 8883, 8834, 7443]);
const HTTP_PROBE_PORTS = new Set([
  80, 81, 82, 83, 84, 85, 88, 280, 591, 593, 631, 3000, 5000, 7080, 8000, 8008,
  8009, 8010, 8020, 8042, 8060, 8080, 8081, 8082, 8083, 8085, 8086, 8087, 8088,
  8089, 8090, 8091, 8098, 8099, 8123, 8161, 8180, 8888, 8983, 9000, 9080, 9090,
  9091, 9200, 10000, 55443,
]);

function httpReq(host) {
  return `GET / HTTP/1.1\r\nHost: ${host}\r\nUser-Agent: NetworkIPScanner\r\nAccept: */*\r\nConnection: close\r\n\r\n`;
}
function matchHeader(text, name) {
  const m = text.match(new RegExp('^' + name + ':\\s*(.+)$', 'im'));
  return m ? m[1].trim() : '';
}
function splitProduct(s) {
  const m = String(s).match(/^([A-Za-z0-9_.+\-]+)[\/ ]?v?([0-9][\w.]*)?/);
  if (!m) return { product: String(s).trim(), version: '' };
  return { product: m[1], version: m[2] || '' };
}
function decodeEntities(s) {
  return String(s)
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ').trim();
}
function parseFingerprint(text, port, isTls, cert) {
  const out = { product: '', version: '', title: '', info: '', certCN: '', tls: !!isTls };
  if (cert && cert.subject) {
    out.certCN = cert.subject.CN || '';
    if (!out.certCN && cert.subjectaltname) {
      out.certCN = cert.subjectaltname.replace(/DNS:/g, '').split(',')[0].trim();
    }
  }
  if (!text) return out;
  const firstLine = (text.split(/\r?\n/)[0] || '').trim();
  if (/^HTTP\//.test(text)) {
    const server = matchHeader(text, 'Server');
    const powered = matchHeader(text, 'X-Powered-By');
    const titleM = text.match(/<title[^>]*>\s*([^<]{1,150})/i);
    if (titleM) out.title = decodeEntities(titleM[1]);
    if (server) { const pv = splitProduct(server); out.product = pv.product; out.version = pv.version; out.info = server; }
    if (!out.product && powered) { const pv = splitProduct(powered); out.product = pv.product; out.version = pv.version; out.info = powered; }
    if (!out.title) {
      const auth = matchHeader(text, 'WWW-Authenticate');
      const rm = auth && auth.match(/realm="?([^"]+)"?/i);
      if (rm) out.title = rm[1];
    }
  } else if (/^SSH-/.test(firstLine)) {
    out.info = firstLine;
    const m = firstLine.match(/^SSH-[\d.]+-(\S+)/);
    if (m) {
      const um = m[1].match(/^([A-Za-z][A-Za-z_]*)[_-]?([0-9][\w.]*)?/);
      if (um) { out.product = um[1].replace(/_/g, ''); out.version = um[2] || ''; } else out.product = m[1];
    }
  } else if (/^220[ -]/.test(firstLine)) {
    out.info = firstLine.replace(/^220[ -]/, '').trim();
    const m = out.info.match(/\(?([A-Za-z][A-Za-z0-9_+.\-]*)[ /]v?([0-9][\w.]*)\)?/);
    if (m) { out.product = m[1]; out.version = m[2]; }
  } else if (/^RFB /.test(firstLine)) {
    out.product = 'VNC'; out.info = firstLine.trim();
  } else if (firstLine) {
    out.info = firstLine.replace(/[^\x20-\x7e]/g, '').slice(0, 120);
  }
  return out;
}

function tcpFingerprint(ip, port, timeout) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let data = Buffer.alloc(0);
    let done = false;
    let sentProbe = false;
    const t = Math.min(Math.max(timeout, 800), 2500);
    const finish = () => {
      if (done) return;
      done = true;
      try { sock.destroy(); } catch (_) {}
      resolve(parseFingerprint(data.toString('latin1'), port, false, null));
    };
    sock.setTimeout(t);
    sock.once('connect', () => {
      if (HTTP_PROBE_PORTS.has(port)) { try { sock.write(httpReq(ip)); sentProbe = true; } catch (_) {} }
      else setTimeout(() => {
        // Server said nothing → it may be waiting for a request; try HTTP.
        if (!done && data.length === 0 && !sentProbe) { try { sock.write(httpReq(ip)); sentProbe = true; } catch (_) {} }
      }, 500);
    });
    sock.on('data', (d) => { data = Buffer.concat([data, d]); if (data.length > 8192) finish(); });
    sock.once('timeout', finish);
    sock.once('error', finish);
    sock.once('close', finish);
    sock.connect(port, ip);
  });
}

function tlsFingerprint(ip, port, timeout) {
  return new Promise((resolve) => {
    let data = Buffer.alloc(0);
    let done = false;
    let cert = null;
    const t = Math.min(Math.max(timeout, 900), 3000);
    let sock;
    const finish = () => {
      if (done) return;
      done = true;
      try { sock.destroy(); } catch (_) {}
      resolve(parseFingerprint(data.toString('latin1'), port, true, cert));
    };
    try {
      sock = tls.connect({ host: ip, port, rejectUnauthorized: false, timeout: t }, () => {
        try { cert = sock.getPeerCertificate(); } catch (_) {}
        try { sock.write(httpReq(ip)); } catch (_) {}
      });
    } catch (_) { return resolve(parseFingerprint('', port, true, null)); }
    sock.setTimeout(t);
    sock.on('data', (d) => { data = Buffer.concat([data, d]); if (data.length > 8192) finish(); });
    sock.once('timeout', finish);
    sock.once('error', finish);
    sock.once('close', finish);
  });
}

function fingerprintPort(ip, port, timeout) {
  return TLS_PORTS.has(port) ? tlsFingerprint(ip, port, timeout) : tcpFingerprint(ip, port, timeout);
}

// ---------------------------------------------------------------------------
// UDP probes — BACnet/IP Who-Is and SNMP
// ---------------------------------------------------------------------------
// BACnet/IP Who-Is (unconfirmed request, global broadcast) — a live BACnet
// device answers with an I-Am. BVLC(4) + NPDU(2) + APDU(4).
function bacnetWhoIs(ip, timeout) {
  return new Promise((resolve) => {
    const sock = dgram.createSocket('udp4');
    const whoIs = Buffer.from([
      0x81, 0x0a, 0x00, 0x0c, // BVLC: type, Original-Unicast-NPDU, length 12
      0x01, 0x20, 0xff, 0xff, 0x00, 0xff, // NPDU with dest network broadcast
      0x10, 0x08, // APDU: unconfirmed-req, Who-Is
    ]);
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      try { sock.close(); } catch (_) {}
      resolve(v);
    };
    const timer = setTimeout(() => finish(false), timeout);
    sock.once('error', () => { clearTimeout(timer); finish(false); });
    sock.once('message', () => { clearTimeout(timer); finish(true); });
    sock.bind(() => {
      try { sock.send(whoIs, 0, whoIs.length, 47808, ip, (e) => { if (e) { clearTimeout(timer); finish(false); } }); }
      catch (_) { clearTimeout(timer); finish(false); }
    });
  });
}

// SNMP v1/v2c GET of sysDescr (public community) — a response proves SNMP.
function snmpProbe(ip, timeout) {
  return new Promise((resolve) => {
    const sock = dgram.createSocket('udp4');
    // GetRequest for 1.3.6.1.2.1.1.1.0 (sysDescr), community "public", v2c
    const pkt = Buffer.from([
      0x30, 0x26, 0x02, 0x01, 0x01, 0x04, 0x06, 0x70, 0x75, 0x62,
      0x6c, 0x69, 0x63, 0xa0, 0x19, 0x02, 0x04, 0x00, 0x00, 0x00,
      0x01, 0x02, 0x01, 0x00, 0x02, 0x01, 0x00, 0x30, 0x0b, 0x30,
      0x09, 0x06, 0x05, 0x2b, 0x06, 0x01, 0x02, 0x01, 0x05, 0x00,
    ]);
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      try { sock.close(); } catch (_) {}
      resolve(v);
    };
    const timer = setTimeout(() => finish(false), timeout);
    sock.once('error', () => { clearTimeout(timer); finish(false); });
    sock.once('message', () => { clearTimeout(timer); finish(true); });
    sock.bind(() => {
      try { sock.send(pkt, 0, pkt.length, 161, ip, (e) => { if (e) { clearTimeout(timer); finish(false); } }); }
      catch (_) { clearTimeout(timer); finish(false); }
    });
  });
}

// ---------------------------------------------------------------------------
// Hostname resolution
// ---------------------------------------------------------------------------
function reverseDns(ip) {
  return new Promise((resolve) => {
    dns.reverse(ip, (err, names) => resolve(err || !names || !names.length ? '' : names[0]));
  });
}
async function nbtName(ip) {
  const { stdout } = await execFileP('nbtstat', ['-A', ip]);
  // Grab the first UNIQUE registered name that isn't a service record.
  for (const line of stdout.split(/\r?\n/)) {
    const m = line.match(/^\s*([\w.\-]+)\s+<00>\s+UNIQUE/i);
    if (m) return m[1].trim();
  }
  return '';
}

// --- mDNS / LLMNR reverse lookup ---------------------------------------------
// Many "foreign" devices (IoT, printers, cameras, Macs, Linux boxes) don't have
// a DNS PTR record or NetBIOS name, but DO answer a reverse PTR query over mDNS
// (UDP 5353) or LLMNR (UDP 5355). We query the host directly (unicast) and parse
// the PTR answer. This is what lets us name unexpected devices on the network.
function reverseArpa(ip) {
  return ip.split('.').reverse().join('.') + '.in-addr.arpa';
}
function encodeName(name) {
  const parts = name.split('.');
  const bufs = [];
  for (const part of parts) {
    const b = Buffer.from(part, 'utf8');
    bufs.push(Buffer.from([b.length]), b);
  }
  bufs.push(Buffer.from([0]));
  return Buffer.concat(bufs);
}
function buildPtrQuery(name, qclass) {
  const header = Buffer.alloc(12);
  header.writeUInt16BE(0x0000, 0); // id
  header.writeUInt16BE(0x0000, 2); // flags: standard query
  header.writeUInt16BE(1, 4); // qdcount
  const q = Buffer.concat([encodeName(name), Buffer.alloc(4)]);
  q.writeUInt16BE(12, q.length - 4); // QTYPE = PTR
  q.writeUInt16BE(qclass, q.length - 2); // QCLASS
  return Buffer.concat([header, q]);
}
// Parse a DNS name at offset, following compression pointers. Returns string.
function parseName(buf, offset) {
  const labels = [];
  let pos = offset;
  let jumped = false;
  let guard = 0;
  while (guard++ < 128) {
    if (pos >= buf.length) break;
    const len = buf[pos];
    if (len === 0) { pos++; break; }
    if ((len & 0xc0) === 0xc0) {
      const ptr = ((len & 0x3f) << 8) | buf[pos + 1];
      if (!jumped) pos += 2;
      pos = ptr;
      jumped = true;
      continue;
    }
    labels.push(buf.toString('utf8', pos + 1, pos + 1 + len));
    pos += 1 + len;
  }
  return labels.join('.');
}
function parsePtrAnswer(buf) {
  if (buf.length < 12) return '';
  const ancount = buf.readUInt16BE(6);
  if (ancount < 1) return '';
  // Skip header + question section.
  let pos = 12;
  // question name
  let guard = 0;
  while (pos < buf.length && guard++ < 128) {
    const len = buf[pos];
    if (len === 0) { pos++; break; }
    if ((len & 0xc0) === 0xc0) { pos += 2; break; }
    pos += 1 + len;
  }
  pos += 4; // qtype + qclass
  // First answer record.
  // name (may be pointer)
  if ((buf[pos] & 0xc0) === 0xc0) pos += 2;
  else { while (pos < buf.length && buf[pos] !== 0) { if ((buf[pos] & 0xc0) === 0xc0) { pos++; break; } pos += 1 + buf[pos]; } pos++; }
  const type = buf.readUInt16BE(pos); pos += 2;
  pos += 2; // class
  pos += 4; // ttl
  const rdlen = buf.readUInt16BE(pos); pos += 2;
  if (type !== 12) return '';
  return parseName(buf, pos, rdlen);
}
function dnsPtr(ip, port, timeout, qclass) {
  return new Promise((resolve) => {
    let done = false;
    const sock = dgram.createSocket('udp4');
    const finish = (v) => {
      if (done) return;
      done = true;
      try { sock.close(); } catch (_) {}
      resolve(v);
    };
    const timer = setTimeout(() => finish(''), timeout);
    const query = buildPtrQuery(reverseArpa(ip), qclass);
    sock.once('error', () => { clearTimeout(timer); finish(''); });
    sock.once('message', (msg) => {
      clearTimeout(timer);
      let name = '';
      try { name = parsePtrAnswer(msg); } catch (_) {}
      finish(name);
    });
    sock.bind(() => {
      try { sock.send(query, 0, query.length, port, ip, (e) => { if (e) { clearTimeout(timer); finish(''); } }); }
      catch (_) { clearTimeout(timer); finish(''); }
    });
  });
}
function stripDot(s) { return String(s || '').replace(/\.$/, ''); }

// Resolve a hostname using every available method; first hit wins.
async function resolveHostname(ip, timeout) {
  const dnsName = await reverseDns(ip);
  if (dnsName) return { name: stripDot(dnsName), src: 'dns' };
  const t = Math.min(Math.max(timeout, 600), 1200);
  const [nb, md, ll] = await Promise.all([
    nbtName(ip).catch(() => ''),
    dnsPtr(ip, 5353, t, 0x8001).catch(() => ''), // mDNS (unicast-response bit)
    dnsPtr(ip, 5355, t, 0x0001).catch(() => ''), // LLMNR
  ]);
  if (nb) return { name: nb, src: 'netbios' };
  if (md) return { name: stripDot(md), src: 'mdns' };
  if (ll) return { name: stripDot(ll), src: 'llmnr' };
  return { name: '', src: '' };
}

// ---------------------------------------------------------------------------
// Service naming
// ---------------------------------------------------------------------------
const PORT_SERVICE = {
  21: 'FTP', 22: 'SSH', 23: 'Telnet', 25: 'SMTP', 53: 'DNS', 80: 'HTTP',
  88: 'Kerberos', 110: 'POP3', 123: 'NTP', 135: 'MSRPC', 139: 'NetBIOS',
  143: 'IMAP', 161: 'SNMP', 443: 'HTTPS', 445: 'SMB', 502: 'Modbus',
  515: 'LPD', 623: 'IPMI', 1025: 'MSRPC', 1433: 'MSSQL', 1521: 'Oracle',
  1723: 'PPTP', 1883: 'MQTT', 1911: 'Niagara-Fox', 2404: 'IEC-104',
  3306: 'MySQL', 3389: 'RDP', 4911: 'Niagara-Foxs', 5000: 'UPnP',
  5432: 'Postgres', 5900: 'VNC', 5985: 'WinRM', 6379: 'Redis',
  8000: 'HTTP-alt', 8080: 'HTTP-Proxy', 8443: 'HTTPS-alt', 8883: 'MQTTS',
  9100: 'JetDirect', 20000: 'DNP3', 44818: 'EtherNet/IP', 47808: 'BACnet',
};
function serviceName(port) {
  return PORT_SERVICE[port] || `TCP/${port}`;
}

// Comprehensive "all common services" TCP set: every well-known port (1-1024)
// plus a curated list of registered / OT / ICS / BMS / IT service ports above
// that. This is the default scan — the UI no longer asks for a port list. The
// scan endpoint additionally unions in every port named by the imported
// register, so RAG is always correct even for obscure expected ports.
function buildTopPorts() {
  const s = new Set();
  for (let p = 1; p <= 1024; p++) s.add(p);
  const extra = [
    1099, 1177, 1200, 1234, 1311, 1400, 1433, 1434, 1500, 1521, 1533, 1583,
    1600, 1604, 1645, 1701, 1723, 1755, 1801, 1812, 1883, 1900, 1911, 1962,
    2000, 2001, 2002, 2020, 2049, 2067, 2101, 2103, 2121, 2181, 2222, 2323,
    2332, 2375, 2376, 2404, 2455, 2480, 2500, 2601, 2604, 2628, 2701, 2809,
    3000, 3128, 3260, 3268, 3269, 3283, 3299, 3306, 3311, 3312, 3333, 3389,
    3460, 3480, 3500, 3541, 3542, 3689, 3690, 3702, 3749, 3784, 3790, 3872,
    3900, 4000, 4022, 4040, 4063, 4064, 4070, 4155, 4200, 4443, 4444, 4500,
    4567, 4711, 4800, 4840, 4843, 4911, 4993, 5000, 5001, 5006, 5007, 5009,
    5050, 5060, 5061, 5094, 5222, 5269, 5353, 5357, 5432, 5450, 5555, 5560,
    5580, 5601, 5631, 5632, 5672, 5683, 5684, 5800, 5900, 5901, 5902, 5938,
    5984, 5985, 5986, 6000, 6001, 6060, 6379, 6443, 6543, 6633, 6666, 6667,
    6881, 7000, 7001, 7070, 7080, 7100, 7443, 7474, 7547, 7657, 7676, 7777,
    7780, 8000, 8001, 8002, 8008, 8009, 8010, 8020, 8042, 8060, 8080, 8081,
    8082, 8083, 8085, 8086, 8087, 8088, 8089, 8090, 8091, 8098, 8099, 8112,
    8123, 8161, 8181, 8200, 8222, 8291, 8333, 8400, 8443, 8500, 8530, 8531,
    8545, 8600, 8649, 8686, 8800, 8834, 8880, 8883, 8888, 8899, 8983, 9000,
    9001, 9002, 9009, 9010, 9042, 9080, 9090, 9091, 9100, 9111, 9200, 9295,
    9300, 9333, 9418, 9443, 9500, 9530, 9595, 9600, 9999, 10000, 10001,
    10162, 10243, 10250, 11211, 12345, 17185, 18245, 20000, 20547, 25565,
    27017, 28784, 44818, 47001, 47808, 49152, 49153, 49154, 50000, 55443, 62078,
  ];
  for (const p of extra) s.add(p);
  return [...s].sort((a, b) => a - b);
}
const DEFAULT_TCP_PORTS = buildTopPorts();
// UDP protocol probes we know how to speak.
const UDP_PROBES = { 47808: 'BACnet', 161: 'SNMP' };

// ---------------------------------------------------------------------------
// Full scan
// ---------------------------------------------------------------------------
/*
 * options = {
 *   range: { start, end },        // required
 *   tcpPorts: [..],               // default DEFAULT_TCP_PORTS
 *   udpPorts: [47808, 161],       // subset of UDP_PROBES keys
 *   timeout: 1000,                // ms per probe
 *   hostConcurrency: 40,          // hosts scanned in parallel
 * }
 * emit(event) is called with { type, ... } as things happen (for SSE).
 */
async function scanNetwork(options, emit = () => {}) {
  const timeout = options.timeout || 1000;
  const tcpPorts = (options.tcpPorts && options.tcpPorts.length ? options.tcpPorts : DEFAULT_TCP_PORTS)
    .slice()
    .sort((a, b) => a - b);
  const udpPorts = (options.udpPorts || Object.keys(UDP_PROBES).map(Number)).filter(
    (p) => UDP_PROBES[p]
  );
  const hostConc = options.hostConcurrency || 40;

  const ips = expandRange(options.range.start, options.range.end);
  emit({ type: 'start', total: ips.length, tcpPorts, udpPorts, timeout });

  // Pass 1: ping sweep (also primes the ARP cache).
  let pingDone = 0;
  const pingResults = {};
  await pool(
    ips,
    Math.min(64, hostConc * 2),
    async (ip) => {
      const r = await pingHost(ip, timeout);
      pingResults[ip] = r;
      return r;
    },
    () => {
      pingDone++;
      if (pingDone % 16 === 0 || pingDone === ips.length) {
        emit({ type: 'progress', phase: 'discovery', done: pingDone, total: ips.length });
      }
    }
  );

  // Pass 2: ARP cache — reveals hosts that ignored ICMP but are on the wire.
  const arp = await arpTable();

  // Candidate = replied to ping OR present in ARP (i.e. real host on subnet).
  const candidates = ips.filter((ip) => pingResults[ip]?.alive || arp[ip]);
  emit({ type: 'discovered', count: candidates.length });

  // Pass 3: per-host port scan + enrichment, streamed as each host completes.
  const devices = [];
  let scanned = 0;
  await pool(
    candidates,
    hostConc,
    async (ip) => {
      const openTcp = [];
      await pool(tcpPorts, Math.min(tcpPorts.length, 48), async (port) => {
        if (await tcpConnect(ip, port, timeout)) openTcp.push(port);
      });
      openTcp.sort((a, b) => a - b);

      const openUdp = [];
      for (const port of udpPorts) {
        let ok = false;
        if (port === 47808) ok = await bacnetWhoIs(ip, Math.max(timeout, 1200));
        else if (port === 161) ok = await snmpProbe(ip, timeout);
        if (ok) openUdp.push(port);
      }

      // Enrichment — resolve hostname via DNS / NetBIOS / mDNS / LLMNR so even
      // unexpected "foreign" devices get named where at all possible.
      const mac = arp[ip] || '';
      const host = await resolveHostname(ip, timeout);
      const hostname = host.name;
      const hostnameSrc = host.src;

      // Deep-fingerprint each open TCP port for product / version / title / cert.
      const tcpServices = openTcp.map((p) => ({
        port: p, proto: 'tcp', name: serviceName(p),
        product: '', version: '', title: '', info: '', certCN: '', tls: false,
      }));
      await pool(tcpServices, Math.min(tcpServices.length, 8) || 1, async (svc) => {
        try { Object.assign(svc, await fingerprintPort(ip, svc.port, timeout)); } catch (_) {}
      });
      const services = [
        ...tcpServices,
        ...openUdp.map((p) => ({ port: p, proto: 'udp', name: serviceName(p), product: '', version: '', title: '', info: '', certCN: '', tls: false })),
      ];
      // Short summary banner: first identified product / title (kept for detail view).
      const idSvc = tcpServices.find((s) => s.product || s.title || s.certCN);
      const banner = idSvc
        ? [idSvc.product && idSvc.product + (idSvc.version ? ' ' + idSvc.version : ''), idSvc.title].filter(Boolean).join(' — ')
        : '';

      const device = {
        ip,
        hostname,
        hostnameSrc,
        mac,
        vendor: ouiVendor(mac),
        alive: true,
        latency: pingResults[ip]?.alive ? pingResults[ip].latency : null,
        openPorts: [...openTcp, ...openUdp],
        openTcp,
        openUdp,
        services,
        banner,
        discoveredBy: pingResults[ip]?.alive ? 'icmp' : 'arp',
      };
      devices.push(device);
      emit({ type: 'device', device });
      return device;
    },
    () => {
      scanned++;
      emit({ type: 'progress', phase: 'ports', done: scanned, total: candidates.length });
    }
  );

  devices.sort((a, b) => ipToInt(a.ip) - ipToInt(b.ip));
  emit({ type: 'complete', devices });
  return devices;
}

module.exports = {
  listAdapters,
  scanNetwork,
  expandRange,
  adapterRange,
  ipToInt,
  intToIp,
  maskToPrefix,
  prefixToMask,
  serviceName,
  DEFAULT_TCP_PORTS,
  UDP_PROBES,
};
