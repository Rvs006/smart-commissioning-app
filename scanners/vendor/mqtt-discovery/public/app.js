'use strict';
/* MQTT Discovery frontend: connection, live SSE tree, flash, search, points, config, export. */

const $ = (id) => document.getElementById(id);
const EXPAND_KEY = 'mqttDiscovery.expanded';
const state = {
  status: {}, stats: {}, tree: [], focused: null,
  selectedAsset: '', selectedPath: '',
  registerLoaded: false, connected: false,
  paused: false, tab: 'overview', sortMode: 'rate',
  histView: null, cfgTouched: false,
  es: null,
  expanded: loadExpanded(),
};

function loadExpanded() { try { return new Set(JSON.parse(localStorage.getItem(EXPAND_KEY) || '[]')); } catch (_) { return new Set(); } }
function saveExpanded() { try { localStorage.setItem(EXPAND_KEY, JSON.stringify(Array.from(state.expanded))); } catch (_) {} }

window.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  initSplitter();
  openStream();
  refreshStatus();
});

function bindEvents() {
  $('connectBtn').addEventListener('click', openConnModal);
  $('disconnectBtn').addEventListener('click', doDisconnect);
  $('importBtn').addEventListener('click', () => $('csvFile').click());
  $('clearRegBtn').addEventListener('click', clearRegister);
  $('csvFile').addEventListener('change', onCsvChosen);
  $('topicSearch').addEventListener('input', onSearchInput);
  $('matchedOnly').addEventListener('change', pushSearch);
  $('publishBtn').addEventListener('click', openPubModal);
  $('saveRegBtn').addEventListener('click', saveAsRegister);
  $('exportBtnGlobal').addEventListener('click', exportArchive);
  $('pauseBtn').addEventListener('click', togglePause);
  $('applyFilterBtn').addEventListener('click', applyFilter);
  $('rootFilterInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') applyFilter(); });

  // tree controls
  document.querySelectorAll('.sort-btn').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('.sort-btn').forEach((x) => x.classList.remove('active'));
    b.classList.add('active'); state.sortMode = b.dataset.sort; renderTree();
  }));
  $('expandTopBtn').addEventListener('click', expandTop);
  $('collapseAllBtn').addEventListener('click', () => { state.expanded.clear(); saveExpanded(); renderTree(); });

  // tabs
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => selectTab(t.dataset.tab)));
  document.querySelectorAll('.nav-tab[data-stub]').forEach((t) => t.addEventListener('click', () => toast(`${t.dataset.stub} is part of the SCA suite — not included in this MQTT Discovery build.`)));

  // connection modal
  $('doConnectBtn').addEventListener('click', submitConnect);
  $('cProto').addEventListener('change', onProtoChange);
  bindCertUploads();
  document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => $('connModal').classList.add('hidden')));
  // publish modal
  $('doPublishBtn').addEventListener('click', submitPublish);
  document.querySelectorAll('[data-close-pub]').forEach((b) => b.addEventListener('click', () => $('pubModal').classList.add('hidden')));
  // config
  $('cfgPayload').addEventListener('input', () => { state.cfgTouched = true; });
  $('cfgPrefillBtn').addEventListener('click', prefillConfig);
  $('cfgSendBtn').addEventListener('click', openCfgConfirm);
  $('cfgConfirmBtn').addEventListener('click', sendConfig);
  document.querySelectorAll('[data-close-cfg]').forEach((b) => b.addEventListener('click', () => $('cfgModal').classList.add('hidden')));
}

// ---------------------------------------------------------------------------
// SSE stream
// ---------------------------------------------------------------------------
function openStream() {
  if (state.es) state.es.close();
  const es = new EventSource('api/stream');
  state.es = es;
  es.onmessage = (msg) => {
    let d; try { d = JSON.parse(msg.data); } catch (_) { return; }
    if (d.type === 'activity') return handleActivity(d.paths || []);
    if (d.type !== 'snapshot') return;
    state.status = d.status || {};
    state.stats = d.stats || {};
    state.tree = d.tree || [];
    state.treeShown = d.treeShown; state.totalTopics = d.totalTopics; state.filtered = d.filtered;
    if (d.focused) state.focused = d.focused;
    applySnapshot();
  };
  es.onerror = () => {};
}

function applySnapshot() {
  renderStatus();
  renderMetrics();
  renderTree();
  renderFoot();
  if (state.selectedAsset && state.focused && state.focused.key === state.selectedAsset) renderDetail();
}

async function refreshStatus() {
  try { const r = await fetch('api/status'); const d = await r.json(); if (d.register) setRegisterLoaded(d.register.assets > 0, d.register); } catch (_) {}
}

// ---------------------------------------------------------------------------
// Status + metrics
// ---------------------------------------------------------------------------
function renderStatus() {
  const s = state.status || {};
  state.connected = s.status === 'connected';
  const el = $('brokerStatus');
  const map = {
    connected: ['ok', `Connected · ${s.host}:${s.port}`],
    connecting: ['warn', 'Connecting…'],
    reconnecting: ['warn', `Reconnecting · ${s.host}:${s.port}`],
    error: ['err', s.error ? 'Error: ' + s.error : 'Error'],
    disconnected: ['muted-value', 'Disconnected'],
  };
  const [cls, txt] = map[s.status] || ['muted-value', 'Disconnected'];
  el.className = 'status-value ' + cls; el.textContent = txt;

  // filter card reflects the live subscription; don't stomp the user mid-edit
  if (document.activeElement !== $('rootFilterInput') && s.rootFilter) $('rootFilterInput').value = s.rootFilter;
  if (document.activeElement !== $('qosSelect') && s.qos != null && state.connected) $('qosSelect').value = String(s.qos);

  $('connectBtn').classList.toggle('hidden', state.connected);
  $('disconnectBtn').classList.toggle('hidden', !state.connected);
  $('liveBadge').classList.toggle('hidden', !state.connected);
  $('applyFilterBtn').disabled = !state.connected;
  $('publishBtn').disabled = !state.connected;
  $('cfgSendBtn').disabled = !(state.connected && state.focused && state.focused.configTopic);
  $('footConn').textContent = state.connected ? `${s.host}:${s.port} · ${s.tls ? 'TLS' : 'plain'} · ${s.rootFilter}` : '';
}

function renderMetrics() {
  const st = state.stats || {};
  $('mExpected').textContent = st.expectedAssets || 0;
  $('mSubscribed').textContent = st.subscribedAssets || 0;
  $('mTopics').textContent = fmtNum(st.topicsDiscovered || 0);
  $('mIssues').textContent = fmtNum(st.issues || 0);
  const has = (st.topicsDiscovered || 0) > 0;
  $('saveRegBtn').disabled = !has; $('exportBtnGlobal').disabled = !has;
}
function renderFoot() {
  const shown = state.treeShown != null ? state.treeShown : 0;
  const total = state.totalTopics != null ? state.totalTopics : 0;
  $('listFoot').textContent = total ? `${fmtNum(shown)} of ${fmtNum(total)} topics${state.filtered ? ' (filtered)' : ''}` : '';
}
function fmtNum(n) { return Number(n).toLocaleString('en-US'); }

// ---------------------------------------------------------------------------
// Tree (incremental DOM, preserves expand state + flashes)
// ---------------------------------------------------------------------------
function sortNodes(nodes) {
  if (state.sortMode === 'name') return nodes.slice().sort((a, b) => a.n.localeCompare(b.n));
  return nodes; // server already sorts by rate desc
}

function renderTree() {
  const container = $('tree');
  if (!state.tree.length) {
    container.innerHTML = `<div class="tree-empty">${state.connected ? (state.filtered ? 'No topics match the search.' : 'Waiting for messages…') : 'Not connected. Click <strong>Connect…</strong> to subscribe to a broker.'}</div>`;
    return;
  }
  if (container.querySelector('.tree-empty')) container.innerHTML = '';
  reconcile(container, sortNodes(state.tree), 0);
}

function reconcile(parentEl, nodes, depth) {
  const existing = new Map();
  for (const el of Array.from(parentEl.children)) if (el.dataset && el.dataset.path != null) existing.set(el.dataset.path, el);
  const seen = new Set();
  let prev = null;
  for (const nd of nodes) {
    seen.add(nd.p);
    let el = existing.get(nd.p);
    if (!el) el = createNodeEl(nd);
    updateNodeEl(el, nd);
    if (prev) { if (prev.nextSibling !== el) parentEl.insertBefore(el, prev.nextSibling); }
    else if (parentEl.firstChild !== el) parentEl.insertBefore(el, parentEl.firstChild);
    prev = el;

    const childrenEl = el._children;
    const exp = state.expanded.has(nd.p);
    const caret = el._caret;
    const hasKids = !!(nd.ch && nd.ch.length);
    caret.className = 'tcaret' + (nd.leaf || !hasKids ? ' leaf' : (exp ? ' open' : ''));
    if (exp && hasKids) { childrenEl.style.display = ''; reconcile(childrenEl, sortNodes(nd.ch), depth + 1); }
    else { childrenEl.style.display = 'none'; }
  }
  for (const [p, el] of existing) if (!seen.has(p)) el.remove();
}

function createNodeEl(nd) {
  const node = document.createElement('div');
  node.className = 'tnode'; node.dataset.path = nd.p;
  const row = document.createElement('div'); row.className = 'trow';
  const caret = document.createElement('span'); caret.className = 'tcaret'; caret.textContent = '▶';
  const name = document.createElement('span'); name.className = 'tname';
  const meta = document.createElement('span'); meta.className = 'tmeta';
  const badges = document.createElement('span'); badges.className = 'tbadges';
  const copy = document.createElement('button'); copy.className = 'tcopy'; copy.textContent = '⧉'; copy.title = 'Copy topic path';
  const rate = document.createElement('span'); rate.className = 'trate';
  row.append(caret, name, meta, badges, copy, rate);
  const children = document.createElement('div'); children.className = 'tchildren';
  node.append(row, children);
  node._row = row; node._caret = caret; node._name = name; node._meta = meta; node._badges = badges; node._rate = rate; node._children = children;

  caret.addEventListener('click', (e) => { e.stopPropagation(); toggleExpand(nd.p); });
  copy.addEventListener('click', (e) => { e.stopPropagation(); copyText(node.dataset.path); });
  row.addEventListener('click', () => onNodeClick(node.dataset.path));
  return node;
}

function updateNodeEl(el, nd) {
  el._node = nd;
  el._name.textContent = nd.n;
  el._name.className = 'tname' + (nd.dev ? ' device' : (nd.leaf ? ' leaffolder' : ''));
  el._meta.textContent = nd.leaf ? `${fmtNum(nd.m)} msgs` : `(${fmtNum(nd.t)} topics · ${fmtNum(nd.m)} msgs)`;
  el._rate.textContent = nd.r ? nd.r.toFixed(1) + ' msg/s' : '';
  let b = '';
  if (nd.mt) b += '<span class="tbadge match">✓</span>';
  if (nd.iss) b += `<span class="tbadge iss">⚠${nd.iss}</span>`;
  if (nd.ret) b += '<span class="tbadge ret">R</span>';
  el._badges.innerHTML = b;
  el._row.classList.toggle('selected', nd.p === state.selectedPath || (nd.a && nd.a.toUpperCase() === state.selectedAsset && nd.dev));
}

function toggleExpand(path) {
  if (state.expanded.has(path)) state.expanded.delete(path); else state.expanded.add(path);
  saveExpanded(); renderTree();
}
function onNodeClick(path) {
  const nd = findNode(state.tree, path);
  if (!nd) return;
  if ((nd.ch && nd.ch.length)) toggleExpand(path);
  if (nd.a) { state.selectedPath = path; focusAsset(nd.a); }
  else renderTree();
}
function findNode(nodes, path) {
  for (const n of nodes) { if (n.p === path) return n; if (n.ch && path.startsWith(n.p + '/')) { const f = findNode(n.ch, path); if (f) return f; } }
  return null;
}
function expandTop() { for (const n of state.tree) state.expanded.add(n.p); saveExpanded(); renderTree(); }

function handleActivity(paths) {
  const treeEl = $('tree');
  const set = new Set();
  for (const p of paths) { let acc = ''; for (const s of p.split('/')) { acc = acc ? acc + '/' + s : s; set.add(acc); } }
  let n = 0;
  for (const path of set) {
    if (n++ > 300) break;
    let el; try { el = treeEl.querySelector('.tnode[data-path="' + cssEsc(path) + '"] > .trow'); } catch (_) { el = null; }
    if (el) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
  }
}
function cssEsc(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&'); }

async function focusAsset(asset) {
  state.selectedAsset = String(asset).toUpperCase();
  state.histView = null; state.cfgTouched = false;
  try {
    const res = await fetch('api/focus', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset }) });
    const d = await res.json();
    if (d.focused) { state.focused = d.focused; renderDetail(); }
  } catch (_) {}
  showDetailBody();
  renderTree();
}

// ---------------------------------------------------------------------------
// Search + filter
// ---------------------------------------------------------------------------
let searchTimer;
function onSearchInput() { clearTimeout(searchTimer); searchTimer = setTimeout(pushSearch, 220); }
async function pushSearch() {
  const q = $('topicSearch').value;
  const matchedOnly = $('matchedOnly').checked;
  try {
    const res = await fetch('api/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ q, matchedOnly }) });
    const d = await res.json();
    if (d.type === 'snapshot') { state.tree = d.tree || []; state.treeShown = d.treeShown; state.totalTopics = d.totalTopics; state.filtered = d.filtered; renderTree(); renderFoot(); }
  } catch (_) {}
}

async function applyFilter() {
  if (!state.connected) return;
  const rootFilter = $('rootFilterInput').value.trim() || '#';
  const qos = Number($('qosSelect').value) || 0;
  const clear = $('clearOnChange').checked;
  try {
    const res = await fetch('api/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rootFilter, qos, clear }) });
    const d = await res.json();
    if (d.ok) { if (clear) { state.selectedAsset = ''; state.selectedPath = ''; clearDetail(); } toast('Subscription changed to ' + rootFilter); }
    else toast('Change failed: ' + (d.error || ''));
  } catch (e) { toast('Change failed: ' + e.message); }
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------
function showDetailBody() { $('detailEmpty').classList.add('hidden'); $('detailBody').classList.remove('hidden'); }
function selectTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tabpane').forEach((p) => p.classList.toggle('active', p.dataset.pane === tab));
  if (tab === 'config') ensureConfigPrefill();
  renderDetail();
}

function renderDetail() {
  const f = state.focused; if (!f) return;
  $('dAsset').textContent = f.asset;
  const badges = [];
  badges.push(f.count ? '<span class="badge green">Subscribed</span>' : '<span class="badge grey">Idle</span>');
  badges.push(f.matched ? '<span class="badge blue">Matched</span>' : '<span class="badge orange">Not in register</span>');
  if (f.schema) badges.push(`<span class="badge grey">${escapeHtml(f.schema)}</span>`);
  $('dBadges').innerHTML = badges.join(' ');
  $('dLive').innerHTML = `<span class="live-dot">●</span> Live &nbsp; ${f.rate ? f.rate.toFixed(1) : '0.0'} msg/s &nbsp; ${fmtNum(f.count)} msgs`;
  $('exportBtn').href = `api/export?asset=${encodeURIComponent(f.key)}`;

  renderOverview(f);
  renderLive(f);
  renderPoints(f);
  renderConfig(f);
  renderMeta(f);
}

function renderOverview(f) {
  const m = f.meta || {}; const u = f.udmi || {};
  const kv = [
    ['Asset', f.asset], ['Type', m.type || '—'],
    ['Schema', f.schema ? f.schema + (u.version ? ' · ' + u.version : '') : '—'],
    ['Topics', (f.topics || []).length], ['Message rate', `${f.rate ? f.rate.toFixed(1) : '0.0'} msg/s`],
    ['Total messages', fmtNum(f.count)], ['Live points', (f.livePoints || []).length],
    ['In register', f.matched ? 'Yes' : 'No'], ['Site', m.site || u.site || '—'], ['Room / Location', m.location || u.room || '—'],
  ];
  if (u.gatewayId) kv.push(['Gateway', u.gatewayId]);
  if (u.proxyIds && u.proxyIds.length) kv.push(['Proxies', u.proxyIds.join(', ')]);
  if (u.guid) kv.push(['GUID', u.guid]);
  $('ovKv').innerHTML = kv.map(([k, v]) => `<div class="k">${k}</div><div class="v">${escapeHtml(String(v))}</div>`).join('');

  const td = f.topicsDetail || [];
  $('ovTopics').innerHTML = td.length ? td.map((t) =>
    `<div class="topic-line"><span class="tl-path">${escapeHtml(t.topic)}</span><span class="tl-rate">${t.rate ? t.rate.toFixed(1) : '0.0'} msg/s</span><button class="tcopy" data-copy="${escapeAttr(t.topic)}">⧉</button></div>`
  ).join('') : '<span class="muted" style="color:var(--muted-2)">—</span>';
  $('ovTopics').querySelectorAll('.tcopy').forEach((b) => b.addEventListener('click', () => copyText(b.dataset.copy)));

  $('ovCmp').innerHTML = f.matched ? comparisonHtml(f.comparison, true) : '<div class="sub-title">Register Comparison</div><div style="font-size:13px;color:var(--muted-2)">This asset is not in the imported register.</div>';
}

function renderLive(f) {
  // history chips from the freshest topic
  const td = (f.topicsDetail || []).find((t) => t.topic === f.lastTopic) || (f.topicsDetail || [])[0];
  const hist = td ? td.history : [];
  const chips = ['<span class="hist-chip' + (state.histView === null ? ' active' : '') + '" data-h="live">Live</span>'];
  hist.slice().reverse().forEach((h, i) => { const idx = hist.length - 1 - i; chips.push(`<span class="hist-chip${state.histView === idx ? ' active' : ''}" data-h="${idx}">${fmtTs(h.ts)}</span>`); });
  $('histChips').innerHTML = hist.length ? chips.join('') : '';
  $('histChips').querySelectorAll('.hist-chip').forEach((c) => c.addEventListener('click', () => {
    state.histView = c.dataset.h === 'live' ? null : Number(c.dataset.h); renderLive(f);
  }));

  let payload;
  if (state.histView !== null && td && td.history[state.histView]) payload = td.history[state.histView].raw;
  else payload = f.lastPayload || '';
  if (!(state.paused && state.histView === null)) $('jsonView').innerHTML = highlightJson(payload);

  const pts = f.livePoints || [];
  $('pointsMini').querySelector('tbody').innerHTML = pts.length
    ? pts.slice(0, 12).map((p) => `<tr><td>${escapeHtml(p.name)}</td><td class="pv">${fmtVal(p.value)}</td><td class="pu">${escapeHtml(p.unit || '')}</td><td class="pt">${fmtTs(p.ts)}</td></tr>`).join('')
    : '<tr><td colspan="4" style="color:var(--muted-2)">No points extracted yet</td></tr>';
  $('cmpMini').innerHTML = f.matched ? comparisonHtml(f.comparison, false) : '<div style="font-size:12px;color:var(--muted-2)">Not in register</div>';
}

function renderPoints(f) {
  const pts = f.livePoints || []; const cmp = f.comparison || {};
  const extra = new Set(cmp.extraNames || []);
  const norm = (s) => String(s || '').trim().toLowerCase().replace(/[\s\-]+/g, '_');
  const rows = pts.map((p) => {
    const reg = f.matched ? (extra.has(norm(p.name)) ? '<span class="pt-extra">extra</span>' : '<span class="pt-match">matched</span>') : '<span class="muted">—</span>';
    return `<tr><td>${escapeHtml(p.name)}</td><td class="pv">${fmtVal(p.value)}</td><td class="pu">${escapeHtml(p.unit || '')}</td><td>${reg}</td><td class="pt">${fmtTs(p.ts)}</td></tr>`;
  });
  for (const mn of (cmp.missingNames || [])) rows.push(`<tr><td>${escapeHtml(mn)}</td><td class="pv muted">—</td><td></td><td><span class="pt-miss">missing</span></td><td></td></tr>`);
  $('pointsFull').querySelector('tbody').innerHTML = rows.length ? rows.join('') : '<tr><td colspan="5" style="color:var(--muted-2)">No points</td></tr>';
}

function renderMeta(f) {
  const m = f.meta; const u = f.udmi || {}; let html = '';
  const uKv = [];
  if (u.gatewayId) uKv.push(['Gateway ID', u.gatewayId]);
  if (u.proxyIds && u.proxyIds.length) uKv.push(['Proxied devices', u.proxyIds.join(', ')]);
  if (u.site) uKv.push(['Site', u.site]);
  if (u.room) uKv.push(['Room', u.room]);
  if (u.guid) uKv.push(['GUID', u.guid]);
  if (u.version) uKv.push(['UDMI version', u.version]);
  if (uKv.length) html += `<div class="sub-title">Discovered (from payload)</div><div class="kv">${uKv.map(([k, v]) => `<div class="k">${k}</div><div class="v">${escapeHtml(String(v))}</div>`).join('')}</div>`;
  if (m) {
    const kv = [['Asset', m.asset], ['Type', m.type || '—'], ['Topic', m.topic || '—'], ['Schema', m.schema || '—'], ['Site', m.site || '—'], ['Location', m.location || '—'], ['Description', m.description || '—']];
    const expPts = (m.points || []).map((p) => `<span class="badge grey" style="margin:2px">${escapeHtml(p.name)}${p.unit ? ' (' + escapeHtml(p.unit) + ')' : ''}</span>`).join(' ');
    html += `<div class="sub-title" style="margin-top:16px">Register (expected)</div><div class="kv">${kv.map(([k, v]) => `<div class="k">${k}</div><div class="v">${escapeHtml(String(v))}</div>`).join('')}</div>
      <div class="sub-title" style="margin-top:16px">Expected Points (${(m.points || []).length})</div><div>${expPts || '<span class="muted">none</span>'}</div>`;
  } else if (!uKv.length) html = '<div style="color:var(--muted-2);font-size:13px">No metadata yet — not in register and no UDMI identity block in the payloads.</div>';
  else html += '<div style="color:var(--muted-2);font-size:12px;margin-top:12px">Not in the imported register.</div>';
  $('metaBody').innerHTML = html;
}

function comparisonHtml(cmp, withTitle) {
  cmp = cmp || { matched: 0, missing: 0, extra: 0 };
  const total = Math.max(1, cmp.matched + cmp.missing + cmp.extra);
  const pct = (n) => Math.round((n / total) * 100);
  const bar = `<div class="cmp-bar"><div class="seg-match" style="width:${(cmp.matched / total) * 100}%"></div><div class="seg-miss" style="width:${(cmp.missing / total) * 100}%"></div><div class="seg-extra" style="width:${(cmp.extra / total) * 100}%"></div></div>`;
  const legend = `<div class="cmp-legend"><div class="row"><span class="dot match"></span>Matched ${cmp.matched} (${pct(cmp.matched)}%)</div><div class="row"><span class="dot miss"></span>Missing ${cmp.missing} (${pct(cmp.missing)}%)</div><div class="row"><span class="dot extra"></span>Extra ${cmp.extra} (${pct(cmp.extra)}%)</div></div>`;
  return (withTitle ? '<div class="sub-title">Register Comparison</div>' : '') + bar + legend;
}

// ---------------------------------------------------------------------------
// Config tab
// ---------------------------------------------------------------------------
function renderConfig(f) {
  $('cfgTopic').textContent = f.configTopic || '(no device path)';
  $('cfgSendBtn').disabled = !(state.connected && f.configTopic);
}
function ensureConfigPrefill() {
  const f = state.focused; if (!f) return;
  if (!state.cfgTouched && !$('cfgPayload').value && f.configPayload) $('cfgPayload').value = prettyJson(f.configPayload);
}
function prefillConfig() {
  const f = state.focused; if (!f) return;
  if (f.configPayload) { $('cfgPayload').value = prettyJson(f.configPayload); state.cfgTouched = false; toast('Loaded current config'); }
  else toast('No current config seen for this device yet');
}
function openCfgConfirm() {
  const f = state.focused; if (!f || !f.configTopic) return;
  const payload = $('cfgPayload').value;
  try { JSON.parse(payload); } catch (e) { showErr('cfgError', 'Payload is not valid JSON: ' + e.message); return; }
  $('cfgError').classList.add('hidden');
  $('cfgConfTopic').textContent = f.configTopic;
  $('cfgConfOpts').textContent = `QoS ${$('cfgQos').value} · ${$('cfgRetain').checked ? 'retained' : 'not retained'}`;
  $('cfgConfPayload').innerHTML = highlightJson(payload);
  $('cfgModal').classList.remove('hidden');
}
async function sendConfig() {
  const f = state.focused; if (!f) return;
  try {
    const res = await fetch('api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset: f.key, payload: $('cfgPayload').value, qos: Number($('cfgQos').value) || 0, retain: $('cfgRetain').checked }) });
    const d = await res.json();
    $('cfgModal').classList.add('hidden');
    if (d.ok) toast('Config sent to ' + d.topic);
    else toast('Config failed: ' + (d.error || ''));
  } catch (e) { toast('Config failed: ' + e.message); }
}

// ---------------------------------------------------------------------------
// Pause / export
// ---------------------------------------------------------------------------
function togglePause() {
  state.paused = !state.paused;
  $('pauseBtn').textContent = state.paused ? '▶ Resume Stream' : '⏸ Pause Stream';
  if (!state.paused && state.focused) renderLive(state.focused);
}
async function exportArchive() {
  toast('Building export…');
  try {
    const res = await fetch('api/export-archive');
    if (!res.ok) { const t = await res.text(); return toast('Export failed: ' + t); }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : 'mqtt-discovery-export.zip';
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href);
    toast('Export downloaded: ' + name);
  } catch (e) { toast('Export failed: ' + e.message); }
}

// ---------------------------------------------------------------------------
// Connection modal
// ---------------------------------------------------------------------------
function openConnModal() { $('connError').classList.add('hidden'); if (!$('cFilter').value) $('cFilter').value = '#'; $('connModal').classList.remove('hidden'); $('cHost').focus(); }
function onProtoChange() {
  const tls = $('cProto').value === 'mqtts';
  if (!$('cPort').value || $('cPort').value === '1883' || $('cPort').value === '8883') $('cPort').value = tls ? '8883' : '1883';
  $('validateWrap').style.display = tls ? '' : 'none';
}
async function submitConnect() {
  const tls = $('cProto').value === 'mqtts';
  const cfg = {
    name: $('cName').value.trim(), host: $('cHost').value.trim(),
    port: Number($('cPort').value) || (tls ? 8883 : 1883), tls, validateCert: $('cValidate').checked,
    clientId: $('cClientId').value.trim() || undefined, username: $('cUser').value.trim() || undefined, password: $('cPass').value || undefined,
    rootFilter: $('cFilter').value.trim() || '#', qos: Number($('cQos').value) || 0,
    ca: $('cCa').value.trim() || undefined, cert: $('cCert').value.trim() || undefined, key: $('cKey').value.trim() || undefined,
  };
  if (!cfg.host) return showErr('connError', 'Host is required');
  $('doConnectBtn').disabled = true; $('doConnectBtn').textContent = 'Connecting…';
  try {
    const res = await fetch('api/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
    const d = await res.json();
    if (d.ok) { $('connModal').classList.add('hidden'); toast('Connected — subscribing to ' + cfg.rootFilter); }
    else showErr('connError', d.error || 'Connection failed');
  } catch (e) { showErr('connError', 'Connection failed: ' + e.message); }
  finally { $('doConnectBtn').disabled = false; $('doConnectBtn').textContent = 'Connect'; }
}
async function doDisconnect() { try { await fetch('api/disconnect', { method: 'POST' }); toast('Disconnected'); state.focused = null; state.selectedAsset = ''; state.selectedPath = ''; clearDetail(); } catch (_) {} }

function bindCertUploads() {
  document.querySelectorAll('.file-btn[data-cert]').forEach((btn) => {
    const id = btn.dataset.cert; const input = $(id + 'Input'); if (!input) return;
    btn.addEventListener('click', () => input.click());
    input.addEventListener('change', (e) => {
      const file = e.target.files[0]; if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || ''); $(id).value = text;
        const badge = $(id + 'File');
        if (/-----BEGIN /.test(text)) { badge.textContent = `✓ ${file.name}`; badge.style.color = 'var(--green)'; }
        else { badge.textContent = `⚠ ${file.name} — not PEM text (DER not supported)`; badge.style.color = 'var(--amber)'; }
      };
      reader.onerror = () => toast('Could not read ' + file.name);
      reader.readAsText(file); e.target.value = '';
    });
  });
}

// ---------------------------------------------------------------------------
// Register
// ---------------------------------------------------------------------------
function onCsvChosen(e) {
  const file = e.target.files[0]; if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const res = await fetch('api/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ csv: reader.result }) });
      const d = await res.json(); setRegisterLoaded(d.imported > 0, { assets: d.imported, points: d.points });
      toast(`Imported ${d.imported} assets · ${d.points} expected points`);
    } catch (err) { toast('Import failed: ' + err.message); }
  };
  reader.readAsText(file); e.target.value = '';
}
function setRegisterLoaded(loaded, info) {
  state.registerLoaded = loaded; const el = $('registerStatus');
  if (loaded) { el.textContent = `Register loaded (${info.assets})`; el.className = 'status-value ok'; $('clearRegBtn').classList.remove('hidden'); }
  else { el.textContent = 'No register'; el.className = 'status-value muted-value'; $('clearRegBtn').classList.add('hidden'); }
}
async function clearRegister() { try { await fetch('api/register', { method: 'DELETE' }); setRegisterLoaded(false, {}); toast('Register cleared'); } catch (_) {} }

// ---------------------------------------------------------------------------
// Save as register + publish
// ---------------------------------------------------------------------------
async function saveAsRegister() {
  if (!(state.stats.topicsDiscovered > 0)) return toast('Nothing discovered yet');
  try {
    const res = await fetch('api/generate-register'); const csv = await res.text();
    const blob = new Blob([csv], { type: 'text/csv' }); const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = 'mqtt-register-discovered.csv'; a.click(); URL.revokeObjectURL(a.href);
    toast(`Saved ${Math.max(0, csv.trim().split('\n').length - 1)} register rows`);
  } catch (e) { toast('Save failed: ' + e.message); }
}
function openPubModal() { $('pubError').classList.add('hidden'); if (state.focused && state.focused.lastTopic) $('pTopic').value = state.focused.lastTopic; $('pubModal').classList.remove('hidden'); $('pTopic').focus(); }
async function submitPublish() {
  const topic = $('pTopic').value.trim(); if (!topic) return showErr('pubError', 'Topic is required');
  const fmt = document.querySelector('input[name="pfmt"]:checked').value; const payload = $('pPayload').value;
  if (fmt === 'json' && payload.trim()) { try { JSON.parse(payload); } catch (e) { return showErr('pubError', 'Payload is not valid JSON: ' + e.message); } }
  try {
    const res = await fetch('api/publish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic, payload, qos: Number($('pQos').value) || 0, retain: $('pRetain').checked }) });
    const d = await res.json();
    if (d.ok) { $('pubModal').classList.add('hidden'); toast('Published to ' + topic); } else showErr('pubError', d.error || 'Publish failed');
  } catch (e) { showErr('pubError', 'Publish failed: ' + e.message); }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function clearDetail() {
  state.selectedAsset = ''; state.selectedPath = '';
  $('detailEmpty').classList.remove('hidden'); $('detailBody').classList.add('hidden');
}
function copyText(t) { if (!t) return; (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(() => toast('Copied: ' + t)).catch(() => { const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); toast('Copied: ' + t); } catch (_) {} ta.remove(); }); }
function showErr(id, msg) { const e = $(id); e.textContent = msg; e.classList.remove('hidden'); }
function fmtVal(v) { if (v == null) return '—'; if (typeof v === 'number') return String(v); if (typeof v === 'boolean') return v ? 'true' : 'false'; return escapeHtml(String(v).slice(0, 40)); }
function fmtTs(ts) { if (!ts) return ''; return new Date(ts).toLocaleTimeString('en-GB', { hour12: false }); }
function prettyJson(raw) { try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (_) { return raw; } }
function highlightJson(raw) {
  if (!raw) return '<span style="color:var(--muted-2)">— no payload —</span>';
  let text = raw; try { text = JSON.stringify(JSON.parse(raw), null, 2); } catch (_) { return escapeHtml(raw.slice(0, 12000)); }
  return escapeHtml(text)
    .replace(/&quot;([^&]*?)&quot;(\s*:)/g, '<span class="jk">&quot;$1&quot;</span>$2')
    .replace(/:\s*&quot;([^&]*?)&quot;/g, ': <span class="js">&quot;$1&quot;</span>')
    .replace(/:\s*(-?\d+\.?\d*)/g, ': <span class="jn">$1</span>')
    .replace(/:\s*(true|false|null)/g, ': <span class="jb">$1</span>');
}
let toastTimer;
function toast(msg) { const t = $('toast'); t.textContent = msg; t.classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 3600); }
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function escapeAttr(s) { return escapeHtml(s).replace(/'/g, '&#39;'); }

// ---------------------------------------------------------------------------
// Resizable split
// ---------------------------------------------------------------------------
const SPLIT_KEY = 'mqttDiscovery.leftWidth';
const SPLIT_DEFAULT = 460;
function initSplitter() {
  const splitter = $('splitter'); const content = document.querySelector('.content'); if (!splitter || !content) return;
  const clamp = (w) => { const total = content.clientWidth; const min = 300; const max = Math.max(min, total - 380); return Math.min(max, Math.max(min, w)); };
  const setWidth = (w) => document.documentElement.style.setProperty('--left-w', clamp(w) + 'px');
  const saved = Number(localStorage.getItem(SPLIT_KEY)); if (saved) setWidth(saved);
  let dragging = false;
  const onMove = (e) => { if (!dragging) return; const x = (e.touches ? e.touches[0].clientX : e.clientX) - content.getBoundingClientRect().left; setWidth(x); e.preventDefault(); };
  const onUp = () => { if (!dragging) return; dragging = false; splitter.classList.remove('dragging'); document.body.classList.remove('col-resizing'); const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--left-w'), 10); if (cur) localStorage.setItem(SPLIT_KEY, String(cur)); window.removeEventListener('mousemove', onMove); window.removeEventListener('touchmove', onMove); window.removeEventListener('mouseup', onUp); window.removeEventListener('touchend', onUp); };
  const onDown = (e) => { dragging = true; splitter.classList.add('dragging'); document.body.classList.add('col-resizing'); window.addEventListener('mousemove', onMove); window.addEventListener('touchmove', onMove, { passive: false }); window.addEventListener('mouseup', onUp); window.addEventListener('touchend', onUp); e.preventDefault(); };
  splitter.addEventListener('mousedown', onDown); splitter.addEventListener('touchstart', onDown, { passive: false });
  splitter.addEventListener('dblclick', () => { setWidth(SPLIT_DEFAULT); localStorage.setItem(SPLIT_KEY, String(SPLIT_DEFAULT)); });
  window.addEventListener('resize', () => { const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--left-w'), 10); if (cur) setWidth(cur); });
}
