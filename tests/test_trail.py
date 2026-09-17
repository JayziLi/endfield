from __future__ import annotations

import random
import unittest

import numpy as np

from rhodes_fast.trail import (
    Calibration,
    TrailCalibrator,
    TrailRecorder,
    TrailWindow,
    estimate_calibration,
    hand_includes_commands,
)


def _record(recorder: TrailRecorder, row: int, **overrides) -> int:
    values = {
        "time_s": row / 240.0,
        "command": (row, -row),
        "hand": (0.5 * row, 0.25 * row),
        "error": (float(row), float(2 * row)),
        "track": 1,
        "profile": 0,
    }
    values.update(overrides)
    return recorder.record(**values)


class RecorderTests(unittest.TestCase):
    def test_rows_are_numbered_in_the_order_they_were_written(self) -> None:
        recorder = TrailRecorder(capacity=8)
        self.assertEqual([_record(recorder, row) for row in range(3)], [0, 1, 2])
        self.assertEqual(recorder.written, 3)

    def test_a_window_is_oldest_first_and_ends_at_the_requested_row(self) -> None:
        recorder = TrailRecorder(capacity=8)
        for row in range(5):
            _record(recorder, row)

        window = recorder.window(3, 3)

        self.assertEqual(window.first_row, 1)
        self.assertEqual(len(window), 3)
        np.testing.assert_array_equal(window.command, [[1, -1], [2, -2], [3, -3]])
        np.testing.assert_array_equal(window.hand, [[0.5, 0.25], [1.0, 0.5], [1.5, 0.75]])
        np.testing.assert_array_equal(window.error, [[1, 2], [2, 4], [3, 6]])
        np.testing.assert_array_equal(window.track, [1, 1, 1])
        np.testing.assert_array_equal(window.profile, [0, 0, 0])
        np.testing.assert_allclose(window.time_s, [1 / 240, 2 / 240, 3 / 240])

    def test_a_window_survives_the_buffer_wrapping_around(self) -> None:
        recorder = TrailRecorder(capacity=8)
        for row in range(20):
            _record(recorder, row)

        window = recorder.window(19, 5)

        self.assertEqual(window.first_row, 15)
        np.testing.assert_array_equal(window.command[:, 0], [15, 16, 17, 18, 19])

    def test_rows_already_overwritten_are_left_out_rather_than_returned_stale(self) -> None:
        recorder = TrailRecorder(capacity=8)
        for row in range(20):
            _record(recorder, row)

        window = recorder.window(19, 50)

        # 最旧的那一格就是写入端下一个要覆盖的格子, 读的时候它可能正在变, 所以不给。
        self.assertEqual(window.first_row, 13)
        np.testing.assert_array_equal(window.command[:, 0], np.arange(13, 20))

    def test_a_window_can_end_before_the_newest_row(self) -> None:
        # 预览线程渲染的是交给它的那一帧, 不是主循环此刻写到的最新一帧。
        recorder = TrailRecorder(capacity=16)
        for row in range(10):
            _record(recorder, row)

        window = recorder.window(6, 3)

        np.testing.assert_array_equal(window.command[:, 0], [4, 5, 6])

    def test_a_window_is_clipped_to_the_rows_that_exist(self) -> None:
        recorder = TrailRecorder(capacity=16)
        for row in range(3):
            _record(recorder, row)

        self.assertEqual(len(recorder.window(2, 10)), 3)
        self.assertEqual(len(TrailRecorder(capacity=16).window(0, 10)), 0)

    def test_a_frame_without_a_target_stores_track_zero(self) -> None:
        recorder = TrailRecorder(capacity=8)
        _record(recorder, 0, error=None, track=7)

        window = recorder.window(0, 1)

        self.assertEqual(window.track[0], 0)
        np.testing.assert_array_equal(window.error[0], [0.0, 0.0])

    def test_the_window_is_a_copy_the_writer_cannot_change_afterwards(self) -> None:
        recorder = TrailRecorder(capacity=8)
        for row in range(4):
            _record(recorder, row)
        window = recorder.window(3, 4)
        for row in range(4, 12):
            _record(recorder, row)

        np.testing.assert_array_equal(window.command[:, 0], [0, 1, 2, 3])


_SCALE = 0.63
_LAG = 7


def _simulate(
    *,
    seed: int = 3,
    frames: int = 4096,
    hand_echoes_commands: bool = False,
    move_hand: bool = True,
    detection_noise: float = 0.8,
    tracking_gain: float = 0.03,
    scale: float = _SCALE,
) -> tuple[TrailRecorder, np.ndarray, np.ndarray]:
    """带延迟抖动和检测噪声的闭环: 返回记录器、真实的程序移动和真实的手移动。"""
    rng = random.Random(seed)
    recorder = TrailRecorder(capacity=frames)
    command = np.zeros((frames, 2))
    hand = np.zeros((frames, 2))
    camera = np.zeros((frames, 2))
    target = np.zeros((frames, 2))
    velocity = np.array([2.0, 0.0])
    flick_left = 0
    flick = np.zeros(2)
    hand_left = 0
    hand_step = np.zeros(2)
    moved = np.zeros(2)
    for k in range(frames):
        if k and k % rng.randint(50, 80) == 0:
            velocity = np.array([rng.choice((-2.0, 2.0)), rng.uniform(-0.3, 0.3)])
        target[k] = (target[k - 1] if k else 0.0) + velocity
        lag = _LAG + rng.randint(-1, 1)
        seen = max(0, k - lag)
        error = target[seen] - camera[seen] + np.array(
            [rng.gauss(0.0, detection_noise), rng.gauss(0.0, detection_noise)]
        )
        if flick_left == 0 and rng.random() < 0.01:
            flick_left = rng.randint(6, 14)
            flick = np.array([rng.uniform(-15, 15), rng.uniform(-8, 8)])
        if flick_left:
            flick_left -= 1
            command[k] = np.round(flick)
        else:
            command[k] = np.round(np.clip(error * tracking_gain, -3, 3))
        if move_hand:
            if hand_left == 0 and rng.random() < 0.006:
                hand_left = rng.randint(15, 30)
                hand_step = np.array([rng.uniform(-8, 8), rng.uniform(-4, 4)])
            if hand_left:
                hand_left -= 1
                hand[k] = np.round(hand_step)
        moved = moved + scale * (command[k] + hand[k])
        camera[k] = moved
        recorded_hand = hand[k] + command[k] if hand_echoes_commands else hand[k]
        recorder.record(
            time_s=k / 241.0,
            command=(int(command[k, 0]), int(command[k, 1])),
            hand=(float(recorded_hand[0]), float(recorded_hand[1])),
            error=(float(error[0]), float(error[1])),
            track=1,
            profile=0,
        )
    return recorder, command, hand


def _predicted_motion(calibration: Calibration, recorder: TrailRecorder) -> tuple[np.ndarray, int]:
    window = recorder.window(recorder.written - 1, recorder.written)
    motion = calibration.px_per_command * window.command + calibration.px_per_hand * window.hand
    return motion, window.first_row


def _windowed(values: np.ndarray, width: int = 60) -> np.ndarray:
    sums = np.cumsum(values, axis=0)
    return sums[width:] - sums[:-width]


def _whole(recorder: TrailRecorder) -> TrailWindow:
    return recorder.window(recorder.written - 1, recorder.written)


# 跟随时准心和目标同向运动, 回归会把比例估低。仿真里 20 个种子平均偏低约 15%,
# 所以带跟随的场景按 20% 验收; 不跟随的场景估计器是无偏的, 按 10% 验收。
_TRACKING_TOLERANCE = 0.2
_UNBIASED_TOLERANCE = 0.1


class CalibrationTests(unittest.TestCase):
    def test_recovers_the_loop_delay_and_pixels_per_count(self) -> None:
        recorder, _command, _hand = _simulate()

        calibration = estimate_calibration(_whole(recorder))

        self.assertIsNotNone(calibration)
        self.assertIn(calibration.lag_frames, (_LAG - 1, _LAG, _LAG + 1))
        self.assertAlmostEqual(calibration.px_per_command, _SCALE, delta=_SCALE * _TRACKING_TOLERANCE)
        self.assertAlmostEqual(calibration.px_per_hand, _SCALE, delta=_SCALE * _TRACKING_TOLERANCE)
        self.assertFalse(calibration.hand_includes_commands)

    def test_the_estimate_is_unbiased_when_nothing_is_tracking_the_target(self) -> None:
        recorder, _command, _hand = _simulate(tracking_gain=0.0)

        calibration = estimate_calibration(_whole(recorder))

        self.assertIsNotNone(calibration)
        self.assertAlmostEqual(calibration.px_per_command, _SCALE, delta=_SCALE * _UNBIASED_TOLERANCE)

    def test_hand_data_that_already_contains_the_commands_is_not_counted_twice(self) -> None:
        # 监听口的报文里含不含程序通过 KMBox 发出的移动, 没法事先知道。两列分开回归,
        # 画出来的总移动都必须对。
        recorder, command, hand = _simulate(hand_echoes_commands=True)

        calibration = estimate_calibration(recorder.window(recorder.written - 1, recorder.written))

        self.assertIsNotNone(calibration)
        predicted, first = _predicted_motion(calibration, recorder)
        truth = _windowed(_SCALE * (command + hand)[first:])
        predicted = _windowed(predicted)
        moving = np.abs(truth) > 20
        relative = np.abs(predicted[moving] - truth[moving]) / np.abs(truth[moving])
        self.assertLess(float(np.median(relative)), _TRACKING_TOLERANCE)

    def test_works_when_the_hand_never_moves(self) -> None:
        recorder, command, _hand = _simulate(move_hand=False)

        calibration = estimate_calibration(recorder.window(recorder.written - 1, recorder.written))

        self.assertIsNotNone(calibration)
        predicted, first = _predicted_motion(calibration, recorder)
        truth = _windowed(_SCALE * command[first:])
        predicted = _windowed(predicted)
        moving = np.abs(truth) > 20
        relative = np.abs(predicted[moving] - truth[moving]) / np.abs(truth[moving])
        self.assertLess(float(np.median(relative)), _TRACKING_TOLERANCE)

    def test_no_movement_gives_no_calibration(self) -> None:
        recorder = TrailRecorder(capacity=1024)
        rng = random.Random(1)
        for k in range(1024):
            recorder.record(
                time_s=k / 241.0, command=(0, 0), hand=(0.0, 0.0),
                error=(rng.gauss(0, 1), rng.gauss(0, 1)), track=1, profile=-1,
            )

        self.assertIsNone(estimate_calibration(recorder.window(1023, 1024)))

    def test_movement_without_any_target_gives_no_calibration(self) -> None:
        recorder = TrailRecorder(capacity=1024)
        for k in range(1024):
            recorder.record(
                time_s=k / 241.0, command=(10, 0), hand=(0.0, 0.0),
                error=None, track=0, profile=0,
            )

        self.assertIsNone(estimate_calibration(recorder.window(1023, 1024)))

    def test_responses_across_a_target_switch_are_ignored(self) -> None:
        # 换目标那一帧的误差跳变不是我们移动造成的。算进去的话, 一次换目标就是一个
        # 几十像素的假响应, 比例会被带歪。
        #
        # 换目标放在拉枪落地的那几帧: 只有这时候的响应会过移动量门槛进入统计。放在别处
        # 的话, 就算不检查是不是同一个目标, 也几乎没有假响应混进来, 测不出区别。
        # 跳变方向和拉枪同向, 正好抵消真实响应。不排除的话 10 次换目标就把 0.525 拽到 0.34。
        recorder, _command, _hand = _simulate()
        window = recorder.window(recorder.written - 1, recorder.written)
        rng = random.Random(9)
        error = window.error.copy()
        track = window.track.copy()
        for frame in range(_LAG, len(track)):
            landing = window.command[frame - _LAG, 0]
            if abs(landing) >= 6 and rng.random() < 0.03:
                error[frame:, 0] += 80.0 * np.sign(landing)
                track[frame:] += 1
        switched = type(window)(
            first_row=window.first_row, time_s=window.time_s, command=window.command,
            hand=window.hand, error=error, track=track, profile=window.profile,
        )
        self.assertGreaterEqual(int(np.count_nonzero(np.diff(track))), 8)

        clean = estimate_calibration(window)
        calibration = estimate_calibration(switched)

        self.assertIsNotNone(calibration)
        self.assertAlmostEqual(calibration.px_per_command, clean.px_per_command, delta=clean.px_per_command * 0.05)


def _shifted(window: TrailWindow, rows: int) -> TrailWindow:
    return TrailWindow(
        first_row=window.first_row + rows, time_s=window.time_s, command=window.command,
        hand=window.hand, error=window.error, track=window.track, profile=window.profile,
    )


def _slice(window: TrailWindow, first: int, last: int) -> TrailWindow:
    """窗口里第 first..last 行(按窗口内下标, 含两端)。"""
    return TrailWindow(
        first_row=window.first_row + first,
        time_s=window.time_s[first : last + 1],
        command=window.command[first : last + 1],
        hand=window.hand[first : last + 1],
        error=window.error[first : last + 1],
        track=window.track[first : last + 1],
        profile=window.profile[first : last + 1],
    )


class CalibratorTests(unittest.TestCase):
    """预览线程每秒喂一次新数据, 统计量要跨次累加。"""

    def test_feeding_a_second_at_a_time_reaches_the_same_answer_as_all_at_once(self) -> None:
        recorder, _command, _hand = _simulate()
        whole = _whole(recorder)
        calibrator = TrailCalibrator(decay_per_step=1.0)
        for end in range(241, len(whole) + 241, 241):
            last = min(end, len(whole)) - 1
            first = max(0, last - 241 - calibrator.context_rows)
            calibrator.update(_slice(whole, first, last))

        at_once = estimate_calibration(whole)

        self.assertIsNotNone(calibrator.calibration)
        # 延迟可以差一帧: 分块喂的时候前面几块先定下了一个 lag, 后来相邻的 lag 只好一点点,
        # 滞回不让它换。这正是想要的 —— 在途虚线长度不该一秒一变。
        self.assertLessEqual(abs(calibrator.calibration.lag_frames - at_once.lag_frames), 1)
        self.assertAlmostEqual(
            calibrator.calibration.px_per_hand, at_once.px_per_hand, delta=at_once.px_per_hand * 0.1
        )

    def test_rows_seen_before_are_not_counted_again(self) -> None:
        # 预览线程每次取的窗口会和上一次重叠。重叠部分再算一遍的话, 比例不变但配对数
        # 虚高, 衰减也会对不上。
        recorder, _command, _hand = _simulate()
        whole = _whole(recorder)
        overlapping = TrailCalibrator(decay_per_step=1.0)
        overlapping.update(_slice(whole, 0, 2000))
        overlapping.update(_slice(whole, 1000, len(whole) - 1))
        disjoint = TrailCalibrator(decay_per_step=1.0)
        disjoint.update(_slice(whole, 0, 2000))
        disjoint.update(_slice(whole, 2000 - disjoint.context_rows - 3, len(whole) - 1))

        self.assertEqual(overlapping.calibration, disjoint.calibration)

    def test_the_same_window_twice_changes_nothing(self) -> None:
        recorder, _command, _hand = _simulate()
        whole = _whole(recorder)
        calibrator = TrailCalibrator()
        first = calibrator.update(whole)

        self.assertEqual(calibrator.update(whole), first)

    def test_follows_a_change_in_sensitivity(self) -> None:
        # 换了游戏或者改了灵敏度, 旧数据要慢慢淡出。
        before, _command, _hand = _simulate(scale=0.63)
        after, _command, _hand = _simulate(scale=1.0, seed=8)
        calibrator = TrailCalibrator(decay_per_step=0.5)
        calibrator.update(_whole(before))
        self.assertLess(calibrator.calibration.px_per_hand, 0.8)

        calibrator.update(_shifted(_whole(after), before.written))

        self.assertAlmostEqual(calibrator.calibration.px_per_hand, 1.0, delta=_TRACKING_TOLERANCE)


class EchoTests(unittest.TestCase):
    """监听口报上来的手的移动里, 含不含程序发出去的移动。"""

    def test_hand_data_with_the_commands_echoed_back_is_recognised(self) -> None:
        recorder, _command, _hand = _simulate(hand_echoes_commands=True)
        self.assertTrue(hand_includes_commands(_whole(recorder)))

    def test_independent_hand_data_is_recognised(self) -> None:
        recorder, _command, _hand = _simulate()
        self.assertFalse(hand_includes_commands(_whole(recorder)))

    def test_a_hand_that_never_moves_does_not_look_like_an_echo(self) -> None:
        recorder, _command, _hand = _simulate(move_hand=False)
        self.assertFalse(hand_includes_commands(_whole(recorder)))

    def test_an_echo_is_recognised_even_when_the_hand_never_moves(self) -> None:
        recorder, _command, _hand = _simulate(move_hand=False, hand_echoes_commands=True)
        self.assertTrue(hand_includes_commands(_whole(recorder)))

    def test_an_echo_that_arrives_a_few_frames_late_is_still_recognised(self) -> None:
        # 指令发出去之后 KMBox 才报回来, 可能要过一两帧才被主循环取走。
        # 指令每帧随机、前后不相关: 拉枪那种连续几帧一样的指令, 晚到的回声和同一帧的
        # 指令本来就相关, 测不出「往后看几帧」到底有没有起作用。
        rng = random.Random(2)
        frames = 600
        command = np.array(
            [[rng.choice((-1, 1)) * rng.randint(3, 15), rng.choice((-1, 1)) * rng.randint(3, 15)] for _ in range(frames)],
            dtype=float,
        )
        late = np.zeros_like(command)
        late[2:] = command[:-2]
        delayed = TrailWindow(
            first_row=0, time_s=np.arange(frames) / 241.0, command=command, hand=late,
            error=np.zeros((frames, 2)), track=np.ones(frames, dtype=np.int64),
            profile=np.zeros(frames, dtype=np.int64),
        )
        self.assertTrue(hand_includes_commands(delayed))

    def test_no_commands_means_no_echo(self) -> None:
        recorder = TrailRecorder(capacity=512)
        for k in range(512):
            recorder.record(
                time_s=k / 241.0, command=(0, 0), hand=(5.0, 0.0),
                error=(0.0, 0.0), track=1, profile=-1,
            )
        self.assertFalse(hand_includes_commands(_whole(recorder)))


if __name__ == "__main__":
    unittest.main()
