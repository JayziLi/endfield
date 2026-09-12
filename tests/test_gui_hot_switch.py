from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from rhodes_fast.gui import RhodesFastGui
from rhodes_fast.model_inspection import ModelContract


class RuntimeHandoffTests(unittest.TestCase):
    """界面写运行时文件的那一半。

    管线那一半在 tests/test_algorithm_hot_switch.py。两边都测到, 通道才算通——
    少了任何一头, 下拉框改完都是没反应。
    """

    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()
        # _write_runtime_aim_settings 在没有管线时直接返回, 这里假装管线在跑。
        self.app.process = object()
        self._runtime = tempfile.TemporaryDirectory()
        self.app.runtime_aim_file = (
            pathlib.Path(self._runtime.name) / "missing-cache" / "runtime.aim.json"
        )

    def tearDown(self) -> None:
        self.app.process = None
        self.app.root.destroy()
        self.app.runtime_aim_file.unlink(missing_ok=True)
        self._runtime.cleanup()

    def _written(self) -> list[dict]:
        return json.loads(self.app.runtime_aim_file.read_text(encoding="utf-8"))["profiles"]

    def test_the_algorithm_is_handed_to_the_pipeline(self) -> None:
        self.app.profile_algorithm[1].set("扣在途 + 速度前馈")
        self.app._algorithm_changed(1)
        profiles = self._written()
        self.assertEqual(profiles[1]["algorithm"], "inflight_ff")
        self.assertEqual(
            sorted(profiles[1]["algorithm_params"]),
            ["gain", "loop_delay_frames", "velocity_smoothing"],
        )

    def test_the_other_profile_is_handed_over_untouched(self) -> None:
        before = self.app.profile_algorithm[0].get()
        self.app.profile_algorithm[1].set("比例 + 微分")
        self.app._algorithm_changed(1)
        self.assertEqual(self.app.profile_algorithm[0].get(), before)
        self.assertEqual(self._written()[0]["algorithm"], "p")

    def test_editing_a_parameter_is_handed_over_on_its_own(self) -> None:
        # 换算法要传, 只动参数也要传——否则调 loop_delay_frames 得重启才生效。
        self.app.profile_algorithm[1].set("扣除在途指令")
        self.app._algorithm_changed(1)
        self.app.runtime_aim_file.unlink(missing_ok=True)
        self.app.profile_algorithm_params[1]["loop_delay_frames"].set(3.0)
        self.assertEqual(self._written()[1]["algorithm_params"]["loop_delay_frames"], 3.0)

    def test_a_half_typed_parameter_does_not_write_a_broken_file(self) -> None:
        # 参数框可以手打, 打到一半里面可能是空的。那一下必须跳过, 不能把半份
        # 配置推给管线, 更不能让界面炸掉。
        self.app.profile_algorithm[1].set("扣除在途指令")
        self.app._algorithm_changed(1)
        good = self.app.runtime_aim_file.read_text(encoding="utf-8")
        self.app.profile_algorithm_params[1]["loop_delay_frames"].set("")
        self.app._write_runtime_aim_settings()
        self.assertEqual(self.app.runtime_aim_file.read_text(encoding="utf-8"), good)

    def test_the_dropdown_stays_usable_while_running(self) -> None:
        self.app._set_running(True, "正在运行")
        for combo in self.app.algorithm_combos:
            self.assertEqual(str(combo["state"]), "readonly")
        self.app._set_running(False, "已停止")

    def test_startup_model_inspection_accepts_the_saved_relative_path(self) -> None:
        self.app.model_path.set("MODEL/example.onnx")
        absolute_path = self.app.config_path.parent / "MODEL" / "example.onnx"
        contract = ModelContract((1, 300, 6), "end2end", "candidates_first", 80)

        self.app._apply_model_contract(absolute_path, contract)

        self.assertEqual(self.app.output_format.get(), "端到端 NMS")
        self.assertEqual(len(self.app.target_class_combos[0]["values"]), 80)


if __name__ == "__main__":
    unittest.main()
