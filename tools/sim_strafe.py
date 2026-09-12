"""拉枪 + 随机左右跟随的闭环仿真。

控制律不重写: 直接驱动真实的 KmboxController.move_toward, 所以死区、EMA 平滑、
亚像素累加、限幅、动态 kp、以及「进死区就清在途窗口」这些production行为都在里面。
仿真只负责被控对象一侧: 目标怎么动、画面延迟多少帧、检测噪声多大。
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

from rhodes_fast.config import AimConfig, AimProfileConfig, KmboxConfig
from rhodes_fast.detector import Detection
from rhodes_fast.kmbox_control import KmboxController

FRAME_W = FRAME_H = 640
FPS = 241.0
DT_MS = 1000.0 / FPS
TRUE_LAG = 7          # 实测回路延迟(帧)
LAG_JITTER = 1
NOISE_SIGMA = 0.8     # 实测检测噪声
STRAFE_SPEED = 482.0  # px/s, 实测
STRAFE_BOUND = 120.0
HOLD_MS = (80.0, 350.0)
FLICK_X = 100.0
FLICK_Y = 30.0
HIT_PX = 10.0         # 「在靶上」的判据
MOVING = True         # False = 静止靶对照


def _detection(error_x: float, error_y: float, ratio: float) -> Detection:
    """反解一个 Detection, 使 move_toward 算出来的误差正好是给定值。"""
    cx = FRAME_W * 0.5 + error_x
    aim_y = FRAME_H * 0.5 + error_y
    half = 20.0
    height = 60.0
    y1 = aim_y - height * ratio
    return Detection(cx - half, y1, cx + half, y1 + height, 0.9, 0)


class Plant:
    """画面 = 世界的 L 帧前快照。鼠标指令第 m 帧发出, 第 m+L 帧才看得见。"""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.cmds: list[tuple[float, float]] = []
        self.tgt: list[tuple[float, float]] = []
        self.tx = FLICK_X
        self.ty = FLICK_Y
        self.dir = rng.choice((-1.0, 1.0))
        self.hold_left = rng.uniform(*HOLD_MS)

    def step_target(self) -> None:
        if not MOVING:
            self.tgt.append((self.tx, self.ty))
            return
        self.hold_left -= DT_MS
        if self.hold_left <= 0.0:
            self.dir = -self.dir
            self.hold_left = self.rng.uniform(*HOLD_MS)
        self.tx += self.dir * STRAFE_SPEED * DT_MS / 1000.0
        if abs(self.tx) > STRAFE_BOUND:
            self.tx = math.copysign(STRAFE_BOUND, self.tx)
            self.dir = -self.dir
        self.tgt.append((self.tx, self.ty))

    def observed(self, frame: int) -> tuple[float, float]:
        """第 frame 帧看到的误差 = 快照时刻的 目标位置 - 相机位置。"""
        lag = TRUE_LAG + self.rng.randint(-LAG_JITTER, LAG_JITTER)
        snap = frame - lag + 1
        if snap < 0:
            snap = 0
        tx, ty = self.tgt[snap] if snap < len(self.tgt) else self.tgt[-1]
        cam_x = sum(c[0] for c in self.cmds[:snap])
        cam_y = sum(c[1] for c in self.cmds[:snap])
        return tx - cam_x, ty - cam_y

    def truth(self, frame: int) -> tuple[float, float]:
        """当前真实误差(用于评分, 控制器看不到)。"""
        tx, ty = self.tgt[-1]
        return tx - sum(c[0] for c in self.cmds), ty - sum(c[1] for c in self.cmds)


def run(algorithm: str, params: dict, gains: tuple[float, float, float], seed: int,
        frames: int = 600) -> dict:
    rng = random.Random(seed)
    kp_min, kp_max, kp_growth = gains
    folder = tempfile.TemporaryDirectory()
    runtime = Path(folder.name) / "aim.json"
    runtime.write_text(json.dumps({"profiles": []}), encoding="utf-8")
    profile = AimProfileConfig(
        trigger="side1", kp_min=kp_min, kp_max=kp_max, kp_growth=kp_growth,
        target_y_ratio=0.5, algorithm=algorithm, algorithm_params=params,
    )
    controller = KmboxController(
        KmboxConfig(uuid="00000000"),
        AimConfig(smoothing=1.0, deadzone=2.0, max_step=30, target_y_ratio=0.5),
        runtime,
        profiles=(profile, AimProfileConfig(enabled=False, trigger="left")),
    )
    plant = Plant(rng)
    client = Mock()
    controller._client = client
    client.move.side_effect = lambda dx, dy: None

    errors: list[float] = []
    for frame in range(frames):
        plant.step_target()
        ex, ey = plant.observed(frame)
        ex += rng.gauss(0.0, NOISE_SIGMA)
        ey += rng.gauss(0.0, NOISE_SIGMA)
        dx, dy = controller.move_toward(_detection(ex, ey, 0.5), FRAME_W, FRAME_H)
        plant.cmds.append((float(dx), float(dy)))
        tx, ty = plant.truth(frame)
        errors.append(math.hypot(tx, ty))
    folder.cleanup()

    # 拉枪段: 第一次进到 HIT_PX 以内算抓到
    acquire = next((i for i, e in enumerate(errors) if e <= HIT_PX), None)
    settle = (acquire + 1) * DT_MS if acquire is not None else float("nan")
    window = int(100.0 / DT_MS)
    if acquire is None:
        rebound = float("nan")
        start = int(200.0 / DT_MS)
    else:
        # 回摆: 抓到之后 100ms 内冲出去多远。目标在动, 所以这不是教科书超调,
        # 而是「刚咬上就被甩掉多少」——这才是实战里感觉得到的那个东西。
        rebound = max(errors[acquire : acquire + window] or [0.0])
        start = acquire + window
    track = errors[start:] or errors[-100:]
    ordered = sorted(track)
    return {
        "settle_ms": settle,
        "rebound": rebound,
        "track_mean": statistics.fmean(track),
        "track_p95": ordered[int(len(ordered) * 0.95)],
        "hit_rate": sum(1 for e in track if e <= HIT_PX) / len(track),
        "hit5": sum(1 for e in track if e <= 5.0) / len(track),
    }


def trials(algorithm: str, params: dict, gains, seeds=range(24)) -> dict:
    rows = [run(algorithm, params, gains, seed) for seed in seeds]
    out = {}
    for key in rows[0]:
        values = [r[key] for r in rows if not math.isnan(r[key])]
        out[key] = statistics.fmean(values) if values else float("nan")
    out["lost"] = sum(1 for r in rows if math.isnan(r["settle_ms"]))
    return out


USER_GAINS = (0.023, 0.083, 0.031)

CASES = [
    ("p", {}, "比例(现状)"),
    ("pd", {"kd": 0.3}, "pd kd=0.3"),
    ("pd", {"kd": 0.6}, "pd kd=0.6"),
    ("feedforward", {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25}, "前馈 g=1.0"),
    ("feedforward", {"loop_delay_frames": 8.0, "gain": 1.2, "velocity_smoothing": 0.25}, "前馈 g=1.2"),
    ("feedforward", {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.40}, "前馈 平滑0.4"),
    ("inflight", {"loop_delay_frames": 8.0}, "扣在途"),
    ("inflight_ff", {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25}, "扣在途+前馈"),
]

HEADER = f"{'算法':<16}{'增益':>6}{'抓到ms':>8}{'回摆px':>8}{'跟随均值':>9}{'跟随p95':>9}{'<=10px':>8}{'<=5px':>7}{'丢':>4}"


def line(label: str, scale: float, r: dict) -> str:
    return (f"{label:<16}{scale:>5.1f}x{r['settle_ms']:>8.0f}{r['rebound']:>8.1f}"
            f"{r['track_mean']:>9.1f}{r['track_p95']:>9.1f}"
            f"{r['hit_rate']*100:>7.0f}%{r['hit5']*100:>6.0f}%{r['lost']:>4}")


def table(title: str, scales, cases=None) -> None:
    print()
    print(f"=== {title} ===")
    print(HEADER)
    print("-" * 76)
    for name, params, label in (cases or CASES):
        for scale in scales:
            gains = tuple(g * scale for g in USER_GAINS)
            print(line(label, scale, trials(name, params, gains)))


FINE = [
    ("p", {}, "比例(现状)"),
    ("pd", {"kd": 0.6}, "pd kd=0.6"),
    ("feedforward", {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25}, "前馈"),
    ("inflight", {"loop_delay_frames": 8.0}, "扣在途"),
    ("inflight_ff", {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25}, "扣在途+前馈"),
]

# 拟人化那三个 vs 基线。1.5x 增益是前面扫出来的 feedforward 最佳工作点。
# 注意 windmouse 完全不用 kp(它有自己的 gravity), 所以增益倍数对它没有意义。
_FF = {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25}
_WM = {"gravity": 4.0, "wind": 2.0, "max_step": 3.0, "damp_px": 25.0,
       "lead_frames": 8.0, "seed": 11.0}

HUMAN = [
    ("feedforward", _FF, "前馈（基线）"),
    ("feedforward_wind", {**_FF, "wind_strength": 0.0, "wind_decay_px": 40.0, "seed": 7.0},
     "风 0.0（应同基线）"),
    ("feedforward_wind", {**_FF, "wind_strength": 0.2, "wind_decay_px": 40.0, "seed": 7.0},
     "风 0.2"),
    ("feedforward_wind", {**_FF, "wind_strength": 0.35, "wind_decay_px": 40.0, "seed": 7.0},
     "风 0.35（默认）"),
    ("feedforward_wind", {**_FF, "wind_strength": 0.7, "wind_decay_px": 40.0, "seed": 7.0},
     "风 0.7"),
    ("feedforward_bezier", {**_FF, "arc_strength": 0.25, "commit_frames": 0.0, "seed": 7.0},
     "贝塞尔 0.25 闭环"),
    ("feedforward_bezier", {**_FF, "arc_strength": 0.6, "commit_frames": 0.0, "seed": 7.0},
     "贝塞尔 0.6 闭环"),
    ("feedforward_bezier", {**_FF, "arc_strength": 0.25, "commit_frames": 4.0, "seed": 7.0},
     "贝塞尔 承诺 4 帧"),
    ("feedforward_bezier", {**_FF, "arc_strength": 0.25, "commit_frames": 8.0, "seed": 7.0},
     "贝塞尔 承诺 8 帧"),
    ("windmouse", {**_WM, "wind": 0.0}, "WindMouse 无风"),
    ("windmouse", _WM, "WindMouse 默认"),
    ("windmouse", {**_WM, "wind": 6.0}, "WindMouse 风 6"),
]

LAGS = [
    ("feedforward", {"loop_delay_frames": L, "gain": 1.0, "velocity_smoothing": 0.25}, f"前馈 L={L:.0f}")
    for L in (4.0, 6.0, 7.0, 8.0, 10.0, 14.0)
]


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(f"条件: {FPS:.0f}fps  真实延迟{TRUE_LAG}+-{LAG_JITTER}帧  检测噪声sigma={NOISE_SIGMA}px")
    print(f"      拉枪({FLICK_X:.0f},{FLICK_Y:.0f})px, 随后目标{STRAFE_SPEED:.0f}px/s 每{HOLD_MS[0]:.0f}~{HOLD_MS[1]:.0f}ms 随机换向")
    print(f"      1x增益 = settings.txt 方案1 实际值 kp_min/max/growth={USER_GAINS}")
    print(f"      每格 24 个随机种子平均。控制律走真实 KmboxController.move_toward")
    table("拉枪 + 随机左右跟随", (1.0,))
    table("增益扫描", (2.0, 4.0))
    table("细扫增益 (每个算法各自的最佳工作点)", (1.5, 2.0, 2.5, 3.0), FINE)
    table("前馈对 loop_delay_frames 的敏感度 (真实延迟 7 帧)", (2.0,), LAGS)
    table("拟人化的代价 (1.5x 增益)", (1.5,), HUMAN)