"""移动输出选 SendInput 时, 控制器和管线的那几处接线。"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from rhodes_fast.config import AimConfig, AimProfileConfig, KmboxConfig, load_config
from rhodes_fast.detector import Detection
from rhodes_fast.kmbox_control import KmboxController
from rhodes_fast.pipeline import check_connections, output_label

_CONFIG = Path(__file__).parents[1] / "config.example.toml"


def _controller(**kmbox) -> KmboxController:
    return KmboxController(
        KmboxConfig(uuid="00000000", **kmbox),
        AimConfig(smoothing=1.0, deadzone=0),
        profiles=(AimProfileConfig(trigger="right"), AimProfileConfig(enabled=False, trigger="left")),
        output="sendinput",
    )


class SendInputControllerTest(unittest.TestCase):
    @patch("rhodes_fast.kmbox_control.RawMouseMonitor")
    @patch("rhodes_fast.kmbox_control.KMBoxClient")
    @patch("rhodes_fast.kmbox_control.LocalMouseClient")
    def test_connect_opens_the_local_mouse_even_with_the_box_switched_off(
        self, local_class: Mock, box_class: Mock, _monitor: Mock
    ) -> None:
        """两个设置互不干扰: kmbox.enabled 只管 KMBox。选了 SendInput 就是启用。"""
        controller = _controller(enabled=False)
        controller.connect()
        self.assertIs(controller._client, local_class.return_value)
        box_class.assert_not_called()

    def test_a_move_goes_through_move_not_enc_move(self) -> None:
        """kmbox.encrypted 默认是 true。拿它来挑方法的话, SendInput 这边会去调一个
        不存在的 enc_move —— Mock 不会报错, 真的客户端会 AttributeError 打挂主循环。"""
        controller = _controller(encrypted=True)
        client = Mock(spec=["move", "close", "isdown_right", "isdown_left"])
        controller._client = client
        moved = controller.move_toward(Detection(200, 140, 240, 180, 0.9, 0), 320, 320)
        self.assertNotEqual(moved, (0, 0))
        client.move.assert_called_once_with(*moved)

    def test_a_refused_send_is_recorded_as_nothing_sent(self) -> None:
        """SendInput 拒绝时 LocalMouseClient 抛 OSError。记成已发出的话,「扣在途」
        会减掉一段根本没发生的位移。"""
        controller = _controller()
        client = Mock(spec=["move", "close"])
        client.move.side_effect = OSError(5, "denied")
        controller._client = client
        moved = controller.move_toward(Detection(200, 140, 240, 180, 0.9, 0), 320, 320)
        self.assertEqual(moved, (0, 0))
        self.assertEqual(controller._motion_states[0].recent_commands[-1], (0, 0))

    def test_the_trigger_is_read_from_the_local_mouse(self) -> None:
        controller = _controller()
        client = Mock(spec=["move", "close", "isdown_right", "isdown_left"])
        client.isdown_right.return_value = True
        client.isdown_left.return_value = False
        controller._client = client
        self.assertTrue(controller.trigger_active())

    def test_the_kmbox_path_is_unchanged(self) -> None:
        controller = KmboxController(KmboxConfig(uuid="00000000"), AimConfig(smoothing=1.0, deadzone=0))
        client = Mock()
        controller._client = client
        controller.move_toward(Detection(200, 140, 240, 180, 0.9, 0), 320, 320)
        client.enc_move.assert_called_once()
        client.move.assert_not_called()


def _config(output: str, language: str = "zh"):
    config = load_config(_CONFIG, validate_model=False)
    return replace(
        config,
        ui=replace(config.ui, language=language),
        mouse=replace(config.mouse, output=output),
        kmbox=replace(config.kmbox, host="10.0.0.9", port=8810),
    )


class OutputBannerTest(unittest.TestCase):
    def test_the_banner_names_who_moves_the_mouse(self) -> None:
        self.assertEqual(output_label(_config("kmbox")), "KMBox 10.0.0.9:8810")
        self.assertEqual(output_label(_config("sendinput")), "SendInput")


class OutputCheckTest(unittest.TestCase):
    def _run(self, config) -> tuple[str, Exception | None]:
        source = Mock()
        source.error = None
        source.wait_next.return_value = SimpleNamespace(frame=np.zeros((320, 320, 3), dtype=np.uint8))
        printed = io.StringIO()
        raised = None
        with redirect_stdout(printed), patch("rhodes_fast.pipeline.create_source", return_value=source):
            try:
                check_connections(config)
            except RuntimeError as exc:
                raised = exc
        return printed.getvalue(), raised

    @patch("rhodes_fast.pipeline.RawMouseMonitor")
    @patch("rhodes_fast.pipeline.KmboxController")
    @patch("rhodes_fast.pipeline.check_local_mouse")
    def test_sendinput_is_checked_without_touching_the_box(
        self, check: Mock, controller: Mock, monitor: Mock
    ) -> None:
        output, raised = self._run(_config("sendinput"))
        self.assertIsNone(raised)
        self.assertIn("SendInput 正常", output)
        self.assertNotIn("KMBox", output)
        check.assert_called_once()
        controller.return_value.connect.assert_not_called()
        self.assertIn("Raw Input 正常", output)
        monitor.return_value.stop.assert_called_once()

    @patch("rhodes_fast.pipeline.RawMouseMonitor")
    @patch("rhodes_fast.pipeline.check_local_mouse")
    def test_a_raw_input_failure_is_only_a_warning(self, _check: Mock, monitor: Mock) -> None:
        """读不到手的移动只影响轨迹。把它算成测试失败的话, 单机用户的「测试输入」
        会因为一个次要功能一直是红的。"""
        monitor.return_value.start.side_effect = OSError("注册 Raw Input 失败")
        output, raised = self._run(_config("sendinput"))
        self.assertIsNone(raised)
        self.assertIn("!! Raw Input 注册失败：注册 Raw Input 失败", output)

    @patch("rhodes_fast.pipeline.RawMouseMonitor")
    @patch("rhodes_fast.pipeline.check_local_mouse", side_effect=OSError("没有 user32"))
    def test_a_sendinput_failure_is_reported(self, _check: Mock, _monitor: Mock) -> None:
        output, raised = self._run(_config("sendinput"))
        self.assertIn("SendInput 连接失败：没有 user32", output)
        self.assertIsNotNone(raised)

    @patch("rhodes_fast.pipeline.RawMouseMonitor")
    @patch("rhodes_fast.pipeline.check_local_mouse")
    def test_sendinput_in_english(self, _check: Mock, _monitor: Mock) -> None:
        output, _ = self._run(_config("sendinput", "en"))
        self.assertIn("SendInput OK", output)


if __name__ == "__main__":
    unittest.main()
