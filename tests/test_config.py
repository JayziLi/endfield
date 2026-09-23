from __future__ import annotations

import tempfile
import unittest
import re
from dataclasses import asdict, replace
from pathlib import Path

import tomli_w

from rhodes_fast.config import AimProfileConfig, default_config, load_config, save_config


_ROOT = Path(__file__).parents[1]
_TOML_CONFIG = _ROOT / "config.example.toml"
_TEXT_CONFIG = _ROOT / "settings.example.txt"


class ConfigTests(unittest.TestCase):
    def test_udp_settings_round_trip(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        changed = replace(
            original,
            input=replace(original.input, mode="udp_jpeg"),
            udp=replace(original.udp, host="127.0.0.1", port=55123),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save_config(changed, path)
            loaded = load_config(path, validate_model=False)
        self.assertEqual(loaded.input.mode, "udp_jpeg")
        self.assertEqual(loaded.ui.language, "zh")
        self.assertEqual(loaded.udp.host, "127.0.0.1")
        self.assertEqual(loaded.udp.port, 55123)

    def test_text_settings_round_trip(self) -> None:
        original = load_config(_TEXT_CONFIG, validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            save_config(original, path)
            saved = path.read_text(encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        # 逐字段比对整份配置, 而不是挑几个值硬编码。硬编码会跟着本机 settings.txt 漂:
        # 之前就因为本机启用了 aim_profile_2 而挂掉, 而那与 round-trip 是否正确无关。
        self.assertEqual(asdict(loaded), asdict(original))
        self.assertEqual(loaded.model.output_layout, "auto")
        self.assertTrue(saved.endswith("\n"))
        self.assertFalse(saved.endswith("\n\n"))
        self.assertFalse(any(line.endswith(" ") for line in saved.splitlines()))

    def test_invalid_settings_do_not_replace_existing_config(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        invalid = replace(original, udp=replace(original.udp, port=70_000))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save_config(original, path)
            before = path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UDP port"):
                save_config(invalid, path)
            after = path.read_text(encoding="utf-8")
        self.assertEqual(after, before)

    def test_rejects_unknown_runtime_language(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        invalid = replace(original, ui=replace(original.ui, language="fr"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            with self.assertRaisesRegex(ValueError, "runtime language"):
                save_config(invalid, path)

    def test_old_fixed_gain_settings_migrate_to_dynamic_kp_defaults(self) -> None:
        current = _TEXT_CONFIG.read_text(encoding="utf-8")
        old = re.sub(
            r"kp_min = .*\nkp_max = .*\nkp_growth = .*",
            "gain_x = 0.16\ngain_y = 0.16",
            current,
            count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        self.assertEqual(
            (loaded.aim_profile_1.kp_min, loaded.aim_profile_1.kp_max, loaded.aim_profile_1.kp_growth),
            (0.1, 0.164, 0.167),
        )

    def test_old_single_profile_settings_migrate_without_enabling_a_second_trigger(self) -> None:
        current = _TEXT_CONFIG.read_text(encoding="utf-8")
        old = re.sub(r"\n\[aim_profile_1\][\s\S]*", "", current)
        old = old.replace(
            "fov_radius = 150.0",
            "fov_radius = 150.0\nenabled = True\ntrigger = side1\nkp_min = 0.05\nkp_max = 0.08\nkp_growth = 0.06",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)

        self.assertTrue(loaded.aim_profile_1.enabled)
        self.assertEqual(loaded.aim_profile_1.trigger, "side1")
        self.assertEqual(loaded.aim_profile_1.kp_min, 0.05)
        self.assertFalse(loaded.aim_profile_2.enabled)
        self.assertNotEqual(loaded.aim_profile_1.trigger, loaded.aim_profile_2.trigger)

    def test_profiles_inherit_shared_target_settings_from_old_files(self) -> None:
        current = _TEXT_CONFIG.read_text(encoding="utf-8")
        old = re.sub(
            r"(\[aim_profile_\d\][\s\S]*?)(?=\n\[|\Z)",
            lambda match: re.sub(r"\n(target_class|target_y_ratio|fov_radius) = [^\n]*", "", match.group(1)),
            current,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        for profile in (loaded.aim_profile_1, loaded.aim_profile_2):
            self.assertEqual(profile.target_class, loaded.aim.target_class)
            self.assertEqual(profile.target_y_ratio, loaded.aim.target_y_ratio)
            self.assertEqual(profile.fov_radius, loaded.aim.fov_radius)

    def test_per_profile_target_settings_round_trip(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        changed = replace(
            original,
            aim_profile_1=replace(original.aim_profile_1, target_class=3, target_y_ratio=0.25, fov_radius=90.0),
            aim_profile_2=replace(original.aim_profile_2, target_class=5, target_y_ratio=0.6, fov_radius=200.0),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save_config(changed, path)
            loaded = load_config(path, validate_model=False)
        self.assertEqual(loaded.aim_profile_1.target_class, 3)
        self.assertEqual(loaded.aim_profile_1.target_y_ratio, 0.25)
        self.assertEqual(loaded.aim_profile_1.fov_radius, 90.0)
        self.assertEqual(loaded.aim_profile_2.target_class, 5)
        self.assertEqual(loaded.aim_profile_2.target_y_ratio, 0.6)
        self.assertEqual(loaded.aim_profile_2.fov_radius, 200.0)

    def test_rejects_invalid_profile_target_settings(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        invalid = replace(
            original,
            aim_profile_1=replace(original.aim_profile_1, target_y_ratio=1.5),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            with self.assertRaisesRegex(ValueError, "Aim position"):
                save_config(invalid, path)

    def test_rejects_duplicate_profile_triggers(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        duplicate = replace(
            original,
            aim_profile_2=replace(original.aim_profile_2, trigger=original.aim_profile_1.trigger),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            with self.assertRaisesRegex(ValueError, "different trigger"):
                save_config(duplicate, path)

    def test_gui_can_load_settings_when_model_was_moved(self) -> None:
        original = load_config(_TOML_CONFIG, validate_model=False)
        moved = replace(original, model=replace(original.model, path=Path("missing.onnx")))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            raw_path = path.with_name(f".{path.name}.tmp")
            raw = asdict(moved)
            raw["model"]["path"] = "missing.onnx"
            raw_path.write_text(tomli_w.dumps(raw), encoding="utf-8")
            raw_path.replace(path)
            loaded = load_config(path, validate_model=False)
        self.assertEqual(loaded.model.path, (path.parent / "missing.onnx").resolve())

    def test_relative_model_path_is_resolved_from_the_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.txt"
            path.write_text(_TEXT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            loaded = load_config(path, validate_model=False)

        self.assertEqual(loaded.model.path, (root / "models" / "yolov5n.onnx").resolve())

    def test_default_config_is_safe_for_a_first_run(self) -> None:
        config = default_config()

        self.assertEqual(config.input.mode, "udp_video")
        self.assertEqual(config.model.provider, "auto")
        self.assertFalse(config.kmbox.enabled)
        self.assertEqual(config.kmbox.uuid, "")

    def test_old_settings_enable_cuda_graph_by_default(self) -> None:
        current = _TOML_CONFIG.read_text(encoding="utf-8")
        old = re.sub(r"^cuda_graph = .*\n", "", current, flags=re.MULTILINE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        self.assertTrue(loaded.model.cuda_graph)

    def test_old_settings_enable_gpu_preprocess_by_default(self) -> None:
        current = _TOML_CONFIG.read_text(encoding="utf-8")
        old = re.sub(r"^gpu_preprocess = .*\n", "", current, flags=re.MULTILINE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        self.assertTrue(loaded.model.gpu_preprocess)

    def test_algorithm_selection_round_trips_through_both_formats(self) -> None:
        # settings.txt 是 INI, config.toml 是 TOML, 两条存储路径完全不同。
        # 参数字典在 INI 里只能是一行 JSON 字符串, 很容易只修好一边。
        # 用随仓库分发的示例配置, 不要读本机的 settings.txt / config.toml——
        # 那两个文件是个人运行时配置, 没被 git 跟踪, 而且指向本机的模型路径。
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                original = load_config(source, validate_model=False)
                changed = replace(
                    original,
                    aim_profile_1=replace(
                        original.aim_profile_1,
                        algorithm="inflight_ff",
                        algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0},
                    ),
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / source.name
                    save_config(changed, path)
                    loaded = load_config(path, validate_model=False)
                self.assertEqual(loaded.aim_profile_1.algorithm, "inflight_ff")
                self.assertEqual(
                    loaded.aim_profile_1.algorithm_params,
                    {"loop_delay_frames": 8.0, "gain": 1.0},
                )

    def test_the_last_used_preset_round_trips_through_both_formats(self) -> None:
        # 井号、分号、等号、百分号在 INI 里都有特殊含义的前科, 名字里允许出现。
        name = "Apex 高敏 #1; a=b 100%"
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                original = load_config(source, validate_model=False)
                self.assertEqual(original.ui.preset, "")
                changed = replace(original, ui=replace(original.ui, preset=name))
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / source.name
                    save_config(changed, path)
                    loaded = load_config(path, validate_model=False)
                self.assertEqual(loaded.ui.preset, name)

    def test_the_trail_length_round_trips_through_both_formats(self) -> None:
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                original = load_config(source, validate_model=False)
                self.assertEqual(original.ui.trail_seconds, 0.5)
                changed = replace(original, ui=replace(original.ui, trail_seconds=1.3))
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / source.name
                    save_config(changed, path)
                    loaded = load_config(path, validate_model=False)
                self.assertEqual(loaded.ui.trail_seconds, 1.3)

    def test_whether_the_log_is_folded_round_trips_through_both_formats(self) -> None:
        """折叠运行日志是为了把高度让给预览画面。不记住的话, 每次打开都要再折一次
        —— 而这正是用户要它能折的原因。"""
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                original = load_config(source, validate_model=False)
                self.assertIs(original.ui.log_collapsed, False, "默认展开")
                changed = replace(original, ui=replace(original.ui, log_collapsed=True))
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / source.name
                    save_config(changed, path)
                    loaded = load_config(path, validate_model=False)
                self.assertIs(loaded.ui.log_collapsed, True)

    def test_the_trail_checkboxes_are_not_remembered(self) -> None:
        # 打开程序时总是只勾「画面」, 所以勾选状态不存。上一个版本存过这两个键,
        # 那样写出来的文件必须照常能读。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            text = _TEXT_CONFIG.read_text(encoding="utf-8")
            text = text.replace("[ui]\n", "[ui]\ntrail_enabled = True\ntrail_optimal_path = True\n", 1)
            self.assertIn("trail_enabled = True", text)
            path.write_text(text, encoding="utf-8")

            loaded = load_config(path, validate_model=False)
            save_config(loaded, path)
            saved = path.read_text(encoding="utf-8")

        self.assertFalse(hasattr(loaded.ui, "trail_enabled"))
        self.assertNotIn("trail_enabled", saved)
        self.assertNotIn("trail_optimal_path", saved)

    def test_a_trail_length_outside_the_slider_range_is_rejected(self) -> None:
        original = load_config(_TEXT_CONFIG, validate_model=False)
        for seconds in (0.1, 2.5):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.txt"
                with self.assertRaises(ValueError):
                    save_config(replace(original, ui=replace(original.ui, trail_seconds=seconds)), path)

    def test_a_profile_without_an_algorithm_defaults_to_p(self) -> None:
        # 升级前写出的配置文件里没有这两个键, 必须当成现状算法而不是报错。
        self.assertEqual(AimProfileConfig().algorithm, "p")
        self.assertEqual(AimProfileConfig().algorithm_params, {})


class SinglePcSettingsTests(unittest.TestCase):
    """单机模式加了两节: [desktop] 管本机屏幕采集, [mouse] 管移动由谁发。"""

    def test_files_from_before_the_upgrade_keep_todays_behaviour(self) -> None:
        """升级后打不开 settings.txt 是最糟的情况; 读得开但换了行为是第二糟 ——
        老文件没有 [mouse], 读进来必须还是 KMBox。"""
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                loaded = load_config(source, validate_model=False)
                self.assertEqual(loaded.mouse.output, "kmbox")
                self.assertEqual(
                    (loaded.desktop.backend, loaded.desktop.monitor, loaded.desktop.width, loaded.desktop.height),
                    ("dxgi", 0, 320, 320),
                )

    def test_desktop_and_mouse_round_trip_through_both_formats(self) -> None:
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                original = load_config(source, validate_model=False)
                changed = replace(
                    original,
                    input=replace(original.input, mode="desktop"),
                    desktop=replace(original.desktop, backend="winrt", monitor=1, width=256, height=192),
                    mouse=replace(original.mouse, output="sendinput"),
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / source.name
                    save_config(changed, path)
                    loaded = load_config(path, validate_model=False)
                self.assertEqual(loaded.input.mode, "desktop")
                self.assertEqual(loaded.desktop, changed.desktop)
                self.assertEqual(loaded.mouse.output, "sendinput")

    def test_sendinput_leaves_the_kmbox_switch_alone(self) -> None:
        """两个设置互不干扰: kmbox.enabled 只管 KMBox, 选 SendInput 不去改它。"""
        original = default_config()
        self.assertIs(original.kmbox.enabled, False)
        self.assertEqual(original.mouse.output, "kmbox")

    def test_invalid_desktop_and_mouse_settings_are_rejected(self) -> None:
        original = load_config(_TEXT_CONFIG, validate_model=False)
        cases = {
            "unknown backend": replace(original, desktop=replace(original.desktop, backend="gdi")),
            "negative monitor": replace(original, desktop=replace(original.desktop, monitor=-1)),
            "zero width": replace(original, desktop=replace(original.desktop, width=0)),
            "negative height": replace(original, desktop=replace(original.desktop, height=-5)),
            "unknown output": replace(original, mouse=replace(original.mouse, output="arduino")),
        }
        for label, invalid in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    save_config(invalid, Path(directory) / "settings.txt")


if __name__ == "__main__":
    unittest.main()
