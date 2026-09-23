"""界面的业务动作: 预设、算法库、子进程。不碰控件, 不碰 tkinter。

要问用户的地方一律走 self.prompter (见 prompts.py), 要往运行状态里写字的地方
一律交回给调用方 —— 怎么显示是界面的事。

本模块只管「文件里有什么」和「要不要问一句」。「现在选中的是哪个预设」
(current_preset)、「表单跟文件比有没有改动」(_preset_baseline) 这类界面状态
留在 gui.py: 它们随控件一起活着, 搬进来只会多一份要同步的副本。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from ..aim_algorithms import available_algorithms, set_installed_algorithms
from ..algorithm_library import (
    Candidate,
    DuplicateAlgorithm,
    InstalledAlgorithm,
    LibraryError,
    inspect_candidate,
    install,
    load_installed,
    read_registry,
    rename,
    uninstall,
)
from ..config import AppConfig
from ..presets import (
    DIRECTORY_NAME as PRESETS_DIRECTORY,
    Preset,
    PresetError,
    delete_preset as remove_preset_file,
    find_preset as find_preset_file,
    list_presets as list_preset_files,
    preset_from_config,
    read_preset,
    write_preset,
)
from .prompts import Prompter


class GuiSession:
    """一次界面会话要做的事: 读写预设、管算法库、起停子进程。

    config_path 是 settings.txt 的位置; 预设和算法都放在它旁边, 跟 gui.py
    现在的算法一致。
    """

    def __init__(self, config_path: Path, prompter: Prompter) -> None:
        self.config_path = config_path
        self.prompter = prompter          # 公开: 测试要读它的记录
        self.base_dir = config_path.parent
        self.presets_dir = self.base_dir / PRESETS_DIRECTORY
        self.algorithms_dir = self.base_dir / "algorithms"
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        # stop() 要 touch 的那个文件。由 start() 记下来: 哪个停止文件对应哪个
        # 子进程是启动那一刻才确定的, 而 stop() 不该再去猜一遍。
        self._stop_file: Path | None = None

    # ---- 预设 ----

    def list_presets(self) -> list[str]:
        return list_preset_files(self.presets_dir)

    def find_preset(self, name: str) -> str | None:
        """按名字找一份已存在的预设, 不分大小写。找不到返回 None。"""
        return find_preset_file(self.presets_dir, name)

    def load_preset(self, name: str) -> Preset | None:
        """读一份要往表单里填的预设。读不了就弹窗说明, 返回 None。

        带上 known_algorithms: 预设指着一个没装的算法时宁可拒绝, 也不能静默
        换成默认算法 —— 用户会以为在用这份预设, 实际手感完全是另一回事。
        """
        try:
            return read_preset(
                self.presets_dir,
                name,
                base_directory=self.base_dir,
                known_algorithms=set(available_algorithms()),
            )
        except PresetError as error:
            self.prompter.notify_error("载入预设失败", str(error))
            return None

    def read_preset_baseline(self, name: str) -> Preset:
        """读一份只拿来比对「有没有改动」的预设快照。读不了就抛 PresetError。

        跟 load_preset 是两条路, 三处不同都是故意的, 别合并:
        1. require_model=False —— 这份快照不往表单里填, 模型文件在不在无所谓;
        2. 不传 known_algorithms —— 开机恢复时一份用着未安装算法的预设仍要认得出来;
        3. 抛异常而不是弹窗 —— 开机时读不了只往运行状态里写一行, 不打断用户。
        """
        return read_preset(
            self.presets_dir, name, base_directory=self.base_dir, require_model=False
        )

    def store_preset(self, name: str, config: AppConfig) -> str | None:
        """把一份已经读好的配置存成预设, 返回实际存下的名字; 存不了返回 None。

        收 AppConfig 而不是自己去读表单: 读表单是界面的事, 而且那一步抛的
        TclError 只有界面接得住。
        """
        try:
            return write_preset(
                self.presets_dir,
                name,
                preset_from_config(config),
                base_directory=self.base_dir,
            )
        except (OSError, ValueError) as error:
            # PresetError 是 ValueError 的子类, 名字不合法和模型不在都走这里。
            self.prompter.notify_error("预设无法保存", str(error))
            return None

    def delete_preset(self, name: str) -> bool:
        """问一句再删。删掉了返回 True, 用户取消或删不掉返回 False。"""
        if not self.prompter.confirm(
            "删除预设",
            f"确定删除预设「{name}」吗？\n\n删除后找不回来。界面上现在的设置不会变。",
            # 破坏性操作: tkinter 那边据此补上警告图标, 并把默认按钮挪到取消,
            # 手滑按回车删不掉。
            danger=True,
        ):
            return False
        try:
            remove_preset_file(self.presets_dir, name)
        except OSError as error:
            self.prompter.notify_error("删除失败", str(error))
            return False
        return True

    # ---- 算法库 ----

    def reload_algorithms(self) -> list[str]:
        """重新读一遍算法库, 换掉进程里那份已加载的算法表, 返回加载失败的说明。

        返回警告而不是自己弹窗: 加载失败今天只往运行日志里写一行, 弹窗会在开机
        和每次导入之后打断用户。怎么显示是界面的事。
        """
        installed, warnings = load_installed(self.algorithms_dir)
        set_installed_algorithms(installed)
        return warnings

    def library_rows(self) -> list[tuple[str, str, str, str, str, str]]:
        """内置 ∪ 注册表。每行是 显示名 / 标识 / 作者 / 源文件 / 导入时间 / 状态。

        刚导入的算法还不在 available_algorithms() 里——那份是启动时加载的, 要重启
        才会变。但「装没装上」得当场看见: 用户点了导入、提示说成功了, 回头列表里
        一行没变的话, 只能以为坏了。所以列表读注册表, 不读已加载的那份。
        """
        installed = read_registry(self.algorithms_dir)
        loaded = available_algorithms()
        rows: list[tuple[str, str, str, str, str, str]] = []
        for name in sorted(set(loaded) | set(installed)):
            entry = installed.get(name)
            if entry is None:
                rows.append((loaded[name].DISPLAY_NAME, name, "Endfield", "—", "—", "内置"))
            else:
                rows.append(
                    (
                        entry.display_name,
                        name,
                        entry.author,
                        entry.source_file,
                        entry.imported_at or "—",
                        "已导入",
                    )
                )
        return rows

    def import_algorithm(
        self,
        source: Path,
        *,
        show_source: Callable[[str, str], None],
        on_installed: Callable[[InstalledAlgorithm], None] | None = None,
    ) -> InstalledAlgorithm | None:
        """把一个已经选好的 .py 装进算法库。装上了返回注册表里那一行, 否则 None。

        选文件留在界面那边: 这里收的是一个路径, 不是一个文件对话框。

        show_source 也是界面的: 确认框里「是」那个按钮是查看源码, 而源码窗口是
        个 Toplevel。on_installed 在装好之后、弹「导入成功」之前调用, 界面拿它
        重载列表 —— 顺序反了用户就会隔着弹窗盯着一份没变的列表。
        """
        # 第一步只读文本和 ast。这个文件到这里为止一行都没有执行过。
        try:
            candidate = inspect_candidate(source)
        except LibraryError as error:
            self.prompter.notify_error("导入失败", str(error))
            return None
        if not candidate.name:
            self.prompter.notify_error("导入失败", "源码里找不到带 NAME 的算法类。")
            return None
        if not self._confirm_import(candidate, show_source=show_source):
            return None
        return self._install_candidate(
            candidate, replace_existing=False, on_installed=on_installed
        )

    def _confirm_import(
        self, candidate: Candidate, *, show_source: Callable[[str, str], None]
    ) -> bool:
        """问「要不要导入」, 顺带给一条看源码的路。看完了再问一遍。

        三个按钮不是「是 / 否 / 取消 = 导入 / 不导入 / 不导入」: 是 = 先看源码,
        否 = 直接导入, 取消 = 放弃。文案里写清楚了, 别按直觉改。
        """
        while True:
            message = "\n".join(
                [
                    "确定要导入这个算法吗？",
                    "",
                    f"文件：{candidate.path.name}（{candidate.size_bytes / 1024:.1f} KB）",
                    f"作者：{candidate.author}",
                    f"标识：{candidate.name}",
                    f"显示名：{candidate.display_name}",
                    "",
                    "算法是一段会在你机器上运行的 Python 代码。只导入你信得过的来源。",
                    "",
                    "选「是」查看源码，选「否」直接导入，选「取消」放弃。",
                ]
            )
            answer = self.prompter.confirm_three_way("导入算法", message)
            if answer is None:
                return False
            if not answer:
                return True
            show_source(candidate.path.name, candidate.source)

    def _install_candidate(
        self,
        candidate: Candidate,
        *,
        replace_existing: bool,
        on_installed: Callable[[InstalledAlgorithm], None] | None,
    ) -> InstalledAlgorithm | None:
        try:
            entry = install(
                self.algorithms_dir, candidate.path, replace_existing=replace_existing
            )
        except DuplicateAlgorithm as clash:
            # 覆盖确实是破坏性的, 但今天这个框没传 icon=WARNING / default=CANCEL,
            # 补上 danger=True 会换掉图标、把默认按钮挪走, 用户看得见。
            if self.prompter.confirm(
                "已有同名算法",
                f"已存在同名算法「{clash.name}」（来自 {clash.existing_file}）。要替换吗？\n\n"
                "替换会保留你给它起的显示名。",
            ):
                return self._install_candidate(
                    candidate, replace_existing=True, on_installed=on_installed
                )
            return None
        except LibraryError as error:
            self.prompter.notify_error("导入失败", str(error))
            return None
        if on_installed is not None:
            on_installed(entry)
        self.prompter.notify_info(
            "导入成功",
            f"「{entry.display_name}」已装进算法库，现在就能在控制方案里选它。",
        )
        return entry

    def algorithm_source(self, name: str) -> tuple[str, str] | None:
        """读一份已装算法的源码, 返回 (源文件名, 源码)。读不了就弹窗, 返回 None。

        带上文件名是因为源码窗口的标题就是它; 只返回正文的话界面还得自己去翻
        一次注册表。内置算法不在注册表里, 安静地返回 None —— 界面上那个按钮
        对它本来就是灰的。
        """
        entry = read_registry(self.algorithms_dir).get(name)
        if entry is None:
            return None
        path = self.algorithms_dir / entry.source_file
        try:
            return entry.source_file, path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self.prompter.notify_error("打不开源码", str(error))
            return None

    def rename_algorithm(self, name: str) -> str | None:
        """问一个新显示名再改。返回注册表实际存下的那个名字, 取消或失败返回 None。

        返回改后的名字而不是 True: 界面要拿它写日志, 而 rename 会去掉首尾空格,
        用户打进去的那个跟存下的不是一回事。
        """
        entry = read_registry(self.algorithms_dir).get(name)
        if entry is None:
            return None
        new_name = self.prompter.ask_text(
            "重命名算法",
            f"给「{entry.display_name}」起个新的显示名。\n\n"
            f"标识 {entry.name} 不会变——别人发来的调校认的是标识。",
            initial=entry.display_name,
        )
        if new_name is None:
            return None
        try:
            rename(self.algorithms_dir, entry.name, new_name)
        except LibraryError as error:
            self.prompter.notify_error("改名失败", str(error))
            return None
        return new_name.strip()

    def delete_algorithm(self, name: str) -> str | None:
        """问一句再删。删掉了返回它的显示名, 取消或删不掉返回 None。

        同样返回名字而不是 True: 删完注册表里就没这一行了, 界面再想拿显示名
        写日志已经晚了。

        内置算法不在注册表里, 查不到就安静返回 —— 今天界面上那两个按钮对内置
        算法是灰的, 这条路走不到; 走到了也不该冒出一个从来没人见过的弹窗。
        """
        entry = read_registry(self.algorithms_dir).get(name)
        if entry is None:
            return None
        # 这个框今天没传 icon=WARNING / default=CANCEL (删预设那个传了), 所以
        # 不给 danger —— 补上就是换图标 + 挪默认按钮, 是可见的差异。
        if not self.prompter.confirm(
            "删除算法",
            f"要删掉「{entry.display_name}」吗？\n\n"
            f"{entry.source_file} 会被删除。正指着它的控制方案会当场改回比例控制。",
        ):
            return None
        try:
            uninstall(self.algorithms_dir, entry.name)
        except LibraryError as error:
            self.prompter.notify_error("删除失败", str(error))
            return None
        return entry.display_name

    # ---- 子进程 ----

    def build_command(
        self,
        *,
        config_path: Path,
        stop_file: Path,
        runtime_aim_file: Path,
        preview_port: int | None = None,
        preview_enable_file: Path | None = None,
        trail_settings_file: Path | None = None,
        latency_log: Path | None = None,
        extra: Sequence[str] = (),
    ) -> list[str]:
        """组子进程的命令行。纯函数: 不碰文件系统, 不碰进程。

        -u 不能掉: 子进程的输出要实时流进运行状态框, 有缓冲就成了一坨一坨地出,
        用户会以为卡死了。

        解释器要 python.exe 而不是 sys.executable 本身: 界面自己可能跑在
        pythonw.exe 下, 那时 sys.executable 是个没有控制台的解释器, 起出来的
        子进程连 stdout 都没有, 日志一行也读不到。

        预览的三个开关跟 preview_port 绑在一起给: 基准测试那条路 (gui.py 的
        _launch 收到 arguments) 不建预览 socket, 也就一个预览参数都不带。
        延迟日志今天只在这条路上给, 所以一起放在里面。

        路径收的是「已经算好的」: 预览端口是 gui.py 那边 bind 完的真端口,
        延迟日志的文件名带时间戳, 都不是这里能自己算出来的。
        """
        python = Path(sys.executable).with_name("python.exe")
        command = [
            str(python),
            "-u",
            "-m",
            "rhodes_fast",
            "--config",
            str(config_path),
            "--stop-file",
            str(stop_file),
            "--runtime-aim-file",
            str(runtime_aim_file),
        ]
        if preview_port is not None:
            command.extend(["--preview-port", str(preview_port)])
            if preview_enable_file is not None:
                command.extend(["--preview-enable-file", str(preview_enable_file)])
            if trail_settings_file is not None:
                command.extend(["--trail-settings-file", str(trail_settings_file)])
            if latency_log is not None:
                command.extend(["--latency-log", str(latency_log)])
        command.extend(extra)
        return command

    @property
    def is_running(self) -> bool:
        return self._process is not None

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
        stop_file: Path | None = None,
    ) -> subprocess.Popen[str] | None:
        """起子进程并把它的输出一行行喂给 on_line。起不来就弹窗, 返回 None。

        返回 Popen 而不是 True: 调用方要拿着它自己那一份引用。子进程可能在
        本函数返回之前就跑完了 (--check 几十毫秒就结束), 那时 self._process
        已经被读取线程清空, 调用方再回头问一次会拿到 None, 以为压根没起来。

        PYTHONUTF8=1 和 errors="replace" 都不能掉: 中文日志在 GBK 控制台上会炸,
        而一行解不出来的字节不该让整个读取线程死掉, 后面的日志还得流进来。

        on_line / on_exit 在读取线程上调用, 不在调用方的线程上。tkinter 那边
        靠它们往队列里塞, 再由 root.after 在主线程上取 —— 控件只能主线程碰。
        """
        if self._process is not None:
            return None
        # Windows 上不给子进程弹一个黑框出来。
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
                env=environment,
            )
        except Exception as exc:  # noqa: BLE001 - 起不来的原因五花八门, 一律报给用户
            self.prompter.notify_error("无法启动", str(exc))
            return None
        self._process = process
        self._stop_file = stop_file

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                on_line(line.rstrip())
            code = process.wait()
            self._process = None
            on_exit(code)

        self._reader = threading.Thread(target=pump, daemon=True)
        self._reader.start()
        return process

    def stop(self) -> None:
        """请子进程停下来。照 gui.py 的 _stop 搬。

        先写停止文件让管线自己收尾 (关 KMBox、把延迟日志落盘), 等四秒还没退
        才 terminate, 再等一秒才 kill。直接杀会丢掉这些收尾动作。

        不阻塞: 调用方是个按钮回调, 在这里等四秒就把界面冻住了。等待和硬杀
        都交给一个后台线程, 「真的停了」靠 on_exit 回来的那一下通知。
        """
        process = self._process
        if process is None:
            return
        if self._stop_file is not None:
            self._stop_file.touch()

        def force_stop() -> None:
            try:
                process.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()

        threading.Thread(target=force_stop, daemon=True).start()
