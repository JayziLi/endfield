"""Import this file through Endfield's algorithm library (algorithm API v3).

Screen-space constant-velocity Kalman prediction. Flight time is player-tuned
at one distance and, when a reference box height is set, rescaled by the
target's box height. No hero table, automatic firing or gravity model.
"""

from __future__ import annotations

import math

import numpy as np

from rhodes_fast.aim_algorithms.contract import (
    ALGORITHM_API_VERSION, Param, dynamic_kp,
)

__author__ = "Endfield"

if ALGORITHM_API_VERSION < 3:
    raise RuntimeError("卡尔曼弹道预测需要支持算法接口 v3 的 Endfield。")

# Distance changes flight time by at most this factor either way. Past it the
# box is more likely a detection problem than a target 4x nearer or farther.
_DISTANCE_FACTOR_LIMIT = 4.0
_HEIGHT_SMOOTHING_S = 0.1
_ASPECT_SMOOTHING_S = 2.0
# A box this much squatter than usual is crouching or covered from below.
_SHORTER_THAN_USUAL = 0.75
_EDGE_PX = 0.5


class KalmanProjectile:
    NAME = "kalman_projectile"
    DISPLAY_NAME = "卡尔曼弹道预测"
    # Opt in to observing the target inside the raw-error deadzone. The
    # algorithm applies the deadzone to its predicted error before gain.
    HANDLES_DEADZONE = True
    PARAMS = (
        Param("projectile_lead_ms", 0.0, 0.0, 1000.0, "提前时间（毫秒）",
              step=5.0, presets=(0, 50, 100, 200, 300, 500), slider=True),
        Param("reference_box_height", 0.0, 0.0, 1080.0, "参考框高（像素）", step=1.0),
        Param("response", 50.0, 0.0, 100.0, "变向响应（慢→快）",
              step=1.0, slider=True),
        Param("max_lead_px", 80.0, 0.0, 160.0, "最大提前距离（像素）", step=5.0, advanced=True),
        Param("loop_delay_ms", 30.0, 0.0, 200.0, "控制回路延迟（毫秒）", step=1.0, advanced=True),
        Param("camera_scale", 1.0, 0.05, 20.0, "视角补偿（像素/计数）", step=0.05, advanced=True),
    )

    def __init__(self, params):
        # Imported files and hand-edited settings can bypass GUI ranges.
        values = {}
        for spec in self.PARAMS:
            value = float(params.get(spec.name, spec.default))
            if not math.isfinite(value):
                raise ValueError(f"{spec.label}必须是有限数值。")
            values[spec.name] = min(spec.maximum, max(spec.minimum, value))
        self._lead_time = values["projectile_lead_ms"] / 1000.0
        self._reference_height = values["reference_box_height"]
        self._lag = values["loop_delay_ms"] / 1000.0
        self._max_lead = values["max_lead_px"]
        self._scale = values["camera_scale"]
        self._q = (40.0 + 12.0 * values["response"]) ** 2
        self.reset()

    def reset(self):
        self._state = None
        self._cov = np.diag([2.25, 1000000.0])
        self._previous_at = None
        self._tracked_seconds = 0.0
        self._frame_interval = None
        self._height = None
        self._aspect = None

    def _commands(self, observation):
        """Camera motion that became visible this frame, still pending, and how
        uncertain the visible part is.

        A command shows up in the first frame captured after it takes effect,
        so its visible delay spans about one frame around the loop delay.
        Counting it all-or-nothing at the nominal delay puts a whole command
        into the wrong frame, which the filter reads as target velocity and
        the projectile lead multiplies. Commands near the cutoff are counted
        in proportion instead, and their remaining uncertainty is returned so
        the update can trust that measurement less.
        """
        landed = np.zeros(2)
        pending = np.zeros(2)
        variance = 0.0
        width = self._frame_interval or 1.0 / 240.0

        def visible_share(frame_at, issued_at):
            # Nothing issued after a frame arrived can be in that frame.
            if frame_at is None or issued_at >= frame_at:
                return 0.0
            return min(1.0, max(0.0, (frame_at - self._lag - issued_at) / width + 0.5))

        for at, command in zip(observation.recent_command_times, observation.recent_commands):
            before = visible_share(self._previous_at, at)
            if before >= 1.0:
                continue
            shown = visible_share(observation.timestamp, at)
            movement = np.asarray(command, dtype=float) * self._scale
            landed += movement * (shown - before)
            pending += movement * (1.0 - shown)
            variance += float(np.max(movement * movement)) * shown * (1.0 - shown)
        return landed, pending, variance

    def _track_box_height(self, observation, dt):
        """Follow the target's full box height, which stands in for distance."""
        x1, y1, x2, y2 = (float(value) for value in observation.box)
        height, width = y2 - y1, x2 - x1
        if not (math.isfinite(height) and math.isfinite(width)) or height <= 0.0 or width <= 0.0:
            return
        if observation.frame_height and (
                y1 <= _EDGE_PX or y2 >= observation.frame_height - _EDGE_PX):
            # Cut off at the top or bottom: the target is at least this tall.
            self._height = height if self._height is None else max(self._height, height)
            return
        elapsed = min(max(dt, 0.0), 0.15)
        cut_sideways = observation.frame_width and (
            x1 <= _EDGE_PX or x2 >= observation.frame_width - _EDGE_PX)
        if not cut_sideways:
            aspect = height / width
            usual = self._aspect
            self._aspect = aspect if usual is None else (
                usual + (1.0 - math.exp(-elapsed / _ASPECT_SMOOTHING_S)) * (aspect - usual))
            if usual is not None and aspect < _SHORTER_THAN_USUAL * usual:
                # Crouching or covered from below: shorter, not farther away.
                return
        if self._height is None:
            self._height = height
        else:
            self._height += (1.0 - math.exp(-elapsed / _HEIGHT_SMOOTHING_S)) * (height - self._height)

    def _flight_time(self):
        if self._reference_height <= 0.0 or not self._height:
            return self._lead_time
        factor = self._reference_height / self._height
        return self._lead_time * min(_DISTANCE_FACTOR_LIMIT, max(1.0 / _DISTANCE_FACTOR_LIMIT, factor))

    def compute(self, observation):
        measurement = np.array([observation.error_x, observation.error_y], dtype=float)
        if (not np.isfinite(measurement).all()
                or not math.isfinite(observation.timestamp)
                or not math.isfinite(observation.dt)
                or not math.isfinite(observation.age)
                or observation.age > 0.25):
            self.reset()
            return 0.0, 0.0
        if self._previous_at is not None and observation.timestamp <= self._previous_at:
            return 0.0, 0.0

        landed, pending, camera_variance = self._commands(observation)
        dt = observation.dt
        if self._state is None or not 0.0 < dt <= 0.15:
            self.reset()
            self._state = np.array([measurement, np.zeros(2)])
        else:
            # The camera changes observed position, not target velocity.
            transition = np.array([[1.0, dt], [0.0, 1.0]])
            predicted = transition @ self._state
            predicted[0] -= landed
            process_noise = self._q * np.array([
                [dt ** 3 / 3.0, dt ** 2 / 2.0],
                [dt ** 2 / 2.0, dt],
            ])
            covariance = transition @ self._cov @ transition.T + process_noise
            residual = measurement - predicted[0]
            if np.linalg.norm(residual) > max(24.0, 2500.0 * dt):
                # Identity/box discontinuity: do not extrapolate the jump.
                self.reset()
                self._state = np.array([measurement, np.zeros(2)])
            else:
                noise = 2.25 + camera_variance
                gain = covariance[:, 0] / (covariance[0, 0] + noise)
                self._state = predicted + gain[:, None] * residual
                correction = np.eye(2)
                correction[:, 0] -= gain
                # Joseph form retains positive covariance under rounding.
                self._cov = (
                    correction @ covariance @ correction.T
                    + noise * np.outer(gain, gain)
                )
                self._tracked_seconds += dt
        self._previous_at = observation.timestamp
        self._track_box_height(observation, dt)
        if 0.0 < dt <= 0.05:
            # Skipped frames are rare; a slow average stays near the capture interval.
            self._frame_interval = (
                dt if self._frame_interval is None
                else self._frame_interval + 0.05 * (dt - self._frame_interval)
            )

        horizon = self._lag + max(0.0, observation.age) + self._flight_time()
        # Let several observations establish velocity after acquisition/reset.
        settled = min(1.0, self._tracked_seconds / 0.05)
        lead = self._state[1] * horizon * settled
        # Turn with the target until the next frame. Proportional output alone
        # only reaches the target's speed once the camera trails by
        # speed x dt / kp, which is tens of pixels for a strafing target.
        pacing = self._state[1] * dt * settled
        uncertainty = math.sqrt(max(0.0, float(
            self._cov[0, 0] + 2 * horizon * self._cov[0, 1]
            + horizon * horizon * self._cov[1, 1]
        )))
        if uncertainty > max(8.0, self._max_lead / 2.0):
            lead *= max(8.0, self._max_lead / 2.0) / uncertainty
        length = float(np.linalg.norm(lead))
        if length > self._max_lead:
            lead *= self._max_lead / length
        aim = self._state[0] - pending + lead
        distance = float(np.linalg.norm(aim))
        correction = np.zeros(2)
        if distance > observation.deadzone:
            kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
            correction = aim * kp
        movement = (correction + pacing) / self._scale
        return float(movement[0]), float(movement[1])
