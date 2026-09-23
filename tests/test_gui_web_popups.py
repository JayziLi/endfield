"""两种独立窗口: 看源码的, 和放大看预览的。

都用假的 create_window: 这里要钉的是「开几个、什么时候开、关了之后谁收到」,
真开一个 WebView2 窗口验不出这些, 只会让测试变慢变飘。窗口本身在真机上另验。
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock

from rhodes_fast.gui_web.popups import Popups, SourcePage


class FakeEvent:
    """pywebview 的 Event 用 += 挂回调。只模仿用到的那一点。"""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in list(self.handlers):
            handler()


class FakeWindow:
    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs
        self.events = Mock()
        self.events.closed = FakeEvent()
        self.events.minimized = FakeEvent()
        self.events.restored = FakeEvent()
        self.destroyed = False
        self.restored = 0

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self.events.closed.fire()

    def restore(self) -> None:
        self.restored += 1

    def show(self) -> None:
        pass


class FakeFactory:
    def __init__(self) -> None:
        self.windows: list[FakeWindow] = []

    def __call__(self, title, url=None, **kwargs) -> FakeWindow:
        window = FakeWindow(dict(kwargs, title=title, url=url))
        self.windows.append(window)
        return window


def _popups(on_change=None):
    factory = FakeFactory()
    return Popups(factory, "http://127.0.0.1:9999", on_change=on_change), factory


class SourceWindowTest(unittest.TestCase):
    """小屏下那个页面内的源码框太小: 最宽 560px, 没有高度上限也没有滚动, 源码
    一长下半截就切掉了。改成一个自己的窗口, 大小不受主窗口限制。"""

    def _show_in_background(self, popups, title="my_aim.py", body="class A:\n    pass\n"):
        done: list[str] = []

        def run() -> None:
            popups.show_source(title, body)
            done.append("回来了")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        return thread, done

    def _wait_for_window(self, factory) -> FakeWindow:
        for _ in range(200):
            if factory.windows:
                return factory.windows[-1]
            time.sleep(0.01)
        self.fail("窗口一直没开出来")

    def test_it_opens_a_window_of_its_own(self) -> None:
        popups, factory = _popups()
        thread, _done = self._show_in_background(popups)
        window = self._wait_for_window(factory)
        self.assertTrue(window.kwargs["url"].endswith("/source.html"))
        window.destroy()
        thread.join(2)

    def test_it_blocks_until_the_window_is_closed(self) -> None:
        """导入那条路在两次确认之间同步调它。不阻塞的话, 源码窗口刚开出来,
        紧接着的「确定导入吗」就弹了 —— 而「导入前先看清楚这份 .py 是什么」
        正是那条流程存在的全部理由。"""
        popups, factory = _popups()
        thread, done = self._show_in_background(popups)
        window = self._wait_for_window(factory)
        time.sleep(0.05)
        self.assertEqual(done, [], "窗口还开着就返回了")
        window.destroy()
        thread.join(2)
        self.assertEqual(done, ["回来了"])

    def test_it_is_big_and_resizable(self) -> None:
        """用户报的就是「太小」。能拖大、能最大化, 才算解决了。"""
        popups, factory = _popups()
        thread, _done = self._show_in_background(popups)
        window = self._wait_for_window(factory)
        self.assertGreaterEqual(window.kwargs["width"], 800)
        self.assertGreaterEqual(window.kwargs["height"], 600)
        self.assertIs(window.kwargs.get("resizable", True), True)
        window.destroy()
        thread.join(2)

    def test_the_code_can_be_selected_and_copied(self) -> None:
        """pywebview 默认禁止选中文字。看源码的窗口里选不中代码, 用户没法把可疑
        的那一段复制出去问人。"""
        popups, factory = _popups()
        thread, _done = self._show_in_background(popups)
        window = self._wait_for_window(factory)
        self.assertIs(window.kwargs["text_select"], True)
        window.destroy()
        thread.join(2)

    def test_the_page_pulls_exactly_what_was_passed(self) -> None:
        """源码是页面自己拉的 (等 pywebview 就绪再拉), 不是 Python 趁窗口刚建出来
        往里推 —— 推的话要赌 loaded 事件还没触发。少一个字符, 用户看到的就不是
        他将要执行的那份代码。"""
        popups, factory = _popups()
        body = 'class Mine:\n    PATH = "C:\\\\MODEL\\\\a.onnx"\n    # 注释 <script>\n'
        thread, _done = self._show_in_background(popups, "evil.py", body)
        window = self._wait_for_window(factory)
        page = window.kwargs["js_api"]
        self.assertEqual(page.get_payload(), {"title": "evil.py", "body": body})
        window.destroy()
        thread.join(2)

    def test_the_page_can_close_its_own_window(self) -> None:
        """页面里的「关闭」和 Esc 都走这一条。"""
        popups, factory = _popups()
        thread, done = self._show_in_background(popups)
        window = self._wait_for_window(factory)
        window.kwargs["js_api"].close()
        thread.join(2)
        self.assertEqual(done, ["回来了"])

    def test_closing_everything_releases_a_waiting_viewer(self) -> None:
        """主窗口关掉时源码窗口还开着的话, 调用方那条线程会一直等下去 —— 而进程
        正在等它。表现是「点了关闭, 窗口没了, 进程还在」。"""
        popups, factory = _popups()
        thread, done = self._show_in_background(popups)
        self._wait_for_window(factory)
        popups.close_all()
        thread.join(2)
        self.assertEqual(done, ["回来了"])


class SourcePageTest(unittest.TestCase):
    def test_only_the_intended_methods_are_visible_to_js(self) -> None:
        """pywebview 把 js_api 对象的每个公开方法都挂到页面上。多一个公开方法就
        是多一个页面能调的东西 —— 而这个页面显示的是一份别人写的、还没审过的
        代码。"""
        exposed = sorted(
            name for name in dir(SourcePage)
            if not name.startswith("_") and callable(getattr(SourcePage, name))
        )
        self.assertEqual(exposed, ["close", "get_payload"])


class PreviewPopoutTest(unittest.TestCase):
    """实时预览放大到一个独立的方形窗口。"""

    def test_it_opens_a_square_window(self) -> None:
        """采集画面是 320x320 的正方形, 窗口也做成正方形才铺得满。

        给出去的是外框尺寸, 要把系统边框扣掉之后内容区才是方的 —— 第一版只补了
        标题栏, 真机上量出来 624x633。
        """
        popups, factory = _popups()
        popups.open_preview()
        window = factory.windows[-1]
        self.assertTrue(window.kwargs["url"].endswith("/popout.html"))
        content_w = window.kwargs["width"] - Popups.FRAME_EXTRA_WIDTH
        content_h = window.kwargs["height"] - Popups.FRAME_EXTRA_HEIGHT
        self.assertEqual(content_w, content_h)
        self.assertGreater(content_w, 320)

    def test_a_second_click_brings_back_the_same_window(self) -> None:
        """点两下就开两个的话, 两个窗口各拉一条流, 而且关掉一个另一个还在 ——
        用户会以为关不掉。"""
        popups, factory = _popups()
        popups.open_preview()
        popups.open_preview()
        self.assertEqual(len(factory.windows), 1)
        self.assertEqual(factory.windows[0].restored, 1)

    def test_it_can_be_reopened_after_closing(self) -> None:
        popups, factory = _popups()
        popups.open_preview()
        factory.windows[0].destroy()
        popups.open_preview()
        self.assertEqual(len(factory.windows), 2)

    def test_it_reports_whether_anyone_is_watching(self) -> None:
        """管线只在有人看的时候才渲染和编码 JPEG。开着 -> 要; 关了 -> 不要;
        最小化 -> 不要 (浏览器最小化会节流, 旧帧积在内核缓冲里, 见 app.py 的
        _on_minimized)。"""
        popups, factory = _popups()
        self.assertFalse(popups.preview_active)
        popups.open_preview()
        self.assertTrue(popups.preview_active)
        factory.windows[0].events.minimized.fire()
        self.assertFalse(popups.preview_active)
        factory.windows[0].events.restored.fire()
        self.assertTrue(popups.preview_active)
        factory.windows[0].destroy()
        self.assertFalse(popups.preview_active)

    def test_every_change_is_announced(self) -> None:
        """Api 靠这个回调去翻预览开关。漏一次, 大窗口就会停在最后一帧上 ——
        而且看起来跟「管线没发帧」一模一样。"""
        changes: list[bool] = []
        popups, factory = _popups(on_change=lambda: changes.append(popups.preview_active))
        popups.open_preview()
        factory.windows[0].events.minimized.fire()
        factory.windows[0].events.restored.fire()
        factory.windows[0].destroy()
        self.assertEqual(changes, [True, False, True, False])

    def test_closing_everything_closes_it(self) -> None:
        """主窗口没了, 留一个孤零零的预览窗口是没意义的 —— 而且它还挂着一条流,
        server 那边的线程收不掉。"""
        popups, factory = _popups()
        popups.open_preview()
        popups.close_all()
        self.assertTrue(factory.windows[0].destroyed)
        self.assertFalse(popups.preview_active)

    def test_a_failure_to_open_is_contained(self) -> None:
        """这是从 JS 调过来的。开窗失败 (WebView2 抽风) 抛出去的话, 异常会浮到
        promise 上, 页面上多一条看不懂的报错 —— 而且 _preview 会停在一个半建的
        状态上, 之后再点永远被当成「已经开着」。"""
        popups = Popups(Mock(side_effect=RuntimeError("boom")), "http://x")
        with self.assertRaises(RuntimeError):
            popups.open_preview()
        self.assertFalse(popups.preview_active)


if __name__ == "__main__":
    unittest.main()
