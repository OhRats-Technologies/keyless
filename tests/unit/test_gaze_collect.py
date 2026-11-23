"""Session collector: bounded writer, JSONL round-trip, blobs, explicit redaction."""

from __future__ import annotations

import hashlib
import io
import json

import numpy as np
import pytest

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.collect import SessionCollector
from keyless.gaze.schema import make_event, validate_event


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    resolved = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    resolved.ensure_directories()
    return resolved


def _frame_event(ts_ns: int, session_id: str = "s") -> dict:
    return make_event(
        session_id,
        ts_ns,
        frame={
            "cams": ["cam1", "cam2"],
            "landmarks": [[0.0, 0.0, 0.0]],
            "head_pose": {"yaw": 1.0, "pitch": -2.0, "roll": 0.5},
            "eye_crops": {"left": "a" * 64, "right": "b" * 64},
            "features": [0.1, 0.2],
        },
    )


def _screen_event(ts_ns: int, session_id: str = "s") -> dict:
    return make_event(
        session_id,
        ts_ns,
        screen={
            "display_id": 1,
            "app": "Chrome",
            "window_id": 42,
            "screenshot": "c" * 64,
            "candidates": [
                {
                    "id": "c1",
                    "bbox": [0.0, 0.0, 10.0, 10.0],
                    "role": "button",
                    "text": "Reply",
                    "visual": "d" * 64,
                    "score": 0.8,
                }
            ],
        },
        prediction={"top": "c1", "p": 0.8, "entropy": 0.3, "pixel": [3.0, 4.0]},
        interaction={
            "kind": "click",
            "mouse": [3.0, 4.0],
            "target": "c1",
            "voice": None,
            "accepted": True,
            "latency_ms": 120,
        },
    )


def test_bounded_queue_drops_instead_of_blocking(paths: AppPaths) -> None:
    collector = SessionCollector(paths, model_version="test-v0", queue_size=4)
    # A visual landmark payload makes the writer's serialize+flush slower than
    # the producer, so a 4-slot queue must overflow.
    accepted = 0
    for index in range(200):
        event = make_event(
            collector.session_id,
            ts_ns=1_000 + index,
            frame={"landmarks": [float(index)] * 1500},
        )
        accepted += collector.record(event)
    collector.close()

    stats = collector.stats()
    assert stats["written"] + stats["dropped_events"] == 200
    assert stats["dropped_events"] > 0
    assert stats["written"] == accepted
    lines = collector.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == stats["written"]


def test_jsonl_round_trip_preserves_fields(paths: AppPaths) -> None:
    collector = SessionCollector(paths, session_id="roundtrip", model_version="test-v0")
    events = [
        _frame_event(1),
        _screen_event(2),
        make_event("roundtrip", 3, prediction={"top": None, "p": 0.0, "entropy": 0.0}),
        make_event(
            "roundtrip",
            4,
            interaction={"kind": "correction", "target": "c1", "accepted": False},
        ),
    ]
    for event in events:
        event["session_id"] = "roundtrip"
        collector.record(event)
    collector.close()

    lines = collector.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    loaded = [json.loads(line) for line in lines]
    assert loaded == events
    for event in loaded:
        assert validate_event(event) == []


def test_blob_store_round_trip_and_dedup(paths: AppPaths) -> None:
    collector = SessionCollector(paths, session_id="blobs")
    png = b"\x89PNG\r\n\x1a\n" + b"pixels"
    digest = collector.put_blob(png, "png")

    assert digest == hashlib.sha256(png).hexdigest()
    assert collector.blob_path(digest, "png").name == f"{digest}.png"
    assert collector.get_blob(digest, "png") == png

    buffer = io.BytesIO()
    np.savez(buffer, frame=np.zeros((2, 3), dtype=np.float32))
    npz = buffer.getvalue()
    npz_digest = collector.put_blob(npz, "npz")
    assert npz_digest == hashlib.sha256(npz).hexdigest()
    assert collector.get_blob(npz_digest, "npz") == npz
    assert np.load(io.BytesIO(collector.get_blob(npz_digest, "npz")))["frame"].shape == (2, 3)

    before = sorted(p.name for p in collector.blobs_dir.iterdir())
    assert collector.put_blob(png, "png") == digest
    assert sorted(p.name for p in collector.blobs_dir.iterdir()) == before
    collector.close()


def test_redaction_is_explicit_and_recorded(paths: AppPaths) -> None:
    redacting = SessionCollector(paths, session_id="redact-on", redact_text=True)
    redacting.record(_screen_event(1, redacting.session_id))
    redacting.close()
    event = json.loads(redacting.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["screen"]["candidates"][0]["text"] == "[redacted]"
    assert redacting.meta_path.read_text(encoding="utf-8").find('"redact_text": true') >= 0

    verbatim = SessionCollector(paths, session_id="redact-off")
    verbatim.record(_screen_event(1, verbatim.session_id))
    verbatim.close()
    event = json.loads(verbatim.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["screen"]["candidates"][0]["text"] == "Reply"
    meta = json.loads(verbatim.meta_path.read_text(encoding="utf-8"))
    assert meta["redact_text"] is False


def test_redaction_covers_voice_but_not_other_fields(paths: AppPaths) -> None:
    collector = SessionCollector(paths, session_id="redact-voice", redact_text=True)
    collector.record(
        make_event(
            "redact-voice",
            1,
            interaction={
                "kind": "voice",
                "voice": "open the reply box",
                "mouse": [1.0, 2.0],
                "accepted": True,
            },
        )
    )
    collector.close()
    event = json.loads(collector.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["interaction"]["voice"] == "[redacted]"
    assert event["interaction"]["mouse"] == [1.0, 2.0]
    assert event["interaction"]["kind"] == "voice"


def test_meta_records_provenance(paths: AppPaths) -> None:
    collector = SessionCollector(paths, session_id="meta", model_version="gaze-v0")
    meta = json.loads(collector.meta_path.read_text(encoding="utf-8"))

    assert meta["schema_version"] == 0
    assert meta["session_id"] == "meta"
    assert isinstance(meta["started_ts_ns"], int)
    assert meta["model_version"] == "gaze-v0"
    assert len(meta["config_hash"]) == 64
    int(meta["config_hash"], 16)
    assert meta["git_commit"] == "unknown" or len(meta["git_commit"]) == 40
    assert meta["redact_text"] is False

    collector.record(_frame_event(1, collector.session_id))
    collector.close()
    meta = json.loads(collector.meta_path.read_text(encoding="utf-8"))
    assert meta["ended_ts_ns"] >= meta["started_ts_ns"]
    assert meta["counts"] == {"events": 1, "dropped": 0}
    assert collector.stats()["written"] == 1


def test_sessions_are_session_keyed_under_data_dir(paths: AppPaths) -> None:
    first = SessionCollector(paths, session_id="explicit")
    second = SessionCollector(paths)
    assert first.session_dir == paths.data_dir / "sessions" / "explicit"
    assert second.session_dir.parent == paths.data_dir / "sessions"
    assert len(second.session_id) == 32
    int(second.session_id, 16)
    assert second.session_dir.name == second.session_id
    first.close()
    second.close()


def test_invalid_event_is_rejected_before_disk(paths: AppPaths) -> None:
    collector = SessionCollector(paths)
    with pytest.raises(ValueError):
        collector.record({"session_id": collector.session_id, "ts_ns": "soon"})
    with pytest.raises(ValueError):
        collector.record({"session_id": "someone-else", "ts_ns": 1})
    with pytest.raises(ValueError):
        collector.record({"session_id": collector.session_id, "ts_ns": 1})
    collector.close()
    assert collector.stats()["written"] == 0
