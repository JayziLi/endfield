from __future__ import annotations

import socket
import threading
import time
import unittest

from rhodes_fast.gui_web.preview_relay import FrameBus, PreviewRelay


class FrameBusTest(unittest.TestCase):
    def test_a_subscriber_gets_the_frame_bytes_unchanged(self) -> None:
        """整条路径不解码。收到什么字节就发什么字节, 这是它比 tkinter 那条
        路径轻的全部理由。"""
        bus = FrameBus()
        self.addCleanup(bus.close)
        got: list[bytes] = []
        ready = threading.Event()

        def reader() -> None:
            ready.set()
            for frame in bus.subscribe():
                got.append(frame)
                return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        ready.wait(timeout=5)
        bus.publish(b"\xff\xd8not-really-a-jpeg\xff\xd9")
        thread.join(timeout=5)
        self.assertEqual(got, [b"\xff\xd8not-really-a-jpeg\xff\xd9"])

    def test_only_the_newest_frame_survives_a_slow_subscriber(self) -> None:
        """预览要的是「现在什么样」。攒一队旧帧只会让延迟越拖越长。"""
        bus = FrameBus()
        self.addCleanup(bus.close)
        for i in range(10):
            bus.publish(f"frame-{i}".encode())
        self.assertEqual(bus.latest, b"frame-9")

    def test_close_ends_the_iteration(self) -> None:
        """不结束的话 MJPEG 那个响应线程会在关窗后活下去。"""
        bus = FrameBus()
        done = threading.Event()

        def reader() -> None:
            for _frame in bus.subscribe():
                pass
            done.set()

        threading.Thread(target=reader, daemon=True).start()
        bus.close()
        self.assertTrue(done.wait(timeout=5), "close() 之后订阅迭代没有结束")

    def test_a_slow_subscriber_skips_instead_of_replaying_the_backlog(self) -> None:
        """跟不上就跳帧, 不补课。

        上一条只看 latest 这个属性, 那证明不了订阅者拿到的是什么 —— 一个把帧
        排进队列的实现照样能让 latest 是最新的, 却让订阅者从头补 10 帧旧画面,
        每补一帧画面就多落后一帧。这里从订阅者那一侧看: 它醒来时只该看见最新的
        那一帧, 中间的必须已经被覆盖掉。
        """
        bus = FrameBus()
        self.addCleanup(bus.close)
        seen: list[bytes] = []
        subscribed = threading.Event()
        go_on = threading.Event()

        def reader() -> None:
            stream = bus.subscribe()
            subscribed.set()
            for frame in stream:
                seen.append(frame)
                # 第一帧之后装死, 让发布侧把后面 9 帧全发完。
                go_on.wait(timeout=5)
                if len(seen) == 2:
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(subscribed.wait(timeout=5))
        bus.publish(b"frame-0")
        while not seen:
            time.sleep(0.005)
        for i in range(1, 10):
            bus.publish(f"frame-{i}".encode())
        go_on.set()
        thread.join(timeout=5)
        self.assertEqual(seen, [b"frame-0", b"frame-9"], "订阅者补了旧帧")

    def test_publish_does_not_wait_for_a_slow_subscriber(self) -> None:
        """消费端卡住绝不能反压到上游。

        预览的订阅者是浏览器, 它随时可能被最小化、被切走、被网络栈拖住。如果
        publish 要等它, 那条阻塞会顺着 UDP 接收线程一路传回去 —— 而接收线程慢了
        就丢数据报, 那才是真正会被看见的延迟。
        """
        bus = FrameBus()
        self.addCleanup(bus.close)
        stuck = threading.Event()
        released = threading.Event()
        self.addCleanup(released.set)

        def reader() -> None:
            for _frame in bus.subscribe():
                stuck.set()
                released.wait(timeout=10)
                return

        threading.Thread(target=reader, daemon=True).start()
        bus.publish(b"first")
        self.assertTrue(stuck.wait(timeout=5), "订阅者没拿到第一帧")

        payload = b"\xff\xd8" + b"x" * 40_000 + b"\xff\xd9"
        started = time.perf_counter()
        for _ in range(100):
            bus.publish(payload)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f"订阅者卡住时 100 次 publish 花了 {elapsed:.3f} 秒")
        released.set()


class PreviewRelayTest(unittest.TestCase):
    def _relay(self) -> tuple[FrameBus, PreviewRelay]:
        """建一对 bus + relay, 并登记好清理。

        addCleanup 是 LIFO, 所以这里先登记 relay.close 再登记 bus.close, 实际
        跑的顺序是 bus 先关。这跟 MJPEG 那边「先 bus.close() 再 server.stop()」
        是同一条规矩: bus 一关, publish 就变成空操作, 拆到一半时还在途的数据报
        再也落不到一个已经没人订阅的总线上。
        """
        bus = FrameBus()
        relay = PreviewRelay(bus)
        self.addCleanup(relay.close)
        self.addCleanup(bus.close)
        return bus, relay

    def _sender(self) -> socket.socket:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        return sender

    def _wait_for(self, bus: FrameBus, expected: bytes, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while bus.latest != expected and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(bus.latest, expected)

    def test_a_datagram_lands_on_the_bus_byte_for_byte(self) -> None:
        """收到什么字节就推什么字节。这条路径上没有 imdecode, 也没有重新编码 ——
        中继层从来不看一眼像素, 所以「原样」是可以逐字节断言的。"""
        bus, relay = self._relay()
        port = relay.start()

        payload = b"\xff\xd8" + bytes(range(256)) * 4 + b"\xff\xd9"
        self._sender().sendto(payload, ("127.0.0.1", port))

        self._wait_for(bus, payload)

    def test_it_binds_loopback_and_reports_the_port(self) -> None:
        """端口要写进子进程的命令行, 所以必须先 bind 才知道号码 ——
        start() 的返回值就是这个用途。"""
        bus, relay = self._relay()
        port = relay.start()
        self.assertGreater(port, 0)
        self.assertEqual(relay.port, port)
        self.assertEqual(relay.address[0], "127.0.0.1", "预览端口不该绑到 127.0.0.1 之外")

    def test_close_is_safe_before_start(self) -> None:
        """窗口可能在管线还没起来时就被关掉, 那时候 relay 还没 start 过。"""
        PreviewRelay(FrameBus()).close()

    def test_an_empty_datagram_does_not_kill_the_loop(self) -> None:
        """管线把帧压到 60,000 字节以下, 但任何东西都能往一个 UDP 端口发包。
        一个坏包不该让预览从此黑屏 —— 跳过它, 下一帧照常到。"""
        bus, relay = self._relay()
        port = relay.start()

        sender = self._sender()
        sender.sendto(b"", ("127.0.0.1", port))  # 空包
        sender.sendto(b"\xff\xd8ok\xff\xd9", ("127.0.0.1", port))

        self._wait_for(bus, b"\xff\xd8ok\xff\xd9")

    def test_a_maximum_sized_datagram_survives(self) -> None:
        """recvfrom(65_507) 是 UDP 数据报的理论上限。正好这么大的包必须完整落地,
        不能被截断 —— 截断一半的 JPEG 会让浏览器把整条 MJPEG 流判成坏的。"""
        bus, relay = self._relay()
        port = relay.start()

        sender = self._sender()
        # 发送缓冲默认可能不到 64KB, 太小时 sendto 直接 WSAENOBUFS。
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1_048_576)
        payload = b"\xff\xd8" + b"M" * (65_507 - 4) + b"\xff\xd9"
        self.assertEqual(len(payload), 65_507)
        sender.sendto(payload, ("127.0.0.1", port))

        self._wait_for(bus, payload)

        # 扛过上限之后还得继续工作, 否则「没崩」也没有意义。
        sender.sendto(b"\xff\xd8after\xff\xd9", ("127.0.0.1", port))
        self._wait_for(bus, b"\xff\xd8after\xff\xd9")

    def test_close_really_stops_the_pump_thread(self) -> None:
        """settimeout(0.25) 的全部意义就是让读取线程有机会看见 close。

        它要是看不见, 每开一次预览就多留一条线程和一个占着的 UDP 端口, 而这两样
        都要到进程退出才还回来。开关 20 次是因为一次看不出「累积」。
        """
        before = threading.active_count()
        for _ in range(20):
            bus = FrameBus()
            relay = PreviewRelay(bus)
            relay.start()
            relay.close()
            bus.close()

        deadline = time.monotonic() + 10
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(
            threading.active_count(),
            before,
            f"20 次开关之后线程从 {before} 涨到 {threading.active_count()}",
        )


if __name__ == "__main__":
    unittest.main()


class RelayRestartTest(unittest.TestCase):
    """界面每次启动管线都调 start()。不防重入的话第二次会孤儿掉第一个 socket
    和泵线程 —— 它们还绑着端口、还在收帧, 但再没人能关掉它们。"""

    def test_starting_twice_reuses_the_same_socket(self) -> None:
        bus = FrameBus()
        relay = PreviewRelay(bus)
        self.addCleanup(bus.close)
        self.addCleanup(relay.close)

        first = relay.start()
        pump = relay._thread
        second = relay.start()
        third = relay.start()

        self.assertEqual(first, second)
        self.assertEqual(first, third)
        # 三次 start 只该有一条泵线程, 不是三条。
        #
        # 断言的是「还是同一个线程对象」, 不是数线程。数线程要么用
        # threading.active_count() (进程全局), 要么按名字扫 threading.enumerate()
        # —— 两种都受别的用例影响: 全量跑时它们的后台线程随时在生灭 (模态框那
        # 几条、MJPEG 的响应线程、别处建的中继), 于是这条断言成了随机数, 单独
        # 跑这个模块绿、全量跑偶尔红, 而且跟本模块一点关系都没有。两种都实际
        # 踩过。同一个对象是确定的, 而且它正是这条测试真正想说的话。
        self.assertIs(relay._thread, pump)
        self.assertTrue(pump.is_alive())

    def test_the_reused_port_still_delivers_frames(self) -> None:
        """端口一样还不够 —— 得确认还在收。"""
        import socket as socket_module
        import time

        bus = FrameBus()
        relay = PreviewRelay(bus)
        self.addCleanup(bus.close)
        self.addCleanup(relay.close)

        port = relay.start()
        relay.start()

        sender = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(b"\xff\xd8restarted\xff\xd9", ("127.0.0.1", port))

        deadline = time.monotonic() + 5
        while bus.latest is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(bus.latest, b"\xff\xd8restarted\xff\xd9")
