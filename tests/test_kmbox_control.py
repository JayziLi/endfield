from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kmbox_universal import KMBoxTimeoutError

from rhodes_fast.config import AimConfig, AimProfileConfig, KmboxConfig
from rhodes_fast.detector import Detection
from rhodes_fast.kmbox_control import KmboxController, dynamic_kp


def _profiles(primary: AimProfileConfig) -> tuple[AimProfileConfig, AimProfileConfig]:
    return (primary, AimProfileConfig(enabled=False, trigger="left"))


class KmboxControllerTests(unittest.TestCase):
    @patch("rhodes_fast.kmbox_control.time.sleep")
    @patch("rhodes_fast.kmbox_control.KMBoxClient")
    def test_retries_device_timeout_during_startup(self, client_class: Mock, _sleep: Mock) -> None:
        client = Mock()
        client_class.side_effect = [KMBoxTimeoutError("timeout"), client]
        controller = KmboxController(
            KmboxConfig(uuid="00000000", connect_attempts=2),
            AimConfig(),
            profiles=_profiles(AimProfileConfig(trigger="side1")),
        )
        controller.connect()
        self.assertIs(controller._client, client)
        self.assertEqual(client_class.call_count, 2)

    def test_move_timeout_does_not_terminate_pipeline(self) -> None:
        controller = KmboxController(KmboxConfig(uuid="00000000"), AimConfig(smoothing=1.0))
        client = Mock()
        client.enc_move.side_effect = KMBoxTimeoutError("timeout")
        controller._client = client
        moved = controller.move_toward(Detection(200, 100, 240, 180, 0.9, 0), 320, 320)
        self.assertEqual(moved, (0, 0))

    def test_dynamic_kp_starts_at_minimum_and_saturates_at_maximum(self) -> None:
        config = AimProfileConfig(kp_min=0.1, kp_max=0.164, kp_growth=0.167)
        self.assertAlmostEqual(dynamic_kp(0, config), 0.1)
        self.assertGreater(dynamic_kp(10, config), dynamic_kp(2, config))
        self.assertAlmostEqual(dynamic_kp(100, config), 0.164, places=6)

    def test_dynamic_kp_controls_mouse_step(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(smoothing=1.0, deadzone=0),
            profiles=_profiles(AimProfileConfig(kp_min=0.1, kp_max=0.164, kp_growth=0.167)),
        )
        client = Mock()
        controller._client = client

        moved = controller.move_toward(Detection(200, 140, 240, 180, 0.9, 0), 320, 320)

        self.assertEqual(moved, (10, -1))
        client.enc_move.assert_called_once_with(10, -1)

    def test_error_inside_deadzone_does_not_move(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(target_y_ratio=0.5, smoothing=1.0, deadzone=1.5, max_step=30),
            profiles=_profiles(AimProfileConfig(kp_min=2.0, kp_max=2.0, kp_growth=0.0, target_y_ratio=0.5)),
        )
        client = Mock()
        controller._client = client
        target = Detection(151, 150, 171, 170, 0.9, 0)

        for _frame in range(20):
            self.assertEqual(controller.move_toward(target, 320, 320), (0, 0))

        client.enc_move.assert_not_called()

    def test_deadzone_measures_error_distance_not_output_step(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(target_y_ratio=0.5, smoothing=1.0, deadzone=1.5, max_step=30),
            profiles=_profiles(AimProfileConfig(kp_min=0.09, kp_max=0.09, kp_growth=0.0, target_y_ratio=0.5)),
        )
        client = Mock()
        controller._client = client
        target = Detection(160, 150, 180, 170, 0.9, 0)

        self.assertEqual(controller.move_toward(target, 320, 320), (1, 0))
        client.enc_move.assert_called_once_with(1, 0)

    def test_low_gain_converges_to_within_deadzone(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(target_y_ratio=0.5, smoothing=0.35, deadzone=1.5, max_step=30),
            profiles=_profiles(AimProfileConfig(kp_min=0.053, kp_max=0.09, kp_growth=0.058, target_y_ratio=0.5)),
        )
        controller._client = Mock()
        error_x = 10.0

        for _frame in range(120):
            target = Detection(150 + error_x, 150, 170 + error_x, 170, 0.9, 0)
            dx, _ = controller.move_toward(target, 320, 320)
            error_x -= dx

        self.assertLessEqual(abs(error_x), 1.5)

    def test_subpixel_remainders_accumulate_into_whole_pixel_moves(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(target_y_ratio=0.5, smoothing=1.0, deadzone=0, max_step=30),
            profiles=_profiles(
                AimProfileConfig(kp_min=0.1, kp_max=0.1, kp_growth=0.0, target_y_ratio=0.5)
            ),
        )
        controller._client = Mock()
        target = Detection(154, 150, 174, 170, 0.9, 0)

        commands = [controller.move_toward(target, 320, 320)[0] for _frame in range(10)]

        self.assertEqual(sum(commands), 4)

    def test_reset_clears_subpixel_accumulator(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(target_y_ratio=0.5, smoothing=1.0, deadzone=0, max_step=30),
            profiles=_profiles(
                AimProfileConfig(kp_min=0.1, kp_max=0.1, kp_growth=0.0, target_y_ratio=0.5)
            ),
        )
        controller._client = Mock()
        target = Detection(154, 150, 174, 170, 0.9, 0)

        self.assertEqual(controller.move_toward(target, 320, 320), (0, 0))
        self.assertEqual(controller.move_toward(target, 320, 320), (1, 0))

        controller.reset()
        self.assertEqual(controller.move_toward(target, 320, 320), (0, 0))

    def test_a_timed_out_trigger_query_does_not_take_down_the_pipeline(self) -> None:
        # KMBox 超时只有 20ms。进程被 Windows 丢进效率模式后, 一次查询按键状态
        # 就可能超时——而这条路径原本没有兜底, 异常会一路穿透主循环把管线打挂。
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=_profiles(AimProfileConfig(trigger="right")),
        )
        client = Mock()
        client.isdown_right.side_effect = KMBoxTimeoutError("timeout")
        controller._client = client

        self.assertFalse(controller.trigger_active())
        self.assertIsNone(controller.active_profile_number)

    def test_the_aim_resumes_after_a_trigger_query_recovers(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=_profiles(AimProfileConfig(trigger="right")),
        )
        client = Mock()
        client.isdown_right.side_effect = [KMBoxTimeoutError("timeout"), 1, 1]
        controller._client = client

        self.assertFalse(controller.trigger_active())
        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 1)

    def test_reset_discards_smoothed_movement(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(
                target_y_ratio=0.5,
                smoothing=1.0,
                deadzone=0,
            ),
            profiles=_profiles(AimProfileConfig(kp_min=0.1, kp_max=0.1, kp_growth=0.0)),
        )
        client = Mock()
        controller._client = client
        target = Detection(154, 150, 174, 170, 0.9, 0)

        self.assertEqual(controller.move_toward(target, 320, 320), (0, 0))
        controller.reset()
        self.assertEqual(controller.move_toward(target, 320, 320), (0, 0))
        client.enc_move.assert_not_called()

    def test_timed_out_move_is_retried_while_error_remains(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(
                target_y_ratio=0.5,
                smoothing=1.0,
                deadzone=0,
            ),
            profiles=_profiles(AimProfileConfig(kp_min=0.1, kp_max=0.1, kp_growth=0.0)),
        )
        client = Mock()
        client.enc_move.side_effect = [KMBoxTimeoutError("timeout"), None]
        controller._client = client
        target = Detection(210, 150, 230, 170, 0.9, 0)

        self.assertEqual(controller.move_toward(target, 320, 320), (0, 0))
        self.assertEqual(controller.move_toward(target, 320, 320), (6, 0))
        self.assertEqual(client.enc_move.call_count, 2)

    def test_noisy_stationary_target_does_not_chase_detection_jitter(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(
                target_y_ratio=0.5,
                smoothing=0.35,
                deadzone=1.5,
                max_step=30,
            ),
            profiles=_profiles(
                AimProfileConfig(
                    kp_min=0.053, kp_max=0.09, kp_growth=0.058, target_y_ratio=0.5
                )
            ),
        )
        controller._client = Mock()
        actual_error = 0.0
        excursions: list[float] = []
        commands: list[int] = []
        noise_samples = ([3.0] * 12 + [-3.0] * 12) * 4

        for noise in noise_samples:
            measured_error = actual_error + noise
            target = Detection(
                150 + measured_error,
                150,
                170 + measured_error,
                170,
                0.9,
                0,
            )
            dx, _ = controller.move_toward(target, 320, 320)
            actual_error -= dx
            commands.append(dx)
            excursions.append(abs(actual_error))

        self.assertLessEqual(max(excursions), 1.5)
        self.assertLessEqual(max(abs(command) for command in commands), 1)

    def test_trigger_and_aim_position_update_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / "aim.json"
            runtime_file.write_text(
                json.dumps(
                    {
                        "target_class": 3,
                        "target_y_ratio": 0.25,
                        "fov_radius": 75,
                        "profiles": [
                            {
                                "enabled": True,
                                "trigger": "side1",
                                "kp_min": 0.2,
                                "kp_max": 0.2,
                                "kp_growth": 0.0,
                            },
                            {
                                "enabled": False,
                                "trigger": "left",
                                "kp_min": 0.1,
                                "kp_max": 0.164,
                                "kp_growth": 0.167,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller = KmboxController(
                KmboxConfig(uuid="00000000"),
                AimConfig(target_y_ratio=0.4, smoothing=1.0, deadzone=0),
                runtime_file,
                profiles=_profiles(AimProfileConfig(trigger="right")),
            )
            client = Mock()
            client.isdown_side1.return_value = 1
            controller._client = client

            controller.refresh_runtime_settings(force=True)

        self.assertTrue(controller.trigger_active())
        client.isdown_side1.assert_called_once_with()
        self.assertEqual(controller.target_y_ratio, 0.25)
        self.assertEqual(controller.target_class, 3)
        self.assertEqual(controller.fov_radius, 75)
        self.assertEqual(
            controller.move_toward(Detection(170, 160, 190, 200, 0.9, 0), 320, 320),
            (4, 2),
        )

    def test_disabled_profiles_never_activate(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=(
                AimProfileConfig(enabled=False, trigger="right"),
                AimProfileConfig(enabled=False, trigger="left"),
            ),
        )
        client = Mock()
        client.isdown_right.return_value = 1
        client.isdown_left.return_value = 1
        controller._client = client

        self.assertFalse(controller.trigger_active())
        self.assertIsNone(controller.active_profile_number)
        client.isdown_right.assert_not_called()
        client.isdown_left.assert_not_called()

    def test_last_pressed_profile_wins_and_release_falls_back(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=(
                AimProfileConfig(trigger="right"),
                AimProfileConfig(trigger="left"),
            ),
        )
        client = Mock()
        controller._client = client

        client.isdown_right.return_value = 1
        client.isdown_left.return_value = 0
        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 1)

        client.isdown_left.return_value = 1
        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 2)

        client.isdown_left.return_value = 0
        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 1)

    @patch("rhodes_fast.kmbox_control.time.perf_counter", return_value=0.0)
    def test_each_profile_uses_its_own_dynamic_p_values(self, _clock: Mock) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(target_y_ratio=0.5, smoothing=1.0, deadzone=0),
            profiles=(
                AimProfileConfig(trigger="right", kp_min=0.05, kp_max=0.05, kp_growth=0.0),
                AimProfileConfig(trigger="left", kp_min=0.2, kp_max=0.2, kp_growth=0.0),
            ),
        )
        client = Mock()
        client.isdown_right.return_value = 1
        client.isdown_left.return_value = 0
        controller._client = client
        target = Detection(250, 150, 270, 170, 0.9, 0)

        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.move_toward(target, 320, 320), (5, 0))

        client.isdown_left.return_value = 1
        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 2)
        self.assertEqual(controller.move_toward(target, 320, 320), (20, 0))

    def test_simultaneous_first_press_uses_profile_one(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=(
                AimProfileConfig(trigger="right"),
                AimProfileConfig(trigger="left"),
            ),
        )
        client = Mock()
        client.isdown_right.return_value = 1
        client.isdown_left.return_value = 1
        controller._client = client

        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 1)

    def test_each_profile_uses_its_own_target_settings(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=(
                AimProfileConfig(trigger="right", target_class=1, target_y_ratio=0.2, fov_radius=80.0),
                AimProfileConfig(trigger="left", target_class=3, target_y_ratio=0.6, fov_radius=200.0),
            ),
        )
        client = Mock()
        client.isdown_right.return_value = 0
        client.isdown_left.return_value = 0
        controller._client = client

        self.assertFalse(controller.trigger_active())
        self.assertEqual(controller.target_class, 1)
        self.assertEqual(controller.target_y_ratio, 0.2)
        self.assertEqual(controller.fov_radius, 80.0)

        client.isdown_left.return_value = 1
        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 2)
        self.assertEqual(controller.target_class, 3)
        self.assertEqual(controller.target_y_ratio, 0.6)
        self.assertEqual(controller.fov_radius, 200.0)

    def test_runtime_update_applies_per_profile_target_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / "aim.json"
            runtime_file.write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "enabled": True,
                                "trigger": "right",
                                "kp_min": 0.1,
                                "kp_max": 0.1,
                                "kp_growth": 0.0,
                                "target_class": 2,
                                "target_y_ratio": 0.3,
                                "fov_radius": 90.0,
                            },
                            {
                                "enabled": True,
                                "trigger": "left",
                                "kp_min": 0.1,
                                "kp_max": 0.1,
                                "kp_growth": 0.0,
                                "target_class": 4,
                                "target_y_ratio": 0.7,
                                "fov_radius": 220.0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller = KmboxController(
                KmboxConfig(uuid="00000000"),
                AimConfig(),
                runtime_file,
                profiles=_profiles(AimProfileConfig(trigger="right")),
            )
            client = Mock()
            client.isdown_right.return_value = 0
            client.isdown_left.return_value = 1
            controller._client = client

            controller.refresh_runtime_settings(force=True)

        self.assertTrue(controller.trigger_active())
        self.assertEqual(controller.active_profile_number, 2)
        self.assertEqual(controller.target_class, 4)
        self.assertEqual(controller.target_y_ratio, 0.7)
        self.assertEqual(controller.fov_radius, 220.0)


if __name__ == "__main__":
    unittest.main()
