from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kmbox_universal import KMBoxTimeoutError

from rhodes_fast.config import AimConfig, KmboxConfig
from rhodes_fast.detector import Detection
from rhodes_fast.kmbox_control import KmboxController, dynamic_kp


class KmboxControllerTests(unittest.TestCase):
    @patch("rhodes_fast.kmbox_control.time.sleep")
    @patch("rhodes_fast.kmbox_control.KMBoxClient")
    def test_retries_device_timeout_during_startup(self, client_class: Mock, _sleep: Mock) -> None:
        client = Mock()
        client_class.side_effect = [KMBoxTimeoutError("timeout"), client]
        controller = KmboxController(
            KmboxConfig(uuid="00000000", connect_attempts=2),
            AimConfig(trigger="side1"),
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
        config = AimConfig(kp_min=0.1, kp_max=0.164, kp_growth=0.167)
        self.assertAlmostEqual(dynamic_kp(0, config), 0.1)
        self.assertGreater(dynamic_kp(10, config), dynamic_kp(2, config))
        self.assertAlmostEqual(dynamic_kp(100, config), 0.164, places=6)

    def test_dynamic_kp_controls_mouse_step(self) -> None:
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(kp_min=0.1, kp_max=0.164, kp_growth=0.167, smoothing=1.0, deadzone=0),
        )
        client = Mock()
        controller._client = client

        moved = controller.move_toward(Detection(200, 140, 240, 180, 0.9, 0), 320, 320)

        self.assertEqual(moved, (10, -1))
        client.enc_move.assert_called_once_with(10, -1)

    def test_trigger_and_aim_position_update_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / "aim.json"
            runtime_file.write_text(
                json.dumps(
                    {
                        "trigger": "side1",
                        "target_class": 3,
                        "target_y_ratio": 0.25,
                        "kp_min": 0.2,
                        "kp_max": 0.2,
                        "kp_growth": 0.0,
                        "fov_radius": 75,
                    }
                ),
                encoding="utf-8",
            )
            controller = KmboxController(
                KmboxConfig(uuid="00000000"),
                AimConfig(trigger="right", target_y_ratio=0.4, smoothing=1.0, deadzone=0),
                runtime_file,
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


if __name__ == "__main__":
    unittest.main()
