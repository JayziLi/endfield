from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Callable

from kmbox_universal import KMBoxClient, KMBoxError

from .aim_algorithms import Observation, UnknownAlgorithm, create_algorithm, dynamic_kp as _kp
from .config import AimConfig, AimProfileConfig, KmboxConfig
from .detector import Detection

# 在途指令窗口。算法最多回看自己的延迟参数那么多帧, 64 足够覆盖合理范围。
_COMMAND_HISTORY = 64


class _AimMotionState:
    def __init__(self) -> None:
        self.recent_commands: deque[tuple[int, int]] = deque(maxlen=_COMMAND_HISTORY)
        self.reset()

    def reset(self, *, keep_commands: bool = False) -> None:
        self.smooth_x = 0.0
        self.smooth_y = 0.0
        self.residual_x = 0.0
        self.residual_y = 0.0
        self.frame_index = 0
        self.last_frame_at = 0.0
        if not keep_commands:
            # 在途指令记的是物理事实——已经发给鼠标、还没反映到画面上的那些位移。
            # 调参数不该把它抹掉: 抹掉的话「扣在途」会以为什么都没发, 当场多走一截。
            self.recent_commands.clear()


class KmboxController:
    def __init__(
        self,
        device: KmboxConfig,
        aim: AimConfig,
        runtime_file: Path | None = None,
        *,
        profiles: tuple[AimProfileConfig, AimProfileConfig] | None = None,
        reload_algorithms: Callable[[], list[str]] | None = None,
    ):
        self.device_config = device
        self.aim_config = aim
        self.runtime_file = runtime_file
        # 重新加载算法库用的钩子, 由管线提供。放成回调而不是让这个模块自己去读
        # algorithms/ 目录, 是为了让控制路径不必知道 import 用户代码那套东西。
        self._reload_algorithms = reload_algorithms
        self._client = None
        self._profiles = list(
            profiles
            or (
                AimProfileConfig(
                    target_class=aim.target_class,
                    target_y_ratio=aim.target_y_ratio,
                    fov_radius=aim.fov_radius,
                ),
                AimProfileConfig(
                    enabled=False,
                    trigger="left",
                    target_class=aim.target_class,
                    target_y_ratio=aim.target_y_ratio,
                    fov_radius=aim.fov_radius,
                ),
            )
        )
        self._motion_states = [_AimMotionState(), _AimMotionState()]
        self._trigger_down = [False, False]
        self._press_order = [0, 0]
        self._press_counter = 0
        self._active_profile_index: int | None = None
        self._runtime_mtime_ns: int | None = None
        self._next_runtime_check = 0.0
        self.last_send_ms = 0.0
        self.trigger_errors = 0
        self.algorithm_warnings: list[str] = []
        # 运行中切算法时产生的消息, 由管线每帧取走打印。放在这里而不是直接 print,
        # 是为了让这个模块保持不做 I/O。
        self.algorithm_notices: list[str] = []
        self._algorithms = [self._build_algorithm(profile) for profile in self._profiles]

    def _try_create(self, profile: AimProfileConfig):
        try:
            return create_algorithm(profile.algorithm, profile.algorithm_params)
        except UnknownAlgorithm:
            return None

    def _build_algorithm(self, profile: AimProfileConfig):
        algorithm = self._try_create(profile)
        if algorithm is not None:
            return algorithm
        self.algorithm_warnings.append(f"算法 {profile.algorithm} 找不到，已回退到 p")
        return create_algorithm("p", {})

    def _switch_algorithm(self, index: int, profile: AimProfileConfig) -> None:
        """运行中换算法。和启动时的 _build_algorithm 分开写是因为要说的话不一样:
        启动时回退只需要记一条警告, 运行中还得当场告诉用户这一下到底换成了什么。
        """
        algorithm = self._try_create(profile)
        reloaded = False
        if algorithm is None and self._reload_algorithms is not None:
            # 名字对不上, 通常意味着这是管线启动之后才导入的算法。重新加载一次
            # 算法库再试——这一下要读盘并 import 用户的 .py, 有几毫秒顿挫, 所以
            # 只在查不到时才做, 不是每次切换都付这个钱。
            for warning in self._reload_algorithms():
                self.algorithm_notices.append(f"!! {warning}")
            algorithm = self._try_create(profile)
            reloaded = algorithm is not None
        if algorithm is None:
            self._algorithms[index] = create_algorithm("p", {})
            self.algorithm_notices.append(
                f"!! 控制方案 {index + 1} 要的算法 {profile.algorithm} 找不到，已回退到 p"
            )
            return
        self._algorithms[index] = algorithm
        self.algorithm_notices.append(
            f"控制方案 {index + 1} 的算法已切换为 {profile.algorithm}"
            + ("（刚导入的，已加载）" if reloaded else "")
        )

    def algorithm_name(self, index: int) -> str:
        return self._algorithms[index].NAME

    def connect(self) -> None:
        if not self.device_config.enabled:
            return
        last_error: Exception | None = None
        for attempt in range(self.device_config.connect_attempts):
            try:
                self._client = KMBoxClient(
                    self.device_config.host,
                    self.device_config.port,
                    self.device_config.uuid,
                    timeout=self.device_config.timeout_seconds,
                )
                self._client.monitor_start(self.device_config.monitor_port)
                return
            except (KMBoxError, OSError) as exc:
                last_error = exc
                self.close()
                if attempt + 1 < self.device_config.connect_attempts:
                    time.sleep(0.01)
        raise RuntimeError(
            f"KMBox did not respond after {self.device_config.connect_attempts} attempts: {last_error}"
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def trigger_active(self) -> bool:
        self.refresh_runtime_settings()
        return self._resolve_active_profile() is not None

    @property
    def active_profile_number(self) -> int | None:
        return self._active_profile_index + 1 if self._active_profile_index is not None else None

    @property
    def active_profile(self) -> AimProfileConfig:
        """当前生效的那个方案。延迟日志要记的是它, 不是方案 1。"""
        return self._target_profile()

    @property
    def target_y_ratio(self) -> float:
        return self._target_profile().target_y_ratio

    @property
    def target_class(self) -> int:
        return self._target_profile().target_class

    @property
    def fov_radius(self) -> float:
        return self._target_profile().fov_radius

    def _target_profile(self) -> AimProfileConfig:
        index = self._active_profile_index if self._active_profile_index is not None else 0
        return self._profiles[index]

    def refresh_runtime_settings(self, *, force: bool = False) -> None:
        if self.runtime_file is None:
            return
        now = time.perf_counter()
        if not force and now < self._next_runtime_check:
            return
        self._next_runtime_check = now + 0.05
        try:
            modified = self.runtime_file.stat().st_mtime_ns
            if modified == self._runtime_mtime_ns and not force:
                return
            values = json.loads(self.runtime_file.read_text(encoding="utf-8"))
            profiles_raw = values.get("profiles")
            if profiles_raw is None:
                profiles = list(self._profiles)
                profiles[0] = replace(
                    profiles[0],
                    trigger=str(values.get("trigger", profiles[0].trigger)),
                    kp_min=float(values.get("kp_min", profiles[0].kp_min)),
                    kp_max=float(values.get("kp_max", profiles[0].kp_max)),
                    kp_growth=float(values.get("kp_growth", profiles[0].kp_growth)),
                    target_class=int(values.get("target_class", profiles[0].target_class)),
                    target_y_ratio=float(values.get("target_y_ratio", profiles[0].target_y_ratio)),
                    fov_radius=float(values.get("fov_radius", profiles[0].fov_radius)),
                )
            else:
                if not isinstance(profiles_raw, list) or len(profiles_raw) != 2:
                    return
                profiles = [
                    AimProfileConfig(
                        enabled=bool(raw["enabled"]),
                        trigger=str(raw["trigger"]),
                        kp_min=float(raw["kp_min"]),
                        kp_max=float(raw["kp_max"]),
                        kp_growth=float(raw["kp_growth"]),
                        target_class=int(raw.get("target_class", values.get("target_class", fallback.target_class))),
                        target_y_ratio=float(
                            raw.get("target_y_ratio", values.get("target_y_ratio", fallback.target_y_ratio))
                        ),
                        fov_radius=float(raw.get("fov_radius", values.get("fov_radius", fallback.fov_radius))),
                        algorithm=str(raw.get("algorithm", fallback.algorithm)),
                        algorithm_params=_runtime_params(raw.get("algorithm_params"), fallback),
                    )
                    for raw, fallback in zip(profiles_raw, self._profiles)
                ]
            if not _valid_profiles(profiles):
                return
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        for index, (old, new) in enumerate(zip(self._profiles, profiles)):
            if old == new:
                continue
            # 只有算法名或参数真的变了才重建。无条件重建的话, pd 和 feedforward
            # 存的上一帧误差会每 50 毫秒被清一次——微分项和前馈项永久失效, 而表现
            # 只是「手感怪但说不出哪儿怪」, 几乎查不出来。
            if new.algorithm != old.algorithm or new.algorithm_params != old.algorithm_params:
                self._switch_algorithm(index, new)
            # 在途指令留着: 那些位移物理上已经发给鼠标了。
            self._reset_profile(index, keep_commands=True)
            self._trigger_down[index] = False
            self._press_order[index] = 0
        self._profiles = profiles
        self._runtime_mtime_ns = modified

    def _reset_profile(self, index: int, *, keep_commands: bool = False) -> None:
        self._motion_states[index].reset(keep_commands=keep_commands)
        self._algorithms[index].reset()

    def reset(self) -> None:
        for index in range(len(self._motion_states)):
            self._reset_profile(index)

    def forget_target(self) -> None:
        """换目标了: 算法内部状态作废, 但在途指令留着。

        那些位移物理上已经发给鼠标、只是还没反映到画面上。抹掉的话「扣在途」会以为
        什么都没发, 当场多走一截 —— 而换目标恰恰是你在看手感的时刻, 这个过冲最误导人。
        """
        for index in range(len(self._motion_states)):
            self._reset_profile(index, keep_commands=True)

    def move_toward(self, target: Detection, frame_width: int, frame_height: int) -> tuple[int, int]:
        self.last_send_ms = 0.0
        if self._client is None:
            return (0, 0)
        profile_index = self._active_profile_index if self._active_profile_index is not None else 0
        profile = self._profiles[profile_index]
        state = self._motion_states[profile_index]
        error_x = target.center_x - frame_width * 0.5
        error_y = target.aim_y(profile.target_y_ratio) - frame_height * 0.5
        error_distance = math.hypot(error_x, error_y)
        if error_distance <= self.aim_config.deadzone:
            self._reset_profile(profile_index)
            return (0, 0)
        now = time.perf_counter()
        observation = Observation(
            error_x=error_x,
            error_y=error_y,
            dt=now - state.last_frame_at if state.last_frame_at else 0.0,
            frame_index=state.frame_index,
            recent_commands=state.recent_commands,
            kp_min=profile.kp_min,
            kp_max=profile.kp_max,
            kp_growth=profile.kp_growth,
        )
        state.last_frame_at = now
        state.frame_index += 1
        raw_x, raw_y = self._algorithms[profile_index].compute(observation)
        if raw_x * state.smooth_x < 0:
            state.smooth_x = 0.0
        if raw_y * state.smooth_y < 0:
            state.smooth_y = 0.0
        alpha = self.aim_config.smoothing
        state.smooth_x += alpha * (raw_x - state.smooth_x)
        state.smooth_y += alpha * (raw_y - state.smooth_y)
        dx, state.residual_x = _axis_step(state.smooth_x, state.residual_x, self.aim_config.max_step)
        dy, state.residual_y = _axis_step(state.smooth_y, state.residual_y, self.aim_config.max_step)
        if dx == 0 and dy == 0:
            state.recent_commands.append((0, 0))
            return (0, 0)
        move = self._client.enc_move if self.device_config.encrypted else self._client.move
        started = time.perf_counter()
        try:
            move(dx, dy)
        except (KMBoxError, OSError):
            # 指令没发出去就记 (0, 0): 记成发了会让「扣在途」减掉一段根本没发生的位移。
            state.recent_commands.append((0, 0))
            return (0, 0)
        finally:
            self.last_send_ms = (time.perf_counter() - started) * 1000.0
        state.recent_commands.append((dx, dy))
        return (dx, dy)

    def _resolve_active_profile(self) -> int | None:
        if self._client is None:
            self._set_active_profile(None)
            return None
        try:
            down = [
                profile.enabled and bool(getattr(self._client, f"isdown_{profile.trigger}")())
                for profile in self._profiles
            ]
        except (KMBoxError, OSError):
            # 查不到按键状态时按「没按」处理: 宁可少瞄一帧, 也不能在不知道用户
            # 是否按住的情况下操控鼠标。而且异常一旦穿透就会打挂整个主循环——
            # KMBox 超时只有 20ms, 进程被系统丢进效率模式后很容易撞上。
            self.trigger_errors += 1
            down = [False for _ in self._profiles]
        rising = [index for index, value in enumerate(down) if value and not self._trigger_down[index]]
        if rising:
            self._press_counter += 1
            for index in rising:
                self._press_order[index] = self._press_counter
        self._trigger_down = down
        candidates = [index for index, value in enumerate(down) if value]
        selected = max(candidates, key=lambda index: (self._press_order[index], -index)) if candidates else None
        self._set_active_profile(selected)
        return selected

    def _set_active_profile(self, selected: int | None) -> None:
        if selected == self._active_profile_index:
            return
        if self._active_profile_index is not None:
            self._reset_profile(self._active_profile_index)
        if selected is not None:
            self._reset_profile(selected)
        self._active_profile_index = selected


def _axis_step(value: float, residual: float, maximum: int) -> tuple[int, float]:
    total = residual + value
    step = round(total)
    return max(-maximum, min(maximum, step)), total - step


def _runtime_params(raw: object, fallback: AimProfileConfig) -> dict[str, float]:
    if not isinstance(raw, dict):
        return dict(fallback.algorithm_params)
    # 值不是数字的话这里会抛, 由外层的 except 兜住 —— 整次刷新作废, 保留现有设置。
    # 宁可不生效, 也不要拿半份配置去重建算法。
    return {str(name): float(value) for name, value in raw.items()}


def _valid_profiles(profiles: list[AimProfileConfig]) -> bool:
    if len(profiles) != 2 or profiles[0].trigger == profiles[1].trigger:
        return False
    supported = {"left", "right", "side1", "side2"}
    return all(
        profile.trigger in supported
        and profile.kp_min >= 0
        and profile.kp_max >= profile.kp_min
        and profile.kp_growth >= 0
        and profile.target_class >= 0
        and 0 <= profile.target_y_ratio <= 1
        and profile.fov_radius > 0
        for profile in profiles
    )


def dynamic_kp(error_distance: float, config: AimProfileConfig) -> float:
    return _kp(error_distance, config.kp_min, config.kp_max, config.kp_growth)
