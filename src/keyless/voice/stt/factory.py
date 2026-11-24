"""Parakeet STT selection and status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.models.catalog import DEFAULT_STT_ASSET_ID, get_asset
from keyless.models.downloader import DownloadError, DownloadProgress, download_asset
from keyless.models.store import ModelStore
from keyless.voice.stt.base import SttEngine
from keyless.voice.stt.parakeet import CUDA_PROVIDERS, DEFAULT_PROVIDERS, ParakeetSttEngine

ProgressCallback = Callable[[DownloadProgress], None]


class SttModelMissing(RuntimeError):
    """The app-owned Parakeet files are not downloaded or verified."""


@dataclass
class SttSelection:
    engine: SttEngine
    requested: str = "parakeet"

    @property
    def fell_back(self) -> bool:
        return False


def cuda_available() -> bool:
    try:
        import onnxruntime

        return "CUDAExecutionProvider" in onnxruntime.get_available_providers()
    except Exception:
        return False


def resolve_stt_provider(config: AppConfig) -> str:
    """Resolve the compute device: auto means CUDA when the runtime has it."""
    preference = (config.stt_provider or "auto").lower()
    if preference == "cuda" and cuda_available():
        return "cuda"
    if preference == "cpu":
        return "cpu"
    return "cuda" if cuda_available() else "cpu"


def _providers(config: AppConfig) -> tuple[str, ...]:
    if resolve_stt_provider(config) == "cuda":
        return CUDA_PROVIDERS
    return DEFAULT_PROVIDERS


def create_stt_engine(
    config: AppConfig,
    *,
    paths: AppPaths | None = None,
    allow_download: bool = False,
    progress: ProgressCallback | None = None,
) -> SttSelection:
    """Load the pinned Parakeet model; never substitute another engine."""
    asset = get_asset(config.stt_model_id or DEFAULT_STT_ASSET_ID)
    store = ModelStore(paths or current_paths())
    status = store.status(asset)
    if not status.ready and allow_download:
        try:
            download_asset(asset, paths=paths, store=store, progress=progress)
            status = store.status(asset, verify_hashes=True)
        except DownloadError as exc:
            raise SttModelMissing(f"Speech model download failed: {exc}") from exc
    if not status.ready:
        raise SttModelMissing(
            f"Speech model is {status.state.value}. Run `keyless models download stt`."
        )
    engine = ParakeetSttEngine(
        store.asset_dir(asset),
        model_id=asset.id,
        quantization=asset.quantization,
        providers=_providers(config),
    )
    return SttSelection(engine=engine)


def stt_report(config: AppConfig, *, paths: AppPaths | None = None) -> dict[str, object]:
    """Cheap status for `doctor` and the UI; does not load the engine."""
    asset = get_asset(config.stt_model_id or DEFAULT_STT_ASSET_ID)
    status = ModelStore(paths or current_paths()).status(asset)
    return {
        "requested": "parakeet",
        "selected": "parakeet" if status.ready else "missing",
        "model_id": asset.id,
        "display_name": asset.display_name,
        "total_bytes": asset.total_bytes,
        "parakeet": status.as_dict(),
        "cuda_available": cuda_available(),
    }
