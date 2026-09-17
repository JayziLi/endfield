from __future__ import annotations

import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from rhodes_fast.detector import Detection
from rhodes_fast.preview import PreviewPublisher, render_preview
from rhodes_fast.trail import (
    TRAIL_NEEDS_KMBOX,
    TrailGeometry,
    TrailOverlay,
    TrailRecorder,
    TrailSettings,
    TrailSettingsFile,
    read_trail_settings,
    write_trail_settings,
)

_TRAIL_ON = TrailSettings(enabled=True)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _folder(test: unittest.TestCase) -> Path:
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    return Path(folder.name)


class SettingsFileTests(unittest.TestCase):
    def test_a_fresh_start_shows_only_the_frame(self) -> None:
        self.assertEqual(
            TrailSettings(),
            TrailSettings(show_frame=True, enabled=False, seconds=0.5, optimal_path=False),
        )

    def test_round_trips(self) -> None:
        path = _folder(self) / ".cache" / "trail.json"
        settings = TrailSettings(show_frame=False, enabled=True, seconds=1.25, optimal_path=True)

        write_trail_settings(path, settings)

        self.assertEqual(read_trail_settings(path), settings)

    def test_a_missing_file_gives_the_defaults(self) -> None:
        self.assertEqual(read_trail_settings(_folder(self) / "nope.json"), TrailSettings())
        self.assertEqual(read_trail_settings(None), TrailSettings())

    def test_a_half_written_file_gives_the_defaults(self) -> None:
        path = _folder(self) / "trail.json"
        path.write_text('{"enabled": tr', encoding="utf-8")
        self.assertEqual(read_trail_settings(path), TrailSettings())

    def test_the_length_is_kept_inside_the_slider_range(self) -> None:
        path = _folder(self) / "trail.json"
        path.write_text(json.dumps({"seconds": 30}), encoding="utf-8")
        self.assertEqual(read_trail_settings(path).seconds, 2.0)
        path.write_text(json.dumps({"seconds": 0.01}), encoding="utf-8")
        self.assertEqual(read_trail_settings(path).seconds, 0.2)

    def test_values_of_the_wrong_type_fall_back_one_by_one(self) -> None:
        path = _folder(self) / "trail.json"
        path.write_text(
            json.dumps({"show_frame": "no", "enabled": "yes", "seconds": "long", "optimal_path": True}),
            encoding="utf-8",
        )
        self.assertEqual(read_trail_settings(path), TrailSettings(optimal_path=True))


class SettingsWatcherTests(unittest.TestCase):
    def test_rereads_the_file_a_few_times_a_second(self) -> None:
        path = _folder(self) / "trail.json"
        write_trail_settings(path, TrailSettings())
        clock = _Clock()
        watcher = TrailSettingsFile(path, clock=clock)

        write_trail_settings(path, _TRAIL_ON)
        clock.now += 0.1
        self.assertFalse(watcher.current().enabled)
        clock.now += 0.2
        self.assertTrue(watcher.current().enabled)
        self.assertTrue(watcher.settings.enabled)


def _busy_recorder(frames: int = 3000) -> TrailRecorder:
    """够标定用的一段: 延迟 7 帧、比例 0.63, 隔一阵拉一次枪。"""
    rng = random.Random(5)
    recorder = TrailRecorder()
    moved = []
    total = np.zeros(2)
    flick = 0
    step = np.zeros(2)
    for k in range(frames):
        if flick == 0 and rng.random() < 0.02:
            flick = rng.randint(6, 12)
            step = np.array([rng.uniform(-15, 15), rng.uniform(-8, 8)])
        command = np.round(step) if flick else np.zeros(2)
        flick = max(0, flick - 1)
        seen = moved[k - 7] if k >= 7 else np.zeros(2)
        error = np.array([30.0, 10.0]) - seen + np.array([rng.gauss(0, 0.8), rng.gauss(0, 0.8)])
        total = total + 0.63 * command
        moved.append(total.copy())
        recorder.record(
            time_s=k / 241.0,
            command=(int(command[0]), int(command[1])),
            hand=(0.0, 0.0),
            error=(float(error[0]), float(error[1])),
            track=1,
            profile=0,
        )
    return recorder


class OverlayTests(unittest.TestCase):
    def _overlay(self, recorder: TrailRecorder, **kwargs):
        clock = _Clock()
        options = {"available": True, "clock": clock}
        options.update(kwargs)
        return TrailOverlay(recorder, **options), clock

    def test_draws_nothing_while_the_trail_is_off(self) -> None:
        overlay, _clock = self._overlay(_busy_recorder())
        self.assertEqual(overlay.prepare(2999, (160.0, 160.0), TrailSettings()), (None, ""))

    def test_says_so_when_there_is_no_kmbox(self) -> None:
        overlay, _clock = self._overlay(_busy_recorder(), available=False)
        self.assertEqual(overlay.prepare(2999, (160.0, 160.0), _TRAIL_ON), (None, TRAIL_NEEDS_KMBOX))

    def test_says_it_is_calibrating_until_there_is_enough_movement(self) -> None:
        recorder = TrailRecorder()
        for k in range(300):
            recorder.record(time_s=k / 241.0, command=(0, 0), hand=(0.0, 0.0), error=(5.0, 0.0), track=1, profile=0)
        overlay, _clock = self._overlay(recorder)

        self.assertEqual(overlay.prepare(299, (160.0, 160.0), _TRAIL_ON), (None, "trail calibrating"))

    def test_draws_the_trail_once_calibrated_and_announces_it_once(self) -> None:
        announced = Mock()
        overlay, clock = self._overlay(_busy_recorder(), on_calibrated=announced)

        geometry, status = overlay.prepare(2999, (160.0, 160.0), _TRAIL_ON)
        clock.now += 2.0
        overlay.prepare(2999, (160.0, 160.0), _TRAIL_ON)

        self.assertIsInstance(geometry, TrailGeometry)
        np.testing.assert_allclose(geometry.landed[-1], (160.0, 160.0))
        self.assertTrue(status.startswith("trail 0."), status)
        announced.assert_called_once()

    def test_calibrates_at_most_once_a_second(self) -> None:
        overlay, clock = self._overlay(_busy_recorder())
        with patch.object(overlay.calibrator, "update", wraps=overlay.calibrator.update) as update:
            for _ in range(10):
                overlay.prepare(2999, (160.0, 160.0), _TRAIL_ON)
                clock.now += 0.05
            self.assertEqual(update.call_count, 1)
            clock.now += 1.0
            overlay.prepare(2999, (160.0, 160.0), _TRAIL_ON)
            self.assertEqual(update.call_count, 2)

    def test_only_rows_it_has_not_seen_are_read_for_calibration(self) -> None:
        # 17 秒的缓冲每秒全读一遍是白费力气; 标定器只要新行加上一小段上下文。
        recorder = _busy_recorder()
        overlay, clock = self._overlay(recorder)
        overlay.prepare(2000, (160.0, 160.0), _TRAIL_ON)
        clock.now += 1.5
        with patch.object(recorder, "window", wraps=recorder.window) as window:
            overlay.prepare(2241, (160.0, 160.0), _TRAIL_ON)
        calibration_reads = [call for call in window.call_args_list if call.args[0] == 2241]
        self.assertLessEqual(min(call.args[1] for call in calibration_reads), 241 + 40)

    def test_the_trail_length_setting_is_used(self) -> None:
        overlay, _clock = self._overlay(_busy_recorder())
        short, _status = overlay.prepare(2999, (160.0, 160.0), TrailSettings(enabled=True, seconds=0.2))
        long, _status = overlay.prepare(2999, (160.0, 160.0), TrailSettings(enabled=True, seconds=1.0))

        self.assertGreater(len(long.landed), 3 * len(short.landed))


def _line_geometry(target=()) -> TrailGeometry:
    return TrailGeometry(
        landed=np.array([[20.0, 40.0], [140.0, 40.0]]),
        landed_age=np.array([0.0]),
        in_flight=np.array([[140.0, 40.0]]),
        target=tuple(np.array(line, dtype=float) for line in target),
        optimal=None,
    )


class RenderTests(unittest.TestCase):
    def _render(self, frame: np.ndarray | None = None, detections=(), **kwargs) -> np.ndarray:
        frame = np.zeros((160, 160, 3), dtype=np.uint8) if frame is None else frame
        detections = list(detections)
        return render_preview(
            frame, detections, detections[0] if detections else None,
            target_y_ratio=0.5, fov_radius=60, inference_ms=1.0, detection_ms=1.0, **kwargs,
        )

    def test_draws_the_trail_into_the_frame(self) -> None:
        plain = self._render()
        with_trail = self._render(trail=_line_geometry())
        # 只看 FOV 圈(半径 60)里面的那一段, 圈本身会在 x≈35 和 x≈125 穿过这一行。
        self.assertGreater(int(with_trail[40, 50:110].max()), int(plain[40, 50:110].max()) + 100)

    def test_writes_the_trail_status_in_the_top_corner(self) -> None:
        plain = self._render()
        with_status = self._render(trail_status="trail calibrating")
        self.assertGreater(int(with_status[:20, :120].sum()), int(plain[:20, :120].sum()))

    def test_trail_only_is_the_trail_on_black(self) -> None:
        # 只勾轨迹: 推流画面、识别框、FOV 圈、准心十字、底部状态栏、目标轨迹都不画。
        frame = np.full((160, 160, 3), 90, dtype=np.uint8)
        detection = Detection(100, 90, 130, 150, 0.9, 0)
        geometry = _line_geometry(target=[((20.0, 120.0), (140.0, 120.0))])

        rendered = self._render(frame, [detection], trail=geometry, show_frame=False)

        mask = np.ones(rendered.shape[:2], dtype=bool)
        mask[34:47, 14:147] = False
        self.assertEqual(int(rendered[mask].max()), 0)
        self.assertGreater(int(rendered[40, 30:130].max()), 150)

    def test_trail_only_still_says_how_the_calibration_is_going(self) -> None:
        rendered = self._render(show_frame=False, trail_status="trail calibrating")
        self.assertGreater(int(rendered[:20, :120].sum()), 0)
        self.assertEqual(int(rendered[20:].max()), 0)


class PublisherTests(unittest.TestCase):
    def _publisher(self, overlay, settings: TrailSettings) -> PreviewPublisher:
        path = _folder(self) / "trail.json"
        write_trail_settings(path, settings)
        publisher = PreviewPublisher(9, max_fps=1000, trail_overlay=overlay, settings_file=path)
        self.addCleanup(publisher.close)
        return publisher

    def _publish(self, publisher: PreviewPublisher, **kwargs) -> tuple[Mock, list]:
        rendered = []
        with patch(
            "rhodes_fast.preview.render_preview",
            side_effect=lambda *a, **k: rendered.append(k) or np.zeros((80, 120, 3), np.uint8),
        ) as render:
            publisher.publish(
                np.zeros((80, 120, 3), dtype=np.uint8), [], None,
                target_y_ratio=0.5, fov_radius=30, inference_ms=1.0, detection_ms=1.0, **kwargs,
            )
            deadline = time.perf_counter() + 1.0
            while not rendered and time.perf_counter() < deadline:
                time.sleep(0.005)
        return render, rendered

    def test_the_frame_is_drawn_with_the_trail_for_its_own_row(self) -> None:
        overlay = Mock()
        sentinel = object()
        overlay.prepare.return_value = (sentinel, "trail 0.63px/ct 7f")
        publisher = self._publisher(overlay, _TRAIL_ON)

        render, _rendered = self._publish(publisher, trail_row=42)

        overlay.prepare.assert_called_once_with(42, (60.0, 40.0), _TRAIL_ON)
        self.assertIs(render.call_args.kwargs["trail"], sentinel)
        self.assertEqual(render.call_args.kwargs["trail_status"], "trail 0.63px/ct 7f")
        self.assertTrue(render.call_args.kwargs["show_frame"])

    def test_a_frame_without_a_row_has_no_trail(self) -> None:
        overlay = Mock()
        publisher = self._publisher(overlay, _TRAIL_ON)

        render, _rendered = self._publish(publisher)

        overlay.prepare.assert_not_called()
        self.assertIsNone(render.call_args.kwargs["trail"])

    def test_trail_only_renders_without_the_frame(self) -> None:
        overlay = Mock()
        overlay.prepare.return_value = (None, "trail calibrating")
        settings = TrailSettings(show_frame=False, enabled=True)
        publisher = self._publisher(overlay, settings)

        render, _rendered = self._publish(publisher, trail_row=3)

        self.assertFalse(render.call_args.kwargs["show_frame"])

    def test_nothing_is_rendered_when_both_are_off(self) -> None:
        overlay = Mock()
        # 给个正常的返回值: 否则工作线程在拆这个返回值时就抛了, 根本走不到渲染,
        # 这个测试就算把保护拿掉也会过。
        overlay.prepare.return_value = (None, "")
        publisher = self._publisher(overlay, TrailSettings(show_frame=False, enabled=False))

        render, _rendered = self._publish(publisher, trail_row=3)

        render.assert_not_called()

    def test_tells_the_aim_loop_whether_the_frame_is_shown(self) -> None:
        # 只看轨迹时不用为了画识别框去多做一次全类别检测。
        self.assertTrue(self._publisher(Mock(), TrailSettings()).shows_frame)
        self.assertFalse(self._publisher(Mock(), TrailSettings(show_frame=False, enabled=True)).shows_frame)


if __name__ == "__main__":
    unittest.main()
