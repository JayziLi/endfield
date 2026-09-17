from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from tkinter import ttk

from rhodes_fast.aim_algorithms import set_installed_algorithms
from rhodes_fast.algorithm_library import install
from rhodes_fast.gui import RhodesFastGui
from rhodes_fast.tuning_share import dump_tuning, load_tuning


ROOT = Path(__file__).parents[1]


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class ProjectileGuiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        directory = Path(self.folder.name)
        self.config = directory / "settings.txt"
        self.config.write_text((ROOT / "settings.example.txt").read_text(encoding="utf-8"), encoding="utf-8")
        install(directory / "algorithms", ROOT / "examples" / "kalman_projectile.py")
        self.app = RhodesFastGui(self.config)
        self.app.profile_algorithm[0].set("卡尔曼弹道预测")
        self.app._algorithm_changed(0)
        self.app.notebook.select(1)
        self.app.root.update()
        self.app.root.update_idletasks()

    def tearDown(self):
        for callback in self.app.root.tk.call("after", "info"):
            self.app.root.after_cancel(callback)
        self.app.root.destroy()
        set_installed_algorithms({})
        self.folder.cleanup()

    def test_time_presets_update_saved_and_shared_parameters(self):
        frame = self.app.algorithm_param_frames[0]
        button = next(w for w in descendants(frame) if isinstance(w, ttk.Button) and w.cget("text") == "200")
        button.invoke()
        profile = self.app._read_form().aim_profiles[0]
        self.assertEqual(profile.algorithm_params["projectile_lead_ms"], 200)
        self.assertEqual(load_tuning(dump_tuning(profile)).params["projectile_lead_ms"], 200)
        spinboxes = [w for w in descendants(frame) if isinstance(w, ttk.Spinbox)]
        self.assertEqual(float(spinboxes[0].cget("increment")), 5)

    def test_advanced_settings_expand_and_collapse(self):
        frame = self.app.algorithm_param_frames[0]
        toggle = next(w for w in descendants(frame) if isinstance(w, ttk.Button) and w.cget("text") == "高级参数 ▸")
        label = next(w for w in descendants(frame) if isinstance(w, ttk.Label) and w.cget("text") == "控制回路延迟（毫秒）")
        self.assertFalse(label.master.winfo_manager())
        toggle.invoke()
        self.assertEqual(label.master.winfo_manager(), "grid")
        toggle.invoke()
        self.assertFalse(label.master.winfo_manager())

    def test_parameters_fit_default_window_width(self):
        frame = self.app.algorithm_param_frames[0]
        self.assertLessEqual(frame.winfo_reqwidth(), frame.winfo_width())


if __name__ == "__main__":
    unittest.main()
