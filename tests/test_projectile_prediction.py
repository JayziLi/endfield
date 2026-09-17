from __future__ import annotations

import math
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rhodes_fast.aim_algorithms import Observation, create_algorithm, set_installed_algorithms
from rhodes_fast.algorithm_library import install, load_installed
from rhodes_fast.config import AimConfig, AimProfileConfig, KmboxConfig
from rhodes_fast.detector import Detection
from rhodes_fast.kmbox_control import KmboxController


SOURCE = Path(__file__).parents[1] / "examples" / "kalman_projectile.py"


def observe(x, y=0.0, *, at=1.0, dt=1 / 240, commands=(), times=(), deadzone=0.0,
            box=(0.0, 0.0, 0.0, 0.0), frame=(0, 0)):
    return Observation(
        x, y, dt, 0, commands, 1.0, 1.0, 0.0,
        timestamp=at, recent_command_times=times, deadzone=deadzone,
        box=box, frame_width=frame[0], frame_height=frame[1],
    )


def body(height, width=None, top=100.0):
    """A target box well inside a 320 x 320 frame, three times as tall as wide."""
    width = height / 3 if width is None else width
    return (150.0, top, 150.0 + width, top + height)


class ProjectilePredictionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        install(Path(self.folder.name), SOURCE)
        factories, warnings = load_installed(Path(self.folder.name))
        self.assertEqual(warnings, [])
        set_installed_algorithms(factories)

    def tearDown(self):
        set_installed_algorithms({})
        self.folder.cleanup()

    def algorithm(self, **params):
        return create_algorithm("kalman_projectile", {"loop_delay_ms": 0, **params})

    # These open-loop checks use kp = 1, so the output is the predicted aim point
    # plus the camera motion needed to keep pace with the target until the next
    # frame (target speed x dt).

    def test_library_import_and_default_zero_extra_lead(self):
        algorithm = self.algorithm()
        for i in range(240):
            output = algorithm.compute(observe(i / 240 * 120, at=1 + i / 240))
        self.assertAlmostEqual(output[0], 119.5 + 0.5, delta=0.05)

    def test_constant_velocity_predicts_position_at_projectile_arrival(self):
        # Target moves 120 px/s; a 100 ms projectile must meet it 12 px ahead.
        for fps in (60, 120, 240):
            with self.subTest(fps=fps):
                algorithm = self.algorithm(projectile_lead_ms=100)
                for i in range(fps + 1):
                    output = algorithm.compute(observe(120 * i / fps, at=1 + i / fps, dt=1 / fps))
                self.assertAlmostEqual(output[0], 132.0 + 120 / fps, delta=0.1)
                self.assertAlmostEqual(output[1], 0.0)

    def test_irregular_capture_intervals_preserve_time_based_lead(self):
        algorithm = self.algorithm(projectile_lead_ms=100)
        elapsed = 0.0
        for i in range(300):
            dt = (0.003, 0.004, 0.013, 0.006)[i % 4]
            elapsed += dt
            output = algorithm.compute(observe(120 * elapsed, at=1 + elapsed, dt=dt))
        self.assertAlmostEqual(output[0] - 120 * elapsed, 12.0 + 120 * dt, delta=0.1)

    def test_small_gain_keeps_pace_with_a_moving_target(self):
        # Proportional output alone trails until kp * error equals the target's
        # per-frame motion: 0.5 px / 0.05 = 10 px behind at 120 px/s.
        algorithm = self.algorithm()
        camera = 0.0
        commands, times = [], []
        for i in range(480):
            at = 1 + i / 240
            target = 120 * i / 240
            dx, _ = algorithm.compute(Observation(
                target - camera, 0.0, 1 / 240, i, commands, 0.05, 0.05, 0.0,
                timestamp=at, recent_command_times=times,
            ))
            camera += dx
            commands.append((dx, 0.0))
            times.append(at + 0.001)
        self.assertLess(abs(target - camera), 1.0)

    def test_stationary_target_stays_stationary_during_camera_motion(self):
        algorithm = self.algorithm(projectile_lead_ms=200, loop_delay_ms=20, camera_scale=2)
        commands, times = [], []
        for i in range(101):
            at = 1 + i * 0.01
            visible = sum(2 * dx for (dx, _), t in zip(commands, times) if t <= at - 0.02)
            output = algorithm.compute(observe(100 - visible, at=at, dt=0.01, commands=commands, times=times))
            # Camera has moved once per issued command, including pending ones.
            self.assertAlmostEqual(output[0], (100 - 2 * len(commands)) / 2, delta=0.01)
            commands.append((1, 0))
            # Issued half a frame after the capture, so no frame is ambiguous.
            times.append(at + 0.005)

    def test_camera_motion_with_uncertain_landing_frame_does_not_shift_prediction(self):
        # A command shows up in the first frame captured after it takes effect,
        # so its visible delay varies by up to a frame. Counting it all-or-nothing
        # at the nominal delay reads our own camera motion as target velocity,
        # which the 300 ms lead then multiplies.
        for fps in (60, 240):
            with self.subTest(fps=fps):
                rng = random.Random(3)
                frame = 1 / fps
                algorithm = self.algorithm(projectile_lead_ms=300, loop_delay_ms=26, max_lead_px=160)
                commands, times, visible_at, drift = [], [], [], []
                for i in range(2 * fps):
                    at = 1 + i * frame
                    seen = sum(dx for (dx, _), shown in zip(commands, visible_at) if shown <= at)
                    output = algorithm.compute(
                        observe(100 - seen, at=at, dt=frame, commands=commands, times=times))
                    if i >= fps // 2:
                        drift.append(output[0] - (100 - sum(dx for dx, _ in commands)))
                    sent = at + 0.003
                    commands.append((rng.uniform(-15, 15), 0.0))
                    times.append(sent)
                    visible_at.append(sent + 0.026 + rng.uniform(-frame / 2, frame / 2))
                rms = math.sqrt(sum(d * d for d in drift) / len(drift))
                self.assertLess(rms, 25.0)

    def test_detection_jitter_right_after_lock_does_not_jerk_the_camera(self):
        # Two frames in, velocity is a guess built from one noisy difference.
        algorithm = self.algorithm()
        algorithm.compute(Observation(0.0, 0.0, 1 / 240, 0, (), 0.0, 0.0, 0.0, timestamp=1.0))
        output = algorithm.compute(Observation(5.0, 0.0, 1 / 240, 1, (), 0.0, 0.0, 0.0, timestamp=1 + 1 / 240))
        self.assertLess(abs(output[0]), 1.0)

    def test_flight_time_does_not_change_command_alignment(self):
        outputs = []
        for lead_ms in (0, 500):
            algorithm = self.algorithm(projectile_lead_ms=lead_ms, loop_delay_ms=30)
            algorithm.compute(observe(100, at=1.0))
            outputs.append(algorithm.compute(observe(100, at=1.01, dt=0.01, commands=((10, 0),), times=(1.0,))))
        self.assertEqual(outputs, [(90.0, 0.0), (90.0, 0.0)])

    def test_still_target_inside_the_deadzone_asks_for_no_movement(self):
        algorithm = self.algorithm(projectile_lead_ms=100)
        for i in range(60):
            output = algorithm.compute(observe(1.5, at=1 + i / 240, deadzone=2))
        self.assertEqual(output, (0.0, 0.0))

    def test_target_crossing_raw_deadzone_keeps_future_lead(self):
        algorithm = self.algorithm(projectile_lead_ms=100)
        for i in range(241):
            output = algorithm.compute(observe(-120 + i * 0.5, at=1 + i / 240, deadzone=2))
        self.assertAlmostEqual(output[0], 12.0 + 0.5, delta=0.1)

    def test_reversal_response_reduces_old_direction_lead(self):
        errors = []
        for response in (0, 100):
            algorithm = self.algorithm(projectile_lead_ms=100, response=response)
            for i in range(241):
                algorithm.compute(observe(120 * i / 240, at=1 + i / 240))
            for i in range(1, 25):
                output = algorithm.compute(observe(120 - 120 * i / 240, at=2 + i / 240))
            errors.append(abs(output[0] - (96.0 - 0.5)))
        self.assertLess(errors[1], errors[0])
        self.assertLess(errors[1], 2.0)

    def lead_after(self, boxes, **params):
        """Lead in px for a 120 px/s target given one box per frame, 100 ms tuned lead."""
        algorithm = self.algorithm(projectile_lead_ms=100, max_lead_px=160, **params)
        for i, box in enumerate(boxes):
            x = 120 * i / 240
            output = algorithm.compute(observe(x, at=1 + i / 240, box=box, frame=(320, 320)))
        return output[0] - x - 0.5

    def test_lead_time_follows_distance_from_box_height(self):
        # Twice as far away: half the box height, twice the projectile flight time.
        for height, lead in ((60, 12.0), (30, 24.0), (120, 6.0)):
            with self.subTest(height=height):
                self.assertAlmostEqual(
                    self.lead_after([body(height)] * 241, reference_box_height=60), lead, delta=0.2)

    def test_tuned_lead_is_used_without_reference_height_or_box(self):
        self.assertAlmostEqual(self.lead_after([body(30)] * 241), 12.0, delta=0.2)
        self.assertAlmostEqual(
            self.lead_after([(0.0, 0.0, 0.0, 0.0)] * 241, reference_box_height=60), 12.0, delta=0.2)

    def test_one_frame_box_glitch_barely_moves_the_lead(self):
        boxes = [body(60)] * 240 + [body(20)]
        self.assertAlmostEqual(self.lead_after(boxes, reference_box_height=60), 12.0, delta=1.0)

    def test_target_walking_away_gets_more_lead(self):
        boxes = [body(60 - 30 * min(1.0, i / 240)) for i in range(361)]
        self.assertAlmostEqual(self.lead_after(boxes, reference_box_height=60), 24.0, delta=1.0)

    def test_box_cut_off_by_the_frame_edge_does_not_read_as_farther(self):
        # Only the top 40 px of the 60 px target is inside the frame.
        cut = body(40, top=280.0)
        boxes = [body(60)] * 240 + [cut] * 240
        self.assertAlmostEqual(self.lead_after(boxes, reference_box_height=60), 12.0, delta=0.5)

    def test_crouching_does_not_read_as_farther(self):
        boxes = [body(60, 20)] * 240 + [body(39, 22)] * 240
        self.assertAlmostEqual(self.lead_after(boxes, reference_box_height=60), 12.0, delta=0.5)

    def test_box_cut_off_at_the_side_does_not_skew_the_usual_shape(self):
        # Half out of the frame sideways the box is narrow, not tall. Learning that
        # shape as normal would make every whole box afterwards look like crouching.
        cut = (0.0, 100.0, 8.0, 160.0)
        boxes = [cut] * 480 + [body(30)] * 240
        self.assertAlmostEqual(self.lead_after(boxes, reference_box_height=60), 24.0, delta=1.0)

    def test_new_target_does_not_inherit_the_previous_distance(self):
        algorithm = self.algorithm(projectile_lead_ms=100, max_lead_px=160, reference_box_height=60)
        for i in range(240):
            algorithm.compute(observe(120 * i / 240, at=1 + i / 240, box=body(30), frame=(320, 320)))
        algorithm.reset()
        for i in range(24):
            x = 120 * i / 240
            output = algorithm.compute(observe(x, at=3 + i / 240, box=body(120), frame=(320, 320)))
        self.assertAlmostEqual(output[0] - x - 0.5, 6.0, delta=0.5)

    def test_distance_scaling_is_bounded(self):
        # 20x farther is clamped to 4x the tuned flight time, 5x nearer to a quarter.
        self.assertAlmostEqual(self.lead_after([body(3)] * 241, reference_box_height=60), 48.0, delta=0.5)
        self.assertAlmostEqual(self.lead_after([body(200)] * 241, reference_box_height=40), 3.0, delta=0.2)

    def test_prediction_offset_is_capped_radially(self):
        algorithm = self.algorithm(projectile_lead_ms=500, max_lead_px=10)
        for i in range(241):
            output = algorithm.compute(observe(i * 0.5, i * 0.5, at=1 + i / 240))
        self.assertAlmostEqual(math.hypot(output[0] - 120.5, output[1] - 120.5), 10, delta=0.1)

    def test_reset_gap_and_box_jump_do_not_extrapolate_stale_velocity(self):
        for event in ("reset", "gap", "jump"):
            with self.subTest(event=event):
                algorithm = self.algorithm(projectile_lead_ms=200)
                for i in range(241):
                    algorithm.compute(observe(i * 0.5, at=1 + i / 240))
                if event == "reset":
                    algorithm.reset()
                output = algorithm.compute(observe(-100, at=2.5, dt=0.5 if event == "gap" else 1 / 240))
                self.assertEqual(output, (-100.0, 0.0))

    def test_stale_and_nonfinite_observations_stop_output(self):
        for sample in (observe(float("nan")), replace(observe(10), age=0.3), replace(observe(10), dt=float("inf"))):
            self.assertEqual(self.algorithm().compute(sample), (0.0, 0.0))

    def test_invalid_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            self.algorithm(projectile_lead_ms=float("nan"))

    def test_controller_uses_receiver_time_and_keeps_predicting_at_center(self):
        profile = AimProfileConfig(
            algorithm="kalman_projectile", algorithm_params={"projectile_lead_ms": 100, "loop_delay_ms": 0},
            kp_min=1, kp_max=1, kp_growth=0, target_y_ratio=0.5,
        )
        controller = KmboxController(
            KmboxConfig(uuid="00000000"), AimConfig(smoothing=1, deadzone=2, max_step=100),
            profiles=(profile, AimProfileConfig(enabled=False, trigger="left")),
        )
        controller._client = Mock()
        # Isolated zero-camera-output plant: failed commands must not be
        # subtracted, but prediction continues and eventually requests lead.
        controller._client.enc_move.side_effect = OSError("simulated disconnected actuator")
        for i in range(241):
            at = 1 + i / 240
            x = 40 + i * 0.5
            target = Detection(x - 5, 155, x + 5, 165, 0.9, 0)
            with patch("rhodes_fast.kmbox_control.time.perf_counter", return_value=at):
                controller.move_toward(target, 320, 320, observed_at=at)
        # 12 px lead plus 0.5 px of pacing; the sub-pixel residual alternates 12/13.
        self.assertIn(controller._client.enc_move.call_args.args, ((12, 0), (13, 0)))

    def test_out_of_order_receiver_frame_does_not_issue_a_command(self):
        controller = KmboxController(
            KmboxConfig(uuid="00000000"), AimConfig(smoothing=1),
            profiles=(AimProfileConfig(algorithm="kalman_projectile"), AimProfileConfig(enabled=False)),
        )
        controller._client = Mock()
        target = Detection(200, 150, 220, 170, 0.9, 0)
        with patch("rhodes_fast.kmbox_control.time.perf_counter", return_value=2):
            controller.move_toward(target, 320, 320, observed_at=1.9)
            controller._client.reset_mock()
            self.assertEqual(controller.move_toward(target, 320, 320, observed_at=1.8), (0, 0))
            controller._client.enc_move.assert_not_called()


if __name__ == "__main__":
    unittest.main()
