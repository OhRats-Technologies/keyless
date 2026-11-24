"""NVIDIA Parakeet STT via onnx-asr (Parakeet TDT 0.6B v3, the only supported model)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from keyless.models.catalog import PARAKEET_TDT_V3_ID
from keyless.voice.stt.base import SttResult, SttStatus

logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS = ("CPUExecutionProvider",)
CUDA_PROVIDERS = ("CUDAExecutionProvider", "CPUExecutionProvider")

_ONNX_ASR_MODEL_NAMES: dict[str, str] = {
    PARAKEET_TDT_V3_ID: "nemo-parakeet-tdt-0.6b-v3",
}


class ParakeetSttEngine:
    """Offline / VAD-chunked recognition from the pinned local ONNX export."""

    name = PARAKEET_TDT_V3_ID

    def __init__(
        self,
        model_dir: Path | str,
        *,
        model_id: str = PARAKEET_TDT_V3_ID,
        quantization: str = "int8",
        providers: Sequence[str] | None = None,
        warmup: bool = True,
    ) -> None:
        import onnx_asr
        import onnxruntime

        if "CUDAExecutionProvider" in tuple(providers or DEFAULT_PROVIDERS):
            # Load pip-delivered CUDA/cuDNN (nvidia-cudnn-cu13) before any
            # session is created; on a CPU wheel the symbol is absent.
            preload = getattr(onnxruntime, "preload_dlls", None)
            if preload is not None:
                preload()

        self.model_dir = Path(model_dir)
        self.model_id = model_id
        self.name = model_id
        self.quantization = quantization
        self.compute_type = quantization
        self.providers = tuple(providers or DEFAULT_PROVIDERS)
        self.device = "cuda" if "CUDAExecutionProvider" in self.providers else "cpu"
        onnx_name = _ONNX_ASR_MODEL_NAMES.get(model_id)
        if onnx_name is None:
            raise ValueError(f"No onnx-asr model is wired for {model_id!r}")
        self._model = onnx_asr.load_model(
            onnx_name,
            path=str(self.model_dir),
            quantization=quantization,
            providers=list(self.providers),
        )
        if warmup:
            self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe_detailed(self, audio_f32: np.ndarray) -> SttResult:
        arr = np.asarray(audio_f32, dtype=np.float32).reshape(-1)
        if arr.size < 2560:  # <160ms (Parakeet TDT v3 supports >=160ms segments)
            return SttResult(text="")
        if float(np.max(np.abs(arr))) < 1e-4:
            return SttResult(text="")

        started = time.perf_counter()
        text = str(self._model.recognize(arr, sample_rate=16000) or "").strip()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # Parakeet does not expose no-speech/logprob metadata.  The VAD
        # endpointing and the lexical gates still apply in the listener.
        return SttResult(text=text, latency_ms=elapsed_ms)

    def transcribe(self, audio_f32: np.ndarray) -> str:
        return self.transcribe_detailed(audio_f32).text

    def status(self) -> SttStatus:
        return SttStatus(
            name=self.name,
            ready=True,
            device=self.device,
            compute_type=self.quantization,
            model=str(self.model_dir),
            detail="App-owned pinned model.",
        )

    def close(self) -> None:
        self._model = None
