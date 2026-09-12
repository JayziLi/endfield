from __future__ import annotations

import pathlib
import unittest

from rhodes_fast.config import AimProfileConfig
from rhodes_fast.gui import RhodesFastGui
from rhodes_fast.tuning_share import dump_preset, load_preset


class TuningFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()

    def tearDown(self) -> None:
        self.app.root.destroy()

    def _preset(self):
        return load_preset(
            dump_preset(
                AimProfileConfig(
                    kp_min=0.14,
                    kp_max=0.35,
                    kp_growth=0.031,
                    target_y_ratio=0.08,
                    fov_radius=123.0,
                    algorithm="inflight_ff",
                    algorithm_params={
                        "loop_delay_frames": 8.0,
                        "gain": 1.0,
                        "velocity_smoothing": 0.25,
                    },
                )
            )
        )

    def test_applying_a_preset_lands_in_the_saved_config(self) -> None:
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        profile = self.app._read_form().aim_profiles[1]
        self.assertEqual(profile.algorithm, "inflight_ff")
        self.assertEqual(profile.algorithm_params["loop_delay_frames"], 8.0)
        self.assertAlmostEqual(profile.kp_max, 0.35, places=3)
        self.assertAlmostEqual(profile.kp_growth, 0.031, places=3)
        # 百分比滑块容易搞反: 0.08 的比例对应 8%。
        self.assertAlmostEqual(profile.target_y_ratio, 0.08, places=2)
        self.assertAlmostEqual(profile.fov_radius, 123.0, places=0)

    def test_applying_to_one_profile_leaves_the_other_alone(self) -> None:
        before = self.app._read_form().aim_profiles[0]
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        after = self.app._read_form().aim_profiles[0]
        self.assertEqual(after.algorithm, before.algorithm)
        self.assertAlmostEqual(after.kp_max, before.kp_max, places=4)

    def test_the_preset_does_not_touch_local_habits(self) -> None:
        before = self.app._read_form().aim_profiles[1]
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        after = self.app._read_form().aim_profiles[1]
        self.assertEqual(after.trigger, before.trigger)
        self.assertEqual(after.target_class, before.target_class)

    def test_the_parameter_fields_get_rebuilt_for_the_new_algorithm(self) -> None:
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        self.assertEqual(
            list(self.app.profile_algorithm_params[1]),
            ["loop_delay_frames", "gain", "velocity_smoothing"],
        )


if __name__ == "__main__":
    unittest.main()
