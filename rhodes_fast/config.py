from __future__ import annotations

import configparser
import json
import tomllib
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path
from typing import get_type_hints

import tomli_w

# 轨迹长度滑条的范围(秒)。
TRAIL_MIN_SECONDS = 0.2
TRAIL_MAX_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class InputConfig:
    mode: str = "udp_video"


@dataclass(frozen=True, slots=True)
class UiConfig:
    language: str = "zh"
    # 上次载入或保存的预设名, 空串 = 没用预设。只是个指针, 设置本身仍然存在这份文件里。
    preset: str = ""
    # 实时预览里的轨迹长度。是看的偏好, 不是一套游戏配置, 所以不进预设。
    # 画面/轨迹/最优路径这几个勾选框不存: 每次打开都只勾「画面」。
    trail_seconds: float = 0.5


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
    provider: str = "auto"
    cuda_graph: bool = True
    gpu_preprocess: bool = True
    output_format: str = "yolov5"
    output_layout: str = "auto"
    confidence: float = 0.375
    iou: float = 0.5


@dataclass(frozen=True, slots=True)
class KmboxConfig:
    enabled: bool = True
    host: str = "192.168.1.100"
    port: int = 8808
    uuid: str = ""
    monitor_port: int = 5002
    encrypted: bool = True
    timeout_seconds: float = 0.02
    connect_attempts: int = 10


@dataclass(frozen=True, slots=True)
class AimProfileConfig:
    enabled: bool = True
    trigger: str = "right"
    kp_min: float = 0.1
    kp_max: float = 0.164
    kp_growth: float = 0.167
    target_class: int = 0
    target_y_ratio: float = 0.4
    fov_radius: float = 150.0
    algorithm: str = "p"
    algorithm_params: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AimConfig:
    target_class: int = 0
    target_y_ratio: float = 0.4
    smoothing: float = 1.0
    deadzone: float = 2.0
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
    aim_profile_1: AimProfileConfig = field(default_factory=AimProfileConfig)
    aim_profile_2: AimProfileConfig = field(
        default_factory=lambda: AimProfileConfig(enabled=False, trigger="left")
    )

    @property
    def aim_profiles(self) -> tuple[AimProfileConfig, AimProfileConfig]:
        return (self.aim_profile_1, self.aim_profile_2)


def load_config(path: str | Path, *, validate_model: bool = True) -> AppConfig:
    config_path = Path(path).resolve()
    raw = _read_config(config_path)

    input_config = InputConfig(**raw.get("input", {}))
    ui_raw = dict(raw.get("ui", {}))
    # 上一个版本把轨迹的勾选状态也存了进来, 现在每次打开都重置, 读到就扔掉。
    ui_raw.pop("trail_enabled", None)
    ui_raw.pop("trail_optimal_path", None)
    ui = UiConfig(**ui_raw)
    udp = UdpConfig(**raw.get("udp", {}))
    obs = ObsConfig(**raw["obs"])
    model_raw = dict(raw["model"])
    model_path = Path(model_raw["path"])
    if not model_path.is_absolute():
        model_path = config_path.parent / model_path
    model_raw["path"] = model_path.resolve()
    # Layout is derived from every model's actual output shape. Keep accepting
    # the old setting so existing files migrate without user intervention.
    model_raw["output_layout"] = "auto"
    model = ModelConfig(**model_raw)
    kmbox = KmboxConfig(**raw.get("kmbox", {}))
    aim_raw = dict(raw.get("aim", {}))
    aim_raw.pop("gain_x", None)
    aim_raw.pop("gain_y", None)
    legacy_profile = {
        "enabled": _as_bool(aim_raw.pop("enabled", True)),
        "trigger": str(aim_raw.pop("trigger", "right")),
        "kp_min": float(aim_raw.pop("kp_min", 0.1)),
        "kp_max": float(aim_raw.pop("kp_max", 0.164)),
        "kp_growth": float(aim_raw.pop("kp_growth", 0.167)),
    }
    aim = AimConfig(**aim_raw)
    # Target settings used to be shared in [aim]; they now live in each aim
    # profile. Old files only carry them in [aim], so inherit them as defaults.
    shared_target_defaults = {
        "target_class": aim.target_class,
        "target_y_ratio": aim.target_y_ratio,
        "fov_radius": aim.fov_radius,
    }
    profile_1_raw = {**shared_target_defaults, **raw.get("aim_profile_1", legacy_profile)}
    profile_1_raw.pop("gain_x", None)
    profile_1_raw.pop("gain_y", None)
    profile_1 = AimProfileConfig(**profile_1_raw)
    profile_2_raw = raw.get("aim_profile_2")
    profile_2 = (
        AimProfileConfig(
            **{key: value for key, value in {**shared_target_defaults, **profile_2_raw}.items() if key not in {"gain_x", "gain_y"}}
        )
        if profile_2_raw is not None
        else AimProfileConfig(
            enabled=False,
            trigger=_unused_trigger(profile_1.trigger),
            kp_min=profile_1.kp_min,
            kp_max=profile_1.kp_max,
            kp_growth=profile_1.kp_growth,
            target_class=aim.target_class,
            target_y_ratio=aim.target_y_ratio,
            fov_radius=aim.fov_radius,
        )
    )

    if validate_model and not model.path.is_file():
        raise FileNotFoundError(f"ONNX model not found: {model.path}")
    if input_config.mode not in {"udp_video", "udp_jpeg", "obs_websocket"}:
        raise ValueError(f"Unsupported input mode: {input_config.mode}")
    if ui.language not in {"zh", "en"}:
        raise ValueError(f"Unsupported runtime language: {ui.language}")
    if not TRAIL_MIN_SECONDS <= ui.trail_seconds <= TRAIL_MAX_SECONDS:
        # 手改 settings.txt 才会走到这里, GUI 的滑条出不了这个范围。
        raise ValueError(
            f"Trail length must be between {TRAIL_MIN_SECONDS} and {TRAIL_MAX_SECONDS} seconds"
        )
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
    if aim.target_class < 0:
        raise ValueError("Target class must be zero or greater")
    if not 0 <= aim.target_y_ratio <= 1:
        raise ValueError("Aim position must be between 0 and 1")
    if aim.fov_radius <= 0:
        raise ValueError("FOV radius must be positive")
    _validate_aim_profiles((profile_1, profile_2))
    return AppConfig(
        input=input_config,
        ui=ui,
        udp=udp,
        obs=obs,
        model=model,
        kmbox=kmbox,
        aim=aim,
        aim_profile_1=profile_1,
        aim_profile_2=profile_2,
    )


def save_config(config: AppConfig, path: str | Path) -> None:
    target = Path(path)
    raw = asdict(config)
    model_path = config.model.path.resolve()
    try:
        stored_model_path = model_path.relative_to(target.resolve().parent)
    except ValueError:
        stored_model_path = model_path
    raw["model"]["path"] = str(stored_model_path).replace("\\", "/")
    temporary = target.with_name(f".{target.stem}.tmp{target.suffix}")
    try:
        _write_config(temporary, raw)
        load_config(temporary, validate_model=False)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def default_config() -> AppConfig:
    """Return the safe configuration used by a first-time installation."""

    aim = AimConfig()
    return AppConfig(
        input=InputConfig(),
        ui=UiConfig(),
        udp=UdpConfig(),
        obs=ObsConfig(host="127.0.0.1"),
        model=ModelConfig(path=Path("models/yolov5n.onnx")),
        kmbox=KmboxConfig(enabled=False),
        aim=aim,
        aim_profile_1=AimProfileConfig(
            target_class=aim.target_class,
            target_y_ratio=aim.target_y_ratio,
            fov_radius=aim.fov_radius,
        ),
        aim_profile_2=AimProfileConfig(
            enabled=False,
            trigger="left",
            target_class=aim.target_class,
            target_y_ratio=aim.target_y_ratio,
            fov_radius=aim.fov_radius,
        ),
    )


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
        "aim_profile_1": AimProfileConfig,
        "aim_profile_2": AimProfileConfig,
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
        elif expected is dict or getattr(expected, "__origin__", None) is dict:
            # INI 一行只能是字符串, 所以字典按 JSON 存。TOML 路径不走这里,
            # tomli_w 原生支持嵌套表。
            converted[key] = json.loads(value) if value.strip() else {}
        else:
            converted[key] = value
    return converted


def _unused_trigger(primary: str) -> str:
    return "left" if primary != "left" else "right"


def _as_bool(value: object) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}


def _validate_aim_profiles(profiles: tuple[AimProfileConfig, AimProfileConfig]) -> None:
    supported = {"left", "right", "side1", "side2"}
    for profile in profiles:
        if profile.trigger not in supported:
            raise ValueError(f"Unsupported aim trigger: {profile.trigger}")
        if profile.kp_min < 0 or profile.kp_max < profile.kp_min or profile.kp_growth < 0:
            raise ValueError("Dynamic Kp requires 0 <= minimum <= maximum and a non-negative growth slope")
        if profile.target_class < 0:
            raise ValueError("Target class must be zero or greater")
        if not 0 <= profile.target_y_ratio <= 1:
            raise ValueError("Aim position must be between 0 and 1")
        if profile.fov_radius <= 0:
            raise ValueError("FOV radius must be positive")
    if profiles[0].trigger == profiles[1].trigger:
        raise ValueError("Aim profiles must use different trigger buttons")


def _write_config(path: Path, raw: dict) -> None:
    if path.suffix.lower() != ".txt":
        path.write_text(tomli_w.dumps(raw), encoding="utf-8")
        return
    parser = configparser.ConfigParser(interpolation=None)
    for section, values in raw.items():
        parser[section] = {
            # str(dict) 出来的是 Python repr（单引号）, json.loads 读不回来。
            key: json.dumps(value) if isinstance(value, dict) else str(value)
            for key, value in values.items()
        }
    buffer = StringIO()
    parser.write(buffer)
    content = "\n".join(line.rstrip() for line in buffer.getvalue().splitlines())
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
