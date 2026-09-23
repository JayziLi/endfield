from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import Mock

from rhodes_fast.gui_core.prompts import Prompter
from rhodes_fast.gui_web.prompts import ModalPrompter


def _payload_of(window: Mock) -> dict:
    """从 evaluate_js 的脚本里把 showModal 的参数抠回来。

    断言「推过去的是什么」必须看真实的负载, 不是看脚本里有没有某个子串 ——
    子串断言在参数被拼错时照样绿。
    """
    script = window.evaluate_js.call_args.args[0]
    return json.loads(script[script.index("(") + 1 : script.rindex(")")])


class ProtocolTest(unittest.TestCase):
    def test_it_satisfies_the_prompter_protocol(self) -> None:
        """业务侧 (GuiSession) 只认这个协议。少一个方法的话, 症状是某个流程走到
        一半 AttributeError, 而那是从 JS 调过来的 —— 异常消失在 pywebview 里,
        界面什么都不说。"""
        self.assertIsInstance(ModalPrompter(), Prompter)


class NotifyTest(unittest.TestCase):
    """三个 notify 不阻塞。

    阻塞它们的话, 万一哪天从读取线程调一次 (日志那条路就在那条线程上), 整个
    运行状态就停了, 而且看起来像管线挂了。
    """

    def _prompter(self):
        window = Mock()
        pushed: list[str] = []
        return ModalPrompter(window=window, push=pushed.append), window, pushed

    def test_notifications_return_immediately(self) -> None:
        prompter, _window, _pushed = self._prompter()
        started = time.perf_counter()
        prompter.notify_error("标题", "正文")
        prompter.notify_warning("标题", "正文")
        prompter.notify_info("标题", "正文")
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_a_notification_reaches_the_page_and_the_log(self) -> None:
        prompter, window, pushed = self._prompter()
        prompter.notify_error("载入预设失败", "文件读不了")
        self.assertEqual(_payload_of(window)["title"], "载入预设失败")
        self.assertTrue(
            any("载入预设失败" in line for line in pushed),
            "弹窗关掉之后就什么都不剩了, 运行状态里得留一行",
        )

    def test_the_three_kinds_are_told_apart(self) -> None:
        """错误和提示长一个样的话, 一句「已保存」和一句「保存失败」就没区别了。"""
        prompter, window, _pushed = self._prompter()
        kinds = []
        for notify in (prompter.notify_error, prompter.notify_warning, prompter.notify_info):
            notify("标题", "正文")
            kinds.append(_payload_of(window)["kind"])
        self.assertEqual(len(set(kinds)), 3)

    def test_the_payload_is_json_not_string_concatenation(self) -> None:
        """标题和正文里有中文、引号、换行和 Windows 路径的反斜杠。直接拼进 JS
        字符串, 一个带引号的模型路径就能把脚本当场截断。"""
        prompter, window, _pushed = self._prompter()
        message = '找不到 "C:\\MODEL\\a.onnx"\n换行也要挺住'
        prompter.notify_error("模型", message)
        self.assertEqual(_payload_of(window)["message"], message)


class BlockingTest(unittest.TestCase):
    """confirm / confirm_three_way / ask_text 阻塞到 JS 回答为止。

    这条桥成立的前提是实测过的: pywebview 把每个 JS->Python 的 api 调用放在
    自己的线程上, 阻塞其中一个不会冻住 JS, 而且阻塞期间第二个调用照样被处理 ——
    所以 answer() 进得来。
    """

    def _answer_from_another_thread(self, prompter, value, delay=0.05):
        def reply() -> None:
            time.sleep(delay)
            token = prompter._pending_tokens()[0]
            prompter.answer(token, value)

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)

    def test_confirm_returns_what_the_page_answered(self) -> None:
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, True)
        self.assertIs(prompter.confirm("删除预设", "确定吗"), True)

    def test_confirm_returns_false_when_the_page_says_no(self) -> None:
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, False)
        self.assertIs(prompter.confirm("删除预设", "确定吗"), False)

    def test_three_way_can_come_back_as_cancel(self) -> None:
        """askyesnocancel 的第三态。把它折成 False 的话「取消」会当成「否」执行
        下去 —— 而调用点正是「要不要先保存」这类问题。"""
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, None)
        self.assertIsNone(prompter.confirm_three_way("未保存", "先保存吗"))

    def test_ask_text_comes_back_as_a_string(self) -> None:
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, "我的预设")
        self.assertEqual(prompter.ask_text("另存为", "名字"), "我的预设")

    def test_ask_text_carries_the_initial_value(self) -> None:
        """改名那条路要把现在的名字填进去, 不然用户每次都得重打一遍。"""
        window = Mock()
        prompter = ModalPrompter(window=window)
        self._answer_from_another_thread(prompter, None)
        prompter.ask_text("重命名", "新名字", initial="我的算法")
        self.assertEqual(_payload_of(window)["initial"], "我的算法")

    def test_danger_is_carried_into_the_payload(self) -> None:
        """破坏性操作要红按钮 + 焦点落在取消。gui.py:1244 的注释:
        「默认按钮是取消: 手滑按回车删不掉」。"""
        window = Mock()
        prompter = ModalPrompter(window=window)
        self._answer_from_another_thread(prompter, False)
        prompter.confirm("删除算法", "删了找不回来", danger=True)
        self.assertIs(_payload_of(window)["danger"], True)

    def test_two_questions_at_once_do_not_cross_their_answers(self) -> None:
        """每个弹窗带自己的 token。不带的话, 回答会串到另一个问题上 —— 而那两个
        问题很可能一个是「要保存吗」一个是「确定删除吗」。"""
        prompter = ModalPrompter(window=Mock(), timeout=5)
        answers: dict[str, object] = {}

        def ask(key: str) -> None:
            answers[key] = prompter.confirm(key, "?")

        threads = [threading.Thread(target=ask, args=(key,), daemon=True) for key in ("甲", "乙")]
        for thread in threads:
            thread.start()
        while len(prompter._pending_tokens()) < 2:
            time.sleep(0.01)
        first, second = prompter._pending_tokens()
        prompter.answer(second, True)
        prompter.answer(first, False)
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(sorted(answers.values(), key=str), [False, True])


class SafetyNetTest(unittest.TestCase):
    def test_it_gives_up_after_the_timeout_instead_of_hanging_forever(self) -> None:
        """窗口在问题挂着的时候没了 (Alt+F4、崩溃), 那条线程会永远等下去。
        超时之后按保守答案放行。"""
        prompter = ModalPrompter(window=Mock(), timeout=0.2)
        started = time.perf_counter()
        self.assertIs(prompter.confirm("删除", "确定吗"), False)
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_cancel_all_releases_everyone_at_once(self) -> None:
        """关窗时叫。不叫的话每个挂着的问题都要等满超时, 而进程正在等它们。"""
        prompter = ModalPrompter(window=Mock(), timeout=30)
        answers: list[object] = []
        thread = threading.Thread(
            target=lambda: answers.append(prompter.confirm("a", "b")), daemon=True
        )
        thread.start()
        while not prompter._pending_tokens():
            time.sleep(0.01)
        prompter.cancel_all()
        thread.join(timeout=2)
        self.assertEqual(answers, [False])

    def test_answering_an_unknown_token_is_ignored(self) -> None:
        """JS 那边可能在超时之后才回答 (用户去倒了杯水)。那时 token 已经不在了,
        这一下必须是空操作 —— 抛异常的话它会浮到 JS 的 promise 上, 页面上多一条
        看不懂的报错。"""
        ModalPrompter(window=Mock()).answer("根本不存在的 token", True)

    def test_a_prompt_without_a_window_answers_conservatively(self) -> None:
        """窗口还没建出来就问了 (启动时载入预设那条路)。没地方画弹窗, 只能按
        「不做」放行 —— 而且绝不能卡住。"""
        prompter = ModalPrompter()
        started = time.perf_counter()
        self.assertIs(prompter.confirm("删除", "确定吗"), False)
        self.assertIsNone(prompter.ask_text("另存为", "名字"))
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_attach_lets_a_late_window_take_over(self) -> None:
        """构造和建窗是两步: GuiSession 要 prompter, 而 prompter 要 window,
        谁都不能先有对方。"""
        window = Mock()
        prompter = ModalPrompter()
        prompter.attach(window)
        prompter.notify_info("标题", "正文")
        self.assertEqual(_payload_of(window)["title"], "标题")


class ShowSourceTest(unittest.TestCase):
    """导入别人写的 .py 之前先看清楚它是什么 —— 那一眼是这条流程的全部理由。"""

    def test_it_blocks_until_the_page_closes_it(self) -> None:
        """GuiSession._confirm_import 在两次确认之间同步调它。不阻塞的话源码框
        会被紧接着弹出的确认框顶掉, 用户根本来不及看。"""
        prompter = ModalPrompter(window=Mock(), timeout=5)
        done: list[str] = []

        def show() -> None:
            prompter.show_source("my_aim.py", "class Mine:\n    pass\n")
            done.append("回来了")

        thread = threading.Thread(target=show, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        while not prompter._pending_tokens():
            time.sleep(0.01)
        self.assertEqual(done, [], "还没关就返回了")
        prompter.answer(prompter._pending_tokens()[0], None)
        thread.join(timeout=2)
        self.assertEqual(done, ["回来了"])

    def test_the_source_goes_over_verbatim(self) -> None:
        """源码里有引号、反斜杠、缩进和换行。少一个字符, 用户看到的就不是他将要
        执行的那份代码 —— 而这个框的意义全在于此。"""
        window = Mock()
        prompter = ModalPrompter(window=window, timeout=0.2)
        source = 'class Mine:\n    PATH = "C:\\MODEL\\a.onnx"\n    # 注释\n'
        prompter.show_source("my_aim.py", source)
        self.assertEqual(_payload_of(window)["message"], source)
        self.assertEqual(_payload_of(window)["kind"], "source")


if __name__ == "__main__":
    unittest.main()
