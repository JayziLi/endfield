"""管线里跟「本机屏幕」这种画面来源有关的几处接线。"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from rhodes_fast.config import load_config
from rhodes_fast.desktop_source import DesktopSource
from rhodes_fast.gui_web.lamps import lamp_updates
from rhodes_fast.pipeline import check_connections, create_source, input_frame_size, source_label

_CONFIG = Path(__file__).parents[1] / "config.example.toml"


def _desktop_config(language: str = "zh"):
    config = load_config(_CONFIG, validate_model=False)
    return replace(
        config,
        input=replace(config.input, mode="desktop"),
        ui=replace(config.ui, language=language),
        desktop=replace(config.desktop, backend="dxgi", monitor=1, width=288, height=256),
        kmbox=replace(config.kmbox, enabled=False),
    )


class DesktopWiringTest(unittest.TestCase):
    def test_the_desktop_mode_gets_a_desktop_source(self) -> None:
        source = create_source(_desktop_config())
        self.assertIsInstance(source, DesktopSource)
        self.assertEqual(source.config, _desktop_config().desktop)

    def test_the_banner_names_the_backend_monitor_and_size(self) -> None:
        self.assertEqual(source_label(_desktop_config("zh")), "本机屏幕 DXGI · 显示器 1 · 288x256")
        self.assertEqual(source_label(_desktop_config("en")), "Local screen DXGI · monitor 1 · 288x256")

    def test_the_frame_size_comes_from_the_desktop_section(self) -> None:
        """测速报告里记的画面尺寸。按 UDP 那一节记的话, 报告里写着 320x320,
        实际抓的是另一个尺寸 —— 对比两次测速时会得出错的结论。"""
        self.assertEqual(input_frame_size(_desktop_config()), (288, 256))
        config = load_config(_CONFIG, validate_model=False)
        self.assertEqual(input_frame_size(config), (config.udp.width, config.udp.height))
        obs = replace(config, input=replace(config.input, mode="obs_websocket"))
        self.assertEqual(input_frame_size(obs), (config.obs.width, config.obs.height))


class DesktopCheckTest(unittest.TestCase):
    """「测试输入」对本机屏幕说的话。显示器分辨率要一起打出来: 用户靠它确认选中的
    是不是想要的那块屏。"""

    def _run(self, source, language: str = "zh") -> tuple[str, Exception | None]:
        printed = io.StringIO()
        raised = None
        with redirect_stdout(printed), patch("rhodes_fast.pipeline.create_source", return_value=source):
            try:
                check_connections(_desktop_config(language))
            except RuntimeError as exc:
                raised = exc
        return printed.getvalue(), raised

    def _source(self, *, frame=True, error=None):
        source = Mock()
        source.error = error
        source.screen_size = (2560, 1440)
        source.wait_next.return_value = (
            SimpleNamespace(frame=np.zeros((256, 288, 3), dtype=np.uint8)) if frame else None
        )
        return source

    def test_success_names_frame_screen_and_backend(self) -> None:
        output, raised = self._run(self._source())
        self.assertIsNone(raised)
        self.assertIn("本机屏幕 正常：画面=288x256 · 显示器=2560x1440 · 后端=dxgi", output)
        self.assertNotIn("UDP", output)

    def test_success_in_english(self) -> None:
        output, _ = self._run(self._source(), "en")
        self.assertIn("Desktop OK: frame=288x256 · screen=2560x1440 · backend=dxgi", output)

    def test_failure_says_what_went_wrong(self) -> None:
        output, raised = self._run(self._source(frame=False, error="本机屏幕采集需要 dxcam"))
        self.assertIn("本机屏幕 连接失败：本机屏幕采集需要 dxcam", output)
        self.assertIsNotNone(raised)

    def test_the_lamp_understands_both_answers(self) -> None:
        """「测试输入」这两句话要能点亮 / 点红视频流那盏灯, 否则按了测试灯不动。"""
        for line, expected in (
            ("本机屏幕 正常：画面=288x256 · 显示器=2560x1440 · 后端=dxgi", "online"),
            ("Desktop OK: frame=288x256 · screen=2560x1440 · backend=dxgi", "online"),
            ("本机屏幕 连接失败：本机屏幕采集需要 dxcam", "error"),
            ("Desktop FAILED: no frame", "error"),
        ):
            with self.subTest(line=line):
                self.assertEqual(lamp_updates(line, output_enabled=True)["stream"]["state"], expected)
        ok = lamp_updates("本机屏幕 正常：画面=288x256 · 显示器=2560x1440 · 后端=dxgi", output_enabled=True)
        self.assertEqual(ok["stream"]["text"], "288x256")


if __name__ == "__main__":
    unittest.main()
