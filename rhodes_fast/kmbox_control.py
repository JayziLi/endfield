from __future__ import annotations

import json
import math
import time
from pathlib import Path

from kmbox_universal import KMBoxClient, KMBoxError

from .config import AimConfig, KmboxConfig
from .detector import Detection


class KmboxController:
    def __init__(self, device: KmboxConfig, aim: AimConfig, runtime_file: Path | None = None):
        self.device_config = device
        self.aim_config = aim
        self.runtime_file = runtime_file
        self._client = None
        self._smooth_x = 0.0
        self._smooth_y = 0.0
        self._trigger = aim.trigger
        self._target_class = aim.target_class
        self._target_y_ratio = aim.target_y_ratio
        self._kp_min = aim.kp_min
        self._kp_max = aim.kp_max
        self._kp_growth = aim.kp_growth
        self._fov_radius = aim.fov_radius
        self._runtime_mtime_ns: int | None = None
        self._next_runtime_check = 0.0
        self.last_send_ms = 0.0

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
        if not self.aim_config.enabled or self._client is None:
            return False
        return bool(getattr(self._client, f"isdown_{self._trigger}")())

    @property
    def target_y_ratio(self) -> float:
        return self._target_y_ratio

    @property
    def target_class(self) -> int:
        return self._target_class

    @property
    def fov_radius(self) -> float:
        return self._fov_radius

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
            trigger = str(values["trigger"])
            target_class = int(values.get("target_class", self._target_class))
            target_y_ratio = float(values["target_y_ratio"])
            kp_min = float(values.get("kp_min", self._kp_min))
            kp_max = float(values.get("kp_max", self._kp_max))
            kp_growth = float(values.get("kp_growth", self._kp_growth))
            fov_radius = float(values.get("fov_radius", self._fov_radius))
            if (
                trigger not in {"left", "right", "side1", "side2"}
                or target_class < 0
                or not 0 <= target_y_ratio <= 1
                or kp_min < 0
                or kp_max < kp_min
                or kp_growth < 0
                or fov_radius <= 0
            ):
                return
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self._trigger = trigger
        self._target_class = target_class
        self._target_y_ratio = target_y_ratio
        self._kp_min = kp_min
        self._kp_max = kp_max
        self._kp_growth = kp_growth
        self._fov_radius = fov_radius
        self._runtime_mtime_ns = modified

    def reset(self) -> None:
        self._smooth_x = 0.0
        self._smooth_y = 0.0

    def move_toward(self, target: Detection, frame_width: int, frame_height: int) -> tuple[int, int]:
        self.last_send_ms = 0.0
        if self._client is None:
            return (0, 0)
        error_x = target.center_x - frame_width * 0.5
        error_y = target.aim_y(self._target_y_ratio) - frame_height * 0.5
        kp = _dynamic_kp(math.hypot(error_x, error_y), self._kp_min, self._kp_max, self._kp_growth)
        raw_x = error_x * kp
        raw_y = error_y * kp
        if raw_x * self._smooth_x < 0:
            self._smooth_x = 0.0
        if raw_y * self._smooth_y < 0:
            self._smooth_y = 0.0
        alpha = self.aim_config.smoothing
        self._smooth_x += alpha * (raw_x - self._smooth_x)
        self._smooth_y += alpha * (raw_y - self._smooth_y)
        dx = _axis_step(self._smooth_x, self.aim_config.deadzone, self.aim_config.max_step)
        dy = _axis_step(self._smooth_y, self.aim_config.deadzone, self.aim_config.max_step)
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


def _axis_step(value: float, deadzone: float, maximum: int) -> int:
    if abs(value) <= deadzone:
        return 0
    return max(-maximum, min(maximum, round(value)))


def dynamic_kp(error_distance: float, config: AimConfig) -> float:
    return _dynamic_kp(error_distance, config.kp_min, config.kp_max, config.kp_growth)


def _dynamic_kp(error_distance: float, kp_min: float, kp_max: float, kp_growth: float) -> float:
    span = kp_max - kp_min
    growth = 1.0 - math.exp(-kp_growth * max(0.0, error_distance))
    return kp_min + span * growth
