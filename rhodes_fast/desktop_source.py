"""本机屏幕采集: 单机模式的画面来源。

用 DXcam 抓所选显示器正中央的 width x height。DXcam 同时提供 Windows 自带的两种
采集接口: dxgi (桌面复制) 和 winrt (Windows.Graphics.Capture)。两种抓的都是
合成之后的整块屏幕, 所以本程序自己的窗口挡在正中央的话会被一起抓进去。

跟 UdpSource 一样只留最新一帧, 不排队。

dxcam 延迟导入: 它是可选依赖 (pip install -e .[local]), 只用副机的人不装。
没装的时候错误写进 error, 不让整个程序起不来。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from .config import DesktopConfig
from .obs_source import FrameSnapshot

BACKEND_NAMES = {"dxgi": "DXGI", "winrt": "WGC"}

# DXGI 那条路的 grab 不等新帧: 画面没变就立刻返回 None。两次尝试之间歇这么久。
# Python 3.11 起 Windows 上的 time.sleep 用高精度定时器, 0.5ms 是真的 0.5ms;
# 歇得更久的话, 画面出来到被抓到平均要多等半个间隔。WGC 那条路 grab 自己会等
# 帧到达, 用不着这一下, 但歇一下也无害。
_POLL_SECONDS = 0.0005
# DXGI 报的出帧时间比现在早这么多以上, 就不是这一帧的时间了。
_PRESENT_TIME_MAX_AGE = 1.0

_INSTALL_HINT = "本机屏幕采集需要 dxcam：pip install -e .[local]"


def capture_region(screen_width: int, screen_height: int, width: int, height: int) -> tuple[int, int, int, int]:
    """屏幕正中央 width x height 那一块, (left, top, right, bottom)。

    比屏幕还大就报错, 不裁剪凑数: 裁了的话送进模型的画面尺寸就跟设置里写的
    不一样了。
    """
    if width > screen_width or height > screen_height:
        raise ValueError(
            f"采集尺寸 {width}x{height} 比显示器 {screen_width}x{screen_height} 还大"
        )
    left = (screen_width - width) // 2
    top = (screen_height - height) // 2
    return (left, top, left + width, top + height)


# ---- DXcam 0.3 的双重释放 ----
#
# StageSurface.release 和 DXGIDuplicator.release 先手动 texture.Release() /
# duplicator.Release(), 再把指针设成 None —— 而 comtypes 的指针被回收时自己还会
# Release 一次。同一个 COM 对象被释放两次, 进程当场 access violation, 连 Python
# 的异常都来不及抛。实测两处都会走到: 按区域抓的第一次 grab (暂存面按区域尺寸
# 重建, 先 release 旧的) 和 camera.release()。两个后端都中, 因为暂存面是共用的。
# 上游 main 分支截至 2026-09 仍是这样。
#
# 修法是只丢引用、不手动 Release, 让 comtypes 各释放一次。只给钉住的 0.3.x 打:
# 上游修好之后不该被我们盖回去。
_DXCAM_BROKEN_VERSIONS = ("0.3.",)


def _stage_release(self) -> None:
    if self.texture is not None:
        self.width = 0
        self.height = 0
        # 先丢 QueryInterface 拿到的那个引用, 再丢 texture。反过来的话 texture 那
        # 一下可能已经把对象释放掉了, interface 被回收时再 Release 就是对着一块
        # 已释放的内存。
        self.interface = None
        self.texture = None


def _duplicator_release(self) -> None:
    if self.duplicator is not None:
        self.release_frame()
        self.duplicator = None


def _patch_dxcam(version: str, stage_surface_type: type, duplicator_type: type) -> bool:
    if not version.startswith(_DXCAM_BROKEN_VERSIONS):
        return False
    stage_surface_type.release = _stage_release
    duplicator_type.release = _duplicator_release
    return True


def _create_dxcam(**kwargs):
    try:
        import dxcam
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc
    try:
        from importlib.metadata import version

        from dxcam.core.dxgi_duplicator import DXGIDuplicator
        from dxcam.core.stagesurf import StageSurface
    except ImportError:
        pass  # 内部结构变了, 说明不是我们钉住的那个版本, 也就不需要补丁
    else:
        _patch_dxcam(version("dxcam"), StageSurface, DXGIDuplicator)
    return dxcam.create(**kwargs)


def _present_time(camera, grabbed_at: float) -> float | None:
    """这一帧在屏幕上出现的时刻, 拿不到或者不可信就是 None。

    DXGI 的 LastPresentTime 是 QPC 计数, 跟 perf_counter 同一个时钟。DXcam 只在
    start() 那套连续采集里公开它; 我们用的是 grab, 只能读它内部的 _duplicator。
    内部属性随时可能变, 所以每一步都容错, 读不到就退回用我们自己计的时间。
    """
    value = getattr(getattr(camera, "_duplicator", None), "latest_frame_time", None)
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    if value > grabbed_at or grabbed_at - value > _PRESENT_TIME_MAX_AGE:
        return None
    return float(value)


class DesktopSource:
    def __init__(
        self,
        config: DesktopConfig,
        *,
        create_camera: Callable | None = None,
        retry_seconds: float = 0.5,
    ) -> None:
        self.config = config
        self._create_camera = create_camera or _create_dxcam
        self._retry_seconds = retry_seconds
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: FrameSnapshot | None = None
        self._error: str | None = None
        self._frames = 0
        self._started_at = 0.0
        self._screen_size: tuple[int, int] | None = None

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def fps(self) -> float:
        elapsed = time.perf_counter() - self._started_at
        return self._frames / elapsed if elapsed > 0 else 0.0

    @property
    def label(self) -> str:
        backend = BACKEND_NAMES.get(self.config.backend, self.config.backend)
        return f"本机屏幕 {backend} · 显示器 {self.config.monitor} · {self.config.width}x{self.config.height}"

    @property
    def screen_size(self) -> tuple[int, int] | None:
        """所选显示器的分辨率 (物理像素)。拿到 camera 之前是 None。"""
        with self._condition:
            return self._screen_size

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name="desktop-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def wait_next(self, after_sequence: int, timeout: float = 3.0) -> FrameSnapshot | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._stop.is_set():
                if self._latest is not None and self._latest.sequence > after_sequence:
                    return self._latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            camera = None
            try:
                camera = self._create_camera(
                    output_idx=self.config.monitor,
                    output_color="BGR",
                    backend=self.config.backend,
                )
                with self._condition:
                    self._screen_size = (int(camera.width), int(camera.height))
                region = capture_region(camera.width, camera.height, self.config.width, self.config.height)
                self._set_error(None)
                self._capture(camera, region)
            except Exception as exc:  # noqa: BLE001 - 任何失败都重建 camera 再试
                if not self._stop.is_set():
                    self._set_error(str(exc) or type(exc).__name__)
                    self._stop.wait(self._retry_seconds)
            finally:
                if camera is not None:
                    try:
                        camera.release()
                    except Exception:  # noqa: BLE001 - 收尾路径上, 释放失败不能拦住重建
                        pass

    def _capture(self, camera, region: tuple[int, int, int, int]) -> None:
        # 出帧时间只信 DXGI 的。实测 WGC 那条路 135 帧里只有 27 帧过得了合理性
        # 检查, 过了的里面还有一帧老到 65ms —— 它给的不一定是这一帧的时间。
        trust_present_time = self.config.backend == "dxgi"
        while not self._stop.is_set():
            started = time.perf_counter()
            frame = camera.grab(region=region, new_frame_only=True)
            if frame is None:
                time.sleep(_POLL_SECONDS)
                continue
            presented = _present_time(camera, started) if trust_present_time else None
            self._publish(np.ascontiguousarray(frame), started, presented)

    def _publish(self, frame: np.ndarray, started: float, presented: float | None) -> None:
        """两段分开记, 跟 UdpSource 同一个意思: assembly_ms 是「画面已经出来了、
        还没开始抓」等掉的时间 (UDP 那边是等分片到齐), decode_ms 是抓这一下本身
        (UDP 那边是解码)。拿不到出帧时间时第一段记 0。"""
        ready_at = time.perf_counter()
        first_packet_at = presented if presented is not None else started
        with self._condition:
            sequence = 1 if self._latest is None else self._latest.sequence + 1
            self._latest = FrameSnapshot(
                sequence,
                first_packet_at,
                ready_at,
                frame,
                assembly_ms=(started - first_packet_at) * 1000.0,
                decode_ms=(ready_at - started) * 1000.0,
            )
            self._frames += 1
            self._error = None
            self._condition.notify_all()

    def _set_error(self, error: str | None) -> None:
        with self._condition:
            self._error = error
            self._condition.notify_all()
