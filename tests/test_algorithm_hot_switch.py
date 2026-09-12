from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rhodes_fast.aim_algorithms import set_installed_algorithms
from rhodes_fast.config import AimConfig, AimProfileConfig, KmboxConfig
from rhodes_fast.detector import Detection
from rhodes_fast.kmbox_control import KmboxController


def _runtime_payload(
    algorithm: str = "p",
    params: dict[str, float] | None = None,
    kp_max: float = 0.2,
) -> dict[str, object]:
    return {
        "profiles": [
            {
                "enabled": True,
                "trigger": "side1",
                "kp_min": 0.2,
                "kp_max": kp_max,
                "kp_growth": 0.0,
                "target_class": 0,
                "target_y_ratio": 0.5,
                "fov_radius": 150.0,
                "algorithm": algorithm,
                "algorithm_params": params if params is not None else {},
            },
            {
                "enabled": False,
                "trigger": "left",
                "kp_min": 0.1,
                "kp_max": 0.164,
                "kp_growth": 0.167,
                "algorithm": "p",
                "algorithm_params": {},
            },
        ]
    }


class HotSwitchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.runtime_file = Path(self._folder.name) / "aim.json"

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _write(self, payload: dict[str, object]) -> None:
        self.runtime_file.write_text(json.dumps(payload), encoding="utf-8")

    def _controller(
        self,
        algorithm: str = "p",
        params: dict[str, float] | None = None,
        reload_algorithms=None,
    ):
        self._write(_runtime_payload())
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(smoothing=1.0, deadzone=0),
            self.runtime_file,
            profiles=(
                AimProfileConfig(
                    trigger="side1",
                    algorithm=algorithm,
                    algorithm_params=params or {},
                ),
                AimProfileConfig(enabled=False, trigger="left"),
            ),
            reload_algorithms=reload_algorithms,
        )
        client = Mock()
        client.isdown_side1.return_value = 1
        client.isdown_left.return_value = 0
        controller._client = client
        return controller


class SwitchingTests(HotSwitchTestCase):
    def test_the_algorithm_can_be_swapped_while_the_pipeline_runs(self) -> None:
        controller = self._controller("p")
        self.assertEqual(controller.algorithm_name(0), "p")
        self._write(_runtime_payload("pd", {"kd": 0.5}))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller.algorithm_name(0), "pd")

    def test_the_switch_is_announced_so_the_user_knows_it_took(self) -> None:
        controller = self._controller("p")
        self._write(_runtime_payload("pd", {"kd": 0.5}))
        controller.refresh_runtime_settings(force=True)
        self.assertTrue(any("pd" in notice for notice in controller.algorithm_notices))

    def test_changing_a_parameter_rebuilds_with_the_new_value(self) -> None:
        controller = self._controller("inflight", {"loop_delay_frames": 8.0})
        self._write(_runtime_payload("inflight", {"loop_delay_frames": 3.0}))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller._algorithms[0]._lag, 3)

    def test_an_algorithm_that_is_not_installed_falls_back_loudly(self) -> None:
        controller = self._controller("p")
        self._write(_runtime_payload("someone_elses_kalman"))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller.algorithm_name(0), "p")
        self.assertTrue(
            any("someone_elses_kalman" in notice for notice in controller.algorithm_notices)
        )
        # 回退了也得还能开枪, 不能把管线拖死。
        self.assertIsInstance(
            controller.move_toward(Detection(170, 160, 190, 200, 0.9, 0), 320, 320), tuple
        )


class NoNeedlessRebuildTests(HotSwitchTestCase):
    def test_touching_only_kp_leaves_the_algorithm_object_alone(self) -> None:
        # 这条是这个功能最关键的护栏。refresh 每 50 毫秒跑一次, 只要无条件重建,
        # pd 和 feedforward 的上一帧误差就每 50 毫秒被清一次——微分项和前馈项
        # 永久失效, 而表现只是「手感怪但说不出哪儿怪」, 几乎查不出来。
        controller = self._controller("pd", {"kd": 0.5})
        before = controller._algorithms[0]
        self._write(_runtime_payload("pd", {"kd": 0.5}, kp_max=0.3))
        controller.refresh_runtime_settings(force=True)
        self.assertIs(controller._algorithms[0], before)

    def test_an_identical_write_leaves_the_algorithm_object_alone(self) -> None:
        controller = self._controller("pd", {"kd": 0.5})
        before = controller._algorithms[0]
        self._write(_runtime_payload("pd", {"kd": 0.5}))
        controller.refresh_runtime_settings(force=True)
        self.assertIs(controller._algorithms[0], before)


class InFlightWindowTests(HotSwitchTestCase):
    def _fill_in_flight(self, controller) -> None:
        for _ in range(12):
            controller.move_toward(Detection(200, 190, 220, 230, 0.9, 0), 320, 320)

    def test_the_in_flight_window_survives_an_algorithm_switch(self) -> None:
        # 那些位移是物理上已经发给鼠标的。清掉的话「扣在途」会以为什么都没发,
        # 当场多走一截——而切算法正是你在评估手感的时刻, 这个过冲最误导人。
        controller = self._controller("p")
        self._fill_in_flight(controller)
        self.assertGreater(len(controller._motion_states[0].recent_commands), 0)
        self._write(_runtime_payload("inflight", {"loop_delay_frames": 8.0}))
        controller.refresh_runtime_settings(force=True)
        self.assertGreater(len(controller._motion_states[0].recent_commands), 0)

    def test_the_in_flight_window_survives_a_plain_kp_change_too(self) -> None:
        controller = self._controller("inflight", {"loop_delay_frames": 8.0})
        self._fill_in_flight(controller)
        filled = len(controller._motion_states[0].recent_commands)
        self.assertGreater(filled, 0)
        self._write(_runtime_payload("inflight", {"loop_delay_frames": 8.0}, kp_max=0.3))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(len(controller._motion_states[0].recent_commands), filled)


class _Late:
    NAME = "late_kalman"
    DISPLAY_NAME = "迟到的卡尔曼"
    PARAMS = ()

    def __init__(self, params) -> None:
        self.params = params

    def reset(self) -> None:
        pass

    def compute(self, observation):
        return 0.0, 0.0


class LateImportTests(HotSwitchTestCase):
    """管线启动之后才导入的算法。

    启动时加载的那份注册表里没有它, 所以第一次查必然查不到。读盘重新加载一次
    再试是有代价的——要 import 用户的 .py, 会顿几毫秒——所以只在查不到时才做。
    """

    def tearDown(self) -> None:
        set_installed_algorithms({})
        super().tearDown()

    def _reloader(self, calls: list[int]):
        def reload() -> list[str]:
            calls.append(1)
            set_installed_algorithms({_Late.NAME: _Late})
            return []

        return reload

    def test_an_algorithm_imported_after_startup_is_picked_up_on_the_switch(self) -> None:
        calls: list[int] = []
        controller = self._controller("p", reload_algorithms=self._reloader(calls))
        self._write(_runtime_payload("late_kalman"))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller.algorithm_name(0), "late_kalman")
        self.assertEqual(calls, [1])

    def test_the_reload_is_skipped_when_the_algorithm_is_already_loaded(self) -> None:
        # 每次切换都读盘 import 一遍的话, 切换成本白涨几毫秒。
        calls: list[int] = []
        controller = self._controller("p", reload_algorithms=self._reloader(calls))
        self._write(_runtime_payload("pd", {"kd": 0.5}))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller.algorithm_name(0), "pd")
        self.assertEqual(calls, [])

    def test_a_name_still_unknown_after_reloading_falls_back_to_p(self) -> None:
        calls: list[int] = []
        controller = self._controller("p", reload_algorithms=self._reloader(calls))
        self._write(_runtime_payload("never_existed"))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller.algorithm_name(0), "p")
        self.assertEqual(calls, [1])
        self.assertTrue(any("never_existed" in note for note in controller.algorithm_notices))

    def test_a_failed_name_is_not_retried_every_frame(self) -> None:
        # 回退之后 _profiles 已经记成新配置, 后续 refresh 不该再为同一个名字读盘。
        calls: list[int] = []
        controller = self._controller("p", reload_algorithms=self._reloader(calls))
        self._write(_runtime_payload("never_existed"))
        for _ in range(5):
            controller.refresh_runtime_settings(force=True)
        self.assertEqual(calls, [1])

    def test_without_a_reloader_the_behaviour_is_the_old_loud_fallback(self) -> None:
        controller = self._controller("p")
        self._write(_runtime_payload("late_kalman"))
        controller.refresh_runtime_settings(force=True)
        self.assertEqual(controller.algorithm_name(0), "p")


if __name__ == "__main__":
    unittest.main()


class ForgetTargetTests(HotSwitchTestCase):
    """换目标时算法状态作废, 但在途指令留着。"""

    def test_the_in_flight_window_survives_a_target_change(self) -> None:
        controller = self._controller("inflight", {"loop_delay_frames": 8.0})
        for _ in range(12):
            controller.move_toward(Detection(200, 190, 220, 230, 0.9, 0), 320, 320)
        filled = len(controller._motion_states[0].recent_commands)
        self.assertGreater(filled, 0)
        controller.forget_target()
        # 那些位移物理上已经发给鼠标了。抹掉的话「扣在途」会以为什么都没发,
        # 当场多走一截 —— 而换目标恰恰是最容易把这个过冲看成「算法有问题」的时刻。
        self.assertEqual(len(controller._motion_states[0].recent_commands), filled)

    def test_the_algorithm_state_itself_is_dropped(self) -> None:
        controller = self._controller("feedforward", {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25})
        for _ in range(12):
            controller.move_toward(Detection(200, 190, 220, 230, 0.9, 0), 320, 320)
        self.assertIsNotNone(controller._algorithms[0]._previous)
        controller.forget_target()
        self.assertIsNone(controller._algorithms[0]._previous)
