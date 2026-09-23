"""随程序分发的内置算法。

它们和用户自己写的算法走完全相同的契约, 不开任何后门。如果某个内置算法需要
特殊通道, 说明契约设计得不对。
"""

from __future__ import annotations

import math
from typing import Mapping

from .contract import Observation, Param, dynamic_kp


class Proportional:
    """现状: 误差乘以随距离变化的比例增益。"""

    NAME = "p"
    DISPLAY_NAME = "比例控制（现状）"
    PARAMS: tuple[Param, ...] = ()

    def __init__(self, params: Mapping[str, float]) -> None:
        del params

    def reset(self) -> None:
        return None

    def compute(self, observation: Observation) -> tuple[float, float]:
        distance = math.hypot(observation.error_x, observation.error_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return observation.error_x * kp, observation.error_y * kp


class ProportionalDerivative:
    """比例加微分。治超调, 对匀速移动目标的稳态落后没有作用——那时误差恒定,
    微分项为零。"""

    NAME = "pd"
    DISPLAY_NAME = "比例 + 微分"
    PARAMS: tuple[Param, ...] = (Param("kd", 0.3, 0.0, 2.0, "微分系数"),)

    def __init__(self, params: Mapping[str, float]) -> None:
        self._kd = params["kd"]
        self.reset()

    def reset(self) -> None:
        self._previous: tuple[float, float] | None = None

    def compute(self, observation: Observation) -> tuple[float, float]:
        distance = math.hypot(observation.error_x, observation.error_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        raw_x = observation.error_x * kp
        raw_y = observation.error_y * kp
        if self._previous is not None:
            raw_x += self._kd * (observation.error_x - self._previous[0])
            raw_y += self._kd * (observation.error_y - self._previous[1])
        self._previous = (observation.error_x, observation.error_y)
        return raw_x, raw_y


def _landed_command(recent_commands, lag: int) -> tuple[float, float]:
    """本帧刚在画面里生效的那一条指令。

    recent_commands 最新的在末尾, 对应上一帧。第 k-lag 帧发出的指令在第 k 帧生效,
    所以往回数第 lag 个就是它。

    lag <= 0 要单独挡掉, 不能靠下标: Python 的 -0 等于 0, commands[-0] 取的是
    **最老**的那一条, 而不是「没有」。这一支原来被构造函数里的 max(1, ...) 盖着,
    代价是回路延迟根本填不进 0 —— 而 0 是个有意义的设置 (不要前馈)。
    没有延迟就没有「刚落地」这回事, 返回 0。
    """
    if lag <= 0:
        return 0.0, 0.0
    commands = list(recent_commands)
    if len(commands) < lag:
        return 0.0, 0.0
    x, y = commands[-lag]
    return float(x), float(y)


# 目标一帧里最多可能移动多少像素。实测横移 482px/s, 241fps 下每帧 2px, 这里留到
# 12px(≈2900px/s)已经是六倍余量。
#
# 超过这个量的不是运动, 是**不连续**: 目标换人(上报的切换, 以及位置歧义导致的静默
# 换身份)、漏检回来的第一帧、检测框突然抖一下。把它当成速度会让前馈量乘上延迟帧数
# —— 跳 60px 就是 120px 的前馈量, 单帧走 17px 而正确值约 6px, 一次切换一记暴力甩枪。
#
# 顺带还治了另一个毛病: 拉枪的头 lag 帧里指令历史还不够长, _landed_command 返回 0,
# 于是误差变化被整个当成目标位移, 前馈量大到能让瞄准点翻号。那几帧现在也走这条路。
_MAX_PLAUSIBLE_DELTA = 12.0


def _smoothed_target_velocity(
    previous: tuple[float, float] | None,
    current: tuple[float, float],
    landed: tuple[float, float],
    estimate: tuple[float, float],
    alpha: float,
) -> tuple[float, float]:
    """Update target velocity without treating a detection jump as motion."""
    if previous is None:
        return estimate
    delta_x = (current[0] - previous[0]) + landed[0]
    delta_y = (current[1] - previous[1]) + landed[1]
    if math.hypot(delta_x, delta_y) > _MAX_PLAUSIBLE_DELTA:
        return 0.0, 0.0
    return (
        estimate[0] + alpha * (delta_x - estimate[0]),
        estimate[1] + alpha * (delta_y - estimate[1]),
    )


class Feedforward:
    """按目标速度提前量瞄准。

    目标位移要绕完整条回路才出现在画面里, 所以看到的永远是它几帧前的位置。
    把「目标速度 x 延迟帧数」加到误差上, 瞄的就是它现在大概在的地方。
    """

    NAME = "feedforward"
    DISPLAY_NAME = "速度前馈"
    PARAMS: tuple[Param, ...] = (
        # 三个下限都是 0, 而且 0 都有意义: 回路延迟 0 = 不要前馈 (整个退化成比例
        # 控制, 但在子类里就是「只要风 / 只要弧线」这种别处给不了的配法), 前馈强度
        # 0 = 同上, 速度平滑 0 = 速度估计冻在 0。界面上的滑条按这里画, 写个非零
        # 下限用户就拖不到底, 手打也会被夹回去。
        Param("loop_delay_frames", 8.0, 0.0, 30.0, "回路延迟（帧）"),
        Param("gain", 1.0, 0.0, 2.0, "前馈强度"),
        Param("velocity_smoothing", 0.25, 0.0, 1.0, "速度平滑"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        self._lag = max(0, int(round(params["loop_delay_frames"])))
        self._gain = params["gain"]
        self._alpha = params["velocity_smoothing"]
        self.reset()

    def reset(self) -> None:
        self._previous: tuple[float, float] | None = None
        self._velocity_x = 0.0
        self._velocity_y = 0.0

    def _track(self, observation: Observation) -> None:
        landed_x, landed_y = _landed_command(observation.recent_commands, self._lag)
        self._velocity_x, self._velocity_y = _smoothed_target_velocity(
            self._previous,
            (observation.error_x, observation.error_y),
            (landed_x, landed_y),
            (self._velocity_x, self._velocity_y),
            self._alpha,
        )
        self._previous = (observation.error_x, observation.error_y)

    def _lead(self) -> tuple[float, float]:
        return (
            self._gain * self._velocity_x * self._lag,
            self._gain * self._velocity_y * self._lag,
        )

    def compute(self, observation: Observation) -> tuple[float, float]:
        self._track(observation)
        lead_x, lead_y = self._lead()
        aim_x = observation.error_x + lead_x
        aim_y = observation.error_y + lead_y
        distance = math.hypot(aim_x, aim_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return aim_x * kp, aim_y * kp


def _in_flight(recent_commands, lag: int) -> tuple[float, float]:
    """已经发出但还没在画面里生效的位移总量。

    第 m 帧发出的指令在第 m+lag 帧才可见, 所以最近 lag-1 帧发出的都还在途中。
    注意窗口和 _landed_command 不同: 那个取单独一条(刚落地的), 这个取一整段。
    """
    if lag <= 1:
        return 0.0, 0.0
    window = list(recent_commands)[-(lag - 1):]
    return float(sum(x for x, _ in window)), float(sum(y for _, y in window))


class InFlight:
    """扣掉在途指令。

    控制器看到的误差还没反映出最近几帧已经发出的移动, 不扣掉就会为同一段位移
    重复下单。注意它假设目标不动——目标在动时在途位移补不上那段距离, 跟踪反而
    比纯比例更差, 所以实用时应与前馈配对（见 inflight_ff）。
    """

    NAME = "inflight"
    DISPLAY_NAME = "扣除在途指令"
    PARAMS: tuple[Param, ...] = (
        Param("loop_delay_frames", 8.0, 0.0, 30.0, "回路延迟（帧）"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        # 0 = 没有在途指令要扣, 整个退化成比例控制。_in_flight 的 lag <= 1 那一支
        # 已经覆盖了。
        self._lag = max(0, int(round(params["loop_delay_frames"])))

    def reset(self) -> None:
        return None

    def compute(self, observation: Observation) -> tuple[float, float]:
        flight_x, flight_y = _in_flight(observation.recent_commands, self._lag)
        work_x = observation.error_x - flight_x
        work_y = observation.error_y - flight_y
        distance = math.hypot(work_x, work_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return work_x * kp, work_y * kp


class InFlightFeedforward(Feedforward):
    """两者合用: 在途量补我们自己的位移, 前馈补目标的位移。

    它们补的是同一段死区时间的不同部分, 必须成对使用。
    """

    NAME = "inflight_ff"
    DISPLAY_NAME = "扣在途 + 速度前馈"

    def compute(self, observation: Observation) -> tuple[float, float]:
        self._track(observation)
        lead_x, lead_y = self._lead()
        flight_x, flight_y = _in_flight(observation.recent_commands, self._lag)
        aim_x = observation.error_x - flight_x + lead_x
        aim_y = observation.error_y - flight_y + lead_y
        distance = math.hypot(aim_x, aim_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return aim_x * kp, aim_y * kp


BUILTIN_ALGORITHMS: tuple[type, ...] = (
    Proportional,
    ProportionalDerivative,
    Feedforward,
    InFlight,
    InFlightFeedforward,
)
