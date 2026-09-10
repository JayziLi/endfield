from __future__ import annotations

import csv
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from rhodes_fast.config import load_config
from rhodes_fast.pipeline import _latency_report, compare_latency_logs
from rhodes_fast.latency_log import (
    _FIELDS,
    _OPTIONAL_DEFAULTS,
    LatencySample,
    LatencyLogWriter,
    estimate_loop_delay,
    read_latency_log,
)


def _sample(**overrides) -> LatencySample:
    values = {
        "monotonic_ms": 0.0,
        "sequence": 1,
        "error_x": 0.0,
        "error_y": 0.0,
        "dx": 0,
        "dy": 0,
        "total_ms": 6.0,
        "assembly_ms": 1.0,
        "decode_ms": 1.5,
        "queue_ms": 0.5,
        "preprocess_ms": 0.4,
        "inference_ms": 1.3,
        "postprocess_ms": 0.2,
        "send_ms": 1.1,
        "trigger": True,
        "track_id": 1,
        "skipped": 0,
    }
    values.update(overrides)
    return LatencySample(**values)


def _synthetic(
    *,
    delay: int,
    frames: int = 500,
    frame_interval_ms: float = 8.0,
    total_ms: float = 6.0,
    target_velocity: float = 0.0,
    noise: float = 0.8,
    kp: float = 0.12,
    seed: int = 1,
) -> list[LatencySample]:
    """一次闭环追踪: 第 k 帧发出的 dx, 要到第 k+delay 帧才在画面里反映出来。"""
    rng = random.Random(seed)
    samples: list[LatencySample] = []
    commands: list[int] = []
    target = 120.0
    for k in range(frames):
        target += target_velocity
        visible = sum(commands[: max(0, k - delay + 1)])
        error = target - visible + (rng.gauss(0.0, noise) if noise else 0.0)
        dx = int(round(error * kp))
        commands.append(dx)
        samples.append(
            _sample(
                monotonic_ms=k * frame_interval_ms,
                sequence=k + 1,
                error_x=error,
                dx=dx,
                total_ms=total_ms,
            )
        )
    return samples


def _engagements(
    *,
    delay: int,
    frames: int = 3000,
    noise: float = 8.0,
    kp: float = 0.12,
    max_step: int = 30,
    seed: int = 1,
    engage: int = 90,
) -> list[LatencySample]:
    """反复交火: 每隔 engage 帧目标跳到新位置, 控制器拉枪过去。

    真实使用是一场接一场的交火, 不是只拉一次枪。拉枪那几帧的指令幅度远大于
    检测噪声, 延迟就是从这些帧里认出来的。
    """
    rng = random.Random(seed)
    samples: list[LatencySample] = []
    commands: list[int] = []
    target = rng.uniform(-150.0, 150.0)
    for k in range(frames):
        if k % engage == 0:
            target = rng.uniform(-180.0, 180.0)
        target += rng.gauss(0.0, 0.6)
        visible = sum(commands[: max(0, k - delay + 1)])
        error = target - visible + rng.gauss(0.0, noise)
        dx = max(-max_step, min(max_step, int(round(error * kp))))
        commands.append(dx)
        samples.append(
            _sample(monotonic_ms=k * 8.0, sequence=k + 1, error_x=error, dx=dx)
        )
    return samples



def _bursts(
    *,
    delay: int,
    bursts: int = 60,
    burst: int = 15,
    noise: float = 2.0,
    seed: int = 1,
) -> list[LatencySample]:
    """真实使用: 按住瞄准键打一小段就松开, 所以帧序号在段与段之间有大缺口。

    跨缺口的两行在画面上并不相邻, 拿它们配对算出来的滞后是假的。
    """
    rng = random.Random(seed)
    out: list[LatencySample] = []
    sequence = 0
    stamp = 0.0
    for index in range(bursts):
        segment = _engagements(
            delay=delay, frames=burst, noise=noise, seed=seed * 1000 + index, engage=burst
        )
        for sample in segment:
            sequence += 1
            stamp += 8.0
            out.append(replace(sample, sequence=sequence, monotonic_ms=stamp))
        gap = rng.randint(50, 400)
        sequence += gap
        stamp += gap * 8.0
    return out


class EstimateLoopDelayTests(unittest.TestCase):
    def test_recovers_known_loop_delay(self) -> None:
        estimate = estimate_loop_delay(_synthetic(delay=3))
        self.assertEqual(estimate.frames, 3)

    def test_recovers_loop_delay_across_the_plausible_range(self) -> None:
        for delay in (1, 2, 3, 4, 5):
            with self.subTest(delay=delay):
                estimate = estimate_loop_delay(_synthetic(delay=delay, seed=delay))
                self.assertEqual(estimate.frames, delay)

    def test_recovers_loop_delay_while_the_target_keeps_moving(self) -> None:
        estimate = estimate_loop_delay(_synthetic(delay=2, target_velocity=1.5, seed=9))
        self.assertEqual(estimate.frames, 2)

    def test_splits_the_loop_into_local_and_remote_segments(self) -> None:
        estimate = estimate_loop_delay(
            _synthetic(delay=3, frame_interval_ms=8.0, total_ms=6.0)
        )
        self.assertAlmostEqual(estimate.frame_interval_ms, 8.0, places=1)
        self.assertAlmostEqual(estimate.loop_ms, 24.0, places=1)
        self.assertAlmostEqual(estimate.ai_side_ms, 6.0, places=1)
        self.assertAlmostEqual(estimate.remote_ms, 18.0, places=1)

    def test_reports_confidence_high_enough_to_trust_the_peak(self) -> None:
        estimate = estimate_loop_delay(_synthetic(delay=3))
        self.assertGreater(estimate.correlation, 0.5)
        self.assertGreater(estimate.correlation, estimate.runner_up_correlation)

    def test_survives_heavy_detection_noise_by_leaning_on_flick_frames(self) -> None:
        estimate = estimate_loop_delay(_engagements(delay=3, noise=8.0))
        self.assertEqual(estimate.frames, 3)

    def test_survives_heavy_detection_noise_across_the_plausible_range(self) -> None:
        for delay in (1, 2, 3, 5):
            with self.subTest(delay=delay):
                estimate = estimate_loop_delay(
                    _engagements(delay=delay, noise=8.0, seed=delay)
                )
                self.assertEqual(estimate.frames, delay)

    def test_reports_which_command_threshold_the_estimate_leaned_on(self) -> None:
        estimate = estimate_loop_delay(_engagements(delay=3, noise=8.0))
        self.assertGreaterEqual(estimate.command_threshold, 6.0)
        self.assertGreaterEqual(estimate.pairs, 100)

    def test_falls_back_to_a_lower_threshold_when_flicks_are_scarce(self) -> None:
        estimate = estimate_loop_delay(_synthetic(delay=3, frames=500, noise=0.8))
        self.assertEqual(estimate.frames, 3)
        self.assertLess(estimate.command_threshold, 6.0)

    def test_ignores_pairs_that_straddle_a_gap_in_the_frame_sequence(self) -> None:
        estimate = estimate_loop_delay(_bursts(delay=5, burst=15, seed=5))
        self.assertEqual(estimate.frames, 5)

    def test_measures_the_frame_interval_from_consecutive_frames_only(self) -> None:
        estimate = estimate_loop_delay(_bursts(delay=3, burst=15, seed=3))
        self.assertAlmostEqual(estimate.frame_interval_ms, 8.0, places=1)

    def test_ignores_pairs_that_span_a_target_switch(self) -> None:
        samples = _engagements(delay=3, frames=900, seed=11)
        # 中途换了锁定目标: 误差会突跳, 但那跳变不是我们发的指令造成的
        switched = [
            replace(sample, track_id=1 if index < 450 else 2)
            for index, sample in enumerate(samples)
        ]
        self.assertEqual(estimate_loop_delay(switched).frames, 3)

    def test_ignores_frames_where_no_target_was_detected(self) -> None:
        samples = _engagements(delay=3, frames=900, seed=12)
        # 没有目标时误差没有意义, 这些帧必须完全不参与配对
        blinded = [
            replace(sample, track_id=0, error_x=999.0)
            if 300 <= index < 400
            else sample
            for index, sample in enumerate(samples)
        ]
        self.assertEqual(estimate_loop_delay(blinded).frames, 3)

    def test_pairs_a_flick_with_response_frames_logged_after_release(self) -> None:
        # 拉枪时按住瞄准键, 但响应要几帧后才到画面, 那时往往已经松开了。
        # 松开后的帧 dx=0, 可误差依然有效, 必须能拿来当响应用。
        samples = _engagements(delay=3, frames=900, seed=13)
        released = [
            replace(sample, trigger=False, dx=0) if abs(sample.dx) < 6 else sample
            for sample in samples
        ]
        self.assertEqual(estimate_loop_delay(released).frames, 3)

    def test_recovers_a_delay_larger_than_the_old_eight_frame_window(self) -> None:
        # 高帧率下回路延迟很容易超过 8 帧: 241fps 时 8 帧才 33 毫秒。
        estimate = estimate_loop_delay(_engagements(delay=12, frames=4000, seed=12))
        self.assertEqual(estimate.frames, 12)

    def test_pairs_counts_the_winning_lag_not_the_widest_one(self) -> None:
        # 滞后越大, 被序号缺口滤掉的配对越多。报"所有滞后里最多的那个"会虚高,
        # 让人以为结论比实际更有依据。
        samples = _bursts(delay=5, burst=15, seed=5)
        at_lag_one = estimate_loop_delay(samples, max_lag=1)
        estimate = estimate_loop_delay(samples)

        self.assertEqual(estimate.frames, 5)
        self.assertLess(estimate.pairs, at_lag_one.pairs)

    def test_returns_none_when_the_crosshair_never_moved(self) -> None:
        still = [_sample(monotonic_ms=k * 8.0, sequence=k + 1) for k in range(200)]
        self.assertIsNone(estimate_loop_delay(still))

    def test_returns_none_when_there_are_too_few_samples(self) -> None:
        self.assertIsNone(estimate_loop_delay(_synthetic(delay=2, frames=5)))


class LatencyLogWriterTests(unittest.TestCase):
    def test_written_rows_round_trip_back_through_the_csv(self) -> None:
        samples = _synthetic(delay=2, frames=40)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            writer = LatencyLogWriter(path)
            for sample in samples:
                writer.write(sample)
            writer.close()
            restored = read_latency_log(path)

        self.assertEqual(len(restored), len(samples))
        self.assertEqual(restored[0], samples[0])
        self.assertEqual(restored[-1], samples[-1])

    def test_a_written_log_can_be_analysed_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            writer = LatencyLogWriter(path)
            for sample in _synthetic(delay=4, seed=4):
                writer.write(sample)
            writer.close()
            estimate = estimate_loop_delay(read_latency_log(path))

        self.assertEqual(estimate.frames, 4)

    def test_rows_reach_disk_before_the_writer_is_closed(self) -> None:
        # 进程被强杀时缓冲区里的数据会全部丢失, 所以必须定期落盘。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            writer = LatencyLogWriter(path)
            for index in range(300):
                writer.write(_sample(sequence=index + 1, dx=7))

            recovered = read_latency_log(path)
            writer.close()

        self.assertGreaterEqual(len(recovered), 200)

    def test_reads_an_older_log_that_predates_the_trigger_columns(self) -> None:
        # 旧版本写出的 CSV 没有 trigger / track_id 两列, 仍然要能读回来分析。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.csv"
            samples = _engagements(delay=3, frames=900, noise=3.0, seed=21)
            legacy_fields = [
                name for name in _FIELDS if name not in _OPTIONAL_DEFAULTS
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                legacy = csv.writer(handle)
                legacy.writerow(legacy_fields)
                for sample in samples:
                    legacy.writerow([getattr(sample, name) for name in legacy_fields])

            restored = read_latency_log(path)

        self.assertEqual(len(restored), len(samples))
        self.assertEqual(estimate_loop_delay(restored).frames, 3)

    def test_close_is_safe_to_call_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            writer = LatencyLogWriter(path)
            writer.write(_sample())
            writer.close()
            writer.close()
            self.assertEqual(len(read_latency_log(path)), 1)


if __name__ == "__main__":
    unittest.main()


_CONFIG = Path(__file__).parents[1] / "config.example.toml"


def _write_log(path: Path, samples: list[LatencySample]) -> Path:
    writer = LatencyLogWriter(path)
    for sample in samples:
        writer.write(sample)
    writer.close()
    return path


class CompareLatencyLogsTests(unittest.TestCase):
    """做 OBS 采集方式 A/B 时要把几份日志并排看, 而不是跑一次读一次。"""

    def _row(self, report: str, name: str) -> str:
        matches = [line for line in report.splitlines() if name in line]
        self.assertEqual(len(matches), 1, f"{name} 应当恰好占一行, 实际 {len(matches)} 行")
        return matches[0]

    def test_each_log_gets_its_own_row_with_its_own_loop_delay(self) -> None:
        config = load_config(_CONFIG, validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            before = _write_log(
                Path(directory) / "before.csv", _engagements(delay=7, seed=7)
            )
            after = _write_log(
                Path(directory) / "after.csv", _engagements(delay=4, seed=4)
            )
            report = compare_latency_logs(config, [before, after])

        self.assertIn("7", self._row(report, "before.csv").split())
        self.assertIn("4", self._row(report, "after.csv").split())

    def test_a_log_without_enough_movement_is_flagged_not_crashed(self) -> None:
        config = load_config(_CONFIG, validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            still = _write_log(
                Path(directory) / "still.csv",
                [_sample(monotonic_ms=k * 8.0, sequence=k + 1) for k in range(200)],
            )
            report = compare_latency_logs(config, [still])

        self.assertIn("still.csv", report)

    def test_a_missing_file_is_reported_rather_than_raising(self) -> None:
        config = load_config(_CONFIG, validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            report = compare_latency_logs(config, [Path(directory) / "nope.csv"])
        self.assertIn("nope.csv", report)


class _StubWriter:
    """_latency_report 只用到 path 和 rows。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows = len(read_latency_log(path))


class LatencyReportTests(unittest.TestCase):
    def test_the_report_shows_the_runner_up_so_a_flat_peak_is_visible(self) -> None:
        # 真实数据里 7 帧和 8 帧的相关系数可能只差 0.003, 峰是平的。
        # 只报一个整数会让人以为精度比实际高。
        config = load_config(_CONFIG, validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_log(
                Path(directory) / "latency.csv", _engagements(delay=5, seed=5)
            )
            estimate = estimate_loop_delay(read_latency_log(path))
            report = _latency_report(config, _StubWriter(path))

        self.assertIn("次高", report)
        self.assertIn(str(estimate.runner_up_frames), report)
