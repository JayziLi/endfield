from __future__ import annotations

import socket
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from rhodes_fast.detector import Detection
from rhodes_fast.preview import PreviewPublisher, render_preview


class PreviewTests(unittest.TestCase):
    def test_render_preview_draws_detection_and_status(self) -> None:
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        detection = Detection(40, 20, 100, 90, 0.87, 2)

        rendered = render_preview(
            frame,
            [detection],
            detection,
            target_y_ratio=0.4,
            fov_radius=50,
            inference_ms=4.2,
            detection_ms=4.8,
        )

        self.assertEqual(rendered.shape, frame.shape)
        self.assertGreater(np.count_nonzero(rendered), 0)
        self.assertEqual(np.count_nonzero(frame), 0)

    def test_selected_target_label_shows_its_box_height(self) -> None:
        # The projectile predictor's reference box height is read off this label.
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        target = Detection(40, 20, 100, 90, 0.87, 2)
        other = Detection(110, 30, 150, 60, 0.5, 2)

        with patch("rhodes_fast.preview.cv2.putText", wraps=cv2.putText) as put_text:
            render_preview(
                frame,
                [target, other],
                target,
                target_y_ratio=0.4,
                fov_radius=50,
                inference_ms=4.2,
                detection_ms=4.8,
            )

        labels = [call.args[1] for call in put_text.call_args_list]
        self.assertIn("ID 2  0.87  h70", labels)
        self.assertIn("ID 2  0.50", labels)

    def test_publisher_sends_decodable_preview(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1.0)
        publisher = PreviewPublisher(receiver.getsockname()[1], max_fps=1000)
        try:
            publisher.publish(
                np.zeros((120, 160, 3), dtype=np.uint8),
                [],
                None,
                target_y_ratio=0.4,
                fov_radius=50,
                inference_ms=4.2,
                detection_ms=4.8,
            )
            payload, _address = receiver.recvfrom(65_507)
            decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        finally:
            publisher.close()
            receiver.close()
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.shape, (120, 160, 3))

    def test_due_reports_whether_the_next_frame_will_actually_render(self) -> None:
        # 主循环靠这个提前知道该不该为预览做更重的全类别检测。
        # 预览只有 20fps, 不该让每一帧都付出那份代价。
        frame = np.zeros((80, 80, 3), dtype=np.uint8)
        publisher = PreviewPublisher(9, max_fps=20.0)

        self.assertTrue(publisher.due)
        publisher.publish(
            frame, [], None, target_y_ratio=0.4, fov_radius=30,
            inference_ms=1.0, detection_ms=1.0,
        )
        self.assertFalse(publisher.due)

    def test_due_is_false_while_the_preview_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enabled_file = Path(directory) / "on"
            publisher = PreviewPublisher(9, enabled_file=enabled_file)
            self.assertFalse(publisher.due)
            enabled_file.touch()
            self.assertTrue(publisher.due)

    def test_publisher_skips_all_rendering_while_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enabled_file = Path(directory) / "preview.enabled"
            publisher = PreviewPublisher(9, enabled_file=enabled_file)
            try:
                with patch("rhodes_fast.preview.render_preview") as render:
                    publisher.publish(
                        np.zeros((120, 160, 3), dtype=np.uint8),
                        [],
                        None,
                        target_y_ratio=0.4,
                        fov_radius=50,
                        inference_ms=4.2,
                        detection_ms=4.8,
                    )
                render.assert_not_called()
            finally:
                publisher.close()


class PreviewOffloadTests(unittest.TestCase):
    """预览的渲染和编码必须离开主循环。

    主循环每 4.16 毫秒要处理一帧并发出鼠标指令; 渲染一次预览实测 0.78 毫秒
    (p95 0.98), 同步做就等于每 50 毫秒给瞄准回路插一根刺。
    """

    def _publisher(self, **kwargs) -> PreviewPublisher:
        publisher = PreviewPublisher(9, **kwargs)
        self.addCleanup(publisher.close)
        return publisher

    def test_publish_returns_without_waiting_for_the_render(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_render(*args, **kwargs):
            started.set()
            release.wait(5.0)
            return np.zeros((80, 80, 3), dtype=np.uint8)

        publisher = self._publisher(max_fps=1000)
        with patch("rhodes_fast.preview.render_preview", side_effect=blocking_render):
            begin = time.perf_counter()
            publisher.publish(
                np.zeros((80, 80, 3), dtype=np.uint8), [], None,
                target_y_ratio=0.4, fov_radius=30, inference_ms=1.0, detection_ms=1.0,
            )
            elapsed = time.perf_counter() - begin
            self.assertTrue(started.wait(5.0), "worker never picked the frame up")
            release.set()
        self.assertLess(elapsed, 0.05, f"publish blocked the aim loop for {elapsed * 1000:.1f} ms")

    def test_a_backed_up_worker_drops_stale_frames_instead_of_queueing(self) -> None:
        # 预览慢下来时要丢掉过期的帧, 而不是让主循环排队等它。
        rendered: list[float] = []
        first = threading.Event()
        release = threading.Event()

        def blocking_render(frame, *args, **kwargs):
            rendered.append(float(frame[0, 0, 0]))
            if not first.is_set():
                first.set()
                release.wait(5.0)
            return np.zeros((80, 80, 3), dtype=np.uint8)

        publisher = self._publisher(max_fps=1000)
        publisher.interval = 0.0  # 关掉限流闸门, 这里测的是丢帧不是帧率
        with patch("rhodes_fast.preview.render_preview", side_effect=blocking_render):
            for marker in (1, 2, 3):
                frame = np.full((80, 80, 3), marker, dtype=np.uint8)
                publisher.publish(
                    frame, [], None,
                    target_y_ratio=0.4, fov_radius=30, inference_ms=1.0, detection_ms=1.0,
                )
                if marker == 1:
                    self.assertTrue(first.wait(5.0))
            release.set()
            deadline = time.perf_counter() + 5.0
            while len(rendered) < 2 and time.perf_counter() < deadline:
                time.sleep(0.005)
        self.assertEqual(rendered, [1.0, 3.0], "the stale middle frame should have been dropped")

    def test_close_stops_the_worker_thread(self) -> None:
        publisher = self._publisher(max_fps=1000)
        publisher.publish(
            np.zeros((80, 80, 3), dtype=np.uint8), [], None,
            target_y_ratio=0.4, fov_radius=30, inference_ms=1.0, detection_ms=1.0,
        )
        self.assertTrue(publisher.is_rendering, "publish should have started the worker")
        publisher.close()
        self.assertFalse(publisher.is_rendering)

    def test_default_preview_rate_is_smooth_enough_to_watch(self) -> None:
        # 20fps 的画面在 GUI 里看着一顿一顿的; 渲染既然已经不在主循环上,
        # 提高帧率只花工作线程的时间。
        self.assertGreaterEqual(self._publisher().interval ** -1, 30.0)
