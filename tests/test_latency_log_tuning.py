from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from rhodes_fast.config import AimProfileConfig
from rhodes_fast.latency_log import LatencyLogWriter, LatencySample, read_latency_log

_BASE = dict(
    monotonic_ms=0.0,
    sequence=1,
    error_x=10.0,
    error_y=2.0,
    dx=1,
    dy=0,
    total_ms=4.0,
    assembly_ms=1.0,
    decode_ms=0.5,
    queue_ms=0.1,
    preprocess_ms=0.0,
    inference_ms=1.8,
    postprocess_ms=0.3,
    send_ms=0.3,
    trigger=True,
    track_id=1,
    skipped=0,
)


class TuningColumnsTests(unittest.TestCase):
    """日志得自己说清楚是哪套设置跑出来的。

    没有这几列, 两份日志根本没法比 —— 只能靠记性去猜哪份是什么算法什么增益跑的,
    而任何 A/B 的前提就是知道两边各是什么。
    """

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.path = Path(self._folder.name) / "latency.csv"

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _round_trip(self, **overrides) -> LatencySample:
        writer = LatencyLogWriter(self.path)
        writer.write(LatencySample(**{**_BASE, **overrides}))
        writer.close()
        rows = read_latency_log(self.path)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_row_carries_the_algorithm_name(self) -> None:
        row = self._round_trip(algorithm="feedforward")
        self.assertEqual(row.algorithm, "feedforward")

    def test_the_row_carries_the_algorithm_parameters(self) -> None:
        params = json.dumps({"gain": 1.0, "loop_delay_frames": 8.0}, sort_keys=True)
        row = self._round_trip(algorithm="feedforward", algorithm_params=params)
        self.assertEqual(json.loads(row.algorithm_params)["loop_delay_frames"], 8.0)

    def test_the_row_carries_the_three_gains(self) -> None:
        row = self._round_trip(kp_min=0.023, kp_max=0.083, kp_growth=0.031)
        self.assertAlmostEqual(row.kp_min, 0.023)
        self.assertAlmostEqual(row.kp_max, 0.083)
        self.assertAlmostEqual(row.kp_growth, 0.031)

    def test_the_algorithm_columns_survive_as_text_not_numbers(self) -> None:
        # 读取时缺省按 float 转换。算法名不登记成 str 的话, 读一份新日志就当场崩。
        row = self._round_trip(algorithm="inflight_ff", algorithm_params="{}")
        self.assertIsInstance(row.algorithm, str)
        self.assertIsInstance(row.algorithm_params, str)


class OldLogTests(unittest.TestCase):
    """那份 18MB 的旧日志必须还能读。"""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.path = Path(self._folder.name) / "old.csv"
        old_columns = [
            name
            for name in (field.name for field in fields(LatencySample))
            if name not in {"algorithm", "algorithm_params", "kp_min", "kp_max", "kp_growth"}
        ]
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(old_columns)
            writer.writerow([_BASE[name] for name in old_columns])

    def tearDown(self) -> None:
        self._folder.cleanup()

    def test_a_log_written_before_these_columns_existed_still_reads(self) -> None:
        rows = read_latency_log(self.path)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].error_x, 10.0)

    def test_the_missing_algorithm_reads_as_empty_meaning_unrecorded(self) -> None:
        # 空字符串是「没记」的信号。填个 "p" 就是撒谎 —— 我们并不知道它跑的是什么。
        self.assertEqual(read_latency_log(self.path)[0].algorithm, "")


class ActiveProfileTests(unittest.TestCase):
    """记的必须是*当时生效的那个方案*, 不是方案 1。"""

    def test_the_controller_exposes_the_profile_in_effect(self) -> None:
        from unittest.mock import Mock

        from rhodes_fast.config import AimConfig, KmboxConfig
        from rhodes_fast.kmbox_control import KmboxController

        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "aim.json"
            runtime.write_text('{"profiles": []}', encoding="utf-8")
            first = AimProfileConfig(trigger="side1", algorithm="p")
            second = replace(
                first,
                trigger="left",
                algorithm="feedforward",
                algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0},
                kp_max=0.125,
            )
            controller = KmboxController(
                KmboxConfig(uuid="00000000"),
                AimConfig(),
                runtime,
                profiles=(first, second),
            )
            client = Mock()
            client.isdown_side1.return_value = 0
            client.isdown_left.return_value = 1
            controller._client = client
            controller.trigger_active()
            self.assertEqual(controller.active_profile.algorithm, "feedforward")
            self.assertAlmostEqual(controller.active_profile.kp_max, 0.125)


class PipelineWritesTuningTests(unittest.TestCase):
    """管线真的把当前设置写进每一行了吗。

    前面那些测试只证明 LatencySample 装得下这几列。装得下但没人往里填, 是这类
    改动最常见的坏法 —— 而且日志看起来完全正常, 只是那几列永远是空的。
    """

    def _samples(self, profiles):
        from tests.test_pipeline_loop import _Harness

        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "latency.csv"
            _Harness([1, 2], profiles=profiles).run(latency_log=log)
            return read_latency_log(log)

    def test_the_running_algorithm_lands_in_every_row(self) -> None:
        profile = AimProfileConfig(
            algorithm="feedforward",
            algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0},
            kp_min=0.023,
            kp_max=0.083,
            kp_growth=0.031,
        )
        rows = self._samples([profile])
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.algorithm, "feedforward")
            self.assertAlmostEqual(row.kp_max, 0.083)
            self.assertEqual(json.loads(row.algorithm_params)["gain"], 1.0)

    def test_the_parameter_text_is_stable_so_two_logs_can_be_grouped(self) -> None:
        # 同一套参数在两份日志里必须长得一样, 否则分组时会被当成两套设置。
        forwards = AimProfileConfig(
            algorithm="feedforward", algorithm_params={"gain": 1.0, "loop_delay_frames": 8.0}
        )
        backwards = replace(
            forwards, algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0}
        )
        self.assertEqual(
            self._samples([forwards])[0].algorithm_params,
            self._samples([backwards])[0].algorithm_params,
        )

    def test_a_mid_run_switch_shows_up_row_by_row(self) -> None:
        # 算法现在可以在跑的过程中热切换。存一份开局快照的话, 切换之后的每一行
        # 都会标错设置 —— 而那恰恰是你最想拿来对比的那几行。
        before = AimProfileConfig(algorithm="p")
        after = AimProfileConfig(algorithm="inflight_ff", kp_max=0.35)
        rows = self._samples([before, after])
        self.assertEqual([row.algorithm for row in rows], ["p", "inflight_ff"])
        self.assertAlmostEqual(rows[1].kp_max, 0.35)


if __name__ == "__main__":
    unittest.main()
