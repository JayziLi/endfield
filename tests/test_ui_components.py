from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rhodes_fast.ui.theme import THEME, build_stylesheet  # noqa: E402
from rhodes_fast.ui.widgets import (  # noqa: E402
    IndustrialButton,
    IndustrialField,
    IndustrialIconButton,
    IndustrialSelect,
    LogConsole,
    MetricRail,
    PreviewCanvas,
    SegmentedControl,
    Sidebar,
    Sparkline,
    TabRail,
    TechSlider,
)


class UiComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_theme_exposes_the_approved_accent_colors(self) -> None:
        stylesheet = build_stylesheet()
        self.assertEqual(THEME.acid, "#E6FF18")
        self.assertEqual(THEME.paper, "#F1F1EB")
        self.assertEqual(THEME.text, "#101110")
        self.assertIn(THEME.acid, stylesheet)

    def test_industrial_button_validates_its_visual_variant(self) -> None:
        button = IndustrialButton("应用", "primary")
        self.assertEqual(button.variant, "primary")
        with self.assertRaises(ValueError):
            IndustrialButton("应用", "glossy")
        with self.assertRaises(ValueError):
            button.variant = "glossy"

    def test_sidebar_selects_pages_through_its_public_api(self) -> None:
        sidebar = Sidebar()
        sidebar.set_current("control")
        self.assertTrue(sidebar.buttons["control"].isChecked())
        with self.assertRaises(KeyError):
            sidebar.set_current("missing")

    def test_slider_clamps_and_round_trips_values(self) -> None:
        slider = TechSlider("置信度", 0.05, 0.95, 0.375)
        self.assertAlmostEqual(slider.value(), 0.375, places=3)
        slider.setValue(2.0)
        self.assertAlmostEqual(slider.value(), 0.95, places=3)

    def test_industrial_field_exposes_explicit_states(self) -> None:
        field = IndustrialField("端口", "4455", state="read_only", suffix="UDP")
        self.assertTrue(field.line_edit.isReadOnly())
        self.assertEqual(field.text(), "4455")
        field.set_state("error")
        self.assertFalse(field.line_edit.isReadOnly())
        self.assertEqual(field.state_label.text(), "INVALID")
        with self.assertRaises(ValueError):
            field.set_state("loading")
        no_suffix = IndustrialField("地址")
        self.assertTrue(no_suffix.suffix_label.isHidden())

    def test_default_field_tracks_real_keyboard_focus(self) -> None:
        field = IndustrialField("地址", "udp://127.0.0.1", suffix="UDP")
        field.show()
        field.line_edit.setFocus()
        self.app.processEvents()
        self.assertEqual(field.state_label.text(), "ACTIVE")
        field.line_edit.clearFocus()
        self.app.processEvents()
        self.assertEqual(field.state_label.text(), "READY")
        field.close()

    def test_segmented_control_changes_one_selection(self) -> None:
        control = SegmentedControl((("trt", "TensorRT"), ("cpu", "CPU")), "trt")
        self.assertEqual(control.current, "trt")
        changes: list[str] = []
        control.currentChanged.connect(changes.append)
        control.buttons["cpu"].click()
        self.assertEqual(control.current, "cpu")
        self.assertEqual(control.buttons["cpu"].variant, "primary")
        self.assertEqual(changes, ["cpu"])
        with self.assertRaises(KeyError):
            control.set_current("missing")
        with self.assertRaises(ValueError):
            SegmentedControl((("cpu", "CPU"), ("cpu", "CPU SAFE")))

    def test_tabs_select_and_icon_controls_have_public_state(self) -> None:
        tabs = TabRail((("udp", "UDP"), ("obs", "OBS")), "obs")
        self.assertEqual(tabs.current, "obs")
        self.assertEqual(tabs.buttons["obs"].variant, "secondary")
        select = IndustrialSelect("精度", ("FP16", "FP32"), "FP16")
        self.assertEqual(select.current, "FP16")
        icon = IndustrialIconButton("export", "导出配置")
        self.assertEqual(icon.accessibleName(), "导出配置")
        with self.assertRaises(ValueError):
            IndustrialSelect("精度", ("FP16",), "INT8")

    def test_metric_rail_updates_named_values(self) -> None:
        rail = MetricRail((("FPS", "120", ""), ("GPU", "40", "%")))
        rail.set_value("FPS", "238")
        self.assertEqual(rail.value_labels["FPS"].text(), "238")
        with self.assertRaises(KeyError):
            rail.set_value("CPU", "8")
        with self.assertRaises(ValueError):
            MetricRail((("FPS", "120", ""), ("FPS", "60", "")))

    def test_sparkline_batch_updates_obey_the_repaint_cap(self) -> None:
        class CountingSparkline(Sparkline):
            def __init__(self) -> None:
                self.update_count = 0
                super().__init__()

            def update(self, *args) -> None:  # noqa: ANN002
                self.update_count += 1
                super().update(*args)

        sparkline = CountingSparkline()
        sparkline.show()
        self.app.processEvents()
        sparkline._last_repaint = time.monotonic()
        before = sparkline.update_count
        sparkline.set_values((1, 2, 3))
        self.assertEqual(sparkline.update_count, before)
        QTest.qWait(160)
        self.assertGreater(sparkline.update_count, before)
        self.assertEqual(tuple(sparkline._values), (1, 2, 3))
        sparkline.close()

    def test_console_discards_old_lines(self) -> None:
        console = LogConsole(max_lines=3)
        for index in range(5):
            console.append_line(f"line {index}")
        self.assertLessEqual(console.document().blockCount(), 3)
        self.assertNotIn("line 0", console.toPlainText())

    def test_preview_rejects_frames_while_inactive(self) -> None:
        preview = PreviewCanvas()
        frame = QImage(16, 16, QImage.Format.Format_RGB32)
        self.assertFalse(preview.set_frame(frame))
        preview.set_active(True)
        self.assertTrue(preview.set_frame(frame))

    def test_preview_has_a_configurable_frame_rate_limit(self) -> None:
        preview = PreviewCanvas(max_fps=24)
        self.assertAlmostEqual(preview.max_fps, 24)
        preview.set_max_fps(0)
        self.assertEqual(preview.max_fps, 0)
        with self.assertRaises(ValueError):
            preview.set_max_fps(-1)

    def test_preview_eventually_paints_the_latest_throttled_frame(self) -> None:
        preview = PreviewCanvas(max_fps=20)
        preview.resize(160, 90)
        preview.show()
        preview.set_active(True)
        first = QImage(16, 9, QImage.Format.Format_RGB32)
        first.fill(QColor("#AA0000"))
        latest = QImage(16, 9, QImage.Format.Format_RGB32)
        latest.fill(QColor("#00AA00"))
        preview.set_frame(first)
        self.app.processEvents()
        preview.set_frame(latest)
        QTest.qWait(70)
        rendered = preview.grab().toImage()
        self.assertGreater(rendered.pixelColor(8, 8).green(), rendered.pixelColor(8, 8).red())
        preview.close()

    def test_sidebar_accepts_a_smaller_page_set(self) -> None:
        sidebar = Sidebar(items=(("overview", "01", "总览", "OVERVIEW"),))
        self.assertEqual(set(sidebar.buttons), {"overview"})


if __name__ == "__main__":
    unittest.main()
