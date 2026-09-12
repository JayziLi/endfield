from __future__ import annotations

import unittest

from rhodes_fast.aim_algorithms import available_algorithms
from rhodes_fast.gui import algorithm_choices, algorithm_param_specs


class GuiAlgorithmTests(unittest.TestCase):
    def test_every_algorithm_is_offered_with_a_human_readable_label(self) -> None:
        choices = algorithm_choices()
        self.assertEqual(set(choices.values()), set(available_algorithms()))
        for label in choices:
            self.assertTrue(label.strip())

    def test_p_declares_no_parameters_so_no_fields_are_drawn(self) -> None:
        self.assertEqual(algorithm_param_specs("p"), ())

    def test_inflight_ff_declares_the_three_fields_it_needs(self) -> None:
        names = [spec.name for spec in algorithm_param_specs("inflight_ff")]
        self.assertEqual(names, ["loop_delay_frames", "gain", "velocity_smoothing"])

    def test_an_unknown_algorithm_declares_nothing_rather_than_raising(self) -> None:
        # 配置里指着一个已删掉的算法时, 界面仍要能画出来。
        self.assertEqual(algorithm_param_specs("no_such_algorithm"), ())


if __name__ == "__main__":
    unittest.main()
