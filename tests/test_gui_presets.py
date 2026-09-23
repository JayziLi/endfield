from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, simpledialog
from unittest import mock

from rhodes_fast.config import AimProfileConfig, load_config, save_config
from rhodes_fast.gui import NO_PRESET, RhodesFastGui
from rhodes_fast.presets import Preset, preset_from_config, read_preset, same_settings, write_preset

_EXAMPLE = Path(__file__).resolve().parents[1] / "settings.example.txt"


class _PresetGui(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        models = self.base / "models"
        models.mkdir()
        for name in ("yolov5n.onnx", "other.onnx"):
            (models / name).write_bytes(b"not really onnx")
        self.settings = self.base / "settings.txt"
        shutil.copy(_EXAMPLE, self.settings)
        # 真去检查模型会起线程加载 onnxruntime; 这里只关心有没有要求检查。
        inspect = mock.patch.object(RhodesFastGui, "_inspect_selected_model")
        self.inspect = inspect.start()
        self.addCleanup(inspect.stop)
        self.app = self._open()

    def _open(self) -> RhodesFastGui:
        app = RhodesFastGui(self.settings)
        self.addCleanup(self._close, app)
        app.root.update_idletasks()
        return app

    def _reopen(self) -> RhodesFastGui:
        # 两个 Tk 同时活着的话, 第二个界面的变量会挂到第一个解释器上。
        self._close(self.app)
        self.app = self._open()
        return self.app

    @staticmethod
    def _close(app: RhodesFastGui) -> None:
        try:
            app.root.destroy()
        except Exception:
            pass

    @property
    def directory(self) -> Path:
        return self.base / "presets"

    def _save_as(self, name: str, app: RhodesFastGui | None = None) -> bool:
        with mock.patch.object(simpledialog, "askstring", return_value=name):
            return (app or self.app)._save_preset_as()

    def _form(self) -> Preset:
        return preset_from_config(self.app._read_form())

    def _read(self, name: str) -> Preset:
        return read_preset(self.directory, name, base_directory=self.base)

    def _other_game(self) -> Preset:
        base = self._form()
        return Preset(
            model=replace(
                base.model,
                path=(self.base / "models" / "other.onnx").resolve(),
                provider="cuda",
                cuda_graph=False,
                gpu_preprocess=False,
                output_format="yolov8",
                confidence=0.42,
                iou=0.61,
            ),
            input=replace(base.input, mode="obs_websocket"),
            udp=replace(base.udp, host="192.0.2.164", port=4466, width=416, height=384),
            obs=replace(base.obs, host="10.0.0.5", port=4460, password="obs-pass", source_name="游戏画面"),
            kmbox=replace(base.kmbox, enabled=True, host="10.9.8.7", port=8810, uuid="ABCD1234"),
            aim_profile_1=AimProfileConfig(
                enabled=True,
                trigger="side2",
                kp_min=0.028,
                kp_max=0.056,
                kp_growth=0.045,
                target_class=3,
                target_y_ratio=0.029,
                fov_radius=122.0,
                algorithm="feedforward",
                algorithm_params={"loop_delay_frames": 8.0, "gain": 1.2, "velocity_smoothing": 0.3},
            ),
            aim_profile_2=AimProfileConfig(
                enabled=False,
                trigger="side1",
                kp_min=0.062,
                kp_max=0.113,
                kp_growth=0.058,
                target_class=1,
                target_y_ratio=0.17,
                fov_radius=149.0,
                algorithm="pd",
                algorithm_params={"kd": 0.2},
            ),
        )

    def _write(self, name: str, preset: Preset) -> None:
        write_preset(self.directory, name, preset, base_directory=self.base)

    def _nudge_kp(self, value: float = 0.2) -> None:
        self.app._kp_max_changed(0, str(value))
        self.app._refresh_preset_marker()


class SaveTests(_PresetGui):
    def test_save_as_writes_the_form_and_selects_it(self) -> None:
        self.assertTrue(self._save_as("日常"))
        self.assertEqual(self.app.current_preset, "日常")
        self.assertIn("日常", self.app.preset_combo.cget("values"))
        self.assertEqual(self.app.preset_choice.get(), "日常")
        self.assertTrue(same_settings(self._read("日常"), self._form()))

    def test_the_chosen_preset_is_remembered_in_settings(self) -> None:
        self._save_as("日常")
        self.assertEqual(load_config(self.settings).ui.preset, "日常")

    def test_save_without_a_preset_asks_for_a_name(self) -> None:
        with mock.patch.object(simpledialog, "askstring", return_value="日常") as ask:
            self.assertTrue(self.app._save_preset())
        ask.assert_called_once()
        self.assertEqual(self.app.current_preset, "日常")

    def test_save_overwrites_the_current_preset_without_asking(self) -> None:
        self._save_as("日常")
        self._nudge_kp(0.2)
        with mock.patch.object(simpledialog, "askstring") as ask, mock.patch.object(
            messagebox, "askokcancel"
        ) as confirm:
            self.assertTrue(self.app._save_preset())
        ask.assert_not_called()
        confirm.assert_not_called()
        self.assertAlmostEqual(self._read("日常").aim_profile_1.kp_max, 0.2)

    def test_cancelling_the_name_saves_nothing(self) -> None:
        with mock.patch.object(simpledialog, "askstring", return_value=None):
            self.assertFalse(self.app._save_preset_as())
        self.assertFalse(self.directory.exists() and any(self.directory.iterdir()))
        self.assertIsNone(self.app.current_preset)

    def test_a_bad_name_says_why_and_saves_nothing(self) -> None:
        with mock.patch.object(messagebox, "showerror") as error:
            self.assertFalse(self._save_as("a/b"))
        error.assert_called_once()
        self.assertFalse(self.directory.exists() and any(self.directory.iterdir()))

    def test_saving_as_another_existing_name_asks_first_and_defaults_to_cancel(self) -> None:
        self._save_as("A")
        self._nudge_kp(0.2)
        self._save_as("B")
        with mock.patch.object(messagebox, "askokcancel", return_value=False) as confirm:
            self.assertFalse(self._save_as("a"))
        self.assertEqual(confirm.call_args.kwargs.get("default"), messagebox.CANCEL)
        self.assertNotAlmostEqual(self._read("A").aim_profile_1.kp_max, 0.2)
        self.assertEqual(self.app.current_preset, "B")

    def test_confirming_the_overwrite_replaces_it(self) -> None:
        self._save_as("A")
        self._nudge_kp(0.2)
        with mock.patch.object(messagebox, "askokcancel", return_value=True):
            self.assertTrue(self._save_as("A2"))
            self.assertTrue(self._save_as("a"))
        self.assertEqual(self.app.current_preset, "a")
        self.assertAlmostEqual(self._read("a").aim_profile_1.kp_max, 0.2)

    def test_a_missing_model_is_reported_not_saved(self) -> None:
        self.app.model_path.set("models/gone.onnx")
        with mock.patch.object(messagebox, "showerror") as error:
            self.assertFalse(self._save_as("日常"))
        error.assert_called_once()
        self.assertIsNone(self.app.current_preset)


class MarkerTests(_PresetGui):
    def test_a_change_marks_the_preset(self) -> None:
        self._save_as("日常")
        self._nudge_kp(0.2)
        self.assertEqual(self.app.preset_choice.get(), "日常 *")

    def test_changing_it_back_clears_the_mark(self) -> None:
        self._save_as("日常")
        original = self.app.profile_kp_max[0].get()
        self._nudge_kp(0.2)
        self._nudge_kp(original)
        self.assertEqual(self.app.preset_choice.get(), "日常")

    def test_saving_clears_the_mark(self) -> None:
        self._save_as("日常")
        self._nudge_kp(0.2)
        self.app._save_preset()
        self.app._refresh_preset_marker()
        self.assertEqual(self.app.preset_choice.get(), "日常")

    def test_changes_outside_the_aim_panel_mark_too(self) -> None:
        self._save_as("日常")
        self.app.model_path.set("models/other.onnx")
        self.app._refresh_preset_marker()
        self.assertEqual(self.app.preset_choice.get(), "日常 *")

    def test_input_and_kmbox_changes_mark_too(self) -> None:
        for variable, value in (
            (self.app.udp_host, "10.1.2.3"),
            (self.app.obs_password, "changed"),
            (self.app.kmbox_uuid, "FFFF0000"),
        ):
            with self.subTest(value=value):
                self._save_as("日常")
                variable.set(value)
                self.app._refresh_preset_marker()
                self.assertEqual(self.app.preset_choice.get(), "日常 *")

    def test_without_a_preset_there_is_nothing_to_mark(self) -> None:
        self._nudge_kp(0.2)
        self.assertEqual(self.app.preset_choice.get(), NO_PRESET)

    def test_a_half_typed_number_keeps_the_last_mark(self) -> None:
        self._save_as("日常")
        self._nudge_kp(0.2)
        self.app.udp_port.set("")
        self.app._refresh_preset_marker()
        self.assertEqual(self.app.preset_choice.get(), "日常 *")

    def test_the_marker_keeps_polling(self) -> None:
        with mock.patch.object(self.app.root, "after") as after:
            self.app._watch_preset()
        self.assertEqual(after.call_args.args[1], self.app._watch_preset)


class LoadTests(_PresetGui):
    def setUp(self) -> None:
        super().setUp()
        self.game = self._other_game()
        self._write("game", self.game)

    def test_loading_fills_every_preset_field(self) -> None:
        self.assertTrue(self.app._load_preset("game"))
        self.assertTrue(same_settings(self._form(), self.game))
        self.assertEqual(self.app.current_preset, "game")
        self.app._refresh_preset_marker()
        self.assertEqual(self.app.preset_choice.get(), "game")

    def test_loading_fills_input_and_kmbox(self) -> None:
        self.app._load_preset("game")
        self.assertEqual(self.app.udp_host.get(), "192.0.2.164")
        self.assertEqual(self.app.udp_width.get(), "416")
        self.assertEqual(self.app.obs_password.get(), "obs-pass")
        self.assertEqual(self.app.obs_source.get(), "游戏画面")
        self.assertTrue(self.app.kmbox_enabled.get())
        self.assertEqual(self.app.kmbox_port.get(), "8810")
        self.assertEqual(self.app.kmbox_uuid.get(), "ABCD1234")

    def test_loading_shows_the_panel_for_the_loaded_input_mode(self) -> None:
        self.assertEqual(self.app.obs_panel.winfo_manager(), "")
        self.app._load_preset("game")
        self.assertEqual(self.app.obs_panel.winfo_manager(), "grid")
        self.assertEqual(self.app.udp_panel.winfo_manager(), "")

    def test_advanced_settings_outside_the_window_are_kept(self) -> None:
        # 超时、缓冲区这些不在预设里, 载入预设不能把它们打回默认。
        config = load_config(self.settings, validate_model=False)
        save_config(
            replace(
                config,
                udp=replace(config.udp, fifo_packets=99),
                kmbox=replace(config.kmbox, monitor_port=6001),
            ),
            self.settings,
        )
        self._reopen()
        self.app._load_preset("game")
        form = self.app._read_form()
        self.assertEqual(form.udp.fifo_packets, 99)
        self.assertEqual(form.kmbox.monitor_port, 6001)

    def test_loading_is_remembered_in_settings(self) -> None:
        self.app._load_preset("game")
        loaded = load_config(self.settings)
        self.assertEqual(loaded.ui.preset, "game")
        self.assertTrue(same_settings(preset_from_config(loaded), self.game))

    def test_the_new_model_gets_inspected_before_labels_are_checked(self) -> None:
        # 旧模型只有 2 类。拿它去卡新预设的标签 3, 会被悄悄改成 0。
        self.app.model_contract = mock.Mock(class_count_for=mock.Mock(return_value=2))
        self.app._load_preset("game")
        self.inspect.assert_called_with((self.base / "models" / "other.onnx").resolve())
        self.assertEqual(self.app.profile_target_class[0].get(), "3")

    def test_the_trigger_conflict_guard_remembers_the_loaded_triggers(self) -> None:
        self.app._load_preset("game")
        self.assertEqual(
            self.app._last_profile_triggers,
            [variable.get() for variable in self.app.profile_trigger],
        )

    def test_parameters_missing_from_the_file_use_defaults_not_leftovers(self) -> None:
        self.app._load_preset("game")
        self.app.profile_algorithm_params[0]["gain"].set(3.0)
        path = self.directory / "lean.json"
        payload = json.loads((self.directory / "game.json").read_text(encoding="utf-8"))
        del payload["aim_profiles"][0]["algorithm_params"]["gain"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch.object(messagebox, "askyesnocancel", return_value=False):
            self.assertTrue(self.app._load_preset("lean"))
        self.assertEqual(self.app.profile_algorithm_params[0]["gain"].get(), 1.0)

    def test_a_missing_model_refuses_and_changes_nothing(self) -> None:
        before = self._form()
        (self.base / "models" / "other.onnx").unlink()
        with mock.patch.object(messagebox, "showerror") as error:
            self.assertFalse(self.app._load_preset("game"))
        error.assert_called_once()
        self.assertTrue(same_settings(self._form(), before))
        self.assertIsNone(self.app.current_preset)

    def test_an_algorithm_that_is_not_installed_refuses(self) -> None:
        payload = json.loads((self.directory / "game.json").read_text(encoding="utf-8"))
        payload["aim_profiles"][0]["algorithm"] = "someone_elses_plugin"
        (self.directory / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch.object(messagebox, "showerror") as error:
            self.assertFalse(self.app._load_preset("plugin"))
        self.assertIn("someone_elses_plugin", error.call_args.args[1])

    def test_choosing_from_the_dropdown_loads_it(self) -> None:
        self.app._refresh_preset_bar()
        self.app.preset_choice.set("game")
        self.app._preset_selected()
        self.assertEqual(self.app.current_preset, "game")
        self.assertTrue(same_settings(self._form(), self.game))


class SwitchWithChangesTests(_PresetGui):
    def setUp(self) -> None:
        super().setUp()
        self._save_as("A")
        self.saved_kp = self.app.profile_kp_max[0].get()
        self._write("B", self._other_game())
        self._nudge_kp(0.2)

    def _switch(self, answer):
        with mock.patch.object(messagebox, "askyesnocancel", return_value=answer) as ask:
            loaded = self.app._load_preset("B")
        ask.assert_called_once()
        return loaded

    def test_cancel_stays_with_the_changes(self) -> None:
        self.assertFalse(self._switch(None))
        self.assertEqual(self.app.current_preset, "A")
        self.assertAlmostEqual(self.app.profile_kp_max[0].get(), 0.2)
        self.app._refresh_preset_bar()
        self.assertEqual(self.app.preset_choice.get(), "A *")

    def test_discard_switches_and_leaves_the_file_alone(self) -> None:
        self.assertTrue(self._switch(False))
        self.assertEqual(self.app.current_preset, "B")
        self.assertAlmostEqual(self._read("A").aim_profile_1.kp_max, self.saved_kp)

    def test_save_keeps_the_changes_then_switches(self) -> None:
        self.assertTrue(self._switch(True))
        self.assertEqual(self.app.current_preset, "B")
        self.assertAlmostEqual(self._read("A").aim_profile_1.kp_max, 0.2)

    def test_a_clean_preset_switches_without_asking(self) -> None:
        self._nudge_kp(self.saved_kp)
        with mock.patch.object(messagebox, "askyesnocancel") as ask:
            self.assertTrue(self.app._load_preset("B"))
        ask.assert_not_called()

    def test_picking_the_same_preset_again_can_throw_the_changes_away(self) -> None:
        with mock.patch.object(messagebox, "askyesnocancel", return_value=False):
            self.assertTrue(self.app._load_preset("A"))
        self.assertAlmostEqual(self.app.profile_kp_max[0].get(), self.saved_kp)


class DeleteTests(_PresetGui):
    def setUp(self) -> None:
        super().setUp()
        self._save_as("日常")

    def test_cancel_keeps_the_file_and_defaults_to_cancel(self) -> None:
        with mock.patch.object(messagebox, "askokcancel", return_value=False) as confirm:
            self.app._delete_preset()
        confirm.assert_called_once()
        self.assertIn("日常", confirm.call_args.args[1])
        self.assertEqual(confirm.call_args.kwargs.get("default"), messagebox.CANCEL)
        self.assertTrue((self.directory / "日常.json").exists())
        self.assertEqual(self.app.current_preset, "日常")

    def test_confirm_deletes_but_leaves_the_form_alone(self) -> None:
        before = self._form()
        with mock.patch.object(messagebox, "askokcancel", return_value=True):
            self.app._delete_preset()
        self.assertFalse((self.directory / "日常.json").exists())
        self.assertIsNone(self.app.current_preset)
        self.assertEqual(self.app.preset_choice.get(), NO_PRESET)
        self.assertNotIn("日常", self.app.preset_combo.cget("values"))
        self.assertTrue(same_settings(self._form(), before))

    def test_delete_is_only_offered_with_a_preset(self) -> None:
        self.assertEqual(str(self.app.delete_preset_button.cget("state")), "normal")
        with mock.patch.object(messagebox, "askokcancel", return_value=True):
            self.app._delete_preset()
        self.assertEqual(str(self.app.delete_preset_button.cget("state")), "disabled")


class RunningLockTests(_PresetGui):
    def _run(self) -> None:
        self.app.process = mock.Mock()
        self.app._set_running(True, "运行中")

    def _stop(self) -> None:
        self.app.process = None
        self.app._set_running(False, "已停止")

    def test_running_locks_loading_and_the_model(self) -> None:
        self._run()
        for widget in (
            self.app.preset_combo,
            self.app.model_entry,
            self.app.browse_model_button,
            self.app.provider_combo,
            self.app.cuda_graph_check,
            self.app.gpu_preprocess_check,
        ):
            with self.subTest(widget=str(widget)):
                self.assertEqual(str(widget.cget("state")), "disabled")

    def test_stopping_unlocks_them(self) -> None:
        self._run()
        self._stop()
        self.assertEqual(str(self.app.preset_combo.cget("state")), "readonly")
        self.assertEqual(str(self.app.provider_combo.cget("state")), "readonly")
        self.assertEqual(str(self.app.model_entry.cget("state")), "normal")
        self.assertEqual(str(self.app.browse_model_button.cget("state")), "normal")

    def test_stopping_still_respects_the_provider(self) -> None:
        self.app.provider.set("CPU")
        self._run()
        self._stop()
        self.assertEqual(str(self.app.cuda_graph_check.cget("state")), "disabled")

    def test_loading_while_running_is_refused(self) -> None:
        self._write("game", self._other_game())
        before = self._form()
        self._run()
        with mock.patch.object(messagebox, "showinfo") as info:
            self.assertFalse(self.app._load_preset("game"))
        info.assert_called_once()
        self.assertTrue(same_settings(self._form(), before))

    def test_saving_while_running_still_works(self) -> None:
        self._run()
        self.assertTrue(self._save_as("调好的"))
        self.assertTrue((self.directory / "调好的.json").exists())


class StartupTests(_PresetGui):
    def _remember(self, name: str) -> None:
        config = load_config(self.settings)
        save_config(replace(config, ui=replace(config.ui, preset=name)), self.settings)

    def test_the_remembered_preset_is_selected(self) -> None:
        self._save_as("日常")
        reopened = self._reopen()
        self.assertEqual(reopened.current_preset, "日常")
        self.assertEqual(reopened.preset_choice.get(), "日常")

    def test_settings_that_drifted_from_the_preset_show_the_mark(self) -> None:
        self._save_as("日常")
        config = load_config(self.settings)
        save_config(
            replace(config, aim_profile_1=replace(config.aim_profile_1, kp_max=0.25)),
            self.settings,
        )
        reopened = self._reopen()
        self.assertEqual(reopened.preset_choice.get(), "日常 *")

    def test_a_deleted_preset_is_quietly_forgotten(self) -> None:
        self._remember("gone")
        with mock.patch.object(messagebox, "showerror") as error:
            reopened = self._reopen()
        error.assert_not_called()
        self.assertIsNone(reopened.current_preset)
        self.assertEqual(reopened.preset_choice.get(), NO_PRESET)


class SinglePcInOldWindowTests(_PresetGui):
    """旧的 tkinter 界面不加单机模式的控件, 只保证不出错 (规格 §6.5)。"""

    def test_the_local_screen_hides_both_network_panels(self) -> None:
        """本机屏幕没有地址端口可填。显示 UDP 那组的话, 用户会去改一个根本不起
        作用的端口。"""
        self.app.input_mode.set("本机屏幕")
        self.app._switch_input_panel()
        self.assertEqual(self.app.udp_panel.winfo_manager(), "")
        self.assertEqual(self.app.obs_panel.winfo_manager(), "")

    def test_the_old_window_keeps_the_single_pc_settings(self) -> None:
        """在新界面里选了 SendInput, 到旧界面点一下保存就悄悄变回 KMBox 的话,
        用户根本想不到是保存那一下改的。"""
        config = load_config(self.settings, validate_model=False)
        save_config(
            replace(
                config,
                input=replace(config.input, mode="desktop"),
                desktop=replace(config.desktop, backend="winrt", monitor=1, width=256, height=224),
                mouse=replace(config.mouse, output="sendinput"),
            ),
            self.settings,
        )
        form = self._reopen()._read_form()
        self.assertEqual(form.input.mode, "desktop")
        self.assertEqual((form.desktop.backend, form.desktop.monitor), ("winrt", 1))
        self.assertEqual((form.desktop.width, form.desktop.height), (256, 224))
        self.assertEqual(form.mouse.output, "sendinput")


if __name__ == "__main__":
    unittest.main()
