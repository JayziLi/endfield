from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_MODEL_RELATIVE_PATH = Path("models") / "yolov5n.onnx"
DEFAULT_MODEL_URL = "https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx"
DEFAULT_MODEL_SHA256 = "04f0e55c26f58d17145b36045780fe1250d5bd2187543e11568e5141d05b3262"
DEFAULT_MODEL_SIZE = 3_981_910


def ensure_default_model(
    config_path: str | Path,
    configured_path: str | Path,
    *,
    url: str = DEFAULT_MODEL_URL,
    expected_sha256: str = DEFAULT_MODEL_SHA256,
    expected_size: int = DEFAULT_MODEL_SIZE,
    opener=urlopen,
) -> Path:
    """Download the checked default model only when that exact path is configured."""

    config_directory = Path(config_path).resolve().parent
    configured = Path(configured_path)
    target = configured if configured.is_absolute() else config_directory / configured
    target = target.resolve()
    default_target = (config_directory / DEFAULT_MODEL_RELATIVE_PATH).resolve()

    if target != default_target or target.is_file():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(f"{target.suffix}.part")
    digest = hashlib.sha256()
    downloaded = 0
    print(f"Downloading the default YOLOv5n model to {target} ...", flush=True)
    request = Request(url, headers={"User-Agent": "Endfield-model-bootstrap/0.2"})
    try:
        with opener(request, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
        if downloaded != expected_size:
            raise RuntimeError(
                f"Default model size mismatch: expected {expected_size} bytes, got {downloaded}"
            )
        actual_sha256 = digest.hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            raise RuntimeError(
                "Default model checksum mismatch; the downloaded file was discarded "
                f"(expected {expected_sha256}, got {actual_sha256})"
            )
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    print("Default YOLOv5n model is ready.", flush=True)
    return target


__all__ = [
    "DEFAULT_MODEL_RELATIVE_PATH",
    "DEFAULT_MODEL_SHA256",
    "DEFAULT_MODEL_SIZE",
    "DEFAULT_MODEL_URL",
    "ensure_default_model",
]
