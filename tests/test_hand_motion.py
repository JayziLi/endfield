from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from kmbox_universal.monitor import MonitorState
from kmbox_universal.types import MouseReport

from rhodes_fast.config import AimConfig, KmboxConfig
from rhodes_fast.kmbox_control import HandMotion, KmboxController


def _report(x: int, y: int) -> MonitorState:
    return MonitorState(mouse=MouseReport(report_id=1, buttons=0, x=x, y=y, wheel=0))


class HandMotionTests(unittest.TestCase):
    def test_adds_up_every_report_until_taken(self) -> None:
        motion = HandMotion()
        motion.on_report(_report(3, -1))
        motion.on_report(_report(4, 2))

        self.assertEqual(motion.take(), (7, 1))

    def test_taking_starts_the_count_again(self) -> None:
        # 主循环每帧取一次。不清零的话每帧拿到的是开机以来的总位移。
        motion = HandMotion()
        motion.on_report(_report(5, 5))
        motion.take()
        motion.on_report(_report(1, 0))

        self.assertEqual(motion.take(), (1, 0))

    def test_nothing_reported_means_no_movement(self) -> None:
        self.assertEqual(HandMotion().take(), (0, 0))


class ControllerHandMotionTests(unittest.TestCase):
    def _controller(self) -> KmboxController:
        return KmboxController(KmboxConfig(uuid="00000000"), AimConfig())

    @patch("rhodes_fast.kmbox_control.KMBoxClient")
    def test_connecting_listens_to_the_physical_mouse(self, client_class: Mock) -> None:
        client = Mock()
        client_class.return_value = client
        controller = self._controller()

        controller.connect()

        client.monitor.add_callback.assert_called_once()
        listener = client.monitor.add_callback.call_args.args[0]
        listener(_report(6, -2))
        listener(_report(1, 1))
        self.assertEqual(controller.take_hand_motion(), (7, -1))

    def test_without_a_device_the_hand_never_moves(self) -> None:
        self.assertEqual(self._controller().take_hand_motion(), (0, 0))

    @patch("rhodes_fast.kmbox_control.KMBoxClient")
    def test_movement_from_before_a_reconnect_is_dropped(self, client_class: Mock) -> None:
        client = Mock()
        client_class.return_value = client
        controller = self._controller()
        controller.connect()
        client.monitor.add_callback.call_args.args[0](_report(9, 9))

        controller.close()

        self.assertEqual(controller.take_hand_motion(), (0, 0))


if __name__ == "__main__":
    unittest.main()
