"""Run the real candidate pipeline over the synthetic 3D lab renders.

Reads ``ground_truth.json`` + ``screen_*.png`` produced by ``build_scene.py``,
feeds each screenshot through :class:`keyless.gaze.candidate_generator.CandidateGenerator`
with the same RapidOCR backend ``keyless gaze record`` uses, matches the
ground-truth click to a candidate, and writes a schema-v0 session directory
plus a report.

    .venv/bin/python experiments/synth_lab/make_session.py [--lab-dir DIR]

Checks, with exact ground truth available:

1. candidate recall — is the gazed-at button among the generated candidates?
2. the standard metric table (``baseline_top1`` etc.) over the recorded session
3. the session replays cleanly with ``keyless gaze replay``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from keyless.gaze.candidate_generator import CandidateGenerator  # noqa: E402
from keyless.gaze.collect import SessionCollector  # noqa: E402
from keyless.gaze.metrics import metric_table  # noqa: E402
from keyless.gaze.schema import make_event  # noqa: E402


def load_bgr(path: Path) -> np.ndarray:
    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lab-dir",
        type=Path,
        default=REPO_ROOT / ".tmp-synth-lab",
        help="Directory written by build_scene.py",
    )
    parser.add_argument(
        "--session-id",
        default="synth-lab-01",
        help="Session id to write under <lab-dir>/sessions/ (use 2+ ids for probe splits)",
    )
    parser.add_argument(
        "--gaze-jitter",
        type=float,
        default=0.0,
        help="Uniform ±px offset on the gaze/click pixel (seeded by the session id); "
        "keep it under half the smallest button extent so labels stay correct.",
    )
    args = parser.parse_args()
    lab_dir = args.lab_dir.resolve()
    truth = json.loads((lab_dir / "ground_truth.json").read_text())
    rng = np.random.default_rng(abs(hash(args.session_id)) % (2**32))

    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()

    def ocr_fn(image):
        result, _ = engine(image)
        items = []
        for box, text, score in result or []:
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            items.append(
                ((int(min(xs)), int(min(ys)), int(max(xs)) - int(min(xs)), int(max(ys)) - int(min(ys))), text, float(score))
            )
        return items

    generator = CandidateGenerator(ax_bridge=None, dom_bridge=None, ocr_fn=ocr_fn)

    # SessionCollector builds <paths.data_dir>/sessions/<session_id>/;
    # start from a clean directory so reruns do not append to old events.
    session_dir = lab_dir / "sessions" / args.session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    collector = SessionCollector(
        SimpleNamespace(data_dir=lab_dir, settings_path=lab_dir / "settings.json"),
        session_id=args.session_id,
    )

    events: list[dict] = []
    hits = 0
    for frame in truth["frames"]:
        image = load_bgr(lab_dir / frame["screen_png"])
        digest = hashlib.sha256(image.tobytes()).hexdigest()
        result = generator.generate("synth", "synth-window", digest, image=image)
        candidates = result.candidates

        button = next(b for b in truth["buttons"] if b["id"] == frame["target_id"])
        bx, by, bw, bh = button["bbox"]
        click_x, click_y = frame["gaze_pixel"]
        if args.gaze_jitter:
            click_x += float(rng.uniform(-args.gaze_jitter, args.gaze_jitter))
            click_y += float(rng.uniform(-args.gaze_jitter, args.gaze_jitter))

        matched = None
        for candidate in candidates:
            cx, cy, cw, ch = candidate.bbox
            if cx <= click_x <= cx + cw and cy <= click_y <= cy + ch:
                matched = candidate
                break
        if matched is not None:
            hits += 1
        events.append(
            make_event(
                args.session_id,
                1_000_000_000 + frame["index"] * 1_000_000_000,
                screen={
                    "candidates": [
                        {
                            "id": candidate.id,
                            "bbox": list(candidate.bbox),
                            "role": candidate.role,
                            "text": candidate.text,
                            "score": candidate.confidence,
                        }
                        for candidate in candidates
                        if candidate.id != ""
                    ]
                },
                prediction={"top": None, "p": 0.0, "entropy": 0.0, "pixel": [click_x, click_y]},
                interaction={
                    "kind": "click",
                    "mouse": [click_x, click_y],
                    "target": matched.id if matched else None,
                    "accepted": matched is not None,
                },
            )
        )
        print(
            f"frame {frame['index']:02d} target={frame['target_id']:<10} "
            f"candidates={len([c for c in candidates if c.id])} "
            f"texts={[c.text for c in candidates if c.text][:6]} "
            f"matched={'yes' if matched else 'NO'}"
        )
        collector.record(events[-1])
    collector.close()

    recall = hits / len(truth["frames"])
    table = metric_table(events)
    report = {
        "frames": len(truth["frames"]),
        "ground_truth_recall": recall,
        "metric_table": table,
        "session_dir": str(collector.session_dir),
    }
    (lab_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\ncandidate recall vs ground truth: {recall:.2%}")
    print("metric table:", json.dumps(table, indent=2, default=str))
    print(f"session written to {collector.session_dir}")
    return 0 if recall >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
