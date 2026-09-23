"""本机屏幕采集源。

camera 一律是假的: 这里钉的是「抓哪一块、什么时候发布、坏了怎么恢复」,
真抓屏验不出这些, 还会让测试依赖这台机器的显示器。真机另验 (规格 §8)。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

import numpy as np

from rhodes_fast.config import DesktopConfig
from rhodes_fast.desktop_source import DesktopSource, capture_region


def _frame(value: int, size: tuple[int, int] = (320, 320)) -> np.ndarray:
    return np.full((size[1], size[0], 3), value, dtype=np.uint8)


class FakeCamera:
    """grab 按脚本返回: 数组 = 新帧, None = 画面没变, 异常 = 抛出去。脚本放完
    之后一直返回 None, 就像一块不再变化的屏幕。"""

    def __init__(self, script=(), *, width: int = 2560, height: int = 1440, present_time=None) -> None:
        self.width = width
        self.height = height
        self.script = deque(script)
        self.grabs: list[tuple] = []
        self.released = False
        if present_time is not None:
            self._duplicator = SimpleNamespace(latest_frame_time=present_time)

    def grab(self, region=None, new_frame_only=True):
        self.grabs.append((region, new_frame_only))
        if not self.script:
            time.sleep(0.001)
            return None
        item = self.script.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def release(self) -> None:
        self.released = True


class FakeFactory:
    def __init__(self, *cameras) -> None:
        self.cameras = deque(cameras)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        camera = self.cameras.popleft() if len(self.cameras) > 1 else self.cameras[0]
        if isinstance(camera, BaseException):
            raise camera
        return camera


def _source(factory, **config) -> DesktopSource:
    source = DesktopSource(DesktopConfig(**config), create_camera=factory, retry_seconds=0.01)
    return source


class CaptureRegionTest(unittest.TestCase):
    def test_it_is_the_middle_of_the_screen(self) -> None:
        """准心在屏幕正中央。算偏了, 画面中心就不是准心, 所有瞄准都偏同样多。"""
        self.assertEqual(capture_region(2560, 1440, 320, 320), (1120, 560, 1440, 880))

    def test_odd_leftovers_round_down(self) -> None:
        self.assertEqual(capture_region(1921, 1081, 320, 320), (800, 380, 1120, 700))

    def test_the_whole_screen_is_allowed(self) -> None:
        self.assertEqual(capture_region(640, 480, 640, 480), (0, 0, 640, 480))

    def test_a_region_bigger_than_the_screen_is_refused(self) -> None:
        """悄悄裁成屏幕大小的话, 送进模型的画面尺寸就跟设置里写的不一样了。"""
        with self.assertRaises(ValueError) as caught:
            capture_region(1920, 1080, 2000, 320)
        self.assertIn("1920x1080", str(caught.exception))
        self.assertIn("2000x320", str(caught.exception))


class DesktopSourceTest(unittest.TestCase):
    def _wait(self, source: DesktopSource, after: int = 0):
        snapshot = source.wait_next(after, timeout=2.0)
        self.assertIsNotNone(snapshot, source.error)
        return snapshot

    def test_it_grabs_the_middle_of_the_chosen_monitor_in_bgr(self) -> None:
        camera = FakeCamera([_frame(7)])
        factory = FakeFactory(camera)
        source = _source(factory, backend="winrt", monitor=1)
        source.start()
        self.addCleanup(source.stop)
        snapshot = self._wait(source)
        self.assertEqual(int(snapshot.frame[0, 0, 0]), 7)
        self.assertEqual(snapshot.sequence, 1)
        self.assertEqual(factory.calls[0], {"output_idx": 1, "output_color": "BGR", "backend": "winrt"})
        self.assertEqual(camera.grabs[0], ((1120, 560, 1440, 880), True))

    def test_an_unchanged_screen_publishes_nothing(self) -> None:
        """grab 返回 None 是「画面没变」。当成新帧发布的话, 同一帧会被推理两次,
        时间类算法还会按两帧的间隔去算速度。"""
        camera = FakeCamera([None, None, _frame(1), None, None, _frame(2)])
        source = _source(FakeFactory(camera))
        source.start()
        self.addCleanup(source.stop)
        first = self._wait(source)
        second = self._wait(source, first.sequence)
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(int(second.frame[0, 0, 0]), 2)

    def test_the_timestamps_cover_the_grab(self) -> None:
        camera = FakeCamera([_frame(3)])
        source = _source(FakeFactory(camera))
        before = time.perf_counter()
        source.start()
        self.addCleanup(source.stop)
        snapshot = self._wait(source)
        self.assertGreaterEqual(snapshot.first_packet_at, before)
        self.assertLessEqual(snapshot.first_packet_at, snapshot.ready_at)
        # 拿不到出帧时间: 没有「等」这一段, 全部算在抓这一下上。
        self.assertEqual(snapshot.assembly_ms, 0.0)
        self.assertAlmostEqual(
            snapshot.decode_ms, (snapshot.ready_at - snapshot.first_packet_at) * 1000.0, places=6
        )

    def test_the_present_time_is_used_when_it_is_believable(self) -> None:
        """DXGI 的 LastPresentTime 跟 perf_counter 是同一个时钟 (QPC)。能用的话,
        延迟就多量出「画面出来到被抓到」那一段, 记在 assembly_ms 上 —— 跟 UDP 那边
        「等分片到齐」同一个位置, 不混进 decode_ms。"""
        presented = time.perf_counter() - 0.004
        camera = FakeCamera([_frame(3)], present_time=presented)
        source = _source(FakeFactory(camera))
        source.start()
        self.addCleanup(source.stop)
        snapshot = self._wait(source)
        self.assertEqual(snapshot.first_packet_at, presented)
        self.assertGreaterEqual(snapshot.assembly_ms, 4.0)
        self.assertAlmostEqual(
            snapshot.assembly_ms + snapshot.decode_ms,
            (snapshot.ready_at - presented) * 1000.0,
            places=6,
        )

    def test_the_wgc_present_time_is_not_trusted(self) -> None:
        """实测 WGC 那条路: 135 帧里只有 27 帧的出帧时间过得了合理性检查, 过了的
        里面还有一帧老到 65ms —— 说明它给的不一定是这一帧的时间。拿它当起点,
        延迟日志会凭空多出几十毫秒。"""
        presented = time.perf_counter() - 0.004
        camera = FakeCamera([_frame(3)], present_time=presented)
        source = _source(FakeFactory(camera), backend="winrt")
        before = time.perf_counter()
        source.start()
        self.addCleanup(source.stop)
        snapshot = self._wait(source)
        self.assertGreaterEqual(snapshot.first_packet_at, before)
        self.assertEqual(snapshot.assembly_ms, 0.0)

    def test_an_unbelievable_present_time_is_ignored(self) -> None:
        """0 (还没出过帧)、在未来、或者一秒以前 —— 都说明拿到的不是这一帧的时间。
        照用的话延迟日志里会出现负数或者几十秒。"""
        for presented in (0.0, time.perf_counter() + 5.0, time.perf_counter() - 30.0):
            with self.subTest(presented=presented):
                camera = FakeCamera([_frame(3)], present_time=presented)
                source = _source(FakeFactory(camera))
                before = time.perf_counter()
                source.start()
                snapshot = self._wait(source)
                source.stop()
                self.assertGreaterEqual(snapshot.first_packet_at, before)

    def test_a_missing_dxcam_is_reported_not_raised(self) -> None:
        """没装 dxcam 不能让整个程序起不来。错误要说清楚装什么。"""
        with mock.patch.dict(sys.modules, {"dxcam": None}):
            source = DesktopSource(DesktopConfig(), retry_seconds=0.01)
            source.start()
            try:
                deadline = time.monotonic() + 2.0
                while source.error is None and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                source.stop()
        self.assertIn("dxcam", source.error)
        self.assertIn("pip install", source.error)

    def test_a_broken_camera_is_rebuilt(self) -> None:
        """锁屏、UAC、切分辨率都会让桌面复制失效。不重建的话画面永远停在那一刻。"""
        broken = FakeCamera([RuntimeError("access lost")])
        healthy = FakeCamera([_frame(9)])
        source = _source(FakeFactory(broken, healthy))
        source.start()
        self.addCleanup(source.stop)
        snapshot = self._wait(source)
        self.assertEqual(int(snapshot.frame[0, 0, 0]), 9)
        self.assertTrue(broken.released)
        self.assertIsNone(source.error)

    def test_a_region_bigger_than_the_screen_is_an_error_not_a_crash(self) -> None:
        source = _source(FakeFactory(FakeCamera(width=1280, height=720)), width=1920, height=1080)
        source.start()
        self.addCleanup(source.stop)
        deadline = time.monotonic() + 2.0
        while source.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("1280x720", source.error or "")

    def test_stop_releases_the_camera_and_ends_the_thread(self) -> None:
        """桌面复制是按进程占着的资源, 不释放的话同一个进程里下一次拿不到。"""
        camera = FakeCamera([_frame(1)])
        source = _source(FakeFactory(camera))
        source.start()
        self._wait(source)
        source.stop()
        self.assertTrue(camera.released)
        self.assertFalse(any(thread.name == "desktop-capture" for thread in threading.enumerate()))

    def test_it_knows_the_screen_size_once_it_has_a_camera(self) -> None:
        """连接测试要把显示器分辨率打出来, 用户靠它确认选中的是哪块屏。"""
        source = _source(FakeFactory(FakeCamera([_frame(1)], width=1920, height=1080)))
        self.assertIsNone(source.screen_size)
        source.start()
        self.addCleanup(source.stop)
        self._wait(source)
        self.assertEqual(source.screen_size, (1920, 1080))


class FakeComPointer:
    """记下有没有被手动 Release。真的 comtypes 指针被回收时会自己 Release 一次,
    所以手动再调一次就是两次。"""

    def __init__(self, log: list[str], name: str) -> None:
        self.log = log
        self.name = name

    def Release(self) -> int:  # noqa: N802 - COM 的方法名
        self.log.append(f"Release {self.name}")
        return 0


class _Recorder:
    """按顺序记下每个属性被设成了什么。"""

    def __init__(self, log: list[str], **values) -> None:
        object.__setattr__(self, "log", log)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value) -> None:
        self.log.append(f"{name}={value!r}")
        object.__setattr__(self, name, value)


class DxcamDoubleReleasePatchTest(unittest.TestCase):
    """DXcam 0.3 的两个 release 先手动 Release 再丢引用, comtypes 回收指针时还会
    再 Release 一次 —— 同一个 COM 对象释放两次, 进程 access violation。实测:
    第一次按区域 grab 就崩 (暂存面按区域尺寸重建), camera.release() 也崩。"""

    def test_the_staging_surface_drops_its_references_without_releasing_by_hand(self) -> None:
        from rhodes_fast.desktop_source import _stage_release

        log: list[str] = []
        surface = _Recorder(
            log, width=320, height=320,
            texture=FakeComPointer(log, "texture"), interface=FakeComPointer(log, "interface"),
        )
        _stage_release(surface)
        self.assertNotIn("Release texture", log)
        self.assertNotIn("Release interface", log)
        self.assertIsNone(surface.texture)
        self.assertIsNone(surface.interface)
        self.assertEqual((surface.width, surface.height), (0, 0))
        # QueryInterface 拿到的那个引用先丢, texture 后丢: 反过来的话 texture 那一下
        # 可能已经把对象释放掉了, interface 被回收时再 Release 就是对着一块已释放的内存。
        self.assertLess(log.index("interface=None"), log.index("texture=None"))

    def test_an_already_released_surface_is_left_alone(self) -> None:
        from rhodes_fast.desktop_source import _stage_release

        log: list[str] = []
        surface = _Recorder(log, width=0, height=0, texture=None, interface=None)
        _stage_release(surface)
        self.assertEqual(log, [])

    def test_the_duplicator_drops_its_reference_without_releasing_by_hand(self) -> None:
        from rhodes_fast.desktop_source import _duplicator_release

        log: list[str] = []
        duplicator = _Recorder(log, duplicator=FakeComPointer(log, "duplicator"))
        object.__setattr__(duplicator, "release_frame", lambda: log.append("release_frame"))
        _duplicator_release(duplicator)
        self.assertNotIn("Release duplicator", log)
        self.assertIsNone(duplicator.duplicator)
        # 手上还攥着一帧的话要先还给系统, 跟原来的 release 一样。
        self.assertLess(log.index("release_frame"), log.index("duplicator=None"))

    def test_only_the_pinned_version_is_patched(self) -> None:
        """修过的版本不该被我们的补丁盖掉 —— 那样上游修好之后我们反而在跑旧逻辑。"""
        from rhodes_fast.desktop_source import _duplicator_release, _patch_dxcam, _stage_release

        for version, expected in (("0.3.0", True), ("0.3.7", True), ("0.4.0", False), ("0.2.0", False)):
            with self.subTest(version=version):
                stage = type("StageSurface", (), {"release": lambda self: "original"})
                duplicator = type("DXGIDuplicator", (), {"release": lambda self: "original"})
                self.assertIs(_patch_dxcam(version, stage, duplicator), expected)
                self.assertIs(stage.release is _stage_release, expected)
                self.assertIs(duplicator.release is _duplicator_release, expected)


if __name__ == "__main__":
    unittest.main()
