"""「问用户」的回调协议。

业务逻辑不该知道自己跑在 tkinter 还是浏览器里, 所以它不直接调 messagebox,
而是调这里的 Prompter。tkinter 界面用 messagebox 实现它, WebView 界面用前端
弹窗实现它, 测试用 RecordingPrompter 实现它。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Prompter(Protocol):
    def notify_error(self, title: str, message: str) -> None:
        """报错。对应 messagebox.showerror。"""

    def notify_warning(self, title: str, message: str) -> None:
        """警告。对应 messagebox.showwarning。

        只有一个调用点 (触发键冲突), 但折进 notify_error 或 notify_info 图标就变了,
        而这个计划的验收标准是零可见变化。
        """

    def notify_info(self, title: str, message: str) -> None:
        """告知。对应 messagebox.showinfo。"""

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        """确认。对应 messagebox.askokcancel —— 默认是「不做」。

        danger=True 表示这是破坏性操作: tkinter 那边补上 icon=WARNING 和
        default=CANCEL (手滑按回车删不掉, 见 gui.py:1244 的注释),
        WebView 那边给红色按钮 + 焦点落在取消。
        """

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        """三态确认。对应 messagebox.askyesnocancel: True 是, False 否, None 取消。"""

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        """要一段文字。对应 simpledialog.askstring, 取消返回 None。"""


class RecordingPrompter:
    """测试替身: 记下问过什么, 按队列回答。

    队列空时一律答「不做」(confirm → False, ask_text → None)。忘记排答案的测试
    会走保守分支, 不会把删除操作真跑完。
    """

    def __init__(self, answers: list[object] | None = None) -> None:
        self.errors: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []
        self.asked: list[tuple[str, str]] = []
        self.dangerous: list[str] = []      # 标了 danger=True 的确认框标题
        self._answers = list(answers or [])

    def _next(self, default: object) -> object:
        if not self._answers:
            return default
        return self._answers.pop(0)

    def notify_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def notify_warning(self, title: str, message: str) -> None:
        self.warnings.append((title, message))

    def notify_info(self, title: str, message: str) -> None:
        self.infos.append((title, message))

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        self.asked.append((title, message))
        if danger:
            self.dangerous.append(title)
        return bool(self._next(False))

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        self.asked.append((title, message))
        answer = self._next(None)
        return None if answer is None else bool(answer)

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        self.asked.append((title, message))
        answer = self._next(None)
        return None if answer is None else str(answer)
