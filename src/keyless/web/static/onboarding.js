// Keyless onboarding: one thing per screen, no jargon, auto-advance when it can.
// Steps: hello -> camera -> ready -> (countdown) -> run -> solving -> done | retry
// The "light" is the same stimulus the native stage draws (overlay_wayland.c):
// amber halo, 3-arm inward spiral, ivory pip. Keep the two in sync.

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const STEPS = ['hello', 'camera', 'ready', 'run', 'done'];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------------------------------------------------------------------------
// Orb: WebGL fragment shader, canvas2D fallback
// ---------------------------------------------------------------------------

const FRAG = `
precision mediump float;
uniform vec2 uRes;
uniform float uTime;
uniform float uSize;
uniform float uBurst;
uniform float uDim;
const vec3 AMBER = vec3(0.886, 0.478, 0.231);
const vec3 IVORY = vec3(0.957, 0.953, 0.875);
const vec3 TERRA = vec3(0.42, 0.16, 0.07);
void main() {
  vec2 p = (gl_FragCoord.xy - 0.5 * uRes) / (0.5 * min(uRes.x, uRes.y));
  float R = 0.42 * uSize * (1.0 + 0.04 * sin(uTime * 2.1));
  float d = length(p);
  float rho = d / max(R, 1e-4);
  vec4 c = vec4(0.0);
  // halo
  float halo = exp(-rho * rho * 1.6) * 0.42;
  c = vec4(AMBER * halo, halo) + c * (1.0 - halo);
  // spiral
  if (rho < 1.05) {
    float th = atan(p.y, p.x);
    float s = sin(3.0 * th + 17.0 * sqrt(rho) - 5.5 * uTime);
    float band = smoothstep(-0.25, 0.25, s);
    float edge = smoothstep(1.03, 0.80, rho);
    float core = 1.0 - rho;
    vec3 bc = mix(AMBER, IVORY, core * core);
    float a1 = 0.55 * edge;
    c = vec4(TERRA * a1, a1) + c * (1.0 - a1);
    float a2 = band * edge * 0.92;
    c = vec4(bc * a2, a2) + c * (1.0 - a2);
  }
  // burst ring
  if (uBurst > 0.0 && uBurst < 1.0) {
    float rr = R * (1.0 + 1.3 * uBurst);
    float w = (2.0 + 3.0 * (1.0 - uBurst)) / (0.5 * min(uRes.x, uRes.y));
    float ring = smoothstep(w, 0.0, abs(d - rr)) * (1.0 - uBurst) * (1.0 - uBurst) * 0.9;
    c = vec4(IVORY * ring, ring) + c * (1.0 - ring);
  }
  // pip
  float px = 1.0 / (0.5 * min(uRes.x, uRes.y));
  float pip = max(2.4 * px, R * 0.045);
  float ringA = smoothstep(pip + 2.6 * px, pip + 1.2 * px, d) * 0.85 * step(0.02, uSize);
  c = vec4(vec3(0.05, 0.03, 0.02) * ringA, ringA) + c * (1.0 - ringA);
  float dot = smoothstep(pip + 0.6 * px, pip - 0.6 * px, d) * step(0.02, uSize);
  c = vec4(IVORY * dot, dot) + c * (1.0 - dot);
  gl_FragColor = c * uDim;
}`;

const VERT = `attribute vec2 a; void main(){ gl_Position = vec4(a, 0.0, 1.0); }`;

export class Orb {
  constructor(canvas) {
    this.canvas = canvas;
    this.size = 1.0;
    this.burst = -1;
    this.dim = 1.0;
    this.speed = REDUCED ? 0.25 : 1.0;
    this.t = 0;
    this._last = performance.now();
    this.gl = canvas.getContext('webgl', { premultipliedAlpha: true, alpha: true, antialias: true });
    if (this.gl) this._initGL();
    this._raf = 0;
  }

  _initGL() {
    const gl = this.gl;
    const sh = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { this.gl = null; return; }
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, 'a');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    this.u = {
      res: gl.getUniformLocation(prog, 'uRes'),
      time: gl.getUniformLocation(prog, 'uTime'),
      size: gl.getUniformLocation(prog, 'uSize'),
      burst: gl.getUniformLocation(prog, 'uBurst'),
      dim: gl.getUniformLocation(prog, 'uDim'),
    };
  }

  _resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  frame() {
    const now = performance.now();
    this.t += ((now - this._last) / 1000) * this.speed;
    this._last = now;
    this._resize();
    const { width: w, height: h } = this.canvas;
    if (this.gl) {
      const gl = this.gl;
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform2f(this.u.res, w, h);
      gl.uniform1f(this.u.time, this.t);
      gl.uniform1f(this.u.size, this.size);
      gl.uniform1f(this.u.burst, this.burst);
      gl.uniform1f(this.u.dim, this.dim);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    } else {
      const ctx = this.canvas.getContext('2d');
      ctx.clearRect(0, 0, w, h);
      const r = 0.42 * this.size * 0.5 * Math.min(w, h);
      const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, r * 1.6);
      g.addColorStop(0, 'rgba(244,243,223,0.95)');
      g.addColorStop(0.35, 'rgba(226,122,59,0.9)');
      g.addColorStop(1, 'rgba(226,122,59,0)');
      ctx.globalAlpha = this.dim;
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(w / 2, h / 2, r * 1.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }

  start() {
    const loop = () => { this.frame(); this._raf = requestAnimationFrame(loop); };
    cancelAnimationFrame(this._raf);
    this._last = performance.now();
    loop();
  }

  stop() { cancelAnimationFrame(this._raf); }
}

// ---------------------------------------------------------------------------
// Copy (short, friendly, no jargon)
// ---------------------------------------------------------------------------

const GRADE_COPY = {
  great: ['Nailed it.', 'Your eyes and Keyless are in sync.'],
  good: ['Looking good.', 'Keyless knows your eyes now.'],
  okay: ['You’re all set.', 'It’ll sharpen up as you go.'],
};

function faceHint(pred) {
  if (!pred || !pred.face_detected) return ['Looking for you…', 'Sit in front of your camera.', false];
  const dist = Math.abs(pred.tz || 0) * 1000;
  if (dist > 0 && dist < 380) return ['A little farther back', 'Lean back into your seat.', false];
  if (dist > 950) return ['Come a little closer', 'About an arm’s length is perfect.', false];
  if (Math.abs(pred.yaw || 0) > 0.42) return ['Face the screen', 'Turn your head toward the middle.', false];
  if (Math.abs(pred.pitch || 0) > 0.42) return ['Face the screen', 'Tilt your head toward the middle.', false];
  return ['Perfect.', 'Hold that.', true];
}

function accuracyPhrase(px) {
  if (px == null) return 'Calibrated';
  if (px <= 45) return 'About the size of a button';
  if (px <= 90) return 'About the size of an icon';
  if (px <= 160) return 'About the size of a toolbar';
  return 'Good enough to pick a window';
}

// ---------------------------------------------------------------------------
// Onboarding controller
// ---------------------------------------------------------------------------

export class Onboarding {
  constructor({ onClose } = {}) {
    this.onClose = onClose || (() => {});
    this.root = null;
    this.step = 'hello';
    this.pred = null;
    this.calib = null;
    this.goodSince = 0;
    this.orb = null;
    this.runOrb = null;
    this._gazeSrc = null;
    this._calSrc = null;
    this._keys = (e) => this._onKey(e);
    this._minSolveUntil = 0;
    this.monitors = [];
  }

  static async shouldAutoOpen() {
    try {
      if (localStorage.getItem('keyless.onboard.dismissed') === '1') return false;
    } catch {}
    try {
      const prof = await api('/v1/calibration');
      return !prof.has_profile;
    } catch {
      return false;
    }
  }

  async open(step = 'hello') {
    if (this.root) return;
    const st = await api('/v1/gaze/status').catch(() => null);
    this.monitors = (st && st.display && st.display.monitors) || [];
    this.root = document.createElement('div');
    this.root.className = 'ob';
    this.root.setAttribute('role', 'dialog');
    this.root.setAttribute('aria-modal', 'true');
    this.root.setAttribute('aria-label', 'Set up eye tracking');
    this.root.innerHTML = `
      <div class="ob-glow"></div>
      <div class="ob-top">
        <div class="ob-pips">${STEPS.map((s) => `<i data-step="${s}"></i>`).join('')}</div>
        <button class="ob-close" type="button" aria-label="Close">Not now</button>
      </div>
      <div class="ob-stage"></div>`;
    document.body.appendChild(this.root);
    this.root.querySelector('.ob-close').addEventListener('click', () => this.close({ dismissed: true }));
    document.addEventListener('keydown', this._keys);
    requestAnimationFrame(() => this.root.classList.add('in'));
    this.go(step, { first: true });
  }

  close({ dismissed = false } = {}) {
    if (!this.root) return;
    if (this.calib && ['intro', 'running', 'paused', 'starting'].includes(this.calib.state)) {
      api('/v1/calibration/cancel', { method: 'POST', body: {} }).catch(() => {});
    }
    if (dismissed) {
      try { localStorage.setItem('keyless.onboard.dismissed', '1'); } catch {}
    }
    this._stopStreams();
    this._unwatchViewport();
    if (this.orb) this.orb.stop();
    if (this.runOrb) this.runOrb.stop();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    document.removeEventListener('keydown', this._keys);
    const root = this.root;
    this.root = null;
    root.classList.remove('in');
    root.classList.add('out');
    setTimeout(() => root.remove(), REDUCED ? 0 : 260);
    this.onClose();
  }

  _onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      if (this.step === 'run') this._cancelRun();
      else this.close({ dismissed: this.step === 'hello' });
    } else if (e.key === 'Enter') {
      const btn = this.root && this.root.querySelector('.ob-primary:not([disabled])');
      if (btn) { e.preventDefault(); btn.click(); }
    }
  }

  _pips(step) {
    const idx = STEPS.indexOf(step === 'retry' ? 'done' : step);
    this.root.querySelectorAll('.ob-pips i').forEach((el, i) => {
      el.classList.toggle('on', i === idx);
      el.classList.toggle('past', i < idx);
    });
  }

  go(step, { first = false } = {}) {
    if (!this.root) return;
    if (step !== 'run') this._unwatchViewport();
    this.step = step;
    this._pips(step);
    this.root.dataset.step = step;
    const stage = this.root.querySelector('.ob-stage');
    const old = stage.querySelector('.ob-card');
    const card = document.createElement('div');
    card.className = 'ob-card';
    card.innerHTML = this[`_${step}`]();
    if (old && !first && !REDUCED) {
      old.classList.add('leave');
      setTimeout(() => old.remove(), 200);
    } else if (old) {
      old.remove();
    }
    stage.appendChild(card);
    requestAnimationFrame(() => requestAnimationFrame(() => card.classList.add('enter')));
    this[`_wire_${step}`] && this[`_wire_${step}`](card);
  }

  // --- streams -------------------------------------------------------------

  _ensureGazeStream() {
    if (this._gazeSrc) return;
    this._gazeSrc = new EventSource('/v1/gaze/stream');
    this._gazeSrc.addEventListener('gaze', (evt) => {
      try {
        const d = JSON.parse(evt.data);
        this.pred = d.prediction;
        if (this.step === 'camera') this._updateCamera();
      } catch {}
    });
  }

  _ensureCalStream() {
    if (this._calSrc) return;
    this._calSrc = new EventSource('/v1/calibration/stream');
    this._calSrc.addEventListener('calibration', (evt) => {
      try { this._onCalibration(JSON.parse(evt.data)); } catch {}
    });
  }

  _stopStreams() {
    if (this._gazeSrc) { this._gazeSrc.close(); this._gazeSrc = null; }
    if (this._calSrc) { this._calSrc.close(); this._calSrc = null; }
  }

  // --- hello ---------------------------------------------------------------

  _hello() {
    return `
      <div class="ob-hero"><canvas class="ob-orb"></canvas></div>
      <h1 class="ob-title">Hi there.</h1>
      <p class="ob-sub">Let’s teach Keyless your eyes.<br>It takes about a minute.</p>
      <div class="ob-actions">
        <button class="ob-btn ob-primary" type="button">Let’s go</button>
      </div>`;
  }

  _wire_hello(card) {
    this._mountOrb(card.querySelector('.ob-orb'), { size: 0.95 });
    card.querySelector('.ob-primary').addEventListener('click', () => this.go('camera'));
  }

  _mountOrb(canvas, { size = 1 } = {}) {
    if (this.orb) this.orb.stop();
    this.orb = new Orb(canvas);
    this.orb.size = 0;
    this.orb.start();
    // Spring the orb open.
    const t0 = performance.now();
    const grow = () => {
      const u = Math.min(1, (performance.now() - t0) / (REDUCED ? 1 : 700));
      const k = 1 - Math.pow(1 - u, 3);
      const over = Math.sin(u * Math.PI) * 0.06;
      this.orb.size = size * (k + over);
      if (u < 1) requestAnimationFrame(grow);
    };
    grow();
  }

  // --- camera --------------------------------------------------------------

  _camera() {
    return `
      <div class="ob-hero">
        <div class="ob-face">
          <svg class="ob-face-ring" viewBox="0 0 220 220" aria-hidden="true">
            <circle class="track" cx="110" cy="110" r="100"></circle>
            <circle class="fill" cx="110" cy="110" r="100" pathLength="1"></circle>
          </svg>
          <div class="ob-face-puck"><i></i><i></i></div>
          <svg class="ob-check" viewBox="0 0 52 52" aria-hidden="true"><path d="M14 27 l8 8 l16 -18" pathLength="1"/></svg>
        </div>
      </div>
      <h1 class="ob-title" id="ob-cam-title">Looking for you…</h1>
      <p class="ob-sub" id="ob-cam-sub">Starting your camera.</p>
      <div class="ob-actions"><button class="ob-btn ob-ghost" type="button" id="ob-cam-skip" hidden>Continue anyway</button></div>`;
  }

  async _wire_camera(card) {
    if (this.orb) this.orb.stop();
    this.goodSince = 0;
    card.querySelector('#ob-cam-skip').addEventListener('click', () => this.go('ready'));
    setTimeout(() => {
      const b = card.querySelector('#ob-cam-skip');
      if (b && this.step === 'camera') b.hidden = false;
    }, 9000);
    try {
      const st = await api('/v1/gaze/status');
      if (!st.running) await api('/v1/gaze/start', { method: 'POST', body: { camera_mode: 'auto' } });
    } catch (err) {
      this._cameraError(String(err.message || err));
      return;
    }
    this._ensureGazeStream();
  }

  _cameraError(msg) {
    const card = this.root && this.root.querySelector('.ob-card:not(.leave)');
    if (!card) return;
    const noCam = /camera|webcam|video/i.test(msg);
    card.querySelector('#ob-cam-title').textContent = noCam ? 'No camera found' : 'Camera didn’t start';
    card.querySelector('#ob-cam-sub').textContent = noCam
      ? 'Plug in a webcam, then try again.'
      : 'Close other apps using the camera, then try again.';
    const actions = card.querySelector('.ob-actions');
    actions.innerHTML = '<button class="ob-btn ob-primary" type="button">Try again</button>';
    actions.querySelector('button').addEventListener('click', () => this.go('camera'));
    this.root.querySelector('.ob-face').classList.add('err');
  }

  _updateCamera() {
    const card = this.root && this.root.querySelector('.ob-card:not(.leave)');
    if (!card) return;
    const p = this.pred;
    const [title, sub, good] = faceHint(p);
    const t = card.querySelector('#ob-cam-title');
    if (t.textContent !== title) {
      t.textContent = title;
      t.classList.remove('pop');
      void t.offsetWidth;
      t.classList.add('pop');
    }
    card.querySelector('#ob-cam-sub').textContent = sub;
    const face = card.querySelector('.ob-face');
    const puck = card.querySelector('.ob-face-puck');
    if (p && p.face_detected) {
      // Mirror horizontally so moving left moves the puck left.
      const dx = Math.max(-1, Math.min(1, -(p.yaw || 0) / 0.6)) * 46;
      const dy = Math.max(-1, Math.min(1, (p.pitch || 0) / 0.6)) * 46;
      const dist = Math.abs(p.tz || 0) * 1000 || 600;
      const scale = Math.max(0.55, Math.min(1.45, 600 / dist));
      puck.style.transform = `translate(${dx}px, ${dy}px) scale(${scale})`;
      puck.classList.add('seen');
      puck.classList.toggle('blink', !!p.blinking);
    } else {
      puck.classList.remove('seen');
    }
    const now = performance.now();
    if (good) {
      if (!this.goodSince) this.goodSince = now;
      const u = Math.min(1, (now - this.goodSince) / 1000);
      face.style.setProperty('--fill', String(u));
      if (u >= 1 && !face.classList.contains('ok')) {
        face.classList.add('ok');
        setTimeout(() => this.step === 'camera' && this.go('ready'), REDUCED ? 200 : 750);
      }
    } else if (!face.classList.contains('ok')) {
      this.goodSince = 0;
      face.style.setProperty('--fill', '0');
    }
  }

  // --- ready ---------------------------------------------------------------

  _ready() {
    const n = Math.max(1, this.monitors.length);
    const secs = Math.round(n * 10 + 2);
    return `
      <div class="ob-hero"><div class="ob-map" id="ob-map"></div></div>
      <h1 class="ob-title">Follow the light.</h1>
      <p class="ob-sub">Just watch it with your eyes. Keep your head relaxed.<br>
        Your screen${n > 1 ? 's' : ''} will dim for about ${secs} seconds.</p>
      <div class="ob-actions">
        <button class="ob-btn ob-primary" type="button">Start</button>
      </div>`;
  }

  _wire_ready(card) {
    this._drawPreviewMap(card.querySelector('#ob-map'));
    card.querySelector('.ob-primary').addEventListener('click', () => this._countdown());
  }

  _drawPreviewMap(host) {
    const mons = this.monitors.length ? this.monitors : [{ x: 0, y: 0, width: 1920, height: 1080, connector: 'Display' }];
    const minX = Math.min(...mons.map((m) => m.x));
    const minY = Math.min(...mons.map((m) => m.y));
    const maxX = Math.max(...mons.map((m) => m.x + m.width));
    const maxY = Math.max(...mons.map((m) => m.y + m.height));
    const W = 340, H = 150;
    const s = Math.min(W / (maxX - minX), H / (maxY - minY));
    const ox = (W - (maxX - minX) * s) / 2, oy = (H - (maxY - minY) * s) / 2;
    const ordered = [...mons].sort((a, b) => a.y - b.y || a.x - b.x);
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">
      ${mons.map((m) => `<rect class="mon" x="${ox + (m.x - minX) * s + 3}" y="${oy + (m.y - minY) * s + 3}"
        width="${m.width * s - 6}" height="${m.height * s - 6}" rx="7"></rect>`).join('')}
      <circle class="spark-halo" r="13"></circle><circle class="spark" r="4.5"></circle>
    </svg>`;
    const halo = host.querySelector('.spark-halo');
    const dot = host.querySelector('.spark');
    const per = 2600;
    const t0 = performance.now();
    const anim = () => {
      if (!host.isConnected) return;
      const t = (performance.now() - t0) % (per * ordered.length);
      const m = ordered[Math.floor(t / per)];
      const u = (t % per) / per;
      const cx = ox + (m.x - minX + m.width / 2) * s;
      const cy = oy + (m.y - minY + m.height / 2) * s;
      const x = cx + Math.sin(2 * Math.PI * u) * m.width * s * 0.34;
      const y = cy + Math.sin(4 * Math.PI * u) * m.height * s * 0.3;
      halo.setAttribute('cx', x); halo.setAttribute('cy', y);
      dot.setAttribute('cx', x); dot.setAttribute('cy', y);
      if (!REDUCED) requestAnimationFrame(anim);
    };
    anim();
  }

  async _countdown() {
    // Fullscreen needs this user gesture; only the browser stage uses it,
    // but requesting now keeps the option open.
    const wantFs = !!document.documentElement.requestFullscreen;
    const stage = this.root.querySelector('.ob-stage');
    const card = stage.querySelector('.ob-card');
    card.classList.add('leave');
    const cd = document.createElement('div');
    cd.className = 'ob-count';
    stage.appendChild(cd);
    for (const n of ['3', '2', '1']) {
      cd.innerHTML = `<span>${n}</span>`;
      await new Promise((r) => setTimeout(r, REDUCED ? 450 : 650));
      if (!this.root) return;
    }
    cd.remove();
    card.remove();
    this.calib = { state: 'starting' };
    this.go('run');
    try {
      const res = await api('/v1/calibration/start', { method: 'POST', body: {} });
      this._onCalibration(res);
      if (res.mode === 'browser') {
        this._watchViewport();
        if (wantFs && !document.fullscreenElement) {
          document.documentElement.requestFullscreen().catch(() => {}).finally(() => this._reportViewport());
        }
      }
    } catch (err) {
      this.calib = { state: 'failed', message: String(err.message || err) };
      this.go('retry');
      return;
    }
    this._ensureCalStream();
  }

  // --- browser-stage viewport ------------------------------------------------
  // The light is placed in display fractions, so labels are only right while
  // the page covers the whole display. Maximized is not enough (menu bar, tabs).

  _viewportIsScreen() {
    if (!document.documentElement.requestFullscreen) return true; // cannot do better here
    return !!document.fullscreenElement
      && Math.abs(window.innerWidth - screen.width) <= 2
      && Math.abs(window.innerHeight - screen.height) <= 2;
  }

  _reportViewport() {
    if (!this.root || this.step !== 'run') return;
    api('/v1/calibration/viewport', { method: 'POST', body: { fullscreen: this._viewportIsScreen() } }).catch(() => {});
  }

  _watchViewport() {
    if (this._viewportWatch) return;
    this._viewportWatch = () => this._reportViewport();
    document.addEventListener('fullscreenchange', this._viewportWatch);
    window.addEventListener('resize', this._viewportWatch);
    this._reportViewport();
  }

  _unwatchViewport() {
    if (!this._viewportWatch) return;
    document.removeEventListener('fullscreenchange', this._viewportWatch);
    window.removeEventListener('resize', this._viewportWatch);
    this._viewportWatch = null;
  }

  // --- run -----------------------------------------------------------------

  _run() {
    return `
      <div class="ob-run">
        <canvas class="ob-run-orb"></canvas>
        <div class="ob-run-hud">
          <svg class="ob-progress" viewBox="0 0 44 44" aria-hidden="true">
            <circle class="track" cx="22" cy="22" r="18"></circle>
            <circle class="fill" cx="22" cy="22" r="18" pathLength="1"></circle>
          </svg>
          <div>
            <strong id="ob-run-title">Follow the light</strong>
            <span id="ob-run-sub">Press Esc to stop</span>
          </div>
          <button class="ob-btn ob-primary" type="button" id="ob-run-fs" hidden>Full screen</button>
        </div>
      </div>`;
  }

  _wire_run(card) {
    card.querySelector('#ob-run-fs').addEventListener('click', () => {
      document.documentElement.requestFullscreen().catch(() => {}).finally(() => this._reportViewport());
    });
    if (this.orb) this.orb.stop();
    this.runOrb = new Orb(card.querySelector('.ob-run-orb'));
    this.runOrb.size = 0;
  }

  _onCalibration(c) {
    if (!this.root) return;
    const prev = this.calib ? this.calib.state : '';
    this.calib = c;
    if (this.step === 'run') {
      const card = this.root.querySelector('.ob-card:not(.leave)');
      if (!card) return;
      this.root.dataset.mode = c.mode || 'overlay';
      card.style.setProperty('--p', String(c.progress || 0));
      const title = card.querySelector('#ob-run-title');
      const sub = card.querySelector('#ob-run-sub');
      const fsBtn = card.querySelector('#ob-run-fs');
      if (fsBtn) fsBtn.hidden = !(c.state === 'paused' && c.message === 'not_fullscreen');
      if (c.state === 'paused' && c.message === 'not_fullscreen') {
        title.textContent = 'Go full screen';
        sub.textContent = 'The light needs the whole screen.';
      } else if (c.state === 'paused') {
        title.textContent = 'Where did you go?';
        sub.textContent = 'Look back at the screen to keep going.';
      } else if (c.state === 'solving') {
        title.textContent = 'Tuning to your eyes…';
        sub.textContent = '';
      } else {
        title.textContent = 'Follow the light';
        sub.textContent = 'Press Esc to stop';
      }
      card.classList.toggle('paused', c.state === 'paused');
      if (c.mode === 'browser' && this.runOrb) this._placeBrowserOrb(card, c);
      if (c.state === 'solving' && prev !== 'solving') this._minSolveUntil = performance.now() + 900;
    }
    if (['done', 'retry', 'failed'].includes(c.state) && this.step === 'run') {
      const wait = Math.max(0, this._minSolveUntil - performance.now());
      setTimeout(() => {
        if (!this.root || this.step !== 'run') return;
        if (this.runOrb) this.runOrb.stop();
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        if (this._calSrc) { this._calSrc.close(); this._calSrc = null; }
        this.go(c.state === 'done' ? 'done' : 'retry');
      }, wait);
    } else if (c.state === 'cancelled' && this.step === 'run') {
      if (this.runOrb) this.runOrb.stop();
      this.go('ready');
    }
  }

  _placeBrowserOrb(card, c) {
    const t = c.target || {};
    const canvas = card.querySelector('.ob-run-orb');
    if (!this.runOrb._raf) this.runOrb.start();
    const side = 300;
    canvas.style.width = `${side}px`;
    canvas.style.height = `${side}px`;
    const x = (t.u == null ? 0.5 : t.u) * window.innerWidth;
    const y = (t.v == null ? 0.5 : t.v) * window.innerHeight;
    canvas.style.transform = `translate(${x - side / 2}px, ${y - side / 2}px)`;
    // Stage `size` is in units where 1.0 = 120 px radius; the shader's unit
    // radius is 0.42 * side / 2 = 63 px, so scale to match the native stage.
    this.runOrb.size = (t.size || 0) * (120 / 63);
    this.runOrb.burst = t.burst == null ? -1 : t.burst;
    this.runOrb.dim = c.state === 'paused' ? 0.38 : 1;
    this.runOrb.speed = c.state === 'paused' ? 0 : 1;
  }

  _cancelRun() {
    api('/v1/calibration/cancel', { method: 'POST', body: {} }).catch(() => {});
    if (this.runOrb) this.runOrb.stop();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    this.go('ready');
  }

  // --- done / retry -----------------------------------------------------------

  _done() {
    const r = (this.calib && this.calib.result) || {};
    const [title, sub] = GRADE_COPY[r.grade] || GRADE_COPY.okay;
    const screens = (r.rig && r.rig.displays) ? r.rig.displays.length : this.monitors.length || 1;
    return `
      <div class="ob-hero">
        <div class="ob-burst">
          <svg viewBox="0 0 120 120" aria-hidden="true">
            <circle class="disc" cx="60" cy="60" r="44"></circle>
            <path class="tick" d="M41 61 l13 13 l26 -28" pathLength="1"></path>
          </svg>
          ${Array.from({ length: 12 }, (_, i) => `<i style="--a:${i * 30}deg"></i>`).join('')}
        </div>
      </div>
      <h1 class="ob-title">${title}</h1>
      <p class="ob-sub">${sub}<br>From here on it learns quietly as you use your computer.</p>
      <div class="ob-stats">
        <div class="ob-stat"><b>${accuracyPhrase(r.accuracy_px)}</b><span>Accuracy${r.accuracy_deg != null ? ` · ${r.accuracy_deg.toFixed(1)}°` : ''}</span></div>
        <div class="ob-stat"><b>${r.latency_ms != null ? Math.round(r.latency_ms) + ' ms' : '—'}</b><span>Eye to cursor</span></div>
        <div class="ob-stat"><b>${screens} screen${screens > 1 ? 's' : ''}</b><span>${r.swapped ? 'Order fixed for you' : 'Arranged'}</span></div>
      </div>
      ${screens > 1 && r.rig ? '<div class="ob-rig" id="ob-rig"></div>' : ''}
      <div class="ob-actions">
        <button class="ob-btn ob-primary" type="button">Done</button>
      </div>
      <p class="ob-foot">Look around — the soft ring follows your eyes.</p>`;
  }

  _wire_done(card) {
    try { localStorage.setItem('keyless.onboard.dismissed', '1'); } catch {}
    const r = (this.calib && this.calib.result) || {};
    const host = card.querySelector('#ob-rig');
    if (host && r.rig) this._drawRig(host, r.rig.displays || []);
    card.querySelector('.ob-primary').addEventListener('click', () => this.close());
  }

  _drawRig(host, displays) {
    // Top-down view: user at the bottom, each display a segment facing them.
    const segs = displays.map((d) => {
      const y = (d.yaw_deg || 0) * Math.PI / 180;
      const hw = (d.width_mm || 500) / 2;
      return {
        name: d.connector,
        x1: d.cx_mm - hw * Math.cos(y), z1: d.cz_mm + hw * Math.sin(y),
        x2: d.cx_mm + hw * Math.cos(y), z2: d.cz_mm - hw * Math.sin(y),
        cx: d.cx_mm, cz: d.cz_mm,
      };
    });
    const xs = segs.flatMap((s) => [s.x1, s.x2]);
    const zs = segs.flatMap((s) => [s.z1, s.z2]).concat([0]);
    const W = 340, H = 130, pad = 18;
    const span = Math.max(1, Math.max(...xs) - Math.min(...xs));
    const depth = Math.max(1, Math.max(...zs) - Math.min(...zs));
    const k = Math.min((W - 2 * pad) / span, (H - 2 * pad) / depth);
    const mx = (Math.max(...xs) + Math.min(...xs)) / 2;
    const X = (x) => W / 2 + (x - mx) * k;
    const Z = (z) => H - pad - z * k;
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-label="Your screens, seen from above">
      ${segs.map((s, i) => `
        <line class="ray" x1="${X(0)}" y1="${Z(0)}" x2="${X(s.cx)}" y2="${Z(s.cz)}" style="--d:${i * 90}ms"></line>
        <line class="seg" x1="${X(s.x1)}" y1="${Z(s.z1)}" x2="${X(s.x2)}" y2="${Z(s.z2)}" style="--d:${i * 90}ms"></line>
        <text x="${X(s.cx)}" y="${Z(Math.max(s.z1, s.z2)) - 10}">${s.name}</text>`).join('')}
      <circle class="you" cx="${X(0)}" cy="${Z(0)}" r="5"></circle>
    </svg>`;
  }

  _retry() {
    const c = this.calib || {};
    const lowFace = c.message === 'not_enough_face';
    return `
      <div class="ob-hero"><div class="ob-hmm"><i></i><i></i><b></b></div></div>
      <h1 class="ob-title">Hmm, that was tricky.</h1>
      <p class="ob-sub">${lowFace ? 'We lost sight of you for most of it.' : 'Let’s give it one more go.'}</p>
      <ul class="ob-tips">
        <li>Light your face from the front, not behind</li>
        <li>Sit about an arm’s length away</li>
        <li>Move only your eyes, not your head</li>
      </ul>
      <div class="ob-actions">
        <button class="ob-btn ob-primary" type="button">Try again</button>
        <button class="ob-btn ob-ghost" type="button" id="ob-later">Skip for now</button>
      </div>`;
  }

  _wire_retry(card) {
    card.querySelector('.ob-primary').addEventListener('click', () => this.go('ready'));
    card.querySelector('#ob-later').addEventListener('click', () => this.close({ dismissed: true }));
  }
}
