"""预览里的瞄准轨迹: 记录、标定、换算坐标、画线。

主循环每帧往环形缓冲里写一行, 其余全部在预览线程里做。准心永远在画面中心, 移动的
是镜头, 所以「轨迹」要把每帧的鼠标移动换算成像素、再按回路延迟对齐到正在显示的那一帧上。
这两个换算系数取决于游戏灵敏度和采集缩放, 没法事先知道, 只能从数据里估。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS

# 241fps 下约 17 秒。标定要攒够拉枪帧, 轨迹最长只画 2 秒, 两者都够。
CAPACITY = 4096

_TIME, _COMMAND_X, _COMMAND_Y, _HAND_X, _HAND_Y, _ERROR_X, _ERROR_Y, _TRACK, _PROFILE = range(9)
_COLUMNS = 9

_MAX_LAG = 30
# 只用移动量够大的帧, 拉枪太少时再逐级放宽。
#
# 门槛高是为了躲开「跟随偏差」: 跟随时准心和目标同向运动, 误差变化小, 回归会以为移动
# 没什么效果。跟随指令小、拉枪指令大, 而拉枪方向和目标自己往哪走无关。仿真里 20 个
# 种子: 不跟随时无偏(0.636, 真值 0.63); 带跟随、门槛 6 时仍偏低约 15%(0.53)。
# 9/9 真实日志在门槛 3/6/12 下是 0.636/0.656/0.616, 说明实战里这份偏差小得多。
# 试过把响应和移动都差分来消掉目标速度: 无偏, 但标准差 0.2 到 6, 不能用。
# 也试过把 lag 左右几帧一起回归来吸收延迟抖动: 门槛 6 下和单帧结果一样, 不值得。
_MOVE_THRESHOLDS = (6.0, 3.0, 1.0)
_MIN_PAIRS = 200
# 9/9 那份真实日志在 0.34~0.71 之间。门槛定在它下面留出余量,
# 但要高过「毫无关系」—— 纯噪声的相关系数在几百个配对上落在 ±0.1 以内。
_MIN_CORRELATION = 0.25
# 统计量每处理 241 帧(241fps 下一秒)乘一次这个数, 记忆大约 100 秒。
# 真实日志上按秒更新: 0.97 时比例在 0.44~0.89 之间来回跳、延迟换了 46 次;
# 0.99 加上下面的滞回, 每秒变化 p95 2%, 延迟一整局只换 1 次。
_DECAY_PER_STEP = 0.99
_FRAMES_PER_STEP = 241
# 相关性峰很宽, 相邻几个 lag 差不多高。新的 lag 要比现在的明显更好才换, 免得轨迹
# 的在途虚线长度一秒一变。
_LAG_SWITCH_MARGIN = 0.03
# 程序发出的指令, 监听口在接下来几帧里报不报回来。
_ECHO_FRAMES = 4
_ECHO_MIN_PAIRS = 50
_ECHO_COMMAND = 3.0
# 一次处理这么多帧就让一次 GIL。预览页刚打开时可能一下子要补 17 秒的数据。
_CHUNK_FRAMES = 512


@dataclass(frozen=True, slots=True)
class TrailWindow:
    """一段连续的行, 最旧的在前。都是拷贝, 写入端之后怎么写都不影响它。"""

    first_row: int
    time_s: np.ndarray
    command: np.ndarray
    hand: np.ndarray
    error: np.ndarray
    track: np.ndarray
    profile: np.ndarray

    def __len__(self) -> int:
        return len(self.time_s)


class TrailRecorder:
    """一个写入端(主循环)、一个读取端(预览线程)的环形缓冲。

    主循环只做一次整行赋值再把计数加一, 实测 620 纳秒, 一帧的预算是 4150000 纳秒。
    不加锁: 读取端只读计数之前的行, 读完再看一眼计数, 把读的过程中可能被覆盖的行丢掉。
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        self.capacity = capacity
        self._rows = np.zeros((capacity, _COLUMNS))
        self._written = 0

    @property
    def written(self) -> int:
        return self._written

    def record(
        self,
        *,
        time_s: float,
        command: tuple[int, int],
        hand: tuple[float, float],
        error: tuple[float, float] | None,
        track: int,
        profile: int,
    ) -> int:
        row = self._written
        if error is None:
            error = (0.0, 0.0)
            track = 0
        self._rows[row % self.capacity] = (
            time_s, command[0], command[1], hand[0], hand[1], error[0], error[1], track, profile,
        )
        # 必须在整行写完之后才加: 读取端靠这个计数判断哪些行已经完整。
        self._written = row + 1
        return row

    def window(self, end_row: int, count: int) -> TrailWindow:
        written = self._written
        end = min(end_row, written - 1)
        start = max(end - count + 1, 0, written - self.capacity)
        if end < start:
            return _empty_window(max(0, end + 1))
        rows = self._rows[np.arange(start, end + 1) % self.capacity]
        # 写入端此刻可能正在写的那个格子, 存的是还没被覆盖掉的最旧一行。读的过程中
        # 它可能已经变了, 所以最旧的那一行一律不要。
        oldest_safe = self._written - self.capacity + 1
        if start < oldest_safe:
            rows = rows[oldest_safe - start :]
            start = oldest_safe
        return TrailWindow(
            first_row=start,
            time_s=rows[:, _TIME],
            command=rows[:, _COMMAND_X : _COMMAND_Y + 1],
            hand=rows[:, _HAND_X : _HAND_Y + 1],
            error=rows[:, _ERROR_X : _ERROR_Y + 1],
            track=rows[:, _TRACK].astype(np.int64),
            profile=rows[:, _PROFILE].astype(np.int64),
        )


def _empty_window(first_row: int) -> TrailWindow:
    pairs = np.zeros((0, 2))
    return TrailWindow(
        first_row=first_row,
        time_s=np.zeros(0),
        command=pairs,
        hand=pairs,
        error=pairs,
        track=np.zeros(0, dtype=np.int64),
        profile=np.zeros(0, dtype=np.int64),
    )


@dataclass(frozen=True, slots=True)
class Calibration:
    lag_frames: int
    # 画轨迹用的是这两个: 一帧的镜头移动(像素) = 程序指令 x px_per_command + 手 x px_per_hand。
    # 监听口报回了程序发的移动时, 手那一列已经含有程序移动, px_per_command 就是 0。
    px_per_command: float
    px_per_hand: float
    hand_includes_commands: bool
    correlation: float
    pairs: float


_N, _SX, _SY, _SXX, _SYY, _SXY = range(6)


class TrailCalibrator:
    """估回路延迟帧数和每计数像素, 统计量跨多次更新累加。

    延迟: 让「第 k-L 帧的镜头移动」和「第 k 帧的误差变化」最相关的那个 L。
    比例: 这个 L 上的回归斜率。

    每次只处理上次之后的新帧, 旧数据按帧数衰减。单看 17 秒一段的真实日志, 比例能在
    0.38~1.08 之间跳; 累加之后是稳定收敛的。
    「监听口含不含程序移动」两种假设的统计量一起攒, 判断结果变了也不用重来。
    """

    def __init__(self, *, max_lag: int = _MAX_LAG, decay_per_step: float = _DECAY_PER_STEP) -> None:
        self.max_lag = max_lag
        self.decay_per_step = decay_per_step
        # [假设(0=不含, 1=含), 门槛, lag-1, 统计量]
        self._stats = np.zeros((2, len(_MOVE_THRESHOLDS), max_lag, 6))
        # 回声: [配对数, Σ指令², Σ指令·之后几帧的手]
        self._echo = np.zeros(3)
        self._next_row = 0
        self._lag: tuple[bool, int] | None = None
        self.calibration: Calibration | None = None

    @property
    def context_rows(self) -> int:
        """update 需要新帧之前还带着这么多行。"""
        return self.max_lag + 1

    def rows_needed(self, end_row: int) -> int:
        """想处理到 end_row 为止, 窗口要取多少行: 没见过的新行加上前面的上下文。"""
        return max(0, end_row - self._next_row + 1) + self.context_rows

    def update(self, window: TrailWindow) -> Calibration | None:
        last = window.first_row + len(window) - 1
        start = max(self._next_row, window.first_row + self.max_lag)
        # 回声要看之后几帧的手, 最后几帧留到下次。
        stop = last - (_ECHO_FRAMES - 1)
        if stop < start:
            return self.calibration
        decay = self.decay_per_step ** ((stop - start + 1) / _FRAMES_PER_STEP)
        self._stats *= decay
        self._echo *= decay
        for chunk_start in range(start, stop + 1, _CHUNK_FRAMES):
            chunk_stop = min(stop, chunk_start + _CHUNK_FRAMES - 1)
            self._accumulate(window, chunk_start - window.first_row, chunk_stop - window.first_row)
            time.sleep(0)
        self._next_row = stop + 1
        self.calibration = self._decide()
        return self.calibration

    def _accumulate(self, window: TrailWindow, first: int, last: int) -> None:
        frames = np.arange(first, last + 1)
        self._echo += _echo_sums(window, frames)
        # 第 k 帧的响应只在「相邻两帧看的是同一个目标」时有意义。换目标、丢目标那一帧的
        # 误差跳变不是我们移动造成的。
        track = window.track
        same_target = (track[frames] != 0) & (track[frames] == track[frames - 1])
        response = -(window.error[frames] - window.error[frames - 1])
        lags = np.arange(1, self.max_lag + 1)
        for hypothesis, motion in enumerate((window.command + window.hand, window.hand)):
            moved = motion[frames[None, :] - lags[:, None]]
            size = np.abs(moved)
            for level, threshold in enumerate(_MOVE_THRESHOLDS):
                mask = (size >= threshold) & same_target[None, :, None]
                x = np.where(mask, moved, 0.0)
                y = np.where(mask, response[None, :, :], 0.0)
                stats = self._stats[hypothesis, level]
                stats[:, _N] += mask.sum(axis=(1, 2))
                stats[:, _SX] += x.sum(axis=(1, 2))
                stats[:, _SY] += y.sum(axis=(1, 2))
                stats[:, _SXX] += (x * x).sum(axis=(1, 2))
                stats[:, _SYY] += (y * y).sum(axis=(1, 2))
                stats[:, _SXY] += (x * y).sum(axis=(1, 2))

    def _decide(self) -> Calibration | None:
        includes = _echo_decision(self._echo)
        for stats in self._stats[int(includes)]:
            pairs = stats[:, _N]
            correlation = _correlations(stats)
            usable = (pairs >= _MIN_PAIRS) & np.isfinite(correlation)
            if not usable.any():
                continue
            correlation = np.where(usable, correlation, -np.inf)
            best = int(np.argmax(correlation))
            lag = best
            if self._lag is not None and self._lag[0] == includes and usable[self._lag[1]]:
                current = self._lag[1]
                if correlation[best] <= correlation[current] + _LAG_SWITCH_MARGIN:
                    lag = current
            self._lag = (includes, lag)
            if correlation[lag] < _MIN_CORRELATION or stats[lag, _SXX] <= 0.0:
                return None
            scale = float(stats[lag, _SXY] / stats[lag, _SXX])
            if scale <= 0.0:
                return None
            return Calibration(
                lag_frames=lag + 1,
                px_per_command=0.0 if includes else scale,
                px_per_hand=scale,
                hand_includes_commands=includes,
                correlation=float(correlation[lag]),
                pairs=float(pairs[lag]),
            )
        return None


def estimate_calibration(window: TrailWindow, *, max_lag: int = _MAX_LAG) -> Calibration | None:
    """只看这一段数据估一次, 不累加。"""
    return TrailCalibrator(max_lag=max_lag, decay_per_step=1.0).update(window)


def hand_includes_commands(window: TrailWindow) -> bool:
    """监听口报上来的「手的移动」里, 含不含程序通过 KMBox 发出去的移动。

    物理上只有含或不含两种。直接看: 程序发出一条指令后, 接下来几帧的监听数据里有没有
    同样的位移。含的话回归系数约等于 1, 不含约等于 0。这一步完全不看画面, 不受目标
    移动和跟随偏差的干扰。
    """
    frames = np.arange(0, len(window) - _ECHO_FRAMES + 1)
    if frames.size == 0:
        return False
    return _echo_decision(_echo_sums(window, frames))


def _echo_sums(window: TrailWindow, frames: np.ndarray) -> np.ndarray:
    sent = window.command[frames]
    echoed = sum(window.hand[frames + offset] for offset in range(_ECHO_FRAMES))
    loud = np.abs(sent) >= _ECHO_COMMAND
    return np.array(
        [float(loud.sum()), float((sent * sent)[loud].sum()), float((sent * echoed)[loud].sum())]
    )


def _echo_decision(sums: np.ndarray) -> bool:
    pairs, sent_squared, sent_times_echo = sums
    return bool(pairs >= _ECHO_MIN_PAIRS and sent_squared > 0.0 and sent_times_echo / sent_squared >= 0.5)


def _correlations(stats: np.ndarray) -> np.ndarray:
    n = stats[:, _N]
    with np.errstate(invalid="ignore", divide="ignore"):
        return (n * stats[:, _SXY] - stats[:, _SX] * stats[:, _SY]) / np.sqrt(
            (n * stats[:, _SXX] - stats[:, _SX] ** 2) * (n * stats[:, _SYY] - stats[:, _SY] ** 2)
        )


# OpenCV 是 BGR。准心轨迹新的一端黄, 越旧越接近灰; 不分方案、不分是不是手在动。
TRAIL_NEW = (40, 215, 255)
TRAIL_OLD = (140, 140, 140)
# 比准心轨迹旧的那一端暗, 而且只有 1 像素: 准心轨迹淡成灰以后还是 2 像素, 两条分得开。
TARGET_GREY = (95, 95, 95)
OPTIMAL_COLOR = (225, 225, 225)
TRAIL_NEEDS_KMBOX = "trail needs KMBox"
_TRAIL_WIDTH = 2
# 按新旧分这么多档画。每档一次 polylines, 而不是每段一次 line: 2 秒的轨迹有 480 段,
# 逐段画是 480 次 Python 调用。16 档在 2 秒的轨迹上看不出台阶。
_FADE_LEVELS = 16
# 两像素粗的抗锯齿线两头各多出 1~2 像素。画 6 空 5 实际渲染出来空隙只剩亮度一半的
# 两个像素, 看不出是虚线; 画 5 空 8 实际是亮 8 暗 5。
_DASH_ON = 5.0
_DASH_OFF = 8.0
_DOT_SPACING = 6.0


@dataclass(frozen=True, slots=True)
class TrailGeometry:
    # 实线: 已经出现在画面里的准心位置, 最旧的在前, 最后一个就是画面中心。
    landed: np.ndarray
    # 第 i 段(landed[i] 到 landed[i+1])有多旧: 0 = 最新, 1 = 轨迹里最旧的那一段。
    landed_age: np.ndarray
    # 虚线: 从画面中心接着画已经发出、还没出现在画面里的移动。最后一个点是准心此刻的真实位置。
    in_flight: np.ndarray
    # 目标瞄准点走过的路径, 换目标或丢目标的地方断开。
    target: tuple[np.ndarray, ...]
    optimal: tuple[tuple[float, float], tuple[float, float]] | None


def trail_geometry(
    window: TrailWindow,
    calibration: Calibration,
    *,
    seconds: float,
    center: tuple[float, float],
    optimal_path: bool,
) -> TrailGeometry:
    """把窗口里的数据换算成「窗口最后一行那一帧」画面上的坐标。

    记第 i 帧的镜头移动为 m[i](像素), C(i) = m[0] + ... + m[i]。第 k 帧画面里, 截止第
    k-L 帧发出的移动都已经落地。所以:
    - 截止第 i 帧的移动都落地时, 准心在第 k 帧画面上的位置是 中心 + C(i) - C(k-L);
    - 第 j 帧看到的目标瞄准点, 在第 k 帧画面上是 中心 + e[j] - (C(k-L) - C(j-L))。
    """
    count = len(window)
    origin = np.asarray(center, dtype=float)
    if count == 0:
        return _empty_geometry(origin)
    newest = count - 1
    lag = calibration.lag_frames
    motion = calibration.px_per_command * window.command + calibration.px_per_hand * window.hand
    # cumulative[i + 1] = C(i), cumulative[0] = 窗口开始之前 = 0。
    cumulative = np.vstack((np.zeros((1, 2)), np.cumsum(motion, axis=0)))
    now_on_screen = newest - lag
    anchor = cumulative[max(now_on_screen, -1) + 1]
    oldest_snapshot = int(
        np.searchsorted(window.time_s, window.time_s[newest] - seconds - 1e-9, side="left")
    )

    first_landed = max(0, oldest_snapshot - lag)
    if now_on_screen >= first_landed:
        rows = np.arange(first_landed, now_on_screen + 1)
        landed = origin + cumulative[rows + 1] - anchor
        segment_rows = rows[1:]
        landed_age = (now_on_screen - segment_rows) / max(1, now_on_screen - first_landed)
    else:
        landed = np.zeros((0, 2))
        landed_age = np.zeros(0)

    flight_rows = np.arange(max(now_on_screen, -1), newest + 1)
    in_flight = origin + cumulative[flight_rows + 1] - anchor

    snapshots = np.arange(max(oldest_snapshot, lag), newest + 1)
    target = _target_lines(
        origin + window.error[snapshots] - (anchor - cumulative[snapshots - lag + 1]),
        window.track[snapshots],
    )

    optimal = None
    if optimal_path and window.track[newest] != 0 and now_on_screen >= 0:
        start = _latest_aim_start(window, max(1, oldest_snapshot, lag))
        if start is not None:
            begin = origin + cumulative[start - lag + 1] - anchor
            end = origin + window.error[newest]
            optimal = ((float(begin[0]), float(begin[1])), (float(end[0]), float(end[1])))

    return TrailGeometry(
        landed=landed,
        landed_age=landed_age,
        in_flight=in_flight,
        target=target,
        optimal=optimal,
    )


def _empty_geometry(origin: np.ndarray) -> TrailGeometry:
    return TrailGeometry(
        landed=np.zeros((0, 2)),
        landed_age=np.zeros(0),
        in_flight=origin[None, :].copy(),
        target=(),
        optimal=None,
    )


def _target_lines(points: np.ndarray, tracks: np.ndarray) -> tuple[np.ndarray, ...]:
    if len(points) == 0:
        return ()
    breaks = np.flatnonzero(np.diff(tracks) != 0) + 1
    lines = []
    for piece, piece_tracks in zip(np.split(points, breaks), np.split(tracks, breaks)):
        if piece_tracks[0] != 0 and len(piece) >= 2:
            lines.append(piece)
    return tuple(lines)


def _latest_aim_start(window: TrailWindow, earliest: int) -> int | None:
    """最近一次「开始瞄」: 按下触发键, 或者按着的时候认准了一个新目标。

    中间掉一帧检测(目标编号记成 0)再回来的还是同一个目标, 不算重新开始。
    """
    newest = len(window) - 1
    if earliest > newest:
        return None
    track = window.track
    profile = window.profile
    seen = np.where(track != 0, np.arange(len(track)), -1)
    last_seen = np.maximum.accumulate(seen)
    previous_target = np.where(last_seen >= 0, track[np.maximum(last_seen, 0)], 0)
    frames = np.arange(earliest, newest + 1)
    aiming = profile[frames] >= 0
    pressed = aiming & (profile[frames - 1] < 0)
    switched = aiming & (track[frames] != 0) & (track[frames] != previous_target[frames - 1])
    starts = frames[pressed | switched]
    return int(starts[-1]) if starts.size else None


def draw_trail(image: np.ndarray, geometry: TrailGeometry, *, with_target: bool = True) -> None:
    if with_target:
        for line in geometry.target:
            cv2.polylines(image, [np.round(line).astype(np.int32)], False, TARGET_GREY, 1, cv2.LINE_AA)
    if geometry.optimal is not None:
        _dotted(image, geometry.optimal[0], geometry.optimal[1])
    _draw_landed(image, geometry)
    _dashed(image, geometry.in_flight)


def trail_status(calibration: Calibration | None) -> str:
    # 预览状态栏用 OpenCV 自带字体, 画不了中文。
    if calibration is None:
        return "trail calibrating"
    return f"trail {calibration.px_per_hand:.2f}px/ct {calibration.lag_frames}f"


def _faded(age: float) -> tuple[int, int, int]:
    return tuple(int(round(new + (old - new) * age)) for new, old in zip(TRAIL_NEW, TRAIL_OLD))


def _draw_landed(image: np.ndarray, geometry: TrailGeometry) -> None:
    points = geometry.landed
    if len(points) < 2:
        return
    levels = np.rint(geometry.landed_age * (_FADE_LEVELS - 1)).astype(np.int64)
    edges = np.flatnonzero(np.diff(levels)) + 1
    pixels = np.round(points).astype(np.int32)
    for start, stop in zip(np.concatenate(([0], edges)), np.concatenate((edges, [len(levels)]))):
        color = _faded(int(levels[start]) / (_FADE_LEVELS - 1))
        cv2.polylines(image, [pixels[start : stop + 1]], False, color, _TRAIL_WIDTH, cv2.LINE_AA)


def _dashed(image: np.ndarray, points: np.ndarray) -> None:
    period = _DASH_ON + _DASH_OFF
    travelled = 0.0
    for index in range(len(points) - 1):
        start = points[index]
        delta = points[index + 1] - start
        length = float(np.hypot(delta[0], delta[1]))
        if length == 0.0:
            continue
        position = 0.0
        while position < length:
            phase = travelled % period
            if phase < _DASH_ON:
                step = min(_DASH_ON - phase, length - position)
                begin = start + delta * (position / length)
                end = start + delta * ((position + step) / length)
                cv2.line(image, _pixel(begin), _pixel(end), TRAIL_NEW, _TRAIL_WIDTH, cv2.LINE_AA)
            else:
                step = min(period - phase, length - position)
            position += step
            travelled += step


def _dotted(image: np.ndarray, start: tuple[float, float], end: tuple[float, float]) -> None:
    begin = np.asarray(start, dtype=float)
    delta = np.asarray(end, dtype=float) - begin
    length = float(np.hypot(delta[0], delta[1]))
    for step in range(int(length // _DOT_SPACING) + 1):
        point = begin + delta * (step * _DOT_SPACING / length) if length else begin
        cv2.circle(image, _pixel(point), 1, OPTIMAL_COLOR, -1, cv2.LINE_8)


def _pixel(point: np.ndarray) -> tuple[int, int]:
    return int(round(float(point[0]))), int(round(float(point[1])))


# 窗口按这个帧率上限多取几行, 真正截多长由每行的时间戳决定。
_MAX_FPS = 600
_CALIBRATE_EVERY_S = 1.0
_SETTINGS_CHECK_EVERY_S = 0.25


@dataclass(frozen=True, slots=True)
class TrailSettings:
    # 预览页上的两个勾选框。打开程序时总是只勾「画面」: 轨迹是想看的时候才打开的东西。
    show_frame: bool = True
    enabled: bool = False
    seconds: float = 0.5
    optimal_path: bool = False


def read_trail_settings(path: Path | None) -> TrailSettings:
    """GUI 写的设置文件。读不到、写到一半、类型不对, 都按默认值来, 绝不抛。"""
    default = TrailSettings()
    if path is None:
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default
    seconds = raw.get("seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and np.isfinite(seconds):
        seconds = min(TRAIL_MAX_SECONDS, max(TRAIL_MIN_SECONDS, float(seconds)))
    else:
        seconds = default.seconds
    return TrailSettings(
        show_frame=_flag(raw, "show_frame", default.show_frame),
        enabled=_flag(raw, "enabled", default.enabled),
        seconds=seconds,
        optimal_path=_flag(raw, "optimal_path", default.optimal_path),
    )


def _flag(raw: dict, name: str, default: bool) -> bool:
    value = raw.get(name)
    return value if isinstance(value, bool) else default


def write_trail_settings(path: Path, settings: TrailSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "show_frame": settings.show_frame,
                "enabled": settings.enabled,
                "seconds": settings.seconds,
                "optimal_path": settings.optimal_path,
            }
        ),
        encoding="utf-8",
    )
    # 先写临时文件再替换: 预览线程随时可能来读, 不能让它读到半个文件。
    temporary.replace(path)


class TrailSettingsFile:
    """隔一会儿重读一次 GUI 写的设置文件。

    不比修改时间, 直接重读: Windows 上修改时间的精度可能只有十几毫秒, 拖滑条时两次写入
    落在同一个时间戳上, 最后那次改动就永远读不到了。文件才几十字节。
    """

    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._path = path
        self._clock = clock
        # 别的线程直接读这个属性(一次引用赋值, 不会读到一半), 不用自己去碰文件。
        self.settings = read_trail_settings(path)
        self._next_check = clock() + _SETTINGS_CHECK_EVERY_S

    def current(self) -> TrailSettings:
        now = self._clock()
        if now >= self._next_check:
            self._next_check = now + _SETTINGS_CHECK_EVERY_S
            self.settings = read_trail_settings(self._path)
        return self.settings


class TrailOverlay:
    """预览线程用: 定时标定, 算出这一帧要画的轨迹。"""

    def __init__(
        self,
        recorder: TrailRecorder,
        *,
        available: bool,
        on_calibrated: Callable[[Calibration], None] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.recorder = recorder
        self.calibrator = TrailCalibrator()
        self.available = available
        self.on_calibrated = on_calibrated
        self._clock = clock
        self._next_calibration = 0.0
        self._announced = False

    def prepare(
        self, row: int, center: tuple[float, float], settings: TrailSettings
    ) -> tuple[TrailGeometry | None, str]:
        if not settings.enabled:
            return None, ""
        if not self.available:
            return None, TRAIL_NEEDS_KMBOX
        now = self._clock()
        if now >= self._next_calibration:
            self._next_calibration = now + _CALIBRATE_EVERY_S
            window = self.recorder.window(row, self.calibrator.rows_needed(row))
            calibration = self.calibrator.update(window)
            if calibration is not None and not self._announced:
                self._announced = True
                if self.on_calibrated is not None:
                    self.on_calibrated(calibration)
        calibration = self.calibrator.calibration
        if calibration is None:
            return None, trail_status(None)
        rows = int(settings.seconds * _MAX_FPS) + calibration.lag_frames + 2
        geometry = trail_geometry(
            self.recorder.window(row, rows),
            calibration,
            seconds=settings.seconds,
            center=center,
            optimal_path=settings.optimal_path,
        )
        return geometry, trail_status(calibration)
