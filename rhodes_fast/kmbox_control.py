from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from pathlib import Path

from kmbox_universal import KMBoxClient, KMBoxError

from .config import AimConfig, AimProfileConfig, KmboxConfig
from .detector import Detection


class _AimMotionState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.smooth_x = 0.0
        self.smooth_y = 0.0
        self.residual_x = 0.0
        self.residual_y = 0.0


class KmboxController:
    def __init__(
        self,
        device: KmboxConfig,
        aim: AimConfig,
        runtime_file: Path | None = None,
        *,
        profiles: tuple[AimProfileConfig, AimProfileConfig] | None = None,
    ):
        self.device_config = device
        self.aim_config = aim
        self.runtime_file = runtime_file
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
                    )
                    for raw, fallback in zip(profiles_raw, self._profiles)
                ]
            if not _valid_profiles(profiles):
                return
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        for index, (old, new) in enumerate(zip(self._profiles, profiles)):
            if old != new:
                self._motion_states[index].reset()
                self._trigger_down[index] = False
                self._press_order[index] = 0
        self._profiles = profiles
        self._runtime_mtime_ns = modified

    def reset(self) -> None:
        for state in self._motion_states:
            state.reset()

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
            state.reset()
            return (0, 0)
        kp = _dynamic_kp(error_distance, profile.kp_min, profile.kp_max, profile.kp_growth)
        raw_x = error_x * kp
        raw_y = error_y * kp
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
            return (0, 0)
        move = self._client.enc_move if self.device_config.encrypted else self._client.move
        started = time.perf_counter()
        try:
            move(dx, dy)
        except (KMBoxError, OSError):
            return (0, 0)
        finally:
            self.last_send_ms = (time.perf_counter() - started) * 1000.0
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
            self._motion_states[self._active_profile_index].reset()
        if selected is not None:
            self._motion_states[selected].reset()
        self._active_profile_index = selected


def _axis_step(value: float, residual: float, maximum: int) -> tuple[int, float]:
    total = residual + value
    step = round(total)
    return max(-maximum, min(maximum, step)), total - step


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
    return _dynamic_kp(error_distance, config.kp_min, config.kp_max, config.kp_growth)


def _dynamic_kp(error_distance: float, kp_min: float, kp_max: float, kp_growth: float) -> float:
    span = kp_max - kp_min
    growth = 1.0 - math.exp(-kp_growth * max(0.0, error_distance))
    return kp_min + span * growth
