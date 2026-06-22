/* ============================================================
   app.js — Backup Manager dashboard core.
   Talks to the live API; pairs with forms.js (openProjectForm,
   retentionLabel, cronLabel).
   ============================================================ */

const HOUR = 3600e3;
const ENG = { postgres:['PG','pg'], mariadb:['Maria','maria'], mysql:['MySQL','mysql'] };
const STATUS_LABEL = { ok:'Healthy', warn:'Stale', idle:'No data' };

// ── tiny helpers ──
const $  = (s, r=document) => r.querySelector(s);
function el(html) { const t=document.createElement('template'); t.innerHTML=html.trim(); return t.content.firstChild; }
function esc(s) { const d=document.createElement('div'); d.textContent = s==null?'':s; return d.innerHTML; }
function engBadge(e) { const m=ENG[e]||[e,'pg']; return `<span class="eng ${m[1]}">${m[0]}</span>`; }
function ago(ms) { if(ms==null) return '—'; const s=(Date.now()-ms)/1000; if(s<60)return 'just now'; const m=s/60; if(m<60)return Math.round(m)+'m ago'; const h=m/60; return h<24?Math.round(h)+'h ago':Math.round(h/24)+'d ago'; }
function nextFmt(iso) { if(!iso) return 'Manual'; const d=new Date(iso)-new Date(); if(d<0) return 'soon'; const h=d/HOUR; if(h<1)return 'in '+Math.round(h*60)+'m'; if(h<24)return 'in '+Math.round(h)+'h'; return 'in '+Math.round(h/24)+'d'; }
function fmtDate(iso) { return new Date(iso).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
function fmtSize(b) { if(b==null)return '—'; if(b<1024)return b+' B'; if(b<1048576)return (b/1024).toFixed(1)+' KB'; if(b<1073741824)return (b/1048576).toFixed(1)+' MB'; return (b/1073741824).toFixed(2)+' GB'; }

// ── API wrapper (no reload loop on 401) ──
let authWarned = false;
async function api(url, opts={}) {
  let r;
  try { r = await fetch(url, opts); }
  catch (err) { toast('Network error: ' + err.message, 'err'); return null; }
  if (r.status === 401) { if(!authWarned){ authWarned=true; toast('Session expired — refresh the page to sign in again', 'err'); } return null; }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : { text: await r.text() };
  if (!r.ok && data && typeof data === 'object') data.__error = data.detail || data.error || ('HTTP ' + r.status);
  return data;
}

// ── state ──
let projects = [];
let statuses = {};            // id -> { status, lastMs, count }
let view = 'fleet';
let activeId = null;
let detailTab = 'snapshots';

// ── theme ──
const savedTheme = localStorage.getItem('bm-theme');
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

// ── load + statuses ──
async function loadProjects() {
  const d = await api('/api/projects');
  if (!d) return;
  projects = d.projects || [];
  renderSidebar();
  if (view === 'fleet') renderFleet();
  loadAllStatuses();
}

async function loadAllStatuses() {
  await Promise.allSettled(projects.map(async p => {
    const d = await api(`/api/projects/${p.id}/snapshots`);
    if (d && d.snapshots && d.snapshots.length) {
      const last = new Date(d.snapshots[0].time).getTime();
      statuses[p.id] = { count:d.snapshots.length, lastMs:last, status:(Date.now()-last)/HOUR > 36 ? 'warn' : 'ok' };
    } else {
      statuses[p.id] = { count:0, lastMs:null, status:'idle' };
    }
  }));
  renderSidebar();
  if (view === 'fleet') renderFleet();
}

async function refreshAll(focusId) {
  await loadProjects();
  if (focusId) openDetail(focusId);
}

// ── sidebar ──
function renderSidebar() {
  const f = ($('#search').value || '').toLowerCase();
  const items = projects.filter(p => p.name.toLowerCase().includes(f));
  $('#projCount').textContent = `(${items.length})`;
  const list = $('#projectList'); list.innerHTML = '';
  items.forEach(p => {
    const st = (statuses[p.id] || {}).status || 'idle';
    const b = el(`<button class="proj ${view==='detail'&&activeId===p.id?'active':''}">
      <i class="dot ${st}"></i><span class="pname">${esc(p.name)}</span>
      ${p.ssh?'<span class="ssh" title="SSH tunnel">⚡</span>':''}${engBadge(p.db_engine)}</button>`);
    b.onclick = () => openDetail(p.id);
    list.appendChild(b);
  });
}

// ── fleet overview ──
function setFleetNav(on) { $('#fleetNav').classList.toggle('active', on); }

function renderFleet() {
  view = 'fleet'; activeId = null; setFleetNav(true);
  $('#crumb').textContent = 'Fleet Health';
  renderSidebar();
  const c = $('#content');
  if (!projects.length) { c.innerHTML = '<div class="empty">No projects yet. Click “Add Project” to get started.</div>'; return; }

  const ok   = projects.filter(p => (statuses[p.id]||{}).status === 'ok').length;
  const attn = projects.filter(p => ['warn'].includes((statuses[p.id]||{}).status)).length;
  const total = Object.values(statuses).reduce((a,s)=>a+(s.count||0), 0);
  const next = projects.filter(p=>p.next_run).sort((a,b)=>new Date(a.next_run)-new Date(b.next_run))[0];
  const checking = projects.some(p => !statuses[p.id]);

  c.innerHTML = '';
  c.appendChild(el(`<div class="tiles">
    <div class="tile accent"><div class="t-label"><i class="dot ok"></i>Healthy</div><div class="t-value">${ok}</div><div class="t-sub">of ${projects.length} projects</div></div>
    <div class="tile"><div class="t-label"><i class="dot warn"></i>Need attention</div><div class="t-value">${attn}</div><div class="t-sub">stale &gt; 36h</div></div>
    <div class="tile"><div class="t-label">Total snapshots</div><div class="t-value">${total.toLocaleString()}${checking?' <span class="spinner spin-dim"></span>':''}</div><div class="t-sub">across all repos</div></div>
    <div class="tile"><div class="t-label">Next backup</div><div class="t-value" style="font-size:18px;">${next?esc(next.name):'—'}</div><div class="t-sub">${next?nextFmt(next.next_run):'no schedules'}</div></div>
  </div>`));

  const panel = el(`<div class="panel">
    <div class="panel-head"><h2>Projects</h2><span class="sub">${projects.length} total · click a row for detail</span></div>
    <table><thead><tr><th>Project</th><th>Status</th><th>Last backup</th><th>Next run</th><th>Snapshots</th><th>Storage</th><th></th></tr></thead><tbody></tbody></table>
  </div>`);
  const tb = $('tbody', panel);
  projects.forEach(p => {
    const s = statuses[p.id];
    const st = s ? s.status : 'idle';
    const storage = p.storage_type === 'local' ? 'Local' : 'S3 · Contabo';
    const r = el(`<tr class="fleet-row">
      <td><div class="cell-name"><i class="dot ${st}"></i><div><div class="nm">${esc(p.name)} ${engBadge(p.db_engine)} ${p.ssh?'<span class="ssh" title="SSH">⚡</span>':''}</div><div class="meta">${esc(p.type)} · ${p.connection_type==='docker'?'docker exec':'connection string'}</div></div></div></td>
      <td>${s?`<span class="pill ${st}">${STATUS_LABEL[st]}</span>`:'<span class="muted spin-dim"><span class="spinner"></span></span>'}</td>
      <td>${s?ago(s.lastMs):'<span class="muted">…</span>'}</td>
      <td class="muted">${p.next_run?nextFmt(p.next_run):'<span class="muted">Manual</span>'}</td>
      <td><span class="mono">${s?s.count:'—'}</span></td>
      <td class="muted">${storage}</td>
      <td><div class="row-actions"><button class="btn btn-ghost btn-sm bk">Backup</button></div></td>
    </tr>`);
    r.onclick = (ev) => {
      const bk = ev.target.closest('.bk');
      if (bk) { ev.stopPropagation(); backupProject(p.id, bk); return; }
      openDetail(p.id);
    };
    tb.appendChild(r);
  });
  c.appendChild(panel);
}

// ── detail ──
async function openDetail(id) {
  view = 'detail'; activeId = id; detailTab = 'snapshots'; setFleetNav(false);
  renderSidebar();
  $('#content').innerHTML = '<div class="empty"><span class="spinner spin-dim"></span> Loading…</div>';
  const detail = await api(`/api/projects/${id}`);
  if (!detail || detail.__error) { $('#content').innerHTML = `<div class="empty">Could not load project: ${esc(detail&&detail.__error||'unknown')}</div>`; return; }
  renderDetail(detail, null);                       // shell first — snapshots still loading
  const snaps = await api(`/api/projects/${id}/snapshots`);
  if (view === 'detail' && activeId === id) renderDetail(detail, snaps);   // fill in (skip if navigated away)
}

function renderDetail(p, snaps) {
  const listed = projects.find(x => x.id === p.id) || {};
  const pending = snaps == null;
  const snapList = pending ? [] : (snaps.snapshots || []);
  const lastMs = snapList.length ? new Date(snapList[0].time).getTime() : null;
  const st = pending || !snapList.length ? 'idle' : (Date.now()-lastMs)/HOUR > 36 ? 'warn' : 'ok';
  const storage = p.storage_type === 'local' ? `Local · ${esc(p.local_repo_path||'/opt/backups/restic/'+p.restic_tag)}` : 'S3 · Contabo';

  $('#crumb').innerHTML = `<span class="crumb-dim" id="crumbHome">Fleet</span> / ${esc(p.name)}`;
  const c = $('#content'); c.innerHTML = '';
  $('#crumbHome') && ($('#crumbHome').onclick = renderFleet);

  c.appendChild(el(`<div class="detail-head">
    <div>
      <div class="detail-title"><i class="dot ${st}"></i><h1>${esc(p.name)}</h1>${pending?'<span class="pill idle"><span class="spinner"></span> checking</span>':`<span class="pill ${st}">${STATUS_LABEL[st]}</span>`}</div>
      <div class="detail-sub">${engBadge(p.db_engine)} <span>${esc(p.project_type)}</span>
        <span class="sep">·</span><span>${p.connection_type==='docker'?'docker exec':'connection string'}</span>
        ${p.ssh_host?'<span class="sep">·</span><span class="ssh">⚡ SSH tunnel</span>':''}
        <span class="sep">·</span><span class="mono">${storage}</span></div>
    </div>
    <div class="detail-actions">
      <button class="btn btn-ghost btn-sm" id="d_edit">Edit</button>
      <button class="btn btn-primary" id="d_backup">
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none"><path d="M8 2v8M4.5 6 8 2l3.5 4M2.5 14h11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>Back up now</button>
    </div>
  </div>`));

  c.appendChild(el(`<div class="stat-strip">
    <div class="stat-cell"><div class="s-label">Last backup</div><div class="s-value">${pending?'<span class="spinner spin-dim"></span>':ago(lastMs)}</div></div>
    <div class="stat-cell"><div class="s-label">Next run</div><div class="s-value">${listed.next_run?nextFmt(listed.next_run):'Manual'}</div></div>
    <div class="stat-cell"><div class="s-label">Snapshots</div><div class="s-value">${pending?'<span class="spinner spin-dim"></span>':snapList.length}</div></div>
    <div class="stat-cell"><div class="s-label">Schedule</div><div class="s-value" style="font-size:13.5px;">${esc(cronLabel(p.schedule_cron))}</div></div>
    <div class="stat-cell"><div class="s-label">Retention</div><div class="s-value" style="font-size:13.5px;">${esc(retentionLabel(p))}</div></div>
  </div>`));

  c.appendChild(buildHeatmap(snapList));

  const tabs = el(`<div>
    <div class="tabs">
      <button class="tab ${detailTab==='snapshots'?'active':''}" data-tab="snapshots">Snapshots</button>
      <button class="tab ${detailTab==='logs'?'active':''}" data-tab="logs">Logs</button>
      <button class="tab ${detailTab==='settings'?'active':''}" data-tab="settings">Settings</button>
    </div><div id="tabBody"></div></div>`);
  tabs.querySelectorAll('.tab').forEach(t => t.onclick = () => { detailTab = t.dataset.tab; renderDetail(p, snaps); });
  c.appendChild(tabs);

  if (detailTab === 'snapshots') renderSnapshots(p, snaps, $('#tabBody', tabs));
  else if (detailTab === 'logs') renderLogs(p, $('#tabBody', tabs));
  else renderSettings(p, $('#tabBody', tabs));

  $('#d_backup').onclick = (e) => backupProject(p.id, e.currentTarget);
  $('#d_edit').onclick = () => openProjectForm(p);
}

function buildHeatmap(snapList) {
  const days = new Set(snapList.map(s => new Date(s.time).toDateString()));
  const panel = el(`<div class="panel"><div class="panel-head"><h2>Backup history</h2><span class="sub">last 17 weeks</span></div>
    <div style="padding:16px 18px;"><div class="heatmap"></div>
    <div class="hm-legend">Less <span class="hm-cell"></span><span class="hm-cell l2"></span><span class="hm-cell l3"></span><span class="hm-cell l4"></span> More</div></div></div>`);
  const grid = $('.heatmap', panel);
  const today = new Date(); today.setHours(0,0,0,0);
  // 17 weeks back, week columns oldest→newest
  for (let w = 16; w >= 0; w--) {
    const col = el('<div class="hm-col"></div>');
    for (let d = 0; d < 7; d++) {
      const day = new Date(today); day.setDate(today.getDate() - (w*7 + (6-d)));
      const cls = days.has(day.toDateString()) ? 'l4' : '';
      col.appendChild(el(`<div class="hm-cell ${cls}" title="${day.toDateString()}"></div>`));
    }
    grid.appendChild(col);
  }
  return panel;
}

function renderSnapshots(p, snaps, host) {
  if (snaps == null) { host.appendChild(el('<div class="empty"><span class="spinner spin-dim"></span> Loading snapshots…</div>')); return; }
  if (snaps.__error) { host.appendChild(el(`<div class="empty">Could not list snapshots: ${esc(snaps.error||snaps.__error)}</div>`)); return; }
  const list = (snaps && snaps.snapshots) || [];
  if (!list.length) { host.appendChild(el('<div class="empty">No snapshots yet. Run a backup to create the first one.</div>')); return; }
  const panel = el(`<div class="panel"><table><thead><tr><th>Snapshot</th><th>Created</th><th>Tags</th><th>Contents</th><th></th></tr></thead><tbody></tbody></table></div>`);
  const tb = $('tbody', panel);
  list.forEach(s => {
    const paths = (s.paths||[]).map(pt => pt.toLowerCase().includes('upload') ? 'WP uploads' : (pt.endsWith('.sql') ? 'DB dump' : pt.split('/').pop())).join(' · ');
    const r = el(`<tr>
      <td><span class="snap-id">${esc(s.id)}</span></td>
      <td>${fmtDate(s.time)} <span class="muted">· ${ago(new Date(s.time).getTime())}</span></td>
      <td>${(s.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join(' ')}</td>
      <td class="path-chip">${esc(paths)||'—'}</td>
      <td><div class="row-actions"><button class="btn btn-ghost btn-sm dl">Download</button><button class="btn btn-danger btn-sm rs">Restore</button></div></td>
    </tr>`);
    $('.dl', r).onclick = (e) => downloadSnapshot(p.id, s.id, e.currentTarget);
    $('.rs', r).onclick = () => restoreSnapshot(p, s.id);
    tb.appendChild(r);
  });
  host.appendChild(panel);
}

async function renderLogs(p, host) {
  host.innerHTML = '<div class="empty"><span class="spinner spin-dim"></span> Loading logs…</div>';
  const d = await api(`/api/projects/${p.id}/logs?lines=200`);
  host.innerHTML = '';
  const text = (d && d.logs) || (d && d.__error) || 'No logs available.';
  const html = esc(text).split('\n').map(line =>
    /error|fail/i.test(line) ? `<span class="lerr">${line}</span>` :
    /complete|success|info|applied/i.test(line) ? `<span class="lok">${line}</span>` : line
  ).join('\n');
  host.appendChild(el(`<div class="logs">${html}</div>`));
}

function renderSettings(p, host) {
  const storage = p.storage_type === 'local' ? `Local · ${esc(p.local_repo_path||'/opt/backups/restic/'+p.restic_tag)}` : 'S3 · Contabo';
  const panel = el(`<div class="panel"><div style="padding:18px;font-size:13px;line-height:2;color:var(--text-mut);">
    <div><b style="color:var(--text)">Restic tag</b> · <span class="mono">${esc(p.restic_tag)}</span></div>
    <div><b style="color:var(--text)">Connection</b> · ${p.connection_type==='docker'?'docker exec into '+esc(p.docker_container||'?'):'connection string'}</div>
    ${p.ssh_host?`<div><b style="color:var(--text)">SSH</b> · ${esc(p.ssh_user||'')}@${esc(p.ssh_host)}:${esc(p.ssh_port||22)}</div>`:''}
    <div><b style="color:var(--text)">Storage</b> · <span class="mono">${storage}</span></div>
    <div><b style="color:var(--text)">Schedule</b> · ${esc(cronLabel(p.schedule_cron))} ${p.schedule_cron?`<span class="mono">(${esc(p.schedule_cron)})</span>`:''}</div>
    <div><b style="color:var(--text)">Retention</b> · ${esc(retentionLabel(p))}</div>
    <div style="margin-top:16px;display:flex;gap:9px;"><button class="btn btn-ghost btn-sm" id="s_edit">Edit settings</button><button class="btn btn-danger btn-sm" id="s_del">Delete project</button></div>
  </div></div>`);
  $('#s_edit', panel).onclick = () => openProjectForm(p);
  $('#s_del', panel).onclick = () => deleteProject(p.id, p.name);
  host.appendChild(panel);
}

// ── actions ──
async function backupProject(id, btn) {
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Backing up…'; }
  const d = await api(`/api/projects/${id}/backup`, { method:'POST' });
  if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  if (d && d.success) toast('Backup completed', 'ok');
  else toast((d && (d.__error || d.output)) || 'Backup failed', 'err');
  await loadProjects();
  if (view === 'detail' && activeId === id) openDetail(id);
}

async function downloadSnapshot(projectId, snapId, btn) {
  const orig = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Preparing…';
  const d = await api(`/api/projects/${projectId}/snapshots/${snapId}/prepare-download`, { method:'POST' });
  btn.disabled = false; btn.innerHTML = orig;
  if (d && d.ready) {
    toast(`Archive ready (${fmtSize(d.size)}) — downloading…`, 'ok');
    window.location.href = `/api/projects/${projectId}/snapshots/${snapId}/download`;
    setTimeout(() => api(`/api/downloads/${snapId}`, { method:'DELETE' }), 60000);
  } else toast((d && (d.__error||d.error)) || 'Failed to prepare download', 'err');
}

async function restoreSnapshot(p, snapId) {
  // Restore dialog: pick scope + type the project name to enable the button (AC9).
  const choice = await restoreDialog(p, snapId);
  if (!choice) return;                                    // cancelled
  toast('Restoring…', 'idle');
  const d = await api(`/api/projects/${p.id}/snapshots/${snapId}/restore`, {
    method:'POST',
    headers:{ 'Content-Type':'application/json' },
    body: JSON.stringify({ scope: choice.scope }),        // 'all' | 'db' | 'files'
  });
  if (d && d.success) toast('Restore complete: ' + (d.output||'').split('\n')[0], 'ok');
  else toast((d && (d.__error||d.error)) || 'Restore failed', 'err');
}

async function deleteProject(id, name) {
  const okGo = await confirmDialog('Delete project',
    `Delete <b>${esc(name)}</b>? It is removed from the manager but existing snapshots in the repo are <b>not</b> deleted.`,
    'Delete project');
  if (!okGo) return;
  const d = await api(`/api/projects/${id}`, { method:'DELETE' });
  if (d && d.deleted) { toast('Project deleted', 'ok'); delete statuses[id]; await loadProjects(); renderFleet(); }
  else toast('Failed to delete project', 'err');
}

// ── confirm modal ──
function confirmDialog(title, bodyHtml, okLabel) {
  return new Promise(res => {
    $('#confirmTitle').textContent = title;
    $('#confirmBody').innerHTML = bodyHtml;
    $('#confirmOk').textContent = okLabel;
    $('#confirmWrap').classList.add('show');
    const close = v => { $('#confirmWrap').classList.remove('show'); $('#confirmOk').onclick=null; $('#confirmCancel').onclick=null; res(v); };
    $('#confirmOk').onclick = () => close(true);
    $('#confirmCancel').onclick = () => close(false);
  });
}

// ── restore modal (scope + typed confirm, BKP-1.6 / AC9) ──
// Input : project dict (needs .name) + snapshot id (display only).
// Output: resolves { scope:'all'|'db'|'files' } on Restore, or null on cancel.
// The Restore button stays disabled until the typed text exactly equals the
// project name; scope and the typed value reset every time the dialog opens.
function restoreDialog(project, snapId) {
  return new Promise(res => {
    const wrap = $('#restoreWrap'), ok = $('#restoreOk'), input = $('#restoreConfirmInput');
    $('#restoreBody').innerHTML =
      `Restore <b>${esc(snapId)}</b> into <b>${esc(project.name)}</b>.`;
    $('#restoreName').textContent = project.name;

    // Reset state on open: default scope, empty input, disabled button.
    const allRadio = wrap.querySelector('input[name="restoreScope"][value="all"]');
    if (allRadio) allRadio.checked = true;
    input.value = '';
    ok.disabled = true;

    const sync = () => { ok.disabled = input.value !== project.name; };
    const close = result => {
      wrap.classList.remove('show');
      input.oninput = null; ok.onclick = null; $('#restoreCancel').onclick = null;
      input.value = ''; ok.disabled = true;                 // reset on close too
      res(result);
    };
    input.oninput = sync;
    ok.onclick = () => {
      if (ok.disabled) return;                              // guard (AC9)
      const scope = (wrap.querySelector('input[name="restoreScope"]:checked') || {}).value || 'all';
      close({ scope });
    };
    $('#restoreCancel').onclick = () => close(null);

    wrap.classList.add('show');
    input.focus();
  });
}

// ── toast ──
let toastT;
function toast(msg, status='ok') {
  const t = $('#toast'); t.innerHTML = `<i class="dot ${status}"></i>${esc(msg)}`; t.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 3200);
}

// ── wire shell ──
$('#search').addEventListener('input', renderSidebar);
$('#fleetNav').addEventListener('click', renderFleet);
$('#addBtn').addEventListener('click', () => openProjectForm(null));
$('#overlay').addEventListener('click', closeProjectForm);
$('#themeToggle').addEventListener('click', () => {
  const h = document.documentElement;
  h.dataset.theme = h.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('bm-theme', h.dataset.theme);
});

loadProjects();
