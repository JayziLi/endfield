from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from rhodes_fast.config import load_config
from rhodes_fast.detector import Detection
from rhodes_fast.latency_log import LatencySample, read_latency_log
from rhodes_fast.pipeline import run_pipeline

_CONFIG = Path(__file__).parents[1] / "config.example.toml"


class _Harness:
    """驱动 run_pipeline 跑指定的几帧, 记录主循环里各步骤的先后顺序。"""

    def __init__(self, sequences: list[int]) -> None:
        self.sequences = sequences
        self.order: list[str] = []

    def run(self, *, preview_enabled: bool = True, latency_log: Path | None = None) -> None:
        frame = np.zeros((320, 320, 3), dtype=np.uint8)

        detector = Mock()
        detector.detect.return_value = [Detection(150, 150, 170, 170, 0.9, 0)]
        detector.last_inference_ms = 1.0
        detector.last_detection_ms = 1.2
        detector.last_preprocess_ms = 0.0
        detector.last_postprocess_ms = 0.2

        controller = Mock()
        controller.trigger_active.return_value = True
        controller.target_class = 0
        controller.target_y_ratio = 0.5
        controller.fov_radius = 150.0
        controller.last_send_ms = 0.2
        controller.active_profile_number = 1
        controller.move_toward.side_effect = lambda *_a, **_k: (
            self.order.append("move"),
            (3, 0),
        )[1]

        preview = Mock()
        preview.enabled = preview_enabled
        preview.due = preview_enabled
        preview.publish.side_effect = lambda *_a, **_k: self.order.append("preview")

        pending = list(self.sequences)

        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop"

            def wait_next(_after, timeout=0.25):
                if not pending:
                    stop_file.touch()
                    return None
                now = time.perf_counter()
                return SimpleNamespace(
                    sequence=pending.pop(0),
                    first_packet_at=now,
                    ready_at=now,
                    frame=frame,
                    assembly_ms=1.0,
                    decode_ms=0.5,
                )

            source = Mock()
            source.fps = 240.0
            source.error = None
            source.wait_next.side_effect = wait_next

            config = load_config(_CONFIG, validate_model=False)
            with (
                patch("rhodes_fast.pipeline.YoloDetector", return_value=detector),
                patch("rhodes_fast.pipeline.KmboxController", return_value=controller),
                patch("rhodes_fast.pipeline.create_source", return_value=source),
                patch("rhodes_fast.pipeline.PreviewPublisher", return_value=preview),
            ):
                run_pipeline(
                    config,
                    stop_file=stop_file,
                    preview_port=54321,
                    latency_log=latency_log,
                )


class PipelineLoopTests(unittest.TestCase):
    def _order(self, **kwargs) -> list[str]:
        harness = _Harness([1])
        harness.run(**kwargs)
        return harness.order

    def _samples(self, sequences: list[int]) -> list[LatencySample]:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "latency.csv"
            _Harness(sequences).run(latency_log=log)
            return read_latency_log(log)

    def test_mouse_command_is_sent_before_the_preview_is_rendered(self) -> None:
        # 预览渲染做两次全帧拷贝 + addWeighted + 最多三次 JPEG 编码, 同步阻塞。
        # 它夹在检测和发指令之间的话, 每一帧的鼠标指令都被白白推迟。
        self.assertEqual(self._order(), ["move", "preview"])

    def test_the_frame_is_still_previewed_when_the_preview_tab_is_open(self) -> None:
        self.assertIn("preview", self._order())

    def test_records_how_many_frames_the_loop_skipped(self) -> None:
        # 主循环跟不上时 wait_next 直接跳到最新帧, 中间那些帧被静默丢掉。
        # 生产运行时完全看不到丢了多少, 只有 pipeline_benchmark 统计过。
        self.assertEqual([sample.skipped for sample in self._samples([1, 5, 6])], [0, 3, 0])

    def test_the_first_frame_never_counts_as_a_skip(self) -> None:
        # 管线可能在画面流跑了一阵之后才启动, 首帧序号本来就不是 1。
        self.assertEqual([sample.skipped for sample in self._samples([50, 51])], [0, 0])


if __name__ == "__main__":
    unittest.main()
