"""主窗口之外的两种独立窗口: 看源码的, 和放大看预览的。

两种都是带系统边框的普通窗口, 不学主窗口做无边框。主窗口无边框是设计系统的
要求 (它要自己画标题栏和状态栏), 代价是拖动、缩放、贴边分屏全要自己接, 而且
Aero Snap 至今接不上。这两个是工具窗口, 用户要的正是「能随便拖大、能最大化、能
扔到另一块屏上」—— 系统边框把这些白给了。

create_window 由调用方传进来 (生产里是 webview.create_window), 这个模块自己不
import webview: pywebview 是可选依赖, 而这里的逻辑 (开几个、什么时候开、关了之后
谁收到) 要能在没装它的机器上测。
"""

from __future__ import annotations

import threading
from typing import Callable


class SourcePage:
    """源码窗口的 js_api。

    **只有两个公开方法**, 这是故意的: pywebview 把 js_api 的每个公开方法都挂到
    页面上, 而这个页面显示的是一份别人写的、还没审过的代码。页面只需要「把我要
    显示的东西给我」和「关掉我」。

    源码由页面自己来拉, 而不是 Python 在窗口建出来之后往里推: 推的话要赌 loaded
    事件还没触发 —— create_window 是异步的, 窗口什么时候加载完不归这边管。拉的话
    页面等 pywebviewready 再调, 先后顺序由页面自己保证。
    """

    def __init__(self, title: str, body: str) -> None:
        self._title = title
        self._body = body
        self._window = None

    def _attach(self, window) -> None:
        self._window = window

    def get_payload(self) -> dict[str, str]:
        return {"title": self._title, "body": self._body}

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()


class Popups:
    """管着那两种窗口。

    on_change 在预览窗口的「有没有人在看」变了的时候调用 —— Api 靠它去翻管线的
    预览开关。开着、关掉、最小化、还原, 四种都要报: 漏一次, 大窗口就会停在最后
    一帧上, 而且看起来跟「管线没发帧」一模一样。
    """

    SOURCE_SIZE = (960, 720)
    SOURCE_MIN = (480, 360)
    # 预览窗口的内容区边长。采集画面是 320x320, 放大两倍看得清检测框又不至于
    # 占满屏: 这台机器缩放 150%, 逻辑高度只有 960。
    PREVIEW_SIDE = 640
    PREVIEW_MIN = 320
    # 带系统边框的窗口, 给 create_window 的 width/height 是整个外框, 不是内容区。
    # 实测 (Windows 11, 缩放 150%, 逻辑像素): 左右各一条 8px 的隐形缩放边框,
    # 上面标题栏加下面边框一共 39px。第一版只补了标题栏的 32px, 内容区量出来是
    # 624x633 —— 不是方的。这几个值是系统按 DPI 缩放过的逻辑尺寸, 换缩放比例
    # 基本不变。
    FRAME_EXTRA_WIDTH = 16
    FRAME_EXTRA_HEIGHT = 39

    def __init__(
        self,
        create_window: Callable,
        base_url: str,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._create_window = create_window
        self._base_url = base_url.rstrip("/")
        self._on_change = on_change
        self._lock = threading.Lock()
        self._preview = None
        self._preview_minimized = False
        # 还开着的源码窗口。关主窗口时要一起关掉 —— 不然调用 show_source 的那条
        # 线程会一直等下去, 而进程正在等它。
        self._sources: set = set()

    # ---- 源码 ----

    def show_source(self, title: str, body: str) -> None:
        """开一个源码窗口, 阻塞到它被关掉为止。

        必须阻塞: GuiSession._confirm_import 在两次确认之间同步调它。不阻塞的话
        源码窗口刚开出来, 紧接着的「确定导入吗」就弹了 —— 而「导入前先看清楚这份
        .py 是什么」正是那条流程存在的全部理由。
        """
        page = SourcePage(title, body)
        closed = threading.Event()
        width, height = self.SOURCE_SIZE
        window = self._create_window(
            f"源码 · {title}",
            url=f"{self._base_url}/source.html",
            js_api=page,
            width=width,
            height=height,
            min_size=self.SOURCE_MIN,
            resizable=True,
            # pywebview 默认禁止选中文字。看源码的窗口里选不中代码, 用户没法把
            # 可疑的那一段复制出去问人。
            text_select=True,
            background_color="#F1F1EB",
        )
        page._attach(window)
        window.events.closed += closed.set
        with self._lock:
            self._sources.add(window)
        try:
            closed.wait()
        finally:
            with self._lock:
                self._sources.discard(window)

    # ---- 预览 ----

    @property
    def preview_active(self) -> bool:
        """有没有人在看放大的预览。开着且没最小化才算。"""
        with self._lock:
            return self._preview is not None and not self._preview_minimized

    def open_preview(self) -> None:
        """开放大预览。已经开着就把它叫回前面, 不开第二个。

        开两个的话两个窗口各拉一条流, 而且关掉一个另一个还在 —— 用户会以为关
        不掉。
        """
        with self._lock:
            existing = self._preview
        if existing is not None:
            existing.restore()
            existing.show()
            return
        side = self.PREVIEW_SIDE
        window = self._create_window(
            "实时预览",
            url=f"{self._base_url}/popout.html",
            width=side + self.FRAME_EXTRA_WIDTH,
            height=side + self.FRAME_EXTRA_HEIGHT,
            min_size=(
                self.PREVIEW_MIN + self.FRAME_EXTRA_WIDTH,
                self.PREVIEW_MIN + self.FRAME_EXTRA_HEIGHT,
            ),
            resizable=True,
            # 页面加载出来之前这块是系统画的, 默认纯白 —— 在一个黑底的视频窗口
            # 上会闪一下白。
            background_color="#101110",
        )
        with self._lock:
            self._preview = window
            self._preview_minimized = False
        window.events.closed += self._on_preview_closed
        window.events.minimized += self._on_preview_minimized
        window.events.restored += self._on_preview_restored
        self._announce()

    def _on_preview_closed(self) -> None:
        with self._lock:
            self._preview = None
            self._preview_minimized = False
        self._announce()

    def _on_preview_minimized(self) -> None:
        with self._lock:
            self._preview_minimized = True
        self._announce()

    def _on_preview_restored(self) -> None:
        with self._lock:
            self._preview_minimized = False
        self._announce()

    def _announce(self) -> None:
        if self._on_change is not None:
            self._on_change()

    # ---- 收尾 ----

    def close_all(self) -> None:
        """主窗口关掉时叫。留下的窗口没意义, 而且各自挂着东西: 预览窗口挂着一条
        流, 源码窗口挂着一条在等它的线程。"""
        with self._lock:
            windows = list(self._sources)
            if self._preview is not None:
                windows.append(self._preview)
        for window in windows:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 - 收尾路径上, 一个关不掉不能拦住别的
                pass
