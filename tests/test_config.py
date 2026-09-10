from __future__ import annotations

import tempfile
import unittest
import re
from dataclasses import asdict, replace
from pathlib import Path

import tomli_w

from rhodes_fast.config import default_config, load_config, save_config


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


if __name__ == "__main__":
    unittest.main()
