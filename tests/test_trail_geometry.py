from __future__ import annotations

import unittest

import numpy as np

from rhodes_fast.trail import (
    Calibration,
    TrailGeometry,
    TrailRecorder,
    TrailWindow,
    draw_trail,
    trail_geometry,
    trail_status,
)

_CENTER = (160.0, 160.0)


def _calibration(lag: int = 3, px_per_command: float = 1.0, px_per_hand: float = 1.0) -> Calibration:
    return Calibration(
        lag_frames=lag,
        px_per_command=px_per_command,
        px_per_hand=px_per_hand,
        hand_includes_commands=False,
        correlation=0.8,
        pairs=1000.0,
    )


def _window(
    frames: int,
    *,
    fps: float = 100.0,
    command=lambda k: (0, 0),
    hand=lambda k: (0.0, 0.0),
    error=lambda k: (0.0, 0.0),
    track=lambda k: 1,
    profile=lambda k: 0,
) -> TrailWindow:
    recorder = TrailRecorder(capacity=frames + 1)
    for k in range(frames):
        recorder.record(
            time_s=k / fps,
            command=command(k),
            hand=hand(k),
            error=error(k),
            track=track(k),
            profile=profile(k),
        )
    return recorder.window(frames - 1, frames)


def _geometry(window: TrailWindow, calibration: Calibration | None = None, **kwargs) -> TrailGeometry:
    options = {"seconds": 0.5, "center": _CENTER, "optimal_path": False}
    options.update(kwargs)
    return trail_geometry(window, calibration or _calibration(), **options)


class CrosshairTrailTests(unittest.TestCase):
    def test_the_solid_trail_ends_at_the_centre(self) -> None:
        geometry = _geometry(_window(60, command=lambda k: (k % 7, -(k % 5))))
        np.testing.assert_allclose(geometry.landed[-1], _CENTER)

    def test_older_points_lie_behind_the_direction_of_travel(self) -> None:
        # 镜头一直往右转, 过去的准心位置在现在的画面里就在左边。
        geometry = _geometry(_window(60, command=lambda k: (10, 0)), seconds=0.1)

        xs = geometry.landed[:, 0]
        self.assertTrue(np.all(np.diff(xs) > 0))
        np.testing.assert_allclose(geometry.landed[:, 1], _CENTER[1])
        self.assertAlmostEqual(xs[0], _CENTER[0] - 10 * (len(xs) - 1))

    def test_the_dashed_part_is_what_was_sent_but_is_not_on_screen_yet(self) -> None:
        # 延迟 3 帧: 最近 3 帧发出去的移动还没出现在画面里, 虚线的终点是准心此刻真正在的地方。
        geometry = _geometry(_window(60, command=lambda k: (10, 5)), _calibration(lag=3))

        np.testing.assert_allclose(
            geometry.in_flight,
            [[160, 160], [170, 165], [180, 170], [190, 175]],
        )

    def test_commands_and_hand_movement_use_their_own_scale(self) -> None:
        geometry = _geometry(
            _window(60, command=lambda k: (4, 0), hand=lambda k: (0.0, 3.0)),
            _calibration(lag=2, px_per_command=0.5, px_per_hand=2.0),
        )

        np.testing.assert_allclose(geometry.in_flight[-1], [160 + 2 * 2.0, 160 + 2 * 6.0])

    def test_the_trail_length_is_measured_in_time_not_frames(self) -> None:
        # 采集帧率不是固定的 241, 用每一行记下的时间戳来截。
        geometry = _geometry(_window(100, fps=100.0, command=lambda k: (1, 0)), seconds=0.1)
        self.assertEqual(len(geometry.landed), 11)

        faster = _geometry(_window(100, fps=200.0, command=lambda k: (1, 0)), seconds=0.1)
        self.assertEqual(len(faster.landed), 21)

    def test_older_segments_are_older(self) -> None:
        geometry = _geometry(_window(80, command=lambda k: (2, 0)), seconds=0.5)

        self.assertEqual(geometry.landed_age[-1], 0.0)
        self.assertTrue(np.all(np.diff(geometry.landed_age) < 0))
        self.assertLess(geometry.landed_age[0], 1.0)

    def test_a_window_shorter_than_the_delay_draws_no_solid_trail(self) -> None:
        geometry = _geometry(_window(3, command=lambda k: (5, 0)), _calibration(lag=5))

        self.assertLessEqual(len(geometry.landed), 1)
        self.assertEqual(len(geometry.landed_age), 0)


class TargetTrailTests(unittest.TestCase):
    def test_a_target_that_stands_still_stays_in_one_place_while_the_camera_moves(self) -> None:
        # 对齐正确与否就看这个: 世界里不动的目标, 换算到当前画面上必须是一个点。
        lag = 4
        scale = 0.6
        world_target = np.array([40.0, -25.0])
        commands = [(int(6 * np.sin(k / 5)), int(4 * np.cos(k / 7))) for k in range(90)]
        moved = np.cumsum(np.array(commands, dtype=float) * scale, axis=0)

        def error(k):
            seen = k - lag
            camera = moved[seen] if seen >= 0 else np.zeros(2)
            return tuple(world_target - camera)

        geometry = _geometry(
            _window(90, command=lambda k: commands[k], error=error),
            _calibration(lag=lag, px_per_command=scale),
            seconds=0.5,
        )

        self.assertEqual(len(geometry.target), 1)
        points = geometry.target[0]
        self.assertGreater(len(points), 10)
        np.testing.assert_allclose(points, np.tile(points[-1], (len(points), 1)), atol=1e-9)
        np.testing.assert_allclose(points[-1], np.array(_CENTER) + world_target - moved[89 - lag])

    def test_the_target_trail_breaks_where_the_target_is_lost_or_changes(self) -> None:
        def track(k):
            if k < 20:
                return 1
            if k < 25:
                return 0
            if k < 35:
                return 1
            return 2

        geometry = _geometry(_window(50, error=lambda k: (float(k), 0.0), track=track), seconds=1.0)

        self.assertEqual([len(line) for line in geometry.target], [17, 10, 15])


class OptimalPathTests(unittest.TestCase):
    def test_runs_from_where_the_aim_started_to_the_target_now(self) -> None:
        lag = 3
        geometry = _geometry(
            _window(
                60,
                command=lambda k: (4, 0) if k >= 40 else (0, 0),
                error=lambda k: (30.0, 12.0),
                profile=lambda k: 0 if k >= 40 else -1,
            ),
            _calibration(lag=lag),
            optimal_path=True,
        )

        self.assertIsNotNone(geometry.optimal)
        start, end = geometry.optimal
        # 按下触发键是第 40 帧; 那一帧画面里准心在的位置是 P(40 - lag)。
        expected_start = geometry.landed[len(geometry.landed) - 1 - ((59 - lag) - (40 - lag))]
        np.testing.assert_allclose(start, expected_start)
        np.testing.assert_allclose(end, [190.0, 172.0])

    def test_is_off_unless_asked_for(self) -> None:
        geometry = _geometry(
            _window(60, error=lambda k: (30.0, 0.0), profile=lambda k: 0 if k >= 40 else -1),
        )
        self.assertIsNone(geometry.optimal)

    def test_a_target_switch_while_aiming_starts_a_new_path(self) -> None:
        geometry = _geometry(
            _window(
                60,
                command=lambda k: (3, 0),
                error=lambda k: (20.0, 0.0),
                track=lambda k: 2 if k >= 50 else 1,
                profile=lambda k: 0 if k >= 10 else -1,
            ),
            _calibration(lag=3),
            optimal_path=True,
            seconds=1.0,
        )
        start, _end = geometry.optimal
        # 起点是第 50 帧画面里的准心, 即 landed 里下标 50-3 的那个点。
        np.testing.assert_allclose(start, geometry.landed[50 - 3])

    def test_a_dropped_frame_of_the_same_target_does_not_restart_the_path(self) -> None:
        geometry = _geometry(
            _window(
                60,
                command=lambda k: (3, 0),
                error=lambda k: (20.0, 0.0),
                track=lambda k: 0 if k == 50 else 1,
                profile=lambda k: 0 if k >= 10 else -1,
            ),
            _calibration(lag=3),
            optimal_path=True,
            seconds=1.0,
        )
        start, _end = geometry.optimal
        np.testing.assert_allclose(start, geometry.landed[10 - 3])

    def test_disappears_once_the_start_has_faded_out_of_the_trail(self) -> None:
        geometry = _geometry(
            _window(200, error=lambda k: (20.0, 0.0), profile=lambda k: 0 if k >= 10 else -1),
            optimal_path=True,
            seconds=0.5,
        )
        self.assertIsNone(geometry.optimal)

    def test_needs_a_target_right_now(self) -> None:
        geometry = _geometry(
            _window(
                60,
                error=lambda k: (20.0, 0.0),
                track=lambda k: 0 if k >= 55 else 1,
                profile=lambda k: 0 if k >= 40 else -1,
            ),
            optimal_path=True,
        )
        self.assertIsNone(geometry.optimal)


def _blank() -> np.ndarray:
    return np.zeros((200, 200, 3), dtype=np.uint8)


def _geometry_of(
    landed=((20.0, 100.0), (180.0, 100.0)),
    age=0.0,
    in_flight=((180.0, 100.0),),
    target=(),
    optimal=None,
) -> TrailGeometry:
    landed = np.array(landed, dtype=float)
    segments = max(0, len(landed) - 1)
    return TrailGeometry(
        landed=landed,
        landed_age=np.full(segments, age),
        in_flight=np.array(in_flight, dtype=float),
        target=tuple(np.array(line, dtype=float) for line in target),
        optimal=optimal,
    )


def _is_yellow(pixel) -> bool:
    blue, green, red = (int(value) for value in pixel)
    return red > 180 and green > 150 and blue < 90


def _lit_rows(image: np.ndarray, column: int) -> int:
    return int((image[:, column].max(axis=1) > 60).sum())


class DrawTests(unittest.TestCase):
    def test_the_newest_part_is_yellow(self) -> None:
        image = _blank()
        draw_trail(image, _geometry_of(age=0.0))
        self.assertTrue(_is_yellow(image[100, 100]), image[100, 100])

    def test_the_oldest_part_has_faded_to_grey(self) -> None:
        image = _blank()
        draw_trail(image, _geometry_of(age=1.0))
        pixel = image[100, 100].astype(int)
        self.assertGreater(pixel.min(), 100)
        self.assertLess(pixel.max() - pixel.min(), 12)

    def test_the_fade_is_gradual(self) -> None:
        # 一条从旧到新的轨迹, 沿着走过去颜色是一点点变黄的, 不是突然跳过去。
        image = _blank()
        points = [(10.0 + 18.0 * index, 100.0) for index in range(11)]
        geometry = TrailGeometry(
            landed=np.array(points),
            landed_age=np.linspace(1.0, 0.0, 10),
            in_flight=np.array([points[-1]]),
            target=(),
            optimal=None,
        )
        draw_trail(image, geometry)
        blue = [int(image[100, int(10 + 18 * index + 9)][0]) for index in range(10)]
        self.assertTrue(all(later <= earlier for earlier, later in zip(blue, blue[1:])), blue)
        self.assertGreaterEqual(len(set(blue)), 6, blue)

    def test_the_trail_keeps_its_width_as_it_fades(self) -> None:
        # 目标轨迹是 1 像素的灰线。准心轨迹淡成灰以后要是也变细, 两条就分不清了。
        fresh = _blank()
        draw_trail(fresh, _geometry_of(age=0.0))
        faded = _blank()
        draw_trail(faded, _geometry_of(age=1.0))
        self.assertEqual(_lit_rows(faded, 100), _lit_rows(fresh, 100))
        target = _blank()
        draw_trail(target, _geometry_of(landed=((0.0, 0.0),), in_flight=((0.0, 0.0),), target=(((20.0, 100.0), (180.0, 100.0)),)))
        self.assertLess(_lit_rows(target, 100), _lit_rows(faded, 100))

    def test_the_in_flight_part_is_dashed_and_yellow(self) -> None:
        image = _blank()
        draw_trail(image, _geometry_of(landed=((100.0, 150.0),), in_flight=((10.0, 150.0), (190.0, 150.0))))
        row = image[150, 15:185].max(axis=1)
        self.assertGreater(int((row > 100).sum()), 30)
        self.assertGreater(int((row == 0).sum()), 30)
        lit = [image[150, column] for column in range(15, 185) if image[150, column].max() > 200]
        self.assertTrue(lit and all(_is_yellow(pixel) for pixel in lit))

    def test_the_target_trail_is_grey(self) -> None:
        image = _blank()
        draw_trail(image, _geometry_of(landed=((0.0, 0.0),), in_flight=((0.0, 0.0),), target=(((20.0, 60.0), (180.0, 60.0)),)))
        pixel = image[60, 100].astype(int)
        self.assertGreater(pixel.min(), 60)
        self.assertLess(pixel.max() - pixel.min(), 12)

    def test_the_target_trail_can_be_left_out(self) -> None:
        image = _blank()
        draw_trail(
            image,
            _geometry_of(landed=((0.0, 0.0),), in_flight=((0.0, 0.0),), target=(((20.0, 60.0), (180.0, 60.0)),)),
            with_target=False,
        )
        self.assertEqual(int(image.max()), 0)

    def test_the_optimal_path_is_dotted(self) -> None:
        image = _blank()
        draw_trail(
            image,
            _geometry_of(landed=((0.0, 0.0),), in_flight=((0.0, 0.0),), optimal=((10.0, 40.0), (190.0, 40.0))),
        )
        row = image[40, 15:185].max(axis=1)
        self.assertGreater(int((row > 100).sum()), 20)
        self.assertGreater(int((row == 0).sum()), 20)

    def test_nothing_is_drawn_for_an_empty_trail(self) -> None:
        image = _blank()
        draw_trail(image, _geometry_of(landed=((100.0, 100.0),), in_flight=((100.0, 100.0),)))
        self.assertEqual(int(image.max()), 0)


class StatusTests(unittest.TestCase):
    def test_says_it_is_calibrating_until_it_has_numbers(self) -> None:
        self.assertEqual(trail_status(None), "trail calibrating")

    def test_shows_pixels_per_count_and_delay(self) -> None:
        self.assertEqual(trail_status(_calibration(lag=7, px_per_hand=0.634)), "trail 0.63px/ct 7f")


if __name__ == "__main__":
    unittest.main()
