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

    def test_raw_input_adds_to_the_same_total(self) -> None:
        """SendInput 模式下手的移动从 Raw Input 来, 走 add, 跟 KMBox 监听口的报告
        累加在同一个地方, 主循环取的方式不变。"""
        motion = HandMotion()
        motion.add(3, -1)
        motion.add(4, 2)
        self.assertEqual(motion.take(), (7, 1))


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

    @patch("rhodes_fast.kmbox_control.LocalMouseClient")
    @patch("rhodes_fast.kmbox_control.RawMouseMonitor")
    def test_sendinput_listens_to_the_physical_mouse_through_raw_input(
        self, monitor_class: Mock, client_class: Mock
    ) -> None:
        controller = KmboxController(KmboxConfig(uuid="00000000"), AimConfig(), output="sendinput")
        controller.connect()
        monitor_class.return_value.start.assert_called_once()
        on_move = monitor_class.call_args.args[0]
        on_move(5, -2)
        self.assertEqual(controller.take_hand_motion(), (5, -2))
        # 监听交给客户端管, 关的时候跟着一起关。
        self.assertIs(client_class.call_args.kwargs["monitor"], monitor_class.return_value)

    @patch("rhodes_fast.kmbox_control.LocalMouseClient")
    @patch("rhodes_fast.kmbox_control.RawMouseMonitor")
    def test_a_failed_raw_input_registration_still_moves_the_mouse(
        self, monitor_class: Mock, client_class: Mock
    ) -> None:
        """读不到手的移动只影响轨迹。为它拦下整条管线不值得, 但要说出来。"""
        monitor_class.return_value.start.side_effect = OSError(5, "denied")
        controller = KmboxController(KmboxConfig(uuid="00000000"), AimConfig(), output="sendinput")
        controller.connect()
        self.assertIs(controller._client, client_class.return_value)
        self.assertIsNone(client_class.call_args.kwargs["monitor"])
        self.assertTrue(any("Raw Input" in notice for notice in controller.algorithm_notices))

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
