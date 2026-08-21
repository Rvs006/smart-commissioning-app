'use strict';
/* Frontend logic: adapter discovery, live SSE scan, RAG rendering, register import. */

const $ = (id) => document.getElementById(id);
const state = {
  adapters: [],
  rows: [],
  summary: null,
  registerCount: 0,
  filter: 'all',
  search: '',
  selectedIp: null,
  scanning: false,
  es: null,
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
window.addEventListener('DOMContentLoaded', async () => {
  bindEvents();
  await loadAdapters();
  await loadRegister();
});

function bindEvents() {
  $('adapterSelect').addEventListener('change', onAdapterChange);
  $('scanBtn').addEventListener('click', toggleScan);
  $('importBtn').addEventListener('click', () => $('csvFile').click());
  $('clearBtn').addEventListener('click', clearRegister);
  $('csvFile').addEventListener('change', onCsvChosen);
  $('exportBtn').addEventListener('click', exportCsv);
  $('saveRegBtn').addEventListener('click', saveAsRegister);
  $('searchInput').addEventListener('input', (e) => { state.search = e.target.value.toLowerCase(); render(); });
  document.querySelectorAll('.chip').forEach((c) =>
    c.addEventListener('click', () => {
      document.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
      c.classList.add('active');
      state.filter = c.dataset.filter;
      render();
    })
  );
}

// ---------------------------------------------------------------------------
// Adapters
// ---------------------------------------------------------------------------
async function loadAdapters() {
  try {
    const res = await fetch('api/adapters');
    const data = await res.json();
    state.adapters = data.adapters || [];
    const sel = $('adapterSelect');
    sel.innerHTML = '';
    if (!state.adapters.length) {
      sel.innerHTML = '<option>No adapters found</option>';
      return;
    }
    state.adapters.forEach((a, i) => {
      const opt = document.createElement('option');
      opt.value = String(i);
      const status = a.status && a.status.toLowerCase() === 'up' ? '● ' : '○ ';
      opt.textContent = `${status}${a.description} — ${a.cidr}`;
      sel.appendChild(opt);
    });
    onAdapterChange();
  } catch (e) {
    toast('Could not load adapters: ' + e.message);
  }
}

function currentAdapter() {
  const i = Number($('adapterSelect').value);
  return state.adapters[i];
}

function onAdapterChange() {
  const a = currentAdapter();
  if (!a) return;
  $('rangeStart').value = a.suggestedRange.start;
  $('rangeEnd').value = a.suggestedRange.end;
  $('adapterMeta').textContent = `${a.ip} · ${a.status}${a.linkSpeed ? ' · ' + a.linkSpeed : ''} · ${a.mac || ''}`;
  $('rangeHint').textContent = `Subnet ${a.cidr} · ~${a.hostCount} hosts`;
  $('footAdapter').textContent = `Adapter: ${a.description} (${a.ip})`;
}

// ---------------------------------------------------------------------------
// Register import / template
// ---------------------------------------------------------------------------
async function loadRegister() {
  try {
    const res = await fetch('api/register');
    const data = await res.json();
    setRegisterCount((data.register || []).length);
  } catch (_) {}
}

function setRegisterCount(n) {
  state.registerCount = n;
  const el = $('registerStatus');
  if (n > 0) {
    el.textContent = `${n} devices loaded`;
    el.className = 'register-status loaded';
    $('clearBtn').classList.remove('hidden');
  } else {
    el.textContent = 'No register loaded';
    el.className = 'register-status none';
    $('clearBtn').classList.add('hidden');
  }
  $('sumExpected').textContent = n;
}

async function clearRegister() {
  try {
    await fetch('api/register', { method: 'DELETE' });
    setRegisterCount(0);
    toast('Register cleared — scans will run without RAG comparison');
    if (state.rows.length) recompare();
  } catch (e) {
    toast('Clear failed: ' + e.message);
  }
}

function onCsvChosen(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const res = await fetch('api/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csv: reader.result }),
      });
      const data = await res.json();
      setRegisterCount(data.imported || 0);
      toast(`Imported ${data.imported} devices from register`);
      if (state.rows.length) recompare();
    } catch (err) {
      toast('Import failed: ' + err.message);
    }
  };
  reader.readAsText(file);
  e.target.value = '';
}

// ---------------------------------------------------------------------------
// Scan (SSE)
// ---------------------------------------------------------------------------
function toggleScan() {
  if (state.scanning) return stopScan();
  startScan();
}

function startScan() {
  const start = $('rangeStart').value.trim();
  const end = $('rangeEnd').value.trim() || start;
  if (!start) return toast('Enter a target IP range');

  const params = new URLSearchParams({
    start, end,
    timeout: $('timeoutInput').value || '1000',
  });

  state.scanning = true;
  state.rows = [];
  state.summary = null;
  state.selectedIp = null;
  $('scanBtn').textContent = '■ Stop';
  $('scanBtn').classList.remove('primary'); $('scanBtn').classList.add('ghost');
  setScanState('running', 'Scanning…');
  $('progressWrap').classList.remove('hidden');
  $('resultsBody').innerHTML = '<tr class="empty-row"><td colspan="9">Discovering hosts…</td></tr>';
  clearDetail();

  const liveDevices = [];
  const es = new EventSource('api/scan?' + params.toString());
  state.es = es;

  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    switch (ev.type) {
      case 'start':
        setProgress(0, `Pinging ${ev.total} addresses…`);
        break;
      case 'progress':
        if (ev.phase === 'discovery') {
          setProgress((ev.done / ev.total) * 40, `Host discovery: ${ev.done}/${ev.total}`);
        } else {
          setProgress(40 + (ev.done / ev.total) * 60, `Port scan: ${ev.done}/${ev.total} live hosts`);
        }
        break;
      case 'discovered':
        setProgress(40, `${ev.count} live hosts found — scanning ports…`);
        break;
      case 'device':
        liveDevices.push(ev.device);
        // Live provisional render (no register comparison yet).
        renderProvisional(liveDevices);
        break;
      case 'result':
        state.rows = ev.rows;
        state.summary = ev.summary;
        render();
        break;
      case 'complete':
        finishScan(liveDevices);
        break;
      case 'error':
        toast('Scan error: ' + ev.message);
        setScanState('error', 'Error');
        finishScan(liveDevices);
        break;
    }
  };
  es.onerror = () => {
    if (state.scanning) { /* stream ended */ finishScan(liveDevices); }
  };
}

function stopScan() {
  if (state.es) { state.es.close(); state.es = null; }
  finishScan(null);
  toast('Scan stopped');
}

function finishScan(liveDevices) {
  if (state.es) { state.es.close(); state.es = null; }
  state.scanning = false;
  state._lastDevices = liveDevices || state._lastDevices;
  $('scanBtn').textContent = '▶ Start Scan';
  $('scanBtn').classList.add('primary'); $('scanBtn').classList.remove('ghost');
  setProgress(100, 'Scan complete');
  setTimeout(() => $('progressWrap').classList.add('hidden'), 1200);
  if ($('scanState').className.indexOf('error') === -1) setScanState('done', 'Complete');
  render();
}

async function recompare() {
  if (!state._lastDevices || !state._lastDevices.length) return;
  const res = await fetch('api/compare', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ devices: state._lastDevices }),
  });
  const data = await res.json();
  state.rows = data.rows;
  state.summary = data.summary;
  render();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderProvisional(devices) {
  state._lastDevices = devices;
  const body = $('resultsBody');
  body.innerHTML = '';
  devices.forEach((d) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="ip-cell">${d.ip}</td>
      <td class="host-cell">${d.hostname || '<span class="muted">—</span>'}</td>
      <td class="mac-cell">${d.mac || '<span class="muted">—</span>'}</td>
      <td><span class="badge green">Reachable</span></td>
      <td>${fmtLatency(d.latency)}</td>
      <td class="ports-cell">${d.openPorts.join(', ') || '—'}</td>
      <td>${servicesShort(d.services)}</td>
      <td><span class="badge grey">Scanning…</span></td>
      <td class="muted">—</td>`;
    body.appendChild(tr);
  });
  $('resultCount').textContent = `(${devices.length})`;
  $('sumReachable').textContent = devices.length;
}

function render() {
  const rows = filteredRows();
  const body = $('resultsBody');
  body.innerHTML = '';
  if (!rows.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="9">${state.rows.length ? 'No rows match the filter.' : 'No scan yet. Select an adapter and press <strong>Start Scan</strong>.'}</td></tr>`;
  } else {
    rows.forEach((r) => body.appendChild(renderRow(r)));
  }
  $('resultCount').textContent = `(${rows.length})`;
  updateSummary();
  if (state.selectedIp) {
    const sel = state.rows.find((r) => r.ip === state.selectedIp);
    if (sel) showDetail(sel);
  }
}

function renderRow(r) {
  const tr = document.createElement('tr');
  if (r.rag === 'red') tr.classList.add('rag-red-row');
  if (r.ip === state.selectedIp) tr.classList.add('selected');
  tr.innerHTML = `
    <td class="ip-cell">${r.ip}</td>
    <td class="host-cell">${hostCell(r)}</td>
    <td class="mac-cell">${r.mac ? r.mac : '<span class="muted">—</span>'}</td>
    <td>${statusBadge(r)}</td>
    <td>${fmtLatency(r.latency)}</td>
    <td class="ports-cell">${(r.openPorts || []).join(', ') || '—'}</td>
    <td>${servicesShort(r.services)}</td>
    <td>${registerBadge(r)}</td>
    <td class="muted">${r.project || '—'}</td>`;
  tr.addEventListener('click', () => { state.selectedIp = r.ip; render(); });
  return tr;
}

function hostCell(r) {
  const shown = r.hostname || '<span class="muted">—</span>';
  if (r.hostnameStatus === 'mismatch') {
    const found = r.discoveredHostname || '—';
    return `${found}<span class="host-mismatch" title="Register expects '${escapeAttr(r.expectedHostname)}', found '${escapeAttr(r.discoveredHostname || '—')}'">⚠ name</span>`;
  }
  if (r.hostnameStatus === 'match') {
    return `${shown}<span class="host-ok" title="Hostname matches register">✓</span>`;
  }
  return shown;
}
function escapeAttr(s) { return escapeHtml(s).replace(/'/g, '&#39;'); }

function statusBadge(r) {
  if (r.status === 'reachable') return '<span class="badge green">Reachable</span>';
  if (r.status === 'rogue') return '<span class="badge red">Rogue Host</span>';
  return '<span class="badge red">Unreachable</span>';
}
function registerBadge(r) {
  switch (r.register) {
    case 'match': return '<span class="badge green">Match</span>';
    case 'partial': return '<span class="badge amber">Partial</span>';
    case 'missing': return '<span class="badge grey">No Data</span>';
    case 'rogue': return '<span class="badge red">Not in Register</span>';
    default: return '<span class="badge blue">Discovered</span>';
  }
}
function svcDescr(s) {
  const parts = [];
  if (s.product) parts.push(escapeHtml(s.product + (s.version ? ' ' + s.version : '')));
  if (s.title) parts.push('“' + escapeHtml(s.title) + '”');
  if (s.certCN) parts.push('cert: ' + escapeHtml(s.certCN));
  if (!parts.length && s.info) parts.push(escapeHtml(s.info));
  return parts.join(' · ');
}

function servicesShort(services) {
  if (!services || !services.length) return '<span class="muted">—</span>';
  const names = [...new Set(services.map((s) => s.name))];
  return names.slice(0, 3).join(', ') + (names.length > 3 ? ` +${names.length - 3}` : '');
}
function fmtLatency(l) {
  if (l === null || l === undefined) return '<span class="muted">—</span>';
  return `${l} ms`;
}

function filteredRows() {
  return state.rows.filter((r) => {
    if (state.filter !== 'all' && r.rag !== state.filter) return false;
    if (state.search) {
      const svcText = (r.services || []).map((s) => `${s.name} ${s.product || ''} ${s.title || ''} ${s.certCN || ''}`).join(' ');
      const hay = `${r.ip} ${r.hostname} ${r.mac || ''} ${svcText} ${r.vendor || ''} ${r.project || ''}`.toLowerCase();
      if (!hay.includes(state.search)) return false;
    }
    return true;
  });
}

function updateSummary() {
  const s = state.summary;
  if (!s) return;
  $('sumExpected').textContent = s.expected;
  if (s.expected) {
    // With a register loaded, show how many EXPECTED devices are reachable
    // (rogues are excluded here — they have their own card).
    $('sumReachable').textContent = s.expectedReachable;
    $('sumReachableSub').textContent = `${pct(s.expectedReachable, s.expected)}% of expected · ${s.reachable} live total`;
  } else {
    $('sumReachable').textContent = s.reachable;
    $('sumReachableSub').textContent = 'Discovered live';
  }
  $('sumMatches').textContent = s.matches;
  $('sumMatchesSub').textContent = s.expected ? `${pct(s.matches, s.expected)}% full match` : 'Full RAG match';
  $('sumRogue').textContent = s.rogue;
  $('sumRogueSub').textContent = s.reachable ? `${s.rogue} of ${s.reachable} live not in register` : 'Not in register';
}
function pct(a, b) { return b ? Math.round((a / b) * 100) : 0; }

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------
function showDetail(r) {
  const panel = $('detailPanel');
  panel.classList.remove('empty');
  const ragBadge = { green: 'green', amber: 'amber', red: 'red', none: 'blue' }[r.rag] || 'grey';
  const services = (r.services || [])
    .map((s) => {
      const d = svcDescr(s);
      return `<div class="svc-row">
        <div class="svc-portname"><span class="svc-proto ${s.proto}">${s.proto}/${s.port}</span> ${escapeHtml(s.name)}${s.tls ? ' 🔒' : ''}</div>
        ${d ? `<div class="svc-descr">${d}</div>` : ''}
      </div>`;
    })
    .join('') || '<span class="muted">None observed</span>';

  let portDiff = '';
  if (r.expected) {
    const miss = (r.missingPorts || []).length ? `<div class="miss">Missing expected: ${r.missingPorts.join(', ')}</div>` : '';
    const extra = (r.extraPorts || []).length ? `<div class="extra">Unexpected open: ${r.extraPorts.join(', ')}</div>` : '';
    const ok = !miss && !extra && r.status === 'reachable' ? '<div style="color:var(--green)">All expected ports present, no extras.</div>' : '';
    portDiff = `<div class="port-diff">${miss}${extra}${ok}</div>`;
  }

  // Hostname verification block
  let hostCheck = '';
  const hs = r.hostnameStatus;
  if (hs && hs !== 'na') {
    const label = {
      match: '<span style="color:var(--green)">✓ Matches register</span>',
      mismatch: '<span style="color:var(--amber)">⚠ Does not match register</span>',
      unverified: '<span style="color:var(--muted)">Could not resolve on network</span>',
    }[hs] || hs;
    hostCheck = `
      <div class="detail-section">
        <h4>Hostname Check</h4>
        <div class="kv">
          <div class="k">Expected</div><div class="v">${escapeHtml(r.expectedHostname || '—')}</div>
          <div class="k">Discovered</div><div class="v">${escapeHtml(r.discoveredHostname || '—')}${r.hostnameSrc ? ' (' + r.hostnameSrc + ')' : ''}</div>
          <div class="k">Result</div><div class="v">${label}</div>
        </div>
      </div>`;
  } else if (r.discoveredHostname) {
    hostCheck = `
      <div class="detail-section">
        <h4>Hostname</h4>
        <div class="kv">
          <div class="k">Discovered</div><div class="v">${escapeHtml(r.discoveredHostname)}${r.hostnameSrc ? ' (' + r.hostnameSrc + ')' : ''}</div>
        </div>
      </div>`;
  }

  panel.innerHTML = `
    <div class="detail-head">
      <div class="detail-host">${r.hostname || r.ip}</div>
      <button class="detail-close" onclick="clearDetail()">✕</button>
    </div>
    <div class="detail-badges">
      ${statusBadge(r)} ${registerBadge(r)}
    </div>
    <div class="detail-section">
      <h4>Device Overview</h4>
      <div class="kv">
        <div class="k">IP Address</div><div class="v">${r.ip}</div>
        <div class="k">Hostname</div><div class="v">${r.hostname || '—'}</div>
        <div class="k">Type</div><div class="v">${r.type || '—'}</div>
        <div class="k">Vendor</div><div class="v">${r.vendor || '—'}</div>
        <div class="k">Model</div><div class="v">${r.model || '—'}</div>
        <div class="k">MAC</div><div class="v">${r.mac || '—'}</div>
        ${r.banner ? `<div class="k">Banner</div><div class="v">${escapeHtml(r.banner)}</div>` : ''}
      </div>
    </div>
    <div class="detail-section">
      <h4>Live Health</h4>
      <div class="kv">
        <div class="k">Status</div><div class="v">${r.status}</div>
        <div class="k">Latency</div><div class="v">${r.latency != null ? r.latency + ' ms' : '—'}</div>
        <div class="k">Found via</div><div class="v">${(r.discoveredBy || '—').toUpperCase()}</div>
      </div>
    </div>
    ${hostCheck}
    <div class="detail-section">
      <h4>Network / Register</h4>
      <div class="kv">
        <div class="k">Project</div><div class="v">${r.project || '—'}</div>
        <div class="k">Location</div><div class="v">${r.location || '—'}</div>
        <div class="k">Expected</div><div class="v">${(r.expectedPorts || []).join(', ') || '—'}</div>
      </div>
      ${portDiff}
      ${r.description ? `<div style="margin-top:8px;color:var(--muted);font-size:12px">${escapeHtml(r.description)}</div>` : ''}
    </div>
    <div class="detail-section">
      <h4>Services &amp; Software</h4>
      <div class="svc-list">${services}</div>
    </div>`;
}

function clearDetail() {
  state.selectedIp = null;
  const panel = $('detailPanel');
  panel.classList.add('empty');
  panel.innerHTML = '<div class="detail-empty">Select a device to view details</div>';
  document.querySelectorAll('.results-table tbody tr').forEach((t) => t.classList.remove('selected'));
}
window.clearDetail = clearDetail;

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------
function csvCell(c) {
  return `"${String(c == null ? '' : c).replace(/"/g, '""')}"`;
}
function downloadCsv(text, filename) {
  const blob = new Blob([text], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportCsv() {
  if (!state.rows.length) return toast('Nothing to export yet');
  const head = ['IP', 'Hostname', 'Status', 'Register', 'RAG', 'Latency', 'Open Ports', 'Services', 'Vendor', 'MAC', 'Expected Ports', 'Missing', 'Extra', 'Project', 'Location'];
  const lines = [head.join(',')];
  for (const r of state.rows) {
    const cells = [
      r.ip, r.hostname || '', r.status, r.register, r.rag, r.latency ?? '',
      (r.openPorts || []).join(' '), (r.services || []).map((s) => s.name).join(' '),
      r.vendor || '', r.mac || '', (r.expectedPorts || []).join(' '),
      (r.missingPorts || []).join(' '), (r.extraPorts || []).join(' '),
      r.project || '', r.location || '',
    ].map(csvCell);
    lines.push(cells.join(','));
  }
  downloadCsv(lines.join('\n'), 'scan-results.csv');
}

// Build an IP Device Register from the CURRENT scan: each discovered device's
// real open ports become its Expected Ports and its resolved name becomes its
// Hostname. Re-importing this and re-scanning yields genuine green matches
// (exact ports + hostname), not presence-only greens.
function saveAsRegister() {
  const discovered = state.rows.filter((r) => r.status === 'reachable' || r.status === 'rogue');
  if (!discovered.length) return toast('Run a scan first, then save it as a register');
  const head = ['IP Address', 'Hostname', 'Type', 'Vendor', 'Model', 'Expected Ports', 'Project', 'Location', 'Description'];
  const lines = [head.join(',')];
  for (const r of discovered) {
    const cells = [
      r.ip,
      r.discoveredHostname || '',
      r.type || '',
      r.vendor || '',
      r.model || '',
      (r.openPorts || []).join(';'),
      r.project || '',
      r.location || '',
      r.description || '',
    ].map(csvCell);
    lines.push(cells.join(','));
  }
  downloadCsv(lines.join('\n'), 'scan-register.csv');
  const named = discovered.filter((r) => r.discoveredHostname).length;
  toast(`Saved ${discovered.length} devices as a register (${named} with hostnames). Import it to validate future scans.`);
}

// ---------------------------------------------------------------------------
// Misc UI helpers
// ---------------------------------------------------------------------------
function setScanState(cls, text) {
  const el = $('scanState');
  el.className = 'scan-state ' + cls;
  el.textContent = text;
}
function setProgress(pctVal, text) {
  $('progressFill').style.width = Math.min(100, pctVal) + '%';
  $('progressText').textContent = text;
}
let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 3200);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
