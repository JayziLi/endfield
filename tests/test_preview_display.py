from __future__ import annotations

import unittest

import numpy as np

from rhodes_fast.gui import PREVIEW_POLL_MS, fit_preview_to_canvas
from rhodes_fast.preview import PreviewPublisher


class PreviewDisplayTests(unittest.TestCase):
    def test_gui_polls_well_ahead_of_the_publish_rate(self) -> None:
        """取帧比发帧慢就一定会卡。

        原来管线 20fps 发, GUI 却挂在 80 毫秒的消息泵上取(12.5Hz): 实际只显示
        12.5fps, 而且画面步长在 50 和 100 毫秒之间来回跳——这就是"卡卡的"。
        取帧至少要比发帧快一倍, 每一帧才都能赶上自己那一拍。
        """
        publisher = PreviewPublisher(9)
        self.addCleanup(publisher.close)
        publish_ms = publisher.interval * 1000.0
        self.assertLessEqual(
            PREVIEW_POLL_MS,
            publish_ms / 2.0,
            f"GUI 每 {PREVIEW_POLL_MS} 毫秒取一次, 管线每 {publish_ms:.1f} 毫秒发一帧",
        )

    def test_fit_preview_scales_into_the_canvas_and_keeps_the_shape(self) -> None:
        frame = np.zeros((320, 320, 3), dtype=np.uint8)
        fitted = fit_preview_to_canvas(frame, 640, 400)
        self.assertEqual(fitted.shape, (400, 400, 3))

    def test_fit_preview_converts_bgr_to_rgb(self) -> None:
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        frame[:, :, 0] = 255  # OpenCV 里这是蓝色通道
        fitted = fit_preview_to_canvas(frame, 8, 8)
        self.assertEqual(tuple(int(value) for value in fitted[0, 0]), (0, 0, 255))

    def test_fit_preview_leaves_a_matching_canvas_untouched(self) -> None:
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        self.assertEqual(fit_preview_to_canvas(frame, 100, 100).shape, (100, 100, 3))
