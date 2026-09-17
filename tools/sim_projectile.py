"""弹道预判的拟真闭环仿真: 目标远近变化、会蹲会跳、检测框有噪声。

控制律一行不重写: 驱动真实的 KmboxController.move_toward 和算法库里的
kalman_projectile。仿真只负责被控对象一侧: 目标在三维里怎么动、怎么投影到画面、
检测框怎么抖、各段延迟多少。

延迟链, 对齐 latency-20260909-235909.csv 和轨迹标定:
  发指令 --8ms--> 游戏里镜头转 --等下一个采集帧--> 编码+网络 15.5ms --> 接收端首包(observed_at)
  --组帧--> 主循环空闲时取最新一帧 --处理约 2.9ms--> 发下一条指令
  仿真结果: 首包到发送中位 4.2ms、p99 12.5ms, 跳帧 3%, 回路 7 帧 (实测 4.06 / 13.8 / 3% / 7)

投影: 1920 宽、水平 FOV 105 的屏幕中心裁 320x320, 焦距 737px。人高 1.8m 宽 0.6m,
15m 处框高 88px。瞄准点在框顶往下 18%, 和 settings.txt 方案 1 一样。

命中: 每个游戏帧都开火, 子弹沿开火瞬间的准心方向直线飞 (无下坠), 飞行时间按弹速和
子弹到达时的距离算。到达时准心方向离瞄准点不超过 R 米算中, R 在目标所在距离处量。

用法: .venv/Scripts/python.exe tools/sim_projectile.py [--calm]
"""
from __future__ import annotations

import io
import math
import random
import sys
import tempfile
import time
import types
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rhodes_fast import kmbox_control  # noqa: E402
from rhodes_fast.aim_algorithms import registry, set_installed_algorithms  # noqa: E402
from rhodes_fast.algorithm_library import install, load_installed  # noqa: E402
from rhodes_fast.config import AimConfig, AimProfileConfig, KmboxConfig  # noqa: E402
from rhodes_fast.detector import Detection  # noqa: E402

KALMAN_SOURCE = ROOT / "examples" / "kalman_projectile.py"

# ---------------------------------------------------------------- 延迟
FPS = 241.0
FRAME_S = 1.0 / FPS
PX_PER_COUNT = 0.64
MOUSE_S = 0.008
TRANSPORT_S = 0.0155
TRANSPORT_JITTER_S = 0.0005
ASSEMBLY_S = (0.0010, 0.00035)
BUSY_MEDIAN_S = 0.0029
BUSY_LOG_SD = 0.30
STALL_RATE = 0.025         # 偶发卡顿, 实测首包到发送 p99 13.8ms 就是它
STALL_S = (0.003, 0.012)
MISS_RATE = 0.01

# ---------------------------------------------------------------- 画面和目标
FRAME_PX = 320
FOCAL_PX = 960.0 / math.tan(math.radians(52.5))
EYE_M = 1.6
STAND_M = (1.8, 0.6)       # 高, 宽
CROUCH_M = (1.2, 0.7)
AIM_RATIO = 0.18
BOX_NOISE = (0.6, 0.01)    # 每条边 σ = 0.6px + 框高的 1%
SPEED = 5.5
ACCEL = 50.0
NEAR_M, FAR_M = 5.0, 40.0
CROUCH_RATE = 0.25         # 每秒
CROUCH_MS = (300.0, 1200.0)
JUMP_RATE = 0.3
JUMP_M, JUMP_S = 1.0, 0.55

# ---------------------------------------------------------------- 评估
DURATION_S = 8.0
EVAL_FROM_S = 0.5
SEEDS = tuple(range(32))
RADII_M = (0.15, 0.30)
BULLETS = (40.0, 60.0, 100.0)
BANDS = ((5.0, 10.0), (10.0, 20.0), (20.0, 40.0))
REFERENCE_M = 15.0
AIM = AimConfig(smoothing=1.0, deadzone=2.0, max_step=30, target_y_ratio=AIM_RATIO)
USER_GAINS = (0.03, 0.056, 0.032)   # settings.txt 方案 1
# 指令到首包 = 8 + 平均等帧 2.1 + 15.5 ≈ 26ms
CALIBRATED = {"loop_delay_ms": 26.0, "camera_scale": PX_PER_COUNT, "max_lead_px": 160.0}


def box_height_px(distance_m: float) -> float:
    return FOCAL_PX * STAND_M[0] / distance_m


# ---------------------------------------------------------------- 目标怎么动

class World:
    """1ms 一格的目标状态: 横向 x, 纵深 z, 身高, 身宽, 离地高度。"""

    def __init__(self, rows: np.ndarray) -> None:
        self.rows = rows

    def at(self, t):
        position = np.asarray(t) * 1000.0
        index = np.clip(position.astype(int), 0, self.rows.shape[1] - 2)
        frac = position - index
        return self.rows[:, index] + (self.rows[:, index + 1] - self.rows[:, index]) * frac

    def aim(self, t):
        """瞄准点的角位置(px, 镜头不动时)和目标距离。"""
        x, z, tall, _, lift = self.at(t)
        top = -FOCAL_PX * np.arctan2(lift + tall - EYE_M, z)
        bottom = -FOCAL_PX * np.arctan2(lift - EYE_M, z)
        return FOCAL_PX * np.arctan2(x, z), top + AIM_RATIO * (bottom - top), np.hypot(x, z)


# (跑动占比, 跑动时长ms, AD 占比, 每次换向ms, AD 来回次数)
STYLES = {
    "erratic": (0.45, (250, 1000), 0.35, (80, 180), (2, 6)),    # 频繁变向
    "calm": (0.70, (400, 1500), 0.15, (200, 400), (2, 4)),      # 少变向
}
STYLE = "erratic"


def _segment(rng: random.Random) -> list[list[float]]:
    run_share, run_ms, strafe_share, swing_ms, swings = STYLES[STYLE]
    roll = rng.random()
    if roll < run_share:   # 朝随机方向跑一段, 远近和左右都会变
        heading = rng.uniform(0.0, 2.0 * math.pi)
        return [[rng.uniform(*run_ms), SPEED * math.cos(heading), SPEED * math.sin(heading)]]
    if roll < run_share + strafe_share:   # AD 横跳
        side = rng.choice((-1.0, 1.0))
        return [[rng.uniform(*swing_ms), side * SPEED * (-1) ** j, 0.0] for j in range(rng.randint(*swings))]
    return [[rng.uniform(100, 400), 0.0, 0.0]]   # 停下


def make_world(rng: random.Random, seconds: float) -> World:
    n = int(seconds * 1000) + 2
    rows = np.empty((5, n))
    x, z = rng.uniform(-1.0, 1.0), rng.uniform(NEAR_M, FAR_M)
    vx = vz = 0.0
    plan: list[list[float]] = []
    crouch_left = crouch = 0.0
    jump: float | None = None
    step = ACCEL * 0.001
    for k in range(n):
        if not plan:
            plan = _segment(rng)
        segment = plan[0]
        if (z < NEAR_M and segment[2] < 0) or (z > FAR_M and segment[2] > 0):
            segment[2] = -segment[2]
        if abs(x) > 0.5 * z and segment[1] * x > 0:
            segment[1] = -segment[1]
        vx += max(-step, min(step, segment[1] - vx))
        vz += max(-step, min(step, segment[2] - vz))
        x += vx * 0.001
        z += vz * 0.001
        segment[0] -= 1.0
        if segment[0] <= 0:
            plan.pop(0)

        if crouch_left <= 0 and rng.random() < CROUCH_RATE * 0.001:
            crouch_left = rng.uniform(*CROUCH_MS)
        crouch_left -= 1.0
        crouch += max(-1 / 120, min(1 / 120, (1.0 if crouch_left > 0 else 0.0) - crouch))

        lift = 0.0
        if jump is None and crouch_left <= 0 and rng.random() < JUMP_RATE * 0.001:
            jump = 0.0
        if jump is not None:
            s = jump / JUMP_S
            lift = 4.0 * JUMP_M * s * (1.0 - s)
            jump += 0.001
            if jump >= JUMP_S:
                jump = None

        tall = STAND_M[0] + (CROUCH_M[0] - STAND_M[0]) * crouch
        wide = STAND_M[1] + (CROUCH_M[1] - STAND_M[1]) * crouch
        rows[:, k] = (x, z, tall, wide, lift)
    return World(rows)


# ---------------------------------------------------------------- 闭环

_CLOCK = [0.0]


class _Client:
    def move(self, dx, dy):
        return None

    enc_move = move


def init_worker(library: str, style: str = "erratic") -> None:
    global STYLE
    STYLE = style
    factories, warnings = load_installed(Path(library))
    if warnings:
        raise RuntimeError(warnings)
    set_installed_algorithms(factories)
    kmbox_control.time = types.SimpleNamespace(perf_counter=lambda: _CLOCK[0], sleep=lambda s: None)
    np.seterr(all="ignore")


@dataclass(frozen=True)
class Case:
    label: str
    algorithm: str                      # "ideal" / "follow" 不走控制器
    params: tuple = ()                  # ((名字, 值), ...)
    gains: tuple = USER_GAINS
    overrides: tuple = ()               # ((算法模块常量, 值), ...), 做消融用
    box_noise: float = 1.0


def _observed_box(world: World, t: float, cam_x: float, cam_y: float, rng, noise: float):
    x, z, tall, wide, lift = world.at(t)
    cx = FRAME_PX / 2 + FOCAL_PX * math.atan2(x, z) - cam_x
    top = FRAME_PX / 2 - FOCAL_PX * math.atan2(lift + tall - EYE_M, z) - cam_y
    bottom = FRAME_PX / 2 - FOCAL_PX * math.atan2(lift - EYE_M, z) - cam_y
    half = FOCAL_PX * wide / (2.0 * z)
    sigma = (BOX_NOISE[0] + BOX_NOISE[1] * (bottom - top)) * noise
    x1 = max(0.0, cx - half + rng.gauss(0.0, sigma))
    x2 = min(float(FRAME_PX), cx + half + rng.gauss(0.0, sigma))
    y1 = max(0.0, top + rng.gauss(0.0, sigma))
    y2 = min(float(FRAME_PX), bottom + rng.gauss(0.0, sigma))
    if x2 - x1 < 2.0 or y2 - y1 < 2.0:
        return None   # 出画面了, 这帧没检测到
    return Detection(x1, y1, x2, y2, 0.9, 0)


def run(case: Case, seed: int) -> dict:
    rng = random.Random(seed)
    world = make_world(random.Random(seed * 7919 + 17), DURATION_S + 2.5)
    n = int(DURATION_S * FPS)
    game = np.arange(int(EVAL_FROM_S * FPS), n) * FRAME_S
    if case.algorithm in ("ideal", "follow"):
        return _score_reference(case, world, game)

    profile = AimProfileConfig(
        trigger="side1", kp_min=case.gains[0], kp_max=case.gains[1], kp_growth=case.gains[2],
        target_y_ratio=AIM_RATIO, algorithm=case.algorithm, algorithm_params=dict(case.params),
    )
    controller = kmbox_control.KmboxController(
        KmboxConfig(uuid="00000000"), AIM,
        profiles=(profile, AimProfileConfig(enabled=False, trigger="left")),
    )
    controller._client = _Client()

    arrive = np.empty(n + 1)
    ready = np.empty(n + 1)
    last = -1.0
    for i in range(n + 1):
        a = max(i * FRAME_S + TRANSPORT_S + rng.gauss(0.0, TRANSPORT_JITTER_S), last + 1e-4)
        last = a
        arrive[i] = a
        ready[i] = a + max(0.0002, rng.gauss(*ASSEMBLY_S))

    start_x, start_y, _ = world.aim(0.0)
    apply_at: list[float] = []
    cum_x = [float(start_x) - 40.0]   # 从偏 40px 的地方拉过去
    cum_y = [float(start_y) - 10.0]
    free = 0.0
    for i in range(n):
        start = max(ready[i], free)
        if ready[i + 1] <= start:
            continue   # 主循环空下来时更新的一帧已经组好, 这帧直接丢
        now = start + BUSY_MEDIAN_S * math.exp(rng.gauss(0.0, BUSY_LOG_SD))
        if rng.random() < STALL_RATE:
            now += rng.uniform(*STALL_S)
        free = now
        if rng.random() < MISS_RATE:
            continue
        k = bisect_right(apply_at, i * FRAME_S)
        detection = _observed_box(world, i * FRAME_S, cum_x[k], cum_y[k], rng, case.box_noise)
        if detection is None:
            continue
        _CLOCK[0] = now
        dx, dy = controller.move_toward(detection, FRAME_PX, FRAME_PX, observed_at=float(arrive[i]))
        if dx or dy:
            apply_at.append(now + MOUSE_S)
            cum_x.append(cum_x[-1] + dx * PX_PER_COUNT)
            cum_y.append(cum_y[-1] + dy * PX_PER_COUNT)

    k = np.searchsorted(np.asarray(apply_at), game, side="right")
    return _score(world, game, np.asarray(cum_x)[k], np.asarray(cum_y)[k])


def _flight(world: World, fired: np.ndarray, speed: float) -> np.ndarray:
    if math.isinf(speed):
        return np.zeros_like(fired)
    flight = world.aim(fired)[2] / speed
    for _ in range(3):
        flight = world.aim(fired + flight)[2] / speed
    return flight


def _score(world: World, fired: np.ndarray, aim_x: np.ndarray, aim_y: np.ndarray) -> dict:
    out = {}
    fire_distance = world.aim(fired)[2]
    for speed in (math.inf, *BULLETS):
        hit_at = fired + _flight(world, fired, speed)
        tx, ty, distance = world.aim(hit_at)
        miss_m = np.hypot(tx - aim_x, ty - aim_y) / FOCAL_PX * distance
        for radius in RADII_M:
            hits = miss_m <= radius
            out[(speed, radius, None)] = float(hits.mean())
            for band in BANDS:
                inside = (fire_distance >= band[0]) & (fire_distance < band[1])
                out[(speed, radius, band)] = (float(hits[inside].sum()), float(inside.sum()))
    return out


def _score_reference(case: Case, world: World, fired: np.ndarray) -> dict:
    if case.algorithm == "follow":   # 准心一直贴着瞄准点, 不提前
        x, y, _ = world.aim(fired)
        return _score(world, fired, x, y)
    # 理想预判: 知道 29ms 前瞄准点的真实角速度和每一发的真实飞行时间, 匀速外推
    seen = fired - 0.029
    px, py, _ = world.aim(seen)
    ax, ay, _ = world.aim(seen - 0.004)
    vx, vy = (px - ax) / 0.004, (py - ay) / 0.004
    out = {}
    fire_distance = world.aim(fired)[2]
    for speed in (math.inf, *BULLETS):
        flight = _flight(world, fired, speed)
        horizon = 0.029 + flight
        tx, ty, distance = world.aim(fired + flight)
        miss_m = np.hypot(tx - (px + vx * horizon), ty - (py + vy * horizon)) / FOCAL_PX * distance
        for radius in RADII_M:
            hits = miss_m <= radius
            out[(speed, radius, None)] = float(hits.mean())
            for band in BANDS:
                inside = (fire_distance >= band[0]) & (fire_distance < band[1])
                out[(speed, radius, band)] = (float(hits[inside].sum()), float(inside.sum()))
    return out


def job(case: Case) -> dict:
    module = None
    saved = {}
    if case.overrides:
        module = registry.available_algorithms()[case.algorithm].compute.__globals__
        saved = {name: module[name] for name, _ in case.overrides}
        module.update(dict(case.overrides))
    try:
        rows = [run(case, seed) for seed in SEEDS]
    finally:
        if module is not None:
            module.update(saved)
    out = {}
    for key, value in rows[0].items():
        if key[2] is None:
            out[key] = sum(r[key] for r in rows) / len(rows)
        else:
            hits = sum(r[key][0] for r in rows)
            total = sum(r[key][1] for r in rows)
            out[key] = hits / total if total else float("nan")
    return out


def run_cases(cases: list[Case], library: str, style: str = "erratic") -> dict[Case, dict]:
    with ProcessPoolExecutor(max_workers=16, initializer=init_worker, initargs=(library, style)) as pool:
        return dict(zip(cases, pool.map(job, cases)))


# ---------------------------------------------------------------- 要比的组合

def kalman(label: str, gains=USER_GAINS, overrides=(), box_noise=1.0, **params) -> Case:
    return Case(label, "kalman_projectile", tuple(sorted({**CALIBRATED, **params}.items())),
                gains, tuple(overrides), box_noise)


def fixed(speed: float, tuned_m: float, **extra) -> Case:
    return kalman(f"固定提前 按{tuned_m:.0f}m调", projectile_lead_ms=tuned_m / speed * 1000.0, **extra)


def scaled(speed: float, label="按框高缩放", **extra) -> Case:
    return kalman(label, projectile_lead_ms=REFERENCE_M / speed * 1000.0,
                  reference_box_height=round(box_height_px(REFERENCE_M)), **extra)


def gains_times(m: float) -> tuple:
    return (USER_GAINS[0] * m, USER_GAINS[1] * m, USER_GAINS[2])


IDEAL = Case("理想预判(真速度+真飞行时间)", "ideal")
FOLLOW = Case("准心贴死瞄准点, 不提前", "follow")
NO_LEAD = kalman("卡尔曼 提前0")
TUNED_AT = (8.0, 12.0, 15.0, 20.0, 25.0)
ABLATIONS = (
    ("去掉框高平滑", (("_HEIGHT_SMOOTHING_S", 1e-6),), 1.0),
    ("去掉蹲下判断", (("_SHORTER_THAN_USUAL", 0.0),), 1.0),
    ("去掉贴边判断", (("_EDGE_PX", -1e9),), 1.0),
    ("去掉缩放上下限", (("_DISTANCE_FACTOR_LIMIT", 1e9),), 1.0),
    ("框噪声 x0", (), 0.0),
    ("框噪声 x2", (), 2.0),
    ("框噪声 x4", (), 4.0),
)


def build_cases() -> list[Case]:
    cases = [IDEAL, FOLLOW, NO_LEAD]
    for speed in BULLETS:
        cases += [fixed(speed, d) for d in TUNED_AT]
        cases.append(scaled(speed))
    for label, overrides, noise in ABLATIONS:
        cases.append(scaled(60.0, label=label, overrides=overrides, box_noise=noise))
    for m in (2.0, 3.0):
        cases.append(scaled(60.0, label=f"按框高缩放 增益{m:.0f}x", gains=gains_times(m)))
        cases.append(fixed(60.0, REFERENCE_M, gains=gains_times(m)))
    return list(dict.fromkeys(cases))


# ---------------------------------------------------------------- 输出

def _row(label: str, result: dict, speed: float, width: int = 30) -> str:
    cells = [f"{result[(speed, 0.15, None)] * 100:>7.0f}%", f"{result[(speed, 0.30, None)] * 100:>7.0f}%"]
    cells += [f"{result[(speed, 0.30, band)] * 100:>9.0f}%" for band in BANDS]
    return f"{label:<{width}}" + "".join(cells)


def _header(width: int = 30) -> str:
    bands = "".join(f"{f'{a:.0f}-{b:.0f}m':>10}" for a, b in BANDS)
    return f"{'':<{width}}{'15cm':>8}{'30cm':>8}{bands}   (距离分段按 30cm)"


def print_tables(results: dict[Case, dict]) -> None:
    print("\n=== 1. 跟枪 (即时命中) ===")
    print(_header())
    for case in (FOLLOW, NO_LEAD):
        print(_row(case.label, results[case], math.inf))

    for speed in BULLETS:
        print(f"\n=== 2. 弹速 {speed:.0f} m/s (5m 飞 {5 / speed * 1000:.0f}ms, 40m 飞 {40 / speed * 1000:.0f}ms) ===")
        print(_header())
        for case in (IDEAL, FOLLOW, NO_LEAD):
            print(_row(case.label, results[case], speed))
        fixed_cases = [fixed(speed, d) for d in TUNED_AT]
        best = max(fixed_cases, key=lambda c: results[c][(speed, 0.30, None)])
        for case in fixed_cases:
            mark = "  <- 固定里最好" if case == best else ""
            print(_row(case.label, results[case], speed) + mark)
        print(_row(f"按框高缩放 (参考 {REFERENCE_M:.0f}m=h{round(box_height_px(REFERENCE_M))})",
                   results[scaled(speed)], speed))

    print(f"\n=== 3. 框高处理每一项的作用 (弹速 60 m/s, 按框高缩放) ===")
    print(_header())
    print(_row("完整", results[scaled(60.0)], 60.0))
    for label, overrides, noise in ABLATIONS:
        print(_row(label, results[scaled(60.0, label=label, overrides=overrides, box_noise=noise)], 60.0))

    print(f"\n=== 4. 增益 (弹速 60 m/s) ===")
    print(_header())
    print(_row("固定 按15m调 1x", results[fixed(60.0, REFERENCE_M)], 60.0))
    print(_row("按框高缩放 1x", results[scaled(60.0)], 60.0))
    for m in (2.0, 3.0):
        print(_row(f"固定 按15m调 {m:.0f}x", results[fixed(60.0, REFERENCE_M, gains=gains_times(m))], 60.0))
        print(_row(f"按框高缩放 {m:.0f}x", results[scaled(60.0, label=f"按框高缩放 增益{m:.0f}x",
                                                         gains=gains_times(m))], 60.0))


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    style = "calm" if "--calm" in sys.argv[1:] else "erratic"
    started = time.perf_counter()
    folder = tempfile.TemporaryDirectory()
    install(Path(folder.name), KALMAN_SOURCE)
    results = run_cases(build_cases(), folder.name, style)
    folder.cleanup()
    run_share, run_ms, strafe_share, swing_ms, _ = STYLES[style]
    print(f"{FPS:.0f}fps, 回路 7 帧, 每计数 {PX_PER_COUNT}px, 目标 {NEAR_M:.0f}-{FAR_M:.0f}m 跑动/AD/急停/蹲/跳,"
          f" 每格 {len(SEEDS)} 个种子 x {DURATION_S:.0f}s, 增益 = 方案 1 的 kp")
    print(f"走位 {style}: 跑动 {run_ms[0]}-{run_ms[1]}ms 占 {run_share:.0%},"
          f" AD 每 {swing_ms[0]}-{swing_ms[1]}ms 换向占 {strafe_share:.0%} (--calm 切换)")
    print(f"命中 = 子弹到达时离瞄准点 <= 15cm / 30cm (在目标距离处量)。用时 {time.perf_counter() - started:.0f}s")
    print_tables(results)


if __name__ == "__main__":
    main()
