"""调校的导出与导入。

这里从头到尾只搬数据, 不 import 任何东西、不执行任何东西。别人发来的调校文件
最坏的情况只是一组难用的数字, 不可能是一段代码——算法实现走 algorithm_library
那条完全独立的路, 那条路上每一步都要用户点头。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Collection

from .config import AimProfileConfig

FORMAT = 1

_NUMBERS = ("kp_min", "kp_max", "kp_growth", "target_y_ratio", "fov_radius")
_REQUIRED = ("algorithm", "params", *_NUMBERS)


class TuningError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TuningPreset:
    algorithm: str
    params: dict[str, float]
    kp_min: float
    kp_max: float
    kp_growth: float
    target_y_ratio: float
    fov_radius: float
    measured_loop_ms: float | None = None


def dump_preset(profile: AimProfileConfig, *, measured_loop_ms: float | None = None) -> str:
    payload: dict[str, object] = {
        "format": FORMAT,
        "algorithm": profile.algorithm,
        # 字典而不是固定字段: 插件自己声明参数, 这里不需要知道有哪些。
        "params": {name: float(value) for name, value in sorted(profile.algorithm_params.items())},
    }
    for key in _NUMBERS:
        payload[key] = float(getattr(profile, key))
    if measured_loop_ms is not None:
        payload["measured_loop_ms"] = round(float(measured_loop_ms), 2)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def load_preset(text: str, *, known_algorithms: Collection[str] | None = None) -> TuningPreset:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise TuningError(f"这不是一份合法的调校文件：{error}") from error
    if not isinstance(payload, dict):
        raise TuningError("调校文件的最外层必须是一个对象。")

    version = payload.get("format")
    if version != FORMAT:
        raise TuningError(f"不认识的调校格式版本 {version!r}，本程序只认 {FORMAT}。")

    missing = [key for key in _REQUIRED if key not in payload]
    if missing:
        raise TuningError("调校文件缺少这些字段：" + "、".join(missing))

    algorithm = payload["algorithm"]
    if not isinstance(algorithm, str) or not algorithm:
        raise TuningError("algorithm 必须是算法标识字符串。")
    if known_algorithms is not None and algorithm not in known_algorithms:
        # 静默回退到默认算法会让人以为在用对方的调校, 实际手感完全是另一回事。
        raise TuningError(f"这份调校需要算法「{algorithm}」，你还没装。先去算法库里导入它。")

    raw_params = payload["params"]
    if not isinstance(raw_params, dict):
        raise TuningError("params 必须是一个对象。")

    try:
        params = {str(name): float(value) for name, value in raw_params.items()}
        numbers = {key: float(payload[key]) for key in _NUMBERS}
    except (TypeError, ValueError) as error:
        raise TuningError(f"调校文件里有不能当数字用的值：{error}") from error

    measured = payload.get("measured_loop_ms")
    if not isinstance(measured, (int, float)):
        measured = None

    return TuningPreset(
        algorithm=algorithm,
        params=params,
        measured_loop_ms=None if measured is None else float(measured),
        **numbers,
    )


def apply_preset(profile: AimProfileConfig, preset: TuningPreset) -> AimProfileConfig:
    """只覆盖手感那几项。

    trigger / target_class / enabled 是本机的习惯和本机用的模型决定的, 别人的
    文件不该动它们。
    """
    return replace(
        profile,
        algorithm=preset.algorithm,
        algorithm_params=dict(preset.params),
        kp_min=preset.kp_min,
        kp_max=preset.kp_max,
        kp_growth=preset.kp_growth,
        target_y_ratio=preset.target_y_ratio,
        fov_radius=preset.fov_radius,
    )


def delay_warning(
    preset_ms: float | None, local_ms: float | None, frame_interval_ms: float = 4.16
) -> str | None:
    """作者和本机的回路延迟差一帧以上就提醒。

    「扣在途」和「速度前馈」的补偿量是按作者那台机器的延迟调的。差一帧就够把
    补偿量从刚好变成过冲或不足。
    """
    if preset_ms is None or local_ms is None:
        return None
    gap = abs(preset_ms - local_ms)
    if gap < frame_interval_ms:
        return None
    return (
        f"作者机器的回路延迟是 {preset_ms:.1f} 毫秒，你这台是 {local_ms:.1f} 毫秒，"
        f"差 {gap:.1f} 毫秒（约 {gap / frame_interval_ms:.1f} 帧）。"
        "扣在途和前馈的补偿量是按作者的延迟调的，套用后可能过冲或不足，"
        "手感不对就先动 loop_delay_frames。"
    )
