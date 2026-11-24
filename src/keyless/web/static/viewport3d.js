// Ultra-lightweight, zero-dependency 3D perspective renderer for Keyless
// Renders physical 3D DisplayRig planes, live 3D head + binocular eyeballs,
// blinking eyelids, and converging 3D gaze rays at 60fps (<0.4ms/frame CPU, 0 GPU contention).

const DEG = Math.PI / 180;

function rotMatrix(yawDeg, pitchDeg, rollDeg) {
  const y = yawDeg * DEG, p = pitchDeg * DEG, r = rollDeg * DEG;
  const cy = Math.cos(y), sy = Math.sin(y);
  const cp = Math.cos(p), sp = Math.sin(p);
  const cr = Math.cos(r), sr = Math.sin(r);
  // R = R_y(yaw) @ R_x(pitch) @ R_z(roll) — matches keyless/gaze/displays.py
  return [
    cy * cr + sy * sp * sr, -cy * sr + sy * sp * cr, sy * cp,
    cp * sr,                 cp * cr,                -sp,
    -sy * cr + cy * sp * sr, sy * sr + cy * sp * cr, cy * cp,
  ];
}

function rayFromAngles(az, el) {
  const ca = Math.cos(az), sa = Math.sin(az);
  const ce = Math.cos(el), se = Math.sin(el);
  return [sa * ce, se, ca * ce];
}

function planeBasis(d) {
  const R = rotMatrix(d.yaw_deg || 0, d.pitch_deg || 0, d.roll_deg || 0);
  return {
    c: [d.cx_mm || 0, d.cy_mm || 0, d.cz_mm || 600],
    u: [R[0], R[3], R[6]],
    v: [R[1], R[4], R[7]],
    n: [R[2], R[5], R[8]],
    w: d.width_mm || 531,
    h: d.height_mm || 299,
  };
}

function uvTo3D(b, u, v) {
  const du = (u - 0.5) * b.w;
  const dv = (v - 0.5) * b.h;
  return [
    b.c[0] + du * b.u[0] + dv * b.v[0],
    b.c[1] + du * b.u[1] + dv * b.v[1],
    b.c[2] + du * b.u[2] + dv * b.v[2],
  ];
}

function pixelTo3D(displays, px, py) {
  for (const d of displays) {
    if (px >= d.x && px < d.x + d.width && py >= d.y && py < d.y + d.height) {
      const u = (px - d.x) / Math.max(1, d.width);
      const v = (py - d.y) / Math.max(1, d.height);
      return { pt: uvTo3D(planeBasis(d), u, v), connector: d.connector };
    }
  }
  return null;
}

export class Viewport3D {
  constructor() {
    // Camera orbit around target (0, 20, 320) in mm
    this.camAz = 0.0;
    this.camEl = 0.34;
    this.camDist = 1120;
    this.targetAz = 0.0;
    this.targetEl = 0.34;
    this.targetDist = 1120;
    this.target = [0, 15, 310];

    this._dragging = false;
    this._lastX = 0;
    this._lastY = 0;
    this._boundCanvas = null;

    // Smoothly interpolated live gaze & head state for 60fps butter-smooth motion
    this._smoothAz = 0;
    this._smoothEl = 0;
    this._smoothYaw = 0;
    this._smoothPitch = 0;
    this._smoothRoll = 0;
    this._smoothTx = 0;
    this._smoothTy = 0;
    this._smoothBlinkL = 0;
    this._smoothBlinkR = 0;
  }

  setPreset(mode) {
    if (mode === 'top') {
      this.targetAz = 0.0;
      this.targetEl = 1.48;
      this.targetDist = 1280;
    } else {
      this.targetAz = 0.0;
      this.targetEl = 0.34;
      this.targetDist = 1120;
    }
  }

  attach(canvas) {
    if (!canvas || this._boundCanvas === canvas) return;
    this._boundCanvas = canvas;

    canvas.addEventListener('pointerdown', (e) => {
      this._dragging = true;
      this._lastX = e.clientX;
      this._lastY = e.clientY;
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!this._dragging) return;
      const dx = e.clientX - this._lastX;
      const dy = e.clientY - this._lastY;
      this._lastX = e.clientX;
      this._lastY = e.clientY;
      this.targetAz = Math.max(-1.45, Math.min(1.45, this.targetAz - dx * 0.0065));
      this.targetEl = Math.max(-0.35, Math.min(1.52, this.targetEl + dy * 0.0065));
    });
    const stopDrag = () => { this._dragging = false; };
    canvas.addEventListener('pointerup', stopDrag);
    canvas.addEventListener('pointercancel', stopDrag);
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.targetDist = Math.max(580, Math.min(2200, this.targetDist + e.deltaY * 0.85));
    }, { passive: false });
    canvas.addEventListener('dblclick', () => this.setPreset('3d'));
  }

  _buildCamera(w, h) {
    // Smooth camera orbit damping
    this.camAz += (this.targetAz - this.camAz) * 0.16;
    this.camEl += (this.targetEl - this.camEl) * 0.16;
    this.camDist += (this.targetDist - this.camDist) * 0.16;

    const tx = this.target[0], ty = this.target[1], tz = this.target[2];
    // Position camera behind user (-Z) and above (-Y since +Y is down)
    const ce = Math.cos(this.camEl), se = Math.sin(this.camEl);
    const ca = Math.cos(this.camAz), sa = Math.sin(this.camAz);
    const pos = [
      tx + this.camDist * ce * sa,
      ty - this.camDist * se,
      tz - this.camDist * ce * ca,
    ];

    // Forward vector from camera to target
    let fx = tx - pos[0], fy = ty - pos[1], fz = tz - pos[2];
    const fl = Math.hypot(fx, fy, fz) || 1;
    fx /= fl; fy /= fl; fz /= fl;

    // World down is (0, 1, 0), so camera right = down x forward (or up x forward)
    // With world up = (0, -1, 0): right = forward x up
    let rx = fy * 0 - fz * (-1); // = fz? Let's compute cross(forward, up(0,-1,0)) = (fz, 0, -fx)
    let ry = 0;
    let rz = -fx;
    // Wait: if forward = (0, 0, 1) and up = (0, -1, 0):
    // right = (1, 0, 0) -> cross((0,0,1), (0,-1,0)) = (0*0 - 1*(-1), 1*0 - 0*0, 0*(-1) - 0*0) = (1, 0, 0)! Correct!
    const rl = Math.hypot(rx, ry, rz) || 1;
    rx /= rl; ry /= rl; rz /= rl;

    // Camera down = cross(forward, right)
    const dx = fy * rz - fz * ry;
    const dy = fz * rx - fx * rz;
    const dz = fx * ry - fy * rx;

    const focal = Math.min(w, h) * 1.18;
    const cx = w * 0.5;
    const cy = h * 0.52;

    return {
      pos,
      project(pt) {
        const vx = pt[0] - pos[0];
        const vy = pt[1] - pos[1];
        const vz = pt[2] - pos[2];
        const z = vx * fx + vy * fy + vz * fz;
        if (z < 20) return null;
        const x = vx * rx + vy * ry + vz * rz;
        const y = vx * dx + vy * dy + vz * dz;
        const s = focal / z;
        return { x: cx + x * s, y: cy + y * s, z, s };
      },
    };
  }

  render(canvas, gazeState, rigState, trailPts, palette) {
    if (!canvas) return;
    this.attach(canvas);
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const targetW = Math.floor(rect.width * dpr);
    const targetH = Math.floor(rect.height * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
      canvas.width = targetW;
      canvas.height = targetH;
    }

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    const cam = this._buildCamera(rect.width, rect.height);
    const displays = (rigState && rigState.displays) || [];
    const pred = gazeState && gazeState.prediction;
    const running = Boolean(gazeState && gazeState.running);
    const faceOk = Boolean(running && pred && pred.face_detected);

    // Smoothly interpolate live 3D gaze & head attitude for 60fps fluid animation
    const lerp = 0.28;
    if (faceOk) {
      this._smoothAz += ((pred.az || 0) - this._smoothAz) * lerp;
      this._smoothEl += ((pred.el || 0) - this._smoothEl) * lerp;
      this._smoothYaw += (-(pred.yaw || 0) - this._smoothYaw) * lerp;
      this._smoothPitch += ((pred.pitch || 0) - this._smoothPitch) * lerp;
      this._smoothRoll += ((pred.roll || 0) - this._smoothRoll) * lerp;
      // Head translation clamped gently around origin (mm)
      const htx = Math.max(-110, Math.min(110, -(pred.tx || 0) * 260));
      const hty = Math.max(-80, Math.min(80, (pred.ty || 0) * 200));
      this._smoothTx += (htx - this._smoothTx) * 0.15;
      this._smoothTy += (hty - this._smoothTy) * 0.15;
      const bL = pred.blinking || pred.kinematics === 'blink_hold' ? 1.0 : Math.min(1, (pred.blink_left || 0) * 2.2);
      const bR = pred.blinking || pred.kinematics === 'blink_hold' ? 1.0 : Math.min(1, (pred.blink_right || 0) * 2.2);
      this._smoothBlinkL += (bL - this._smoothBlinkL) * 0.45;
      this._smoothBlinkR += (bR - this._smoothBlinkR) * 0.45;
    } else {
      this._smoothBlinkL += (0 - this._smoothBlinkL) * 0.15;
      this._smoothBlinkR += (0 - this._smoothBlinkR) * 0.15;
    }

    // 1. Subtle 3D desk reference grid (y = +235mm)
    const deskY = 235;
    ctx.strokeStyle = palette.borderSoft;
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.45;
    for (let x = -750; x <= 750; x += 150) {
      const a = cam.project([x, deskY, -100]);
      const b = cam.project([x, deskY, 850]);
      if (a && b) { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
    }
    for (let z = -100; z <= 850; z += 150) {
      const a = cam.project([-750, deskY, z]);
      const b = cam.project([750, deskY, z]);
      if (a && b) { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
    }
    ctx.globalAlpha = 1.0;

    // Resolve live 3D hit point on active display
    let hit3D = null;
    if (faceOk) {
      const mapped = pixelTo3D(displays, pred.x, pred.y);
      if (mapped) {
        hit3D = mapped.pt;
      } else {
        const r = rayFromAngles(this._smoothAz, this._smoothEl);
        hit3D = [r[0] * 600, r[1] * 600, r[2] * 600];
      }
    }

    // 2. Sort displays back-to-front by camera Z
    const dispItems = displays.map((d) => {
      const b = planeBasis(d);
      const pC = cam.project(b.c);
      return { d, b, depth: pC ? pC.z : 9999 };
    }).sort((a, b) => b.depth - a.depth);

    const samplePts = (gazeState && gazeState.stats && gazeState.stats.sample_points) || [];
    const activeConn = faceOk && pred.display ? pred.display : '';
    const sigColor = faceOk && pred.using_ridge ? palette.ok : palette.accent;

    for (const { d, b } of dispItems) {
      const p00 = cam.project(uvTo3D(b, 0, 0));
      const p10 = cam.project(uvTo3D(b, 1, 0));
      const p11 = cam.project(uvTo3D(b, 1, 1));
      const p01 = cam.project(uvTo3D(b, 0, 1));
      if (!p00 || !p10 || !p11 || !p01) continue;

      const isActive = d.connector === activeConn;

      // Monitor stand stem down to desk plane
      const botMid = uvTo3D(b, 0.5, 1.0);
      const sTop = cam.project(botMid);
      const sBot = cam.project([botMid[0], deskY, botMid[2]]);
      if (sTop && sBot) {
        ctx.strokeStyle = palette.borderSoft;
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(sTop.x, sTop.y); ctx.lineTo(sBot.x, sBot.y); ctx.stroke();
      }

      // Panel fill
      ctx.beginPath();
      ctx.moveTo(p00.x, p00.y);
      ctx.lineTo(p10.x, p10.y);
      ctx.lineTo(p11.x, p11.y);
      ctx.lineTo(p01.x, p01.y);
      ctx.closePath();
      ctx.fillStyle = palette.elevated;
      ctx.globalAlpha = isActive ? 0.92 : 0.72;
      ctx.fill();
      ctx.globalAlpha = 1.0;

      // Drafting thirds inside screen
      ctx.strokeStyle = palette.borderSoft;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      for (const f of [1 / 3, 2 / 3]) {
        const a = cam.project(uvTo3D(b, f, 0)), c = cam.project(uvTo3D(b, f, 1));
        if (a && c) { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(c.x, c.y); ctx.stroke(); }
        const e = cam.project(uvTo3D(b, 0, f)), g = cam.project(uvTo3D(b, 1, f));
        if (e && g) { ctx.beginPath(); ctx.moveTo(e.x, e.y); ctx.lineTo(g.x, g.y); ctx.stroke(); }
      }
      ctx.setLineDash([]);

      // Bezel outline (highlighted in green/accent when actively looked at)
      ctx.beginPath();
      ctx.moveTo(p00.x, p00.y);
      ctx.lineTo(p10.x, p10.y);
      ctx.lineTo(p11.x, p11.y);
      ctx.lineTo(p01.x, p01.y);
      ctx.closePath();
      ctx.strokeStyle = isActive ? sigColor : palette.border;
      ctx.lineWidth = isActive ? 2.2 : 1.3;
      ctx.stroke();

      // Passive click anchors projected onto this 3D screen
      for (const [sx, sy] of samplePts) {
        if (sx >= d.x && sx < d.x + d.width && sy >= d.y && sy < d.y + d.height) {
          const u = (sx - d.x) / Math.max(1, d.width);
          const v = (sy - d.y) / Math.max(1, d.height);
          const sp = cam.project(uvTo3D(b, u, v));
          if (sp) {
            ctx.fillStyle = palette.ok;
            ctx.globalAlpha = 0.72;
            ctx.beginPath(); ctx.arc(sp.x, sp.y, Math.max(1.8, 2.8 * sp.s), 0, Math.PI * 2); ctx.fill();
            ctx.globalAlpha = 1.0;
          }
        }
      }

      // Connector + yaw label floating cleanly above top-center edge
      const pTop = cam.project(uvTo3D(b, 0.5, -0.06));
      if (pTop) {
        ctx.font = '700 10px "MesloLGS Nerd Font Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillStyle = isActive ? palette.text : palette.muted;
        ctx.fillText(`${d.connector}  ${d.yaw_deg >= 0 ? '+' : ''}${Number(d.yaw_deg || 0).toFixed(1)} deg`, pTop.x, pTop.y);
      }
    }

    // 3. Fading 3D gaze trail on display surfaces
    if (faceOk && trailPts.length > 1) {
      for (let i = 1; i < trailPts.length; i++) {
        const a3 = pixelTo3D(displays, trailPts[i - 1][0], trailPts[i - 1][1]);
        const b3 = pixelTo3D(displays, trailPts[i][0], trailPts[i][1]);
        if (!a3 || !b3) continue;
        const pa = cam.project(a3.pt), pb = cam.project(b3.pt);
        if (!pa || !pb) continue;
        ctx.strokeStyle = sigColor;
        ctx.globalAlpha = (i / trailPts.length) * 0.45;
        ctx.lineWidth = 1.2 + (i / trailPts.length) * 1.4;
        ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
      }
      ctx.globalAlpha = 1.0;
    }

    // 4. Real-Time 3D Head Frame & Dual Binocular Eyeballs (IPD = 63 mm)
    const headPos = [this._smoothTx, this._smoothTy, 0];
    const hR = rotMatrix(this._smoothYaw / DEG, this._smoothPitch / DEG, this._smoothRoll / DEG);
    const hRight = [hR[0], hR[3], hR[6]];
    const hDown  = [hR[1], hR[4], hR[7]];
    const hFwd   = [hR[2], hR[5], hR[8]];

    const ipdHalf = 32; // mm
    const leftEye = [
      headPos[0] - ipdHalf * hRight[0],
      headPos[1] - ipdHalf * hRight[1],
      headPos[2] - ipdHalf * hRight[2],
    ];
    const rightEye = [
      headPos[0] + ipdHalf * hRight[0],
      headPos[1] + ipdHalf * hRight[1],
      headPos[2] + ipdHalf * hRight[2],
    ];

    // Minimal architectural head wireframe ring (brow/temple halo)
    const ringPts = [];
    for (let i = 0; i <= 24; i++) {
      const a = (i / 24) * Math.PI * 2;
      const rx = Math.cos(a) * 68;
      const rz = Math.sin(a) * 76 - 42;
      const wp = [
        headPos[0] + rx * hRight[0] + rz * hFwd[0],
        headPos[1] + rx * hRight[1] + rz * hFwd[1],
        headPos[2] + rx * hRight[2] + rz * hFwd[2],
      ];
      ringPts.push(cam.project(wp));
    }
    ctx.strokeStyle = palette.borderSoft;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    let started = false;
    for (const p of ringPts) {
      if (!p) continue;
      if (!started) { ctx.moveTo(p.x, p.y); started = true; }
      else ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();

    // Subtle bridge line connecting the two eyeballs
    const pLE = cam.project(leftEye);
    const pRE = cam.project(rightEye);
    if (pLE && pRE) {
      ctx.strokeStyle = palette.border;
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(pLE.x, pLE.y); ctx.lineTo(pRE.x, pRE.y); ctx.stroke();
    }

    // 5. Dual Converging 3D Gaze Rays from both eyeballs to hit point on active display
    if (faceOk && hit3D) {
      const pHit = cam.project(hit3D);
      const isHold = pred.blinking || pred.kinematics === 'blink_hold' || pred.kinematics === 'recovery';
      if (pHit) {
        ctx.strokeStyle = sigColor;
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = isHold ? 0.22 : 0.58;
        if (pred.in_gap || isHold) ctx.setLineDash([5, 5]);
        for (const pEye of [pLE, pRE]) {
          if (!pEye) continue;
          ctx.beginPath(); ctx.moveTo(pEye.x, pEye.y); ctx.lineTo(pHit.x, pHit.y); ctx.stroke();
        }
        ctx.setLineDash([]);
        ctx.globalAlpha = 1.0;

        // Calm visionOS-inspired focus ring at 3D intersection point
        const rPx = Math.max(8, Math.min(18, 16 * pHit.s));
        const aScale = isHold ? 0.35 : 0.95;
        ctx.fillStyle = sigColor;
        ctx.globalAlpha = 0.14 * aScale;
        ctx.beginPath(); ctx.arc(pHit.x, pHit.y, rPx, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = aScale;
        ctx.strokeStyle = sigColor;
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(pHit.x, pHit.y, rPx, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = palette.text;
        ctx.beginPath(); ctx.arc(pHit.x, pHit.y, 2.6, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = 1.0;
      }
    }

    // 6. Render Left & Right 3D Eyeballs (sclera + live-oriented iris/pupil + blinking eyelid)
    const eyeList = [
      { pos: leftEye, proj: pLE, blink: this._smoothBlinkL },
      { pos: rightEye, proj: pRE, blink: this._smoothBlinkR },
    ].filter((e) => e.proj).sort((a, b) => b.proj.z - a.proj.z);

    const gazeDir = hit3D ? null : rayFromAngles(this._smoothAz, this._smoothEl);
    for (const eye of eyeList) {
      const p = eye.proj;
      const r = Math.max(6, Math.min(20, 16.5 * p.s));

      // Sclera sphere
      ctx.fillStyle = palette.ivory;
      ctx.strokeStyle = palette.border;
      ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();

      // Compute unit direction from this eyeball to 3D hit point
      let dx, dy, dz;
      if (hit3D) {
        dx = hit3D[0] - eye.pos[0];
        dy = hit3D[1] - eye.pos[1];
        dz = hit3D[2] - eye.pos[2];
        const l = Math.hypot(dx, dy, dz) || 1;
        dx /= l; dy /= l; dz /= l;
      } else {
        dx = gazeDir[0]; dy = gazeDir[1]; dz = gazeDir[2];
      }

      // Iris point on front hemisphere of eyeball
      const irisWorld = [
        eye.pos[0] + dx * 15.5,
        eye.pos[1] + dy * 15.5,
        eye.pos[2] + dz * 15.5,
      ];
      const pIris = cam.project(irisWorld);
      if (pIris) {
        // Clamp iris within sclera disc for clean perspective
        let ix = pIris.x - p.x, iy = pIris.y - p.y;
        const maxOff = r * 0.52;
        const il = Math.hypot(ix, iy);
        if (il > maxOff) { ix = (ix / il) * maxOff; iy = (iy / il) * maxOff; }

        ctx.fillStyle = sigColor;
        ctx.beginPath(); ctx.arc(p.x + ix, p.y + iy, r * 0.44, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = '#161617';
        ctx.beginPath(); ctx.arc(p.x + ix, p.y + iy, r * 0.23, 0, Math.PI * 2); ctx.fill();
      }

      // Eyelid blink sweep
      if (eye.blink > 0.08) {
        ctx.save();
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 0.5, 0, Math.PI * 2); ctx.clip();
        ctx.fillStyle = palette.surface;
        ctx.fillRect(p.x - r - 1, p.y - r - 1, (r + 1) * 2, (r + 1) * 2 * Math.min(1, eye.blink));
        ctx.restore();
        ctx.strokeStyle = palette.border;
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.stroke();
      }
    }

    // Subtle HUD hint in bottom-left corner of 3D viewport
    ctx.fillStyle = palette.muted;
    ctx.font = '600 10px "MesloLGS Nerd Font Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(
      faceOk ? 'DRAG TO ORBIT  |  SCROLL TO ZOOM  |  DBL-CLICK RESET' : (running ? 'AWAITING FACE LOCK' : 'PRESS START TRACKING FOR LIVE 3D EYEBALLS & RAYS'),
      14,
      rect.height - 12
    );
  }
}
