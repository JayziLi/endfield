from __future__ import annotations

import socket
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
