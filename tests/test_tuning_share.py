from __future__ import annotations

import json
import unittest

from rhodes_fast.config import AimProfileConfig
from rhodes_fast.tuning_share import (
    TuningError,
    apply_tuning,
    delay_warning,
    dump_tuning,
    load_tuning,
)


def _profile() -> AimProfileConfig:
    return AimProfileConfig(
        enabled=True,
        trigger="side1",
        kp_min=0.14,
        kp_max=0.35,
        kp_growth=0.031,
        target_class=3,
        target_y_ratio=0.08,
        fov_radius=150.0,
        algorithm="inflight_ff",
        algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25},
    )


class DumpTests(unittest.TestCase):
    def test_round_trip_preserves_every_feel_setting(self) -> None:
        tuning = load_tuning(dump_tuning(_profile()))
        self.assertEqual(tuning.algorithm, "inflight_ff")
        self.assertEqual(tuning.params["loop_delay_frames"], 8.0)
        self.assertAlmostEqual(tuning.kp_max, 0.35)
        self.assertAlmostEqual(tuning.kp_growth, 0.031)
        self.assertAlmostEqual(tuning.target_y_ratio, 0.08)
        self.assertAlmostEqual(tuning.fov_radius, 150.0)

    def test_trigger_and_target_class_are_left_out_on_purpose(self) -> None:
        # 按键习惯因人而异, 目标标签取决于用哪个模型。
        payload = json.loads(dump_tuning(_profile()))
        self.assertNotIn("trigger", payload)
        self.assertNotIn("target_class", payload)
        self.assertNotIn("enabled", payload)

    def test_measured_delay_is_included_only_when_known(self) -> None:
        self.assertNotIn("measured_loop_ms", json.loads(dump_tuning(_profile())))
        payload = json.loads(dump_tuning(_profile(), measured_loop_ms=33.3))
        self.assertAlmostEqual(payload["measured_loop_ms"], 33.3)


class LoadTests(unittest.TestCase):
    def test_unknown_format_version_is_an_error_not_a_guess(self) -> None:
        text = json.dumps({"format": 99, "algorithm": "p", "params": {}})
        with self.assertRaises(TuningError) as caught:
            load_tuning(text)
        self.assertIn("99", str(caught.exception))

    def test_missing_algorithm_is_named_in_the_error(self) -> None:
        with self.assertRaises(TuningError) as caught:
            load_tuning(dump_tuning(_profile()), known_algorithms={"p", "pd"})
        self.assertIn("inflight_ff", str(caught.exception))

    def test_a_known_algorithm_passes_the_same_check(self) -> None:
        tuning = load_tuning(dump_tuning(_profile()), known_algorithms={"p", "inflight_ff"})
        self.assertEqual(tuning.algorithm, "inflight_ff")

    def test_garbage_text_is_an_error_not_a_crash(self) -> None:
        with self.assertRaises(TuningError):
            load_tuning("this is not json")

    def test_a_missing_field_is_named(self) -> None:
        payload = json.loads(dump_tuning(_profile()))
        del payload["kp_growth"]
        with self.assertRaises(TuningError) as caught:
            load_tuning(json.dumps(payload))
        self.assertIn("kp_growth", str(caught.exception))

    def test_a_non_numeric_value_is_rejected(self) -> None:
        payload = json.loads(dump_tuning(_profile()))
        payload["kp_max"] = "很大"
        with self.assertRaises(TuningError):
            load_tuning(json.dumps(payload))


class ApplyTests(unittest.TestCase):
    def test_apply_overwrites_feel_but_keeps_local_habits(self) -> None:
        mine = AimProfileConfig(
            enabled=False, trigger="right", target_class=0, algorithm="p", algorithm_params={}
        )
        updated = apply_tuning(mine, load_tuning(dump_tuning(_profile())))
        self.assertEqual(updated.algorithm, "inflight_ff")
        self.assertAlmostEqual(updated.kp_max, 0.35)
        # 触发键、目标标签、启用状态是本机的事, 别人的文件不该动它们。
        self.assertEqual(updated.trigger, "right")
        self.assertEqual(updated.target_class, 0)
        self.assertFalse(updated.enabled)


class DelayWarningTests(unittest.TestCase):
    def test_no_warning_when_the_two_machines_are_within_a_frame(self) -> None:
        self.assertIsNone(delay_warning(33.3, 31.0))

    def test_no_warning_when_either_side_is_unknown(self) -> None:
        self.assertIsNone(delay_warning(None, 31.0))
        self.assertIsNone(delay_warning(33.3, None))

    def test_a_gap_over_a_frame_names_both_numbers(self) -> None:
        message = delay_warning(33.3, 16.0)
        self.assertIsNotNone(message)
        self.assertIn("33.3", message)
        self.assertIn("16.0", message)

    def test_the_warning_fires_in_both_directions(self) -> None:
        self.assertIsNotNone(delay_warning(16.0, 33.3))


if __name__ == "__main__":
    unittest.main()
