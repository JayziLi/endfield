import unittest

from rhodes_fast.gui_core.prompts import RecordingPrompter


class RecordingPrompterTest(unittest.TestCase):
    def test_records_errors_and_infos(self) -> None:
        prompter = RecordingPrompter()
        prompter.notify_error("设置无法保存", "端口不是数字")
        prompter.notify_info("完成", "设置已保存。")
        self.assertEqual(prompter.errors, [("设置无法保存", "端口不是数字")])
        self.assertEqual(prompter.infos, [("完成", "设置已保存。")])

    def test_confirm_defaults_to_false_when_no_answer_queued(self) -> None:
        """没排答案就当用户点了取消。这样忘记 mock 的测试会走「不做」那条路,
        而不是悄悄把删除操作跑完。"""
        prompter = RecordingPrompter()
        self.assertFalse(prompter.confirm("删除预设", "确定删除「默认」？"))

    def test_confirm_consumes_queued_answers_in_order(self) -> None:
        prompter = RecordingPrompter(answers=[True, False])
        self.assertTrue(prompter.confirm("一", "第一次"))
        self.assertFalse(prompter.confirm("二", "第二次"))
        self.assertFalse(prompter.confirm("三", "队列空了"))
        self.assertEqual([title for title, _ in prompter.asked], ["一", "二", "三"])

    def test_confirm_records_which_prompts_were_dangerous(self) -> None:
        """破坏性确认要能跟普通确认区分开 —— tkinter 那边靠它补 icon=WARNING
        和 default=CANCEL, 少传一个 danger 就是可见的安全性回退。"""
        prompter = RecordingPrompter(answers=[True, True])
        prompter.confirm("覆盖预设", "要用现在的设置覆盖它吗？", danger=True)
        prompter.confirm("重新探测", "重新读一次模型？")
        self.assertEqual(prompter.dangerous, ["覆盖预设"])

    def test_warnings_are_recorded_separately_from_errors(self) -> None:
        prompter = RecordingPrompter()
        prompter.notify_warning("触发键冲突", "两套方案不能用同一个键。")
        self.assertEqual(prompter.warnings, [("触发键冲突", "两套方案不能用同一个键。")])
        self.assertEqual(prompter.errors, [])

    def test_confirm_three_way_returns_none_for_cancel(self) -> None:
        prompter = RecordingPrompter(answers=[None])
        self.assertIsNone(prompter.confirm_three_way("保存", "先保存当前预设？"))

    def test_ask_text_returns_none_when_queue_empty(self) -> None:
        prompter = RecordingPrompter()
        self.assertIsNone(prompter.ask_text("重命名", "新名字", initial="旧名字"))


class ImportPurityTest(unittest.TestCase):
    def test_gui_core_does_not_import_tkinter(self) -> None:
        """gui_core 不许碰 tkinter —— WebView 界面要在没有 tk 的环境里 import 它。"""
        import subprocess
        import sys
        from pathlib import Path

        # 显式给 cwd: rhodes_fast 不是 pip 装的, 子进程要从仓库根目录才 import 得到。
        # 不给的话换个目录跑测试会得到一个跟 tkinter 毫无关系的 ModuleNotFoundError。
        repo_root = Path(__file__).resolve().parent.parent
        code = (
            "import sys; import rhodes_fast.gui_core; "
            "leaked = sorted(n for n in sys.modules if n == 'tkinter' or n.startswith('tkinter.')); "
            "assert not leaked, leaked"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
