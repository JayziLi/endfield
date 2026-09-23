from __future__ import annotations

import json
import unittest
from dataclasses import fields, replace
from pathlib import Path

from rhodes_fast.config import default_config
from rhodes_fast.gui_core.state import (
    FormState,
    config_to_form_state,
    default_labels,
    display_path,
)
from rhodes_fast.gui_web.forms import (
    FormBridge,
    UnknownField,
    choice_payload,
    param_payload,
)

CONFIG_DIR = Path("C:/app")


def _bridge() -> FormBridge:
    return FormBridge(
        config_to_form_state(
            default_config(), CONFIG_DIR, default_labels(), display_path=display_path
        )
    )


def _label_for(mapping: dict[str, str], stored: str) -> str:
    return next(label for label, value in mapping.items() if value == stored)


class SetFieldTest(unittest.TestCase):
    def test_it_sets_a_top_level_field(self) -> None:
        bridge = _bridge()
        bridge.set_field("model_path", "MODEL/a.onnx")
        self.assertEqual(bridge.state.model_path, "MODEL/a.onnx")

    def test_it_sets_a_field_on_one_profile_and_leaves_the_other_alone(self) -> None:
        """两套方案是对称的, 下标写错的症状是「调方案 1 结果方案 2 变了」——
        而两栏长得一模一样, 用户第一反应是自己看错了。"""
        bridge = _bridge()
        before = bridge.state.profiles[1].kp_min
        bridge.set_field("profiles.0.kp_min", 0.07)
        self.assertEqual(bridge.state.profiles[0].kp_min, 0.07)
        self.assertEqual(bridge.state.profiles[1].kp_min, before)

    def test_it_sets_one_algorithm_parameter(self) -> None:
        # 默认配置用的是比例控制, 而它一个参数都没有 (algorithm_param_specs("p")
        # 是空的)。先换成有参数的那个, 不然这条测试是空转的。
        bridge = _bridge()
        specs = param_payload(_label_for(default_labels().algorithm, "feedforward"),
                              default_labels())
        profile = replace(
            bridge.state.profiles[1],
            algorithm=_label_for(default_labels().algorithm, "feedforward"),
            algorithm_params={spec["name"]: spec["default"] for spec in specs},
        )
        bridge.replace_state(
            replace(bridge.state, profiles=(bridge.state.profiles[0], profile))
        )

        name = specs[0]["name"]
        bridge.set_field(f"profiles.1.algorithm_params.{name}", 0.123)
        self.assertEqual(bridge.state.profiles[1].algorithm_params[name], 0.123)

    def test_a_numeric_field_that_is_declared_str_stays_str(self) -> None:
        """udp_port 在 FormState 里是 str, 故意的: 表单留着用户打的原文, 非法
        输入要走到「设置无法保存」那个弹窗。JS 的 number 输入框送上来是数字,
        不转回字符串的话那条解析路径就被绕过去了。"""
        bridge = _bridge()
        bridge.set_field("udp_port", 4455)
        self.assertIsInstance(bridge.state.udp_port, str)
        self.assertEqual(bridge.state.udp_port, "4455")

    def test_an_optional_text_field_is_still_coerced_to_text(self) -> None:
        """单机模式那几个字段声明成 str | None (None = 旧界面没有这个控件)。
        _coerce 不拆 Optional 的话, number 框送上来的 1 会原样存成 int, 跟
        udp_port 那条「数字框也存原文」的规矩对不上。"""
        bridge = _bridge()
        bridge.set_field("desktop_monitor", 1)
        self.assertEqual(bridge.state.desktop_monitor, "1")
        bridge.set_field("mouse_output", "本机 SendInput")
        self.assertEqual(bridge.state.mouse_output, "本机 SendInput")

    def test_a_field_declared_float_stays_float(self) -> None:
        """混进 int 的话 runtime_aim_payload 里那个值会 json 成 1 而不是 1.0。
        管线读 float 无所谓, 但比对负载时会莫名其妙。"""
        bridge = _bridge()
        bridge.set_field("confidence", 1)
        self.assertIsInstance(bridge.state.confidence, float)

    def test_a_field_declared_bool_stays_bool(self) -> None:
        bridge = _bridge()
        bridge.set_field("cuda_graph", 1)
        self.assertIs(bridge.state.cuda_graph, True)

    def test_an_unknown_path_raises_instead_of_going_nowhere(self) -> None:
        """JS 里把路径打错很容易, 而静默忽略的症状是「这个控件没用」—— 没有
        任何线索指向拼写。"""
        for path in ("nope", "profiles.0.nope", "profiles.9.kp_min", "profiles", ""):
            with self.subTest(path=path):
                with self.assertRaises(UnknownField):
                    _bridge().set_field(path, 1)

    def test_an_unknown_algorithm_parameter_raises(self) -> None:
        """参数名是跟着算法走的。换算法之后 JS 那边残留的旧路径必须报出来,
        不然那个值会悄悄加进 algorithm_params, 跟着存进 settings.txt。"""
        with self.assertRaises(UnknownField):
            _bridge().set_field("profiles.0.algorithm_params.这个参数不存在", 1.0)

    def test_the_profiles_tuple_stays_a_two_tuple(self) -> None:
        """FormState.profiles 是个两元组, form_state_to_config 按下标读。
        换成 list 的话类型注解就是假的, 而且没人会发现。"""
        bridge = _bridge()
        bridge.set_field("profiles.0.fov", 120.0)
        self.assertIsInstance(bridge.state.profiles, tuple)
        self.assertEqual(len(bridge.state.profiles), 2)


class PayloadTest(unittest.TestCase):
    def test_the_payload_round_trips_through_json(self) -> None:
        """它每次整片刷新都要过 evaluate_js。混进一个 tuple 或 Path 就当场炸,
        而那一下发生在载入预设之后 —— 表单会停在旧值上, 不报错。"""
        payload = _bridge().as_payload()
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_the_payload_has_every_form_field(self) -> None:
        """少一个字段的症状是那个控件永远填不上值。按 FormState 的字段表扫,
        将来加字段时这条会自动红。"""
        payload = _bridge().as_payload()
        self.assertEqual(sorted(payload), sorted(field.name for field in fields(FormState)))

    def test_profiles_come_out_as_a_list_of_two_dicts(self) -> None:
        payload = _bridge().as_payload()
        self.assertIsInstance(payload["profiles"], list)
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertIsInstance(payload["profiles"][0], dict)

    def test_the_payload_is_a_copy_not_a_window_into_the_state(self) -> None:
        """改负载不该动到状态。共享 dict 的话, JS 那边的一次刷新会顺手改掉
        Python 的真相, 而那是最难查的一类。"""
        bridge = _bridge()
        payload = bridge.as_payload()
        payload["profiles"][0]["algorithm_params"]["注入"] = 1.0
        self.assertNotIn("注入", bridge.state.profiles[0].algorithm_params)

    def test_replace_state_swaps_everything_at_once(self) -> None:
        """载入预设走这条路。一个字段一个字段地 set 会让界面闪一串中间状态,
        而且中途任何一步抛异常就停在半新半旧上。"""
        bridge = _bridge()
        fresh = replace(bridge.state, model_path="MODEL/other.onnx")
        bridge.replace_state(fresh)
        self.assertEqual(bridge.state.model_path, "MODEL/other.onnx")


class ChoicesTest(unittest.TestCase):
    def test_every_dropdown_gets_its_options(self) -> None:
        choices = choice_payload(default_labels())
        self.assertEqual(
            sorted(choices),
            [
                "algorithm", "desktop_backend", "input_mode", "language", "mouse_output",
                "output_format", "provider", "trigger",
            ],
        )

    def test_the_options_are_labels_not_stored_values(self) -> None:
        """下拉框里显示的是标签。塞存储值进去的话用户看到的是 auto / side1,
        而且当前值 (标签) 对不上任何一个选项, 每次打开都跳回第一项。"""
        choices = choice_payload(default_labels())
        self.assertNotIn("auto", choices["provider"])
        self.assertIn(_label_for(default_labels().provider, "auto"), choices["provider"])

    def test_the_payload_round_trips_through_json(self) -> None:
        choices = choice_payload(default_labels())
        self.assertEqual(json.loads(json.dumps(choices)), choices)


class ParamPayloadTest(unittest.TestCase):
    def test_it_describes_every_parameter_of_the_algorithm(self) -> None:
        """02 屏的算法参数是动态生成的: 按 Param 的 min/max/default/label 建滑条。
        少给一个字段, 那个控件就画不出来或者范围是错的。"""
        params = param_payload(_label_for(default_labels().algorithm, "feedforward"), default_labels())
        self.assertTrue(params)
        for spec in params:
            self.assertEqual(
                sorted(spec),
                ["advanced", "default", "label", "maximum", "minimum", "name", "slider", "step"],
            )

    def test_an_unknown_algorithm_gives_an_empty_list_not_an_error(self) -> None:
        """settings.txt 指着一个已删掉的算法是真实场景。抛异常的话整个 02 屏
        画不出来, 用户连改回去的机会都没有。"""
        self.assertEqual(param_payload("这个算法不存在", default_labels()), [])

    def test_the_payload_round_trips_through_json(self) -> None:
        params = param_payload(_label_for(default_labels().algorithm, "feedforward"), default_labels())
        self.assertEqual(json.loads(json.dumps(params)), params)


class ImportSurfaceTest(unittest.TestCase):
    def test_it_does_not_drag_in_tkinter(self) -> None:
        """WebView 那条路不该拖进 tk。forms.py 一旦 import 了 gui.py (比如为了
        拿那几张映射), 这条就会红。"""
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, rhodes_fast.gui_web.forms;"
             " print([n for n in sys.modules if n.startswith('tkinter')])"],
            capture_output=True, text=True,
        )
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)


if __name__ == "__main__":
    unittest.main()
