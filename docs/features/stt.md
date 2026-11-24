# Speech-to-text (D10)

## Shipped contract

`keyless.voice.stt.base.SttEngine`:

```python
transcribe(audio_f32) -> str
transcribe_detailed(audio_f32) -> SttResult   # text, no_speech_prob, avg_logprob, latency_ms
status() -> SttStatus                          # name, ready, device, compute_type, model
close()
```

`create_stt_engine` loads the verified Parakeet files and raises an actionable
`SttModelMissing` if they are absent. There is no fallback engine and no
`stt_engine` setting. The setup/Models surfaces offer only the pinned download.

## Pinned Parakeet model

- Asset: `parakeet-tdt-0.6b-v3`
- Repository: `istupakov/parakeet-tdt-0.6b-v3-onnx`
- Revision: `8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce`
- License: CC-BY-4.0 (attribution: NVIDIA Parakeet TDT 0.6B v3, ONNX export by istupakov)
- Files: `config.json`, `vocab.txt`, `nemo128.onnx`, `encoder-model.int8.onnx`,
  `decoder_joint-model.int8.onnx` (about 670.6 MB)
- Language coverage: 25 European languages (v2 was English-only); Keyless voice
  commands stay English.

`parakeet-unified-en-0.6b` and `parakeet-tdt-0.6b-v2` are retired: the catalog
no longer ships them, `AppConfig.load` rewrites their stored ids to the v3
default, and the first daemon start deletes their model directories
(`models/catalog.py:LEGACY_STT_ASSET_IDS`).

Every file pins bytes and SHA-256 in `models/catalog.py`. The downloader writes
`<file>.partial`, resumes with HTTP Range, verifies SHA-256, promotes atomically,
and writes `receipt.json` only after all files verify. `models verify stt`
re-hashes the files. Existing complete files are reused; a corrupt file is kept
as `.corrupt-<timestamp>` instead of being deleted.

Compute is auto-detected (`stt_provider: "auto"`, hidden from Settings): the
CUDA execution provider when the pinned onnxruntime exposes it, otherwise CPU.
Precision is pinned to the asset (`int8`); neither is user-editable. There is
no fallback wrapper or cuBLAS preload.

## Benchmark

Method:

- 12 English command phrases synthesized with `edge-tts` (`en-US-AriaNeural`) and
  converted to 16 kHz mono PCM. These exercise the app's vocabulary
  ("click send", "scroll down", "click the subscribe button", ...).
- 40 real human command clips from
  `airimonda/ai231-me2-voice-commands` (`real_voice` and
  `FluentSpeechCommands` sources, holdout split). The clips are smart-home
  commands ("turn on the lights", "play a song", ...), not desktop commands.
  They are not redistributed in this repository.
- Parakeet loaded once with a warm-up, then each clip transcribed in sequence
  on CPU int8. Latency is wall time around `transcribe`.
- Exact match after lowercase/punctuation normalization; WER is word-level
  Levenshtein ÷ expected words.

Results:

| Engine | TTS exact | TTS WER | TTS median | TTS p95 | Real exact | Real WER | Real median | Real p95 | Real RTF |
|---|---|---|---|---|---|---|---|---|---|
| Parakeet TDT 0.6B v2 | 100% | 0.000 | 170 ms | 198 ms | 92.5% | 0.025 | 245 ms | 260 ms | 0.049 |

Raw per-clip results are in [stt-benchmark-tts.json](stt-benchmark-tts.json) and [stt-benchmark-real.json](stt-benchmark-real.json). Parakeet was faster and had lower WER than the 0.1.0 `base.en` comparison. The
0.1.1 release removes `base.en`; the historical per-clip results remain in the
JSON files below.

## Gaps

- No natural human recordings of this app's desktop command phrases were
  available. The acoustic path was tested with the Razer Seiren Mini, but the
  only output sink was HDMI and the played TTS was too quiet to transcribe, so
  the benchmark uses files rather than over-the-air recordings.
- CUDA, DirectML, CoreML, and macOS/Windows execution were not tested. Only CPU
  int8 on this Linux machine is verified.
- Parakeet does not expose `no_speech_prob`/`avg_logprob`; the listener keeps its
  VAD and lexical gates, and Parakeet results carry neutral confidence.
- Parakeet has no `initial_prompt` vocabulary biasing; the dispatcher's fuzzy
  matching remains the command robustness layer.

## Reproduce

```bash
uv run keyless models download stt
uv run keyless models verify stt
uv run keyless test-audio --seconds 5 --transcribe
uv run python scripts/stt_benchmark.py /path/to/manifest.json --engine parakeet
```

Manifest format: `[{"wav": "...", "text": "expected words"}, ...]`.
