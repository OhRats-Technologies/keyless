# Keyless Gaze v2 — Target-Level Intent

**Status:** approved direction. The legacy calibration/regression/online-bias code has been removed. This is the greenfield contract.

**One sentence:** stop estimating a cursor; estimate the user's intended target. The cursor is a rendering of the posterior, not the objective.

---

## 0. Why this document exists

The old system solved the wrong problem. It fitted a 2-D point from a one-time calibration, then tried to repair the mapping with a scalar bias, a global recenter, and hand-tuned click gates. Every one of those mechanisms encoded a guess about when the point estimate was wrong.

The new system must be a decision system:

```
noisy observations -> attention state -> distribution over candidate targets -> action
```

The physical mouse is not a competitor to the eye cursor. It is a **privileged teacher**: it tells us which target the user actually meant. The camera tells us where the user was looking. The gap between the two is the learning signal.

This reframes the whole project:

| Old question | New question |
|---|---|
| What is the gaze point in pixels? | Which target does the user intend? |
| How do we remove the 78 px error? | How often is the top target correct, and when should we abstain? |
| How do we calibrate once? | How do we build a replayable flywheel that improves every session? |
| Which scalar offset fixes this region? | Which candidates are plausible, and how do we rank them? |

---

## 1. Insights

### From the user's feedback

1. **Two cursors are a product bug, not a rendering detail.** The screen must have exactly one pointer at all times. The cursor is an affordance; ambiguity destroys trust. This is now enforced by a synchronous handoff between the cua overlay and the hardware pointer.

2. **"Steer to mouse" is a UI action, not a calibration event.** Using one manually chosen point to translate the entire screen is a strong, unverified assumption. A single event is a *sample*, not a coordinate transform. If it is to teach anything, it must enter the same gated learner as a click.

3. **The user's own clicks are the asset.** The mouse is always available, always labelled, and reflects intent. Any design that ignores it is leaving free supervision on the table.

4. **The old calibration gate must die.** A long dot grid is a poor cold start and a worse ongoing signal. A handful of natural clicks is a better prior. The product should never block on a calibration file.

5. **The user wants a decisive architecture, not another pile of thresholds.** The right response is a model whose objective is the decision, not a collection of gates that emulate one.

### From reflection

6. **Pixel RMSE is not decision-sufficient.** Two targets can be 20 px apart. A 30 px error is harmless if the ranking is right and fatal if it flips the target. Minimising MSE does not minimise target error. This is the central technical mistake of the old system.

7. **Gaze is attention, not pointing.** The eyes identify *what* the user cares about; the hand resolves *where exactly*. The system should fuse both, not pretend the eye is a mouse.

8. **Invariance is the wrong inductive bias for gaze.** Gaze mapping is **equivariant** to head pose: move the head, the screen point changes. JEPA-style augmentation invariance deliberately throws that information away. This kills the naive "LeJEPA on face points" plan.

9. **The screen is not a physical world.** V-JEPA predicts latent dynamics of video. A UI is arbitrary text, layout, and state; its next frame is not a smooth physical continuation. The predictable object is the user's intention, not the next screenshot.

10. **No architecture is provably best without assumptions.** "Provably best" is only meaningful relative to a data-generating process, a loss, and a decision rule. The honest programme is: state the assumptions, derive the Bayes-optimal decision, choose the architecture that respects them, and falsify it on held-out sessions.

---

## 2. Kill list

Removed from the working tree, not merely deprecated:

- `StereoGazeModel`, RBF/geometric calibration, `calibrator.py`, `geo_gaze.py`
- `OnlineGazeLearner`, scalar bias, `recenter_to`, `online_learner.py`
- the global "steer to mouse" anchor and the `recenter gaze` command
- `cursor_gimbal.py`, `run_vision_loop`, saccade/gimbal/One-Euro tuning
- the macOS click harvester and its 120 ms/180 ms/Huber gates
- the full-screen calibration overlay, Qt calibration page, calibration gate on startup
- calibration-file paths, migrations, and `/v1/calibrate` model launch
- the standalone gaze-dot overlay and screen-cursor launcher
- pixel RMSE as a headline metric

Kept, because they are model-agnostic infrastructure:

- camera capture and MediaPipe landmarks (`vision/landmarks.py`, `stereo_capture.py`)
- the cua agent-cursor overlay and single-cursor arbitration
- the shared-memory gaze channel and the new `keyless.gaze` seam
- voice/STT/LLM routing, Firefox DOM bridge, UI/AX/OCR candidate sources
- the SwiftUI host and the control plane

**Do not resurrect the old model from git history.** It is tagged `legacy-gaze-v1` for audit only. Reusing its losses, gates, or anchors will re-bias the new design.

---

## 3. Formal problem

Let

- \(o_t\) be the observation at time \(t\): face landmarks, eye crops, head pose, camera metadata.
- \(h_t = f_\theta(o_{t-T:t})\) be a latent **attention state** over a short window.
- \(C_t = \{c_1,\dots,c_n\}\) be the candidate targets on screen, each with geometry, role, text, and a visual embedding.
- \(y_t \in C_t \cup \{\varnothing\}\) be the intended target; \(\varnothing\) means "no action / free-space cursor".
- \(u_t\) be the action: click \(c\), move the cursor to \((x,y)\), scroll, type, or abstain.

The model is a **target-conditioned energy function**

\[
s_\theta(h_t, c) = \text{score of target } c \text{ under attention state } h_t,
\qquad
p_\theta(c \mid h_t, C_t) = \frac{\exp(s_\theta(h_t,c))}{\sum_{c' \in C_t}\exp(s_\theta(h_t,c'))}.
\]

The decision is

\[
\hat c = \arg\max_{c \in C_t} p_\theta(c \mid h_t,C_t),
\]

with an abstention rule when the top probability or the margin is too low:

\[
\text{act} \iff p_\theta(\hat c) \ge \tau \ \wedge\ p_\theta(\hat c) - p_\theta(c_{(2)}) \ge \delta.
\]

A separate **pixel head** renders free-space motion:

\[
(\mu_t, \log\sigma_t^2) = g_\phi(h_t), \qquad
\mathcal{L}_{\text{pix}} = \frac{(x_t-\mu_t)^2}{2\sigma_t^2} + \frac{1}{2}\log\sigma_t^2.
\]

The pixel head exists for smooth cursor motion when no candidate is active. It is not the product objective.

---

## 4. Pillars

### Pillar 1 — The data flywheel is the first deliverable

No model work before the replayable event log exists. Every session must produce:

- **Frame events:** timestamp, camera ids, landmarks, head pose, eye crops hash, features, prediction, uncertainty.
- **Screen events:** display, active app/window, candidate list (id, bbox, role, text, visual embedding), screenshot hash.
- **Interaction events:** mouse position, hover, mouse-down/up, drag distance, click target, voice command, correction, accepted/rejected label, latency.
- **Model events:** version, weights hash, config, code commit.

Store locally (Parquet/JSONL + content-addressed screenshots), session-keyed, replayable, privacy-preserving. This is the only durable moat.

### Pillar 2 — Learn the target posterior, not the pixel

Use a listwise objective over candidates:

\[
\mathcal{L}_{\text{target}} = -\log p_\theta(y_t \mid h_t, C_t).
\]

Clicks are strong positives; hover is a weak positive; spatially distant candidates are negatives; the null/free-space candidate is explicit. This directly optimises the decision the product makes.

### Pillar 3 — Model attention as a temporal state

Gaze is a time series. A single frame is noisy and ambiguous; a fixation is evidence accumulated over hundreds of milliseconds. Use a causal temporal encoder (TCN/GRU/1-D transformer) over 0.5–1.5 s. Predict the target distribution from the integrated state, not the instantaneous point.

The old 180 ms lookback heuristic disappears: the model learns its own lead time and its own fixation boundary.

### Pillar 4 — Uncertainty is a first-class output

The model must know when it does not know. Use:

- heteroscedastic heads / temperature scaling;
- a small deep ensemble (3 seeds) or MC dropout;
- split conformal prediction for target sets;
- an explicit abstain/confirm action.

Uncertainty drives label gating, active learning, and the UI. It is not a diagnostic afterthought.

### Pillar 5 — Close the loop with the user

The mouse is a teacher, not a rival. Every natural click is a label. Every correction is a label. When the model is uncertain, it asks for a cheap label (a glance-and-click, a voice confirmation) instead of guessing. This is DAgger with a human in the loop.

### Pillar 6 — Evaluate decisions, not pictures

The primary metric is **actions per correction**, not RMSE. Secondary metrics: top-1 target accuracy, top-3 recall, misclick rate, abstention rate, time-to-target, cold-start labels, drift over 30/60 min, conformal coverage.

---

## 5. Model theory

### 5.1 Theorem — pixel MSE is not decision-sufficient

**Statement.** Let \(R_c \subset \mathbb{R}^2\) be the region of target \(c\), and let \(p(x \mid o)\) be the gaze posterior. The 0–1 target risk of a point estimate \(\hat x\) is

\[
R(\hat x) = 1 - \sum_c \mathbf{1}[\hat x \in R_c]\,P(c \mid o),
\qquad
P(c \mid o) = \int_{R_c} p(x \mid o)\,dx.
\]

There exist posteriors for which the MSE-optimal point \(\hat x_{\text{MSE}} = \mathbb{E}[x \mid o]\) has strictly higher target risk than another point with higher MSE.

**Construction.** Two equal-size targets \(A=[-10,-2]\times[-1,1]\) and \(B=[2,10]\times[-1,1]\), and a symmetric posterior with mass \(0.5\) on each. The mean is \(0\), which lies between the targets: target risk \(1\). The point \(x=-6\) lies in \(A\): target risk \(0.5\), while its MSE is \(36\) versus \(0\) for the mean. ∎

**Consequence.** Minimising pixel RMSE can be actively harmful. The correct loss is over target identities or over the decision, not over coordinates. This is the formal reason the old model's headline metric was wrong.

### 5.2 Theorem — Bayes-optimal target selection

**Statement.** For a finite candidate set \(C\), 0–1 loss, and calibrated posterior \(p(c \mid h,C)\), the optimal action is \(\arg\max_c p(c \mid h,C)\).

**Proof.** The expected loss of choosing \(c\) is \(1-p(c \mid h,C)\). Minimising it is equivalent to maximising \(p(c \mid h,C)\). ∎

With asymmetric costs \(L(c,y)\) (a misclick on a destructive button costs more than a miss), the optimal action is \(\arg\min_c \sum_y L(c,y)p(y \mid h,C)\). The softmax parameterisation is the natural calibrated form; cross-entropy is a strictly proper scoring rule, so its minimiser is the true posterior under realizability. See Gneiting & Raftery (2007).

**Consequence.** A target-conditioned softmax is not a heuristic; it is the Bayes decision rule for the product's actual loss.

### 5.3 Proposition — temporal fusion reduces variance

**Statement.** Let \(z_t = s + \epsilon_t\) with independent zero-mean noise \(\epsilon_t\), \(\operatorname{Var}(\epsilon_t)=\sigma^2\). The sample mean over \(T\) frames has variance \(\sigma^2/T\). For a random-walk state, the Kalman–Bucy filter is the minimum-variance linear estimator; its steady-state variance is strictly smaller than the measurement variance for any finite process noise.

**Proof.** The sample-mean variance is immediate from independence. The Kalman recursion is the Riccati equation for the linear-Gaussian model; its steady-state gain is positive for positive process noise, so the posterior variance is below \(\sigma^2\). ∎

**Consequence.** A temporal encoder is not cosmetic. It is the optimal way to convert many noisy frames into one decision. The old pipeline's frame-by-frame point estimate discarded this for free.

### 5.4 Theorem — invariance destroys the gaze signal

**Statement.** Let \(f\) be an encoder that is invariant to a transformation group \(G\): \(f(gx)=f(x)\) for all \(g \in G\). Then no downstream predictor \(q(f(x))\) can distinguish \(x\) from \(gx\). If the gaze mapping \(g^\*\) satisfies \(g^\*(gx) \ne g^\*(x)\) for some \(g \in G\), an invariant encoder is insufficient to represent the mapping.

**Proof.** \(q(f(gx)) = q(f(x))\) by invariance, while \(g^\*(gx) \ne g^\*(x)\) by assumption. ∎

**Consequence.** Gaze is **equivariant** to head pose, not invariant. A JEPA/LeJEPA trained with random crops, rotations, or pose augmentations learns the wrong symmetry. If pretraining on face video, use **equivariant** augmentations: apply a known transformation to the input and require the embedding to transform predictably, or condition the predictor on the pose. Do not impose invariance on the very variable that determines the label.

This is the strongest argument against "train a LeJEPA from face points" as the core plan. It is not that LeJEPA is bad; it is that its symmetry is wrong for this task.

### 5.5 Proposition — conformal target sets

**Statement.** Given exchangeable calibration and test pairs \((h_i,C_i,y_i)\), split conformal with nonconformity score \(1-p_\theta(y_i \mid h_i,C_i)\) produces a set \(\mathcal{C}_\alpha\) with

\[
P(y_{\text{test}} \in \mathcal{C}_\alpha) \ge 1-\alpha.
\]

**Proof.** Standard split-conformal argument: the rank of the test score among \(n+1\) exchangeable scores is uniform, so the \((1-\alpha)\) empirical quantile covers the test label with probability at least \(1-\alpha\). ∎

**Consequence.** The UI can act when \(|\mathcal{C}_\alpha|=1\), confirm when it is 2–3, and abstain otherwise, with a distribution-free guarantee under exchangeability. Under drift, use adaptive conformal inference (Gibbs & Candès, 2021).

### 5.6 No free lunch, and what "provably best" can mean

The no-free-lunch theorems (Wolpert & Macready, 1997) rule out a universally best architecture over all problems. Any claim of "provably best" is conditional. We therefore state the assumptions explicitly:

1. The candidate generator has high recall (targets are in \(C_t\)).
2. Intent is temporally persistent over a fixation window.
3. The gaze-to-target mapping is user-specific but slowly varying.
4. The decision loss is 0–1 or a known asymmetric cost.

Under 1–4, the Bayes-optimal decision is the target posterior maximiser; the architecture is chosen to approximate that posterior with low latency. This is a proof of the **decision rule**, not of a particular network. The architecture is then falsified against baselines on held-out sessions. That is the scientific standard we will hold.

---

## 6. Architecture decision

### 6.1 Should we start with a 2026 V-JEPA of the screen?

**No.**

Reasons:

1. **The objective is misaligned.** V-JEPA is trained to predict masked spatiotemporal representations of video. A UI is not a physical dynamical system: text, layout, and state change discontinuously; the conditional entropy of the next screen given the past is high. The model would spend capacity on appearance, not intent.
2. **The features are wrong granularity.** V-JEPA's semantic/motion features are excellent for action recognition and physical prediction. Target selection needs fine text, icon identity, and precise geometry. A frozen video encoder loses small UI details; a 1B-parameter ViT is not a 30–60 Hz laptop model.
3. **The label is wrong.** We do not have future-frame labels. We have click targets. Training a screen world model and then attaching a target head is an indirect, data-hungry path to a supervised problem.
4. **The compute budget is wrong.** A 1B-parameter video transformer per frame is incompatible with an always-on local product.

Use a **frozen UI encoder** instead: accessibility tree, OCR, DOM, and a small visual encoder (SigLIP/CLIP-class or a distilled UI model) for candidate crops. Cache embeddings per screen change. This gives the target model the right granularity at the right cost.

V-JEPA becomes relevant only for a different subproblem: understanding a video the user is watching, or predicting app state transitions. It is not the core.

### 6.2 Should we train a LeJEPA from face points?

**Not as the core. Optional later, with equivariant augmentations.**

Reasons:

1. **Face landmarks are already a structured, low-dimensional representation.** A JEPA on 20-D engineered features is overkill; a small MLP/TCN is sufficient.
2. **Invariance is the wrong symmetry.** See Theorem 5.4. Random crops/rotations/pose augmentations destroy the signal that maps head pose to screen position.
3. **No screen labels in pretraining.** Self-supervision on one user's face video learns appearance factors, not the gaze-to-screen mapping. The label problem remains.
4. **LeJEPA's guarantee is about downstream probe risk under an isotropic Gaussian embedding, for linear/kNN/kernel probes.** It is a strong result, but it does not guarantee gaze accuracy, and its Gaussianity assumption is not obviously compatible with pose-equivariant gaze features.

The defensible use: pretrain a **raw eye-crop encoder** on unlabeled video with **equivariant** objectives (predict the effect of a known pose/lighting transformation), then fine-tune end-to-end on target loss. Use SIGReg as a regularizer if it improves held-out target accuracy. Treat this as an ablation, not an article of faith.

### 6.3 The architecture we will build

**Target-conditioned two-tower energy model with a temporal gaze encoder.**

```
cameras ──► face/eye encoder ──► z_g ─┐
             + head pose               ├─► temporal encoder ──► h_t
             (0.5–1.5 s history)      ─┘

screen ──► candidate generator ──► {c_i} ──► candidate encoder ──► {z_i}
           (AX / OCR / DOM /                (text + crop + geometry)
            visual regions)

score_i = MLP([h_t, z_i, h_t ⊙ z_i, geometry_i])
p(c_i | h_t, C) = softmax(score_i)

pixel head: MLP(h_t) ──► (μ, log σ²)
```

**Sizes (initial):**

- Gaze tower: 2–4 layer causal TCN or GRU, 128–256 dims, ~0.5–2 M params.
- Candidate encoder: frozen text encoder (small) + frozen visual encoder (SigLIP/CLIP-class or distilled UI CNN) + 2-layer MLP to 128 dims.
- Scorer: 2-layer MLP, ~0.1–0.5 M params.
- Pixel head: 2-layer MLP, ~50 k params.
- Total trainable: 1–5 M params. CPU/MPS latency target: <10 ms for 50 candidates after screen embeddings are cached.

**Losses:**

\[
\mathcal{L} = \mathcal{L}_{\text{target}} + \lambda_1 \mathcal{L}_{\text{pix}} + \lambda_2 \mathcal{L}_{\text{temp}} + \lambda_3 \mathcal{L}_{\text{rel}}.
\]

- \(\mathcal{L}_{\text{target}}\): listwise cross-entropy / InfoNCE over candidates.
- \(\mathcal{L}_{\text{pix}}\): heteroscedastic Gaussian NLL for free-space motion.
- \(\mathcal{L}_{\text{temp}}\): self-supervised next-gaze / masked-gaze prediction, or a contrastive consistency loss over a fixation window.
- \(\mathcal{L}_{\text{rel}}\): learned label reliability \(P(\text{label valid}\mid\text{context})\), trained on accepted vs. rejected corrections.

**Inference:**

1. On each gaze frame, update the temporal state.
2. When the fixation stabilises (learned, not thresholded), generate/cache candidates and score them.
3. If top-1 is confident, move the cursor to the target centroid; if not, keep the free-space pixel cursor and offer a confirm affordance.
4. On click/voice, log the label and update the replay buffer.

**Cold start:** five to ten natural clicks, not a dot grid. Each click gives a target label; the pixel head and scorer are initialised from those labels plus a weak geometric prior. A short warm-up is allowed, but it is a *training set*, never a global offset.

**Online adaptation:** last-layer update or a small adapter per user, with a replay buffer and a conservative learning rate. Full refits only after a batch and only if held-out target accuracy does not degrade (the old CV gate was a good idea; keep the principle, not the code).

---

## 7. Data flywheel: schema and procedures

### 7.1 Event schema (v0)

```jsonc
{
  "session_id": "uuid",
  "ts_ns": 0,
  "frame": {
    "cams": ["cam1", "cam2"],
    "landmarks": [/* 478 x 3 or a compact subset */],
    "head_pose": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
    "eye_crops": {"left": "sha256", "right": "sha256"},
    "features": [/* model input */]
  },
  "screen": {
    "display_id": 1,
    "app": "Chrome",
    "window_id": 42,
    "screenshot": "sha256",
    "candidates": [
      {"id": "c1", "bbox": [x, y, w, h], "role": "button",
       "text": "Reply", "visual": "sha256", "score": 0.0}
    ]
  },
  "prediction": {"top": "c1", "p": 0.82, "entropy": 0.4, "pixel": [x, y]},
  "interaction": {
    "kind": "click", "mouse": [x, y], "target": "c1",
    "voice": null, "accepted": true, "latency_ms": 120
  }
}
```

### 7.2 Labelling rules

| Signal | Strength | Use |
|---|---|---|
| Click on a candidate | strong | target positive |
| Voice confirmation of a candidate | strong | target positive |
| Hover over a candidate while gazing at it | weak | positive with lower weight |
| Click in free space | weak/negative | null-target or pixel label |
| Correction ("no, that one") | strong | hard negative + positive |
| "Steer to mouse" | none by default | UI mode switch; no model update |

The old 180 ms lookback rule is replaced by a learned reliability model. Until it exists, keep a conservative gate as a **bootstrap**, not as the final design.

### 7.3 Replay and splits

- Store raw events and derived features.
- Split by **session**, never by frame, to avoid leakage.
- Maintain a frozen evaluation set of sessions the model never trains on.
- Re-run the offline evaluation on every model change.
- Log the model version and code commit with every prediction.

---

## 8. Training procedure

### Phase 0 — Instrumentation (1–2 weeks)

- Implement the event schema and a local writer.
- Build a replay loader and an offline evaluation harness.
- Define the metrics and baselines.
- **Gate:** reproduce a session and compute metrics offline.

### Phase 1 — Candidate generator (1–2 weeks)

- Combine AX tree, OCR, DOM, and visual regions into one candidate list.
- Measure candidate recall against real clicks.
- **Gate:** >95% recall. If not, fix perception before any model work.

### Phase 2 — Offline target scorer (2–3 weeks)

- Train the two-tower model on logged clicks.
- Baselines: nearest candidate to the pixel prediction; a logistic regression on geometry; a gradient-boosted tree.
- **Gate:** top-1 target accuracy beats the pixel baseline by a clear margin on held-out sessions.

### Phase 3 — Temporal + uncertainty (2 weeks)

- Add the temporal encoder and heteroscedastic head.
- Temperature-scale, ensemble, and conformalise.
- **Gate:** hit@50 px >90%, misclick <1 per 10 min, conformal coverage within 2% of nominal.

### Phase 4 — Online adaptation and active learning (ongoing)

- Last-layer adaptation with replay.
- Uncertainty-triggered requests for labels.
- DAgger: the model acts, the user corrects, the correction is a label.
- **Gate:** actions per correction improves monotonically over two weeks.

### Phase 5 — Optional representation work

- Raw eye-crop encoder with equivariant pretraining.
- SIGReg/LeJEPA as a regularizer, not a religion.
- V-JEPA only for video-understanding subproblems, not the core.
- **Gate:** a measurable held-out target gain, not a benchmark victory.

---

## 9. Evaluation

**Primary:** actions per correction (correct actions / explicit corrections or labels).

**Secondary:**

- candidate recall @ K (must be >95% before modelling);
- top-1 target accuracy;
- top-3 recall;
- hit rate @ 25/50/100 px (diagnostic only);
- misclick rate per hour;
- abstention/confirmation rate;
- time-to-target;
- cold-start labels to reach a target accuracy;
- drift over 30/60 min;
- conformal coverage.

**Protocols:**

- session-held-out splits;
- cold-start vs. personalised;
- cross-user generalisation;
- ablations: pixel-only, target-only, temporal, uncertainty, candidate features;
- online A/B with a safe fallback (the pixel cursor).

**Kill criteria:**

- Candidate recall <95% → perception bottleneck; stop model work.
- Target model cannot beat "nearest candidate to the pixel prediction" after a few hundred labels → the gaze signal lacks target information; invest in sensing or context, not a bigger net.
- Calibration/cold start >10 labels to reach a usable hit rate → the prior or candidate set is wrong.
- Actions per correction does not improve over two weeks → the flywheel is not closing; audit labels and distribution shift.

---

## 10. Risks and falsifiers

| Risk | Mitigation / falsifier |
|---|---|
| Candidate recall is low | AX/OCR/DOM union, visual fallback, measure before modelling |
| Labels are noisy | learned reliability, gated updates, conformal abstention |
| Distribution shift (new app, lighting, posture) | session-held-out eval, online conformal, adapter updates |
| Latency | cache screen embeddings, score only on fixation, small towers |
| Privacy | local-only storage, redact text where possible, user-controlled export |
| User fatigue | no long calibration; learn from natural clicks |
| Overfitting one user | cross-user eval; per-user adapters, shared backbone |

**What would change our mind:**

- If a V-JEPA screen encoder plus a target head beats the engineered candidate model by >10% top-1 on held-out sessions, adopt it for the screen tower.
- If LeJEPA/SIGReg pretraining improves target accuracy by >5% with equivariant augmentations, adopt it for the raw eye encoder.
- If a pixel-only model with excellent uncertainty beats the target model on actions per correction, the candidate generator is the bottleneck, not the ranking.

These are falsifiable commitments, not preferences.

---

## 11. Roadmap, gates, and deliverables

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | event log + replay + metrics | reproduce a session offline |
| 1 | candidate generator | >95% recall |
| 2 | offline target scorer | beats pixel baseline |
| 3 | temporal + uncertainty | >90% hit@50 px, <1 misclick/10 min |
| 4 | online adaptation + DAgger | actions per correction rising |
| 5 | optional representation work | held-out gain, not benchmark vanity |

**The one number:** actions per correction. If it does not improve by an order of magnitude, the direction is wrong.

---

## 12. References

### JEPA and self-supervised learning

- LeCun, Y. (2022). *A Path Towards Autonomous Machine Intelligence.* OpenReview. https://openreview.net/forum?id=BZ5a1r-kVsf
- Balestriero, R., & LeCun, Y. (2025). *LeJEPA: Provable and Scalable Self-Supervised Learning Without the Heuristics.* arXiv:2511.08544. https://arxiv.org/abs/2511.08544
- Assran, M., et al. (2023). *Self-Supervised Learning from Images with a Joint-Embedding Predictive Architecture (I-JEPA).* arXiv:2301.08243. https://arxiv.org/abs/2301.08243
- Bardes, A., et al. (2024). *Revisiting Feature Prediction for Learning Visual Representations from Video (V-JEPA).* arXiv:2404.08471. https://arxiv.org/abs/2404.08471
- Assran, M., et al. (2025). *V-JEPA 2: Self-Supervised Video Models Enable Understanding, Prediction and Planning.* arXiv:2506.09985. https://arxiv.org/abs/2506.09985
- Meta FAIR (2026). *V-JEPA 2.1 code release.* https://github.com/facebookresearch/vjepa2
- Bardes, A., Ponce, J., & LeCun, Y. (2021). *VICReg: Variance-Invariance-Covariance Regularization for Self-Supervised Learning.* arXiv:2105.04906. https://arxiv.org/abs/2105.04906

### Contrastive and energy-based learning

- van den Oord, A., Li, Y., & Vinyals, O. (2018). *Representation Learning with Contrastive Predictive Coding.* arXiv:1807.03748. https://arxiv.org/abs/1807.03748
- LeCun, Y., Chopra, S., Hadsell, R., Ranzato, M., & Huang, F. (2006). *A Tutorial on Energy-Based Learning.* https://energy-based-models.github.io/
- Gneiting, T., & Raftery, A. E. (2007). *Strictly Proper Scoring Rules, Prediction, and Estimation.* JASA. https://doi.org/10.1198/016214506000001437

### Decision theory and learning to act

- Wolpert, D. H., & Macready, W. G. (1997). *No Free Lunch Theorems for Optimization.* https://doi.org/10.1162/evco.1996.4.1.1
- Elmachtoub, A. N., & Grigas, P. (2022). *Smart "Predict, then Optimize".* Management Science. arXiv:1710.08005. https://arxiv.org/abs/1710.08005
- Ross, S., Gordon, G., & Bagnell, J. A. (2011). *A Reduction of Imitation Learning and Structured Prediction to No-Regret Online Learning (DAgger).* arXiv:1011.0686. https://arxiv.org/abs/1011.0686

### Uncertainty and calibration

- Lakshminarayanan, B., Pritzel, A., & Blundell, C. (2017). *Simple and Scalable Predictive Uncertainty Estimation using Deep Ensembles.* arXiv:1612.01474. https://arxiv.org/abs/1612.01474
- Guo, C., Pleiss, G., Sun, Y., & Weinberger, K. Q. (2017). *On Calibration of Modern Neural Networks.* arXiv:1706.04599. https://arxiv.org/abs/1706.04599
- Angelopoulos, A. N., & Bates, S. (2021). *A Gentle Introduction to Conformal Prediction and Distribution-Free Uncertainty Quantification.* arXiv:2107.07511. https://arxiv.org/abs/2107.07511
- Gibbs, I., & Candès, E. J. (2021). *Adaptive Conformal Inference Under Distribution Shift.* arXiv:2106.00170. https://arxiv.org/abs/2106.00170

### Gaze and interaction

- Krafka, K., et al. (2016). *Eye Tracking for Everyone (GazeCapture).* CVPR. arXiv:1606.05814. https://arxiv.org/abs/1606.05814
- Kalman, R. E. (1960). *A New Approach to Linear Filtering and Prediction Problems.* Journal of Basic Engineering. https://doi.org/10.1115/1.3662552

---

## Appendix A — Proof details

### A.1 MSE counterexample

Let the posterior be

\[
p(x\mid o) = \tfrac12 \mathbf{1}[x\in A] + \tfrac12 \mathbf{1}[x\in B],
\quad A=[-10,-2]\times[-1,1],\ B=[2,10]\times[-1,1].
\]

The MSE-optimal point is the mean:

\[
\hat x_{\text{MSE}} = \mathbb{E}[x] = (0,0).
\]

Neither target contains the origin, so the target risk is \(1\). The point \((-6,0)\in A\) has target risk \(0.5\). Its MSE is \(36\), but the product is a target decision, not a coordinate estimate. Hence MSE minimisation is not decision-sufficient. ∎

### A.2 Bayes decision under asymmetric costs

Let \(L(c,y)\) be the cost of action \(c\) when the true target is \(y\). The expected cost is

\[
\mathbb{E}[L(c,y)\mid h,C] = \sum_y L(c,y)p(y\mid h,C).
\]

Choosing \(c\) to minimise this is optimal by definition of expected loss. For 0–1 loss, \(L(c,y)=\mathbf{1}[c\ne y]\), and the expression reduces to \(1-p(c\mid h,C)\). ∎

### A.3 Temporal variance

For \(z_t=s+\epsilon_t\), \(\mathbb{E}[\epsilon_t]=0\), \(\operatorname{Var}(\epsilon_t)=\sigma^2\), independent across \(t\),

\[
\operatorname{Var}\!\left(\frac1T\sum_{t=1}^T z_t\right)
= \frac1{T^2}\sum_{t=1}^T \operatorname{Var}(\epsilon_t)
= \frac{\sigma^2}{T}.
\]

For a linear-Gaussian state-space model, the Kalman–Bucy filter is the minimum-variance linear estimator; its posterior variance solves the Riccati equation and is below the measurement variance when the process noise is positive. ∎

### A.4 Conformal coverage

Let \(S_i = 1-p_\theta(y_i\mid h_i,C_i)\) for calibration points \(i=1,\dots,n\), and \(S_{n+1}\) for the test point. Under exchangeability, the rank of \(S_{n+1}\) among the \(n+1\) scores is uniform on \(\{1,\dots,n+1\}\). Let \(\hat q\) be the \(\lceil (n+1)(1-\alpha)\rceil/n\) empirical quantile of \(S_1,\dots,S_n\). Then

\[
P(S_{n+1}\le \hat q)\ge 1-\alpha,
\]

which is exactly \(P(y_{n+1}\in \mathcal{C}_\alpha)\ge 1-\alpha\). ∎

### A.5 Invariance versus equivariance

If \(f(gx)=f(x)\) for all \(g\in G\), then for any downstream \(q\),

\[
q(f(gx)) = q(f(x)).
\]

If the target mapping satisfies \(g^\*(gx)\ne g^\*(x)\), then no \(q\circ f\) can represent \(g^\*\). Hence invariance to \(G\) is insufficient; the encoder must be equivariant, \(f(gx)=\rho(g)f(x)\), with a predictor that knows \(g\). ∎

---

## Appendix B — Implementation notes

- The new seam is `keyless.gaze`: `GazeFrame`, `GazeSource`, `SharedMemoryGazeSource`, `run_gaze_loop`. The daemon no longer imports a model.
- `MacOSPointerVisibility` in `keyless.platform.macos.pointer_mode` owns single-cursor arbitration. It is model-agnostic and must stay so.
- `keyless.platform.cua.agent_cursor` owns the cua overlay and synchronous visibility handoff. Its `set_visible` waits for the daemon to apply the toggle, so the hardware cursor never appears before the eye cursor is gone.
- The old calibration path, migration, and startup gate are gone. The app starts voice-only until a gaze publisher exists.
- The next code should be a `keyless.gaze.collect` writer and a `keyless.gaze.replay` reader, not a model.

---

## Appendix C — What "greenfield" means operationally

1. Do not import, copy, or paraphrase old model code, losses, gates, or anchors.
2. Do not use `legacy-gaze-v1` as a source of design ideas; it is an audit trail.
3. Every new model component must have a baseline, a held-out metric, and a kill criterion.
4. Every label must be traceable to a user action or a verified observation.
5. Every online update must be reversible and must not degrade the frozen evaluation set.
6. The cursor is a renderer. The decision is the product.
