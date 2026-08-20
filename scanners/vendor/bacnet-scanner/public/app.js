'use strict';
/* Frontend logic: adapter discovery, live SSE Who-Is scan, identity enrichment,
   RAG rendering, object-list browser, register import/export. */

const $ = (id) => document.getElementById(id);
const state = {
  adapters: [],
  rows: [],
  devices: [],       // raw discovered devices (for recompare / save-as-register)
  summary: null,
  networks: [],
  registerCount: 0,
  filter: 'all',
  search: '',
  selectedInstance: null,
  scanning: false,
  es: null,
  objEs: null,
  objects: {},       // instance -> { objects, count, truncated, loading }
  exportMode: false,
  selected: new Set(),
  exporting: false,
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
  $('exportAssetsBtn').addEventListener('click', toggleExportMode);
  $('selectAll').addEventListener('change', onSelectAll);
  $('exportRunBtn').addEventListener('click', runExport);
  $('exportCancelBtn').addEventListener('click', exitExportMode);
  $('searchInput').addEventListener('input', (e) => { state.search = e.target.value.toLowerCase(); render(); });
  document.querySelectorAll('.chip').forEach((c) =>
    c.addEventListener('click', () => {
      document.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
      c.classList.add('active');
      state.filter = c.dataset.filter;
      render();
    })
  );
  initSplitter();
  $('detailBackdrop').addEventListener('click', collapseDetail);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') collapseDetail(); });
}

// ---------------------------------------------------------------------------
// Resizable split + pop-out detail
// ---------------------------------------------------------------------------
function initSplitter() {
  const divider = $('splitDivider');
  const content = document.querySelector('.content');
  const detail = $('detailPanel');
  const saved = parseInt(localStorage.getItem('bacnetDetailWidth') || '', 10);
  if (Number.isFinite(saved)) detail.style.flexBasis = saved + 'px';
  let dragging = false;
  divider.addEventListener('mousedown', (e) => {
    dragging = true;
    divider.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = content.getBoundingClientRect();
    let w = rect.right - e.clientX - 7; // width from cursor to the right edge
    w = Math.max(300, Math.min(w, rect.width - 340)); // keep the results usable
    detail.style.flexBasis = w + 'px';
    detail.style.width = w + 'px';
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    localStorage.setItem('bacnetDetailWidth', parseInt(detail.style.flexBasis, 10) || 380);
  });
}

function toggleExpand() {
  const panel = $('detailPanel');
  const expanded = panel.classList.toggle('expanded');
  $('detailBackdrop').classList.toggle('hidden', !expanded);
}
window.toggleExpand = toggleExpand;

function collapseDetail() {
  $('detailPanel').classList.remove('expanded');
  $('detailBackdrop').classList.add('hidden');
}
window.collapseDetail = collapseDetail;

// ---------------------------------------------------------------------------
// Adapters
// ---------------------------------------------------------------------------
async function loadAdapters() {
  try {
    const res = await fetch('/api/adapters');
    const data = await res.json();
    state.adapters = data.adapters || [];
    const sel = $('adapterSelect');
    sel.innerHTML = '';
    if (!state.adapters.length) { sel.innerHTML = '<option>No adapters found</option>'; return; }
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

function currentAdapter() { return state.adapters[Number($('adapterSelect').value)]; }

function onAdapterChange() {
  const a = currentAdapter();
  if (!a) return;
  $('adapterMeta').textContent = `${a.ip} · bcast ${a.broadcast} · ${a.status}${a.linkSpeed ? ' · ' + a.linkSpeed : ''}`;
  $('footAdapter').textContent = `Adapter: ${a.description} (${a.ip})`;
}

// ---------------------------------------------------------------------------
// Register import / template
// ---------------------------------------------------------------------------
async function loadRegister() {
  try {
    const res = await fetch('/api/register');
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
    await fetch('/api/register', { method: 'DELETE' });
    setRegisterCount(0);
    toast('Register cleared — scans will run without RAG comparison');
    if (state.devices.length) recompare();
  } catch (e) { toast('Clear failed: ' + e.message); }
}

function onCsvChosen(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const res = await fetch('/api/register', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csv: reader.result }),
      });
      const data = await res.json();
      setRegisterCount(data.imported || 0);
      toast(`Imported ${data.imported} devices from register`);
      if (state.devices.length) recompare();
    } catch (err) { toast('Import failed: ' + err.message); }
  };
  reader.readAsText(file);
  e.target.value = '';
}

// ---------------------------------------------------------------------------
// Scan (SSE)
// ---------------------------------------------------------------------------
function toggleScan() { if (state.scanning) return stopScan(); startScan(); }

function startScan() {
  const a = currentAdapter();
  if (!a) return toast('No adapter selected');
  if (state.exportMode) exitExportMode();

  const params = new URLSearchParams({
    adapter: $('adapterSelect').value,
    discoverMs: $('discoverMs').value || '4000',
  });
  const low = $('rangeLow').value.trim();
  const high = $('rangeHigh').value.trim();
  if (low !== '') params.set('low', low);
  if (high !== '') params.set('high', high);

  state.scanning = true;
  state.rows = [];
  state.devices = [];
  state.summary = null;
  state.networks = [];
  state.selectedInstance = null;
  state.objects = {};
  $('scanBtn').textContent = '■ Stop';
  $('scanBtn').classList.remove('primary'); $('scanBtn').classList.add('ghost');
  setScanState('running', 'Scanning…');
  $('progressWrap').classList.remove('hidden');
  $('networksBar').classList.add('hidden');
  $('resultsBody').innerHTML = '<tr class="empty-row"><td colspan="10">Broadcasting Who-Is…</td></tr>';
  clearDetail();

  const es = new EventSource('/api/scan?' + params.toString());
  state.es = es;

  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    switch (ev.type) {
      case 'start':
        setProgress(5, `Who-Is broadcast on ${ev.broadcast} — listening ${ev.discoverMs} ms…`);
        break;
      case 'device':
        upsertDevice(ev.device);
        renderProvisional();
        break;
      case 'router':
        for (const n of ev.networks) if (!state.networks.includes(n)) state.networks.push(n);
        renderNetworks();
        break;
      case 'discovered':
        state.networks = ev.networks && ev.networks.length ? ev.networks : state.networks;
        renderNetworks();
        setProgress(40, `${ev.count} device(s) found — reading identity…`);
        break;
      case 'progress':
        setProgress(40 + (ev.done / Math.max(1, ev.total)) * 55, `Reading identity: ${ev.done}/${ev.total}`);
        break;
      case 'device-update':
        upsertDevice(ev.device);
        renderProvisional();
        break;
      case 'result':
        state.rows = ev.rows;
        state.summary = ev.summary;
        render();
        break;
      case 'complete':
        finishScan();
        break;
      case 'error':
        toast('Scan error: ' + ev.message);
        setScanState('error', 'Error');
        finishScan();
        break;
    }
  };
  es.onerror = () => { if (state.scanning) finishScan(); };
}

function upsertDevice(dev) {
  const i = state.devices.findIndex((d) => d.instance === dev.instance);
  if (i >= 0) state.devices[i] = { ...state.devices[i], ...dev };
  else state.devices.push(dev);
}

function stopScan() {
  if (state.es) { state.es.close(); state.es = null; }
  finishScan();
  toast('Scan stopped');
}

function finishScan() {
  if (state.es) { state.es.close(); state.es = null; }
  state.scanning = false;
  $('scanBtn').textContent = '▶ Start Scan';
  $('scanBtn').classList.add('primary'); $('scanBtn').classList.remove('ghost');
  setProgress(100, 'Scan complete');
  setTimeout(() => $('progressWrap').classList.add('hidden'), 1200);
  if ($('scanState').className.indexOf('error') === -1) setScanState('done', 'Complete');
  render();
}

async function recompare() {
  if (!state.devices.length) return;
  const res = await fetch('/api/compare', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ devices: state.devices }),
  });
  const data = await res.json();
  state.rows = data.rows;
  state.summary = data.summary;
  render();
}

// ---------------------------------------------------------------------------
// Networks bar
// ---------------------------------------------------------------------------
function renderNetworks() {
  const bar = $('networksBar');
  if (!state.networks.length) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const chips = state.networks.map((n) => `<span class="net-chip">Network ${n}</span>`).join('');
  bar.innerHTML = `<span class="net-label">Remote BACnet networks discovered via routers:</span> ${chips}`;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderProvisional() {
  // While scanning, show raw discovered devices (before RAG result arrives).
  const devices = [...state.devices].sort((a, b) => a.instance - b.instance);
  const body = $('resultsBody');
  body.innerHTML = '';
  devices.forEach((d) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      ${selCell(d.instance)}
      <td class="ip-cell">${d.instance}</td>
      <td class="host-cell">${escapeHtml(d.name || '') || '<span class="muted">…</span>'}</td>
      <td class="mac-cell">${d.ip}${d.port && d.port !== 47808 ? ':' + d.port : ''}</td>
      <td>${d.network || 0}</td>
      <td>${escapeHtml(d.vendor || '') || '<span class="muted">—</span>'}</td>
      <td>${escapeHtml(d.model || '') || '<span class="muted">—</span>'}</td>
      <td>${escapeHtml(d.firmware || '') || '<span class="muted">—</span>'}</td>
      <td>${d.objectCount != null ? d.objectCount : '<span class="muted">—</span>'}</td>
      <td><span class="badge grey">Scanning…</span></td>`;
    body.appendChild(tr);
  });
  $('resultCount').textContent = `(${devices.length})`;
  $('sumDiscovered').textContent = devices.length;
}

function render() {
  const rows = filteredRows();
  const body = $('resultsBody');
  body.innerHTML = '';
  if (!rows.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="10">${state.rows.length ? 'No rows match the filter.' : 'No scan yet. Select an adapter and press <strong>Start Scan</strong>.'}</td></tr>`;
  } else {
    rows.forEach((r) => body.appendChild(renderRow(r)));
  }
  $('resultCount').textContent = `(${rows.length})`;
  updateSummary();
  if (state.exportMode) syncSelectAll();
  if (state.selectedInstance != null) {
    const sel = state.rows.find((r) => r.instance === state.selectedInstance);
    if (sel) showDetail(sel);
  }
}

function renderRow(r) {
  const tr = document.createElement('tr');
  if (r.rag === 'red') tr.classList.add('rag-red-row');
  if (r.instance === state.selectedInstance) tr.classList.add('selected');
  tr.innerHTML = `
    ${selCell(r.instance)}
    <td class="ip-cell">${r.instance}</td>
    <td class="host-cell">${nameCell(r)}</td>
    <td class="mac-cell">${r.ip}</td>
    <td>${r.network || 0}</td>
    <td>${escapeHtml(r.vendor || '') || '<span class="muted">—</span>'}</td>
    <td>${escapeHtml(r.model || '') || '<span class="muted">—</span>'}</td>
    <td>${escapeHtml(r.firmware || '') || '<span class="muted">—</span>'}</td>
    <td>${objectsCell(r)}</td>
    <td>${registerBadge(r)}</td>`;
  tr.addEventListener('click', () => { state.selectedInstance = r.instance; render(); });
  const cb = tr.querySelector('.row-check');
  if (cb) {
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', () => toggleSelect(r.instance, cb.checked));
  }
  return tr;
}

// ---------------------------------------------------------------------------
// Asset export — selection mode + ZIP of per-asset JSON/XLSX
// ---------------------------------------------------------------------------
function selCell(instance) {
  const checked = state.selected.has(instance) ? 'checked' : '';
  return `<td class="sel-col"><input type="checkbox" class="row-check" data-i="${instance}" ${checked}></td>`;
}

function toggleExportMode() { state.exportMode ? exitExportMode() : enterExportMode(); }

function enterExportMode() {
  if (!state.rows.length) return toast('Run a scan first, then export assets');
  state.exportMode = true;
  state.selected = new Set();
  const table = document.querySelector('.results-table');
  if (table) table.classList.add('select-mode');
  $('exportBar').classList.remove('hidden');
  $('exportAssetsBtn').classList.add('active-toggle');
  syncSelectAll();
  updateExportBar();
  render();
}

function exitExportMode() {
  state.exportMode = false;
  const table = document.querySelector('.results-table');
  if (table) table.classList.remove('select-mode');
  $('exportBar').classList.add('hidden');
  $('exportAssetsBtn').classList.remove('active-toggle');
  const sa = $('selectAll'); sa.checked = false; sa.indeterminate = false;
  render();
}

function toggleSelect(instance, on) {
  if (on) state.selected.add(instance); else state.selected.delete(instance);
  updateExportBar();
  syncSelectAll();
}

function onSelectAll(e) {
  const on = e.target.checked;
  const rows = filteredRows();
  state.selected = new Set(on ? rows.map((r) => r.instance) : []);
  updateExportBar();
  render();
}

function syncSelectAll() {
  const rows = filteredRows();
  const sel = rows.filter((r) => state.selected.has(r.instance)).length;
  const cb = $('selectAll');
  cb.checked = rows.length > 0 && sel === rows.length;
  cb.indeterminate = sel > 0 && sel < rows.length;
}

function updateExportBar() {
  $('exportCount').textContent = state.selected.size;
  $('exportRunBtn').disabled = state.selected.size === 0 || state.exporting;
}

async function runExport() {
  const devices = [];
  for (const inst of state.selected) {
    const d = state.devices.find((x) => x.instance === inst);
    if (d) devices.push({ instance: d.instance, ip: d.ip, port: d.port, network: d.network, mac: d.mac, name: d.name, vendor: d.vendor, model: d.model, firmware: d.firmware });
  }
  if (!devices.length) return toast('Nothing selected');

  state.exporting = true;
  updateExportBar();
  $('exportCancelBtn').disabled = true;
  $('progressWrap').classList.remove('hidden');
  setProgress(2, `Exporting ${devices.length} asset(s)…`);
  setScanState('running', 'Exporting…');

  let failures = 0;
  try {
    await postStream('/api/export', { devices }, (ev) => {
      switch (ev.type) {
        case 'device':
          setProgress(5 + (ev.index / ev.total) * 85, `Reading ${ev.name || 'device ' + ev.instance} (${ev.index + 1}/${ev.total})…`);
          break;
        case 'objprogress':
          $('progressText').textContent = `Reading objects ${ev.done}/${ev.total}…`;
          break;
        case 'device-done':
          if (ev.error) failures++;
          break;
        case 'packaging':
          setProgress(95, 'Packaging ZIP…');
          break;
        case 'ready':
          setProgress(100, `Export ready: ${ev.assets} assets, ${ev.points} points`);
          downloadBase64Zip(ev.base64, ev.filename);
          toast(`Exported ${ev.assets} asset(s), ${ev.points} points → ${ev.filename}${failures ? ` (${failures} device(s) had read errors)` : ''}`);
          break;
        case 'error':
          toast('Export error: ' + ev.message);
          break;
      }
    });
  } catch (e) {
    toast('Export failed: ' + e.message);
  }

  state.exporting = false;
  $('exportCancelBtn').disabled = false;
  setScanState('done', 'Complete');
  setTimeout(() => $('progressWrap').classList.add('hidden'), 1500);
  updateExportBar();
}

// Read an SSE-style streamed response from a POST (EventSource is GET-only).
async function postStream(url, body, onEvent) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, idx).replace(/^data: /, '');
      buf = buf.slice(idx + 2);
      if (line) { try { onEvent(JSON.parse(line)); } catch (_) {} }
    }
  }
}

function downloadBase64Zip(b64, filename) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const blob = new Blob([bytes], { type: 'application/zip' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

function nameCell(r) {
  const shown = escapeHtml(r.name || '') || '<span class="muted">—</span>';
  if (r.nameStatus === 'mismatch') {
    return `${escapeHtml(r.name || '—')}<span class="host-mismatch" title="Register expects '${escapeAttr(r.expectedName)}', device reports '${escapeAttr(r.name || '—')}'">⚠ name</span>`;
  }
  if (r.nameStatus === 'match') return `${shown}<span class="host-ok" title="Name matches register">✓</span>`;
  return shown;
}
function escapeAttr(s) { return escapeHtml(s).replace(/'/g, '&#39;'); }

function objectsCell(r) {
  if (r.objectCount == null) return '<span class="muted">—</span>';
  if (r.objectDiff) return `<span class="obj-diff" title="${escapeAttr(r.objectDiff)}">${r.objectCount} ⚠</span>`;
  return String(r.objectCount);
}

function registerBadge(r) {
  switch (r.register) {
    case 'match': return '<span class="badge green">Match</span>';
    case 'partial': return '<span class="badge amber">Partial</span>';
    case 'missing': return '<span class="badge red">Missing</span>';
    case 'rogue': return '<span class="badge red">Not in Register</span>';
    default: return '<span class="badge blue">Discovered</span>';
  }
}

function filteredRows() {
  return state.rows.filter((r) => {
    if (state.filter !== 'all' && r.rag !== state.filter) return false;
    if (state.search) {
      const hay = `${r.instance} ${r.name} ${r.vendor || ''} ${r.model || ''} ${r.firmware || ''} ${r.ip} ${r.location || ''}`.toLowerCase();
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
    $('sumDiscovered').textContent = s.expectedReachable;
    $('sumDiscoveredSub').textContent = `${pct(s.expectedReachable, s.expected)}% of expected · ${s.discovered} total`;
  } else {
    $('sumDiscovered').textContent = s.discovered;
    $('sumDiscoveredSub').textContent = 'Answered Who-Is';
  }
  $('sumMatches').textContent = s.matches;
  $('sumMatchesSub').textContent = s.expected ? `${pct(s.matches, s.expected)}% full match` : 'Full RAG match';
  $('sumRogue').textContent = s.rogue;
  $('sumRogueSub').textContent = s.discovered ? `${s.rogue} of ${s.discovered} not in register` : 'Not in register';
}
function pct(a, b) { return b ? Math.round((a / b) * 100) : 0; }

// ---------------------------------------------------------------------------
// Detail panel + object browser
// ---------------------------------------------------------------------------
function showDetail(r) {
  const panel = $('detailPanel');
  panel.classList.remove('empty');

  let nameCheck = '';
  if (r.nameStatus && r.nameStatus !== 'na') {
    const label = {
      match: '<span style="color:var(--green)">✓ Matches register</span>',
      mismatch: '<span style="color:var(--amber)">⚠ Does not match register</span>',
      unverified: '<span style="color:var(--muted)">Device reports no name</span>',
    }[r.nameStatus] || r.nameStatus;
    nameCheck = `
      <div class="detail-section">
        <h4>Name Check</h4>
        <div class="kv">
          <div class="k">Expected</div><div class="v">${escapeHtml(r.expectedName || '—')}</div>
          <div class="k">Reported</div><div class="v">${escapeHtml(r.name || '—')}</div>
          <div class="k">Result</div><div class="v">${label}</div>
        </div>
      </div>`;
  }

  let objDiff = '';
  if (r.objectDiff) objDiff = `<div class="port-diff"><div class="extra">Object count: ${escapeHtml(r.objectDiff)}</div></div>`;
  else if (r.expectedObjects != null && r.status === 'reachable') objDiff = `<div class="port-diff"><div style="color:var(--green)">Object count matches (${r.objectCount}).</div></div>`;

  const objState = state.objects[r.instance];
  const objBrowser = objBrowserHtml(r, objState);

  panel.innerHTML = `
    <div class="detail-head">
      <div class="detail-host">${escapeHtml(r.name || 'Device ' + r.instance)}</div>
      <div class="detail-actions">
        <button class="detail-expand" onclick="toggleExpand()" title="Pop out / expand (Esc to close)">⤢</button>
        <button class="detail-close" onclick="clearDetail()">✕</button>
      </div>
    </div>
    <div class="detail-badges">${statusBadge(r)} ${registerBadge(r)}</div>

    <div class="detail-section">
      <h4>Device Identity</h4>
      <div class="kv">
        <div class="k">Instance</div><div class="v">${r.instance}</div>
        <div class="k">Name</div><div class="v">${escapeHtml(r.name || '—')}</div>
        <div class="k">Vendor</div><div class="v">${escapeHtml(r.vendor || '—')}${r.vendorId != null ? ' (id ' + r.vendorId + ')' : ''}</div>
        <div class="k">Model</div><div class="v">${escapeHtml(r.model || '—')}</div>
        <div class="k">Firmware</div><div class="v">${escapeHtml(r.firmware || '—')}</div>
        <div class="k">App SW</div><div class="v">${escapeHtml(r.appSoftware || '—')}</div>
        <div class="k">Location</div><div class="v">${escapeHtml(r.location || '—')}</div>
        ${r.description ? `<div class="k">Description</div><div class="v">${escapeHtml(r.description)}</div>` : ''}
      </div>
    </div>

    <div class="detail-section">
      <h4>BACnet / Network</h4>
      <div class="kv">
        <div class="k">Address</div><div class="v">${escapeHtml(r.ip)}</div>
        <div class="k">Network</div><div class="v">${r.network || 0}${r.network ? '' : ' (local)'}</div>
        ${r.mac ? `<div class="k">MAC</div><div class="v">${escapeHtml(r.mac)}</div>` : ''}
        <div class="k">Max APDU</div><div class="v">${r.maxApdu != null ? r.maxApdu : '—'}</div>
        <div class="k">Segmentation</div><div class="v">${escapeHtml(r.segmentation || '—')}</div>
        <div class="k">Protocol Rev</div><div class="v">${r.protocolRevision !== '' && r.protocolRevision != null ? r.protocolRevision : '—'}</div>
        <div class="k">System Status</div><div class="v">${escapeHtml(r.systemStatus || '—')}</div>
        <div class="k">Object Count</div><div class="v">${r.objectCount != null ? r.objectCount : '—'}</div>
      </div>
      ${objDiff}
    </div>

    ${nameCheck}

    <div class="detail-section">
      <div class="obj-head">
        <h4 style="margin:0">Object List</h4>
        ${objBrowserBtn(r, objState)}
      </div>
      ${objBrowser}
    </div>`;
}

function statusBadge(r) {
  if (r.status === 'reachable') return '<span class="badge green">Reachable</span>';
  if (r.status === 'rogue') return '<span class="badge red">Rogue Device</span>';
  return '<span class="badge red">Not Discovered</span>';
}

function objBrowserBtn(r, objState) {
  if (r.status !== 'reachable') return '';
  if (objState && objState.loading) return '<button class="link-btn" disabled>Loading…</button>';
  const label = objState && objState.objects ? '↻ Reload' : '⬇ Load objects';
  return `<button class="link-btn" onclick="loadObjects(${r.instance})">${label}</button>`;
}

function objBrowserHtml(r, objState) {
  if (r.status !== 'reachable') return '<span class="muted">Device not reachable.</span>';
  if (!objState) return `<div class="obj-hint">Click “Load objects” to read this device's object list${r.objectCount != null ? ` (${r.objectCount} objects)` : ''}.</div>`;
  if (objState.loading) return `<div class="obj-hint">Reading objects… ${objState.done || 0}/${objState.totalToRead || '?'}</div>`;
  if (objState.error) return `<div class="obj-hint" style="color:var(--red)">${escapeHtml(objState.error)}</div>`;
  const objs = objState.objects || [];
  if (!objs.length) return '<div class="obj-hint">No objects returned.</div>';
  const rows = objs.map((o) => `
    <div class="obj-row">
      <span class="obj-type">${escapeHtml(o.typeName)}:${o.instance}</span>
      <span class="obj-name">${escapeHtml(o.name || '')}</span>
      <span class="obj-pv">${o.presentValue !== '' && o.presentValue != null ? escapeHtml(String(o.presentValue)) + (o.units ? ' ' + escapeHtml(o.units) : '') : ''}</span>
    </div>`).join('');
  const trunc = objState.truncated ? `<div class="obj-hint">Showing first ${objs.length} of ${objState.count} objects.</div>` : '';
  return `<div class="obj-list">${rows}</div>${trunc}`;
}

function loadObjects(instance) {
  const dev = state.devices.find((d) => d.instance === instance);
  const row = state.rows.find((r) => r.instance === instance);
  if (!dev && !row) return;
  const ip = (dev && dev.ip) || (row && row.ip.split(':')[0]);
  const port = (dev && dev.port) || 47808;
  const network = (dev && dev.network) || (row && row.network) || 0;
  const mac = (dev && dev.mac) || (row && row.mac) || '';

  if (state.objEs) { state.objEs.close(); state.objEs = null; }
  state.objects[instance] = { loading: true, done: 0, totalToRead: dev && dev.objectCount };
  render();

  const params = new URLSearchParams({ instance: String(instance), ip, port: String(port), network: String(network), mac });
  const es = new EventSource('/api/objects?' + params.toString());
  state.objEs = es;
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === 'progress') {
      const o = state.objects[instance];
      if (o) { o.done = ev.done; o.totalToRead = ev.total; render(); }
    } else if (ev.type === 'objects') {
      state.objects[instance] = { loading: false, objects: ev.objects, count: ev.count, truncated: ev.truncated, error: ev.error };
      render();
    } else if (ev.type === 'complete') {
      es.close(); state.objEs = null;
      const o = state.objects[instance];
      if (o) o.loading = false;
      render();
    } else if (ev.type === 'error') {
      state.objects[instance] = { loading: false, error: ev.message };
      es.close(); state.objEs = null;
      render();
    }
  };
  es.onerror = () => {
    const o = state.objects[instance];
    if (o && o.loading) { o.loading = false; if (!o.objects) o.error = 'Connection lost'; render(); }
    es.close(); state.objEs = null;
  };
}
window.loadObjects = loadObjects;

function clearDetail() {
  state.selectedInstance = null;
  const panel = $('detailPanel');
  collapseDetail();
  panel.classList.add('empty');
  panel.innerHTML = '<div class="detail-empty">Select a device to view details</div>';
  document.querySelectorAll('.results-table tbody tr').forEach((t) => t.classList.remove('selected'));
}
window.clearDetail = clearDetail;

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------
function csvCell(c) { return `"${String(c == null ? '' : c).replace(/"/g, '""')}"`; }
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
  const head = ['Device Instance', 'Device Name', 'Address', 'Network', 'Vendor', 'Model', 'Firmware', 'App SW', 'Location', 'Object Count', 'Register', 'RAG', 'Max APDU', 'Segmentation', 'System Status', 'Description'];
  const lines = [head.join(',')];
  for (const r of state.rows) {
    const cells = [
      r.instance, r.name || '', r.ip, r.network || 0, r.vendor || '', r.model || '',
      r.firmware || '', r.appSoftware || '', r.location || '', r.objectCount ?? '',
      r.register, r.rag, r.maxApdu ?? '', r.segmentation || '', r.systemStatus || '', r.description || '',
    ].map(csvCell);
    lines.push(cells.join(','));
  }
  downloadCsv(lines.join('\n'), 'bacnet-scan-results.csv');
}

// Build a BACnet Device Register from the CURRENT scan: each discovered device's
// instance, reported name, vendor/model and actual object count become the
// expected baseline. Import it and re-scan to validate the site stays that way.
function saveAsRegister() {
  const discovered = state.rows.filter((r) => r.status === 'reachable' || r.status === 'rogue');
  if (!discovered.length) return toast('Run a scan first, then save it as a register');
  const head = ['Device Instance', 'Device Name', 'Network', 'IP Address', 'Vendor', 'Model', 'Location', 'Expected Objects', 'Description'];
  const lines = [head.join(',')];
  for (const r of discovered) {
    const cells = [
      r.instance, r.name || '', r.network || 0, r.ip.split(':')[0], r.vendor || '', r.model || '',
      r.location || '', r.objectCount ?? '', r.description || '',
    ].map(csvCell);
    lines.push(cells.join(','));
  }
  downloadCsv(lines.join('\n'), 'bacnet-register.csv');
  toast(`Saved ${discovered.length} devices as a register. Import it to validate future scans.`);
}

// ---------------------------------------------------------------------------
// Misc UI helpers
// ---------------------------------------------------------------------------
function setScanState(cls, text) { const el = $('scanState'); el.className = 'scan-state ' + cls; el.textContent = text; }
function setProgress(pctVal, text) { $('progressFill').style.width = Math.min(100, pctVal) + '%'; $('progressText').textContent = text; }
let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 3200);
}
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
