"""控制算法的契约。

算法只回答一个问题: 看到这样的误差, 这一帧想走多少像素。死区、平滑、亚像素累加、
限幅、发送全部由 KmboxController 的共用管道负责——那些容易写错又和控制律无关,
不该让每个算法各写一遍。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


ALGORITHM_API_VERSION = 3


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    default: float
    minimum: float
    maximum: float
    label: str
    step: float = 0.01
    presets: tuple[float, ...] = ()
    slider: bool = False
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    error_x: float
    error_y: float
    dt: float
    frame_index: int
    # 最近发出的移动量, 最新的在末尾。写「扣在途」「前馈」「卡尔曼」都要靠它:
    # 算法结合自己的延迟参数, 既能算出尚未生效的指令总量, 也能算出本帧刚生效的
    # 那一条, 从而把目标自身的位移和我们的位移区分开。
    recent_commands: Sequence[tuple[int, int]]
    kp_min: float
    kp_max: float
    kp_growth: float
    # Receiver-frame time and command issue times use the same monotonic clock.
    # Defaults keep existing third-party algorithms and observation fixtures valid.
    timestamp: float = 0.0
    age: float = 0.0
    recent_command_times: Sequence[float] = ()
    deadzone: float = 0.0
    # Target box (x1, y1, x2, y2) and the frame it was found in, in the same
    # pixels as the error. Box height is the only distance cue a 2D detector
    # gives; a box touching the frame edge is cut off. Zeros mean unknown.
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    frame_width: int = 0
    frame_height: int = 0


class Algorithm(Protocol):
    NAME: str
    DISPLAY_NAME: str
    PARAMS: tuple[Param, ...]

    def reset(self) -> None: ...

    def compute(self, observation: Observation) -> tuple[float, float]: ...


def dynamic_kp(error_distance: float, kp_min: float, kp_max: float, kp_growth: float) -> float:
    span = kp_max - kp_min
    growth = 1.0 - math.exp(-kp_growth * max(0.0, error_distance))
    return kp_min + span * growth


def resolve_params(specs: tuple[Param, ...], values: Mapping[str, float]) -> dict[str, float]:
    return {spec.name: float(values.get(spec.name, spec.default)) for spec in specs}
