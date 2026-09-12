from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhodes_fast.latency_log import (
    LoopDelayEstimate,
    load_measurement,
    save_measurement,
)


def _estimate(loop_ms: float) -> LoopDelayEstimate:
    return LoopDelayEstimate(
        frames=8,
        correlation=0.71,
        runner_up_frames=7,
        runner_up_correlation=0.70,
        frame_interval_ms=4.16,
        loop_ms=loop_ms,
        ai_side_ms=4.4,
        remote_ms=22.6,
        samples=80405,
        command_threshold=3.0,
        pairs=1204,
    )


class MeasurementTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".loop-latency.json"
            save_measurement(path, _estimate(33.3))
            self.assertAlmostEqual(load_measurement(path), 33.3, places=3)

    def test_missing_file_reads_as_no_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(load_measurement(Path(folder) / "nope.json"))

    def test_corrupt_file_reads_as_no_measurement_rather_than_raising(self) -> None:
        # 这个文件只是个便利, 坏了不该让导出功能整个失败。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".loop-latency.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_measurement(path))

    def test_a_later_run_replaces_the_earlier_number(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".loop-latency.json"
            save_measurement(path, _estimate(33.3))
            save_measurement(path, _estimate(25.0))
            self.assertAlmostEqual(load_measurement(path), 25.0, places=3)


if __name__ == "__main__":
    unittest.main()
