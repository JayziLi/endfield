"""预设: 打一个游戏要用的整套配置快照。

settings.txt 是「现在正在用的」, 预设是存起来的快照, 载入就是把快照填回界面。
和 tuning_share 一样只搬数据, 不 import、不执行任何东西。

存的是界面上能改的全部: 模型、画面输入、KMBox、两个控制方案。超时、缓冲区这些
界面上没有的高级项只留在 settings.txt, 不跟着预设来回换。

文件里有 KMBox 的 UUID 和 OBS 密码, 所以它是本机的私人文件, 别当调校发给别人。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Collection

from .config import (
    AimProfileConfig,
    AppConfig,
    InputConfig,
    KmboxConfig,
    ModelConfig,
    ObsConfig,
    UdpConfig,
    _validate_aim_profiles,
)

FORMAT = 1
DIRECTORY_NAME = "presets"

_SUFFIX = ".json"
_MAX_NAME = 40
_ILLEGAL = frozenset('\\/:*?"<>|')
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
)
_PROVIDERS = frozenset({"auto", "tensorrt", "cuda", "cpu"})
_OUTPUT_FORMATS = frozenset({"yolov5", "yolov8", "end2end"})
_INPUT_MODES = frozenset({"udp_video", "udp_jpeg", "obs_websocket"})
_PROFILE_NUMBERS = ("kp_min", "kp_max", "kp_growth", "target_y_ratio", "fov_radius")
# 连接类设置里界面上能改的那几项。
_WIRING_FIELDS = {
    "udp": ("host", "port", "width", "height"),
    "obs": ("host", "port", "password", "source_name"),
    "kmbox": ("enabled", "host", "port", "uuid"),
}


class PresetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Preset:
    model: ModelConfig
    input: InputConfig
    # 从文件读回来时, 这三个里界面上没有的字段是默认值, 没有意义——只用 _WIRING_FIELDS 列的那几项。
    udp: UdpConfig
    obs: ObsConfig
    kmbox: KmboxConfig
    aim_profile_1: AimProfileConfig
    aim_profile_2: AimProfileConfig

    @property
    def aim_profiles(self) -> tuple[AimProfileConfig, AimProfileConfig]:
        return (self.aim_profile_1, self.aim_profile_2)


def preset_from_config(config: AppConfig) -> Preset:
    return Preset(
        model=config.model,
        input=config.input,
        udp=config.udp,
        obs=config.obs,
        kmbox=config.kmbox,
        aim_profile_1=config.aim_profile_1,
        aim_profile_2=config.aim_profile_2,
    )


def dump_preset(preset: Preset, base_directory: Path) -> str:
    model = preset.model
    payload = {
        "format": FORMAT,
        "model": {
            "path": _stored_path(model.path, base_directory),
            "provider": model.provider,
            "cuda_graph": bool(model.cuda_graph),
            "gpu_preprocess": bool(model.gpu_preprocess),
            "output_format": model.output_format,
            "confidence": float(model.confidence),
            "iou": float(model.iou),
        },
        "input": {"mode": preset.input.mode},
        **_wiring(preset),
        "aim_profiles": [
            {
                "enabled": bool(profile.enabled),
                "trigger": profile.trigger,
                "target_class": int(profile.target_class),
                **{key: float(getattr(profile, key)) for key in _PROFILE_NUMBERS},
                "algorithm": profile.algorithm,
                "algorithm_params": {
                    name: float(value) for name, value in sorted(profile.algorithm_params.items())
                },
            }
            for profile in preset.aim_profiles
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    # 和 save_config 一样: 写出去的东西必须读得回来, 读不回来就别写。
    parse_preset(text, base_directory)
    return text


def parse_preset(
    text: str, base_directory: Path, *, known_algorithms: Collection[str] | None = None
) -> Preset:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PresetError(f"这不是一份合法的预设文件：{error}") from error
    if not isinstance(payload, dict):
        raise PresetError("预设文件的最外层必须是一个对象。")
    if payload.get("format") != FORMAT:
        raise PresetError(f"不认识的预设格式版本 {payload.get('format')!r}，本程序只认 {FORMAT}。")

    raw_model = _section(payload, "model")
    path = raw_model.get("path")
    if not isinstance(path, str) or not path.strip():
        raise PresetError("model.path 必须是模型文件路径。")
    model_path = Path(path)
    if not model_path.is_absolute():
        model_path = base_directory / model_path
    model = ModelConfig(
        path=model_path.resolve(),
        provider=_choice(raw_model, "provider", _PROVIDERS),
        cuda_graph=_flag(raw_model, "cuda_graph"),
        gpu_preprocess=_flag(raw_model, "gpu_preprocess"),
        output_format=_choice(raw_model, "output_format", _OUTPUT_FORMATS),
        output_layout="auto",
        confidence=_fraction(raw_model, "confidence"),
        iou=_fraction(raw_model, "iou"),
    )

    input_mode = _section(payload, "input").get("mode")
    if input_mode not in _INPUT_MODES:
        raise PresetError(f"input.mode 不认识：{input_mode!r}。")
    raw_udp = _section(payload, "udp")
    udp = UdpConfig(
        host=_text(raw_udp, "host", "udp"),
        port=_port(raw_udp, "port", "udp"),
        width=_positive(raw_udp, "width", "udp"),
        height=_positive(raw_udp, "height", "udp"),
    )
    raw_obs = _section(payload, "obs")
    obs = ObsConfig(
        host=_text(raw_obs, "host", "obs"),
        port=_port(raw_obs, "port", "obs"),
        password=_text(raw_obs, "password", "obs"),
        source_name=_text(raw_obs, "source_name", "obs"),
    )
    raw_kmbox = _section(payload, "kmbox")
    kmbox = KmboxConfig(
        enabled=_flag(raw_kmbox, "enabled", "kmbox"),
        host=_text(raw_kmbox, "host", "kmbox"),
        port=_port(raw_kmbox, "port", "kmbox"),
        uuid=_text(raw_kmbox, "uuid", "kmbox"),
    )

    raw_profiles = payload.get("aim_profiles")
    if not isinstance(raw_profiles, list) or len(raw_profiles) != 2:
        raise PresetError("aim_profiles 必须正好是两个控制方案。")
    profiles = tuple(
        _profile(raw, f"控制方案 {index + 1}", known_algorithms) for index, raw in enumerate(raw_profiles)
    )
    try:
        _validate_aim_profiles(profiles)
    except ValueError as error:
        raise PresetError(f"预设里的控制方案设置不对：{error}") from error
    return Preset(
        model=model,
        input=InputConfig(mode=input_mode),
        udp=udp,
        obs=obs,
        kmbox=kmbox,
        aim_profile_1=profiles[0],
        aim_profile_2=profiles[1],
    )


def same_settings(first: Preset, second: Preset) -> bool:
    """差一点浮点尾巴算一样。

    框内位置界面上是百分比, 0.029 进界面再读回来是 0.029000000000000005。
    按严格相等比, 刚载入的预设当场就会被标成「有改动」。
    """
    return _close(_comparable(first), _comparable(second))


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise PresetError("预设名不能是空的。")
    if len(cleaned) > _MAX_NAME:
        raise PresetError(f"预设名最多 {_MAX_NAME} 个字。")
    bad = sorted({repr(char) if ord(char) < 32 else char for char in cleaned if char in _ILLEGAL or ord(char) < 32})
    if bad:
        raise PresetError("预设名里不能有这些字符：" + " ".join(bad))
    if cleaned.endswith("."):
        raise PresetError("预设名不能以句点结尾，Windows 会把它吞掉。")
    if cleaned.split(".")[0].strip().upper() in _RESERVED:
        raise PresetError(f"「{cleaned}」是 Windows 的保留名，换一个。")
    return cleaned


def list_presets(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted((path.stem for path in directory.glob(f"*{_SUFFIX}") if path.is_file()), key=str.casefold)


def find_preset(directory: Path, name: str) -> str | None:
    """同名不分大小写——Windows 的文件系统本来就不分。"""
    wanted = name.strip().casefold()
    return next((existing for existing in list_presets(directory) if existing.casefold() == wanted), None)


def read_preset(
    directory: Path,
    name: str,
    *,
    base_directory: Path,
    known_algorithms: Collection[str] | None = None,
    require_model: bool = True,
) -> Preset:
    try:
        text = (directory / f"{name}{_SUFFIX}").read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PresetError(f"预设「{name}」不存在。") from error
    except (OSError, UnicodeDecodeError) as error:
        raise PresetError(f"读不了预设「{name}」：{error}") from error
    preset = parse_preset(text, base_directory, known_algorithms=known_algorithms)
    if require_model and not preset.model.path.is_file():
        raise PresetError(f"预设「{name}」用的模型文件不在了：{preset.model.path}")
    return preset


def write_preset(directory: Path, name: str, preset: Preset, *, base_directory: Path) -> str:
    """返回实际存下的名字(去掉了首尾空格)。"""
    name = validate_name(name)
    if not preset.model.path.is_file():
        # 存得进去却载入不了的预设, 等于埋一个雷。
        raise PresetError(f"模型文件不存在，没法存成预设：{preset.model.path}")
    text = dump_preset(preset, base_directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{name}.tmp"
    try:
        temporary.write_text(text, encoding="utf-8")
        # 只改了大小写时 Windows 的 replace 会连名字一起换成新的大小写, 不留两份。
        temporary.replace(directory / f"{name}{_SUFFIX}")
    finally:
        temporary.unlink(missing_ok=True)
    return name


def delete_preset(directory: Path, name: str) -> None:
    (directory / f"{name}{_SUFFIX}").unlink(missing_ok=True)


def _stored_path(path: Path, base_directory: Path) -> str:
    resolved = path.resolve()
    try:
        resolved = resolved.relative_to(base_directory.resolve())
    except ValueError:
        pass
    return str(resolved).replace("\\", "/")


def _wiring(preset: Preset) -> dict[str, dict[str, object]]:
    return {
        section: {key: getattr(getattr(preset, section), key) for key in keys}
        for section, keys in _WIRING_FIELDS.items()
    }


def _section(payload: dict, key: str) -> dict:
    section = payload.get(key)
    if not isinstance(section, dict):
        raise PresetError(f"预设文件缺少 {key}。")
    return section


def _profile(raw: object, where: str, known_algorithms: Collection[str] | None) -> AimProfileConfig:
    if not isinstance(raw, dict):
        raise PresetError(f"{where} 必须是一个对象。")
    trigger = raw.get("trigger")
    if not isinstance(trigger, str):
        raise PresetError(f"{where} 缺少触发键。")
    target_class = raw.get("target_class")
    if isinstance(target_class, bool) or not isinstance(target_class, int) or target_class < 0:
        raise PresetError(f"{where} 的目标标签必须是 0 或更大的整数。")
    algorithm = raw.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        raise PresetError(f"{where} 缺少算法标识。")
    if known_algorithms is not None and algorithm not in known_algorithms:
        # 静默换成默认算法会让人以为在用这份预设, 实际手感完全是另一回事。
        raise PresetError(f"{where} 需要算法「{algorithm}」，你还没装。先去算法库里导入它。")
    params = raw.get("algorithm_params")
    if not isinstance(params, dict):
        raise PresetError(f"{where} 的算法参数必须是一个对象。")
    return AimProfileConfig(
        enabled=_flag(raw, "enabled", where),
        trigger=trigger,
        target_class=target_class,
        algorithm=algorithm,
        algorithm_params={str(name): _number(value, f"{where} 的参数 {name}") for name, value in params.items()},
        **{key: _number(raw.get(key), f"{where} 的 {key}") for key in _PROFILE_NUMBERS},
    )


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PresetError(f"{where} 必须是数字。")
    return float(value)


def _text(section: dict, key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str):
        raise PresetError(f"{where}.{key} 必须是文字。")
    return value


def _positive(section: dict, key: str, where: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PresetError(f"{where}.{key} 必须是正整数。")
    return value


def _port(section: dict, key: str, where: str) -> int:
    value = _positive(section, key, where)
    if value > 65535:
        raise PresetError(f"{where}.{key} 必须在 1 到 65535 之间。")
    return value


def _fraction(section: dict, key: str) -> float:
    value = _number(section.get(key), f"model.{key}")
    if not 0.0 <= value <= 1.0:
        raise PresetError(f"model.{key} 必须在 0 到 1 之间。")
    return value


def _flag(section: dict, key: str, where: str = "model") -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise PresetError(f"{where} 的 {key} 必须是 true 或 false。")
    return value


def _choice(section: dict, key: str, allowed: frozenset[str]) -> str:
    value = section.get(key)
    if value not in allowed:
        raise PresetError(f"model.{key} 不认识：{value!r}。")
    return value


def _comparable(preset: Preset) -> dict:
    model = preset.model
    return {
        "model": {
            # 同一个文件换个写法(相对/绝对、大小写)还是同一个模型。
            "path": os.path.normcase(str(model.path.resolve())),
            "provider": model.provider,
            "cuda_graph": model.cuda_graph,
            "gpu_preprocess": model.gpu_preprocess,
            "output_format": model.output_format,
            "confidence": model.confidence,
            "iou": model.iou,
        },
        "input": preset.input.mode,
        # 只比界面上能改的: 其余字段预设里本来就不存, 比了标记会永远亮着。
        **_wiring(preset),
        "profiles": [
            {
                "enabled": profile.enabled,
                "trigger": profile.trigger,
                "target_class": profile.target_class,
                "algorithm": profile.algorithm,
                "algorithm_params": dict(profile.algorithm_params),
                **{key: getattr(profile, key) for key in _PROFILE_NUMBERS},
            }
            for profile in preset.aim_profiles
        ],
    }


def _close(first: object, second: object) -> bool:
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(_close(first[key], second[key]) for key in first)
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(_close(a, b) for a, b in zip(first, second))
    if isinstance(first, bool) or isinstance(second, bool):
        return first == second
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return math.isclose(first, second, rel_tol=1e-9, abs_tol=1e-9)
    return first == second
