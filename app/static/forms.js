/* ============================================================
   forms.js — add / edit project slide-over + shared label maps.
   Globals used from app.js at runtime: api, toast, esc, refreshAll.
   ============================================================ */

const RETENTION_STRATEGIES = {
  '7d':        { keep_daily:7,  keep_weekly:0, keep_monthly:0,  keep_yearly:0 },
  '14d':       { keep_daily:14, keep_weekly:0, keep_monthly:0,  keep_yearly:0 },
  '30d':       { keep_daily:30, keep_weekly:0, keep_monthly:0,  keep_yearly:0 },
  '30d_4w':    { keep_daily:30, keep_weekly:4, keep_monthly:0,  keep_yearly:0 },
  '90d_6m':    { keep_daily:90, keep_weekly:0, keep_monthly:6,  keep_yearly:0 },
  '90d_6m_1y': { keep_daily:90, keep_weekly:0, keep_monthly:6,  keep_yearly:1 },
  '30d_12m_3y':{ keep_daily:30, keep_weekly:0, keep_monthly:12, keep_yearly:3 },
};
const RETENTION_LABELS = {
  '7d':'7 days', '14d':'14 days', '30d':'30 days', '30d_4w':'30 days + 4 weeks',
  '90d_6m':'90 days + 6 months', '90d_6m_1y':'90 days + 6 months + 1 year',
  '30d_12m_3y':'30 days + 12 months + 3 years',
};
const SCHEDULE_PRESETS = {
  '':'Manual (no schedule)', '0 2 * * *':'Daily at 2:00 AM', '0 0 * * *':'Daily at midnight',
  '0 2 * * 0':'Weekly — Sunday 2:00 AM', '0 2 1 * *':'Monthly — 1st at 2:00 AM', '0 * * * *':'Every hour',
};

function retentionKey(p) {
  for (const [k, v] of Object.entries(RETENTION_STRATEGIES))
    if (v.keep_daily==(p.keep_daily??7) && v.keep_weekly==(p.keep_weekly??0)
        && v.keep_monthly==(p.keep_monthly??0) && v.keep_yearly==(p.keep_yearly??0)) return k;
  return '90d_6m_1y';
}
function retentionLabel(p) { return RETENTION_LABELS[retentionKey(p)] || 'Custom'; }
function cronLabel(cron) { return !cron ? 'Manual' : (SCHEDULE_PRESETS[cron] || ('Custom · ' + cron)); }

let editingId = null;

function openProjectForm(p) {
  editingId = p ? p.id : null;
  const e = p || {};
  const so = document.getElementById('slideover');
  so.innerHTML = formHTML(e);
  so.classList.add('show');
  document.getElementById('overlay').classList.add('show');
  wireForm();
  updateFormVisibility();
}
function closeProjectForm() {
  document.getElementById('slideover').classList.remove('show');
  document.getElementById('overlay').classList.remove('show');
  editingId = null;
}

function opt(map, sel) {
  return Object.entries(map).map(([v, l]) =>
    `<option value="${esc(v)}" ${v===sel?'selected':''}>${esc(l)}</option>`).join('');
}

function formHTML(e) {
  const conn = e.connection_type || 'connection_string';
  const store = e.storage_type || 's3';
  const sshOn = !!e.ssh_host;
  const retSel = retentionKey(e);
  const cron = e.schedule_cron || '';
  const presetSel = SCHEDULE_PRESETS[cron] !== undefined ? cron : 'custom';
  return `
  <div class="so-head">
    <h3>${editingId ? 'Edit project' : 'Add project'}</h3>
    <button class="icon-btn" onclick="closeProjectForm()" aria-label="Close">
      <svg viewBox="0 0 16 16" width="15" height="15" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>
  </div>
  <div class="so-body">
    <div class="fsection"><div class="fsection-t">Basic info</div>
      <div class="fgrid">
        <div class="fgroup full"><label class="flabel">Name</label><input id="f_name" class="finput" value="${esc(e.name||'')}" placeholder="My Database"></div>
        <div class="fgroup"><label class="flabel">DB engine</label><select id="f_engine" class="fselect">
          <option value="postgres" ${e.db_engine==='postgres'?'selected':''}>PostgreSQL</option>
          <option value="mariadb" ${e.db_engine==='mariadb'?'selected':''}>MariaDB</option>
          <option value="mysql" ${e.db_engine==='mysql'?'selected':''}>MySQL</option></select></div>
        <div class="fgroup"><label class="flabel">Type</label><select id="f_type" class="fselect">
          <option value="database" ${e.project_type!=='wordpress'?'selected':''}>Database</option>
          <option value="wordpress" ${e.project_type==='wordpress'?'selected':''}>WordPress</option></select></div>
      </div>
    </div>

    <div class="fsection"><div class="fsection-t">Connection</div>
      <div class="seg" style="margin-bottom:13px;">
        <label><input type="radio" name="f_conn" value="connection_string" ${conn!=='docker'?'checked':''}><span>Connection string</span></label>
        <label><input type="radio" name="f_conn" value="docker" ${conn==='docker'?'checked':''}><span>Docker (local)</span></label>
      </div>
      <div id="sec_connstr">
        <div class="fgroup full"><label class="flabel">Connection string</label>
          <input id="f_connstr" class="finput mono" value="${esc(e.connection_string||'')}" placeholder="postgresql://user:pass@host:5432/db">
          <span class="fhint">Use the DB host as seen from the SSH server when tunnelling.</span></div>
      </div>
      <div id="sec_docker" class="fgrid">
        <div class="fgroup full"><label class="flabel">Container name / ID</label><input id="f_container" class="finput" value="${esc(e.docker_container||'')}" placeholder="my_db_container"></div>
        <div class="fgroup"><label class="flabel">DB user</label><input id="f_user" class="finput" value="${esc(e.db_user||'')}" placeholder="postgres"></div>
        <div class="fgroup"><label class="flabel">DB password</label><input id="f_pass" class="finput" type="password" placeholder="••••••••"><span class="fhint" id="passhint" style="display:none;">Leave blank to keep existing.</span></div>
        <div class="fgroup full"><label class="flabel">DB name</label><input id="f_dbname" class="finput" value="${esc(e.db_name||'')}" placeholder="mydb"></div>
      </div>
    </div>

    <div class="fsection" id="sec_ssh"><div class="fsection-t">SSH tunnel <span style="text-transform:none;color:var(--text-dim);font-weight:400;">(optional)</span></div>
      <label class="toggle-row" style="margin-bottom:12px;"><input type="checkbox" id="f_ssh" ${sshOn?'checked':''}> Enable SSH tunnel to a remote VPS</label>
      <div id="ssh_fields" class="fgrid">
        <div class="fgroup"><label class="flabel">SSH host</label><input id="f_sshhost" class="finput" value="${esc(e.ssh_host||'')}" placeholder="vps.example.com"></div>
        <div class="fgroup"><label class="flabel">SSH port</label><input id="f_sshport" class="finput" type="number" value="${esc(e.ssh_port||22)}"></div>
        <div class="fgroup"><label class="flabel">SSH user</label><input id="f_sshuser" class="finput" value="${esc(e.ssh_user||'')}" placeholder="ubuntu"></div>
        <div class="fgroup"><label class="flabel">SSH key path</label><input id="f_sshkey" class="finput" value="${esc(e.ssh_key||'')}" placeholder="/opt/backups/keys/remote.pem"></div>
      </div>
    </div>

    <div class="fsection"><div class="fsection-t">Storage backend</div>
      <div class="seg">
        <label><input type="radio" name="f_store" value="s3" ${store!=='local'?'checked':''}><span>S3 / Contabo</span></label>
        <label><input type="radio" name="f_store" value="local" ${store==='local'?'checked':''}><span>Local VPS</span></label>
      </div>
      <div id="sec_local" class="fgroup full" style="margin-top:12px;"><label class="flabel">Repository path <span style="color:var(--text-dim)">(optional)</span></label>
        <input id="f_localpath" class="finput mono" value="${esc(e.local_repo_path||'')}" placeholder="/opt/backups/restic/my-db">
        <span class="fhint">Defaults to /opt/backups/restic/{tag}. Created on first backup.</span></div>
    </div>

    <div class="fsection"><div class="fsection-t">Retention &amp; schedule</div>
      <div class="fgrid">
        <div class="fgroup full"><label class="flabel">Retention strategy</label><select id="f_retention" class="fselect">${opt(RETENTION_LABELS, retSel)}</select></div>
        <div class="fgroup full"><label class="flabel">Schedule</label><select id="f_schedule" class="fselect">${opt(SCHEDULE_PRESETS, presetSel==='custom'?'':cron)}<option value="custom" ${presetSel==='custom'?'selected':''}>Custom cron…</option></select></div>
        <div class="fgroup full" id="sec_cron"><label class="flabel">Cron expression</label><input id="f_cron" class="finput mono" value="${esc(cron)}" placeholder="0 2 * * *"><span class="fhint">minute hour day month weekday · times in UTC</span></div>
      </div>
    </div>

    <div class="fsection"><div class="fsection-t">Advanced</div>
      <div class="fgrid">
        <div class="fgroup full"><label class="flabel">Restic tag</label><input id="f_tag" class="finput mono" value="${esc(e.restic_tag||'')}" placeholder="my-db"><span class="fhint">Auto-filled from name. Filters snapshots in restic.</span></div>
        <div class="fgroup full" id="sec_wp"><label class="flabel">WordPress volume resource ID</label><input id="f_wp" class="finput mono" value="${esc(e.wp_resource_id||'')}" placeholder="docker_volume_resource_id"></div>
        <div class="fgroup full"><label class="flabel">Backup script <span style="color:var(--text-dim)">(optional, overrides built-in)</span></label><input id="f_script" class="finput mono" value="${esc(e.backup_script||'')}" placeholder="/opt/backups/backup.sh"></div>
        <div class="fgroup full"><label class="flabel">Log file <span style="color:var(--text-dim)">(optional)</span></label><input id="f_logfile" class="finput mono" value="${esc(e.log_file||'')}" placeholder="/opt/backups/my-db.log"></div>
      </div>
    </div>
  </div>
  <div class="so-foot">
    <button class="btn btn-ghost" onclick="closeProjectForm()">Cancel</button>
    <button class="btn btn-primary" id="f_save">${editingId ? 'Save changes' : 'Add project'}</button>
  </div>`;
}

function wireForm() {
  const byId = id => document.getElementById(id);
  if (editingId) byId('passhint').style.display = 'block';
  byId('f_name').addEventListener('input', () => {
    if (editingId) return;
    byId('f_tag').value = byId('f_name').value.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'');
  });
  document.querySelectorAll('input[name="f_conn"]').forEach(r => r.addEventListener('change', updateFormVisibility));
  document.querySelectorAll('input[name="f_store"]').forEach(r => r.addEventListener('change', updateFormVisibility));
  byId('f_ssh').addEventListener('change', updateFormVisibility);
  byId('f_type').addEventListener('change', updateFormVisibility);
  byId('f_schedule').addEventListener('change', updateFormVisibility);
  byId('f_save').addEventListener('click', saveProjectForm);
}

function updateFormVisibility() {
  const v = n => document.querySelector(`input[name="${n}"]:checked`).value;
  const show = (id, on) => document.getElementById(id).style.display = on ? '' : 'none';
  const conn = v('f_conn');
  show('sec_connstr', conn === 'connection_string');
  show('sec_docker', conn === 'docker');
  show('sec_ssh', conn === 'connection_string');
  show('ssh_fields', document.getElementById('f_ssh').checked);
  show('sec_local', v('f_store') === 'local');
  show('sec_wp', document.getElementById('f_type').value === 'wordpress');
  show('sec_cron', document.getElementById('f_schedule').value === 'custom');
}

async function saveProjectForm() {
  const byId = id => document.getElementById(id);
  const conn = document.querySelector('input[name="f_conn"]:checked').value;
  const sshOn = byId('f_ssh').checked;
  const ret = RETENTION_STRATEGIES[byId('f_retention').value] || RETENTION_STRATEGIES['7d'];
  const sched = byId('f_schedule').value;
  const cron = sched === 'custom' ? byId('f_cron').value.trim() : sched;

  const body = {
    name: byId('f_name').value.trim(),
    db_engine: byId('f_engine').value,
    project_type: byId('f_type').value,
    connection_type: conn,
    connection_string: conn === 'connection_string' ? byId('f_connstr').value.trim() : null,
    docker_container: conn === 'docker' ? byId('f_container').value.trim() : null,
    db_user: conn === 'docker' ? byId('f_user').value.trim() : null,
    db_pass: conn === 'docker' ? byId('f_pass').value : null,
    db_name: conn === 'docker' ? byId('f_dbname').value.trim() : null,
    ssh_host: sshOn ? byId('f_sshhost').value.trim() : null,
    ssh_port: sshOn ? (parseInt(byId('f_sshport').value) || 22) : null,
    ssh_user: sshOn ? byId('f_sshuser').value.trim() : null,
    ssh_key: sshOn ? byId('f_sshkey').value.trim() : null,
    keep_daily: ret.keep_daily, keep_weekly: ret.keep_weekly,
    keep_monthly: ret.keep_monthly, keep_yearly: ret.keep_yearly,
    storage_type: document.querySelector('input[name="f_store"]:checked').value,
    local_repo_path: byId('f_localpath').value.trim() || null,
    restic_tag: byId('f_tag').value.trim(),
    backup_script: byId('f_script').value.trim() || null,
    log_file: byId('f_logfile').value.trim() || null,
    wp_resource_id: byId('f_wp').value.trim() || null,
    schedule_cron: cron || '',
  };

  if (!body.name) return toast('Name is required', 'err');
  if (!body.restic_tag) return toast('Restic tag is required', 'err');

  const btn = byId('f_save');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Saving…';
  const data = editingId
    ? await api(`/api/projects/${editingId}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) })
    : await api('/api/projects', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });

  if (data && data.id) {
    toast(editingId ? 'Project updated' : 'Project added', 'ok');
    closeProjectForm();
    refreshAll(data.id);
  } else {
    btn.disabled = false; btn.textContent = editingId ? 'Save changes' : 'Add project';
    toast((data && (data.__error)) || 'Failed to save project', 'err');
  }
}
