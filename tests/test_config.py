from __future__ import annotations

import tempfile
import unittest
import re
from dataclasses import asdict, replace
from pathlib import Path

import tomli_w

from rhodes_fast.config import load_config, save_config


class ConfigTests(unittest.TestCase):
    def test_udp_settings_round_trip(self) -> None:
        original = load_config(Path(__file__).parents[1] / "config.toml", validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.touch()
            changed = replace(
                original,
                input=replace(original.input, mode="udp_jpeg"),
                udp=replace(original.udp, host="127.0.0.1", port=55123),
                model=replace(original.model, path=model),
            )
            path = Path(directory) / "config.toml"
            save_config(changed, path)
            loaded = load_config(path)
        self.assertEqual(loaded.input.mode, "udp_jpeg")
        self.assertEqual(loaded.ui.language, "zh")
        self.assertEqual(loaded.udp.host, "127.0.0.1")
        self.assertEqual(loaded.udp.port, 55123)

    def test_text_settings_round_trip(self) -> None:
        original = load_config(Path(__file__).parents[1] / "settings.txt", validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.touch()
            original = replace(original, model=replace(original.model, path=model))
            path = Path(directory) / "settings.txt"
            save_config(original, path)
            saved = path.read_text(encoding="utf-8")
            loaded = load_config(path)
        self.assertEqual(loaded.input.mode, "udp_jpeg")
        self.assertEqual(loaded.model.path, original.model.path)
        self.assertTrue(loaded.model.cuda_graph)
        self.assertTrue(loaded.model.gpu_preprocess)
        self.assertEqual(loaded.kmbox.uuid, "")
        self.assertGreaterEqual(loaded.aim.target_class, 0)
        self.assertEqual(loaded.model.output_layout, "auto")
        self.assertTrue(saved.endswith("\n"))
        self.assertFalse(saved.endswith("\n\n"))
        self.assertFalse(any(line.endswith(" ") for line in saved.splitlines()))

    def test_invalid_settings_do_not_replace_existing_config(self) -> None:
        original = load_config(Path(__file__).parents[1] / "config.toml", validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.touch()
            original = replace(original, model=replace(original.model, path=model))
            invalid = replace(original, udp=replace(original.udp, port=70_000))
            path = Path(directory) / "config.toml"
            save_config(original, path)
            before = path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UDP port"):
                save_config(invalid, path)
            after = path.read_text(encoding="utf-8")
        self.assertEqual(after, before)

    def test_rejects_unknown_runtime_language(self) -> None:
        original = load_config(Path(__file__).parents[1] / "config.toml", validate_model=False)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.touch()
            invalid = replace(
                original,
                ui=replace(original.ui, language="fr"),
                model=replace(original.model, path=model),
            )
            path = Path(directory) / "settings.txt"
            with self.assertRaisesRegex(ValueError, "runtime language"):
                save_config(invalid, path)

    def test_old_fixed_gain_settings_migrate_to_dynamic_kp_defaults(self) -> None:
        current = (Path(__file__).parents[1] / "settings.txt").read_text(encoding="utf-8")
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
        self.assertEqual((loaded.aim.kp_min, loaded.aim.kp_max, loaded.aim.kp_growth), (0.1, 0.164, 0.167))

    def test_gui_can_load_settings_when_model_was_moved(self) -> None:
        original = load_config(Path(__file__).parents[1] / "config.toml", validate_model=False)
        moved = replace(original, model=replace(original.model, path=Path("missing.onnx")))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            raw_path = path.with_name(f".{path.name}.tmp")
            raw = asdict(moved)
            raw["model"]["path"] = "missing.onnx"
            raw_path.write_text(tomli_w.dumps(raw), encoding="utf-8")
            raw_path.replace(path)
            loaded = load_config(path, validate_model=False)
        self.assertEqual(loaded.model.path, Path("missing.onnx"))

    def test_old_settings_enable_cuda_graph_by_default(self) -> None:
        current = (Path(__file__).parents[1] / "config.toml").read_text(encoding="utf-8")
        old = re.sub(r"^cuda_graph = .*\n", "", current, flags=re.MULTILINE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        self.assertTrue(loaded.model.cuda_graph)

    def test_old_settings_enable_gpu_preprocess_by_default(self) -> None:
        current = (Path(__file__).parents[1] / "config.toml").read_text(encoding="utf-8")
        old = re.sub(r"^gpu_preprocess = .*\n", "", current, flags=re.MULTILINE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(old, encoding="utf-8")
            loaded = load_config(path, validate_model=False)
        self.assertTrue(loaded.model.gpu_preprocess)


if __name__ == "__main__":
    unittest.main()
