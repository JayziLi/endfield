"""从已有的延迟日志里直接读出真实的在靶精度。

关键一点: AI 在第 k 帧看到的误差, 是第 k-L 帧拍下来的那一画面里的误差。也就是说
那个数**就是**当时游戏世界里准心和目标的真实偏差, 只是时间戳标晚了 L 帧。
跟踪精度这种分布型指标对时间平移不敏感, 所以连移位都不用做——观测误差的分布
就是真实误差的分布。唯一的污染是检测噪声 σ≈0.8px, 相对 10px 量级可以忽略。
"""
import io
import math
import pathlib
import statistics
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from rhodes_fast.latency_log import read_latency_log  # noqa: E402

rows = read_latency_log(pathlib.Path(sys.argv[1]))
print(f"总帧数 {len(rows)}")

held = [r for r in rows if r.trigger and r.track_id != 0]
print(f"按住瞄准键且画面里有目标: {len(held)} 帧 ({len(held)/len(rows)*100:.1f}%)")

errors = [math.hypot(r.error_x, r.error_y) for r in held]
ex = [abs(r.error_x) for r in held]


def report(name: str, values: list[float]) -> None:
    if not values:
        print(f"{name}: 无数据")
        return
    s = sorted(values)
    print(f"{name:<14} 均值{statistics.fmean(s):>7.1f}  中位{s[len(s)//2]:>7.1f}"
          f"  p95{s[int(len(s)*0.95)]:>7.1f}"
          f"  <=10px{sum(1 for v in s if v <= 10)/len(s)*100:>5.0f}%"
          f"  <=5px{sum(1 for v in s if v <= 5)/len(s)*100:>5.0f}%")


report("2D 偏差", errors)
report("仅水平", ex)

# 分段: 连续按住的一次交火算一段, 中间断开 >150ms 就切段
segments: list[list[float]] = []
current: list[float] = []
last_ms = None
for r in held:
    if last_ms is not None and r.monotonic_ms - last_ms > 150.0:
        if len(current) > 24:
            segments.append(current)
        current = []
    current.append(math.hypot(r.error_x, r.error_y))
    last_ms = r.monotonic_ms
if len(current) > 24:
    segments.append(current)

print(f"\n交火段数 (连续按住, >100ms): {len(segments)}")
if segments:
    lengths = sorted(len(s) for s in segments)
    print(f"段长中位 {lengths[len(lengths)//2]} 帧 (~{lengths[len(lengths)//2]*4.15:.0f}ms), "
          f"最长 {lengths[-1]} 帧 (~{lengths[-1]*4.15/1000:.1f}s)")
    # 去掉每段前 100ms(拉枪段), 剩下的算跟随
    skip = int(100.0 / 4.15)
    track = [v for seg in segments for v in seg[skip:]]
    report("跟随段(去拉枪)", track)
    per_seg = [statistics.fmean(seg[skip:]) for seg in segments if len(seg) > skip]
    if per_seg:
        s = sorted(per_seg)
        print(f"\n各段均值的离散度: 中位 {s[len(s)//2]:.1f}px, "
              f"p10 {s[int(len(s)*0.1)]:.1f}px, p90 {s[int(len(s)*0.9)]:.1f}px")
        print("——这就是真实交火的不可重复性: 同一套参数不同段之间的差, "
              "决定了要多少段才能分辨出两个算法。")
