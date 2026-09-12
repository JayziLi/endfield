"""瞄准回路的延迟记录与离线分析。

记录的是控制量而不是时间戳：每帧存下「控制器看到的误差」和「实际发出的移动」。
KMBox 一发指令准心立刻就动了，但这个位移要绕完「渲染→截图→编码→网络→解码→推理」
一整圈才会出现在下一次看到的画面里。所以指令和误差响应之间差了几帧，就是整条回路
的真实延迟——包含发送端那半截，而那半截是本机时间戳看不到的。
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import astuple, dataclass, fields
from pathlib import Path

_MIN_SAMPLES = 30
_MIN_COMMANDS = 10
_MIN_PAIRS = 40
# 高帧率下回路延迟很容易超过 8 帧: 241fps 时 8 帧才 33 毫秒, 窗口太窄会把真峰截断。
_DEFAULT_MAX_LAG = 30
# 进程被强杀时缓冲区里的数据会全丢, 所以定期落盘, 把损失限制在不到一秒。
_FLUSH_EVERY = 100
# 旧版本写出的 CSV 没有这几列; 缺列时按「一直按住、始终同一个目标、没丢帧」补齐。
# algorithm 补空串而不是补 "p": 我们并不知道旧日志跑的是什么, 填一个具体名字
# 就是撒谎。分析时先看 algorithm 是不是空的, 空的就说明这份日志没记设置。
_OPTIONAL_DEFAULTS = {
    "trigger": True,
    "track_id": 1,
    "skipped": 0,
    "algorithm": "",
    "algorithm_params": "",
    "kp_min": 0.0,
    "kp_max": 0.0,
    "kp_growth": 0.0,
}

# 控制器是对*带噪的*测量值做反应的, 所以 dx 里混着 kp x 噪声, 而误差变化里也含同一个
# 噪声——两者在滞后 1 帧处天然相关, 噪声越大这个假峰越强, 会把估计值拽到 1 帧。
# 拉枪那几帧的指令幅度远大于噪声, 只用这些帧就能把假峰压掉; 拉枪太少时再逐级放宽。
_COMMAND_THRESHOLDS = (6.0, 3.0, 1.0)


@dataclass(frozen=True, slots=True)
class LatencySample:
    monotonic_ms: float
    sequence: int
    error_x: float
    error_y: float
    dx: int
    dy: int
    total_ms: float
    assembly_ms: float
    decode_ms: float
    queue_ms: float
    preprocess_ms: float
    inference_ms: float
    postprocess_ms: float
    send_ms: float
    trigger: bool
    track_id: int
    skipped: int
    # 这几列让日志自己说清楚是哪套设置跑出来的。没有它们, 两份日志没法比 ——
    # 只能靠记性猜哪份是什么算法什么增益, 而 A/B 的前提就是知道两边各是什么。
    # 按帧记而不是写在表头: 算法和增益现在可以在跑的过程中热切换。
    algorithm: str = ""
    algorithm_params: str = ""
    kp_min: float = 0.0
    kp_max: float = 0.0
    kp_growth: float = 0.0


@dataclass(frozen=True, slots=True)
class LoopDelayEstimate:
    frames: int
    correlation: float
    runner_up_frames: int
    runner_up_correlation: float
    frame_interval_ms: float
    loop_ms: float
    ai_side_ms: float
    remote_ms: float
    samples: int
    command_threshold: float
    pairs: int


_FIELDS = tuple(field.name for field in fields(LatencySample))
_CASTS = {
    "sequence": int,
    "dx": int,
    "dy": int,
    "track_id": int,
    "skipped": int,
    "trigger": lambda value: value == "True",
    # 缺省是 float。这两列不登记成 str 的话, 读一份新日志就当场崩。
    "algorithm": str,
    "algorithm_params": str,
}


class LatencyLogWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(_FIELDS)
        self.rows = 0

    def write(self, sample: LatencySample) -> None:
        self._writer.writerow(astuple(sample))
        self.rows += 1
        if self.rows % _FLUSH_EVERY == 0:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()


def read_latency_log(path: Path) -> list[LatencySample]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [_restore(row) for row in csv.DictReader(handle)]


def _restore(row: dict[str, str]) -> LatencySample:
    values = {}
    for name in _FIELDS:
        raw = row.get(name)
        if raw is None and name in _OPTIONAL_DEFAULTS:
            values[name] = _OPTIONAL_DEFAULTS[name]
        else:
            values[name] = _CASTS.get(name, float)(raw)
    return LatencySample(**values)


def estimate_loop_delay(
    samples: list[LatencySample], max_lag: int = _DEFAULT_MAX_LAG
) -> LoopDelayEstimate | None:
    """找出让「误差的变化」最像「若干帧前发出的移动」的那个滞后帧数。"""
    if len(samples) < _MIN_SAMPLES:
        return None
    if _qualifying(samples, _COMMAND_THRESHOLDS[-1]) < _MIN_COMMANDS:
        return None
    threshold = next(
        (
            candidate
            for candidate in _COMMAND_THRESHOLDS
            if _qualifying(samples, candidate) >= _MIN_PAIRS
        ),
        _COMMAND_THRESHOLDS[-1],
    )

    scored: list[tuple[float, int]] = []
    pairs_by_lag: dict[int, int] = {}
    for lag in range(1, min(max_lag, len(samples) - 2) + 1):
        commands: list[float] = []
        responses: list[float] = []
        for index in range(len(samples) - lag):
            issued = samples[index]
            after = samples[index + lag]
            before = samples[index + lag - 1]
            # 掉帧或中途停记时, 相邻两行在画面上并不相邻, 跨缺口配对算出的滞后是假的。
            if after.sequence - issued.sequence != lag:
                continue
            # track_id 只增不减: 0 表示这帧没检测到目标(误差无意义), 两端不同则说明
            # 中途换了锁定目标——误差会突跳, 但那跳变不是我们发的指令造成的。
            if issued.track_id == 0 or issued.track_id != after.track_id:
                continue
            if abs(issued.dx) >= threshold:
                commands.append(float(issued.dx))
                responses.append(-(after.error_x - before.error_x))
            if abs(issued.dy) >= threshold:
                commands.append(float(issued.dy))
                responses.append(-(after.error_y - before.error_y))
        correlation = _correlation(commands, responses)
        if correlation is not None:
            scored.append((correlation, lag))
            pairs_by_lag[lag] = len(commands)
    if not scored:
        return None

    scored.sort(reverse=True)
    best_correlation, best_lag = scored[0]
    runner_up_correlation, runner_up_lag = scored[1] if len(scored) > 1 else (0.0, 0)

    frame_interval_ms = _frame_interval_ms(samples)
    loop_ms = best_lag * frame_interval_ms
    ai_side_ms = statistics.median(sample.total_ms for sample in samples)
    return LoopDelayEstimate(
        frames=best_lag,
        correlation=best_correlation,
        runner_up_frames=runner_up_lag,
        runner_up_correlation=runner_up_correlation,
        frame_interval_ms=frame_interval_ms,
        loop_ms=loop_ms,
        ai_side_ms=ai_side_ms,
        remote_ms=max(0.0, loop_ms - ai_side_ms),
        samples=len(samples),
        command_threshold=threshold,
        # 报获胜滞后自己的配对数。滞后越大被序号缺口滤掉的越多, 报所有滞后里的
        # 最大值会虚高, 让人以为结论比实际更有依据。
        pairs=pairs_by_lag[best_lag],
    )


def _qualifying(samples: list[LatencySample], threshold: float) -> int:
    return sum(
        (1 if abs(sample.dx) >= threshold else 0)
        + (1 if abs(sample.dy) >= threshold else 0)
        for sample in samples
    )


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    try:
        return statistics.correlation(left, right)
    except statistics.StatisticsError:
        return None


def _frame_interval_ms(samples: list[LatencySample]) -> float:
    gaps = [
        after.monotonic_ms - before.monotonic_ms
        for before, after in zip(samples, samples[1:])
        if after.sequence - before.sequence == 1 and after.monotonic_ms > before.monotonic_ms
    ]
    return statistics.median(gaps) if gaps else 0.0


MEASUREMENT_NAME = ".loop-latency.json"


def save_measurement(path: Path, estimate: LoopDelayEstimate) -> None:
    """把最近一次实测的回路延迟记下来, 供分享调校时比对。

    只有 loop_ms 是必须的, 其余几项是为了人打开这个文件时能看懂。
    """
    payload = {
        "loop_ms": round(estimate.loop_ms, 2),
        "frames": estimate.frames,
        "frame_interval_ms": round(estimate.frame_interval_ms, 3),
        "correlation": round(estimate.correlation, 4),
        "samples": estimate.samples,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 记不下来就算了, 不值得让一局打完的日志汇总因此报错。
        pass


def load_measurement(path: Path) -> float | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = payload.get("loop_ms") if isinstance(payload, dict) else None
    if not isinstance(value, (int, float)):
        return None
    return float(value)
