from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from rhodes_fast.config import (
    AimConfig,
    AppConfig,
    InputConfig,
    KmboxConfig,
    ModelConfig,
    ObsConfig,
    UdpConfig,
    UiConfig,
)
from rhodes_fast.obs_source import FrameSnapshot
from rhodes_fast.pipeline_benchmark import PipelineTiming, run_pipeline_benchmark, summarize_timings


class PipelineBenchmarkTests(unittest.TestCase):
    def test_summarizes_every_pipeline_stage(self) -> None:
        samples = [
            PipelineTiming(10, 1, 2, 1, 1, 3, 1, 1),
            PipelineTiming(20, 2, 4, 2, 2, 6, 2, 2),
            PipelineTiming(30, 3, 6, 3, 3, 9, 3, 3),
            PipelineTiming(40, 4, 8, 4, 4, 12, 4, 4),
        ]

        summary = summarize_timings(samples)

        self.assertEqual(
            set(summary),
            {"total", "assembly", "decode", "queue", "preprocess", "inference", "postprocess", "target"},
        )
        self.assertEqual(summary["total"]["mean_ms"], 25.0)
        self.assertEqual(summary["total"]["p50_ms"], 25.0)
        self.assertAlmostEqual(summary["total"]["p95_ms"], 38.5)
        self.assertAlmostEqual(summary["total"]["p99_ms"], 39.7)
        self.assertEqual(summary["total"]["max_ms"], 40.0)

    def test_runs_dry_pipeline_and_saves_machine_readable_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.onnx"
            model.write_bytes(b"model")
            output = root / "report.json"
            config = AppConfig(
                input=InputConfig(mode="udp_jpeg"),
                ui=UiConfig(language="en"),
                udp=UdpConfig(host="127.0.0.1", port=4455, width=320, height=320),
                obs=ObsConfig(host="127.0.0.1"),
                model=ModelConfig(path=model, provider="cpu", cuda_graph=False, gpu_preprocess=False),
                kmbox=KmboxConfig(enabled=True),
                aim=AimConfig(target_class=2),
            )
            source = _FakeSource()
            detector = _FakeDetector()
            with (
                patch("rhodes_fast.pipeline_benchmark.create_source", return_value=source),
                patch("rhodes_fast.pipeline_benchmark.YoloDetector", return_value=detector),
                patch(
                    "rhodes_fast.pipeline_benchmark._environment_info",
                    return_value={},
                ),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    saved = run_pipeline_benchmark(config, 2, output_path=output, warmup_frames=0)

            assert saved is not None
            self.assertTrue(saved.samefile(output))
            self.assertTrue(source.stopped)
            self.assertEqual(detector.target_classes, [2, 2])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["benchmark"]["samples"], 2)
            self.assertFalse(report["benchmark"]["preview_enabled"])
            self.assertFalse(report["benchmark"]["kmbox_enabled"])
            self.assertEqual(report["summary"]["inference"]["mean_ms"], 0.3)


class _FakeSession:
    @staticmethod
    def get_providers() -> list[str]:
        return ["CPUExecutionProvider"]


class _FakeDetector:
    input_width = 320
    input_height = 320
    cuda_graph_enabled = False
    gpu_preprocess_enabled = False
    last_preprocess_ms = 0.1
    last_inference_ms = 0.3
    last_postprocess_ms = 0.2
    session = _FakeSession()

    def __init__(self) -> None:
        self.target_classes: list[int | None] = []

    @staticmethod
    def warmup() -> None:
        pass

    def detect(self, _frame: np.ndarray, target_class: int | None = None) -> list:
        self.target_classes.append(target_class)
        return []


class _FakeSource:
    fps = 240.0
    error = None

    def __init__(self) -> None:
        self.sequence = 0
        self.stopped = False

    @staticmethod
    def start() -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    def wait_next(self, _after_sequence: int, timeout: float = 3.0) -> FrameSnapshot:
        del timeout
        self.sequence += 1
        now = time.perf_counter()
        return FrameSnapshot(
            sequence=self.sequence,
            first_packet_at=now - 0.001,
            ready_at=now - 0.0005,
            frame=np.zeros((320, 320, 3), dtype=np.uint8),
            assembly_ms=0.1,
            decode_ms=0.2,
        )


if __name__ == "__main__":
    unittest.main()
