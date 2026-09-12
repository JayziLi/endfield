from __future__ import annotations

import unittest

from rhodes_fast.aim_algorithms import (
    Param,
    available_algorithms,
    create_algorithm,
    set_installed_algorithms,
)


class Fake:
    NAME = "fake_kalman"
    DISPLAY_NAME = "假卡尔曼"
    PARAMS = (Param("q", 0.5, 0.0, 1.0, "过程噪声"),)

    def __init__(self, params):
        self.q = params["q"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.q, observation.error_y * self.q


class Impostor:
    NAME = "p"
    DISPLAY_NAME = "假冒比例控制"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, observation):
        return 0.0, 0.0


class RegistryMergeTests(unittest.TestCase):
    def tearDown(self) -> None:
        # 这是进程级的全局状态, 不清干净会污染同一进程里跑的其他测试。
        set_installed_algorithms({})

    def test_builtins_are_there_with_nothing_installed(self) -> None:
        set_installed_algorithms({})
        self.assertIn("p", available_algorithms())
        self.assertNotIn("fake_kalman", available_algorithms())

    def test_an_installed_algorithm_shows_up_and_can_be_constructed(self) -> None:
        set_installed_algorithms({Fake.NAME: Fake})
        self.assertIn("fake_kalman", available_algorithms())
        self.assertAlmostEqual(create_algorithm("fake_kalman", {"q": 0.25}).q, 0.25)

    def test_a_missing_param_falls_back_to_the_declared_default(self) -> None:
        set_installed_algorithms({Fake.NAME: Fake})
        self.assertAlmostEqual(create_algorithm("fake_kalman", {}).q, 0.5)

    def test_an_installed_algorithm_can_never_shadow_a_builtin(self) -> None:
        set_installed_algorithms({"p": Impostor})
        self.assertIsNot(available_algorithms()["p"], Impostor)

    def test_setting_the_installed_set_again_replaces_the_previous_one(self) -> None:
        set_installed_algorithms({Fake.NAME: Fake})
        set_installed_algorithms({})
        self.assertNotIn("fake_kalman", available_algorithms())


if __name__ == "__main__":
    unittest.main()
