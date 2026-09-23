from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

# 顶部就把 gui_core 拉进来, 别等到 main() 里那句 import。下面好几处都是在
# patch.dict(sys.modules, ...) 里面调 main() 的, 而 patch.dict 退出时会把块内
# 新导入的模块整个丢掉 —— numpy 这类 C 扩展在一个进程里只能加载一次, 第二次
# main() 就会撞上 "cannot load module more than once per process"。
from rhodes_fast.gui_core import session as gui_core_session


class ApiTest(unittest.TestCase):
    def test_window_controls_reach_the_window(self) -> None:
        """无边框窗口没有系统按钮, 这三个是唯一的出路 —— 接错了窗口就关不掉。"""
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api._attach(window)

        api.minimize()
        window.minimize.assert_called_once_with()

        api.close()
        window.destroy.assert_called_once_with()

    def test_toggle_maximize_flips_back_and_forth(self) -> None:
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api._attach(window)
        api.toggle_maximize()
        api.toggle_maximize()
        self.assertEqual(window.toggle_fullscreen.call_count, 2)

    def test_only_the_intended_methods_are_visible_to_js(self) -> None:
        """pywebview 把 Api 的每个公开方法都挂到 window.pywebview.api 上, 只跳过
        下划线开头的 (util.py: `if name.startswith("_"): continue`)。实测过一次
        公开的 attach: 它跟三个窗口控制一起出现在 JS 的调用面上, 而 JS 传进来的
        参数是 JSON 对象 —— 误调一次窗口引用就变成了一个 dict, 三个按钮全哑。

        给 Api 加公开方法时这条会变红, 那正是该停一秒的地方: 新方法立刻就是
        JS 能调的东西。真要给 JS 用就把名字加进来, 不给就加下划线 —— 启停这三个
        (start / stop / is_running) 是故意暴露的, 页面上那个按钮就靠它们;
        _wire / _push_log 只有 Python 侧调, 所以带下划线。
        """
        from rhodes_fast.gui_web.app import Api

        exposed = sorted(
            name for name in dir(Api) if not name.startswith("_") and callable(getattr(Api, name))
        )
        self.assertEqual(
            exposed,
            [
                "answer_prompt",
                "browse_model",
                "close",
                "delete_algorithm",
                "delete_preset",
                "import_algorithm",
                "is_running",
                "minimize",
                "open_preview_window",
                "persist_trail_length",
                "refresh_library",
                "refresh_presets",
                "rename_algorithm",
                "run_benchmark",
                "run_check",
                "run_pipeline_benchmark",
                "save_preset",
                "save_preset_as",
                "save_settings",
                "select_preset",
                "set_algorithm",
                "set_field",
                "set_log_collapsed",
                "set_preview_active",
                "show_algorithm_source",
                "start",
                "stop",
                "toggle_maximize",
            ],
        )

    def test_calls_before_attach_do_not_raise(self) -> None:
        """JS 那边可能在窗口就绪之前就点了按钮。炸在这里等于白屏。"""
        from rhodes_fast.gui_web.app import Api

        Api().minimize()


class LaunchHarness:
    """启停和动作行共用的接线。

    不是 TestCase: 让 ActionRowTest 直接继承 StartStopTest 的话, 那十几条启停
    测试会跟着再跑一遍 —— 同样的东西验两次, 而且第二次挂在一个看起来毫不相干的
    类名下。
    """

    def _api(self, *, running: bool = False, process: object | None = None):
        from dataclasses import replace as _replace

        from rhodes_fast.config import default_config
        from rhodes_fast.gui_core.state import config_to_form_state, default_labels, display_path
        from rhodes_fast.gui_web.app import Api
        from rhodes_fast.gui_web.forms import FormBridge

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.config_path = self.base / "settings.txt"
        self.cache = self.base / ".cache"
        # 启动前要把表单写进 settings.txt (子进程读的是文件, 不是界面), 而
        # 写完会照旧界面回读一次校验 —— 那一次是带模型检查的。模型文件不在的话
        # 每条启停测试都会停在「设置无法保存」上, 跟它要测的东西毫无关系。
        model = self.base / "model.onnx"
        model.write_bytes(b"not really a model")
        self.window = Mock()
        self.session = Mock()
        self.session.is_running = running
        self.session.build_command.return_value = ["python", "-u", "-m", "rhodes_fast"]
        self.session.start.return_value = Mock() if process is None else process
        self.relay = Mock()
        self.relay.start.return_value = 54321
        self.relay.port = 54321

        labels = default_labels()
        config = default_config()
        config = _replace(config, model=_replace(config.model, path=model))
        self.bridge = FormBridge(
            config_to_form_state(config, self.base, labels, display_path=display_path)
        )
        api = Api()
        api._attach(self.window)
        api._wire(
            session=self.session,
            relay=self.relay,
            config_path=self.config_path,
            config=config,
            preview_enable_file=self.cache / f"webview-{os.getpid()}.preview",
            bridge=self.bridge,
            labels=labels,
            prompter=Mock(),
        )
        return api

    def _scripts(self) -> list[str]:
        return [call.args[0] for call in self.window.evaluate_js.call_args_list]

    def _log_lines(self) -> list[str]:
        """推进运行状态的那些行, 解回原文。

        脚本里是 json.dumps 的结果, 中文在里面是 \\uXXXX —— 直接拿中文去 assertIn
        永远不会命中, 而那看起来就像「日志没推出去」。
        """
        head = "window.appendLog("
        return [
            json.loads(script[len(head) : -1])
            for script in self._scripts()
            if script.startswith(head)
        ]


class StartStopTest(LaunchHarness, unittest.TestCase):
    """页面上那个按钮到子进程之间的一整条线。

    GuiSession 和 PreviewRelay 都用替身: 这里要钉的是「参数对不对、顺序对不对」,
    真起一个子进程或者真绑一个 UDP 端口都验不出这些, 只会让测试变慢变飘。
    """

    def test_start_passes_the_relay_port_into_the_command(self) -> None:
        """预览端口必须是真绑上的那个 —— 子进程要往它发帧。"""
        api = self._api()

        api.start()

        kwargs = self.session.build_command.call_args.kwargs
        self.assertEqual(kwargs["preview_port"], 54321)
        self.assertEqual(kwargs["config_path"], self.config_path)

    def test_the_relay_is_bound_before_the_command_is_built(self) -> None:
        """端口是 bind 完才知道的, 而它要写进子进程的命令行。反过来的话命令行里
        只能是 None, 子进程一帧都不会发, 而界面这边什么错都看不到。"""
        api = self._api()
        order = Mock()
        order.attach_mock(self.relay.start, "relay_start")
        order.attach_mock(self.session.build_command, "build_command")

        api.start()

        self.assertEqual(
            [name for name, _args, _kwargs in order.mock_calls], ["relay_start", "build_command"]
        )

    def test_start_hands_the_stop_file_to_the_session(self) -> None:
        """漏了 stop_file=, GuiSession.stop() 里 self._stop_file 就是 None,
        停止文件根本不写 —— 子进程收不到「请收尾」的信号, 四秒后被 terminate
        硬杀: KMBox 不会正常关闭, 延迟日志不落盘。"""
        api = self._api()

        api.start()

        self.assertEqual(
            self.session.start.call_args.kwargs["stop_file"],
            self.session.build_command.call_args.kwargs["stop_file"],
        )

    def test_start_runs_the_child_in_the_config_directory(self) -> None:
        """模型路径、预设都是相对 settings.txt 算的。cwd 错了, 子进程找不到模型。"""
        api = self._api()

        api.start()

        self.assertEqual(self.session.start.call_args.kwargs["cwd"], self.config_path.parent)

    def test_starting_twice_is_refused(self) -> None:
        """已经在跑还往下走, 就是第二个子进程抢同一个 KMBox 和同一个预览端口。"""
        api = self._api(running=True)

        api.start()

        self.session.build_command.assert_not_called()
        self.session.start.assert_not_called()

    def test_a_stale_stop_file_is_cleared_before_the_child_starts(self) -> None:
        """上一轮留下的停止文件会让新子进程一起来就自己收尾 —— 用户看到的是
        「点了启动, 闪一下就停了」。照 gui.py:_launch, 起之前先清一遍。"""
        api = self._api()
        self.cache.mkdir(parents=True, exist_ok=True)
        stop_file = self.cache / f"webview-{os.getpid()}.stop"
        stop_file.touch()
        seen: list[bool] = []

        def spy(*_args, **_kwargs):
            seen.append(stop_file.exists())
            return Mock()

        self.session.start.side_effect = spy
        api.start()

        self.assertEqual(seen, [False], "子进程起来的时候停止文件还在")

    def test_log_lines_are_json_escaped_before_reaching_js(self) -> None:
        """日志里有中文、引号和 Windows 路径的反斜杠。直接拼进 JS 字符串,
        一个带引号的模型路径就能把 evaluate_js 打断。"""
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api._attach(window)
        api._push_log('模型 "C:\\MODEL\\a.onnx" 已加载')
        script = window.evaluate_js.call_args.args[0]
        self.assertNotIn('"C:\\MODEL', script)
        self.assertIn("\\\\MODEL", script)

    def test_start_flips_the_interface_into_the_running_state(self) -> None:
        """按钮的文案、状态标签都挂在这一下上。不翻的话用户按第二次就是在
        「启动」一个已经在跑的管线, 而界面拒绝得一声不响。"""
        api = self._api()

        api.start()

        self.assertIn("window.setRunState(true)", self._scripts())

    def test_a_child_that_will_not_start_leaves_the_interface_idle(self) -> None:
        """GuiSession.start() 起不来时返回 None (已经通过 prompter 报过错)。
        这时候把界面翻成「运行中」, 按钮就写着「停止」而根本没东西在跑。"""
        api = self._api(process=False)
        self.session.start.return_value = None

        api.start()

        self.assertNotIn("window.setRunState(true)", self._scripts())

    def test_stop_goes_through_the_session(self) -> None:
        api = self._api()

        api.stop()

        self.session.stop.assert_called_once_with()

    def test_stop_leaves_the_relay_open(self) -> None:
        """PreviewRelay.start() 是可重入的, 同一个 UDP 端口在整个进程生命周期里
        复用就行。停一次关一次, 下一轮就是一个新端口 —— 而 relay 归 main() 的
        finally 关。"""
        api = self._api()

        api.stop()

        self.relay.close.assert_not_called()

    def test_the_exit_callback_reports_the_code_and_flips_back(self) -> None:
        api = self._api()
        api.start()
        on_exit = self.session.start.call_args.kwargs["on_exit"]

        on_exit(3)

        self.assertTrue(any("退出码 3" in line for line in self._log_lines()))
        self.assertIn("window.setRunState(false)", self._scripts())

    def test_the_exit_callback_clears_the_temp_files(self) -> None:
        """停止文件留着, 下一轮子进程一起来就自己收尾; 那份 .aim.json / .trail.json
        留着只是垃圾。预览开关文件归 Task 9, 但停了之后它也没有意义了。"""
        api = self._api()
        api.start()
        on_exit = self.session.start.call_args.kwargs["on_exit"]
        leftovers = [
            self.cache / f"webview-{os.getpid()}{suffix}"
            for suffix in (".stop", ".aim.json", ".trail.json", ".preview")
        ]
        for path in leftovers:
            path.touch()

        on_exit(0)

        for path in leftovers:
            self.assertFalse(path.exists(), f"{path.name} 没清掉")

    def test_is_running_asks_the_session(self) -> None:
        api = self._api(running=True)
        self.assertIs(api.is_running(), True)

    def test_calls_before_wiring_do_not_raise(self) -> None:
        """JS 那边可能在 main() 把东西串起来之前就点了按钮。炸在这里等于白屏。"""
        from rhodes_fast.gui_web.app import Api

        api = Api()
        api.start()
        api.stop()
        self.assertIs(api.is_running(), False)


class ImportSurfaceTest(unittest.TestCase):
    def test_importing_app_does_not_need_pywebview(self) -> None:
        """pywebview 是可选依赖, 而测试机上没人开窗口 —— `import webview` 必须
        留在 main() 里面。

        把 sys.modules["webview"] 设成 None 就等价于「这台机器上没装」:
        之后任何 `import webview` 都会抛 ImportError。只断言 main 可调用是拦不住
        顶部 import 的 —— 开发机上 pywebview 装着, 那样写照样绿。
        """
        # import rhodes_fast 只把顶层包拉进来, 子包不会跟着进 sys.modules ——
        # 单独跑这个类 (python -m unittest tests.test_gui_web_app.ImportSurfaceTest)
        # 时下面那行就是 KeyError。整模块跑时碰巧是绿的, 因为前面的用例已经
        # import 过 rhodes_fast.gui_web.app 了; 靠别人的副作用不算通过。
        import rhodes_fast
        import rhodes_fast.gui_web  # noqa: F401 - 为的就是这个副作用

        original = sys.modules["rhodes_fast.gui_web"]
        # sys.modules 由 patch.dict 还原, 但父包上的属性不归它管, 得自己放回去。
        self.addCleanup(setattr, rhodes_fast, "gui_web", original)

        with mock.patch.dict(sys.modules, {"webview": None}):
            for name in ("rhodes_fast.gui_web.app", "rhodes_fast.gui_web"):
                sys.modules.pop(name, None)
            module = importlib.import_module("rhodes_fast.gui_web.app")
            self.assertTrue(callable(module.main))

    def test_importing_app_does_not_open_a_window(self) -> None:
        """import 时就 create_window 的话, 全量测试会挂在一个没人关的窗口上。"""
        fake_webview = Mock()
        with mock.patch.dict(sys.modules, {"webview": fake_webview}):
            importlib.reload(importlib.import_module("rhodes_fast.gui_web.app"))
        fake_webview.create_window.assert_not_called()
        fake_webview.start.assert_not_called()


class MainWiringTest(unittest.TestCase):
    """main() 把 Task 3-5 的三件东西串起来, 而串错的后果都不会在测试里自己冒出来。"""

    def _run_main(self, *, start_raises: BaseException | None = None):
        from rhodes_fast.gui_web import app

        manager = Mock()
        fake_webview = Mock()
        # MagicMock 而不是 Mock: main() 要给 window.events.minimized 做 `+=` 绑定
        # (pywebview 的 Event 实现了 __add__), 而朴素的 Mock 不支持 + 运算。
        window = mock.MagicMock()
        fake_webview.create_window.return_value = window
        if start_raises is not None:
            fake_webview.start.side_effect = start_raises

        with (
            mock.patch.dict(sys.modules, {"webview": fake_webview}),
            mock.patch("rhodes_fast.gui_web.preview_relay.FrameBus") as bus_class,
            mock.patch("rhodes_fast.gui_web.preview_relay.PreviewRelay") as relay_class,
            mock.patch("rhodes_fast.gui_web.server.WebServer") as server_class,
        ):
            manager.attach_mock(relay_class.return_value.close, "relay_close")
            manager.attach_mock(bus_class.return_value.close, "bus_close")
            manager.attach_mock(server_class.return_value.stop, "server_stop")
            if start_raises is None:
                app.main()
            else:
                with self.assertRaises(type(start_raises)):
                    app.main()
            return manager, fake_webview, window, bus_class, server_class

    def _shutdown_order(self, manager: Mock) -> list[str]:
        return [name for name, _args, _kwargs in manager.mock_calls]

    def test_shutdown_runs_relay_then_bus_then_server(self) -> None:
        """顺序是死的, 反了就是一条泄漏的线程: /preview.mjpg 那条响应线程是
        daemon, server.stop() 里的 _threads.join() 对它是空操作 (见 server.py
        的 stop() docstring), 只有 bus.close() 能让它自己走完。relay 先关是因为
        它还在往 bus 上推帧 —— 关了 bus 再关 relay, 中间那段时间的帧是白收的。"""
        manager, _webview, _window, _bus, _server = self._run_main()
        self.assertEqual(self._shutdown_order(manager), ["relay_close", "bus_close", "server_stop"])

    def test_shutdown_happens_even_if_start_blows_up(self) -> None:
        """窗口起不来 (比如缺 WebView2 运行时) 也得收摊, 否则 HTTP server 的线程
        和那个 UDP 端口会跟着解释器一起挂到天亮。"""
        manager, _webview, _window, _bus, _server = self._run_main(start_raises=RuntimeError("boom"))
        self.assertEqual(self._shutdown_order(manager), ["relay_close", "bus_close", "server_stop"])

    def test_the_window_is_frameless_and_does_not_drag_as_a_whole(self) -> None:
        """easy_drag 开着的话整个窗口都能拖 —— 想拖个滑条结果把窗口拖走了。
        可拖区域由 CSS 的 -webkit-app-region 指定, 那是 Task 7 的事。"""
        _manager, fake_webview, _window, _bus, _server = self._run_main()
        kwargs = fake_webview.create_window.call_args.kwargs
        self.assertIs(kwargs["frameless"], True)
        self.assertIs(kwargs["easy_drag"], False)

    def test_the_window_loads_our_server_and_the_server_can_stream(self) -> None:
        """窗口指错地方 (比如 file://) 就没有 /preview.mjpg; server 少了 bus,
        预览是一条静默的 404 —— 两样都要到界面画完才看得出来。"""
        _manager, fake_webview, _window, bus_class, server_class = self._run_main()
        self.assertIs(fake_webview.create_window.call_args.args[1], server_class.return_value.url)
        self.assertIs(server_class.call_args.kwargs["bus"], bus_class.return_value)
        server_class.return_value.start.assert_called_once_with()

    def test_the_api_handed_to_js_is_attached_to_the_real_window(self) -> None:
        """attach() 漏了的话三个按钮全是哑的, 而窗口没有系统按钮 —— 只能去任务
        管理器杀进程。"""
        _manager, fake_webview, window, _bus, _server = self._run_main()
        api = fake_webview.create_window.call_args.kwargs["js_api"]
        api.close()
        window.destroy.assert_called_once_with()

    def test_closing_the_main_window_closes_the_popups_too(self) -> None:
        """webview.start() 要等**所有**窗口都关了才返回。主窗口关了而放大预览还
        开着的话, start() 一直不返回, 下面 finally 里的停管线也就一直不执行 ——
        留下一个孤零零的预览窗口, 和一条还在跑、还占着 KMBox 的推理。"""
        _manager, fake_webview, window, _bus, _server = self._run_main()
        api = fake_webview.create_window.call_args_list[0].kwargs["js_api"]
        api._popups = Mock()
        # 扫 mock_calls 而不是去 window.events.closed.__iadd__ 上找: `x += h` 会把
        # x 换成 __iadd__ 的返回值, 事后原来那个对象已经不挂在 events 上了。
        handlers = [
            call.args[0] for call in window.mock_calls
            if call[0] == "events.closed.__iadd__"
        ]
        self.assertTrue(handlers, "主窗口的 closed 上什么都没挂")
        for handler in handlers:
            handler()
        api._popups.close_all.assert_called()

    def test_the_start_button_is_not_dead_on_arrival(self) -> None:
        """session / config_path 没接上的话, 页面上那个按钮按下去什么都不会发生
        —— 而启停是这个界面今天唯一能干的事。config_path 照旧界面: 工作目录下的
        settings.txt, 而且是 resolve 过的绝对路径 (子进程的 cwd 是它的父目录)。"""
        _manager, fake_webview, _window, _bus, _server = self._run_main()
        api = fake_webview.create_window.call_args.kwargs["js_api"]
        self.assertIsNotNone(api._session)
        self.assertIsNotNone(api._relay)
        self.assertEqual(api._config_path, Path("settings.txt").resolve())
        self.assertEqual(api._preview_enable_file.suffix, ".preview")

    def test_the_prompter_writes_into_the_runtime_log(self) -> None:
        """GuiSession 起不来子进程时只会调 prompter.notify_error。接错了地方,
        那句「无法启动」就掉进虚空, 用户看到的是一个按了没反应的按钮。"""
        _manager, fake_webview, window, _bus, _server = self._run_main()
        api = fake_webview.create_window.call_args.kwargs["js_api"]
        api._session.prompter.notify_error("无法启动", "找不到 python.exe")
        # 看所有调用而不是最后一次: notify 现在既往运行状态里写一行, 又弹一个
        # 模态框 (弹窗关掉之后就什么都不剩了), 最后一次是 showModal。
        scripts = [call.args[0] for call in window.evaluate_js.call_args_list]
        self.assertTrue(
            any("window.appendLog(" in script and "python.exe" in script for script in scripts),
            "那句「无法启动」没进运行状态",
        )
        self.assertTrue(any("showModal" in script for script in scripts), "也该弹一个框")

    def test_the_child_process_is_asked_to_stop_before_the_window_goes_away(self) -> None:
        """关窗时子进程不该活下来变成孤儿 —— 窗口没了就再没人读它的日志, 也再
        没人停得了它。

        放在 relay.close() 之前是对的: api.stop() 不阻塞 (写完停止文件就返回,
        等四秒和硬杀都在 GuiSession 起的后台线程上), 所以它不会把这条收尾路径
        堵住; 而反过来先关 relay 的话, 子进程收尾那几百毫秒里发的预览帧会打在一个
        已经关掉的端口上。"""
        from rhodes_fast.gui_web import app

        manager = Mock()
        fake_webview = Mock()
        # 窗口要能接住 main() 里那两句 `window.events.X += handler`。
        fake_webview.create_window.return_value = mock.MagicMock()
        with (
            mock.patch.dict(sys.modules, {"webview": fake_webview}),
            mock.patch("rhodes_fast.gui_web.preview_relay.FrameBus"),
            mock.patch("rhodes_fast.gui_web.preview_relay.PreviewRelay") as relay_class,
            mock.patch("rhodes_fast.gui_web.server.WebServer"),
            mock.patch.object(gui_core_session, "GuiSession") as session_class,
        ):
            manager.attach_mock(session_class.return_value.stop, "session_stop")
            manager.attach_mock(relay_class.return_value.close, "relay_close")
            app.main()

        self.assertEqual(
            [name for name, _args, _kwargs in manager.mock_calls],
            ["session_stop", "relay_close"],
        )


if __name__ == "__main__":
    unittest.main()


class PreviewFlagTest(unittest.TestCase):
    """预览开关: 一个空文件, 管线每帧看一眼决定要不要渲染 + 编码 JPEG。

    不接这个开关, 不看预览时子进程照样在干这两件事 —— 而这台副机同时在跑推理。
    """

    def _wired(self, folder: str):
        from rhodes_fast.gui_web.app import Api

        flag = Path(folder) / ".cache" / "webview.preview"
        api = Api()
        api._attach(Mock())
        session = Mock()
        session.is_running = False
        session.build_command.return_value = ["python"]
        session.start.return_value = Mock()
        relay = Mock()
        relay.start.return_value = 54321
        api._wire(
            session=session,
            relay=relay,
            config_path=Path(folder) / "settings.txt",
            preview_enable_file=flag,
        )
        return api, flag, session

    def test_it_touches_and_removes_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            api, flag, _session = self._wired(folder)
            api.set_preview_active(True)
            self.assertTrue(flag.exists())
            api.set_preview_active(False)
            self.assertFalse(flag.exists())

    def test_it_creates_the_cache_directory_on_the_way(self) -> None:
        """切到预览屏可能发生在启动之前, 那时 .cache 还不存在 —— touch 会抛
        FileNotFoundError, 而它是从 JS 调过来的: 异常消失在 pywebview 里, 界面
        什么都不会说, 只是预览永远不出画面。"""
        with tempfile.TemporaryDirectory() as folder:
            api, flag, _session = self._wired(folder)
            self.assertFalse(flag.parent.exists())
            api.set_preview_active(True)
            self.assertTrue(flag.exists())

    def test_starting_puts_the_flag_back_after_the_cleanup(self) -> None:
        """start() 会先清掉上一轮的临时文件, 预览开关也在里面。用户明明停在
        预览屏上, 一按启动开关就没了 —— 画面再也不来, 而且切走再切回才会好。
        """
        with tempfile.TemporaryDirectory() as folder:
            api, flag, _session = self._wired(folder)
            api.set_preview_active(True)
            api.start()
            self.assertTrue(flag.exists(), "启动把预览开关清掉了, 没放回去")

    def test_starting_leaves_the_flag_off_when_nobody_is_watching(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            api, flag, _session = self._wired(folder)
            api.start()
            self.assertFalse(flag.exists())

    def test_minimising_the_window_turns_the_preview_off(self) -> None:
        """窗口最小化时浏览器会节流渲染, 消费端慢下来之后旧帧积在内核 socket
        缓冲里 (实测慢 6 倍时画面旧约 1.38 秒)。与其给 socket 写加非阻塞 + 丢帧,
        不如在这个场景直接让子进程别发 —— 顺手还省了副机的 CPU。"""
        with tempfile.TemporaryDirectory() as folder:
            api, flag, _session = self._wired(folder)
            api.set_preview_active(True)
            api._on_minimized()
            self.assertFalse(flag.exists())
            api._on_restored()
            self.assertTrue(flag.exists(), "还原之后预览没回来")

    def test_restoring_does_not_turn_on_a_preview_nobody_asked_for(self) -> None:
        """「窗口没最小化」和「用户在看预览屏」是两件事。还原时无脑打开的话,
        停在算法库屏的用户会白白让子进程一直渲染。"""
        with tempfile.TemporaryDirectory() as folder:
            api, flag, _session = self._wired(folder)
            api._on_minimized()
            api._on_restored()
            self.assertFalse(flag.exists())

    def test_it_does_nothing_without_a_flag_path(self) -> None:
        from rhodes_fast.gui_web.app import Api

        Api().set_preview_active(True)


class TelemetryPushTest(unittest.TestCase):
    """管线每秒打两行数字。它们本来就要流进运行状态, 顺路读一遍填上状态栏和
    MetricRail —— 不然那几格永远是横杠, 而这个项目的全部意义就是那个延迟数。"""

    def _api(self):
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api._attach(window)
        return api, window

    def _scripts(self, window) -> list[str]:
        return [call.args[0] for call in window.evaluate_js.call_args_list]

    def test_a_status_line_reaches_the_page_as_numbers(self) -> None:
        api, window = self._api()
        api._push_log("采集= 241.0 帧/秒  处理= 240.8 帧/秒  推理=  2.31 毫秒  "
                      "检测=  0.42 毫秒  检测= 3  自瞄标签=person  方案=1  移动=120  丢帧=  0")
        scripts = self._scripts(window)
        # 这一行本身不进日志了 (它每秒来一次), 改去刷新固定状态行 ——
        # StatusLineRoutingTest 钉的是那条路, 这里只管数字有没有解出来。
        self.assertTrue(any("setStatusLine" in script for script in scripts))
        telemetry = [script for script in scripts if "setTelemetry" in script]
        self.assertEqual(len(telemetry), 1)
        self.assertIn('"processed_fps": "240.8"', telemetry[0])

    def test_an_ordinary_line_does_not_touch_the_numbers(self) -> None:
        """每一行日志都过一次解析。误命中的话界面上会多一个假数字, 而假数字比
        横杠糟得多 —— 没人会怀疑它。"""
        api, window = self._api()
        api._push_log("正在加载模型和加速引擎...")
        self.assertEqual([s for s in self._scripts(window) if "setTelemetry" in s], [])

    def test_stopping_clears_the_numbers(self) -> None:
        """子进程没了之后, 状态栏还挂着最后一秒的帧率 —— 那是在撒谎。"""
        api, window = self._api()
        api._set_run_state(False)
        self.assertTrue(any("clearTelemetry" in s for s in self._scripts(window)))


def _form_api():
    """一个接好线的 Api, 外加它的 window 和 bridge。

    模块级而不是某个 TestCase 的方法: 后面几个任务 (算法、预设、算法库) 的测试
    类都要用同一套接线。各自复制一份的话, 改了一处忘了另一处 —— 而症状是「某个
    测试类的假设过时了」, 它照样绿。
    """
    from rhodes_fast.config import default_config
    from rhodes_fast.gui_core.state import config_to_form_state, default_labels, display_path
    from rhodes_fast.gui_web.app import Api
    from rhodes_fast.gui_web.forms import FormBridge

    window = Mock()
    api = Api()
    api._attach(window)
    bridge = FormBridge(
        config_to_form_state(
            default_config(), Path("C:/app"), default_labels(), display_path=display_path
        )
    )
    session = Mock()
    session.is_running = False
    # Mock 的方法默认返回 Mock, 而这几个的返回值是要被迭代、被判 None 的。不给
    # 默认值的话, 任何走到重载或者预设那条路的测试都会炸在「Mock 不可迭代」或者
    # 「已经有一个叫 <Mock ...> 的预设了」上 —— 跟它要测的东西毫无关系。
    session.library_rows.return_value = []
    session.reload_algorithms.return_value = []
    session.list_presets.return_value = []
    session.find_preset.return_value = None
    api._wire(
        session=session,
        relay=Mock(),
        config_path=Path("C:/app/settings.txt"),
        config=default_config(),
        bridge=bridge,
        labels=default_labels(),
        prompter=Mock(),
    )
    return api, window, bridge


def _scripts(window: Mock) -> list[str]:
    return [call.args[0] for call in window.evaluate_js.call_args_list]


def _payload(window: Mock, function: str) -> dict:
    """从 evaluate_js 的脚本里把某个 window.xxx(...) 的参数抠回来。

    按负载解码, 不做子串匹配: json.dumps 默认 ensure_ascii=True, 中文在脚本里
    是转义过的码位, 子串断言会红 —— 而那是测试的错不是代码的错。
    """
    script = next(s for s in _scripts(window) if function in s)
    return json.loads(script[script.index("(") + 1 : script.rindex(")")])


class FormApiTest(unittest.TestCase):
    """表单的真相在 Python 侧, JS 只是视图。"""

    def test_set_field_reaches_the_bridge(self) -> None:
        _api, _window, bridge = _form_api()
        _api.set_field("udp_host", "10.0.0.2")
        self.assertEqual(bridge.state.udp_host, "10.0.0.2")

    def test_a_typo_in_the_path_is_reported_not_swallowed(self) -> None:
        """路径是 JS 里手写的字符串。静默忽略的症状是「这个控件没用」, 没有任何
        线索指向拼写 —— 让它在运行状态里留一行。"""
        api, window, _bridge = _form_api()
        api.set_field("这个字段不存在", 1)
        # 按负载解码, 不做子串匹配: json.dumps 默认 ensure_ascii=True, 中文在
        # 脚本里是 \uXXXX 的转义。子串断言会红, 而那是测试的错不是代码的错。
        logged = [
            json.loads(script[script.index("(") + 1 : script.rindex(")")])
            for script in _scripts(window)
            if "appendLog" in script
        ]
        self.assertTrue(any("这个字段不存在" in line for line in logged))

    def test_a_typo_does_not_take_the_whole_call_down(self) -> None:
        """这是从 JS 调过来的。抛出去的话异常浮到 promise 上, 页面上多一条看不懂
        的报错, 而且那一次改动就丢了。"""
        api, _window, _bridge = _form_api()
        api.set_field("这个字段不存在", 1)

    def test_pushing_the_form_sends_json_not_a_concatenated_string(self) -> None:
        """模型路径里有反斜杠和引号。直接拼进 JS 字符串, 一个带引号的路径就能把
        脚本截断 —— 之后表单再也刷不新, 而且不报错。"""
        api, window, bridge = _form_api()
        bridge.set_field("model_path", 'C:\MODEL\a "b".onnx')
        window.evaluate_js.reset_mock()
        api._push_form()
        script = next(s for s in _scripts(window) if "setForm" in s)
        payload = json.loads(script[script.index("(") + 1 : script.rindex(")")])
        self.assertEqual(payload["model_path"], 'C:\MODEL\a "b".onnx')

    def test_the_choices_go_over_as_labels(self) -> None:
        api, window, _bridge = _form_api()
        window.evaluate_js.reset_mock()
        api._push_choices()
        script = next(s for s in _scripts(window) if "setChoices" in s)
        choices = json.loads(script[script.index("(") + 1 : script.rindex(")")])
        self.assertIn("provider", choices)
        self.assertNotIn("auto", choices["provider"])


class HotPushTest(unittest.TestCase):
    """边跑边调是这个界面存在的理由之一。"""

    def test_an_aim_field_is_pushed_to_a_running_pipeline(self) -> None:
        """滑条动了不热推的话, 用户要停一次再起一次才看得到效果 —— 而手感是要
        连着比的。"""
        api, _window, _bridge = _form_api()
        api._session.is_running = True
        with mock.patch.object(api, "_write_runtime_aim", autospec=True) as writer:
            api.set_field("profiles.0.kp_max", 0.2)
        writer.assert_called_once_with()

    def test_an_algorithm_parameter_is_pushed_too(self) -> None:
        """算法参数正是最需要边跑边调的那一批。"""
        from dataclasses import replace

        from rhodes_fast.gui_core.state import default_labels
        from rhodes_fast.gui_web.forms import param_payload

        api, _window, bridge = _form_api()
        label = next(l for l, n in default_labels().algorithm.items() if n == "feedforward")
        specs = param_payload(label, default_labels())
        profile = replace(
            bridge.state.profiles[0],
            algorithm=label,
            algorithm_params={s["name"]: s["default"] for s in specs},
        )
        bridge.replace_state(
            replace(bridge.state, profiles=(profile, bridge.state.profiles[1]))
        )
        api._session.is_running = True
        with mock.patch.object(api, "_write_runtime_aim", autospec=True) as writer:
            api.set_field(f"profiles.0.algorithm_params.{specs[0]['name']}", 0.5)
        writer.assert_called_once_with()

    def test_a_non_aim_field_does_not_touch_the_running_pipeline(self) -> None:
        """模型路径改了不该热推: 那份文件管线每帧读一次, 里面没有模型这一项。"""
        api, _window, _bridge = _form_api()
        api._session.is_running = True
        with mock.patch.object(api, "_write_runtime_aim", autospec=True) as writer:
            api.set_field("model_path", "MODEL/b.onnx")
        writer.assert_not_called()

    def test_nothing_is_written_when_the_pipeline_is_not_running(self) -> None:
        """没在跑的时候写那个文件是没用的, 而且下一次启动会先清掉它。"""
        with tempfile.TemporaryDirectory() as folder:
            api, _window, _bridge = _form_api()
            api._config_path = Path(folder) / "settings.txt"
            api._session.is_running = False
            api._write_runtime_aim()
            self.assertEqual(list(Path(folder).glob("**/*.aim.json")), [])

    def test_the_file_is_written_atomically(self) -> None:
        """管线每帧读一次。读到半截 JSON 就是一次解析失败 —— 照 gui.py 的做法,
        先写临时文件再 replace。"""
        with tempfile.TemporaryDirectory() as folder:
            api, _window, _bridge = _form_api()
            api._config_path = Path(folder) / "settings.txt"
            api._session.is_running = True
            api._write_runtime_aim()
            written = list(Path(folder).glob("**/*.aim.json"))
            self.assertEqual(len(written), 1)
            self.assertIn("profiles", json.loads(written[0].read_text(encoding="utf-8")))
            self.assertEqual(list(Path(folder).glob("**/*.tmp")), [], "临时文件没收掉")


def _with_feedforward(bridge, profile: int = 0):
    """把某一套方案换成有参数的算法。默认配置用的是比例控制, 而它一个参数都
    没有 —— 拿它测参数相关的东西全是空转。"""
    from dataclasses import replace

    from rhodes_fast.gui_core.state import default_labels
    from rhodes_fast.gui_web.forms import param_payload

    label = next(l for l, n in default_labels().algorithm.items() if n == "feedforward")
    specs = param_payload(label, default_labels())
    updated = replace(
        bridge.state.profiles[profile],
        algorithm=label,
        algorithm_params={spec["name"]: spec["default"] for spec in specs},
    )
    profiles = list(bridge.state.profiles)
    profiles[profile] = updated
    bridge.replace_state(replace(bridge.state, profiles=(profiles[0], profiles[1])))
    return label, specs


class AlgorithmSwitchTest(unittest.TestCase):
    def _label(self, stored: str) -> str:
        from rhodes_fast.gui_core.state import default_labels

        return next(l for l, n in default_labels().algorithm.items() if n == stored)

    def test_switching_replaces_the_parameters_wholesale(self) -> None:
        """每个算法的参数完全不同。换了算法还留着上一个的参数, 那些值会跟着
        保存进 settings.txt, 而新算法根本不认识它们。"""
        from rhodes_fast.gui_core.state import default_labels
        from rhodes_fast.gui_web.forms import param_payload

        api, _window, bridge = _form_api()
        _with_feedforward(bridge, 0)
        api.set_algorithm(0, self._label("windmouse"))
        self.assertEqual(
            set(bridge.state.profiles[0].algorithm_params),
            {s["name"] for s in param_payload(self._label("windmouse"), default_labels())},
        )

    def test_switching_resets_the_values_to_the_contract_defaults(self) -> None:
        from rhodes_fast.gui_core.state import default_labels
        from rhodes_fast.gui_web.forms import param_payload

        api, _window, bridge = _form_api()
        api.set_algorithm(1, self._label("feedforward"))
        for spec in param_payload(self._label("feedforward"), default_labels()):
            self.assertEqual(
                bridge.state.profiles[1].algorithm_params[spec["name"]], spec["default"]
            )

    def test_switching_pushes_the_new_controls_to_the_page(self) -> None:
        """参数控件是现建的。不推的话换完算法参数区还是上一个算法的。"""
        api, window, _bridge = _form_api()
        window.evaluate_js.reset_mock()
        api.set_algorithm(1, self._label("feedforward"))
        self.assertTrue(any("renderParams" in s for s in _scripts(window)))

    def test_switching_leaves_the_other_profile_alone(self) -> None:
        api, _window, bridge = _form_api()
        before = bridge.state.profiles[1].algorithm
        api.set_algorithm(0, self._label("windmouse"))
        self.assertEqual(bridge.state.profiles[1].algorithm, before)

    def test_switching_hot_pushes_to_a_running_pipeline(self) -> None:
        """换算法正是最想边跑边试的一下。"""
        api, _window, _bridge = _form_api()
        api._session.is_running = True
        with mock.patch.object(api, "_write_runtime_aim", autospec=True) as writer:
            api.set_algorithm(0, self._label("feedforward"))
        writer.assert_called_once_with()

    def test_a_bad_profile_index_does_nothing(self) -> None:
        """这是从 JS 调过来的。炸出去的话异常浮到 promise 上, 页面上多一条看不懂
        的报错。"""
        api, _window, bridge = _form_api()
        before = bridge.state.profiles[0].algorithm
        api.set_algorithm(7, self._label("feedforward"))
        self.assertEqual(bridge.state.profiles[0].algorithm, before)


class ParamPushTest(unittest.TestCase):
    def test_the_specs_and_the_current_values_both_go_over(self) -> None:
        """只推契约的话控件建出来是默认值, 用户存过的调校当场丢了。"""
        api, window, bridge = _form_api()
        _label, specs = _with_feedforward(bridge, 0)
        bridge.set_field(f"profiles.0.algorithm_params.{specs[0]['name']}", 0.77)
        window.evaluate_js.reset_mock()
        api._push_params(0)
        script = next(s for s in _scripts(window) if "renderParams" in s)
        body = script[script.index("(") + 1 : script.rindex(")")]
        index, rest = body.split(",", 1)
        pushed_specs, pushed_values = json.loads("[" + rest + "]")
        self.assertEqual(int(index), 0)
        self.assertEqual({s["name"] for s in pushed_specs}, {s["name"] for s in specs})
        self.assertEqual(pushed_values[specs[0]["name"]], 0.77)

    def test_the_target_class_options_cover_what_the_config_uses(self) -> None:
        """配置里用了 9 号类别而候选只到 6 的话, 那个下拉框会跳回 0 —— 而用户
        不会注意到, 直到发现自瞄锁错了东西。"""
        from dataclasses import replace

        api, window, bridge = _form_api()
        profiles = list(bridge.state.profiles)
        profiles[0] = replace(profiles[0], target_class="9")
        bridge.replace_state(replace(bridge.state, profiles=(profiles[0], profiles[1])))
        window.evaluate_js.reset_mock()
        api._push_choices()
        script = next(s for s in _scripts(window) if "setChoices" in s)
        choices = json.loads(script[script.index("(") + 1 : script.rindex(")")])
        self.assertIn("9", choices["target_class"])


class LibraryApiTest(unittest.TestCase):
    """03 算法库屏。业务动作一律转给 GuiSession —— 这边只管界面。"""

    def test_refresh_pushes_the_rows_to_the_page(self) -> None:
        api, window, _bridge = _form_api()
        api._session.library_rows.return_value = [
            ("比例控制", "p", "Endfield", "—", "—", "内置"),
            ("我的算法", "my_aim", "Jayzi", "my.py", "2026-09-01", "已导入"),
        ]
        window.evaluate_js.reset_mock()
        api.refresh_library()
        script = next(s for s in _scripts(window) if "setLibrary" in s)
        rows = json.loads(script[script.index("(") + 1 : script.rindex(")")])
        self.assertEqual(rows[1][1], "my_aim")
        self.assertEqual(rows[0][5], "内置")

    def test_the_rows_survive_json(self) -> None:
        """library_rows 返回的是元组。json 里元组变 list, 直接推过去再读回来
        就不是同一个东西了 —— 而 JS 那边按下标取列。"""
        api, window, _bridge = _form_api()
        api._session.library_rows.return_value = [("a", "b", "c", "d", "e", "内置")]
        window.evaluate_js.reset_mock()
        api.refresh_library()
        script = next(s for s in _scripts(window) if "setLibrary" in s)
        rows = json.loads(script[script.index("(") + 1 : script.rindex(")")])
        self.assertEqual(rows, [["a", "b", "c", "d", "e", "内置"]])

    def test_deleting_goes_through_the_session_which_asks_first(self) -> None:
        """问一句再删是 GuiSession.delete_algorithm 的职责 (它调 prompter)。
        界面再问一遍就是两个弹窗。"""
        api, _window, _bridge = _form_api()
        api._session.delete_algorithm.return_value = "我的算法"
        api.delete_algorithm("my_aim")
        api._session.delete_algorithm.assert_called_once_with("my_aim")

    def test_a_cancelled_delete_does_not_touch_anything(self) -> None:
        """delete_algorithm 取消时返回 None。照样往下走的话, 一次「取消」会
        触发一遍重载 + 三次整片推送。"""
        api, _window, _bridge = _form_api()
        api._session.delete_algorithm.return_value = None
        api.delete_algorithm("my_aim")
        api._session.reload_algorithms.assert_not_called()

    def test_renaming_that_was_cancelled_does_not_resync(self) -> None:
        api, _window, _bridge = _form_api()
        api._session.rename_algorithm.return_value = None
        api.rename_algorithm("my_aim")
        api._session.reload_algorithms.assert_not_called()

    def test_the_source_viewer_blocks_so_the_next_dialog_cannot_cover_it(self) -> None:
        """GuiSession._confirm_import 在两次确认之间同步调 show_source。"""
        api, _window, _bridge = _form_api()
        api._session.algorithm_source.return_value = ("my.py", "class Mine: pass")
        api.show_algorithm_source("my_aim")
        api._prompter.show_source.assert_called_once_with("my.py", "class Mine: pass")

    def test_a_builtin_has_no_source_and_that_is_not_an_error(self) -> None:
        """内置算法不在注册表里。界面上那个按钮对它本来就是灰的, 走到了也不该
        冒出一个从来没人见过的弹窗。"""
        api, _window, _bridge = _form_api()
        api._session.algorithm_source.return_value = None
        api.show_algorithm_source("p")
        api._prompter.show_source.assert_not_called()


class ResyncAlgorithmsTest(unittest.TestCase):
    """重载算法库之后, 两套方案的下拉框要跟上。"""

    def _label(self, api, stored: str) -> str:
        return next(l for l, n in api._labels.algorithm.items() if n == stored)

    def test_a_renamed_algorithm_keeps_its_tuning(self) -> None:
        """算法还在, 只是显示名改了。走 set_algorithm 的话参数会被重置成默认值
        —— 用户调了半天的手感会在一次「改名」之后无声地没掉。"""
        api, _window, bridge = _form_api()
        _label, specs = _with_feedforward(bridge, 0)
        bridge.set_field(f"profiles.0.algorithm_params.{specs[0]['name']}", 0.42)
        api._session.reload_algorithms.return_value = []
        api._resync_algorithms()
        self.assertEqual(bridge.state.profiles[0].algorithm_params[specs[0]["name"]], 0.42)

    def test_a_deleted_algorithm_falls_back_and_says_so(self) -> None:
        """正在用的算法被删掉了。悄悄换成别的会让手感莫名其妙变一个样。

        场景要模拟对: before 记的是**重载前的标识**, 而「消失」是重载之后
        algorithm_choices() 里不再有它。所以要在重载那一下换掉注册表的视图,
        不是一开始就塞一个无效的显示名进去 (那种情况 before 是 None, 照 gui.py
        的做法是跳过不动)。
        """
        api, window, bridge = _form_api()
        label, _specs = _with_feedforward(bridge, 0)
        self.assertEqual(bridge.state.profiles[0].algorithm, label)

        # 重载之后的算法表: 把 feedforward 拿掉, 留下比例控制。
        after = {l: n for l, n in api._labels.algorithm.items() if n != "feedforward"}
        api._session.reload_algorithms.return_value = []
        window.evaluate_js.reset_mock()
        with mock.patch("rhodes_fast.gui_web.app.algorithm_choices", return_value=after):
            api._resync_algorithms()

        expected = next(l for l, n in after.items() if n == "p")
        self.assertEqual(bridge.state.profiles[0].algorithm, expected)
        logged = [
            json.loads(s[s.index("(") + 1 : s.rindex(")")])
            for s in _scripts(window)
            if "appendLog" in s
        ]
        self.assertTrue(any("已改回比例控制" in line for line in logged))

    def test_loader_warnings_reach_the_runtime_log(self) -> None:
        """加载失败的算法只在日志里说一句 —— 弹窗会在开机和每次导入之后打断
        用户 (GuiSession.reload_algorithms 的 docstring 写了)。"""
        api, window, _bridge = _form_api()
        api._session.reload_algorithms.return_value = ["my_aim.py 加载失败：语法错误"]
        window.evaluate_js.reset_mock()
        api._resync_algorithms()
        logged = [
            json.loads(s[s.index("(") + 1 : s.rindex(")")])
            for s in _scripts(window)
            if "appendLog" in s
        ]
        self.assertTrue(any("语法错误" in line for line in logged))

    def test_the_algorithm_dropdowns_are_refreshed(self) -> None:
        """刚导入的算法要当场能选。列表刷了而下拉框没刷的话, 用户在算法库里
        看得见它, 回到控制方案却选不到 —— 只能以为坏了。"""
        api, window, _bridge = _form_api()
        api._session.reload_algorithms.return_value = []
        window.evaluate_js.reset_mock()
        api._resync_algorithms()
        scripts = _scripts(window)
        self.assertTrue(any("setChoices" in s for s in scripts))
        self.assertTrue(any("setLibrary" in s for s in scripts))

def _a_preset(model_path: str = "C:/app/MODEL/other.onnx", algorithm: str = "feedforward"):
    """一份跟默认配置不一样的预设。

    照抄一份默认值的话, 「载入之后表单变了没有」这条断言永远是真的 —— 它比较
    的两边本来就相等。
    """
    from dataclasses import replace

    from rhodes_fast.config import default_config
    from rhodes_fast.gui_core.state import algorithm_param_specs
    from rhodes_fast.presets import preset_from_config

    config = default_config()
    return preset_from_config(
        replace(
            config,
            model=replace(config.model, path=Path(model_path)),
            aim_profile_1=replace(
                config.aim_profile_1,
                algorithm=algorithm,
                # 参数按契约补齐, 别留空: 表单那边一定是补齐的, 留空的话预设跟
                # 表单从一开始就对不上, 「刚载入不该标成改过了」那条测试会因为
                # 夹具而红 —— 而那不是代码的错。真实的预设也总是补齐的, 它们
                # 都是从 _read_form_config() 存出去的。
                algorithm_params={s.name: s.default for s in algorithm_param_specs(algorithm)},
                kp_max=0.77,
            ),
        )
    )


class InitialStateTest(unittest.TestCase):
    """页面加载完之后那一次性的推送。"""

    def test_the_algorithm_library_is_filled_on_startup(self) -> None:
        """不推的话, 03 屏打开是一张只有表头的空表 —— 而算法明明都装着。用户
        只能以为算法库坏了, 或者自己去按一下刷新。端到端跑出来的。"""
        api, window, _bridge = _form_api()
        api._session.library_rows.return_value = [("比例控制", "p", "内置", "-", "-", "内置")]
        window.evaluate_js.reset_mock()
        api._push_initial_state()
        self.assertTrue(any("setLibrary" in script for script in _scripts(window)))


class PresetTest(unittest.TestCase):
    """预设条: 下拉 + 保存 + 另存为… + 删除。"""

    def _api(self):
        """接好线, 并且把「写 settings.txt」挡掉。

        _form_api 的配置路径是 C:/app/settings.txt, 那个目录不存在 —— 不挡的话
        每条预设测试都会真的去碰一次文件系统, 然后走进「设置无法保存」那条跟它
        要测的东西毫无关系的分支。写盘本身另有测试。
        """
        api, window, bridge = _form_api()
        patcher = mock.patch.object(api, "_save_settings", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return api, window, bridge

    def test_loading_a_preset_replaces_the_whole_form_in_one_go(self) -> None:
        """一个字段一个字段地填会让界面闪一串中间状态, 而且中途任何一步抛异常
        就停在半新半旧上 —— 那比什么都没发生更糟。"""
        api, window, _bridge = self._api()
        api._session.load_preset.return_value = _a_preset()
        window.evaluate_js.reset_mock()
        api.select_preset("我的预设")
        self.assertEqual(
            sum("setForm" in call.args[0] for call in window.evaluate_js.call_args_list), 1
        )

    def test_loading_a_preset_actually_fills_the_form(self) -> None:
        """「只推了一次」自己是满足不了的: 一次都不推也是一次都不多。"""
        api, _window, bridge = self._api()
        api._session.load_preset.return_value = _a_preset()
        api.select_preset("我的预设")
        self.assertEqual(bridge.state.model_path, "MODEL/other.onnx")
        self.assertAlmostEqual(bridge.state.profiles[0].kp_max, 0.77)

    def test_a_preset_that_cannot_be_read_leaves_the_form_alone(self) -> None:
        """load_preset 读不了会返回 None 并且自己弹过窗。这边再把表单清成默认值
        的话, 用户会丢掉手上正在调的东西。"""
        api, _window, bridge = self._api()
        before = bridge.state.model_path
        api._session.load_preset.return_value = None
        api.select_preset("坏掉的预设")
        self.assertEqual(bridge.state.model_path, before)

    def test_loading_a_preset_rebuilds_the_parameter_controls(self) -> None:
        """预设里的算法跟当前的不是同一个时, 参数控件整排都不一样。只推 setForm
        的话那几行还是上一个算法的 —— 用户在调一组新算法根本不认识的参数。"""
        api, window, _bridge = self._api()
        api._session.load_preset.return_value = _a_preset()
        window.evaluate_js.reset_mock()
        api.select_preset("我的预设")
        rendered = [s for s in _scripts(window) if "renderParams" in s]
        self.assertEqual(len(rendered), 2, "两套方案各建一次")

    def test_a_preset_is_refused_while_the_pipeline_runs(self) -> None:
        """模型要重启才换得了, 手感却是热切换的 —— 一半生效一半没生效最难排查。"""
        api, _window, bridge = self._api()
        api._session.is_running = True
        before = bridge.state.model_path
        api._session.load_preset.return_value = _a_preset()
        api.select_preset("我的预设")
        api._session.load_preset.assert_not_called()
        self.assertEqual(bridge.state.model_path, before)

    def test_cancelling_the_unsaved_changes_question_keeps_the_current_form(self) -> None:
        """当前预设有改动还没保存时切走, 先问一句。取消 = 留在当前预设 ——
        直接切过去的话那些改动就没了, 而且没有任何提示。"""
        api, _window, bridge = self._api()
        api._current_preset = "旧预设"
        api._preset_baseline = _a_preset()
        api._prompter.confirm_three_way = Mock(return_value=None)
        api._session.load_preset.return_value = _a_preset("C:/app/MODEL/yet-another.onnx")
        api.select_preset("新预设")
        api._session.load_preset.assert_not_called()
        self.assertEqual(api._current_preset, "旧预设")
        self.assertNotEqual(bridge.state.model_path, "MODEL/yet-another.onnx")

    def test_save_as_asks_for_a_name_and_stores_under_it(self) -> None:
        api, _window, _bridge = self._api()
        api._prompter.ask_text = Mock(return_value="新预设")
        api._session.store_preset.return_value = "新预设"
        api.save_preset_as()
        self.assertEqual(api._session.store_preset.call_args.args[0], "新预设")

    def test_cancelling_the_name_saves_nothing(self) -> None:
        """ask_text 取消返回 None。当成空名字存下去的话会冒出一个叫「」的预设。"""
        api, _window, _bridge = self._api()
        api._prompter.ask_text = Mock(return_value=None)
        api.save_preset_as()
        api._session.store_preset.assert_not_called()

    def test_an_illegal_name_never_reaches_the_disk(self) -> None:
        """预设名会变成文件名。斜杠会变成子目录, CON 是 Windows 的保留名 ——
        让 validate_name 先拦, 而不是等写盘时抛一个路径看不懂的 OSError。"""
        api, _window, _bridge = self._api()
        api._prompter.ask_text = Mock(return_value="a/b")
        api.save_preset_as()
        api._session.store_preset.assert_not_called()
        api._prompter.notify_error.assert_called()

    def test_overwriting_another_preset_asks_first(self) -> None:
        """同名的已经有一个了。不问就覆盖, 用户丢的是另一套调好的设置。"""
        api, _window, _bridge = self._api()
        api._prompter.ask_text = Mock(return_value="别人的预设")
        api._session.find_preset.return_value = "别人的预设"
        api._prompter.confirm = Mock(return_value=False)
        api.save_preset_as()
        api._session.store_preset.assert_not_called()
        self.assertIs(api._prompter.confirm.call_args.kwargs.get("danger"), True)

    def test_saving_over_the_current_preset_asks_nothing(self) -> None:
        """存回自己身上不是覆盖别人。每次保存都问一句的话, 那句话就没人看了。"""
        api, _window, _bridge = self._api()
        api._current_preset = "我的预设"
        api._session.store_preset.return_value = "我的预设"
        api.save_preset()
        api._prompter.ask_text.assert_not_called()
        self.assertEqual(api._session.store_preset.call_args.args[0], "我的预设")

    def test_saving_with_nothing_selected_falls_through_to_save_as(self) -> None:
        """没选预设时按「保存」, 存不进任何地方。静默什么都不做的话, 用户以为
        存上了。"""
        api, _window, _bridge = self._api()
        api._prompter.ask_text = Mock(return_value="第一个预设")
        api._session.store_preset.return_value = "第一个预设"
        api.save_preset()
        self.assertEqual(api._session.store_preset.call_args.args[0], "第一个预设")

    def test_deleting_forgets_the_current_preset(self) -> None:
        """问一句再删在 GuiSession 那边。删完这边还记着它的话, 下一次「保存」会
        往一个已经不存在的预设里写。"""
        api, _window, _bridge = self._api()
        api._current_preset = "我的预设"
        api._preset_baseline = _a_preset()
        api._session.delete_preset.return_value = True
        api.delete_preset()
        self.assertIsNone(api._current_preset)
        self.assertIsNone(api._preset_baseline)

    def test_cancelling_the_delete_keeps_it(self) -> None:
        api, _window, _bridge = self._api()
        api._current_preset = "我的预设"
        api._session.delete_preset.return_value = False
        api.delete_preset()
        self.assertEqual(api._current_preset, "我的预设")

    def test_the_list_goes_over_with_the_current_one_named(self) -> None:
        api, window, _bridge = self._api()
        api._session.list_presets.return_value = ["甲", "乙"]
        api._current_preset = "乙"
        window.evaluate_js.reset_mock()
        api.refresh_presets()
        payload = _payload(window, "setPresets")
        self.assertEqual(payload["names"], ["甲", "乙"])
        self.assertEqual(payload["current"], "乙")

    def test_an_edited_preset_is_marked_as_changed(self) -> None:
        """下拉框上那颗星。没有它, 用户关窗时不知道手上这套还没存进预设。"""
        api, window, _bridge = self._api()
        api._current_preset = "我的预设"
        api._preset_baseline = _a_preset()
        api.set_field("profiles.0.kp_max", 0.123)
        self.assertIs(_payload(window, "setPresets")["changed"], True)

    def test_an_untouched_preset_is_not_marked(self) -> None:
        """刚载入就标成「改过了」的话, 那颗星永远亮着, 等于没有。"""
        api, window, _bridge = self._api()
        api._session.load_preset.return_value = _a_preset()
        api.select_preset("我的预设")
        self.assertIs(_payload(window, "setPresets")["changed"], False)

    def test_the_marker_is_not_pushed_on_every_keystroke(self) -> None:
        """每动一下滑条就 evaluate_js 一次, 而那颗星一秒也变不了一次。只在翻面
        的时候推。"""
        api, window, _bridge = self._api()
        api._current_preset = "我的预设"
        api._preset_baseline = _a_preset()
        api.set_field("profiles.0.kp_max", 0.123)
        window.evaluate_js.reset_mock()
        for value in (0.124, 0.125, 0.126):
            api.set_field("profiles.0.kp_max", value)
        self.assertEqual([s for s in _scripts(window) if "setPresets" in s], [])

    def test_the_last_preset_comes_back_on_the_next_launch(self) -> None:
        """settings.txt 的 ui.preset 记着上次用的那个。不恢复的话, 每次打开都
        得自己再选一遍 —— 而界面上看不出现在这套设置是从哪来的。"""
        from dataclasses import replace as _replace

        api, _window, _bridge = self._api()
        api._config = _replace(api._config, ui=_replace(api._config.ui, preset="我的预设"))
        api._session.find_preset.return_value = "我的预设"
        api._session.read_preset_baseline.return_value = _a_preset()
        api._restore_last_preset()
        self.assertEqual(api._current_preset, "我的预设")

    def test_a_deleted_last_preset_is_forgotten_quietly(self) -> None:
        """用户自己删的, 不用在开机时提醒一遍。"""
        api, _window, _bridge = self._api()
        api._session.find_preset.return_value = None
        api._restore_last_preset()
        self.assertIsNone(api._current_preset)


class SaveSettingsTest(unittest.TestCase):
    """真的写 settings.txt 的那条路。上面那些测试把它挡掉了, 这里单独验。"""

    def test_it_writes_the_form_to_the_file(self) -> None:
        from rhodes_fast.config import load_config

        api, _window, bridge = _form_api()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.txt"
            # 模型文件要真的在: _save_settings 写完会照旧界面回读一次校验, 而那
            # 一次是带模型检查的。
            model = Path(folder) / "model.onnx"
            model.write_bytes(b"not really a model")
            api._config_path = path
            bridge.set_field("model_path", "model.onnx")
            bridge.set_field("udp_host", "10.0.0.9")
            self.assertIs(api._save_settings(quiet=True), True)
            self.assertEqual(load_config(path, validate_model=False).udp.host, "10.0.0.9")

    def test_a_failure_says_so_instead_of_disappearing(self) -> None:
        """写不了 (只读目录、路径没了) 而界面什么都不说的话, 用户关窗之后才发现
        设置没存上。"""
        api, _window, _bridge = _form_api()
        api._config_path = Path("Z:/这个盘不存在/settings.txt")
        self.assertIs(api._save_settings(quiet=True), False)
        api._prompter.notify_error.assert_called()

class ActionRowTest(LaunchHarness, unittest.TestCase):
    """动作行: 保存设置 / 测试输入 / 模型测速 / 管线测速 / 记录延迟日志。

    跟启停共用 LaunchHarness 的接线: 这几条走的是同一条 _launch, 只是 extra
    不同, 接线复制一份的话改了一处忘了另一处。
    """

    def _extra(self) -> list:
        return list(self.session.build_command.call_args.kwargs.get("extra", []))

    def test_the_form_is_written_to_settings_before_the_child_starts(self) -> None:
        """子进程读的是 settings.txt, 不是界面。不先存的话, 用户改完直接按启动,
        跑起来的是上一次存的那套设置 —— 而界面上明明是新的。"""
        api = self._api()
        self.bridge.set_field("udp_host", "10.9.9.9")
        seen: list[str] = []

        def spy(*_args, **_kwargs):
            from rhodes_fast.config import load_config

            seen.append(load_config(self.config_path, validate_model=False).udp.host)
            return Mock()

        self.session.start.side_effect = spy
        api.start()
        self.assertEqual(seen, ["10.9.9.9"])

    def test_nothing_starts_when_the_settings_cannot_be_saved(self) -> None:
        """端口打成了 "80a" —— 存不下去, 也就没有一份能跑的配置。照起的话子进程
        读的是上一次的设置, 而界面已经翻成「运行中」。"""
        api = self._api()
        self.bridge.set_field("udp_port", "80a")
        api.start()
        self.session.start.assert_not_called()
        self.assertNotIn("window.setRunState(true)", self._scripts())

    def test_check_passes_its_flag_verbatim(self) -> None:
        api = self._api()
        api.run_check()
        self.assertEqual(self._extra(), ["--check"])

    def test_the_model_benchmark_passes_its_count(self) -> None:
        api = self._api()
        api.run_benchmark()
        self.assertEqual(self._extra(), ["--benchmark", "200"])

    def test_the_pipeline_benchmark_passes_its_count(self) -> None:
        api = self._api()
        api.run_pipeline_benchmark()
        self.assertEqual(self._extra(), ["--pipeline-benchmark", "500"])

    def test_a_benchmark_never_binds_a_preview_socket(self) -> None:
        """基准测试那条路不建预览 (build_command 的 docstring)。绑了的话, 测速
        的数字里就掺进了一份没人看的渲染和 JPEG 编码 —— 而测速正是为了拿准数。"""
        for name in ("run_check", "run_benchmark", "run_pipeline_benchmark"):
            with self.subTest(name=name):
                api = self._api()
                getattr(api, name)()
                self.relay.start.assert_not_called()

    def test_a_benchmark_carries_no_preview_arguments(self) -> None:
        """build_command 里那四个是跟 preview_port 绑在一起给的。端口是 None 的话
        后面三个本来也进不去命令行 —— 但传过去就是在说「这条路要预览」, 下次有人
        改 build_command 时会当真。"""
        api = self._api()
        api.run_check()
        kwargs = self.session.build_command.call_args.kwargs
        for name in ("preview_port", "preview_enable_file", "trail_settings_file", "latency_log"):
            with self.subTest(name=name):
                self.assertIsNone(kwargs.get(name))

    def test_a_benchmark_is_refused_while_something_runs(self) -> None:
        api = self._api(running=True)
        api.run_benchmark()
        self.session.build_command.assert_not_called()

    def test_the_trail_settings_are_on_disk_before_the_child_starts(self) -> None:
        """管线一开始读到的就该是当前设置, 而不是默认值。晚一步的话, 勾着「轨迹」
        启动会先看到几秒没有轨迹的画面。"""
        api = self._api()
        self.bridge.set_field("trail_enabled", True)
        seen: list[bool] = []

        def spy(*_args, **_kwargs):
            import json as _json

            path = self.cache / f"webview-{os.getpid()}.trail.json"
            seen.append(path.exists() and _json.loads(path.read_text(encoding="utf-8"))["enabled"])
            return Mock()

        self.session.start.side_effect = spy
        api.start()
        self.assertEqual(seen, [True])

    def test_the_latency_log_is_only_created_when_asked(self) -> None:
        """每帧一行, 一直记的话是白白的磁盘写入 —— 而这台机器正在跑推理。"""
        api = self._api()
        api.start()
        self.assertIsNone(self.session.build_command.call_args.kwargs.get("latency_log"))

    def test_the_latency_log_goes_next_to_the_settings_file(self) -> None:
        """勾上了就得说出文件名: 记完不告诉用户记到哪了, 那份日志等于没有。"""
        api = self._api()
        self.bridge.set_field("latency_log_enabled", True)
        api.start()
        target = self.session.build_command.call_args.kwargs["latency_log"]
        self.assertEqual(target.parent, self.config_path.parent)
        self.assertTrue(target.name.startswith("latency-"), target.name)
        self.assertTrue(any(target.name in line for line in self._log_lines()))

    def test_saving_the_settings_says_so(self) -> None:
        """按了保存而界面一声不响的话, 用户会再按一次, 然后还是不确定。"""
        api = self._api()
        self.assertIs(api.save_settings(), True)
        self.assertIn("设置已保存。", self._log_lines())


class TrailControlTest(unittest.TestCase):
    """04 屏的控制栏: 画面 / 轨迹 / 最优路径 / 轨迹长度。"""

    def _api(self):
        api, window, bridge = _form_api()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        api._config_path = base / "settings.txt"
        return api, window, bridge, base

    def _trail(self, base: Path) -> dict:
        return json.loads(
            (base / ".cache" / f"webview-{os.getpid()}.trail.json").read_text(encoding="utf-8")
        )

    def test_a_trail_switch_reaches_the_running_pipeline(self) -> None:
        """勾了轨迹要立刻看见。要停一次再起一次的话, 这四个开关就没有意义了 ——
        它们存在的全部理由就是边跑边对比。"""
        api, _window, bridge, base = self._api()
        api._session.is_running = True
        api.set_field("trail_enabled", True)
        self.assertIs(self._trail(base)["enabled"], True)

    def test_the_frame_switch_goes_over_too(self) -> None:
        """只看轨迹不看画面是一种真实的用法 (轨迹在黑底上比压在画面上清楚)。"""
        api, _window, bridge, base = self._api()
        api._session.is_running = True
        api.set_field("preview_frame", False)
        self.assertIs(self._trail(base)["show_frame"], False)

    def test_the_length_is_clamped_and_rounded_before_it_goes_over(self) -> None:
        """滑条是连续的。1.2749 秒进设置文件没有意义, 而越界的值会让管线自己
        再夹一次 —— 两边各夹一次, 哪次说了算就看不出来了。"""
        api, _window, _bridge, base = self._api()
        api._session.is_running = True
        api.set_field("trail_seconds", 9.0)
        self.assertEqual(self._trail(base)["seconds"], 2.0)

    def test_nothing_is_written_when_nothing_runs(self) -> None:
        """没有子进程的时候写那个文件是纯粹的垃圾 —— 而下一轮启动前会先清掉它,
        所以连「提前写好」都算不上。"""
        api, _window, _bridge, base = self._api()
        api._session.is_running = False
        api.set_field("trail_enabled", True)
        self.assertFalse((base / ".cache" / f"webview-{os.getpid()}.trail.json").exists())

    def test_persisting_the_length_touches_only_the_length(self) -> None:
        """轨迹长度记住, 三个勾选框每次打开都回到默认 —— 轨迹是想看的时候才打开
        的东西, 上次忘了关的话下次一启动就在画一堆线。

        持久化只写 trail_seconds 一个字段: 走整份保存的话, 拖一下滑条就把表单上
        别的、用户还没决定保存的改动一起写进去了。
        """
        from rhodes_fast.config import load_config, save_config

        api, _window, bridge, base = self._api()
        save_config(api._config, api._config_path)
        bridge.set_field("udp_host", "10.8.8.8")
        # 1.25 不用: round() 在正好一半时按银行家舍入 (1.25 -> 1.2), 那条规则
        # 不是这个测试要钉的东西。
        bridge.set_field("trail_seconds", 1.27)
        bridge.set_field("trail_enabled", True)
        api.persist_trail_length()
        stored = load_config(api._config_path, validate_model=False)
        self.assertEqual(stored.ui.trail_seconds, 1.3)
        self.assertNotEqual(stored.udp.host, "10.8.8.8", "整份表单被顺手存下去了")


class StatusLineRoutingTest(unittest.TestCase):
    """每秒重复的两行不进日志, 改去刷新一条固定的状态行。

    一股脑往运行状态里堆的话, 一分钟就是 120 行数字, 而「模型已就绪」「KMBox
    连不上」这种只出现一次、真正要看的话早被顶出屏幕了。
    """

    RATE = (
        "采集= 241.0 帧/秒  处理= 240.8 帧/秒  推理=  2.31 毫秒  检测=  0.42 毫秒  "
        "检测= 3  自瞄标签=person  方案=1  移动=120  丢帧=  7"
    )
    LATENCY = (
        "接收端总延迟 平均= 2.45 毫秒 P95= 3.10 毫秒 | "
        "重组=0.11 解码=0.62 排队=0.04 预处理=0.28 推理=1.19 后处理=0.09 KMBox=0.12 毫秒"
    )

    def _api(self):
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api._attach(window)
        return api, window

    def test_the_repeating_lines_never_reach_the_log(self) -> None:
        api, window = self._api()
        api._push_log(self.RATE)
        api._push_log(self.LATENCY)
        self.assertEqual([s for s in _scripts(window) if "appendLog" in s], [])

    def test_they_go_to_the_fixed_line_instead(self) -> None:
        api, window = self._api()
        api._push_log(self.RATE)
        api._push_log(self.LATENCY)
        kinds = [
            json.loads(s[s.index("(") + 1 : s.index(",")])
            for s in _scripts(window)
            if "setStatusLine" in s
        ]
        self.assertEqual(kinds, ["rate", "latency"])

    def test_the_fixed_line_carries_the_original_text(self) -> None:
        """固定行上只放得下几个关键数字, 完整那两行要能看到 (页面把它们挂成
        悬停提示)。少送过去的话, 延迟的分项就彻底没了 —— 而这个项目的全部意义
        就在那几个数上。"""
        api, window = self._api()
        api._push_log(self.LATENCY)
        script = next(s for s in _scripts(window) if "setStatusLine" in s)
        self.assertEqual(json.loads(script[script.index(",") + 1 : script.rindex(")")]), self.LATENCY)

    def test_the_numbers_still_feed_the_rail_and_the_status_bar(self) -> None:
        """不进日志不等于不要数字。MetricRail 和状态栏本来就是从这两行填的。"""
        api, window = self._api()
        api._push_log(self.RATE)
        self.assertTrue(any("setTelemetry" in s for s in _scripts(window)))

    def test_an_ordinary_line_still_goes_to_the_log(self) -> None:
        api, window = self._api()
        api._push_log("正在加载模型和加速引擎...")
        self.assertTrue(any("appendLog" in s for s in _scripts(window)))

    def test_the_banners_still_go_to_the_log(self) -> None:
        """开机横幅只打一次, 而且是「现在跑的是哪个模型、哪个输入」的唯一记录。
        跟着周期行一起被拦掉的话, 运行状态里就再也没有这句话了。"""
        api, window = self._api()
        api._push_log("模型：ow2_v8s_320.onnx | 加速：TensorRT FP16")
        self.assertTrue(any("appendLog" in s for s in _scripts(window)))


def _idle_lamps() -> dict:
    """三盏灯没在跑的时候的样子。照 lamps.IDLE 取, 不在测试里抄一份 ——
    抄的那份跟实现分叉时, 这些测试只会更容易绿。"""
    from rhodes_fast.gui_web.lamps import IDLE

    return {name: dict(value) for name, value in IDLE.items()}


class LampPushTest(LaunchHarness, unittest.TestCase):
    """三盏灯要跟着日志走。原来是写死的标记, 从头到尾一个字不变。"""

    def _lamps(self) -> list[dict]:
        return [
            json.loads(s[s.index("(") + 1 : s.rindex(")")])
            for s in self._scripts()
            if "setLamps" in s
        ]

    def test_a_status_line_moves_the_lamps(self) -> None:
        api = self._api()
        api._push_log("模型：a.onnx | 加速：TensorRT FP16")
        self.assertEqual(self._lamps()[-1]["model"]["state"], "online")

    def test_an_ordinary_line_pushes_nothing(self) -> None:
        """每一行都过一遍。空更新也推的话, 是一秒好几次白跑的 evaluate_js。"""
        api = self._api()
        api._push_log("设置已保存。")
        self.assertEqual(self._lamps(), [])

    def test_a_disabled_box_never_lights_up(self) -> None:
        """表单说 KMBox 关着, 而管线那条横幅照打 —— 光看日志分不出来。"""
        api = self._api()
        self.bridge.set_field("kmbox_enabled", False)
        api._push_log("输入：UDP | 移动：KMBox 1.2.3.4:8808")
        self.assertEqual(self._lamps()[-1]["kmbox"]["state"], "standby")

    def test_an_enabled_box_lights_up(self) -> None:
        api = self._api()
        self.bridge.set_field("kmbox_enabled", True)
        api._push_log("输入：UDP | 移动：KMBox 1.2.3.4:8808")
        self.assertEqual(self._lamps()[-1]["kmbox"]["state"], "online")

    def test_sendinput_counts_as_enabled_even_with_the_box_switched_off(self) -> None:
        """kmbox.enabled 只管 KMBox。选了 SendInput 就是启用了移动输出 —— 拿 KMBox
        的开关去判, 灯会写「已禁用」而鼠标其实在动。"""
        api = self._api()
        self.bridge.set_field("kmbox_enabled", False)
        self.bridge.set_field("mouse_output", "本机 SendInput")
        api._push_log("输入：UDP | 移动：KMBox 1.2.3.4:8808")
        self.assertEqual(self._lamps()[-1]["kmbox"]["state"], "online")

    def test_stopping_a_run_puts_the_lamps_back(self) -> None:
        """管线没了还亮着「已连接」就是在撒谎。"""
        api = self._api()
        api.start()
        self.session.start.call_args.kwargs["on_exit"](0)
        self.assertEqual(self._lamps()[-1]["kmbox"]["state"], "off")

    def test_a_finished_check_keeps_its_verdict(self) -> None:
        """「测试输入」跑几秒就退出, 而它的结论正是用户按那个按钮要看的东西。
        跟普通运行一样清掉的话, 三盏灯会在结果出来的同一刻闪回「未验证」——
        那比没有反馈更气人。
        """
        api = self._api()
        api.run_check()
        api._push_log("KMBox 正常")
        self.session.start.call_args.kwargs["on_exit"](0)
        self.assertEqual(self._lamps()[-1]["kmbox"]["state"], "online")

    def test_a_check_after_a_run_still_resets_first(self) -> None:
        """先跑一轮再按测试输入: 上一轮留下的结论不能当成这一次的。

        断言的是「没有一盏还停在上一轮的 online 上」, 不是「最后一次推的正好是
        IDLE」—— 起子进程之后还会再推一次「正在启动」, 按后者写的话这条测试是在
        钉推送的条数, 而不是钉用户看到的东西。
        """
        api = self._api()
        api.start()
        api._push_log("模型：a.onnx | 加速：TensorRT FP16")
        self.session.start.call_args.kwargs["on_exit"](0)
        self.session.is_running = False
        api.run_check()
        self.assertNotIn("online", [lamp["state"] for lamp in self._lamps()[-1].values()])


class LogCollapseTest(unittest.TestCase):
    """运行日志能折起来, 把高度让给预览画面。"""

    def _api(self):
        api, window, bridge = _form_api()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        api._config_path = base / "settings.txt"
        from rhodes_fast.config import save_config

        save_config(api._config, api._config_path)
        return api, window, bridge

    def test_it_is_remembered(self) -> None:
        """折叠是为了腾地方。每次打开都要再折一次的话, 这个开关就没意义了。"""
        from rhodes_fast.config import load_config

        api, _window, _bridge = self._api()
        api.set_log_collapsed(True)
        self.assertIs(load_config(api._config_path, validate_model=False).ui.log_collapsed, True)

    def test_it_only_touches_that_one_field(self) -> None:
        """跟轨迹长度同一个道理: 走整份保存的话, 折一下日志就把表单上别的、
        用户还没决定保存的改动一起写进去了。"""
        from rhodes_fast.config import load_config

        api, _window, bridge = self._api()
        bridge.set_field("udp_host", "10.6.6.6")
        api.set_log_collapsed(True)
        self.assertNotEqual(
            load_config(api._config_path, validate_model=False).udp.host, "10.6.6.6"
        )

    def test_the_in_memory_config_keeps_up(self) -> None:
        """不同步的话, 下一次「保存设置」会拿一份旧的 base 去 replace, 把刚折起来
        的状态又写回展开。"""
        api, _window, _bridge = self._api()
        api.set_log_collapsed(True)
        self.assertIs(api._config.ui.log_collapsed, True)

    def test_saving_the_whole_form_does_not_unfold_it(self) -> None:
        """form_state_to_config 是在 base.ui 上 replace, 而 base 就是 api._config。
        上一条那个同步没做的话, 这一条会红 —— 而症状是「折了又自己弹回来」。"""
        from rhodes_fast.config import load_config

        api, _window, _bridge = self._api()
        api.set_log_collapsed(True)
        api.save_settings()
        self.assertIs(load_config(api._config_path, validate_model=False).ui.log_collapsed, True)

    def test_the_stored_state_reaches_the_page_on_startup(self) -> None:
        """记住了但开窗时不推, 等于没记住。"""
        from dataclasses import replace as _replace

        api, window, _bridge = self._api()
        api._config = _replace(api._config, ui=_replace(api._config.ui, log_collapsed=True))
        window.evaluate_js.reset_mock()
        api._push_initial_state()
        script = next(s for s in _scripts(window) if "setLogCollapsed" in s)
        self.assertIn("true", script)

    def test_a_failure_to_persist_does_not_take_the_window_down(self) -> None:
        """这是从 JS 调过来的。抛出去的话异常浮到 promise 上, 页面上多一条看不懂
        的报错 —— 而折叠这件事本身是成功的, 只是没存住。"""
        api, _window, _bridge = self._api()
        api._config_path = Path("Z:/这个盘不存在/settings.txt")
        api.set_log_collapsed(True)


class PopupApiTest(unittest.TestCase):
    """源码窗口和放大预览窗口, 从 Api 这一侧看。"""

    def _api(self):
        api, window, bridge = _form_api()
        popups = Mock()
        popups.preview_active = False
        api._popups = popups
        return api, window, popups

    def test_the_source_goes_to_its_own_window(self) -> None:
        """小屏下页面内的源码框太小, 下半截切掉还滚不动。"""
        api, _window, popups = self._api()
        api._show_source("my_aim.py", "class A: pass")
        popups.show_source.assert_called_once_with("my_aim.py", "class A: pass")
        api._prompter.show_source.assert_not_called()

    def test_a_window_that_will_not_open_falls_back_to_the_modal(self) -> None:
        """开窗失败的话, 导入流程里「先看清楚源码」那一步就没了 —— 而那一眼是
        那条流程存在的全部理由。退回页面内的框, 小是小了, 但看得到。"""
        api, _window, popups = self._api()
        popups.show_source.side_effect = RuntimeError("WebView2 抽风")
        api._show_source("my_aim.py", "class A: pass")
        api._prompter.show_source.assert_called_once_with("my_aim.py", "class A: pass")

    def test_the_preview_button_opens_the_popout(self) -> None:
        api, _window, popups = self._api()
        api.open_preview_window()
        popups.open_preview.assert_called_once_with()

    def test_a_popout_that_will_not_open_says_so(self) -> None:
        """这是从 JS 调过来的。抛出去的话异常浮到 promise 上, 页面上多一条看不懂
        的报错, 而按钮看起来就是没反应。"""
        api, window, popups = self._api()
        popups.open_preview.side_effect = RuntimeError("boom")
        api.open_preview_window()
        self.assertTrue(any("appendLog" in s for s in _scripts(window)))


class PopoutPreviewFlagTest(unittest.TestCase):
    """放大窗口开着的时候, 管线要一直出预览帧。"""

    def _api(self, *, popout_active: bool):
        from rhodes_fast.gui_web.app import Api

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.flag = Path(directory.name) / ".cache" / "webview.preview"
        api = Api()
        api._attach(Mock())
        popups = Mock()
        popups.preview_active = popout_active
        api._wire(
            session=Mock(), relay=Mock(), config_path=Path(directory.name) / "settings.txt",
            preview_enable_file=self.flag, popups=popups,
        )
        return api

    def test_leaving_the_preview_screen_does_not_freeze_the_popout(self) -> None:
        """原来切走 04 屏就把开关关了 (那时画面只在 04 上)。放大窗口开着的时候
        照旧关掉的话, 它会停在最后一帧上 —— 而且看起来跟「管线没发帧」一模一样。"""
        api = self._api(popout_active=True)
        api.set_preview_active(False)
        self.assertTrue(self.flag.exists())

    def test_minimising_the_main_window_does_not_freeze_the_popout(self) -> None:
        """放大预览多半就是为了把主窗口收起来单看画面。"""
        api = self._api(popout_active=True)
        api._on_minimized()
        self.assertTrue(self.flag.exists())

    def test_nobody_watching_still_turns_it_off(self) -> None:
        """两边都没人看的时候照旧不出帧: 这台副机同时在跑推理。"""
        api = self._api(popout_active=False)
        api.set_preview_active(False)
        self.assertFalse(self.flag.exists())
