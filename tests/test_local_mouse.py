"""本机鼠标: 用 SendInput 发移动, 用 GetAsyncKeyState 读触发键。

user32 一律是假的: 单元测试不能真去动用户的鼠标。真机另验 (规格 §8)。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import unittest

from rhodes_fast.local_mouse import (
    INPUT,
    INPUT_MOUSE,
    MOUSEEVENTF_MOVE,
    RAWINPUT,
    LocalMouseClient,
    RawMouseMonitor,
    check_local_mouse,
    parse_raw_mouse,
)


class FakeUser32:
    def __init__(self, *, sent: int = 1, down: frozenset[int] = frozenset(), low_bit: frozenset[int] = frozenset()):
        self.sent = sent
        self.down = down
        self.low_bit = low_bit
        self.inputs: list[tuple[int, int, int, int, int]] = []
        self.calls: list[tuple[int, int]] = []
        self.queried: list[int] = []

    def SendInput(self, count, pointer, size):  # noqa: N802 - Win32 的函数名
        self.calls.append((count, size))
        record = pointer.contents
        mouse = record.u.mi
        self.inputs.append((record.type, mouse.dx, mouse.dy, mouse.dwFlags, mouse.mouseData))
        return self.sent

    def GetAsyncKeyState(self, vk):  # noqa: N802
        self.queried.append(vk)
        if vk in self.down:
            return -32768  # SHORT 的最高位: 此刻按着
        if vk in self.low_bit:
            return 1  # 最低位: 上次查过之后按过, 现在已经松开
        return 0


class InputLayoutTest(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64 位布局")
    def test_the_input_record_has_the_64_bit_size(self) -> None:
        """SendInput 的第三个参数必须是 sizeof(INPUT)。union 里只声明 MOUSEINPUT
        或者对齐错了, 大小就不是 40, SendInput 直接返回 0, 什么都不发 —— 而表现
        只是「鼠标不动」。"""
        self.assertEqual(ctypes.sizeof(INPUT), 40)


class MoveTest(unittest.TestCase):
    def test_a_move_is_one_relative_mouse_input(self) -> None:
        user32 = FakeUser32()
        LocalMouseClient(user32=user32).move(3, -2)
        self.assertEqual(user32.inputs, [(INPUT_MOUSE, 3, -2, MOUSEEVENTF_MOVE, 0)])
        self.assertEqual(user32.calls, [(1, ctypes.sizeof(INPUT))])

    def test_the_flag_is_relative_not_absolute(self) -> None:
        """带上 MOUSEEVENTF_ABSOLUTE 的话 dx/dy 是屏幕坐标 (0..65535), 鼠标会
        跳到屏幕左上角附近。"""
        self.assertEqual(MOUSEEVENTF_MOVE, 0x0001)

    def test_a_refused_input_raises_oserror(self) -> None:
        """控制器接住 OSError 之后把这一帧记成 (0, 0)。吞掉的话会被记成已经发出,
        「扣在途」会减掉一段根本没发生的位移。"""
        with self.assertRaises(OSError):
            LocalMouseClient(user32=FakeUser32(sent=0)).move(1, 1)


class TriggerTest(unittest.TestCase):
    VKS = {"left": 0x01, "right": 0x02, "side1": 0x05, "side2": 0x06}

    def test_each_trigger_reads_its_own_button(self) -> None:
        """侧键映射反了的话, 两套方案会悄悄互换。"""
        for trigger, vk in self.VKS.items():
            with self.subTest(trigger=trigger):
                client = LocalMouseClient(user32=FakeUser32(down=frozenset({vk})))
                pressed = {name: getattr(client, f"isdown_{name}")() for name in self.VKS}
                self.assertEqual(pressed, {name: name == trigger for name in self.VKS})

    def test_a_press_that_is_already_released_does_not_count(self) -> None:
        """最低位的意思是「上次查过之后按过」, 不是「现在按着」。当成按着的话,
        点一下侧键会让瞄准多跑一帧。"""
        client = LocalMouseClient(user32=FakeUser32(low_bit=frozenset({0x02})))
        self.assertFalse(client.isdown_right())


class LifecycleTest(unittest.TestCase):
    def test_close_stops_the_hand_monitor(self) -> None:
        stopped: list[bool] = []
        monitor = type("Monitor", (), {"stop": lambda self: stopped.append(True)})()
        client = LocalMouseClient(user32=FakeUser32(), monitor=monitor)
        client.close()
        client.close()
        self.assertEqual(stopped, [True])

    def test_close_without_a_monitor_is_harmless(self) -> None:
        LocalMouseClient(user32=FakeUser32()).close()


class CheckTest(unittest.TestCase):
    def test_the_check_reads_a_button_and_sends_nothing(self) -> None:
        """「测试输入」不能动用户的鼠标: 只确认拿得到这两个函数, 读一次按键。"""
        user32 = FakeUser32()
        check_local_mouse(user32)
        self.assertEqual(user32.inputs, [])
        self.assertEqual(user32.queried, [0x01])

    def test_the_check_fails_when_the_functions_are_missing(self) -> None:
        with self.assertRaises(OSError):
            check_local_mouse(object())


def _raw(*, dx: int = 0, dy: int = 0, device: int = 0x1234, kind: int = 0, flags: int = 0) -> bytes:
    """造一份 GetRawInputData 会写出来的 RAWINPUT。"""
    record = RAWINPUT()
    record.header.dwType = kind
    record.header.dwSize = ctypes.sizeof(RAWINPUT)
    record.header.hDevice = device
    record.data.mouse.usFlags = flags
    record.data.mouse.lLastX = dx
    record.data.mouse.lLastY = dy
    return bytes(record)


class ParseRawMouseTest(unittest.TestCase):
    def test_a_relative_move_from_a_device_counts(self) -> None:
        self.assertEqual(parse_raw_mouse(_raw(dx=7, dy=-3)), (7, -3))

    def test_our_own_injected_moves_are_skipped(self) -> None:
        """SendInput 产生的原始输入没有设备来源 (hDevice 为 0)。算进来的话, 程序
        自己发的移动会被当成手的移动再算一遍, 轨迹上的镜头移动翻倍。"""
        self.assertIsNone(parse_raw_mouse(_raw(dx=7, dy=-3, device=0)))

    def test_absolute_positions_are_not_movements(self) -> None:
        """数位板、远程桌面报的是绝对坐标 (0..65535)。当成相对位移的话,
        一下就是几万个计数。"""
        self.assertIsNone(parse_raw_mouse(_raw(dx=30000, dy=20000, flags=0x01)))

    def test_keyboards_and_other_devices_are_ignored(self) -> None:
        self.assertIsNone(parse_raw_mouse(_raw(dx=7, kind=1)))

    def test_a_button_without_movement_is_nothing(self) -> None:
        self.assertIsNone(parse_raw_mouse(_raw()))

    def test_a_short_buffer_is_ignored_not_misread(self) -> None:
        self.assertIsNone(parse_raw_mouse(_raw(dx=7)[:10]))


@unittest.skipUnless(sys.platform == "win32", "Raw Input 只在 Windows 上有")
class RawMouseMonitorLifecycleTest(unittest.TestCase):
    """真开一个 message-only 窗口并注册 Raw Input。不动鼠标, 只验开得起来、关得掉。"""

    def test_it_starts_and_stops_cleanly(self) -> None:
        monitor = RawMouseMonitor(lambda dx, dy: None)
        monitor.start()
        monitor.stop()
        self.assertFalse(any(thread.name == "raw-mouse" for thread in threading.enumerate()))

    def test_it_can_be_started_again_after_stopping(self) -> None:
        """重连 (停了再启动管线) 走的就是这条: 窗口类没注销的话第二次注册会失败。"""
        for _ in range(2):
            monitor = RawMouseMonitor(lambda dx, dy: None)
            monitor.start()
            monitor.stop()

    def test_stop_is_quick_and_idempotent(self) -> None:
        monitor = RawMouseMonitor(lambda dx, dy: None)
        monitor.start()
        started = time.perf_counter()
        monitor.stop()
        monitor.stop()
        self.assertLess(time.perf_counter() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
