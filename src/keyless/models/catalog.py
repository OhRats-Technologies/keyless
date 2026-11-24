"""Pinned, app-owned model catalog (D2/D10).

Every entry pins a repository revision and the exact bytes of every file.  The
downloader verifies size and SHA-256 before promoting a partial file.
"""

from __future__ import annotations

from dataclasses import dataclass

HF_BASE = "https://huggingface.co"

PARAKEET_TDT_V3_ID = "parakeet-tdt-0.6b-v3"
UNDERDOG_WOOF_ID = "underdog-woof"

# Retired STT assets. Listed so config migration and the model store can
# recognise and remove them; they are not downloadable.
LEGACY_STT_ASSET_IDS = ("parakeet-unified-en-0.6b", "parakeet-tdt-0.6b-v2")


@dataclass(frozen=True)
class ModelFile:
    name: str
    bytes: int
    sha256: str
    remote_name: str | None = None


@dataclass(frozen=True)
class ModelAsset:
    id: str
    display_name: str
    engine: str
    quantization: str
    revision: str
    repo: str
    license: str
    attribution: str
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def url(self, file: ModelFile) -> str:
        remote = file.remote_name or file.name
        return f"{HF_BASE}/{self.repo}/resolve/{self.revision}/{remote}"

    def file(self, name: str) -> ModelFile:
        for item in self.files:
            if item.name == name:
                return item
        raise KeyError(name)


PARAKEET_TDT_0_6B_V3 = ModelAsset(
    id=PARAKEET_TDT_V3_ID,
    display_name="Parakeet TDT 0.6B v3 (Multilingual, int8)",
    engine="onnx-asr",
    quantization="int8",
    revision="8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
    repo="istupakov/parakeet-tdt-0.6b-v3-onnx",
    license="CC-BY-4.0",
    attribution="NVIDIA Parakeet TDT 0.6B v3 (CC-BY-4.0), ONNX export by istupakov",
    files=(
        ModelFile(
            name="config.json",
            bytes=97,
            sha256="666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466",
        ),
        ModelFile(
            name="vocab.txt",
            bytes=93939,
            sha256="d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
        ),
        ModelFile(
            name="nemo128.onnx",
            bytes=139764,
            sha256="a9fde1486ebfcc08f328d75ad4610c67835fea58c73ba57e3209a6f6cf019e9f",
        ),
        ModelFile(
            name="encoder-model.int8.onnx",
            bytes=652183999,
            sha256="6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09",
        ),
        ModelFile(
            name="decoder_joint-model.int8.onnx",
            bytes=18202004,
            sha256="eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70",
        ),
    ),
)

UNDERDOG_WOOF = ModelAsset(
    id=UNDERDOG_WOOF_ID,
    display_name="Underdog Woof TTS (English, int8)",
    engine="underdog-woof",
    quantization="int8",
    revision="be702960539777fc3f698e54335276a8391b00f9",
    repo="ConwayResearch/Underdog-Woof",
    license="underdog-non-commercial-1.0",
    attribution="Voice by Underdog Woof (Conway Research, underdog-non-commercial-1.0)",
    files=(
        ModelFile(
            name="LICENSE",
            bytes=7612,
            sha256="784c4a353d15f370ff387f74bde64f50a2041df9c6f2338c3d8a2f781b944de5",
        ),
        ModelFile(
            name="NOTICE",
            bytes=919,
            sha256="0849de251c109869d24f8a0b520260afa10594334ae3d0c06b75e68be1e2c623",
        ),
        ModelFile(
            name="woof_int8.npz",
            bytes=40595426,
            sha256="d895fbbe84b8f4f461e3e94ab48b1164353b1988ce8d365f5aa8263d0de1203e",
        ),
        ModelFile(
            name="underdog_woof/__init__.py",
            bytes=3002,
            sha256="aaacf02b28eabce10280a60c2699d4e40d4ca5aa22170d169b64943fee7849f7",
        ),
        ModelFile(
            name="underdog_woof/__main__.py",
            bytes=832,
            sha256="737f9823169c5337f49dbc27ab51f0ceab5d5d0302e6c0f3aad051ad13562a3b",
        ),
        ModelFile(
            name="underdog_woof/normalize.py",
            bytes=47736,
            sha256="f6d7c39ec8a58bb6ab1d36ee6ee0eae3c543aac8dddaa29f4065e72706c7c044",
        ),
        ModelFile(
            name="underdog_woof/respell.py",
            bytes=4248,
            sha256="6bf6bd4760474b991230a523d3f17b423012646737000b8974bd257875308a71",
        ),
        ModelFile(
            name="underdog_woof/respell.tsv",
            bytes=590622,
            sha256="4c700d4015390bb6e34c1f7362f87ea75acfb45144d2fbc45cf00d5739b66abc",
        ),
        ModelFile(
            name="underdog_woof/runtime.py",
            bytes=15964,
            sha256="e22d1e2a92f4d28c3784abbb889d77609dcecd1856bf6e65f03432b803a8d220",
        ),
        ModelFile(
            name="underdog_woof/textsplit.py",
            bytes=2982,
            sha256="f46ae52b24fae666ebe1dc55ebf644da757f238f1bb0408a3cc2dadb39eaf6e8",
        ),
    ),
)

ASSETS: dict[str, ModelAsset] = {
    PARAKEET_TDT_0_6B_V3.id: PARAKEET_TDT_0_6B_V3,
    UNDERDOG_WOOF.id: UNDERDOG_WOOF,
}

DEFAULT_STT_ASSET_ID = PARAKEET_TDT_0_6B_V3.id
DEFAULT_TTS_ASSET_ID = UNDERDOG_WOOF.id


def get_asset(asset_id: str) -> ModelAsset:
    try:
        return ASSETS[asset_id]
    except KeyError as exc:
        raise KeyError(f"Unknown model asset: {asset_id!r}") from exc


def stt_assets() -> list[ModelAsset]:
    return [asset for asset in ASSETS.values() if asset.engine == "onnx-asr"]


def tts_assets() -> list[ModelAsset]:
    return [asset for asset in ASSETS.values() if asset.engine == "underdog-woof"]
