from __future__ import annotations

import dataclasses
import json
import unittest
from dataclasses import replace
from pathlib import Path

from rhodes_fast.config import (
    AimProfileConfig,
    AppConfig,
    DesktopConfig,
    InputConfig,
    KmboxConfig,
    ModelConfig,
    MouseConfig,
    ObsConfig,
    UdpConfig,
    default_config,
)
from rhodes_fast.gui import (
    INPUT_MODES,
    LOG_LANGUAGES,
    OUTPUT_FORMATS,
    PROVIDERS,
    TRIGGERS,
    _display_path,
    _resolve_model_path,
    algorithm_choices,
    algorithm_param_specs,
)
from rhodes_fast.gui_core.state import (
    DESKTOP_BACKENDS,
    MOUSE_OUTPUTS,
    FormState,
    Labels,
    ProfileFormState,
    apply_preset_to_state,
    config_to_form_state,
    form_state_to_config,
    runtime_aim_payload,
    trail_settings_from_state,
)
from rhodes_fast.presets import Preset

# 方向: 六张映射一律 {显示标签: 存储值}, 跟 gui.py 里一致。
LABELS = Labels(
    provider=PROVIDERS,
    output_format=OUTPUT_FORMATS,
    input_mode=INPUT_MODES,
    language=LOG_LANGUAGES,
    trigger=TRIGGERS,
    algorithm=algorithm_choices(),
    desktop_backend=DESKTOP_BACKENDS,
    mouse_output=MOUSE_OUTPUTS,
)

CONFIG_DIR = Path("C:/app")


def defaults_of(algorithm: str) -> dict[str, float]:
    """{参数名: 默认值}。走 gui.py 的 algorithm_param_specs, 免得拿 state.py
    自己的私有函数当自己的答案。"""
    return {spec.name: spec.default for spec in algorithm_param_specs(algorithm)}


def _label_for(mapping: dict[str, str], stored: str) -> str:
    """存储值 → 显示标签。测试自己算一遍, 不去借 state.py 的私有 _label —— 拿
    被测代码当自己的答案的话, 反查写反了两边会一起反。"""
    return next(label for label, value in mapping.items() if value == stored)


def make_config(*, profile_1_algorithm_params: dict[str, float] | None = None, **overrides) -> AppConfig:
    """造一个测试用的 AppConfig。

    不能写 AppConfig() —— 它是 frozen dataclass, 七个段都没有默认值, 空构造
    直接 TypeError。仓库里的入口是 default_config() (tests/test_presets.py
    就是这么用的)。

    每个字段都刻意偏离默认值: 转换函数漏掉某个字段时, 断言才有机会红。
    方案 1 用带参数的算法, 否则「参数回落默认值」那条测的是 {} == {}。
    """
    config = default_config()
    config = replace(
        config,
        input=replace(config.input, mode="obs_websocket"),
        ui=replace(config.ui, language="en", preset="夜间", trail_seconds=1.4),
        udp=replace(config.udp, host="192.0.2.164", port=4466, width=416, height=384),
        obs=replace(
            config.obs, host="10.0.0.5", port=4460, password="obs-pass", source_name="游戏画面"
        ),
        model=replace(
            config.model,
            path=CONFIG_DIR / "MODEL" / "game.onnx",
            provider="tensorrt",
            cuda_graph=False,
            gpu_preprocess=False,
            output_format="yolov8",
            confidence=0.42,
            iou=0.61,
        ),
        kmbox=replace(config.kmbox, enabled=True, host="10.9.8.7", port=8810, uuid="ABCD1234"),
        desktop=replace(config.desktop, backend="winrt", monitor=1, width=288, height=256),
        mouse=replace(config.mouse, output="sendinput"),
        aim_profile_1=AimProfileConfig(
            enabled=True,
            trigger="side2",
            kp_min=0.028,
            kp_max=0.056,
            kp_growth=0.045,
            target_class=3,
            target_y_ratio=0.27,
            fov_radius=122.0,
            algorithm="feedforward",
            algorithm_params={"gain": 2.0} if profile_1_algorithm_params is None else profile_1_algorithm_params,
        ),
        aim_profile_2=AimProfileConfig(
            enabled=False,
            trigger="left",
            kp_min=0.062,
            kp_max=0.113,
            kp_growth=0.058,
            target_class=1,
            target_y_ratio=0.17,
            fov_radius=149.0,
            algorithm="pd",
            algorithm_params={"kd": 0.9},
        ),
    )
    return replace(config, **overrides) if overrides else config


def make_preset(
    *,
    algorithm: str = "windmouse",
    algorithm_params: dict[str, float] | None = None,
    **overrides,
) -> Preset:
    """造一份测试用的预设。

    每个字段都跟 make_config() 不一样, 而且尽量连类型之外的形状也不一样(算法
    换了一个、两套方案的启用状态对调): 两边填一样的值时, 一个漏掉的赋值看起来
    跟做过一模一样, 断言就再也红不了。

    UUID 故意存成小写: 预设里写着什么就填什么。大写化是保存那一侧
    (form_state_to_config) 的事, 载入时跟着做会让界面显示的跟文件里的对不上。

    Preset 没有 ui 段 —— 日志语言、轨迹长度、当前预设名不跟着预设走, 所以这里
    也没有对应的字段可造。
    """
    preset = Preset(
        model=ModelConfig(
            path=CONFIG_DIR / "MODEL" / "night.onnx",
            provider="cuda",
            cuda_graph=True,
            gpu_preprocess=True,
            output_format="yolov5",
            confidence=0.31,
            iou=0.55,
        ),
        input=InputConfig(mode="udp_jpeg"),
        udp=UdpConfig(host="198.51.100.9", port=5577, width=640, height=512),
        obs=ObsConfig(host="10.1.2.3", port=4455, password="  夜间密码  ", source_name="夜间画面"),
        kmbox=KmboxConfig(enabled=False, host="10.1.1.1", port=9910, uuid="ffee0011"),
        desktop=DesktopConfig(backend="dxgi", monitor=2, width=224, height=200),
        mouse=MouseConfig(output="kmbox"),
        aim_profile_1=AimProfileConfig(
            enabled=False,
            trigger="right",
            kp_min=0.071,
            kp_max=0.132,
            kp_growth=0.088,
            target_class=5,
            target_y_ratio=0.61,
            fov_radius=88.0,
            algorithm=algorithm,
            algorithm_params=(
                {"gravity": 7.0} if algorithm_params is None else dict(algorithm_params)
            ),
        ),
        aim_profile_2=AimProfileConfig(
            enabled=True,
            trigger="side1",
            kp_min=0.033,
            kp_max=0.091,
            kp_growth=0.019,
            target_class=2,
            target_y_ratio=0.44,
            fov_radius=175.0,
            algorithm="inflight",
            algorithm_params={"loop_delay_frames": 3.0},
        ),
    )
    return replace(preset, **overrides) if overrides else preset


class ConfigToFormStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config()
        self.state = config_to_form_state(
            self.config, CONFIG_DIR, LABELS, display_path=_display_path
        )

    def test_numeric_text_fields_stay_strings(self) -> None:
        """端口等字段在界面上是文本框, 用户能输入任何东西。提前转 int 会把
        「非法输入 → 弹窗报错」变成「构造 FormState 就崩」。"""
        for name in ("udp_port", "udp_width", "udp_height", "obs_port", "kmbox_port"):
            with self.subTest(field=name):
                self.assertIsInstance(getattr(self.state, name), str)
        self.assertEqual(self.state.udp_port, str(self.config.udp.port))
        self.assertEqual(self.state.kmbox_port, str(self.config.kmbox.port))

    def test_single_pc_fields_are_labels_and_text(self) -> None:
        """下拉框存标签, 数字框存原文 —— 跟其他字段同一套规矩。"""
        self.assertEqual(self.state.desktop_backend, _label_for(DESKTOP_BACKENDS, "winrt"))
        self.assertEqual(self.state.mouse_output, _label_for(MOUSE_OUTPUTS, "sendinput"))
        self.assertEqual(
            (self.state.desktop_monitor, self.state.desktop_width, self.state.desktop_height),
            ("1", "288", "256"),
        )

    def test_aim_position_is_a_percentage(self) -> None:
        """配置里存 0..1 的比例, 界面上是 0..100 的百分比滑条。"""
        expected = self.config.aim_profile_1.target_y_ratio * 100.0
        self.assertAlmostEqual(self.state.profiles[0].aim_position, expected)

    def test_algorithm_is_stored_as_its_display_label(self) -> None:
        stored = self.config.aim_profile_1.algorithm
        label = next(k for k, v in algorithm_choices().items() if v == stored)
        self.assertEqual(self.state.profiles[0].algorithm, label)

    def test_every_dropdown_field_holds_a_display_label(self) -> None:
        """下拉框绑的是标签。存反了不光显示成 tensorrt 而不是 TensorRT FP16,
        那个值根本不在下拉框的候选里, 框会显示成空的。"""
        for value, mapping in (
            (self.state.provider, PROVIDERS),
            (self.state.output_format, OUTPUT_FORMATS),
            (self.state.input_mode, INPUT_MODES),
            (self.state.log_language, LOG_LANGUAGES),
            (self.state.profiles[0].trigger, TRIGGERS),
            (self.state.profiles[1].trigger, TRIGGERS),
        ):
            with self.subTest(value=value):
                self.assertIn(value, mapping)

    def test_swapping_two_label_maps_changes_the_result(self) -> None:
        """Labels 存在的理由。六张映射类型全是 dict[str, str], 散着当六个参数传
        时调了位置类型检查一声不吭, 而这里证明调了位置确实出错值 —— 也就是说
        当初那种签名下, 一个装反的调用点只会在下拉框里显形。
        """
        swapped = replace(LABELS, provider=LABELS.output_format, output_format=LABELS.provider)
        state = config_to_form_state(self.config, CONFIG_DIR, swapped, display_path=_display_path)
        self.assertNotEqual(state.provider, self.state.provider)
        self.assertNotEqual(state.output_format, self.state.output_format)
        self.assertNotEqual(state, self.state)

    def test_unknown_stored_value_falls_back_to_the_first_label(self) -> None:
        """配置里指着一个已经没有的选项时, gui.py 的 _display_value 退回第一个标签,
        这样下拉框里仍是个合法候选。退回原始存储值会往框里塞一个候选之外的字符串。"""
        config = make_config()
        config = replace(config, model=replace(config.model, provider="quantum"))
        state = config_to_form_state(config, CONFIG_DIR, LABELS, display_path=_display_path)
        self.assertEqual(state.provider, next(iter(PROVIDERS)))

    def test_missing_algorithm_params_fall_back_to_defaults(self) -> None:
        """配置里没写参数时要补默认值 —— 控件是照算法契约建的, 少一个键界面就
        没东西可绑。"""
        config = make_config(profile_1_algorithm_params={})
        state = config_to_form_state(config, CONFIG_DIR, LABELS, display_path=_display_path)
        expected = defaults_of(config.aim_profile_1.algorithm)
        self.assertTrue(expected, "挑个带参数的算法, 否则这条测了个空")
        self.assertEqual(state.profiles[0].algorithm_params, expected)

    def test_configured_algorithm_params_win_over_defaults(self) -> None:
        """写了的用写的, 没写的补默认 —— 同一个 dict 里两种来源并存。"""
        params = self.state.profiles[0].algorithm_params
        self.assertEqual(params["gain"], 2.0)
        self.assertEqual(params["loop_delay_frames"], defaults_of("feedforward")["loop_delay_frames"])

    def test_unknown_algorithm_yields_no_params_instead_of_raising(self) -> None:
        """配置指着一个已删掉的算法时界面仍要画得出来。对应 gui.py:101 的注释。"""
        config = make_config()
        config = replace(
            config, aim_profile_1=replace(config.aim_profile_1, algorithm="ghost")
        )
        state = config_to_form_state(config, CONFIG_DIR, LABELS, display_path=_display_path)
        self.assertEqual(state.profiles[0].algorithm_params, {})
        self.assertEqual(state.profiles[0].algorithm, next(iter(algorithm_choices())))

    def test_trail_toggles_ignore_the_config_and_start_from_defaults(self) -> None:
        """预览页的三个勾选框每次打开都回到默认, 只有轨迹长度从配置恢复。
        对应 gui.py:214 的注释。"""
        self.assertTrue(self.state.preview_frame)
        self.assertFalse(self.state.trail_enabled)
        self.assertFalse(self.state.trail_optimal_path)
        self.assertEqual(self.state.trail_seconds, self.config.ui.trail_seconds)

    def test_latency_log_always_starts_off(self) -> None:
        self.assertFalse(self.state.latency_log_enabled)

    def test_two_profiles(self) -> None:
        self.assertEqual(len(self.state.profiles), 2)
        self.assertIsInstance(self.state.profiles[0], ProfileFormState)

    def test_the_second_profile_comes_from_the_second_config_profile(self) -> None:
        """两个方案各绑一组控件, 填错顺序会让方案 2 显示方案 1 的设置。"""
        second = self.state.profiles[1]
        self.assertFalse(second.enabled)
        self.assertEqual(second.target_class, "1")
        self.assertAlmostEqual(second.fov, 149.0)
        self.assertAlmostEqual(second.kp_growth, 0.058)
        self.assertEqual(second.algorithm_params["kd"], 0.9)

    def test_model_path_is_shown_relative_to_the_config_directory(self) -> None:
        self.assertEqual(self.state.model_path, "MODEL/game.onnx")

    def test_plain_scalars_come_across_unchanged(self) -> None:
        self.assertFalse(self.state.cuda_graph)
        self.assertFalse(self.state.gpu_preprocess)
        self.assertAlmostEqual(self.state.confidence, 0.42)
        self.assertAlmostEqual(self.state.iou, 0.61)
        self.assertEqual(self.state.udp_host, "192.0.2.164")
        self.assertEqual(self.state.obs_host, "10.0.0.5")
        self.assertEqual(self.state.obs_password, "obs-pass")
        self.assertEqual(self.state.obs_source, "游戏画面")
        self.assertTrue(self.state.kmbox_enabled)
        self.assertEqual(self.state.kmbox_host, "10.9.8.7")
        self.assertEqual(self.state.kmbox_uuid, "ABCD1234")

    def test_target_class_is_text_because_it_is_a_combobox(self) -> None:
        """目标标签是下拉框, 里面装的是文本, 所以状态里也存文本。"""
        self.assertEqual(self.state.profiles[0].target_class, "3")

    def test_form_state_is_a_plain_dataclass(self) -> None:
        """能直接 asdict 成 JSON —— WebView 界面靠这个跟前端通信。"""
        self.assertTrue(dataclasses.is_dataclass(FormState))
        dataclasses.asdict(self.state)


class FormStateToConfigTest(unittest.TestCase):
    def _state(self) -> FormState:
        return config_to_form_state(make_config(), CONFIG_DIR, LABELS, display_path=_display_path)

    def _convert(self, state: FormState, *, current_preset: str | None = None) -> AppConfig:
        return form_state_to_config(
            state,
            make_config(),
            CONFIG_DIR,
            LABELS,
            current_preset=current_preset,
            resolve_model_path=_resolve_model_path,
        )

    def test_round_trip_preserves_the_config(self) -> None:
        """改一圈再转回去, 除了显式重写的字段外应该原样还原。"""
        base = make_config()
        state = config_to_form_state(base, CONFIG_DIR, LABELS, display_path=_display_path)
        result = form_state_to_config(
            state,
            base,
            CONFIG_DIR,
            LABELS,
            current_preset=None,
            resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.udp.port, base.udp.port)
        self.assertEqual(result.kmbox.host, base.kmbox.host)
        self.assertEqual(result.aim_profile_1.algorithm, base.aim_profile_1.algorithm)

    def test_round_trip_restores_every_dropdown(self) -> None:
        """标签 → 存储值这一步跟 _label 的反查共用一份映射, 走反一张表就会在
        这里现形。"""
        base = make_config()
        result = form_state_to_config(
            config_to_form_state(base, CONFIG_DIR, LABELS, display_path=_display_path),
            base,
            CONFIG_DIR,
            LABELS,
            current_preset=None,
            resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.model.provider, base.model.provider)
        self.assertEqual(result.model.output_format, base.model.output_format)
        self.assertEqual(result.input.mode, base.input.mode)
        self.assertEqual(result.ui.language, base.ui.language)
        self.assertEqual(result.aim_profile_1.trigger, base.aim_profile_1.trigger)
        self.assertEqual(result.aim_profile_2.trigger, base.aim_profile_2.trigger)
        self.assertEqual(result.desktop, base.desktop)
        self.assertEqual(result.mouse, base.mouse)

    def test_single_pc_fields_are_read_from_the_form(self) -> None:
        state = replace(
            self._state(),
            input_mode=_label_for(INPUT_MODES, "desktop"),
            desktop_backend=_label_for(DESKTOP_BACKENDS, "dxgi"),
            desktop_monitor=" 0 ",
            desktop_width="320",
            desktop_height="300",
            mouse_output=_label_for(MOUSE_OUTPUTS, "kmbox"),
        )
        result = self._convert(state)
        self.assertEqual(result.input.mode, "desktop")
        self.assertEqual(result.desktop, DesktopConfig(backend="dxgi", monitor=0, width=320, height=300))
        self.assertEqual(result.mouse.output, "kmbox")

    def test_an_interface_without_the_single_pc_controls_keeps_the_saved_values(self) -> None:
        """旧的 tkinter 界面没有这几个控件, 它构造 FormState 时不传它们 (None)。
        这时必须原样保留 settings.txt 里的值 —— 在新界面里选了 SendInput, 到旧界面
        里点一下保存就悄悄变回 KMBox, 用户根本想不到是保存那一下改的。"""
        state = replace(
            self._state(),
            desktop_backend=None,
            desktop_monitor=None,
            desktop_width=None,
            desktop_height=None,
            mouse_output=None,
        )
        result = self._convert(state)
        self.assertEqual(result.desktop, make_config().desktop)
        self.assertEqual(result.mouse, make_config().mouse)

    def test_the_old_interface_can_still_build_a_form_without_them(self) -> None:
        """gui.py 自己一个字段一个字段地构造 FormState。新字段没有默认值的话,
        旧界面一打开就 TypeError。"""
        names = {field.name for field in dataclasses.fields(FormState)}
        for name in ("desktop_backend", "desktop_monitor", "desktop_width", "desktop_height", "mouse_output"):
            with self.subTest(name=name):
                self.assertIn(name, names)
                field = next(f for f in dataclasses.fields(FormState) if f.name == name)
                self.assertIsNone(field.default)

    def test_a_monitor_number_that_is_not_a_number_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            self._convert(replace(self._state(), desktop_monitor="主屏"))

    def test_invalid_port_raises_valueerror(self) -> None:
        """这是「输入非法 → 弹窗报错」那条路的入口, 必须抛, 不能吞。"""
        state = self._state()
        state.udp_port = "不是数字"
        with self.assertRaises(ValueError):
            self._convert(state)

    def test_aim_section_mirrors_profile_one(self) -> None:
        """gui.py:1394 —— 旧配置消费者和 pipeline benchmark 还在读 aim 段。"""
        state = self._state()
        state.profiles[0].target_class = "7"
        state.profiles[0].fov = 123.0
        result = self._convert(state)
        self.assertEqual(result.aim.target_class, 7)
        self.assertEqual(result.aim.fov_radius, 123.0)

    def test_aim_section_ignores_profile_two(self) -> None:
        """只跟方案 1 同步。跟错方案的话切到方案 2 就会把 aim 段改掉。"""
        state = self._state()
        state.profiles[1].fov = 999.0
        self.assertNotEqual(self._convert(state).aim.fov_radius, 999.0)

    def test_aim_section_keeps_the_fields_no_profile_carries(self) -> None:
        """方案里没有 smoothing / deadzone / max_step 这三项, 它们只能从 base
        继承。漏了 replace 的基准对象就会被重置成 AimConfig 的默认值。"""
        base = make_config()
        base = replace(
            base, aim=replace(base.aim, smoothing=0.75, deadzone=3.5, max_step=17)
        )
        result = form_state_to_config(
            config_to_form_state(base, CONFIG_DIR, LABELS, display_path=_display_path),
            base,
            CONFIG_DIR,
            LABELS,
            current_preset=None,
            resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.aim.smoothing, 0.75)
        self.assertEqual(result.aim.deadzone, 3.5)
        self.assertEqual(result.aim.max_step, 17)

    def test_aim_position_is_clamped_to_zero_one(self) -> None:
        state = self._state()
        state.profiles[0].aim_position = 250.0
        result = self._convert(state)
        self.assertEqual(result.aim_profile_1.target_y_ratio, 1.0)
        self.assertEqual(result.aim.target_y_ratio, 1.0)

    def test_negative_aim_position_is_clamped_to_zero(self) -> None:
        state = self._state()
        state.profiles[1].aim_position = -40.0
        self.assertEqual(self._convert(state).aim_profile_2.target_y_ratio, 0.0)

    def test_uuid_is_upper_cased_and_stripped(self) -> None:
        state = self._state()
        state.kmbox_uuid = "  abc123  "
        self.assertEqual(self._convert(state).kmbox.uuid, "ABC123")

    def test_host_and_source_fields_are_stripped(self) -> None:
        state = self._state()
        state.udp_host = "  192.0.2.164 "
        state.obs_host = " 10.0.0.5  "
        state.obs_source = "  游戏画面  "
        state.kmbox_host = " 10.9.8.7 "
        result = self._convert(state)
        self.assertEqual(result.udp.host, "192.0.2.164")
        self.assertEqual(result.obs.host, "10.0.0.5")
        self.assertEqual(result.obs.source_name, "游戏画面")
        self.assertEqual(result.kmbox.host, "10.9.8.7")

    def test_obs_password_keeps_its_whitespace(self) -> None:
        """旁边四个字段都 strip 了, 唯独密码不 —— 空格是密码的一部分, 剪掉
        会让用户连不上而且看不出为什么。对应 gui.py:1382。"""
        state = self._state()
        state.obs_password = "  pa ss  "
        self.assertEqual(self._convert(state).obs.password, "  pa ss  ")

    def test_model_path_is_stripped_before_being_resolved(self) -> None:
        """从资源管理器拖进来的路径两头常带空格, Windows 不会替我们剪掉:
        Path('C:/app/  MODEL') 是个真实存在的另一个目录。对应 gui.py:1362。"""
        state = self._state()
        state.model_path = "  MODEL/game.onnx  "
        self.assertEqual(
            self._convert(state).model.path, _resolve_model_path("MODEL/game.onnx", CONFIG_DIR)
        )

    def test_model_path_is_resolved_against_the_config_directory(self) -> None:
        state = self._state()
        state.model_path = "MODEL/game.onnx"
        self.assertTrue(self._convert(state).model.path.is_absolute())

    def test_output_layout_is_always_auto(self) -> None:
        """界面上没有这个选项, 但它得跟着输出格式一起被写成 auto, 否则旧设置
        文件里遗留的布局会跟新格式对不上。对应 gui.py:1367。"""
        base = make_config()
        base = replace(base, model=replace(base.model, output_layout="nchw"))
        result = form_state_to_config(
            config_to_form_state(base, CONFIG_DIR, LABELS, display_path=_display_path),
            base,
            CONFIG_DIR,
            LABELS,
            current_preset=None,
            resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.model.output_layout, "auto")

    def test_current_preset_lands_in_ui_section(self) -> None:
        self.assertEqual(self._convert(self._state(), current_preset="夜间").ui.preset, "夜间")

    def test_no_preset_becomes_empty_string(self) -> None:
        """base 里写着预设名也要被清掉 —— 没选预设就是没选。"""
        self.assertEqual(self._convert(self._state(), current_preset=None).ui.preset, "")

    def test_ui_trail_seconds_stores_the_rounded_value(self) -> None:
        """写进配置的是夹紧取整之后的秒数, 不是滑条上的原始值。
        对应 gui.py:1434 把 trail.seconds 灌进 ui 段那一行。"""
        state = self._state()
        state.trail_seconds = 1.2749
        self.assertEqual(self._convert(state).ui.trail_seconds, 1.3)

    def test_preview_toggles_never_reach_the_config(self) -> None:
        """三个勾选框是运行期开关, 只有轨迹长度进配置。"""
        state = self._state()
        state.trail_enabled = True
        state.trail_optimal_path = True
        state.latency_log_enabled = True
        result = self._convert(state)
        self.assertEqual(result.ui.trail_seconds, make_config().ui.trail_seconds)

    def test_the_second_profile_is_read_from_the_second_form_profile(self) -> None:
        """两套方案的转换是抄出来的两段, 容易两段都写 profiles[0]。"""
        state = self._state()
        state.profiles[1].kp_growth = 0.099
        state.profiles[1].target_class = "5"
        result = self._convert(state)
        self.assertAlmostEqual(result.aim_profile_2.kp_growth, 0.099)
        self.assertEqual(result.aim_profile_2.target_class, 5)
        self.assertNotEqual(result.aim_profile_1.kp_growth, 0.099)

    def test_algorithm_params_are_copied_not_aliased(self) -> None:
        """AppConfig 是 frozen 的, 但里面那个 dict 不是。直接把 FormState 的
        dict 塞进去, 之后动一下滑条就会改到已经保存的配置。"""
        state = self._state()
        result = self._convert(state)
        state.profiles[0].algorithm_params["gain"] = 99.0
        self.assertEqual(result.aim_profile_1.algorithm_params["gain"], 2.0)

    def test_the_base_config_is_left_untouched(self) -> None:
        """纯函数: 转换不该改掉传进来的那份配置。"""
        base = make_config()
        state = self._state()
        state.udp_port = "9999"
        form_state_to_config(
            state,
            base,
            CONFIG_DIR,
            LABELS,
            current_preset=None,
            resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(base.udp.port, make_config().udp.port)


class TrailSettingsFromStateTest(unittest.TestCase):
    def _state(self) -> FormState:
        return config_to_form_state(make_config(), CONFIG_DIR, LABELS, display_path=_display_path)

    def test_seconds_are_clamped_and_rounded_to_one_decimal(self) -> None:
        """滑条是连续的, 存 0.1 秒一档: 设置文件里不该出现 1.2749 这种数。
        对应 gui.py:1468。"""
        from rhodes_fast.config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS

        state = self._state()
        state.trail_seconds = 1.2749
        self.assertEqual(trail_settings_from_state(state).seconds, 1.3)

        state.trail_seconds = TRAIL_MAX_SECONDS + 100
        self.assertEqual(trail_settings_from_state(state).seconds, TRAIL_MAX_SECONDS)

        state.trail_seconds = TRAIL_MIN_SECONDS - 100
        self.assertEqual(trail_settings_from_state(state).seconds, TRAIL_MIN_SECONDS)

    def test_the_three_toggles_come_straight_from_the_state(self) -> None:
        state = self._state()
        state.preview_frame = False
        state.trail_enabled = True
        state.trail_optimal_path = True
        settings = trail_settings_from_state(state)
        self.assertFalse(settings.show_frame)
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.optimal_path)


class ApplyPresetToStateTest(unittest.TestCase):
    def _state(self) -> FormState:
        return config_to_form_state(make_config(), CONFIG_DIR, LABELS, display_path=_display_path)

    def _apply(self, state: FormState, preset: Preset) -> None:
        apply_preset_to_state(state, preset, CONFIG_DIR, LABELS, display_path=_display_path)

    def test_stale_algorithm_params_do_not_leak_across_presets(self) -> None:
        """gui.py:1165 —— 文件里没写的参数该回到默认, 不该沿用上一个预设的值。"""
        state = self._state()
        state.profiles[0].algorithm_params = {
            "gain": 9.9,
            "velocity_smoothing": 9.9,
            "loop_delay_frames": 9.9,
        }

        self._apply(state, make_preset(algorithm="feedforward", algorithm_params={"gain": 2.0}))

        defaults = defaults_of("feedforward")
        self.assertEqual(state.profiles[0].algorithm_params["gain"], 2.0)
        self.assertEqual(
            state.profiles[0].algorithm_params["velocity_smoothing"],
            defaults["velocity_smoothing"],
        )

    def test_switching_algorithms_drops_the_old_algorithms_params(self) -> None:
        """换算法时参数表要整个换掉, 不是往旧表上叠。留着上一个算法的键, 界面
        建控件时会照新算法的契约来, 那些多出来的键谁也看不见, 却会被原样存回配置。"""
        state = self._state()  # 方案 1 是 feedforward
        self.assertIn("velocity_smoothing", state.profiles[0].algorithm_params)

        self._apply(state, make_preset(algorithm="pd", algorithm_params={"kd": 0.7}))

        self.assertEqual(state.profiles[0].algorithm_params, {"kd": 0.7})

    def test_numeric_fields_come_back_as_strings(self) -> None:
        """端口/宽高在界面上是文本框。灌预设时转成 int 塞进去, 下一次保存就会
        拿一个 int 去 .strip()。"""
        state = self._state()
        self._apply(state, make_preset())
        for name, expected in (
            ("udp_port", "5577"),
            ("udp_width", "640"),
            ("udp_height", "512"),
            ("obs_port", "4455"),
            ("kmbox_port", "9910"),
            ("target_class", "5"),
        ):
            with self.subTest(field=name):
                value = (
                    state.profiles[0].target_class
                    if name == "target_class"
                    else getattr(state, name)
                )
                self.assertIsInstance(value, str)
                self.assertEqual(value, expected)

    def test_latency_and_trail_toggles_are_left_alone(self) -> None:
        """预设不该动预览页的临时开关。"""
        state = self._state()
        state.trail_enabled = True
        state.trail_optimal_path = True
        state.preview_frame = False
        state.trail_seconds = 1.9
        state.latency_log_enabled = True

        self._apply(state, make_preset())

        self.assertTrue(state.trail_enabled)
        self.assertTrue(state.trail_optimal_path)
        self.assertFalse(state.preview_frame)
        self.assertEqual(state.trail_seconds, 1.9)
        self.assertTrue(state.latency_log_enabled)

    def test_log_language_is_left_alone(self) -> None:
        """预设里没有 ui 段, _fill_form 也不碰日志语言。跟着预设改语言的话,
        载入一份别人给的预设会把界面语言换掉。"""
        state = self._state()
        before = state.log_language
        self._apply(state, make_preset())
        self.assertEqual(state.log_language, before)

    def test_it_mutates_in_place_and_returns_nothing(self) -> None:
        """适配层拿的是同一个 FormState 对象, 返回新对象的话调用方会写回旧的那份。"""
        state = self._state()
        profiles_before = state.profiles
        self.assertIsNone(self._apply(state, make_preset()))
        self.assertNotEqual(state.profiles, profiles_before)

    def test_every_field_the_preset_carries_lands_in_the_state(self) -> None:
        """逐个字段对一遍 gui.py:1130-1178 的赋值列表。漏一个的症状是界面上某个
        格子还留着上一个预设的值, 而用户看不出它没跟着换。"""
        state = self._state()
        preset = make_preset()
        self._apply(state, preset)

        self.assertEqual(state.model_path, "MODEL/night.onnx")
        self.assertEqual(state.provider, "CUDA")
        self.assertTrue(state.cuda_graph)
        self.assertTrue(state.gpu_preprocess)
        self.assertEqual(state.output_format, "YOLOv5")
        self.assertAlmostEqual(state.confidence, 0.31)
        self.assertAlmostEqual(state.iou, 0.55)
        self.assertEqual(state.input_mode, _label_for(INPUT_MODES, "udp_jpeg"))
        self.assertEqual(state.udp_host, "198.51.100.9")
        self.assertEqual(state.obs_host, "10.1.2.3")
        self.assertEqual(state.obs_source, "夜间画面")
        self.assertFalse(state.kmbox_enabled)
        self.assertEqual(state.kmbox_host, "10.1.1.1")
        self.assertEqual(state.desktop_backend, _label_for(DESKTOP_BACKENDS, "dxgi"))
        self.assertEqual(
            (state.desktop_monitor, state.desktop_width, state.desktop_height), ("2", "224", "200")
        )
        self.assertEqual(state.mouse_output, _label_for(MOUSE_OUTPUTS, "kmbox"))

    def test_obs_password_is_copied_verbatim(self) -> None:
        """跟保存那一侧一样: 空格可能是密码的一部分。"""
        state = self._state()
        self._apply(state, make_preset())
        self.assertEqual(state.obs_password, "  夜间密码  ")

    def test_uuid_is_not_upper_cased_on_the_way_in(self) -> None:
        """gui.py:1157 原样填。大写化是保存时做的 —— 在这里也做一遍, 界面显示的
        就跟预设文件里存的对不上了。"""
        state = self._state()
        self._apply(state, make_preset())
        self.assertEqual(state.kmbox_uuid, "ffee0011")

    def test_dropdown_fields_get_display_labels(self) -> None:
        """状态里存的是标签。存成 cuda / udp_jpeg / right 的话那些值根本不在
        下拉框的候选里, 框会显示成空的。"""
        state = self._state()
        self._apply(state, make_preset())
        for value, mapping in (
            (state.provider, PROVIDERS),
            (state.output_format, OUTPUT_FORMATS),
            (state.input_mode, INPUT_MODES),
            (state.profiles[0].trigger, TRIGGERS),
            (state.profiles[1].trigger, TRIGGERS),
            (state.profiles[0].algorithm, algorithm_choices()),
        ):
            with self.subTest(value=value):
                self.assertIn(value, mapping)

    def test_aim_position_is_a_percentage(self) -> None:
        """配置里是 0..1 的比例, 滑条是 0..100。对应 gui.py:1037。"""
        state = self._state()
        self._apply(state, make_preset())
        self.assertAlmostEqual(state.profiles[0].aim_position, 61.0)

    def test_both_profiles_are_replaced_from_the_matching_preset_profile(self) -> None:
        """两套方案各绑一组控件。填错顺序的症状是载入预设后两个方案的设置对调了。"""
        state = self._state()
        self._apply(state, make_preset())

        first, second = state.profiles
        self.assertFalse(first.enabled)
        self.assertEqual(first.trigger, _label_for(TRIGGERS, "right"))
        self.assertAlmostEqual(first.kp_min, 0.071)
        self.assertAlmostEqual(first.kp_max, 0.132)
        self.assertAlmostEqual(first.kp_growth, 0.088)
        self.assertAlmostEqual(first.fov, 88.0)

        self.assertTrue(second.enabled)
        self.assertEqual(second.trigger, _label_for(TRIGGERS, "side1"))
        self.assertEqual(second.target_class, "2")
        self.assertAlmostEqual(second.aim_position, 44.0)
        self.assertEqual(second.algorithm, _label_for(algorithm_choices(), "inflight"))
        self.assertEqual(second.algorithm_params, {"loop_delay_frames": 3.0})

    def test_the_preset_is_left_untouched(self) -> None:
        """预设对象是「有没有改动」那个标记的基准 (gui.py:1122 的 _preset_baseline),
        灌完表单还要拿它跟界面比。往它的参数 dict 上挂别名, 用户动一下参数滑条
        基准就跟着变, 标记永远不会亮。"""
        preset = make_preset()
        state = self._state()
        self._apply(state, preset)
        state.profiles[0].algorithm_params["gravity"] = 99.0
        self.assertEqual(preset.aim_profile_1.algorithm_params, {"gravity": 7.0})

    def test_applying_a_preset_matches_loading_the_same_settings_as_a_config(self) -> None:
        """预设覆盖的那些格子, 灌预设和从同一份配置开界面应该长得一模一样 ——
        两条路走岔了, 症状就是「保存成预设再载入回来, 界面变了」。"""
        preset = make_preset()
        config = replace(
            make_config(),
            model=preset.model,
            input=preset.input,
            udp=preset.udp,
            obs=preset.obs,
            kmbox=preset.kmbox,
            aim_profile_1=preset.aim_profile_1,
            aim_profile_2=preset.aim_profile_2,
        )
        from_config = config_to_form_state(config, CONFIG_DIR, LABELS, display_path=_display_path)

        state = self._state()
        self._apply(state, preset)

        self.assertEqual(state.profiles, from_config.profiles)
        for name in (
            "model_path",
            "provider",
            "cuda_graph",
            "gpu_preprocess",
            "output_format",
            "confidence",
            "iou",
            "input_mode",
            "udp_host",
            "udp_port",
            "udp_width",
            "udp_height",
            "obs_host",
            "obs_port",
            "obs_password",
            "obs_source",
            "kmbox_enabled",
            "kmbox_host",
            "kmbox_port",
            "kmbox_uuid",
        ):
            with self.subTest(field=name):
                self.assertEqual(getattr(state, name), getattr(from_config, name))


class PackageSurfaceTest(unittest.TestCase):
    def test_every_state_conversion_is_exported_from_the_package(self) -> None:
        """适配层 import 的是 rhodes_fast.gui_core, 不是它的子模块。少导出一个
        名字不会有任何测试红 —— 直到 gui.py 那边 ImportError, 而那时界面已经
        起不来了。"""
        import rhodes_fast.gui_core as gui_core

        for name in (
            "FormState",
            "Labels",
            "ProfileFormState",
            "apply_preset_to_state",
            "config_to_form_state",
            "form_state_to_config",
            "trail_settings_from_state",
        ):
            with self.subTest(name=name):
                self.assertIn(name, gui_core.__all__)
                self.assertTrue(hasattr(gui_core, name))

    def test_both_interfaces_share_one_copy_of_every_label_map(self) -> None:
        """五张 {显示标签: 存储值} 表只能有一份。两份的话, 将来加一种输入方式
        或者一个加速方式只会改到一边 —— 另一边不报错, 只是那个选项不出现在
        下拉框里, 而用户看不出为什么两个界面不一样。

        比的是 is 不是 ==: 相等的副本今天没症状, 那正是它危险的地方。
        INPUT_MODES 就以这种形状在 gui.py 里多活了好几个提交。
        """
        import rhodes_fast.gui as classic
        import rhodes_fast.gui_core.state as state

        for name in ("PROVIDERS", "OUTPUT_FORMATS", "INPUT_MODES", "LOG_LANGUAGES", "TRIGGERS"):
            with self.subTest(name=name):
                self.assertIs(getattr(classic, name), getattr(state, name))


if __name__ == "__main__":
    unittest.main()


class AlgorithmParamOrderTest(unittest.TestCase):
    """参数控件的上下顺序靠 algorithm_params 的迭代顺序撑着, 得有测试钉住。

    gui.py 的 _create_variables 直接遍历 FormState.algorithm_params 建控件,
    所以这个 dict 的顺序就是界面上参数从上到下的顺序。谁要是给 _param_defaults
    加一个 sorted(), 界面会悄悄变样而现有测试一条都不会红。
    """

    def test_params_keep_the_order_the_algorithm_declares_them_in(self) -> None:
        for stored in algorithm_choices().values():
            specs = algorithm_param_specs(stored)
            if len(specs) < 2:
                continue  # 少于两个参数谈不上顺序
            base = make_config(profile_1_algorithm_params={})
            config = replace(
                base,
                aim_profile_1=replace(base.aim_profile_1, algorithm=stored, algorithm_params={}),
            )
            state = config_to_form_state(
                config, Path("C:/app"), LABELS, display_path=_display_path
            )
            self.assertEqual(
                list(state.profiles[0].algorithm_params),
                [spec.name for spec in specs],
                f"{stored} 的参数顺序跟它 PARAMS 里声明的不一致",
            )

    def test_at_least_one_algorithm_has_enough_params_to_make_this_test_mean_something(
        self,
    ) -> None:
        """守卫: 全都只剩一个参数的话上面那条会静默退化成空循环。"""
        widest = max(len(algorithm_param_specs(s)) for s in algorithm_choices().values())
        self.assertGreaterEqual(widest, 2)


class RuntimeAimPayloadTest(unittest.TestCase):
    """热推给管线的那份 JSON。

    这是特征测试: 它不判断「应该」是什么, 只钉住 gui.py 的
    _write_runtime_aim_settings 原本产出的东西, 好让搬家这一步是可证的。
    管线那边按键名读, 改任何一个键名都是协议变更。
    """

    def _state(self) -> FormState:
        base = config_to_form_state(
            default_config(), CONFIG_DIR, LABELS, display_path=_display_path
        )
        first = replace(
            base.profiles[0],
            enabled=True,
            trigger="鼠标侧键 1",
            target_class="0",
            aim_position=38.0,
            fov=90.0,
            kp_min=0.02,
            kp_max=0.12,
            kp_growth=0.25,
            # 显示名从映射里反查, 不写死: 它是算法自己的 DISPLAY_NAME, 改一次
            # 文案就会让这条测试假红, 而它要钉的是「标签有没有被翻成标识」。
            algorithm=_label_for(LABELS.algorithm, "p"),
            algorithm_params=defaults_of("p"),
        )
        second = replace(base.profiles[1], enabled=False, trigger="鼠标右键")
        return replace(base, profiles=(first, second))

    def test_the_payload_keeps_the_keys_the_pipeline_reads(self) -> None:
        payload = runtime_aim_payload(self._state(), LABELS)
        self.assertEqual(
            sorted(payload), ["fov_radius", "profiles", "target_class", "target_y_ratio"]
        )
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertEqual(
            sorted(payload["profiles"][0]),
            [
                "algorithm",
                "algorithm_params",
                "enabled",
                "fov_radius",
                "kp_growth",
                "kp_max",
                "kp_min",
                "target_class",
                "target_y_ratio",
                "trigger",
            ],
        )

    def test_the_top_level_keys_mirror_profile_one(self) -> None:
        """gui.py 那句注释: Top-level keys mirror profile 1 for older runtime
        consumers。老的读取方只看顶层, 去掉就等于悄悄砍了向后兼容 —— 而只有
        老版本的管线才看得出来。"""
        payload = runtime_aim_payload(self._state(), LABELS)
        for key in ("target_class", "target_y_ratio", "fov_radius"):
            self.assertEqual(payload[key], payload["profiles"][0][key])

    def test_aim_position_is_a_percentage_on_the_form_and_a_ratio_in_the_payload(self) -> None:
        """表单上是 0-100 的百分比, 协议里是 0-1 的比例。漏了这一步准心会瞄到
        框外面去 —— 而 38 和 0.38 都是「看着挺合理」的数, 没人会怀疑。"""
        payload = runtime_aim_payload(self._state(), LABELS)
        self.assertAlmostEqual(payload["profiles"][0]["target_y_ratio"], 0.38)

    def test_the_ratio_is_clamped_to_zero_one(self) -> None:
        state = self._state()
        state = replace(
            state,
            profiles=(replace(state.profiles[0], aim_position=140.0), state.profiles[1]),
        )
        payload = runtime_aim_payload(state, LABELS)
        self.assertEqual(payload["profiles"][0]["target_y_ratio"], 1.0)

    def test_labels_are_translated_to_stored_values(self) -> None:
        """界面上存的是显示标签 (「鼠标侧键 1」), 协议里要的是标识 (side1)。
        直接把标签发过去的话管线一个触发键都认不出来, 而且不报错 —— 只是永远
        不开火。"""
        payload = runtime_aim_payload(self._state(), LABELS)
        self.assertEqual(payload["profiles"][0]["trigger"], "side1")
        self.assertEqual(payload["profiles"][0]["algorithm"], "p")

    def test_target_class_is_an_int_not_the_string_from_the_dropdown(self) -> None:
        payload = runtime_aim_payload(self._state(), LABELS)
        self.assertIsInstance(payload["profiles"][0]["target_class"], int)

    def test_both_profiles_are_carried_not_just_the_active_one(self) -> None:
        """两套方案是一起推的。只推启用的那套, 用户关掉方案 2 之后再打开, 管线
        用的还是上一次的参数。"""
        payload = runtime_aim_payload(self._state(), LABELS)
        self.assertIs(payload["profiles"][0]["enabled"], True)
        self.assertIs(payload["profiles"][1]["enabled"], False)

    def test_the_payload_is_json_serialisable(self) -> None:
        """它是要 json.dumps 进文件的。混进一个 tuple 或 Path 就当场炸在热推
        那条路上, 而那条路每动一下滑条走一次。"""
        json.dumps(runtime_aim_payload(self._state(), LABELS))
