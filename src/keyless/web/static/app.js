// Keyless Unified Local Web UI (zero-build ES module)

const state = {
  view: 'home',
  status: { state: 'stopped', detail: '' },
  gaze: null,
  recall: null,
  sessions: [],
  selectedSessionId: null,
  sessionDetail: null,
  selectedEventIdx: 0,
  eventFilter: 'all',
  history: [],
  historyQuery: '',
  commands: [],
  commandQuery: '',
  config: {},
  schema: [],
  doctor: null,
  models: null,
  lastError: null,
};

const VIEW_META = {
  home: ['Home', 'Hands-free gaze and voice control'],
  gaze: ['Gaze', 'Monocular eye tracking, passive click adaptation, and live preview'],
  sessions: ['Sessions', 'Inspect recorded clicks, verify >95% candidate recall, and curate splits'],
  activity: ['Activity', 'Recent voice commands and browser actions'],
  settings: ['Settings', 'Configuration, permissions, models, and diagnostics'],
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    ...opts,
  });
  let body = {};
  try {
    body = await res.json();
  } catch {
    body = {};
  }
  if (!res.ok) {
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.payload = body;
    err.status = res.status;
    throw err;
  }
  return body;
}

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function showError(title, message, { command = '', showGrant = false } = {}) {
  state.lastError = { title, message, command };
  const box = document.getElementById('error-callout');
  document.getElementById('error-title').textContent = title;
  document.getElementById('error-body').textContent = message;
  const grantBtn = document.getElementById('error-grant-btn');
  grantBtn.hidden = !showGrant;
  const cmdWrap = document.getElementById('error-cmd-wrap');
  if (command) {
    document.getElementById('error-cmd-text').textContent = command;
    cmdWrap.hidden = false;
  } else {
    cmdWrap.hidden = true;
  }
  box.hidden = false;
}

function clearError() {
  state.lastError = null;
  document.getElementById('error-callout').hidden = true;
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const prev = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = prev; }, 1400);
    }
  } catch {
    showError('Clipboard unavailable', text);
  }
}

function applyTheme(dark) {
  document.documentElement.classList.toggle('dark', dark);
  localStorage.setItem('keyless-theme', dark ? 'dark' : 'light');
}

function initThemeAndSidebar() {
  const saved = localStorage.getItem('keyless-theme');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(saved ? saved === 'dark' : prefersDark);

  document.getElementById('theme-toggle').addEventListener('click', () => {
    applyTheme(!document.documentElement.classList.contains('dark'));
    if (state.view === 'gaze') drawGazeCanvas();
    if (state.view === 'sessions') drawSessionCanvas();
  });

  const savedSidebar = localStorage.getItem('keyless-sidebar');
  if (savedSidebar === 'closed') {
    document.documentElement.setAttribute('data-sidebar', 'closed');
  }
  document.getElementById('sidebar-toggle').addEventListener('click', () => {
    const closed = document.documentElement.getAttribute('data-sidebar') === 'closed';
    document.documentElement.setAttribute('data-sidebar', closed ? 'open' : 'closed');
    localStorage.setItem('keyless-sidebar', closed ? 'open' : 'closed');
  });
}

function updateChrome() {
  const st = state.status.state || 'stopped';
  const dot = document.getElementById('sidebar-dot');
  dot.className = 'status-dot ' + (st === 'listening' ? 'ok' : st === 'error' ? 'err' : st === 'paused' ? 'warn' : '');
  document.getElementById('sidebar-state').textContent = st.charAt(0).toUpperCase() + st.slice(1);

  const primaryBtn = document.getElementById('header-primary-btn');
  primaryBtn.textContent = st === 'listening' ? 'Pause' : st === 'paused' ? 'Resume' : 'Start listening';

  const recBtn = document.getElementById('header-record-btn');
  const recording = Boolean(state.gaze && state.gaze.recording);
  recBtn.textContent = recording ? 'Stop recording' : 'Record session';
  recBtn.classList.toggle('btn-danger', recording);

  const badge = document.getElementById('header-recall');
  if (state.recall && state.recall.overall_recall != null) {
    const pct = (state.recall.overall_recall * 100).toFixed(1);
    badge.textContent = `Recall ${pct}% (${state.recall.hits}/${state.recall.clicks})`;
    badge.className = 'badge ' + (state.recall.gate_met ? 'ok' : 'warn');
  } else {
    badge.textContent = 'Recall: no data';
    badge.className = 'badge';
  }
}

async function refreshCore() {
  const [status, gaze, recall] = await Promise.all([
    api('/v1/status').catch(() => state.status),
    api('/v1/gaze/status').catch(() => state.gaze),
    api('/v1/sessions/recall').catch(() => state.recall),
  ]);
  state.status = status;
  state.gaze = gaze;
  state.recall = recall;
  updateChrome();
}

async function setView(view) {
  state.view = view;
  document.querySelectorAll('.nav-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  const [title, sub] = VIEW_META[view] || ['Keyless', ''];
  document.getElementById('view-title').textContent = title;
  document.getElementById('view-sub').textContent = sub;

  if (view === 'home') {
    const [hist, cmds] = await Promise.all([
      api('/v1/history?limit=10').catch(() => ({ actions: [] })),
      state.commands.length ? Promise.resolve({ categories: state.commands }) : api('/v1/commands').catch(() => ({ categories: [] })),
    ]);
    state.history = hist.actions || [];
    state.commands = cmds.categories || [];
  } else if (view === 'sessions') {
    await loadSessionsList();
  } else if (view === 'activity') {
    const hist = await api('/v1/history?limit=100').catch(() => ({ actions: [] }));
    state.history = hist.actions || [];
  } else if (view === 'settings') {
    const [cfg, schema, doc, mod] = await Promise.all([
      api('/v1/config').catch(() => ({})),
      api('/v1/config/schema').catch(() => ({ settings: [] })),
      api('/v1/doctor').catch(() => null),
      api('/v1/models').catch(() => null),
    ]);
    state.config = cfg;
    state.schema = schema.settings || [];
    state.doctor = doc;
    state.models = mod ? mod.model : null;
  }
  renderView();
}

function renderView() {
  const root = document.getElementById('view-root');
  if (state.view === 'home') renderHome(root);
  else if (state.view === 'gaze') renderGaze(root);
  else if (state.view === 'sessions') renderSessions(root);
  else if (state.view === 'activity') renderActivity(root);
  else if (state.view === 'settings') renderSettings(root);
}

// ---- HOME VIEW ---------------------------------------------------------

function renderHome(root) {
  const st = state.status.state || 'stopped';
  const headline =
    st === 'listening' ? 'Listening for commands.' :
    st === 'paused' ? 'Keyless is paused.' :
    st === 'error' ? 'Engine needs attention.' :
    'Ready when you are.';
  const sub =
    st === 'listening' ? 'Speak to click, scroll, dictate, or drive the browser.' :
    st === 'error' ? (state.status.detail || 'Open Settings to inspect diagnostics.') :
    'Start listening for voice control, or open Gaze to run live monocular eye tracking.';

  const gazeRunning = Boolean(state.gaze && state.gaze.running);
  const adapted = Boolean(state.gaze && state.gaze.stats && state.gaze.stats.using_ridge);
  const samples = state.gaze && state.gaze.stats ? state.gaze.stats.samples : 0;
  const recallTxt = state.recall && state.recall.overall_recall != null
    ? `${(state.recall.overall_recall * 100).toFixed(1)}% (${state.recall.hits}/${state.recall.clicks})`
    : 'No clicks recorded';

  const recentRows = (state.history || []).slice(0, 5).map((item) => `
    <tr>
      <td>${esc(item.transcript || '')}</td>
      <td class="muted">${esc(item.route || '')}</td>
      <td>${esc(item.target || item.result || '')}</td>
    </tr>
  `).join('');

  const q = state.commandQuery.trim().toLowerCase();
  const filteredCats = (state.commands || []).map((cat) => ({
    ...cat,
    commands: (cat.commands || []).filter((c) =>
      !q || `${c.phrase} ${c.description} ${c.example}`.toLowerCase().includes(q)
    ),
  })).filter((c) => c.commands.length > 0);

  const cmdHtml = filteredCats.map((cat) => `
    <div style="margin-bottom:12px;">
      <div class="eyebrow" style="margin-bottom:6px;">${esc(cat.title)}</div>
      ${cat.commands.map((c) => `
        <div class="field-row" style="padding:6px 0;">
          <div class="field-label">
            <strong style="font-family:var(--or-font-mono);font-size:0.8rem;">${esc(c.phrase)}</strong>
            <small>${esc(c.description)}</small>
          </div>
          <span class="telemetry">${esc(c.example || '')}</span>
        </div>
      `).join('')}
    </div>
  `).join('');

  root.innerHTML = `
    <section class="hero-block">
      <h1>${esc(headline)}</h1>
      <p>${esc(sub)}</p>
      <div class="hero-actions">
        <button id="home-primary" class="btn btn-primary" type="button">
          ${st === 'listening' ? 'Pause listening' : st === 'paused' ? 'Resume listening' : 'Start listening'}
        </button>
        <button id="home-open-gaze" class="btn" type="button">
          ${gazeRunning ? 'Inspect live gaze' : 'Start monocular gaze'}
        </button>
      </div>
    </section>

    <div class="grid-3">
      <div class="card metric-tile">
        <span class="eyebrow">Monocular gaze</span>
        <span class="metric-value">${gazeRunning ? (adapted ? 'Adapted' : 'Cold-start') : 'Idle'}</span>
        <span class="telemetry">${gazeRunning ? `${samples} passive clicks` : 'Not running'}</span>
      </div>
      <div class="card metric-tile">
        <span class="eyebrow">Phase 1 recall gate</span>
        <span class="metric-value">${esc(recallTxt)}</span>
        <span class="telemetry">${state.recall && state.recall.gate_met ? 'Gate met (>95%)' : 'Target: >95% real clicks'}</span>
      </div>
      <div class="card metric-tile">
        <span class="eyebrow">Display topology</span>
        <span class="metric-value">${state.gaze ? `${state.gaze.display.width} x ${state.gaze.display.height}` : '--'}</span>
        <span class="telemetry">Loopback control plane</span>
      </div>
    </div>

    ${recentRows ? `
      <section class="card">
        <div class="row-between">
          <span class="eyebrow">Recent activity</span>
          <button id="home-see-activity" class="btn btn-sm" type="button">View all</button>
        </div>
        <table>
          <thead><tr><th>Transcript</th><th>Route</th><th>Outcome</th></tr></thead>
          <tbody>${recentRows}</tbody>
        </table>
      </section>
    ` : ''}

    <section class="card">
      <div class="row-between">
        <span class="eyebrow">Voice commands</span>
        <input id="home-cmd-search" type="text" placeholder="Filter commands..." value="${esc(state.commandQuery)}">
      </div>
      <div>${cmdHtml || '<p class="muted">No matching commands.</p>'}</div>
    </section>
  `;

  document.getElementById('home-primary').addEventListener('click', toggleListening);
  document.getElementById('home-open-gaze').addEventListener('click', () => setView('gaze'));
  const seeAll = document.getElementById('home-see-activity');
  if (seeAll) seeAll.addEventListener('click', () => setView('activity'));
  document.getElementById('home-cmd-search').addEventListener('input', (e) => {
    state.commandQuery = e.target.value;
    renderHome(root);
    const inp = document.getElementById('home-cmd-search');
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  });
}

// ---- GAZE VIEW (Monocular Tracking + Adaptation) -----------------------

function renderGaze(root) {
  const g = state.gaze || {};
  const running = Boolean(g.running);
  const recording = Boolean(g.recording);
  const env = g.environment || {};
  const stats = g.stats || {};
  const pred = g.prediction || null;

  root.innerHTML = `
    <div class="grid-2" style="grid-template-columns: minmax(0, 1.7fr) minmax(280px, 1fr);">
      <section class="card">
        <div class="row-between">
          <div>
            <div class="eyebrow">Live monocular gaze preview (display-only)</div>
            <div id="gaze-coords" class="telemetry" style="margin-top:4px;">
              ${pred && pred.face_detected ? `x=${Math.round(pred.x)}, y=${Math.round(pred.y)} (${pred.using_ridge ? 'ridge' : 'cold-start prior'})` : running ? 'Waiting for face detection' : 'Tracker stopped'}
            </div>
          </div>
          <div class="row-gap">
            <button id="gaze-toggle-btn" class="btn btn-primary" type="button">
              ${running ? 'Stop tracking' : 'Start tracking'}
            </button>
          </div>
        </div>
        <div class="canvas-wrap">
          <canvas id="gaze-canvas"></canvas>
        </div>
        <div class="row-between">
          <span class="telemetry" id="gaze-fps-info">
            Frames: ${stats.frames_read || 0} / Faces: ${stats.faces_detected || 0} / Clicks adapted: ${stats.clicks_adapted || 0}
          </span>
          <span class="telemetry">Pointer is never moved implicitly</span>
        </div>
      </section>

      <div style="display:flex;flex-direction:column;gap:16px;">
        <section class="card">
          <span class="eyebrow">Passive click adaptation</span>
          <p class="muted" style="margin:0;">
            Zero calibration grid. Natural desktop clicks adapt a reversible ridge layer after the minimum click threshold.
          </p>
          <div class="field-row">
            <div class="field-label">
              <strong>Min clicks for ridge</strong>
              <small>Prior blends into ridge regression</small>
            </div>
            <input id="gaze-min-clicks" type="number" min="2" max="50" value="5" style="width:78px;">
          </div>
          <div class="field-row">
            <div class="field-label">
              <strong>Ridge alpha (L2)</strong>
              <small>Higher values dampen noisy clicks</small>
            </div>
            <input id="gaze-ridge-alpha" type="number" step="0.005" min="0.0001" max="10" value="0.01" style="width:92px;">
          </div>
          <div class="row-gap">
            <button id="gaze-undo-btn" class="btn btn-sm" type="button" ${!running ? 'disabled' : ''}>Undo last click</button>
            <button id="gaze-reset-btn" class="btn btn-sm" type="button" ${!running ? 'disabled' : ''}>Reset adaptation</button>
          </div>
        </section>

        <section class="card">
          <span class="eyebrow">Session recording</span>
          <p class="muted" style="margin:0;">
            Record real clicks paired with screen candidates and monocular 10D features to verify the Phase 1 >95% recall gate.
          </p>
          <div class="row-between">
            <span class="telemetry">Input permission: ${env.input_group_active ? 'active' : 'locked'}</span>
            ${!env.input_group_active && env.pkexec_available ? '<button id="gaze-grant-btn" class="btn btn-sm" type="button">Grant input access</button>' : ''}
          </div>
          <div class="row-gap">
            <button id="gaze-rec-btn" class="btn ${recording ? 'btn-danger' : ''}" type="button">
              ${recording ? 'Stop recording' : 'Record session'}
            </button>
            <button id="gaze-open-sessions" class="btn" type="button">Review sessions</button>
          </div>
        </section>
      </div>
    </div>
  `;

  document.getElementById('gaze-toggle-btn').addEventListener('click', async () => {
    try {
      clearError();
      if (state.gaze && state.gaze.running) {
        state.gaze = await api('/v1/gaze/stop', { method: 'POST', body: '{}' });
      } else {
        const minClicks = Number(document.getElementById('gaze-min-clicks').value) || 5;
        const ridgeAlpha = Number(document.getElementById('gaze-ridge-alpha').value) || 0.01;
        state.gaze = await api('/v1/gaze/start', {
          method: 'POST',
          body: JSON.stringify({ min_clicks: minClicks, ridge_alpha: ridgeAlpha, adapt_clicks: true }),
        });
      }
      updateChrome();
      renderGaze(root);
    } catch (err) {
      showError('Monocular gaze error', err.message);
    }
  });

  document.getElementById('gaze-undo-btn').addEventListener('click', async () => {
    state.gaze = await api('/v1/gaze/undo', { method: 'POST', body: '{}' });
    renderGaze(root);
  });

  document.getElementById('gaze-reset-btn').addEventListener('click', async () => {
    state.gaze = await api('/v1/gaze/reset', { method: 'POST', body: '{}' });
    renderGaze(root);
  });

  document.getElementById('gaze-rec-btn').addEventListener('click', toggleRecording);
  document.getElementById('gaze-open-sessions').addEventListener('click', () => setView('sessions'));
  const grantBtn = document.getElementById('gaze-grant-btn');
  if (grantBtn) grantBtn.addEventListener('click', requestInputPermission);

  drawGazeCanvas();
}

function drawGazeCanvas() {
  const canvas = document.getElementById('gaze-canvas');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const cs = getComputedStyle(document.documentElement);
  const bg = cs.getPropertyValue('--or-surface').trim();
  const border = cs.getPropertyValue('--or-border-soft').trim();
  const accent = cs.getPropertyValue('--or-accent').trim();
  const muted = cs.getPropertyValue('--or-text-muted').trim();
  const ok = cs.getPropertyValue('--or-positive').trim();

  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, rect.width, rect.height);

  // Subtle blueprint grid
  ctx.strokeStyle = border;
  ctx.lineWidth = 1;
  for (const frac of [0.25, 0.5, 0.75]) {
    ctx.beginPath();
    ctx.moveTo(rect.width * frac, 0);
    ctx.lineTo(rect.width * frac, rect.height);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(0, rect.height * frac);
    ctx.lineTo(rect.width, rect.height * frac);
    ctx.stroke();
  }

  const g = state.gaze;
  const pred = g && g.prediction;
  if (!g || !g.running || !pred || !pred.face_detected) {
    ctx.fillStyle = muted;
    ctx.font = '13px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(
      g && g.running ? 'No face detected in camera frame' : 'Press Start tracking to preview live monocular gaze',
      rect.width / 2,
      rect.height / 2
    );
    return;
  }

  const dw = Math.max(1, g.display.width);
  const dh = Math.max(1, g.display.height);
  const gx = (pred.x / dw) * rect.width;
  const gy = (pred.y / dh) * rect.height;

  // Crosshair + gaze ring
  ctx.strokeStyle = pred.using_ridge ? ok : accent;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(gx, gy, 16, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = pred.using_ridge ? ok : accent;
  ctx.beginPath();
  ctx.arc(gx, gy, 4, 0, Math.PI * 2);
  ctx.fill();
}

// ---- SESSIONS VIEW (Curator + Phase 1 Recall Gate) ---------------------

async function loadSessionsList() {
  const [listRes, recallRes] = await Promise.all([
    api('/v1/sessions').catch(() => ({ sessions: [] })),
    api('/v1/sessions/recall').catch(() => null),
  ]);
  state.sessions = listRes.sessions || [];
  state.recall = recallRes;
  updateChrome();

  if (state.sessions.length > 0) {
    const keep = state.sessions.some((s) => s.session_id === state.selectedSessionId);
    if (!keep) state.selectedSessionId = state.sessions[0].session_id;
    await loadSessionDetail(state.selectedSessionId);
  } else {
    state.selectedSessionId = null;
    state.sessionDetail = null;
  }
}

async function loadSessionDetail(sid) {
  if (!sid) {
    state.sessionDetail = null;
    return;
  }
  state.sessionDetail = await api(`/v1/sessions/${encodeURIComponent(sid)}`).catch(() => null);
  state.selectedEventIdx = 0;
}

function filteredSessionEvents() {
  const events = (state.sessionDetail && state.sessionDetail.events) || [];
  const out = [];
  events.forEach((ev, idx) => {
    const inter = ev.interaction || {};
    const kind = inter.kind || 'frame';
    const mouse = inter.mouse;
    const cands = (ev.screen && ev.screen.candidates) || [];
    const covered = Boolean(mouse && cands.some((c) => {
      const b = c.bbox || [0, 0, 0, 0];
      return mouse[0] >= b[0] && mouse[0] <= b[0] + b[2] && mouse[1] >= b[1] && mouse[1] <= b[1] + b[3];
    }));
    if (state.eventFilter === 'clicks' && kind !== 'click') return;
    if (state.eventFilter === 'missed' && (kind !== 'click' || covered)) return;
    if (state.eventFilter === 'rejected' && inter.accepted !== false) return;
    out.push({ idx, ev, covered });
  });
  return out;
}

function renderSessions(root) {
  const r = state.recall || { sessions: 0, clicks: 0, hits: 0, overall_recall: null, gate_met: false, per_app: [] };
  const rows = filteredSessionEvents();
  if (state.selectedEventIdx >= rows.length) state.selectedEventIdx = Math.max(0, rows.length - 1);
  const activeRow = rows[state.selectedEventIdx] || null;
  const activeEv = activeRow ? activeRow.ev : null;
  const inter = (activeEv && activeEv.interaction) || {};
  const screen = (activeEv && activeEv.screen) || {};

  root.innerHTML = `
    <section class="card">
      <div class="row-between">
        <div>
          <div class="eyebrow">Phase 1 candidate recall (>95% gate)</div>
          <div class="telemetry" style="margin-top:4px;">
            ${r.overall_recall != null ? `${(r.overall_recall * 100).toFixed(1)}% (${r.hits}/${r.clicks} clicks across ${r.sessions} sessions)` : 'No labeled clicks recorded yet'}
          </div>
        </div>
        <div class="row-gap">
          <span class="badge ${r.gate_met ? 'ok' : r.clicks > 0 ? 'warn' : ''}">
            ${r.gate_met ? 'Gate met' : r.clicks > 0 ? 'Below 95%' : 'No data'}
          </span>
          <button id="sess-sample-btn" class="btn btn-sm" type="button">Create sample session</button>
          <button id="sess-export-btn" class="btn btn-sm" type="button" ${state.sessions.length ? '' : 'disabled'}>Export split</button>
        </div>
      </div>
      ${r.per_app && r.per_app.length ? `
        <table>
          <thead><tr><th>App</th><th>Clicks</th><th>Hits</th><th>Recall</th></tr></thead>
          <tbody>
            ${r.per_app.map((a) => `
              <tr>
                <td>${esc(a.app)}</td>
                <td class="telemetry">${a.clicks}</td>
                <td class="telemetry">${a.hits}</td>
                <td class="telemetry">${(a.recall * 100).toFixed(1)}%</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      ` : ''}
    </section>

    <div class="curator-grid">
      <section class="card" style="padding:14px;">
        <div class="row-between">
          <span class="eyebrow">Sessions</span>
          <button id="sess-del-btn" class="btn btn-sm" type="button" ${state.selectedSessionId ? '' : 'disabled'}>Delete</button>
        </div>
        <div class="list-box">
          ${state.sessions.map((s) => `
            <button class="list-item ${s.session_id === state.selectedSessionId ? 'selected' : ''}" data-sid="${esc(s.session_id)}" type="button">
              <strong>${esc(s.session_id.slice(0, 14))}</strong>
              <span class="list-item-meta">${s.click_count} clicks (${s.recall != null ? `${Math.round(s.recall * 100)}%` : '--'})</span>
              <span class="list-item-meta">${esc((s.apps || []).slice(0, 2).join(', ') || 'no apps')}</span>
            </button>
          `).join('') || '<span class="muted">No sessions recorded.</span>'}
        </div>
      </section>

      <section class="card" style="padding:14px;">
        <div class="row-between">
          <span class="eyebrow">Visual inspector (click box to assign target)</span>
          <span class="telemetry">${activeRow ? `#${activeRow.idx} | ${esc(screen.app || 'unknown')} | ${(screen.candidates || []).length} boxes` : 'No event selected'}</span>
        </div>
        <div class="canvas-wrap">
          <canvas id="session-canvas" style="cursor:crosshair;"></canvas>
        </div>
        ${state.sessionDetail && state.sessionDetail.metrics ? `
          <div class="row-gap">
            <span class="telemetry">top1: ${fmtMetric(state.sessionDetail.metrics.top1)}</span>
            <span class="telemetry">baseline_top1: ${fmtMetric(state.sessionDetail.metrics.baseline_top1)}</span>
            <span class="telemetry">hit@50px: ${fmtMetric(state.sessionDetail.metrics['hit@50px'])}</span>
            <span class="telemetry">recall@3: ${fmtMetric(state.sessionDetail.metrics['candidate_recall@3'])}</span>
          </div>
        ` : ''}
      </section>

      <section class="card" style="padding:14px;">
        <div class="row-between">
          <span class="eyebrow">Events</span>
          <select id="sess-filter">
            <option value="all" ${state.eventFilter === 'all' ? 'selected' : ''}>All</option>
            <option value="clicks" ${state.eventFilter === 'clicks' ? 'selected' : ''}>Clicks</option>
            <option value="missed" ${state.eventFilter === 'missed' ? 'selected' : ''}>Missed</option>
            <option value="rejected" ${state.eventFilter === 'rejected' ? 'selected' : ''}>Rejected</option>
          </select>
        </div>
        <div class="list-box" style="max-height:240px;">
          ${rows.map((r, pos) => {
            const it = r.ev.interaction || {};
            const tag = it.accepted === true ? 'OK' : it.accepted === false ? 'REJ' : 'NEW';
            const hit = it.kind === 'click' ? (r.covered ? ' [HIT]' : ' [MISS]') : '';
            return `
              <button class="list-item ${pos === state.selectedEventIdx ? 'selected' : ''}" data-pos="${pos}" type="button">
                <span class="list-item-meta">#${String(r.idx).padStart(3, '0')} ${esc(it.kind || 'frame')} ${tag}${hit}</span>
              </button>
            `;
          }).join('') || '<span class="muted">No matching events.</span>'}
        </div>
        <div class="card-subtle" style="display:flex;flex-direction:column;gap:8px;">
          <div class="telemetry">Target: ${esc(inter.target || 'none')}</div>
          <div class="telemetry">Accepted: ${esc(String(inter.accepted))}</div>
          <div class="row-gap">
            <button id="ev-accept-btn" class="btn btn-sm" type="button" ${activeRow ? '' : 'disabled'}>Accept (A)</button>
            <button id="ev-reject-btn" class="btn btn-sm" type="button" ${activeRow ? '' : 'disabled'}>Reject (R)</button>
            <button id="ev-clear-btn" class="btn btn-sm" type="button" ${activeRow ? '' : 'disabled'}>Clear target</button>
            <button id="ev-del-btn" class="btn btn-sm btn-danger" type="button" ${activeRow ? '' : 'disabled'}>Delete (Del)</button>
          </div>
        </div>
      </section>
    </div>
  `;

  document.getElementById('sess-sample-btn').addEventListener('click', async () => {
    await api('/v1/sessions/sample', { method: 'POST', body: '{}' });
    await loadSessionsList();
    renderSessions(root);
  });

  document.getElementById('sess-export-btn').addEventListener('click', async () => {
    try {
      const res = await api('/v1/sessions/export', { method: 'POST', body: '{}' });
      showError('Exported session-level split', `Saved train/val/test JSONL files to ${res.out_dir}`, { command: res.out_dir });
    } catch (err) {
      showError('Export failed', err.message);
    }
  });

  document.getElementById('sess-del-btn').addEventListener('click', async () => {
    if (!state.selectedSessionId) return;
    if (!confirm(`Delete session ${state.selectedSessionId}?`)) return;
    await api(`/v1/sessions/${encodeURIComponent(state.selectedSessionId)}`, { method: 'DELETE' });
    state.selectedSessionId = null;
    await loadSessionsList();
    renderSessions(root);
  });

  root.querySelectorAll('[data-sid]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      state.selectedSessionId = btn.dataset.sid;
      await loadSessionDetail(state.selectedSessionId);
      renderSessions(root);
    });
  });

  document.getElementById('sess-filter').addEventListener('change', (e) => {
    state.eventFilter = e.target.value;
    state.selectedEventIdx = 0;
    renderSessions(root);
  });

  root.querySelectorAll('[data-pos]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.selectedEventIdx = Number(btn.dataset.pos);
      renderSessions(root);
    });
  });

  if (activeRow) {
    document.getElementById('ev-accept-btn').addEventListener('click', () => patchCurrentEvent({ accepted: true }));
    document.getElementById('ev-reject-btn').addEventListener('click', () => patchCurrentEvent({ accepted: false }));
    document.getElementById('ev-clear-btn').addEventListener('click', () => patchCurrentEvent({ target: null }));
    document.getElementById('ev-del-btn').addEventListener('click', deleteCurrentEvent);
  }

  drawSessionCanvas();
}

function fmtMetric(v) {
  return v == null ? 'n/a' : Number(v).toFixed(3);
}

let lastSessionCoordSize = [1280, 720];

function drawSessionCanvas() {
  const canvas = document.getElementById('session-canvas');
  if (!canvas) return;
  const rows = filteredSessionEvents();
  const activeRow = rows[state.selectedEventIdx] || null;
  const ev = activeRow ? activeRow.ev : null;

  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const cs = getComputedStyle(document.documentElement);
  const bg = cs.getPropertyValue('--or-surface').trim();
  const border = cs.getPropertyValue('--or-border-soft').trim();
  const muted = cs.getPropertyValue('--or-text-muted').trim();
  const ok = cs.getPropertyValue('--or-positive').trim();
  const err = cs.getPropertyValue('--or-negative').trim();
  const warn = cs.getPropertyValue('--or-warning').trim();

  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, rect.width, rect.height);

  if (!ev) {
    ctx.fillStyle = muted;
    ctx.font = '13px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Select a recorded event to inspect candidates and clicks', rect.width / 2, rect.height / 2);
    return;
  }

  const screen = ev.screen || {};
  const cands = screen.candidates || [];
  const inter = ev.interaction || {};
  const pred = ev.prediction || {};

  const renderOverlays = (cw, ch) => {
    lastSessionCoordSize = [cw, ch];
    const sx = rect.width / Math.max(1, cw);
    const sy = rect.height / Math.max(1, ch);

    for (const c of cands) {
      const b = c.bbox || [0, 0, 0, 0];
      const isTarget = inter.target && c.id === inter.target;
      ctx.strokeStyle = isTarget ? ok : muted;
      ctx.lineWidth = isTarget ? 2 : 1;
      ctx.fillStyle = isTarget ? 'rgba(47, 158, 98, 0.18)' : 'rgba(130, 125, 118, 0.08)';
      ctx.fillRect(b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy);
      ctx.strokeRect(b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy);
    }

    if (pred.pixel && pred.pixel.length === 2) {
      ctx.strokeStyle = warn;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(pred.pixel[0] * sx, pred.pixel[1] * sy, 7, 0, Math.PI * 2);
      ctx.stroke();
    }

    if (inter.mouse && inter.mouse.length === 2) {
      const mx = inter.mouse[0] * sx;
      const my = inter.mouse[1] * sy;
      ctx.strokeStyle = activeRow.covered ? ok : err;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(mx - 9, my); ctx.lineTo(mx + 9, my);
      ctx.moveTo(mx, my - 9); ctx.lineTo(mx, my + 9);
      ctx.stroke();
    }
  };

  if (screen.screenshot && state.selectedSessionId) {
    const img = new Image();
    img.onload = () => {
      ctx.drawImage(img, 0, 0, rect.width, rect.height);
      renderOverlays(img.naturalWidth || 1920, img.naturalHeight || 1080);
    };
    img.onerror = () => renderOverlays(1280, 720);
    img.src = `/v1/sessions/${encodeURIComponent(state.selectedSessionId)}/blobs/${encodeURIComponent(screen.screenshot)}.png`;
  } else {
    let mw = 1280, mh = 720;
    for (const c of cands) {
      if (c.bbox) { mw = Math.max(mw, c.bbox[0] + c.bbox[2] + 40); mh = Math.max(mh, c.bbox[1] + c.bbox[3] + 40); }
    }
    if (inter.mouse) { mw = Math.max(mw, inter.mouse[0] + 60); mh = Math.max(mh, inter.mouse[1] + 60); }
    ctx.strokeStyle = border;
    ctx.strokeRect(0.5, 0.5, rect.width - 1, rect.height - 1);
    renderOverlays(mw, mh);
  }

  canvas.onclick = (e) => {
    const bounds = canvas.getBoundingClientRect();
    const x = ((e.clientX - bounds.left) / Math.max(1, bounds.width)) * lastSessionCoordSize[0];
    const y = ((e.clientY - bounds.top) / Math.max(1, bounds.height)) * lastSessionCoordSize[1];
    const hits = cands.filter((c) => {
      const b = c.bbox || [0, 0, 0, 0];
      return x >= b[0] && x <= b[0] + b[2] && y >= b[1] && y <= b[1] + b[3];
    });
    if (hits.length) {
      hits.sort((a, b) => (a.bbox[2] * a.bbox[3]) - (b.bbox[2] * b.bbox[3]));
      patchCurrentEvent({ target: hits[0].id, accepted: true });
    }
  };
}

async function patchCurrentEvent(patch) {
  const rows = filteredSessionEvents();
  const activeRow = rows[state.selectedEventIdx];
  if (!activeRow || !state.selectedSessionId) return;
  try {
    await api(`/v1/sessions/${encodeURIComponent(state.selectedSessionId)}/events/${activeRow.idx}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    await loadSessionDetail(state.selectedSessionId);
    state.recall = await api('/v1/sessions/recall').catch(() => state.recall);
    updateChrome();
    renderView();
  } catch (err) {
    showError('Could not update event', err.message);
  }
}

async function deleteCurrentEvent() {
  const rows = filteredSessionEvents();
  const activeRow = rows[state.selectedEventIdx];
  if (!activeRow || !state.selectedSessionId) return;
  try {
    await api(`/v1/sessions/${encodeURIComponent(state.selectedSessionId)}/events/${activeRow.idx}`, {
      method: 'DELETE',
    });
    await loadSessionDetail(state.selectedSessionId);
    state.recall = await api('/v1/sessions/recall').catch(() => state.recall);
    updateChrome();
    renderView();
  } catch (err) {
    showError('Could not delete event', err.message);
  }
}

// ---- ACTIVITY VIEW -----------------------------------------------------

function renderActivity(root) {
  const q = state.historyQuery.trim().toLowerCase();
  const items = (state.history || []).filter((it) =>
    !q || `${it.transcript || ''} ${it.route || ''} ${it.target || ''} ${it.result || ''}`.toLowerCase().includes(q)
  );
  root.innerHTML = `
    <section class="card">
      <div class="row-between">
        <span class="eyebrow">Action history (${items.length})</span>
        <input id="act-search" type="text" placeholder="Filter activity..." value="${esc(state.historyQuery)}">
      </div>
      ${items.length ? `
        <table>
          <thead><tr><th>Transcript</th><th>Route</th><th>Target / Result</th><th>Latency</th></tr></thead>
          <tbody>
            ${items.map((it) => `
              <tr>
                <td>${esc(it.transcript || '')}</td>
                <td class="muted">${esc(it.route || '')}</td>
                <td>${esc(it.target || it.result || '')}</td>
                <td class="telemetry">${it.latency_ms != null ? `${Math.round(it.latency_ms)} ms` : ''}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      ` : '<p class="muted">No voice or browser actions recorded yet.</p>'}
    </section>
  `;
  document.getElementById('act-search').addEventListener('input', (e) => {
    state.historyQuery = e.target.value;
    renderActivity(root);
    const inp = document.getElementById('act-search');
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  });
}

// ---- SETTINGS VIEW (Schema-Driven) -------------------------------------

function renderSettings(root) {
  const groups = new Map();
  for (const spec of state.schema) {
    const g = spec.group || 'General';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(spec);
  }

  const env = (state.gaze && state.gaze.environment) || {};
  const model = state.models || {};

  const groupsHtml = Array.from(groups.entries()).map(([groupName, specs]) => `
    <section class="card">
      <span class="eyebrow">${esc(groupName)}</span>
      ${specs.map((spec) => renderSpecControl(spec, state.config[spec.key])).join('')}
    </section>
  `).join('');

  root.innerHTML = `
    <section class="card">
      <span class="eyebrow">Permissions & Models</span>
      <div class="field-row">
        <div class="field-label">
          <strong>Linux input capture (/dev/input/event*)</strong>
          <small>${env.input_group_active ? 'Active and readable for global click recording.' : 'Locked: authorize via PolicyKit or run the manual command.'}</small>
        </div>
        ${env.input_group_active
          ? '<span class="badge ok">Active</span>'
          : '<button id="set-grant-input" class="btn btn-sm" type="button">Grant input access</button>'}
      </div>
      <div class="field-row">
        <div class="field-label">
          <strong>Speech-to-text model (${esc(model.name || 'Parakeet Unified EN')})</strong>
          <small>Status: ${esc(model.state || 'unknown')}</small>
        </div>
        ${model.ready
          ? '<span class="badge ok">Ready offline</span>'
          : '<button id="set-dl-model" class="btn btn-sm" type="button">Download model</button>'}
      </div>
    </section>
    ${groupsHtml}
  `;

  const grantBtn = document.getElementById('set-grant-input');
  if (grantBtn) grantBtn.addEventListener('click', requestInputPermission);
  const dlBtn = document.getElementById('set-dl-model');
  if (dlBtn) {
    dlBtn.addEventListener('click', async () => {
      await api('/v1/ui/action', { method: 'POST', body: JSON.stringify({ action: 'models_download' }) });
      setView('settings');
    });
  }

  root.querySelectorAll('[data-setting-key]').forEach((el) => {
    el.addEventListener('change', async () => {
      const key = el.dataset.settingKey;
      const vtype = el.dataset.settingType;
      let val;
      if (vtype === 'bool') val = el.checked;
      else if (vtype === 'int') val = parseInt(el.value, 10);
      else if (vtype === 'float') val = parseFloat(el.value);
      else val = el.value;
      try {
        clearError();
        const res = await api('/v1/ui/action', {
          method: 'POST',
          body: JSON.stringify({ action: 'set_config', arg: { [key]: val } }),
        });
        if (res.result && res.result.config) state.config = res.result.config;
      } catch (err) {
        showError(`Invalid value for ${key}`, err.message);
      }
    });
  });
}

function renderSpecControl(spec, currentVal) {
  const val = currentVal !== undefined ? currentVal : spec.default;
  const badge = spec.apply === 'restart' ? ' <span class="badge">restart</span>' : '';
  let inputHtml = '';
  if (spec.control === 'toggle' || spec.value_type === 'bool') {
    inputHtml = `<input type="checkbox" data-setting-key="${esc(spec.key)}" data-setting-type="bool" ${val ? 'checked' : ''}>`;
  } else if (spec.options && spec.options.length) {
    inputHtml = `
      <select data-setting-key="${esc(spec.key)}" data-setting-type="str">
        ${spec.options.map((opt) => `<option value="${esc(opt)}" ${opt === val ? 'selected' : ''}>${esc(opt)}</option>`).join('')}
      </select>
    `;
  } else if (spec.value_type === 'int' || spec.value_type === 'float') {
    inputHtml = `<input type="number" data-setting-key="${esc(spec.key)}" data-setting-type="${esc(spec.value_type)}" value="${esc(val)}" ${spec.minimum != null ? `min="${spec.minimum}"` : ''} ${spec.maximum != null ? `max="${spec.maximum}"` : ''} ${spec.value_type === 'float' ? 'step="0.05"' : ''} style="width:110px;">`;
  } else {
    inputHtml = `<input type="text" data-setting-key="${esc(spec.key)}" data-setting-type="str" value="${esc(val ?? '')}" style="width:240px;">`;
  }
  return `
    <div class="field-row">
      <div class="field-label">
        <strong>${esc(spec.label)}${badge}</strong>
        <small>${esc(spec.help || '')}</small>
      </div>
      ${inputHtml}
    </div>
  `;
}

// ---- SHARED ACTIONS & LIVE STREAMS -------------------------------------

async function toggleListening() {
  try {
    clearError();
    const action = state.status.state === 'listening' ? 'pause' : 'start';
    await api('/v1/ui/action', { method: 'POST', body: JSON.stringify({ action }) });
    await refreshCore();
    renderView();
  } catch (err) {
    showError('Engine control failed', err.message);
  }
}

async function toggleRecording() {
  try {
    clearError();
    if (state.gaze && state.gaze.recording) {
      state.gaze = await api('/v1/sessions/record/stop', { method: 'POST', body: '{}' });
      await loadSessionsList();
    } else {
      state.gaze = await api('/v1/sessions/record/start', {
        method: 'POST',
        body: JSON.stringify({ save_screenshots: true, use_ocr: true }),
      });
    }
    updateChrome();
    renderView();
  } catch (err) {
    const p = err.payload || {};
    showError(
      p.needs_input_permission ? 'Global click capture is locked' : 'Recording failed',
      err.message,
      { command: p.manual_command || '', showGrant: Boolean(p.needs_input_permission) }
    );
  }
}

async function requestInputPermission() {
  try {
    const res = await api('/v1/permissions/input', { method: 'POST', body: '{}' });
    if (res.ok) {
      clearError();
      await refreshCore();
      renderView();
    } else {
      showError('Could not grant input access', res.message || 'Authorization failed', {
        command: res.manual_command || '',
        showGrant: true,
      });
    }
  } catch (err) {
    showError('Permission request failed', err.message);
  }
}

function connectStreams() {
  try {
    const evSrc = new EventSource('/v1/events');
    evSrc.addEventListener('state', async () => {
      await refreshCore();
      if (state.view === 'home') renderView();
    });
  } catch { /* fallback polling below */ }

  try {
    const gazeSrc = new EventSource('/v1/gaze/stream');
    gazeSrc.addEventListener('gaze', (evt) => {
      try {
        state.gaze = JSON.parse(evt.data);
        updateChrome();
        if (state.view === 'gaze') {
          drawGazeCanvas();
          const coords = document.getElementById('gaze-coords');
          const pred = state.gaze.prediction;
          if (coords) {
            coords.textContent = pred && pred.face_detected
              ? `x=${Math.round(pred.x)}, y=${Math.round(pred.y)} (${pred.using_ridge ? 'ridge' : 'cold-start prior'})`
              : state.gaze.running ? 'Waiting for face detection' : 'Tracker stopped';
          }
          const fpsInfo = document.getElementById('gaze-fps-info');
          const st = state.gaze.stats || {};
          if (fpsInfo) {
            fpsInfo.textContent = `Frames: ${st.frames_read || 0} / Faces: ${st.faces_detected || 0} / Clicks adapted: ${st.clicks_adapted || 0}`;
          }
        }
      } catch { /* ignore malformed frame */ }
    });
  } catch { /* fallback */ }
}

function initEvents() {
  document.querySelectorAll('.nav-btn').forEach((btn) => {
    btn.addEventListener('click', () => setView(btn.dataset.view));
  });
  document.getElementById('header-primary-btn').addEventListener('click', toggleListening);
  document.getElementById('header-record-btn').addEventListener('click', toggleRecording);

  document.getElementById('error-dismiss-btn').addEventListener('click', clearError);
  document.getElementById('error-grant-btn').addEventListener('click', requestInputPermission);
  document.getElementById('error-copy-btn').addEventListener('click', (e) => {
    if (!state.lastError) return;
    const txt = `${state.lastError.title}: ${state.lastError.message}` +
      (state.lastError.command ? `\n${state.lastError.command}` : '');
    copyText(txt, e.currentTarget);
  });
  document.getElementById('error-cmd-copy').addEventListener('click', (e) => {
    copyText(document.getElementById('error-cmd-text').textContent, e.currentTarget);
  });

  document.addEventListener('keydown', (e) => {
    if (state.view !== 'sessions') return;
    if (e.target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
    if (e.key === 'a' || e.key === 'A') patchCurrentEvent({ accepted: true });
    else if (e.key === 'r' || e.key === 'R') patchCurrentEvent({ accepted: false });
    else if (e.key === 'Delete') deleteCurrentEvent();
  });
}

(async function boot() {
  initThemeAndSidebar();
  initEvents();
  await refreshCore();
  await setView('home');
  connectStreams();
})();
