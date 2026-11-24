// Keyless Precision Instrument UI - Unified Monocular + Binocular Gaze & Voice Control

const state = {
  view: 'home',
  status: { state: 'stopped', detail: '' },
  gaze: null,
  cameraMode: 'auto',
  gazeTrail: [],
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
  models: null,
  lastError: null,
};

const VIEW_META = {
  home: ['Home', 'Voice commands, live engine state, and quick launch'],
  gaze: ['Gaze', 'Monocular (10D) and binocular (20D) optical tracking deck'],
  sessions: ['Sessions', 'Phase 1 >95% candidate recall gate and dataset curation'],
  activity: ['Activity', 'Voice and browser-agent execution log'],
  settings: ['Settings', 'Schema-driven configuration, permissions, and offline models'],
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    ...opts,
  });
  let body = {};
  try { body = await res.json(); } catch { body = {}; }
  if (!res.ok) {
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.payload = body;
    err.status = res.status;
    throw err;
  }
  return body;
}

function esc(v) {
  return String(v ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function showError(title, message, { command = '', showGrant = false } = {}) {
  state.lastError = { title, message, command };
  document.getElementById('error-title').textContent = title;
  document.getElementById('error-body').textContent = message;
  document.getElementById('error-grant-btn').hidden = !showGrant;
  const cmdWrap = document.getElementById('error-cmd-wrap');
  if (command) {
    document.getElementById('error-cmd-text').textContent = command;
    cmdWrap.hidden = false;
  } else {
    cmdWrap.hidden = true;
  }
  document.getElementById('error-callout').hidden = false;
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
    if (state.view === 'gaze') { drawGazeCanvas(); drawAttitudeDial(); }
    if (state.view === 'sessions') drawSessionCanvas();
  });

  if (localStorage.getItem('keyless-sidebar') === 'closed') {
    document.documentElement.setAttribute('data-sidebar', 'closed');
  }
  document.getElementById('sidebar-toggle').addEventListener('click', () => {
    const closed = document.documentElement.getAttribute('data-sidebar') === 'closed';
    document.documentElement.setAttribute('data-sidebar', closed ? 'open' : 'closed');
    localStorage.setItem('keyless-sidebar', closed ? 'open' : 'closed');
    setTimeout(() => {
      if (state.view === 'gaze') drawGazeCanvas();
      if (state.view === 'sessions') drawSessionCanvas();
    }, 200);
  });

  window.addEventListener('resize', () => {
    if (state.view === 'gaze') drawGazeCanvas();
    if (state.view === 'sessions') drawSessionCanvas();
  });
}

function updateChrome() {
  const st = state.status.state || 'stopped';
  const dotCls = st === 'listening' ? 'ok' : st === 'error' ? 'err' : st === 'paused' ? 'warn' : '';
  document.getElementById('sidebar-dot').className = 'led ' + dotCls;
  document.getElementById('sidebar-state').textContent = st;

  document.getElementById('header-listen-led').className = 'led ' + dotCls;
  document.getElementById('header-listen-label').textContent =
    st === 'listening' ? 'Listening' : st === 'paused' ? 'Paused' : 'Listen';

  const recording = Boolean(state.gaze && state.gaze.recording);
  document.getElementById('header-rec-led').className = 'led ' + (recording ? 'err' : '');
  document.getElementById('header-rec-label').textContent = recording ? 'Recording' : 'Record';
  document.getElementById('header-record-btn').classList.toggle('btn-danger', recording);

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
      api('/v1/history?limit=8').catch(() => ({ actions: [] })),
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
    const [cfg, schema, mod] = await Promise.all([
      api('/v1/config').catch(() => ({})),
      api('/v1/config/schema').catch(() => ({ settings: [] })),
      api('/v1/models').catch(() => null),
    ]);
    state.config = cfg;
    state.schema = schema.settings || [];
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
  const g = state.gaze || {};
  const cams = g.available_cameras || [];
  const mons = (g.display && g.display.monitors) || [];
  const recallTxt = state.recall && state.recall.overall_recall != null
    ? `${(state.recall.overall_recall * 100).toFixed(1)}%`
    : '--';

  const q = state.commandQuery.trim().toLowerCase();
  const filteredCats = (state.commands || []).map((cat) => ({
    ...cat,
    commands: (cat.commands || []).filter((c) =>
      !q || `${c.phrase} ${c.description} ${c.example}`.toLowerCase().includes(q)
    ),
  })).filter((c) => c.commands.length > 0);

  root.innerHTML = `
    <section class="home-hero">
      <div>
        <span class="eyebrow">Keyless Control Plane / ${esc(st)}</span>
        <h1>${st === 'listening' ? 'Listening for commands.' : st === 'paused' ? 'Engine paused.' : 'Precision gaze and voice.'}</h1>
        <p>${st === 'error' ? esc(state.status.detail || 'Inspect Settings for diagnostics.') : 'Speak naturally to click, scroll, dictate, or drive Firefox. Open Gaze to monitor monocular or binocular tracking in real time.'}</p>
        <div class="row-gap">
          <button id="home-primary" class="btn btn-primary" type="button">
            ${st === 'listening' ? 'Pause listening' : st === 'paused' ? 'Resume listening' : 'Start listening'}
          </button>
          <button id="home-open-gaze" class="btn" type="button">Open gaze deck</button>
          <button id="home-open-sessions" class="btn" type="button">Curate sessions</button>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;align-items:flex-end;">
        <span class="eyebrow">Hardware detected</span>
        <span class="telemetry">${cams.length} camera(s) (${cams.length >= 2 ? 'binocular ready' : 'monocular ready'})</span>
        <span class="telemetry">${mons.length || 1} display(s) (${g.display ? `${g.display.width}x${g.display.height}` : '--'})</span>
        <span class="telemetry">Input capture: ${g.environment && g.environment.input_group_active ? 'active' : 'locked'}</span>
      </div>
    </section>

    <section class="home-stats">
      <div class="home-stat-cell">
        <span class="eyebrow">Optical tracker</span>
        <span class="big-readout">${g.running ? esc((g.mode || 'monocular').toUpperCase()) : 'STANDBY'}</span>
        <span class="telemetry">${g.running && g.stats ? `${g.stats.measured_fps || 0} fps / ${g.stats.samples || 0} clicks adapted` : 'Press Start tracking in Gaze'}</span>
      </div>
      <div class="home-stat-cell">
        <span class="eyebrow">Phase 1 recall gate</span>
        <span class="big-readout">${esc(recallTxt)}</span>
        <span class="telemetry">${state.recall && state.recall.clicks ? `${state.recall.hits}/${state.recall.clicks} clicks (${state.recall.gate_met ? 'gate met' : 'target >95%'})` : 'Record real sessions to measure'}</span>
      </div>
      <div class="home-stat-cell">
        <span class="eyebrow">Recent actions</span>
        <span class="big-readout">${(state.history || []).length}</span>
        <span class="telemetry">${state.history && state.history[0] ? esc(state.history[0].transcript || state.history[0].result || '') : 'No commands executed yet'}</span>
      </div>
    </section>

    <section class="panel-section" style="background:var(--or-bg);">
      <div class="row-between">
        <span class="eyebrow">Voice command reference</span>
        <input id="home-cmd-search" type="text" placeholder="Filter commands..." value="${esc(state.commandQuery)}" style="width:260px;">
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px;margin-top:6px;">
        ${filteredCats.map((cat) => `
          <div style="border:1px solid var(--or-border-soft);background:var(--or-surface);padding:14px 16px;">
            <div class="eyebrow" style="margin-bottom:8px;">${esc(cat.title)}</div>
            ${cat.commands.map((c) => `
              <div class="field-row" style="padding:6px 0;">
                <div class="field-label">
                  <strong style="font-family:var(--or-font-mono);font-size:0.76rem;">${esc(c.phrase)}</strong>
                  <small>${esc(c.description)}</small>
                </div>
              </div>
            `).join('')}
          </div>
        `).join('') || '<span class="muted">No matching commands.</span>'}
      </div>
    </section>
  `;

  document.getElementById('home-primary').addEventListener('click', toggleListening);
  document.getElementById('home-open-gaze').addEventListener('click', () => setView('gaze'));
  document.getElementById('home-open-sessions').addEventListener('click', () => setView('sessions'));
  document.getElementById('home-cmd-search').addEventListener('input', (e) => {
    state.commandQuery = e.target.value;
    renderHome(root);
    const inp = document.getElementById('home-cmd-search');
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  });
}

// ---- GAZE INSTRUMENT DECK (Monocular 10D + Binocular 20D) --------------

function renderGaze(root) {
  const g = state.gaze || {};
  const running = Boolean(g.running);
  const recording = Boolean(g.recording);
  const env = g.environment || {};
  const stats = g.stats || {};
  const pred = g.prediction || null;
  const cams = g.available_cameras || [];
  const hasStereo = cams.length >= 2;
  const samples = stats.samples || 0;
  const minClicks = stats.min_clicks || 5;
  const usingRidge = Boolean(stats.using_ridge);

  const pips = Array.from({ length: 10 }, (_, i) => {
    const threshold = Math.ceil(((i + 1) / 10) * Math.max(minClicks, 10));
    const cls = usingRidge && samples >= threshold ? 'locked' : samples >= threshold ? 'filled' : '';
    return `<div class="seg-pip ${cls}"></div>`;
  }).join('');

  root.innerHTML = `
    <div class="deck deck-gaze">
      <div class="deck-main">
        <div class="viewport-bar">
          <div class="row-gap">
            <span class="led ${running ? (pred && pred.face_detected ? 'ok' : 'warn') : ''}" id="gaze-live-led"></span>
            <span class="eyebrow">Optical Viewport (${g.display ? `${g.display.width}x${g.display.height}` : '--'})</span>
            <span class="badge ${usingRidge ? 'ok' : ''}" id="gaze-mode-badge">
              ${running ? `${esc((g.mode || 'monocular').toUpperCase())} ${stats.feature_dim || 10}D` : 'STANDBY'}
            </span>
          </div>
          <div class="row-gap">
            <span class="telemetry" id="gaze-coords">
              ${pred && pred.face_detected ? `X ${String(Math.round(pred.x)).padStart(4, '0')}  Y ${String(Math.round(pred.y)).padStart(4, '0')}` : 'X ----  Y ----'}
            </span>
            <button id="gaze-toggle-btn" class="btn btn-primary" type="button">
              ${running ? 'Stop tracking' : 'Start tracking'}
            </button>
          </div>
        </div>

        <div class="viewport-stage">
          <div class="viewport-canvas-wrap">
            <canvas id="gaze-canvas"></canvas>
          </div>
        </div>

        <div class="telemetry-strip">
          <div class="telemetry-cell">
            <span class="eyebrow">Frame rate</span>
            <span class="big-readout" id="gaze-fps">${stats.measured_fps ? `${stats.measured_fps}` : '--'}</span>
            <span class="telemetry" id="gaze-frame-counts">${stats.frames_read || 0} frames / ${stats.faces_detected || 0} faces</span>
          </div>
          <div class="telemetry-cell">
            <span class="eyebrow">Adaptation</span>
            <span class="big-readout" id="gaze-adapt-state">${usingRidge ? 'RIDGE' : 'PRIOR'}</span>
            <span class="telemetry" id="gaze-adapt-count">${samples} / ${minClicks} clicks</span>
          </div>
          <div class="telemetry-cell">
            <span class="eyebrow">Stereo skew</span>
            <span class="big-readout" id="gaze-skew">${pred && g.mode === 'binocular' ? `${pred.skew_ms.toFixed(1)}ms` : 'N/A'}</span>
            <span class="telemetry">${cams.length} camera(s) online</span>
          </div>
          <div class="telemetry-cell">
            <span class="eyebrow">Safety mode</span>
            <span class="big-readout">PREVIEW</span>
            <span class="telemetry">Pointer never moves implicitly</span>
          </div>
        </div>
      </div>

      <aside class="deck-rail">
        <section class="panel-section">
          <span class="eyebrow">Optics Mode</span>
          <div class="seg-group">
            <button class="seg-btn ${state.cameraMode === 'auto' ? 'active' : ''}" data-cam-mode="auto" type="button" ${running ? 'disabled' : ''}>Auto</button>
            <button class="seg-btn ${state.cameraMode === 'mono' ? 'active' : ''}" data-cam-mode="mono" type="button" ${running ? 'disabled' : ''}>Mono 10D</button>
            <button class="seg-btn ${state.cameraMode === 'stereo' ? 'active' : ''}" data-cam-mode="stereo" type="button" ${running || !hasStereo ? 'disabled' : ''} title="${hasStereo ? 'Dual-camera binocular 20D' : 'Connect a second webcam for binocular mode'}">Bino 20D</button>
          </div>
          <div class="telemetry">
            ${cams.length ? cams.map((c, i) => `CAM ${i + 1}: ${esc(c.name)}`).join('<br>') : 'No webcam detected'}
          </div>
        </section>

        <section class="panel-section">
          <span class="eyebrow">Head Attitude</span>
          <div class="attitude-row">
            <canvas id="attitude-canvas" class="attitude-dial" width="84" height="84"></canvas>
            <div style="display:flex;flex-direction:column;gap:4px;">
              <div class="telemetry" id="att-yaw">YAW   ${pred ? `${(pred.yaw * 57.3).toFixed(1)} deg` : '--'}</div>
              <div class="telemetry" id="att-pitch">PITCH ${pred ? `${(pred.pitch * 57.3).toFixed(1)} deg` : '--'}</div>
              <div class="telemetry" id="att-roll">ROLL  ${pred ? `${(pred.roll * 57.3).toFixed(1)} deg` : '--'}</div>
              <div class="telemetry" id="att-blink">BLINK ${pred && pred.blinking ? 'CLOSED' : 'OPEN'}</div>
            </div>
          </div>
        </section>

        <section class="panel-section">
          <div class="row-between">
            <span class="eyebrow">Passive Click Adaptation</span>
            <span class="telemetry">${samples} samples</span>
          </div>
          <div class="seg-meter" id="gaze-seg-meter">${pips}</div>
          <div class="field-row">
            <div class="field-label">
              <strong>Min clicks</strong>
              <small>Blend prior into ridge</small>
            </div>
            <input id="gaze-min-clicks" type="number" min="2" max="50" value="${minClicks}" style="width:72px;" ${running ? 'disabled' : ''}>
          </div>
          <div class="field-row">
            <div class="field-label">
              <strong>Ridge alpha</strong>
              <small>L2 damping</small>
            </div>
            <input id="gaze-ridge-alpha" type="number" step="0.005" min="0.0001" max="10" value="0.01" style="width:82px;" ${running ? 'disabled' : ''}>
          </div>
          <div class="row-gap">
            <button id="gaze-undo-btn" class="btn btn-sm" type="button" ${!running || samples === 0 ? 'disabled' : ''}>Undo click</button>
            <button id="gaze-reset-btn" class="btn btn-sm" type="button" ${!running || samples === 0 ? 'disabled' : ''}>Reset prior</button>
          </div>
        </section>

        <section class="panel-section">
          <span class="eyebrow">Dataset Flywheel</span>
          <div class="row-between">
            <span class="telemetry">Input hook: ${env.input_group_active ? 'ACTIVE' : 'LOCKED'}</span>
            ${!env.input_group_active && env.pkexec_available ? '<button id="gaze-grant-btn" class="btn btn-sm" type="button">Grant access</button>' : ''}
          </div>
          <div class="row-gap">
            <button id="gaze-rec-btn" class="btn btn-sm ${recording ? 'btn-danger' : ''}" type="button">
              ${recording ? 'Stop recording' : 'Record session'}
            </button>
            <button id="gaze-open-sessions" class="btn btn-sm" type="button">Curate sessions</button>
          </div>
        </section>
      </aside>
    </div>
  `;

  root.querySelectorAll('[data-cam-mode]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.cameraMode = btn.dataset.camMode;
      renderGaze(root);
    });
  });

  document.getElementById('gaze-toggle-btn').addEventListener('click', async () => {
    try {
      clearError();
      if (state.gaze && state.gaze.running) {
        state.gaze = await api('/v1/gaze/stop', { method: 'POST', body: '{}' });
        state.gazeTrail = [];
      } else {
        const minC = Number(document.getElementById('gaze-min-clicks').value) || 5;
        const alpha = Number(document.getElementById('gaze-ridge-alpha').value) || 0.01;
        state.gaze = await api('/v1/gaze/start', {
          method: 'POST',
          body: JSON.stringify({
            camera_mode: state.cameraMode,
            min_clicks: minC,
            ridge_alpha: alpha,
            adapt_clicks: true,
          }),
        });
      }
      updateChrome();
      renderGaze(root);
    } catch (err) {
      showError('Optical tracker error', err.message);
    }
  });

  document.getElementById('gaze-undo-btn').addEventListener('click', async () => {
    state.gaze = await api('/v1/gaze/undo', { method: 'POST', body: '{}' });
    renderGaze(root);
  });

  document.getElementById('gaze-reset-btn').addEventListener('click', async () => {
    state.gaze = await api('/v1/gaze/reset', { method: 'POST', body: '{}' });
    state.gazeTrail = [];
    renderGaze(root);
  });

  document.getElementById('gaze-rec-btn').addEventListener('click', toggleRecording);
  document.getElementById('gaze-open-sessions').addEventListener('click', () => setView('sessions'));
  const grantBtn = document.getElementById('gaze-grant-btn');
  if (grantBtn) grantBtn.addEventListener('click', requestInputPermission);

  drawGazeCanvas();
  drawAttitudeDial();
}

function drawGazeCanvas() {
  const canvas = document.getElementById('gaze-canvas');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const cs = getComputedStyle(document.documentElement);
  const surface = cs.getPropertyValue('--or-surface').trim();
  const elevated = cs.getPropertyValue('--or-surface-elevated').trim();
  const border = cs.getPropertyValue('--or-border').trim();
  const borderSoft = cs.getPropertyValue('--or-border-soft').trim();
  const accent = cs.getPropertyValue('--or-accent').trim();
  const ok = cs.getPropertyValue('--or-positive').trim();
  const muted = cs.getPropertyValue('--or-text-muted').trim();
  const text = cs.getPropertyValue('--or-text').trim();

  ctx.clearRect(0, 0, rect.width, rect.height);

  const g = state.gaze || { display: { width: 1920, height: 1080, monitors: [] } };
  const dw = Math.max(1, g.display.width || 1920);
  const dh = Math.max(1, g.display.height || 1080);

  // Fit proportional desktop bounding box with architectural margins for rulers
  const padX = 36;
  const padY = 28;
  const availW = Math.max(20, rect.width - padX * 2);
  const availH = Math.max(20, rect.height - padY * 2);
  const scale = Math.min(availW / dw, availH / dh);
  const drawW = dw * scale;
  const drawH = dh * scale;
  const ox = (rect.width - drawW) / 2;
  const oy = (rect.height - drawH) / 2;

  // Draw individual physical monitors proportionally
  const monitors = (g.display.monitors && g.display.monitors.length)
    ? g.display.monitors
    : [{ connector: 'DISPLAY', x: 0, y: 0, width: dw, height: dh, is_primary: true }];

  for (const m of monitors) {
    const mx = ox + m.x * scale;
    const my = oy + m.y * scale;
    const mw = m.width * scale;
    const mh = m.height * scale;

    ctx.fillStyle = elevated;
    ctx.fillRect(mx, my, mw, mh);

    // Subtle internal 3x3 drafting thirds
    ctx.strokeStyle = borderSoft;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 4]);
    for (const f of [1 / 3, 2 / 3]) {
      ctx.beginPath(); ctx.moveTo(mx + mw * f, my); ctx.lineTo(mx + mw * f, my + mh); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(mx, my + mh * f); ctx.lineTo(mx + mw, my + mh * f); ctx.stroke();
    }
    ctx.setLineDash([]);

    // Monitor border + corner brackets
    ctx.strokeStyle = border;
    ctx.lineWidth = 1.25;
    ctx.strokeRect(mx, my, mw, mh);

    // Connector stamp
    ctx.fillStyle = muted;
    ctx.font = '600 10px "MesloLGS Nerd Font Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(`${m.connector}  ${m.width}x${m.height}`, mx + 8, my + 16);
  }

  // Ruler ticks along top & left of desktop envelope
  ctx.fillStyle = muted;
  ctx.font = '10px "MesloLGS Nerd Font Mono", monospace';
  ctx.textAlign = 'center';
  for (const frac of [0, 0.25, 0.5, 0.75, 1]) {
    const tx = ox + drawW * frac;
    ctx.strokeStyle = borderSoft;
    ctx.beginPath(); ctx.moveTo(tx, oy - 6); ctx.lineTo(tx, oy); ctx.stroke();
    ctx.fillText(String(Math.round(dw * frac)), tx, oy - 10);
  }

  // Plot passive click adaptation samples
  const samplePts = (g.stats && g.stats.sample_points) || [];
  for (const [sx, sy] of samplePts) {
    const px = ox + sx * scale;
    const py = oy + sy * scale;
    ctx.fillStyle = ok;
    ctx.globalAlpha = 0.75;
    ctx.beginPath();
    ctx.arc(px, py, 3.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1.0;
  }

  const pred = g.prediction;
  if (!g.running || !pred || !pred.face_detected) {
    ctx.fillStyle = muted;
    ctx.font = '600 12px "MesloLGS Nerd Font Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(
      g.running ? 'AWAITING FACE LOCK' : 'PRESS START TRACKING FOR LIVE OPTICAL RETICLE',
      ox + drawW / 2,
      oy + drawH / 2
    );
    return;
  }

  // Draw fading 24-frame optical comet trail
  const trail = state.gazeTrail;
  const sigColor = pred.using_ridge ? ok : accent;
  for (let i = 1; i < trail.length; i++) {
    const a = trail[i - 1];
    const b = trail[i];
    const alpha = (i / trail.length) * 0.45;
    ctx.strokeStyle = sigColor;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = 1.5 + (i / trail.length) * 1.5;
    ctx.beginPath();
    ctx.moveTo(ox + a[0] * scale, oy + a[1] * scale);
    ctx.lineTo(ox + b[0] * scale, oy + b[1] * scale);
    ctx.stroke();
  }
  ctx.globalAlpha = 1.0;

  // Precision Optical Reticle
  const gx = ox + pred.x * scale;
  const gy = oy + pred.y * scale;
  const radius = pred.blinking ? 24 : 17;

  // Soft signal halo
  ctx.fillStyle = sigColor;
  ctx.globalAlpha = 0.14;
  ctx.beginPath();
  ctx.arc(gx, gy, radius + 6, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = 1.0;

  // Outer calibrated ring + cardinal ticks
  ctx.strokeStyle = sigColor;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(gx, gy, radius, 0, Math.PI * 2);
  ctx.stroke();

  for (const [dx1, dy1, dx2, dy2] of [
    [-radius - 6, 0, -radius + 3, 0],
    [radius - 3, 0, radius + 6, 0],
    [0, -radius - 6, 0, -radius + 3],
    [0, radius - 3, 0, radius + 6],
  ]) {
    ctx.beginPath();
    ctx.moveTo(gx + dx1, gy + dy1);
    ctx.lineTo(gx + dx2, gy + dy2);
    ctx.stroke();
  }

  // Center pip
  ctx.fillStyle = text;
  ctx.beginPath();
  ctx.arc(gx, gy, 3, 0, Math.PI * 2);
  ctx.fill();

  // Coordinate callout tag
  const tag = `${Math.round(pred.x)}, ${Math.round(pred.y)}`;
  ctx.font = '700 10px "MesloLGS Nerd Font Mono", monospace';
  const tw = ctx.measureText(tag).width + 10;
  const tx = Math.min(ox + drawW - tw - 4, gx + radius + 10);
  const ty = Math.max(oy + 14, gy - 10);
  ctx.fillStyle = surface;
  ctx.strokeStyle = border;
  ctx.lineWidth = 1;
  ctx.fillRect(tx, ty - 11, tw, 16);
  ctx.strokeRect(tx, ty - 11, tw, 16);
  ctx.fillStyle = text;
  ctx.textAlign = 'left';
  ctx.fillText(tag, tx + 5, ty);
}

function drawAttitudeDial() {
  const canvas = document.getElementById('attitude-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const r = w / 2 - 4;
  ctx.clearRect(0, 0, w, h);

  const cs = getComputedStyle(document.documentElement);
  const borderSoft = cs.getPropertyValue('--or-border-soft').trim();
  const accent = cs.getPropertyValue('--or-accent').trim();
  const muted = cs.getPropertyValue('--or-text-muted').trim();

  // Crosshair axes
  ctx.strokeStyle = borderSoft;
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r); ctx.stroke();

  const pred = state.gaze && state.gaze.prediction;
  if (!pred || !pred.face_detected) {
    ctx.fillStyle = muted;
    ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.fill();
    return;
  }

  // Horizon line tilted by roll
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(-pred.roll);
  ctx.strokeStyle = muted;
  ctx.lineWidth = 1.2;
  ctx.beginPath(); ctx.moveTo(-r + 6, 0); ctx.lineTo(r - 6, 0); ctx.stroke();
  ctx.restore();

  // Yaw / Pitch bubble
  const bx = cx + Math.max(-1, Math.min(1, pred.yaw * 1.8)) * (r - 8);
  const by = cy + Math.max(-1, Math.min(1, pred.pitch * 1.8)) * (r - 8);
  ctx.fillStyle = accent;
  ctx.beginPath();
  ctx.arc(bx, by, 5, 0, Math.PI * 2);
  ctx.fill();
}

// ---- SESSIONS WORKBENCH ------------------------------------------------

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
  if (!sid) { state.sessionDetail = null; return; }
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
    <div class="viewport-bar">
      <div class="row-gap">
        <span class="eyebrow">Phase 1 Recall Gate (>95%)</span>
        <span class="badge ${r.gate_met ? 'ok' : r.clicks > 0 ? 'warn' : ''}">
          ${r.overall_recall != null ? `${(r.overall_recall * 100).toFixed(1)}% (${r.hits}/${r.clicks})` : 'NO DATA'}
        </span>
      </div>
      <div class="row-gap">
        <button id="sess-sample-btn" class="btn btn-sm" type="button">Create sample</button>
        <button id="sess-export-btn" class="btn btn-sm btn-primary" type="button" ${state.sessions.length ? '' : 'disabled'}>Export split</button>
      </div>
    </div>

    <div class="sessions-deck">
      <div class="sessions-col">
        <div class="panel-section" style="padding:12px 14px;">
          <div class="row-between">
            <span class="eyebrow">Sessions (${state.sessions.length})</span>
            <button id="sess-del-btn" class="btn btn-sm" type="button" ${state.selectedSessionId ? '' : 'disabled'}>Delete</button>
          </div>
        </div>
        <div class="list-scroll">
          ${state.sessions.map((s) => `
            <button class="list-item ${s.session_id === state.selectedSessionId ? 'selected' : ''}" data-sid="${esc(s.session_id)}" type="button">
              <strong style="font-family:var(--or-font-mono);font-size:0.76rem;">${esc(s.session_id.slice(0, 14))}</strong>
              <span class="telemetry">${s.click_count} clicks (${s.recall != null ? `${Math.round(s.recall * 100)}%` : '--'})</span>
              <span class="telemetry">${esc((s.apps || []).slice(0, 2).join(', ') || 'no apps')}</span>
            </button>
          `).join('') || '<span class="muted" style="padding:12px;">No sessions recorded.</span>'}
        </div>
      </div>

      <div class="sessions-col" style="background:var(--or-bg);">
        <div class="viewport-bar">
          <span class="eyebrow">Inspector (click box to assign target)</span>
          <span class="telemetry">${activeRow ? `#${activeRow.idx} | ${esc(screen.app || 'unknown')} | ${(screen.candidates || []).length} boxes` : 'No event'}</span>
        </div>
        <div class="viewport-stage">
          <div class="viewport-canvas-wrap">
            <canvas id="session-canvas" style="cursor:crosshair;"></canvas>
          </div>
        </div>
        <div class="telemetry-strip">
          <div class="telemetry-cell"><span class="eyebrow">top1</span><span class="telemetry">${fmtMetric(state.sessionDetail && state.sessionDetail.metrics && state.sessionDetail.metrics.top1)}</span></div>
          <div class="telemetry-cell"><span class="eyebrow">baseline_top1</span><span class="telemetry">${fmtMetric(state.sessionDetail && state.sessionDetail.metrics && state.sessionDetail.metrics.baseline_top1)}</span></div>
          <div class="telemetry-cell"><span class="eyebrow">hit@50px</span><span class="telemetry">${fmtMetric(state.sessionDetail && state.sessionDetail.metrics && state.sessionDetail.metrics['hit@50px'])}</span></div>
          <div class="telemetry-cell"><span class="eyebrow">recall@3</span><span class="telemetry">${fmtMetric(state.sessionDetail && state.sessionDetail.metrics && state.sessionDetail.metrics['candidate_recall@3'])}</span></div>
        </div>
      </div>

      <div class="sessions-col">
        <div class="panel-section" style="padding:12px 14px;">
          <div class="row-between">
            <span class="eyebrow">Events (${rows.length})</span>
            <select id="sess-filter">
              <option value="all" ${state.eventFilter === 'all' ? 'selected' : ''}>All</option>
              <option value="clicks" ${state.eventFilter === 'clicks' ? 'selected' : ''}>Clicks</option>
              <option value="missed" ${state.eventFilter === 'missed' ? 'selected' : ''}>Missed</option>
              <option value="rejected" ${state.eventFilter === 'rejected' ? 'selected' : ''}>Rejected</option>
            </select>
          </div>
        </div>
        <div class="list-scroll">
          ${rows.map((row, pos) => {
            const it = row.ev.interaction || {};
            const tag = it.accepted === true ? 'OK' : it.accepted === false ? 'REJ' : 'NEW';
            const hit = it.kind === 'click' ? (row.covered ? ' [HIT]' : ' [MISS]') : '';
            return `
              <button class="list-item ${pos === state.selectedEventIdx ? 'selected' : ''}" data-pos="${pos}" type="button">
                <span class="telemetry">#${String(row.idx).padStart(3, '0')} ${esc(it.kind || 'frame')} ${tag}${hit}</span>
              </button>
            `;
          }).join('') || '<span class="muted" style="padding:12px;">No matching events.</span>'}
        </div>
        <div class="panel-section" style="border-top:1px solid var(--or-border-soft);padding:14px;">
          <div class="telemetry">Target: ${esc(inter.target || 'none')}</div>
          <div class="telemetry">Accepted: ${esc(String(inter.accepted))}</div>
          <div class="row-gap">
            <button id="ev-accept-btn" class="btn btn-sm" type="button" ${activeRow ? '' : 'disabled'}>Accept (A)</button>
            <button id="ev-reject-btn" class="btn btn-sm" type="button" ${activeRow ? '' : 'disabled'}>Reject (R)</button>
            <button id="ev-clear-btn" class="btn btn-sm" type="button" ${activeRow ? '' : 'disabled'}>Clear</button>
            <button id="ev-del-btn" class="btn btn-sm btn-danger" type="button" ${activeRow ? '' : 'disabled'}>Del</button>
          </div>
        </div>
      </div>
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

function fmtMetric(v) { return v == null ? 'n/a' : Number(v).toFixed(3); }

let lastSessionCoordSize = [1280, 720];

function drawSessionCanvas() {
  const canvas = document.getElementById('session-canvas');
  if (!canvas) return;
  const rows = filteredSessionEvents();
  const activeRow = rows[state.selectedEventIdx] || null;
  const ev = activeRow ? activeRow.ev : null;

  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const cs = getComputedStyle(document.documentElement);
  const elevated = cs.getPropertyValue('--or-surface-elevated').trim();
  const border = cs.getPropertyValue('--or-border').trim();
  const muted = cs.getPropertyValue('--or-text-muted').trim();
  const ok = cs.getPropertyValue('--or-positive').trim();
  const err = cs.getPropertyValue('--or-negative').trim();
  const warn = cs.getPropertyValue('--or-warning').trim();

  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!ev) {
    ctx.fillStyle = muted;
    ctx.font = '600 12px "MesloLGS Nerd Font Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText('SELECT AN EVENT TO INSPECT CANDIDATES', rect.width / 2, rect.height / 2);
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
      ctx.fillStyle = isTarget ? 'rgba(67, 183, 119, 0.2)' : 'rgba(130, 125, 118, 0.08)';
      ctx.fillRect(b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy);
      ctx.strokeRect(b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy);
    }
    if (pred.pixel && pred.pixel.length === 2) {
      ctx.strokeStyle = warn; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(pred.pixel[0] * sx, pred.pixel[1] * sy, 7, 0, Math.PI * 2); ctx.stroke();
    }
    if (inter.mouse && inter.mouse.length === 2) {
      const mx = inter.mouse[0] * sx; const my = inter.mouse[1] * sy;
      ctx.strokeStyle = activeRow.covered ? ok : err; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(mx - 9, my); ctx.lineTo(mx + 9, my); ctx.moveTo(mx, my - 9); ctx.lineTo(mx, my + 9); ctx.stroke();
    }
  };

  if (screen.screenshot && state.selectedSessionId) {
    const img = new Image();
    img.onload = () => {
      ctx.drawImage(img, 0, 0, rect.width, rect.height);
      renderOverlays(img.naturalWidth || 1920, img.naturalHeight || 1080);
    };
    img.onerror = () => { ctx.fillStyle = elevated; ctx.fillRect(0, 0, rect.width, rect.height); renderOverlays(1280, 720); };
    img.src = `/v1/sessions/${encodeURIComponent(state.selectedSessionId)}/blobs/${encodeURIComponent(screen.screenshot)}.png`;
  } else {
    let mw = 1280, mh = 720;
    for (const c of cands) { if (c.bbox) { mw = Math.max(mw, c.bbox[0] + c.bbox[2] + 40); mh = Math.max(mh, c.bbox[1] + c.bbox[3] + 40); } }
    if (inter.mouse) { mw = Math.max(mw, inter.mouse[0] + 60); mh = Math.max(mh, inter.mouse[1] + 60); }
    ctx.fillStyle = elevated; ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.strokeStyle = border; ctx.strokeRect(0.5, 0.5, rect.width - 1, rect.height - 1);
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
    await api(`/v1/sessions/${encodeURIComponent(state.selectedSessionId)}/events/${activeRow.idx}`, { method: 'DELETE' });
    await loadSessionDetail(state.selectedSessionId);
    state.recall = await api('/v1/sessions/recall').catch(() => state.recall);
    updateChrome();
    renderView();
  } catch (err) {
    showError('Could not delete event', err.message);
  }
}

// ---- ACTIVITY & SETTINGS -----------------------------------------------

function renderActivity(root) {
  const q = state.historyQuery.trim().toLowerCase();
  const items = (state.history || []).filter((it) =>
    !q || `${it.transcript || ''} ${it.route || ''} ${it.target || ''} ${it.result || ''}`.toLowerCase().includes(q)
  );
  root.innerHTML = `
    <div class="viewport-bar">
      <span class="eyebrow">Action history (${items.length})</span>
      <input id="act-search" type="text" placeholder="Filter activity..." value="${esc(state.historyQuery)}" style="width:260px;">
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
    ` : '<div class="panel-section"><span class="muted">No voice or browser actions recorded yet.</span></div>'}
  `;
  document.getElementById('act-search').addEventListener('input', (e) => {
    state.historyQuery = e.target.value;
    renderActivity(root);
    const inp = document.getElementById('act-search');
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  });
}

function renderSettings(root) {
  const groups = new Map();
  for (const spec of state.schema) {
    const g = spec.group || 'General';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(spec);
  }
  const env = (state.gaze && state.gaze.environment) || {};
  const model = state.models || {};

  root.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));border-bottom:1px solid var(--or-border-soft);">
      <section class="panel-section" style="border-right:1px solid var(--or-border-soft);background:var(--or-surface);">
        <span class="eyebrow">Permissions & Offline Models</span>
        <div class="field-row">
          <div class="field-label">
            <strong>Linux input capture (/dev/input/event*)</strong>
            <small>${env.input_group_active ? 'Active and readable for global click recording.' : 'Locked: authorize via PolicyKit or run the manual command.'}</small>
          </div>
          ${env.input_group_active ? '<span class="badge ok">Active</span>' : '<button id="set-grant-input" class="btn btn-sm" type="button">Grant access</button>'}
        </div>
        <div class="field-row">
          <div class="field-label">
            <strong>Speech-to-text (${esc(model.name || 'Parakeet Unified EN')})</strong>
            <small>Status: ${esc(model.state || 'unknown')}</small>
          </div>
          ${model.ready ? '<span class="badge ok">Ready</span>' : '<button id="set-dl-model" class="btn btn-sm" type="button">Download</button>'}
        </div>
      </section>
      ${Array.from(groups.entries()).map(([groupName, specs]) => `
        <section class="panel-section" style="border-right:1px solid var(--or-border-soft);background:var(--or-surface);">
          <span class="eyebrow">${esc(groupName)}</span>
          ${specs.map((spec) => renderSpecControl(spec, state.config[spec.key])).join('')}
        </section>
      `).join('')}
    </div>
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
      const val = vtype === 'bool' ? el.checked : vtype === 'int' ? parseInt(el.value, 10) : vtype === 'float' ? parseFloat(el.value) : el.value;
      try {
        clearError();
        const res = await api('/v1/ui/action', { method: 'POST', body: JSON.stringify({ action: 'set_config', arg: { [key]: val } }) });
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
    inputHtml = `<select data-setting-key="${esc(spec.key)}" data-setting-type="str">${spec.options.map((opt) => `<option value="${esc(opt)}" ${opt === val ? 'selected' : ''}>${esc(opt)}</option>`).join('')}</select>`;
  } else if (spec.value_type === 'int' || spec.value_type === 'float') {
    inputHtml = `<input type="number" data-setting-key="${esc(spec.key)}" data-setting-type="${esc(spec.value_type)}" value="${esc(val)}" ${spec.minimum != null ? `min="${spec.minimum}"` : ''} ${spec.maximum != null ? `max="${spec.maximum}"` : ''} ${spec.value_type === 'float' ? 'step="0.05"' : ''} style="width:96px;">`;
  } else {
    inputHtml = `<input type="text" data-setting-key="${esc(spec.key)}" data-setting-type="str" value="${esc(val ?? '')}" style="width:200px;">`;
  }
  return `
    <div class="field-row">
      <div class="field-label"><strong>${esc(spec.label)}${badge}</strong><small>${esc(spec.help || '')}</small></div>
      ${inputHtml}
    </div>
  `;
}

// ---- SHARED ACTIONS & SSE STREAMS --------------------------------------

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
  } catch {}

  try {
    const gazeSrc = new EventSource('/v1/gaze/stream');
    gazeSrc.addEventListener('gaze', (evt) => {
      try {
        state.gaze = JSON.parse(evt.data);
        const pred = state.gaze.prediction;
        if (state.gaze.running && pred && pred.face_detected) {
          state.gazeTrail.push([pred.x, pred.y]);
          if (state.gazeTrail.length > 24) state.gazeTrail.shift();
        }
        updateChrome();
        if (state.view === 'gaze') {
          drawGazeCanvas();
          drawAttitudeDial();
          const st = state.gaze.stats || {};
          const setTxt = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
          setTxt('gaze-coords', pred && pred.face_detected ? `X ${String(Math.round(pred.x)).padStart(4, '0')}  Y ${String(Math.round(pred.y)).padStart(4, '0')}` : 'X ----  Y ----');
          setTxt('gaze-fps', st.measured_fps ? String(st.measured_fps) : '--');
          setTxt('gaze-frame-counts', `${st.frames_read || 0} frames / ${st.faces_detected || 0} faces`);
          setTxt('gaze-adapt-state', st.using_ridge ? 'RIDGE' : 'PRIOR');
          setTxt('gaze-adapt-count', `${st.samples || 0} / ${st.min_clicks || 5} clicks`);
          setTxt('gaze-skew', pred && state.gaze.mode === 'binocular' ? `${pred.skew_ms.toFixed(1)}ms` : 'N/A');
          setTxt('att-yaw', `YAW   ${pred ? `${(pred.yaw * 57.3).toFixed(1)} deg` : '--'}`);
          setTxt('att-pitch', `PITCH ${pred ? `${(pred.pitch * 57.3).toFixed(1)} deg` : '--'}`);
          setTxt('att-roll', `ROLL  ${pred ? `${(pred.roll * 57.3).toFixed(1)} deg` : '--'}`);
          setTxt('att-blink', `BLINK ${pred && pred.blinking ? 'CLOSED' : 'OPEN'}`);
          const led = document.getElementById('gaze-live-led');
          if (led) led.className = 'led ' + (state.gaze.running ? (pred && pred.face_detected ? 'ok' : 'warn') : '');
        }
      } catch {}
    });
  } catch {}
}

function initEvents() {
  document.querySelectorAll('.nav-btn').forEach((btn) => {
    btn.addEventListener('click', () => setView(btn.dataset.view));
  });
  document.getElementById('header-listen-btn').addEventListener('click', toggleListening);
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
