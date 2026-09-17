"""KMBox 监听口自检: 预览轨迹要靠监听口读手的移动, 这三件事得在自己的硬件上确认。

1. 程序通过 KMBox 发出去的移动, 监听口会不会也报回来(会的话轨迹要避免算两遍;
   标定会自动识别, 这里只是亲眼确认一下)。
2. 只按按键不动鼠标时, 报文里会不会带着旧的位移(会的话手的轨迹会凭空多出来)。
3. 来回推鼠标时会不会丢包(丢包的话手的轨迹会越画越偏)。

用法: .venv\\Scripts\\python.exe tools\\kmbox_monitor_check.py [settings.txt]
第 1 步会真的移动鼠标(先右移再移回原处), 请在桌面上运行, 不要开着游戏。
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

from kmbox_universal import KMBoxClient

from rhodes_fast.config import load_config

_STEPS = 10
_STEP_COUNTS = 100


class _Tally:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.x = 0
            self.y = 0
            self.packets = 0
            self.moving_packets = 0
            self.trace: list[tuple[float, int]] = []

    def on_report(self, state) -> None:
        mouse = state.mouse
        with self._lock:
            self.packets += 1
            if mouse.x or mouse.y:
                self.moving_packets += 1
                self.x += mouse.x
                self.y += mouse.y
                self.trace.append((time.perf_counter(), self.x))

    def snapshot(self) -> tuple[int, int, int, int, list[tuple[float, int]]]:
        with self._lock:
            return self.x, self.y, self.packets, self.moving_packets, list(self.trace)


def turning_points(values: list[int], hysteresis: float) -> list[tuple[str, int]]:
    """来回推的轨迹里, 每一次到头的位置。方向反过来超过 hysteresis 才算真的掉头。"""
    points: list[tuple[str, int]] = []
    if not values:
        return points
    direction = 0
    extreme = values[0]
    for value in values[1:]:
        if direction >= 0 and value > extreme:
            extreme = value
            direction = 1
        elif direction <= 0 and value < extreme:
            extreme = value
            direction = -1
        elif direction == 1 and extreme - value > hysteresis:
            points.append(("right", extreme))
            extreme = value
            direction = -1
        elif direction == -1 and value - extreme > hysteresis:
            points.append(("left", extreme))
            extreme = value
            direction = 1
    if direction:
        points.append(("right" if direction == 1 else "left", extreme))
    return points


def _echo_test(client: KMBoxClient, tally: _Tally, encrypted: bool) -> None:
    move = client.enc_move if encrypted else client.move
    print("\n[1/3] 程序发出的移动会不会出现在监听数据里")
    print(f"      鼠标会先向右移动 {_STEPS * _STEP_COUNTS} 计数, 再移回原处。")
    input("      手离开鼠标, 按回车开始……")
    time.sleep(0.3)
    tally.reset()
    for _ in range(_STEPS):
        move(_STEP_COUNTS, 0)
        time.sleep(0.02)
    time.sleep(0.3)
    x, y, packets, _moving, _trace = tally.snapshot()
    for _ in range(_STEPS):
        move(-_STEP_COUNTS, 0)
        time.sleep(0.02)
    sent = _STEPS * _STEP_COUNTS
    print(f"      发出 {sent} 计数, 监听口报回 x={x} y={y}（{packets} 个包）")
    if abs(x) < sent * 0.1:
        print("      结论: 不含。监听数据只有手的移动。")
    elif abs(x - sent) < sent * 0.2:
        print("      结论: 含。程序发出的移动也会报回来, 轨迹标定会自动避免算两遍。")
    else:
        print("      结论: 不确定。报回的量和发出的对不上, 可能手碰到了鼠标, 再测一次。")


def _button_test(tally: _Tally) -> None:
    print("\n[2/3] 只按按键时, 报文里会不会带着旧的位移")
    input("      接下来 10 秒: 先动一下鼠标, 然后手离开, 只按几下键盘和鼠标按键。按回车开始……")
    time.sleep(0.2)
    # 先让鼠标动过, 这样「旧的位移」才有东西可带。
    print("      现在动一下鼠标……")
    time.sleep(2.0)
    tally.reset()
    print("      停手, 只按按键（8 秒）……")
    time.sleep(8.0)
    x, y, packets, moving, _trace = tally.snapshot()
    print(f"      这 8 秒收到 {packets} 个包, 其中带位移的 {moving} 个, 累计 x={x} y={y}")
    if moving == 0:
        print("      结论: 正常。按键的包不带位移。")
    else:
        print("      结论: 有问题, 或者手碰到了鼠标。按键的包里带着位移的话, 手的轨迹会凭空多出来。")


def _loss_test(tally: _Tally) -> None:
    print("\n[3/3] 会不会丢包")
    print("      把鼠标抵住书边或键盘边, 在两个固定的位置之间来回推 20 次。")
    input("      准备好按回车开始, 推完再按一次回车……")
    tally.reset()
    started = time.perf_counter()
    input("      推吧, 推完按回车……")
    elapsed = time.perf_counter() - started
    x, _y, packets, moving, trace = tally.snapshot()
    values = [value for _time, value in trace]
    if len(values) < 50:
        print("      收到的位移太少, 没法判断。确认监听口开着、鼠标确实在动。")
        return
    span = max(values) - min(values)
    stops = turning_points(values, hysteresis=span * 0.3)
    lefts = [value for side, value in stops if side == "left"]
    rights = [value for side, value in stops if side == "right"]
    print(f"      {elapsed:.1f} 秒内收到 {packets} 个包（带位移的 {moving} 个, 约 {moving / max(elapsed, 1e-9):.0f} 个/秒）")
    print(f"      到头 {len(stops)} 次。左端: {lefts[:3]} … {lefts[-3:]}  右端: {rights[:3]} … {rights[-3:]}")
    if len(lefts) < 3 or len(rights) < 3:
        print("      来回次数太少, 没法判断漂移。")
        return
    stroke = sum(r - l for l, r in zip(lefts, rights)) / min(len(lefts), len(rights))
    drift = lefts[-1] - lefts[0]
    print(f"      每次行程约 {stroke:.0f} 计数, 左端从头到尾漂了 {drift} 计数（{abs(drift) / stroke * 100:.1f}%）")
    if abs(drift) <= stroke * 0.03:
        print("      结论: 基本不丢包。")
    else:
        print("      结论: 漂移明显, 可能在丢包（也可能推的时候没抵紧）。手的轨迹会越画越偏。")


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    settings = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("settings.txt")
    config = load_config(settings, validate_model=False)
    device = config.kmbox
    print(f"连接 KMBox {device.host}:{device.port}, 监听端口 {device.monitor_port}")
    client = KMBoxClient(device.host, device.port, device.uuid, timeout=device.timeout_seconds)
    tally = _Tally()
    try:
        client.monitor_start(device.monitor_port)
        client.monitor.add_callback(tally.on_report)
        _echo_test(client, tally, device.encrypted)
        _button_test(tally)
        _loss_test(tally)
    except KeyboardInterrupt:
        print("\n已中止。")
    finally:
        client.close()


if __name__ == "__main__":
    main()
