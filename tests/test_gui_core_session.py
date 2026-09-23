from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from rhodes_fast.aim_algorithms import available_algorithms, set_installed_algorithms
from rhodes_fast.config import AppConfig, default_config
from rhodes_fast.gui_core.prompts import RecordingPrompter
from rhodes_fast.gui_core.session import GuiSession

ALGORITHM_SOURCE = '''
__author__ = "阿明"

from rhodes_fast.aim_algorithms import Param


class MyAim:
    NAME = "my_aim"
    DISPLAY_NAME = "我的瞄准"
    PARAMS = (Param("gain", 1.0, 0.0, 3.0, "整体增益"),)

    def __init__(self, params):
        self.gain = params["gain"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.gain, observation.error_y * self.gain
'''

# 导入那个三态框问的是「要不要先看源码」, 不是「要不要导入」:
# 是 = 看源码 (看完再问一遍), 否 = 直接导入, 取消 = 放弃。
# 所以排在队列里让它装上的答案是 False, 不是 True。
INSTALL = False
SHOW_SOURCE = True


class PresetOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        models = self.root / "models"
        models.mkdir()
        self.model = models / "game.onnx"
        self.model.write_bytes(b"not really onnx")
        self.config_path = self.root / "settings.txt"
        self.prompter = RecordingPrompter()
        self.session = GuiSession(self.config_path, self.prompter)

    def make_config(self, **overrides) -> AppConfig:
        """造一份能存成预设的配置。

        模型路径必须真的存在: write_preset 拒绝存一份载入不了的预设, 拿
        default_config() 里那个 models/yolov5n.onnx 会直接撞在这条上。
        """
        config = default_config()
        config = replace(config, model=replace(config.model, path=self.model))
        return replace(config, **overrides) if overrides else config

    def new_session(self, answers: list[object] | None = None) -> GuiSession:
        """换一个 prompter 的新会话。删除要单独排答案, 复用 self.prompter 会把
        前面测试步骤问过的记录也带进来。"""
        self.prompter = RecordingPrompter(answers=answers)
        return GuiSession(self.config_path, self.prompter)

    # ---- 存 / 列 / 读 ----

    def test_store_then_list_then_load(self) -> None:
        self.assertTrue(self.session.store_preset("夜间", self.make_config()))
        self.assertIn("夜间", self.session.list_presets())
        self.assertIsNotNone(self.session.load_preset("夜间"))

    def test_list_presets_is_empty_before_anything_is_stored(self) -> None:
        self.assertEqual(self.session.list_presets(), [])

    def test_store_returns_the_name_it_actually_used(self) -> None:
        """界面要拿这个名字去更新「当前预设」, 所以返回的是存下来的那个,
        不是用户打进去的那个 —— write_preset 会去掉首尾空格。"""
        self.assertEqual(self.session.store_preset("  夜间  ", self.make_config()), "夜间")

    def test_storing_under_a_missing_model_reports_an_error(self) -> None:
        self.model.unlink()
        self.assertIsNone(self.session.store_preset("夜间", self.make_config()))
        self.assertEqual([title for title, _ in self.prompter.errors], ["预设无法保存"])
        self.assertEqual(self.session.list_presets(), [])

    def test_loading_a_missing_preset_reports_an_error(self) -> None:
        self.assertIsNone(self.session.load_preset("不存在"))
        self.assertEqual(len(self.prompter.errors), 1)
        self.assertEqual(self.prompter.errors[0][0], "载入预设失败")

    def test_loading_refuses_a_preset_that_needs_an_algorithm_you_do_not_have(self) -> None:
        """跟 _load_preset 一样带上 known_algorithms: 静默换成默认算法会让人
        以为在用这份预设, 实际手感完全是另一回事。"""
        self.session.store_preset("插件", self.make_config())
        path = self.root / "presets" / "插件.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["aim_profiles"][0]["algorithm"] = "someone_elses_plugin"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(self.session.load_preset("插件"))
        self.assertIn("someone_elses_plugin", self.prompter.errors[0][1])

    def test_loading_refuses_when_the_model_file_is_gone(self) -> None:
        self.session.store_preset("夜间", self.make_config())
        self.model.unlink()
        self.assertIsNone(self.session.load_preset("夜间"))
        self.assertEqual(self.prompter.errors[0][0], "载入预设失败")

    # ---- 开机恢复那条路 ----

    def test_the_baseline_read_ignores_a_missing_model(self) -> None:
        """开机恢复上次的预设只拿来比对有没有改动, 不往表单里填, 所以模型不在
        也照样读得出来 —— 这跟 load_preset 是两条路, 别合并。"""
        self.session.store_preset("夜间", self.make_config())
        self.model.unlink()
        self.assertIsNotNone(self.session.read_preset_baseline("夜间"))

    def test_the_baseline_read_does_not_check_algorithms(self) -> None:
        """恢复那条路今天不传 known_algorithms。抹平了的话, 一份用着未安装算法的
        预设开机时就从「安静地记住」变成「读不了」。"""
        self.session.store_preset("插件", self.make_config())
        path = self.root / "presets" / "插件.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["aim_profiles"][0]["algorithm"] = "someone_elses_plugin"
        path.write_text(json.dumps(payload), encoding="utf-8")

        preset = self.session.read_preset_baseline("插件")
        self.assertEqual(preset.aim_profile_1.algorithm, "someone_elses_plugin")

    def test_the_baseline_read_raises_instead_of_popping_a_dialog(self) -> None:
        """读不了要由调用方决定怎么说: 界面把它写进运行状态, 不弹窗。"""
        from rhodes_fast.presets import PresetError

        with self.assertRaises(PresetError):
            self.session.read_preset_baseline("不存在")
        self.assertEqual(self.prompter.errors, [])

    def test_find_preset_ignores_case(self) -> None:
        self.session.store_preset("Night", self.make_config())
        self.assertEqual(self.session.find_preset("night"), "Night")
        self.assertIsNone(self.session.find_preset("白天"))

    # ---- 删除 ----

    def test_delete_asks_before_removing(self) -> None:
        self.session.store_preset("夜间", self.make_config())
        session = self.new_session()          # 队列空 → 答「取消」
        self.assertFalse(session.delete_preset("夜间"))
        self.assertIn("夜间", session.list_presets())   # 还在
        self.assertEqual(len(self.prompter.asked), 1)

    def test_delete_removes_when_confirmed(self) -> None:
        self.session.store_preset("夜间", self.make_config())
        session = self.new_session(answers=[True])
        self.assertTrue(session.delete_preset("夜间"))
        self.assertNotIn("夜间", session.list_presets())

    def test_delete_is_asked_as_a_dangerous_confirmation(self) -> None:
        """漏了 danger=True 的话 tkinter 那边的默认按钮会从「取消」变成「确定」,
        手滑按个回车预设就没了 —— 代码里为这件事专门写了注释。"""
        self.session.store_preset("夜间", self.make_config())
        session = self.new_session()
        session.delete_preset("夜间")
        self.assertEqual(self.prompter.dangerous, ["删除预设"])

    def test_the_delete_wording_is_unchanged(self) -> None:
        self.session.store_preset("夜间", self.make_config())
        session = self.new_session()
        session.delete_preset("夜间")
        self.assertEqual(
            self.prompter.asked,
            [("删除预设", "确定删除预设「夜间」吗？\n\n删除后找不回来。界面上现在的设置不会变。")],
        )


class _InitialRecordingPrompter(RecordingPrompter):
    """RecordingPrompter 不记 ask_text 的 initial, 而重命名框里预填的旧名字是
    用户一眼能看见的东西 —— 少传它, 用户就得把名字重打一遍。"""

    def __init__(self, answers: list[object] | None = None) -> None:
        super().__init__(answers=answers)
        self.initials: list[str] = []

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        self.initials.append(initial)
        return super().ask_text(title, message, initial=initial)


class AlgorithmLibraryTest(unittest.TestCase):
    """算法库: 列表、导入、改名、删除、看源码。

    这些用例故意很少调 reload_algorithms —— 列表读的是注册表, 不是已加载的
    那份算法表, 「刚导入就看得见」正是要守住的行为。
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.config_path = self.root / "settings.txt"
        self.source = self.root / "my_aim.py"
        self.source.write_text(ALGORITHM_SOURCE, encoding="utf-8")
        self.shown: list[tuple[str, str]] = []
        # 已加载的算法表是进程级的全局状态, 不还原会漏给后面的测试。
        self.addCleanup(set_installed_algorithms, {})

    def _session(self, answers: list[object] | None = None) -> GuiSession:
        """换一个 prompter 的新会话: 复用同一个会把前面步骤问过的记录也带进来。"""
        self.prompter = _InitialRecordingPrompter(answers=answers)
        return GuiSession(self.config_path, self.prompter)

    def _import(self, session: GuiSession, **kwargs):
        return session.import_algorithm(
            self.source,
            show_source=lambda title, source: self.shown.append((title, source)),
            **kwargs,
        )

    def _installed(self, answers: list[object] | None = None) -> GuiSession:
        """装好一份算法, 返回一个还没问过任何问题的新会话。"""
        self._import(self._session(answers=[INSTALL]))
        return self._session(answers=answers)

    def _names(self, session: GuiSession) -> list[str]:
        return [row[1] for row in session.library_rows()]

    # ---- 列表 ----

    def test_builtins_show_up_as_six_column_rows(self) -> None:
        rows = self._session().library_rows()
        self.assertTrue(rows)
        self.assertTrue(all(len(row) == 6 for row in rows))
        self.assertIn("p", [row[1] for row in rows if row[5] == "内置"])

    def test_builtin_rows_have_no_author_file_or_date_of_their_own(self) -> None:
        for row in self._session().library_rows():
            if row[5] == "内置":
                self.assertEqual((row[2], row[3], row[4]), ("Endfield", "—", "—"))

    def test_an_imported_algorithm_shows_up_before_any_restart(self) -> None:
        """列表读注册表, 不读已加载那份。点完导入就得看见, 否则用户以为坏了。"""
        session = self._session(answers=[INSTALL])
        self.assertIsNotNone(self._import(session))
        rows = {row[1]: row for row in session.library_rows()}
        self.assertEqual(rows["my_aim"][0], "我的瞄准")
        self.assertEqual(rows["my_aim"][2], "阿明")
        self.assertEqual(rows["my_aim"][3], "my_aim.py")
        self.assertEqual(rows["my_aim"][4][:2], "20")
        self.assertEqual(rows["my_aim"][5], "已导入")
        # 而它确实还没进已加载的算法表 —— 那要等重载。
        self.assertNotIn("my_aim", available_algorithms())

    # ---- 导入 ----

    def test_import_asks_before_the_file_gets_a_chance_to_run(self) -> None:
        session = self._session()          # 队列空 → 取消
        self.assertIsNone(self._import(session))
        title, message = self.prompter.asked[0]
        self.assertEqual(title, "导入算法")
        self.assertIn("文件：my_aim.py", message)
        self.assertIn("作者：阿明", message)
        self.assertIn("标识：my_aim", message)
        self.assertIn("显示名：我的瞄准", message)
        self.assertIn("算法是一段会在你机器上运行的 Python 代码。只导入你信得过的来源。", message)
        self.assertIn("选「是」查看源码，选「否」直接导入，选「取消」放弃。", message)
        self.assertNotIn("my_aim", self._names(session))

    def test_yes_means_show_me_the_source_then_ask_again(self) -> None:
        """三个按钮里「是」不是确认: 它是查看源码, 看完还要再问一次。"""
        session = self._session(answers=[SHOW_SOURCE, INSTALL])
        self.assertIsNotNone(self._import(session))
        self.assertEqual(self.shown, [("my_aim.py", ALGORITHM_SOURCE)])
        self.assertEqual(len(self.prompter.asked), 2)

    def test_a_file_that_is_not_python_is_refused_without_asking(self) -> None:
        broken = self.root / "broken.py"
        broken.write_text("def (:\n", encoding="utf-8")
        session = self._session(answers=[INSTALL])
        self.assertIsNone(
            session.import_algorithm(broken, show_source=lambda *_: None)
        )
        self.assertEqual([title for title, _ in self.prompter.errors], ["导入失败"])
        self.assertEqual(self.prompter.asked, [])

    def test_a_file_without_a_named_class_is_refused(self) -> None:
        plain = self.root / "plain.py"
        plain.write_text("x = 1\n", encoding="utf-8")
        session = self._session(answers=[INSTALL])
        self.assertIsNone(session.import_algorithm(plain, show_source=lambda *_: None))
        self.assertEqual(
            self.prompter.errors, [("导入失败", "源码里找不到带 NAME 的算法类。")]
        )

    def test_importing_over_an_existing_algorithm_asks_first(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        again = self._session(answers=[INSTALL, True])
        self.assertIsNotNone(self._import(again))
        title, message = self.prompter.asked[1]
        self.assertEqual(title, "已有同名算法")
        self.assertEqual(
            message,
            "已存在同名算法「my_aim」（来自 my_aim.py）。要替换吗？\n\n"
            "替换会保留你给它起的显示名。",
        )

    def test_declining_the_replacement_leaves_the_old_one_alone(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        again = self._session(answers=[INSTALL])       # 第二问队列空 → 取消
        self.assertIsNone(self._import(again))
        self.assertEqual(self.prompter.infos, [])      # 没弹「导入成功」
        self.assertIn("my_aim", self._names(again))

    def test_the_replacement_keeps_the_name_the_user_gave_it(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        self._session(answers=["换了个名"]).rename_algorithm("my_aim")
        again = self._session(answers=[INSTALL, True])
        self._import(again)
        rows = {row[1]: row for row in again.library_rows()}
        self.assertEqual(rows["my_aim"][0], "换了个名")

    def test_the_list_is_refreshed_before_the_success_dialog(self) -> None:
        """今天的顺序是先重载列表再弹「导入成功」。反过来的话用户会隔着弹窗
        盯着一份没变的列表, 正是这个流程要避免的误会。"""
        session = self._session(answers=[INSTALL])
        infos_when_refreshed: list[int] = []
        entry = self._import(
            session,
            on_installed=lambda _entry: infos_when_refreshed.append(len(self.prompter.infos)),
        )
        self.assertEqual(infos_when_refreshed, [0])
        self.assertEqual(
            self.prompter.infos,
            [("导入成功", "「我的瞄准」已装进算法库，现在就能在控制方案里选它。")],
        )
        self.assertEqual((entry.name, entry.display_name), ("my_aim", "我的瞄准"))

    # ---- 改名 ----

    def test_rename_asks_for_a_new_display_name_and_prefills_the_old_one(self) -> None:
        session = self._installed(answers=["换了个名"])
        self.assertEqual(session.rename_algorithm("my_aim"), "换了个名")
        self.assertEqual(self.prompter.initials, ["我的瞄准"])
        rows = {row[1]: row for row in session.library_rows()}
        self.assertEqual(rows["my_aim"][0], "换了个名")

    def test_the_rename_wording_is_unchanged(self) -> None:
        session = self._installed(answers=["换了个名"])
        session.rename_algorithm("my_aim")
        self.assertEqual(
            self.prompter.asked,
            [
                (
                    "重命名算法",
                    "给「我的瞄准」起个新的显示名。\n\n"
                    "标识 my_aim 不会变——别人发来的调校认的是标识。",
                )
            ],
        )

    def test_rename_returns_the_trimmed_name_the_registry_actually_stored(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        session = self._session(answers=["  换了个名  "])
        self.assertEqual(session.rename_algorithm("my_aim"), "换了个名")

    def test_cancelling_the_rename_changes_nothing(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        session = self._session()                       # 队列空 → 取消
        self.assertIsNone(session.rename_algorithm("my_aim"))
        rows = {row[1]: row for row in session.library_rows()}
        self.assertEqual(rows["my_aim"][0], "我的瞄准")

    def test_renaming_to_nothing_reports_an_error(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        session = self._session(answers=["   "])
        self.assertIsNone(session.rename_algorithm("my_aim"))
        self.assertEqual([title for title, _ in self.prompter.errors], ["改名失败"])

    def test_renaming_a_builtin_asks_nothing(self) -> None:
        """内置算法不在注册表里。按钮对它是灰的, 走到这里也该悄悄什么都不做。"""
        session = self._session(answers=["随便"])
        self.assertIsNone(session.rename_algorithm("p"))
        self.assertEqual(self.prompter.asked, [])

    # ---- 删除 ----

    def test_delete_asks_first(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        refusing = self._session()                      # 队列空 → 取消
        self.assertIsNone(refusing.delete_algorithm("my_aim"))
        self.assertIn("my_aim", self._names(refusing))

    def test_delete_removes_the_file_and_hands_back_the_display_name(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        session = self._session(answers=[True])
        self.assertEqual(session.delete_algorithm("my_aim"), "我的瞄准")
        self.assertNotIn("my_aim", self._names(session))
        self.assertFalse((session.algorithms_dir / "my_aim.py").exists())

    def test_the_delete_wording_is_unchanged(self) -> None:
        session = self._installed()
        session.delete_algorithm("my_aim")
        self.assertEqual(
            self.prompter.asked,
            [
                (
                    "删除算法",
                    "要删掉「我的瞄准」吗？\n\n"
                    "my_aim.py 会被删除。正指着它的控制方案会当场改回比例控制。",
                )
            ],
        )

    def test_deleting_an_algorithm_is_not_flagged_as_a_dangerous_confirmation(self) -> None:
        """删预设那个框传了 icon=WARNING / default=CANCEL, 这个没传。补上
        danger=True 会换掉图标、把默认按钮从「确定」挪到「取消」—— 用户看得见,
        而这个计划的验收标准是零可见变化。"""
        session = self._installed()
        session.delete_algorithm("my_aim")
        self.assertEqual(self.prompter.dangerous, [])

    def test_deleting_a_builtin_asks_nothing_and_says_nothing(self) -> None:
        session = self._session(answers=[True])
        self.assertIsNone(session.delete_algorithm("p"))
        self.assertEqual(self.prompter.asked, [])
        self.assertEqual(self.prompter.errors, [])
        self.assertIn("p", self._names(session))

    # ---- 源码 ----

    def test_the_source_comes_back_with_the_file_name_to_title_the_window(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        session = self._session()
        self.assertEqual(session.algorithm_source("my_aim"), ("my_aim.py", ALGORITHM_SOURCE))

    def test_a_builtin_has_no_source_to_show(self) -> None:
        self.assertIsNone(self._session().algorithm_source("p"))

    def test_a_source_file_that_went_missing_reports_an_error(self) -> None:
        self._import(self._session(answers=[INSTALL]))
        session = self._session()
        (session.algorithms_dir / "my_aim.py").unlink()
        self.assertIsNone(session.algorithm_source("my_aim"))
        self.assertEqual([title for title, _ in self.prompter.errors], ["打不开源码"])

    # ---- 重载 ----

    def test_reloading_publishes_the_imported_algorithm(self) -> None:
        session = self._installed()
        self.assertEqual(session.reload_algorithms(), [])
        self.assertIn("my_aim", available_algorithms())

    def test_reloading_hands_back_a_warning_per_algorithm_that_will_not_load(self) -> None:
        """加载失败不该弹窗: 界面把它写进运行日志, 不打断用户。"""
        session = self._installed()
        (session.algorithms_dir / "my_aim.py").write_text(
            "raise RuntimeError('炸了')\n", encoding="utf-8"
        )
        warnings = session.reload_algorithms()
        self.assertEqual(len(warnings), 1)
        self.assertIn("我的瞄准", warnings[0])
        self.assertNotIn("my_aim", available_algorithms())
        self.assertEqual(self.prompter.errors, [])


class BuildCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.session = GuiSession(self.root / "settings.txt", RecordingPrompter())

    def _base(self, **overrides) -> list[str]:
        kwargs = dict(
            config_path=self.root / "settings.txt",
            stop_file=self.root / "stop",
            runtime_aim_file=self.root / "aim",
        )
        kwargs.update(overrides)
        return self.session.build_command(**kwargs)

    def test_always_runs_the_package_unbuffered(self) -> None:
        """-u 不能掉: 掉了子进程的输出会卡在缓冲区里, 运行状态就一片空白。"""
        command = self._base()
        self.assertEqual(command[1:4], ["-u", "-m", "rhodes_fast"])
        self.assertIn(str(self.root / "settings.txt"), command)

    def test_the_interpreter_is_the_console_one_next_to_this_one(self) -> None:
        """界面自己可能跑在 pythonw.exe 下, 那时 sys.executable 是 pythonw.exe,
        拿它起子进程就没有 stdout 可读了。对应 gui.py 的 with_name("python.exe")。"""
        interpreter = Path(self._base()[0])
        self.assertEqual(interpreter.name, "python.exe")
        self.assertEqual(interpreter.parent, Path(sys.executable).parent)

    def test_stop_and_runtime_aim_files_are_always_passed(self) -> None:
        command = self._base()
        self.assertEqual(command[command.index("--stop-file") + 1], str(self.root / "stop"))
        self.assertEqual(
            command[command.index("--runtime-aim-file") + 1], str(self.root / "aim")
        )

    def test_no_preview_flags_when_no_port_given(self) -> None:
        """跑基准测试时不开预览 —— 对应 gui.py 里 _launch 的 `if not arguments`。"""
        command = self._base()
        self.assertNotIn("--preview-port", command)
        self.assertNotIn("--preview-enable-file", command)
        self.assertNotIn("--trail-settings-file", command)

    def test_preview_flags_appear_together(self) -> None:
        command = self._base(
            preview_port=54321,
            preview_enable_file=self.root / "preview",
            trail_settings_file=self.root / "trail",
        )
        self.assertIn("--preview-port", command)
        self.assertEqual(command[command.index("--preview-port") + 1], "54321")
        self.assertIn("--preview-enable-file", command)
        self.assertIn("--trail-settings-file", command)

    def test_latency_log_only_when_a_path_is_given(self) -> None:
        self.assertNotIn("--latency-log", self._base(preview_port=1))
        command = self._base(preview_port=1, latency_log=self.root / "latency-x.csv")
        self.assertEqual(
            command[command.index("--latency-log") + 1], str(self.root / "latency-x.csv")
        )

    def test_latency_log_stays_out_of_the_benchmark_runs(self) -> None:
        """延迟日志今天只在开预览那条路上给, 基准测试那条路一个预览开关都不带。"""
        self.assertNotIn("--latency-log", self._base(latency_log=self.root / "x.csv"))

    def test_extra_arguments_go_last(self) -> None:
        """基准测试那条路会追加自己的参数 —— 对应 gui.py 的 command.extend(arguments)。"""
        command = self._base(extra=["--benchmark", "30"])
        self.assertEqual(command[-2:], ["--benchmark", "30"])


class SubprocessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.prompter = RecordingPrompter()
        self.session = GuiSession(self.root / "settings.txt", self.prompter)

    def _stop_and_wait(self, session: GuiSession | None = None) -> None:
        """收摊: 停下来还要等它真的没了。

        stop() 照 gui.py 的 _stop 是不阻塞的 (那是个按钮回调, 阻塞就冻界面),
        硬杀交给一个 daemon 线程。测试进程先结束的话那个线程跟着没了, 子进程
        就留在测试机上成了孤儿, 所以这里等 is_running 落下来。
        """
        session = session or self.session
        session.stop()
        deadline = time.monotonic() + 20.0
        while session.is_running and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(session.is_running, "子进程没收摊, 会在测试机上留孤儿")

    def test_lines_reach_the_callback_and_exit_is_reported(self) -> None:
        lines: list[str] = []
        exits: list[int] = []
        done = threading.Event()
        started = self.session.start(
            [sys.executable, "-u", "-c", "print('准备就绪。')"],
            cwd=self.root,
            on_line=lines.append,
            on_exit=lambda code: (exits.append(code), done.set()),
        )
        self.assertTrue(started)
        self.assertTrue(done.wait(timeout=20))
        self.assertIn("准备就绪。", [line.strip() for line in lines])
        self.assertEqual(exits, [0])
        self.assertFalse(self.session.is_running)

    def test_a_failing_child_reports_its_return_code(self) -> None:
        """非零退出码要原样交给界面 —— 界面靠它区分「已停止」和「运行出错」。"""
        exits: list[int] = []
        done = threading.Event()
        self.session.start(
            [sys.executable, "-c", "raise SystemExit(3)"],
            cwd=self.root,
            on_line=lambda _: None,
            on_exit=lambda code: (exits.append(code), done.set()),
        )
        self.assertTrue(done.wait(timeout=20))
        self.assertEqual(exits, [3])

    def test_the_child_runs_in_utf8_mode(self) -> None:
        """PYTHONUTF8=1 不能掉: 中文日志在 GBK 控制台上会炸。"""
        lines: list[str] = []
        done = threading.Event()
        self.session.start(
            [sys.executable, "-u", "-c", "import sys; print(sys.flags.utf8_mode)"],
            cwd=self.root,
            on_line=lines.append,
            on_exit=lambda _: done.set(),
        )
        self.assertTrue(done.wait(timeout=20))
        self.assertEqual([line.strip() for line in lines if line.strip()], ["1"])

    def test_undecodable_bytes_do_not_kill_the_reader(self) -> None:
        """errors="replace" 不能掉: 一行解不出来的字节不该让整个读取线程死掉,
        后面的日志还得照常流进来。"""
        lines: list[str] = []
        done = threading.Event()
        self.session.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\n'); "
                "sys.stdout.buffer.flush(); print('后面这行还要到')",
            ],
            cwd=self.root,
            on_line=lines.append,
            on_exit=lambda _: done.set(),
        )
        self.assertTrue(done.wait(timeout=20))
        self.assertIn("后面这行还要到", [line.strip() for line in lines])

    def test_starting_twice_is_refused(self) -> None:
        self.session.start(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.root,
            on_line=lambda _: None,
            on_exit=lambda _: None,
        )
        self.addCleanup(self._stop_and_wait)
        self.assertTrue(self.session.is_running)
        self.assertFalse(
            self.session.start(
                [sys.executable, "-c", "pass"],
                cwd=self.root,
                on_line=lambda _: None,
                on_exit=lambda _: None,
            )
        )

    def test_failure_to_spawn_reports_an_error_and_leaves_nothing_running(self) -> None:
        prompter = RecordingPrompter()
        session = GuiSession(self.root / "settings.txt", prompter)
        self.assertFalse(
            session.start(
                [str(self.root / "没有这个程序.exe")],
                cwd=self.root,
                on_line=lambda _: None,
                on_exit=lambda _: None,
            )
        )
        self.assertEqual([title for title, _ in prompter.errors], ["无法启动"])
        self.assertFalse(session.is_running)

    def test_stop_writes_the_stop_file_and_lets_the_child_finish_its_own_way(self) -> None:
        """不许改成直接 kill: 管线要靠这个文件自己收尾 (关 KMBox、落盘延迟日志)。
        硬杀那条路只有超时才走。"""
        stop_file = self.root / "gui.stop"
        lines: list[str] = []
        exits: list[int] = []
        done = threading.Event()
        child = (
            "import pathlib, time\n"
            "stop = pathlib.Path(%r)\n"
            "print('已启动')\n"
            "while not stop.exists():\n"
            "    time.sleep(0.02)\n"
            "print('收尾完毕')\n" % str(stop_file)
        )
        self.session.start(
            [sys.executable, "-u", "-c", child],
            cwd=self.root,
            on_line=lines.append,
            on_exit=lambda code: (exits.append(code), done.set()),
            stop_file=stop_file,
        )
        self.addCleanup(self._stop_and_wait)
        self.session.stop()
        self.assertTrue(done.wait(timeout=20))
        self.assertTrue(stop_file.exists())
        self.assertIn("收尾完毕", [line.strip() for line in lines])
        self.assertEqual(exits, [0])

    def test_stop_does_not_block_the_caller(self) -> None:
        """_stop 是按钮回调, 阻塞就把界面冻住了 —— 硬杀的等待归后台线程。"""
        self.session.start(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.root,
            on_line=lambda _: None,
            on_exit=lambda _: None,
        )
        self.addCleanup(self._stop_and_wait)
        started_at = time.monotonic()
        self.session.stop()
        self.assertLess(time.monotonic() - started_at, 1.0)

    def test_stopping_when_nothing_runs_is_a_no_op(self) -> None:
        self.session.stop()
        self.assertFalse(self.session.is_running)


if __name__ == "__main__":
    unittest.main()
