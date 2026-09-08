from __future__ import annotations

import configparser
import tomllib
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from typing import get_type_hints

import tomli_w


@dataclass(frozen=True, slots=True)
class InputConfig:
    mode: str = "udp_video"


@dataclass(frozen=True, slots=True)
class UiConfig:
    language: str = "zh"


@dataclass(frozen=True, slots=True)
class UdpConfig:
    host: str = "0.0.0.0"
    port: int = 4455
    width: int = 320
    height: int = 320
    timeout_seconds: float = 2.0
    socket_buffer_bytes: int = 1_048_576
    fifo_packets: int = 512


@dataclass(frozen=True, slots=True)
class ObsConfig:
    host: str
    port: int = 4455
    password: str = ""
    source_name: str = ""
    width: int = 320
    height: int = 320
    jpeg_quality: int = 55
    timeout_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class ModelConfig:
    path: Path
    provider: str = "cuda"
    cuda_graph: bool = True
    gpu_preprocess: bool = True
    output_format: str = "yolov5"
    output_layout: str = "auto"
    confidence: float = 0.375
    iou: float = 0.5


@dataclass(frozen=True, slots=True)
class KmboxConfig:
    enabled: bool = True
    host: str = "192.168.2.100"
    port: int = 8808
    uuid: str = ""
    monitor_port: int = 5002
    encrypted: bool = True
    timeout_seconds: float = 0.02
    connect_attempts: int = 10


@dataclass(frozen=True, slots=True)
class AimConfig:
    enabled: bool = True
    trigger: str = "right"
    target_class: int = 0
    target_y_ratio: float = 0.4
    kp_min: float = 0.1
    kp_max: float = 0.164
    kp_growth: float = 0.167
    smoothing: float = 0.35
    deadzone: float = 1.5
    max_step: int = 30
    fov_radius: float = 150.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    input: InputConfig
    ui: UiConfig
    udp: UdpConfig
    obs: ObsConfig
    model: ModelConfig
    kmbox: KmboxConfig
    aim: AimConfig


def load_config(path: str | Path, *, validate_model: bool = True) -> AppConfig:
    config_path = Path(path).resolve()
    raw = _read_config(config_path)

    input_config = InputConfig(**raw.get("input", {}))
    ui = UiConfig(**raw.get("ui", {}))
    udp = UdpConfig(**raw.get("udp", {}))
    obs = ObsConfig(**raw["obs"])
    model_raw = dict(raw["model"])
    model_raw["path"] = Path(model_raw["path"])
    # Layout is derived from every model's actual output shape. Keep accepting
    # the old setting so existing files migrate without user intervention.
    model_raw["output_layout"] = "auto"
    model = ModelConfig(**model_raw)
    kmbox = KmboxConfig(**raw.get("kmbox", {}))
    aim_raw = dict(raw.get("aim", {}))
    aim_raw.pop("gain_x", None)
    aim_raw.pop("gain_y", None)
    aim = AimConfig(**aim_raw)

    if validate_model and not model.path.is_file():
        raise FileNotFoundError(f"ONNX model not found: {model.path}")
    if input_config.mode not in {"udp_video", "udp_jpeg", "obs_websocket"}:
        raise ValueError(f"Unsupported input mode: {input_config.mode}")
    if ui.language not in {"zh", "en"}:
        raise ValueError(f"Unsupported runtime language: {ui.language}")
    if not 1 <= udp.port <= 65535:
        raise ValueError("UDP port must be between 1 and 65535")
    if udp.width <= 0 or udp.height <= 0:
        raise ValueError("UDP frame dimensions must be positive")
    if udp.socket_buffer_bytes <= 0 or udp.fifo_packets <= 0:
        raise ValueError("UDP buffer sizes must be positive")
    if obs.width <= 0 or obs.height <= 0:
        raise ValueError("OBS screenshot dimensions must be positive")
    if not 1 <= obs.jpeg_quality <= 100:
        raise ValueError("OBS JPEG quality must be between 1 and 100")
    if model.output_format not in {"yolov5", "yolov8", "end2end"}:
        raise ValueError(f"Unsupported YOLO output format: {model.output_format}")
    if model.output_layout != "auto":
        raise ValueError(f"Unsupported YOLO output layout: {model.output_layout}")
    if not 0 <= model.confidence <= 1 or not 0 <= model.iou <= 1:
        raise ValueError("Detection confidence and IoU must be between 0 and 1")
    if aim.trigger not in {"left", "right", "side1", "side2"}:
        raise ValueError(f"Unsupported aim trigger: {aim.trigger}")
    if aim.target_class < 0:
        raise ValueError("Target class must be zero or greater")
    if not 0 <= aim.target_y_ratio <= 1:
        raise ValueError("Aim position must be between 0 and 1")
    if aim.kp_min < 0 or aim.kp_max < aim.kp_min or aim.kp_growth < 0:
        raise ValueError("Dynamic Kp requires 0 <= minimum <= maximum and a non-negative growth slope")
    if aim.fov_radius <= 0:
        raise ValueError("FOV radius must be positive")
    return AppConfig(input=input_config, ui=ui, udp=udp, obs=obs, model=model, kmbox=kmbox, aim=aim)


def save_config(config: AppConfig, path: str | Path) -> None:
    raw = asdict(config)
    raw["model"]["path"] = str(config.model.path).replace("\\", "/")
    target = Path(path)
    temporary = target.with_name(f".{target.stem}.tmp{target.suffix}")
    try:
        _write_config(temporary, raw)
        load_config(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _read_config(path: Path) -> dict:
    if path.suffix.lower() != ".txt":
        with path.open("rb") as handle:
            return tomllib.load(handle)

    parser = configparser.ConfigParser(interpolation=None)
    with path.open("r", encoding="utf-8-sig") as handle:
        parser.read_file(handle)
    section_types = {
        "input": InputConfig,
        "ui": UiConfig,
        "udp": UdpConfig,
        "obs": ObsConfig,
        "model": ModelConfig,
        "kmbox": KmboxConfig,
        "aim": AimConfig,
    }
    return {
        name: _convert_section(dict(parser[name]), section_type)
        for name, section_type in section_types.items()
        if parser.has_section(name)
    }


def _convert_section(values: dict[str, str], section_type: type) -> dict:
    hints = get_type_hints(section_type)
    converted: dict = {}
    for key, value in values.items():
        expected = hints.get(key, str)
        if expected is bool:
            converted[key] = value.strip().lower() in {"1", "true", "yes", "on"}
        elif expected is int:
            converted[key] = int(value)
        elif expected is float:
            converted[key] = float(value)
        else:
            converted[key] = value
    return converted


def _write_config(path: Path, raw: dict) -> None:
    if path.suffix.lower() != ".txt":
        path.write_text(tomli_w.dumps(raw), encoding="utf-8")
        return
    parser = configparser.ConfigParser(interpolation=None)
    for section, values in raw.items():
        parser[section] = {key: str(value) for key, value in values.items()}
    buffer = StringIO()
    parser.write(buffer)
    content = "\n".join(line.rstrip() for line in buffer.getvalue().splitlines())
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
