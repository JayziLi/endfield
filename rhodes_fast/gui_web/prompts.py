"""「问用户」在 WebView 这边的实现: 页面内的模态框。

Prompter 的六个方法是同步的 (confirm 要 return bool), 而页面里的弹窗是异步的。
这中间的桥是「阻塞等 JS 回答」, 它成立的前提是实测过的:

    pywebview 把每个 JS->Python 的 api 调用放在自己的线程上 (实测线程名形如
    Thread-27 (_call))。阻塞其中一个不会冻住 JS —— 阻塞 1 秒期间页面的
    setInterval 照跑 59/60 次 —— 而且阻塞期间第二个 api 调用照样被处理,
    所以 answer() 进得来。

三条护栏, 少一条都会挂死:

1. 超时。窗口在问题挂着的时候没了 (Alt+F4、崩溃), 那条线程会永远等下去。
2. cancel_all()。关窗时叫一次, 不用等满超时。
3. notify 不阻塞。万一哪天从读取线程调一次 (日志那条路就在那条线程上),
   整个运行状态就停了, 而且看起来像管线挂了。
"""

from __future__ import annotations

import json
import threading
from typing import Any
from uuid import uuid4

# 问不出答案时给的答案。一律是「不做」—— 问出来的都是删除、覆盖这类事。
_CONSERVATIVE: dict[str, Any] = {
    "error": None,
    "warning": None,
    "info": None,
    "confirm": False,
    "three_way": None,
    "text": None,
    "source": None,
}


class ModalPrompter:
    """把 Prompter 的六个方法接到页面里的模态框上。"""

    def __init__(self, window=None, push=None, timeout: float = 300.0) -> None:
        self._window = window
        # 弹窗关掉之后就什么都不剩了, 所以每一句也往运行状态里写一行。
        self._push = push
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list]] = {}

    def attach(self, window) -> None:
        """窗口建出来之后回填。

        构造和建窗是两步: GuiSession 要 prompter, 而 prompter 要 window, 谁都
        不能先有对方。
        """
        self._window = window

    # ---- 给 Api 用, 不是 Prompter 的一部分 ----

    def answer(self, token: str, value: Any) -> None:
        """JS 回答了。token 不认识就是空操作。

        用户去倒了杯水、超时已经过去的情况下 JS 仍然会回答。抛异常的话它会浮到
        JS 的 promise 上, 页面上多一条看不懂的报错。
        """
        with self._lock:
            entry = self._pending.pop(token, None)
        if entry is None:
            return
        done, box = entry
        box.append(value)
        done.set()

    def cancel_all(self) -> None:
        """关窗时叫。把所有还挂着的问题立刻按取消放行。

        不叫的话每个挂着的问题都要等满超时 (默认五分钟), 而进程正在等它们。
        """
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for done, _box in pending:
            done.set()

    def _pending_tokens(self) -> list[str]:
        """测试用。下划线开头 —— 它不是 Prompter 的一部分。"""
        with self._lock:
            return list(self._pending)

    def show_source(self, title: str, body: str) -> None:
        """把一段源码显示出来, 等用户关掉再返回。

        不在 Prompter 协议里 —— 它是 GuiSession.import_algorithm 收的那个
        show_source 回调, 由界面提供。

        必须阻塞: _confirm_import 在两次确认之间同步调它 (「是」= 先看源码,
        看完再问一遍)。不阻塞的话源码框会被紧接着弹出的确认框顶掉, 用户根本
        来不及看 —— 而那一眼正是这条流程存在的全部理由: 导入别人写的 .py 之前
        先看清楚它是什么。
        """
        self._show("source", title, body, blocking=True)

    # ---- Prompter ----

    def notify_error(self, title: str, message: str) -> None:
        self._show("error", title, message, blocking=False)

    def notify_warning(self, title: str, message: str) -> None:
        self._show("warning", title, message, blocking=False)

    def notify_info(self, title: str, message: str) -> None:
        self._show("info", title, message, blocking=False)

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        answer = self._show("confirm", title, message, blocking=True, danger=danger)
        return bool(answer)

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        answer = self._show("three_way", title, message, blocking=True)
        return None if answer is None else bool(answer)

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        answer = self._show("text", title, message, blocking=True, initial=initial)
        return None if answer is None else str(answer)

    # ---- 内部 ----

    def _show(
        self,
        kind: str,
        title: str,
        message: str,
        *,
        blocking: bool,
        danger: bool = False,
        initial: str = "",
    ) -> Any:
        if self._push is not None:
            # 压成一行: 运行状态是一行一条的, 多行 message 会把它撑开。
            self._push(f"{title}：{' '.join(message.split())}")

        if self._window is None:
            # 窗口还没建出来就问了 (启动时载入预设那条路)。没地方画, 保守放行,
            # 而且绝不能卡住 —— 那时候阻塞就是一个永远起不来的界面。
            return _CONSERVATIVE[kind]

        token = uuid4().hex
        done: threading.Event | None = None
        box: list[Any] = []
        if blocking:
            done = threading.Event()
            # 挂进表要排在 evaluate_js 之前: JS 那边可能在下一行执行完之前就
            # 回答了 (pywebview 每个 api 调用一条线程), 那时 token 必须已经在。
            with self._lock:
                self._pending[token] = (done, box)

        spec = {
            "token": token,
            "kind": kind,
            "title": title,
            "message": message,
            "danger": danger,
            "initial": initial,
        }
        # json.dumps 不是洁癖 —— 标题和正文里有中文、引号、换行和 Windows 路径
        # 的反斜杠, 直接拼进 JS 字符串, 一个带引号的模型路径就能把脚本截断。
        self._window.evaluate_js(f"window.showModal({json.dumps(spec)})")

        if done is None:
            return None

        if not done.wait(self._timeout):
            with self._lock:
                self._pending.pop(token, None)
            return _CONSERVATIVE[kind]
        # cancel_all 走的是「置位但不填答案」, 所以空箱子等于取消。
        return box[0] if box else _CONSERVATIVE[kind]
