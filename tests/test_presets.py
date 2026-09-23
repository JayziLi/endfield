from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from rhodes_fast.config import AimProfileConfig, AppConfig, default_config
from rhodes_fast.presets import (
    PresetError,
    delete_preset,
    dump_preset,
    find_preset,
    list_presets,
    parse_preset,
    preset_from_config,
    read_preset,
    same_settings,
    validate_name,
    write_preset,
)


def _config(base: Path) -> AppConfig:
    model = base / "MODEL" / "game.onnx"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"not really onnx")
    config = default_config()
    return replace(
        config,
        model=replace(
            config.model,
            path=model,
            provider="tensorrt",
            cuda_graph=False,
            gpu_preprocess=True,
            output_format="yolov8",
            confidence=0.42,
            iou=0.61,
        ),
        input=replace(config.input, mode="obs_websocket"),
        udp=replace(config.udp, host="192.0.2.164", port=4466, width=416, height=384, fifo_packets=99),
        obs=replace(config.obs, host="10.0.0.5", port=4460, password="obs-pass", source_name="游戏画面"),
        kmbox=replace(config.kmbox, enabled=True, host="10.9.8.7", port=8810, uuid="ABCD1234", monitor_port=6001),
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
            algorithm="feedforward_bezier",
            algorithm_params={"loop_delay_frames": 8.0, "arc_strength": 0.3},
        ),
        aim_profile_2=AimProfileConfig(
            enabled=False,
            trigger="right",
            kp_min=0.062,
            kp_max=0.113,
            kp_growth=0.058,
            target_class=1,
            target_y_ratio=0.17,
            fov_radius=149.0,
            algorithm="p",
            algorithm_params={},
        ),
    )


class _TempBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)
        self.directory = self.base / "presets"
        self.preset = preset_from_config(_config(self.base))

    def tearDown(self) -> None:
        self._temporary.cleanup()


class RoundTripTests(_TempBase):
    def test_everything_a_game_needs_survives_a_round_trip(self) -> None:
        loaded = parse_preset(dump_preset(self.preset, self.base), self.base)
        self.assertEqual(loaded.model.path, self.preset.model.path.resolve())
        for key in ("provider", "cuda_graph", "gpu_preprocess", "output_format", "confidence", "iou"):
            self.assertEqual(getattr(loaded.model, key), getattr(self.preset.model, key), key)
        # 两个方案整条比: 触发键、启用、标签、算法参数都在里面。
        self.assertEqual(loaded.aim_profile_1, self.preset.aim_profile_1)
        self.assertEqual(loaded.aim_profile_2, self.preset.aim_profile_2)

    def test_input_and_kmbox_survive_a_round_trip(self) -> None:
        loaded = parse_preset(dump_preset(self.preset, self.base), self.base)
        self.assertEqual(loaded.input.mode, "obs_websocket")
        for section, keys in (
            ("udp", ("host", "port", "width", "height")),
            ("obs", ("host", "port", "password", "source_name")),
            ("kmbox", ("enabled", "host", "port", "uuid")),
            ("desktop", ("backend", "monitor", "width", "height")),
            ("mouse", ("output",)),
        ):
            for key in keys:
                with self.subTest(section=section, key=key):
                    self.assertEqual(
                        getattr(getattr(loaded, section), key), getattr(getattr(self.preset, section), key)
                    )

    def test_only_what_the_window_can_edit_is_stored(self) -> None:
        # 超时、缓冲区这些界面上没有的高级项留在 settings.txt, 不跟着预设来回换。
        payload = json.loads(dump_preset(self.preset, self.base))
        self.assertEqual(
            set(payload), {"format", "model", "input", "udp", "obs", "kmbox", "desktop", "mouse", "aim_profiles"}
        )
        self.assertEqual(set(payload["input"]), {"mode"})
        self.assertEqual(set(payload["udp"]), {"host", "port", "width", "height"})
        self.assertEqual(set(payload["obs"]), {"host", "port", "password", "source_name"})
        self.assertEqual(set(payload["kmbox"]), {"enabled", "host", "port", "uuid"})
        self.assertEqual(set(payload["desktop"]), {"backend", "monitor", "width", "height"})
        self.assertEqual(set(payload["mouse"]), {"output"})

    def test_presets_saved_before_the_single_pc_mode_still_load(self) -> None:
        """老预设里没有 desktop 和 mouse 两节。升级之后打不开存好的预设, 等于把
        用户攒下的每一套游戏配置都作废了。缺了就按默认值: 移动还是 KMBox。"""
        payload = json.loads(dump_preset(self.preset, self.base))
        payload.pop("desktop")
        payload.pop("mouse")
        loaded = parse_preset(json.dumps(payload), self.base)
        self.assertEqual(loaded.mouse.output, "kmbox")
        self.assertEqual(loaded.desktop.backend, "dxgi")
        self.assertEqual((loaded.desktop.width, loaded.desktop.height), (320, 320))

    def test_the_model_path_is_stored_relative_to_the_program_folder(self) -> None:
        payload = json.loads(dump_preset(self.preset, self.base))
        self.assertEqual(payload["model"]["path"], "MODEL/game.onnx")

    def test_moving_the_program_folder_keeps_the_preset_working(self) -> None:
        text = dump_preset(self.preset, self.base)
        with tempfile.TemporaryDirectory() as elsewhere:
            loaded = parse_preset(text, Path(elsewhere))
        self.assertEqual(loaded.model.path, (Path(elsewhere) / "MODEL" / "game.onnx").resolve())

    def test_a_model_outside_the_program_folder_stays_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "far.onnx"
            preset = replace(self.preset, model=replace(self.preset.model, path=outside))
            payload = json.loads(dump_preset(preset, self.base))
        self.assertTrue(Path(payload["model"]["path"]).is_absolute())


class ParseRejectsTests(_TempBase):
    def _payload(self) -> dict:
        return json.loads(dump_preset(self.preset, self.base))

    def _assert_rejected(self, payload: object, **kwargs) -> None:
        with self.assertRaises(PresetError):
            parse_preset(json.dumps(payload), self.base, **kwargs)

    def test_text_that_is_not_json(self) -> None:
        with self.assertRaises(PresetError):
            parse_preset("definitely not json", self.base)

    def test_broken_payloads(self) -> None:
        def edit(change):
            payload = self._payload()
            change(payload)
            return payload

        cases = {
            "top level list": [],
            "unknown format": edit(lambda p: p.update(format=99)),
            "no model": edit(lambda p: p.pop("model")),
            "no profiles": edit(lambda p: p.pop("aim_profiles")),
            "one profile": edit(lambda p: p["aim_profiles"].pop()),
            "no trigger": edit(lambda p: p["aim_profiles"][0].pop("trigger")),
            "no model path": edit(lambda p: p["model"].pop("path")),
            "unknown provider": edit(lambda p: p["model"].update(provider="quantum")),
            "unknown output format": edit(lambda p: p["model"].update(output_format="yolov99")),
            "confidence above one": edit(lambda p: p["model"].update(confidence=1.5)),
            "cuda graph as text": edit(lambda p: p["model"].update(cuda_graph="yes")),
            "enabled as number": edit(lambda p: p["aim_profiles"][0].update(enabled=1)),
            "fractional class": edit(lambda p: p["aim_profiles"][0].update(target_class=1.5)),
            "boolean class": edit(lambda p: p["aim_profiles"][0].update(target_class=True)),
            "negative class": edit(lambda p: p["aim_profiles"][0].update(target_class=-1)),
            "kp as text": edit(lambda p: p["aim_profiles"][0].update(kp_max="fast")),
            "param as text": edit(lambda p: p["aim_profiles"][0]["algorithm_params"].update(gain="x")),
            "params as list": edit(lambda p: p["aim_profiles"][0].update(algorithm_params=[1, 2])),
            "kp min above max": edit(lambda p: p["aim_profiles"][0].update(kp_min=0.9, kp_max=0.1)),
            "same trigger twice": edit(lambda p: p["aim_profiles"][1].update(trigger="side2")),
            "unknown trigger": edit(lambda p: p["aim_profiles"][0].update(trigger="nose")),
            "empty algorithm": edit(lambda p: p["aim_profiles"][0].update(algorithm="")),
            "no input": edit(lambda p: p.pop("input")),
            "no kmbox": edit(lambda p: p.pop("kmbox")),
            "unknown input mode": edit(lambda p: p["input"].update(mode="carrier_pigeon")),
            "udp port zero": edit(lambda p: p["udp"].update(port=0)),
            "udp port too big": edit(lambda p: p["udp"].update(port=70000)),
            "udp port as text": edit(lambda p: p["udp"].update(port="4455")),
            "udp width zero": edit(lambda p: p["udp"].update(width=0)),
            "udp host missing": edit(lambda p: p["udp"].pop("host")),
            "obs port as boolean": edit(lambda p: p["obs"].update(port=True)),
            "obs password missing": edit(lambda p: p["obs"].pop("password")),
            "kmbox enabled as text": edit(lambda p: p["kmbox"].update(enabled="yes")),
            "kmbox port fractional": edit(lambda p: p["kmbox"].update(port=8808.5)),
            "kmbox uuid as number": edit(lambda p: p["kmbox"].update(uuid=1234)),
            "desktop as list": edit(lambda p: p.update(desktop=[1])),
            "unknown desktop backend": edit(lambda p: p["desktop"].update(backend="gdi")),
            "negative monitor": edit(lambda p: p["desktop"].update(monitor=-1)),
            "monitor as text": edit(lambda p: p["desktop"].update(monitor="0")),
            "desktop width zero": edit(lambda p: p["desktop"].update(width=0)),
            "mouse as text": edit(lambda p: p.update(mouse="kmbox")),
            "unknown mouse output": edit(lambda p: p["mouse"].update(output="arduino")),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self._assert_rejected(payload)

    def test_not_a_number_is_not_a_number(self) -> None:
        # json.loads 认 NaN 和 Infinity 这两个词, 所以必须自己挡。放在没有范围检查的
        # 算法参数和视野上: NaN 跟任何数比较都是 False, 范围检查挡不住它。
        original = dump_preset(self.preset, self.base)
        for old, new in (
            ('"arc_strength": 0.3', '"arc_strength": NaN'),
            ('"fov_radius": 122.0', '"fov_radius": Infinity'),
        ):
            with self.subTest(new):
                text = original.replace(old, new)
                self.assertNotEqual(text, original)
                with self.assertRaises(PresetError):
                    parse_preset(text, self.base)

    def test_an_algorithm_that_is_not_installed(self) -> None:
        # 静默换成默认算法会让人以为在用这份预设, 实际手感是另一回事。
        with self.assertRaises(PresetError) as caught:
            parse_preset(dump_preset(self.preset, self.base), self.base, known_algorithms={"p"})
        self.assertIn("feedforward_bezier", str(caught.exception))

    def test_known_algorithms_pass(self) -> None:
        parse_preset(
            dump_preset(self.preset, self.base),
            self.base,
            known_algorithms={"p", "feedforward_bezier"},
        )

    def test_dump_refuses_what_it_could_not_read_back(self) -> None:
        broken = replace(self.preset, aim_profile_2=replace(self.preset.aim_profile_2, trigger="side2"))
        with self.assertRaises(PresetError):
            dump_preset(broken, self.base)


class NameTests(unittest.TestCase):
    def test_surrounding_spaces_are_trimmed(self) -> None:
        self.assertEqual(validate_name("  终末地-日常 "), "终末地-日常")

    def test_rejected_names(self) -> None:
        for name in ("", "   ", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "tab\there",
                     "CON", "con", "Com1", "nul.backup", "lpt9", "trailing.", "x" * 41):
            with self.subTest(name=name):
                with self.assertRaises(PresetError):
                    validate_name(name)

    def test_ordinary_names_pass(self) -> None:
        for name in ("终末地-日常", "Apex 高敏", "console", "com10", "v1.2", "x" * 40):
            with self.subTest(name=name):
                self.assertEqual(validate_name(name), name)


class StoreTests(_TempBase):
    def test_a_missing_directory_has_no_presets(self) -> None:
        self.assertEqual(list_presets(self.directory), [])

    def test_saved_presets_are_listed_in_name_order(self) -> None:
        for name in ("beta", "Alpha", "终末地"):
            write_preset(self.directory, name, self.preset, base_directory=self.base)
        self.assertEqual(list_presets(self.directory), ["Alpha", "beta", "终末地"])

    def test_other_files_in_the_folder_are_not_presets(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        (self.directory / "notes.txt").write_text("hi", encoding="utf-8")
        (self.directory / ".half.tmp").write_text("{", encoding="utf-8")
        self.assertEqual(list_presets(self.directory), ["real"])

    def test_writing_leaves_no_temporary_file(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["real.json"])

    def test_a_saved_preset_reads_back_the_same(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        loaded = read_preset(self.directory, "real", base_directory=self.base)
        self.assertTrue(same_settings(loaded, self.preset))

    def test_the_stored_name_is_the_trimmed_one(self) -> None:
        stored = write_preset(self.directory, "  real  ", self.preset, base_directory=self.base)
        self.assertEqual(stored, "real")
        self.assertEqual(list_presets(self.directory), ["real"])

    def test_saving_again_replaces_the_content(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        changed = replace(self.preset, model=replace(self.preset.model, confidence=0.9))
        write_preset(self.directory, "real", changed, base_directory=self.base)
        loaded = read_preset(self.directory, "real", base_directory=self.base)
        self.assertEqual(loaded.model.confidence, 0.9)

    def test_a_preset_without_its_model_file_is_not_saved(self) -> None:
        missing = replace(self.preset, model=replace(self.preset.model, path=self.base / "gone.onnx"))
        with self.assertRaises(PresetError):
            write_preset(self.directory, "real", missing, base_directory=self.base)
        self.assertEqual(list_presets(self.directory), [])

    def test_a_bad_name_is_not_saved(self) -> None:
        with self.assertRaises(PresetError):
            write_preset(self.directory, "a/b", self.preset, base_directory=self.base)
        self.assertEqual(list_presets(self.directory), [])

    def test_names_match_regardless_of_case(self) -> None:
        write_preset(self.directory, "Apex", self.preset, base_directory=self.base)
        self.assertEqual(find_preset(self.directory, "apex"), "Apex")
        self.assertEqual(find_preset(self.directory, " APEX "), "Apex")
        self.assertIsNone(find_preset(self.directory, "apex2"))

    def test_saving_under_another_case_leaves_one_file_with_the_new_name(self) -> None:
        write_preset(self.directory, "Apex", self.preset, base_directory=self.base)
        write_preset(self.directory, "apex", self.preset, base_directory=self.base)
        self.assertEqual(list_presets(self.directory), ["apex"])

    def test_reading_a_preset_that_does_not_exist(self) -> None:
        with self.assertRaises(PresetError):
            read_preset(self.directory, "ghost", base_directory=self.base)

    def test_reading_a_preset_whose_model_is_gone(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        self.preset.model.path.unlink()
        with self.assertRaises(PresetError) as caught:
            read_preset(self.directory, "real", base_directory=self.base)
        self.assertIn("game.onnx", str(caught.exception))
        # 只拿来比较有没有改动时, 模型不在也照样读。
        read_preset(self.directory, "real", base_directory=self.base, require_model=False)

    def test_reading_checks_the_algorithm_is_installed(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        with self.assertRaises(PresetError):
            read_preset(self.directory, "real", base_directory=self.base, known_algorithms={"p"})

    def test_delete_removes_the_file(self) -> None:
        write_preset(self.directory, "real", self.preset, base_directory=self.base)
        write_preset(self.directory, "other", self.preset, base_directory=self.base)
        delete_preset(self.directory, "real")
        self.assertEqual(list_presets(self.directory), ["other"])

    def test_deleting_twice_is_harmless(self) -> None:
        delete_preset(self.directory, "never-existed")


class SameSettingsTests(_TempBase):
    def _changed(self, profile: int = 1, **changes):
        field = f"aim_profile_{profile}"
        return replace(self.preset, **{field: replace(getattr(self.preset, field), **changes)})

    def test_identical_presets_match(self) -> None:
        self.assertTrue(same_settings(self.preset, self.preset))

    def test_floating_point_tails_from_the_percentage_slider_still_match(self) -> None:
        # 框内位置界面上是百分比: 导入调校带来的 0.029 乘 100 再除 100 就回不去了。
        saved = self._changed(target_y_ratio=0.029)
        through_slider = (0.029 * 100.0) / 100.0
        self.assertNotEqual(through_slider, 0.029)
        self.assertTrue(same_settings(saved, self._changed(target_y_ratio=through_slider)))

    def test_real_differences_are_noticed(self) -> None:
        model = self.preset.model
        cases = {
            "kp": self._changed(kp_max=0.057),
            "trigger": self._changed(trigger="left"),
            "enabled": self._changed(enabled=False),
            "class": self._changed(target_class=4),
            "algorithm": self._changed(algorithm="pd"),
            "param value": self._changed(algorithm_params={"loop_delay_frames": 9.0, "arc_strength": 0.3}),
            "param added": self._changed(
                algorithm_params={"loop_delay_frames": 8.0, "arc_strength": 0.3, "seed": 1.0}
            ),
            "second profile": self._changed(profile=2, fov_radius=150.0),
            "model file": replace(self.preset, model=replace(model, path=self.base / "other.onnx")),
            "provider": replace(self.preset, model=replace(model, provider="cuda")),
            "cuda graph": replace(self.preset, model=replace(model, cuda_graph=True)),
            "confidence": replace(self.preset, model=replace(model, confidence=0.43)),
            "input mode": replace(self.preset, input=replace(self.preset.input, mode="udp_jpeg")),
            "udp host": replace(self.preset, udp=replace(self.preset.udp, host="192.0.2.165")),
            "udp size": replace(self.preset, udp=replace(self.preset.udp, width=320)),
            "obs password": replace(self.preset, obs=replace(self.preset.obs, password="other")),
            "obs source": replace(self.preset, obs=replace(self.preset.obs, source_name="桌面")),
            "kmbox enabled": replace(self.preset, kmbox=replace(self.preset.kmbox, enabled=False)),
            "kmbox port": replace(self.preset, kmbox=replace(self.preset.kmbox, port=8808)),
            "kmbox uuid": replace(self.preset, kmbox=replace(self.preset.kmbox, uuid="FFFF0000")),
            "desktop backend": replace(self.preset, desktop=replace(self.preset.desktop, backend="dxgi")),
            "desktop monitor": replace(self.preset, desktop=replace(self.preset.desktop, monitor=0)),
            "desktop size": replace(self.preset, desktop=replace(self.preset.desktop, height=320)),
            "mouse output": replace(self.preset, mouse=replace(self.preset.mouse, output="kmbox")),
        }
        for label, other in cases.items():
            with self.subTest(label):
                self.assertFalse(same_settings(self.preset, other))
                self.assertFalse(same_settings(other, self.preset))

    def test_settings_the_window_cannot_edit_are_not_compared(self) -> None:
        # 预设里本来就不存它们, 比了只会让标记永远亮着。
        other = replace(
            self.preset,
            udp=replace(self.preset.udp, fifo_packets=1),
            kmbox=replace(self.preset.kmbox, monitor_port=1),
        )
        self.assertTrue(same_settings(self.preset, other))


if __name__ == "__main__":
    unittest.main()
