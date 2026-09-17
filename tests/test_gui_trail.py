from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from rhodes_fast.config import load_config
from rhodes_fast.gui import RhodesFastGui
from rhodes_fast.trail import TrailSettings, read_trail_settings

_EXAMPLE = Path(__file__).resolve().parents[1] / "settings.example.txt"


class _TrailGui(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        models = self.base / "models"
        models.mkdir()
        (models / "yolov5n.onnx").write_bytes(b"not really onnx")
        self.settings = self.base / "settings.txt"
        shutil.copy(_EXAMPLE, self.settings)
        inspect = mock.patch.object(RhodesFastGui, "_inspect_selected_model")
        inspect.start()
        self.addCleanup(inspect.stop)

    def _open(self) -> RhodesFastGui:
        app = RhodesFastGui(self.settings)
        self.addCleanup(self._close, app)
        app.root.update_idletasks()
        return app

    @staticmethod
    def _close(app: RhodesFastGui) -> None:
        try:
            app._close_preview_socket()
            app.root.destroy()
        except Exception:
            pass

    @staticmethod
    def _pretend_running(app: RhodesFastGui) -> None:
        """当成管线已经在跑、预览通道已经开着, 而且正停在预览页上。"""
        # 真启动时 _launch 会先建好 .cache; 这里没走 _launch, 自己建。
        app.preview_enable_file.parent.mkdir(parents=True, exist_ok=True)
        app.process = mock.Mock()
        app.preview_socket = mock.Mock()
        app.notebook.select(app.preview_tab)
        app.root.update()

    @staticmethod
    def _message(app: RhodesFastGui) -> str:
        texts = [
            app.preview_canvas.itemcget(item, "text")
            for item in app.preview_canvas.find_withtag("preview")
            if app.preview_canvas.type(item) == "text"
        ]
        return " ".join(texts)


class ControlsTests(_TrailGui):
    def test_opening_shows_only_the_frame_even_if_the_trail_was_on_last_time(self) -> None:
        text = self.settings.read_text(encoding="utf-8").replace(
            "[ui]\n", "[ui]\ntrail_enabled = True\ntrail_optimal_path = True\ntrail_seconds = 1.2\n", 1
        )
        self.settings.write_text(text, encoding="utf-8")

        app = self._open()

        self.assertTrue(app.preview_frame.get())
        self.assertFalse(app.trail_enabled.get())
        self.assertFalse(app.trail_optimal_path.get())
        self.assertAlmostEqual(app.trail_seconds.get(), 1.2)
        self.assertEqual(app.trail_seconds_label.cget("text"), "1.2 秒")

    def test_optimal_path_and_length_wait_for_the_trail(self) -> None:
        app = self._open()
        self.assertTrue(app.trail_optimal_check.instate(["disabled"]))
        self.assertTrue(app.trail_seconds_scale.instate(["disabled"]))

        app.trail_enabled.set(True)
        self.assertFalse(app.trail_optimal_check.instate(["disabled"]))
        self.assertFalse(app.trail_seconds_scale.instate(["disabled"]))

        app.trail_enabled.set(False)
        self.assertTrue(app.trail_optimal_check.instate(["disabled"]))

    def test_the_slider_snaps_to_tenths(self) -> None:
        app = self._open()
        app.trail_seconds.set(1.2749)

        self.assertEqual(app._current_trail_settings().seconds, 1.3)
        self.assertEqual(app.trail_seconds_label.cget("text"), "1.3 秒")

    def test_the_controls_sit_on_the_preview_page(self) -> None:
        app = self._open()
        for widget in (app.trail_seconds_scale, app.trail_optimal_check):
            ancestor = widget.master
            while ancestor is not None and ancestor is not app.preview_tab:
                ancestor = ancestor.master
            self.assertIs(ancestor, app.preview_tab)


class RunningTests(_TrailGui):
    def test_a_change_while_running_reaches_the_pipeline_straight_away(self) -> None:
        app = self._open()
        app.process = mock.Mock()

        app.trail_enabled.set(True)
        app.trail_optimal_path.set(True)
        app.trail_seconds.set(0.8)
        app.preview_frame.set(False)

        self.assertEqual(
            read_trail_settings(app.trail_settings_file),
            TrailSettings(show_frame=False, enabled=True, seconds=0.8, optimal_path=True),
        )

    def test_nothing_is_written_while_stopped(self) -> None:
        app = self._open()
        app.trail_enabled.set(True)
        self.assertFalse(app.trail_settings_file.exists())

    def test_starting_hands_the_settings_file_to_the_pipeline(self) -> None:
        app = self._open()
        app.trail_seconds.set(1.5)
        process = mock.Mock()
        process.stdout = io.StringIO("")
        process.wait.return_value = 0
        with mock.patch("rhodes_fast.gui.subprocess.Popen", return_value=process) as popen:
            app._start()

        command = popen.call_args.args[0]
        self.assertIn("--trail-settings-file", command)
        path = Path(command[command.index("--trail-settings-file") + 1])
        self.assertEqual(path, app.trail_settings_file)
        self.assertEqual(read_trail_settings(path), TrailSettings(seconds=1.5))

    def test_turning_both_off_stops_the_preview_and_says_why(self) -> None:
        app = self._open()
        self._pretend_running(app)
        app._on_tab_changed()
        self.assertTrue(app.preview_enable_file.exists())

        app.preview_frame.set(False)

        self.assertFalse(app.preview_enable_file.exists())
        self.assertIn("画面", self._message(app))
        self.assertIn("轨迹", self._message(app))

        app.trail_enabled.set(True)

        self.assertTrue(app.preview_enable_file.exists())
        self.assertNotIn("勾选", self._message(app))

    def test_a_late_frame_does_not_cover_the_hint_after_both_are_off(self) -> None:
        # 取消勾选之后管线还有一两帧在路上, 到了也不能把提示盖掉。
        app = self._open()
        self._pretend_running(app)
        app._on_tab_changed()
        app.preview_frame.set(False)

        app._display_preview(np.zeros((80, 80, 3), dtype=np.uint8))

        self.assertIn("勾选", self._message(app))

    def test_switching_between_frame_and_trail_only_keeps_the_preview_going(self) -> None:
        app = self._open()
        self._pretend_running(app)
        app._on_tab_changed()
        app.trail_enabled.set(True)

        with mock.patch.object(app, "_show_preview_message") as message:
            app.preview_frame.set(False)
            app.preview_frame.set(True)

        self.assertTrue(app.preview_enable_file.exists())
        message.assert_not_called()

    def test_opening_the_page_with_both_off_does_not_start_rendering(self) -> None:
        app = self._open()
        app.preview_frame.set(False)
        app.preview_enable_file.parent.mkdir(parents=True, exist_ok=True)
        app.process = mock.Mock()
        app.preview_socket = mock.Mock()

        app.notebook.select(app.preview_tab)
        app.root.update()
        app._on_tab_changed()

        self.assertFalse(app.preview_enable_file.exists())
        self.assertIn("勾选", self._message(app))


class PersistTests(_TrailGui):
    def test_the_length_is_saved_to_the_settings_file(self) -> None:
        app = self._open()
        app.trail_seconds.set(1.7)
        app._persist_trail_settings()

        self.assertEqual(load_config(self.settings, validate_model=False).ui.trail_seconds, 1.7)

    def test_the_checkboxes_are_never_saved(self) -> None:
        app = self._open()
        app.trail_enabled.set(True)
        app.trail_optimal_path.set(True)
        app.preview_frame.set(False)
        app._persist_trail_settings()
        app._save(quiet=True)

        text = self.settings.read_text(encoding="utf-8")
        for name in ("trail_enabled", "trail_optimal_path", "show_frame"):
            self.assertNotIn(name, text)

    def test_saving_the_length_does_not_sneak_in_other_unsaved_edits(self) -> None:
        # 只是拖了一下轨迹长度, 不能顺手把表单上别的、还没点保存的改动也写进去。
        before = load_config(self.settings, validate_model=False)
        app = self._open()
        app.confidence.set(before.model.confidence + 0.1)
        app.trail_seconds.set(1.1)
        app._persist_trail_settings()

        after = load_config(self.settings, validate_model=False)
        self.assertEqual(after.model.confidence, before.model.confidence)
        self.assertEqual(after.ui.trail_seconds, 1.1)

    def test_a_later_full_save_keeps_the_length(self) -> None:
        app = self._open()
        app.trail_seconds.set(1.4)
        app._save(quiet=True)

        self.assertEqual(load_config(self.settings, validate_model=False).ui.trail_seconds, 1.4)

    def test_the_length_is_saved_after_a_short_pause_not_on_every_slider_step(self) -> None:
        app = self._open()
        with mock.patch.object(app, "_persist_trail_settings") as persist:
            for value in (0.5, 0.6, 0.7, 0.8):
                app.trail_seconds.set(value)
            persist.assert_not_called()
            app.root.after(700, app.root.quit)
            app.root.mainloop()
        persist.assert_called_once()

    def test_ticking_a_checkbox_does_not_touch_settings_txt(self) -> None:
        app = self._open()
        with mock.patch.object(app, "_persist_trail_settings") as persist:
            app.trail_enabled.set(True)
            app.trail_optimal_path.set(True)
            app.preview_frame.set(False)
            app.root.after(700, app.root.quit)
            app.root.mainloop()
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
