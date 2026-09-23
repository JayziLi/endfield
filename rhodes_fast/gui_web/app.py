"""WebView 界面的入口: 起本地 server, 开一个无边框窗口指过去。

窗口是无边框的 —— 设计系统的 TitleBar / StatusBar 组件就是为这个做的, 它的
HANDOFF 写着「the app is frameless Windows software, so it must own its own
caption bar and footer」。代价是系统什么都不再给: 拖动、最小化、最大化、关闭
全要自己接, 那就是下面 Api 那三个方法和 Task 7 的 CSS 要干的活。

这个模块顶部没有 `import webview`, 那一行在 main() 里面。pywebview 是可选依赖
(pyproject 的 webview extra), 在第三份计划把默认入口切过来之前, 旧界面不该被迫
装它 —— 顶部 import 会让没装的检出连测试都跑不起来。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

from ..config import AppConfig, default_config, load_config, save_config
from ..gui_core.state import (
    algorithm_choices,
    apply_preset_to_state,
    display_path,
    form_state_to_config,
    resolve_model_path,
    runtime_aim_payload,
)
from ..gui_core.state import trail_settings_from_state
from ..presets import PresetError, preset_from_config, same_settings, validate_name
from ..trail import write_trail_settings
from .forms import UnknownField, choice_payload, param_payload
from .lamps import IDLE as IDLE_LAMPS
from .lamps import STARTING as STARTING_LAMPS
from .lamps import lamp_updates
from .prompts import ModalPrompter
from .telemetry import parse_telemetry, periodic_kind

WEB_ROOT = Path(__file__).resolve().parent / "web"


def _label_of(mapping: dict[str, str], stored: str) -> str:
    """存储值 → 显示标签。找不到就退回第一个候选, 跟 gui_core 的 _label 同一个
    兜底 —— 界面上那个下拉框必须落在某个真实选项上。"""
    return next((label for label, value in mapping.items() if value == stored),
                next(iter(mapping)))


def _cache_file(config_path: Path, suffix: str) -> Path:
    """这一轮子进程用的一个临时文件, 放在 settings.txt 旁边的 .cache 里。

    前缀是 webview- 而不是旧界面的 gui-: 两个界面完全可能同时开着, 同名的话
    一边的停止文件就成了另一边的停止文件, 谁按停止都是两个一起停。
    """
    return config_path.parent / ".cache" / f"webview-{os.getpid()}{suffix}"


def _load_or_create_config(config_path: Path) -> AppConfig:
    """Give the standalone WebView entry point the same first-run behavior as the CLI."""
    if not config_path.is_file():
        save_config(default_config(), config_path)
    return load_config(config_path, validate_model=False)


class Api:
    """暴露给 JS 的对象。

    pywebview 把它的**每一个**公开方法挂到 window.pywebview.api 上 (util.py 里
    那句 `if name.startswith("_"): continue`), 所以这里的公开方法名就是 JS 的
    调用面, 一个不多一个不少 —— 实测过: 一个叫 attach 的公开方法会跟三个窗口
    控制一起出现在 window.pywebview.api 里, 而 JS 传进来的参数是 JSON 对象,
    误调一次就把窗口引用换成了一个 dict, 之后三个按钮全哑。回填窗口因此用下划
    线开头的 _attach。

    分两步 (先构造, 再回填) 是因为两边互相要对方: create_window 需要 js_api,
    而 api 需要 window。
    """

    def __init__(self) -> None:
        self._window = None
        # 这四样由 _wire 回填。先在这里摆出来, 是为了「还没接上」是一个明确的
        # 状态 (四个 None) 而不是一个 AttributeError —— JS 那边完全可能在 main()
        # 串完之前就点了启动。
        self._session = None
        self._relay = None
        self._config_path: Path | None = None
        self._preview_enable_file: Path | None = None
        self._prompter = None
        self._bridge = None
        self._labels = None
        # 源码窗口和放大预览窗口。None = 开不了独立窗口 (测试里, 或者没装
        # pywebview), 那时源码退回页面内的框, 放大按钮什么都不做。
        self._popups = None
        # 预览要不要开, 是两件事的与: 用户在不在预览屏, 和窗口是不是最小化了。
        # 合成一个 bool 就会做错其中一件 —— 还原窗口时把停在算法库屏的人的预览
        # 一起打开, 或者最小化之后切回预览屏又把它打开。
        self._preview_wanted = False
        self._minimized = False
        # 预设条的三样状态。current 是「现在用的是哪个」, baseline 是它刚载入
        # 时的样子 (拿来算那颗「改过了」的星), dirty 是上一次推给 JS 的星的
        # 状态 —— 留着它才能只在翻面时推, 而不是每次 set_field 都推一遍。
        self._config: AppConfig | None = None
        self._current_preset: str | None = None
        self._preset_baseline = None
        self._preset_dirty = False
        # 上一次起子进程带的 extra。只用来分辨「这一轮是测试输入还是真跑」——
        # 那两者停下来之后, 三盏灯该不该清是相反的。
        self._last_arguments: list[str] = []

    def _attach(self, window) -> None:
        self._window = window

    def _wire(
        self,
        *,
        session,
        relay,
        config_path: Path,
        config: AppConfig | None = None,
        preview_enable_file: Path | None = None,
        prompter=None,
        bridge=None,
        labels=None,
        popups=None,
    ) -> None:
        """把业务侧的几件东西接上。只有 Python 调, 所以带下划线 —— JS 拿到它
        只会把 session 换成一个 JSON 对象。"""
        self._session = session
        self._relay = relay
        self._config_path = config_path
        # 「现在这份配置」。保存和另存为都要它当底 —— 界面上没有的字段 (aim 段的
        # 平滑、死区、限幅等十来项) 全靠它继承, 不留着的话每存一次就被打回默认值。
        self._config = config
        self._prompter = prompter
        self._bridge = bridge
        self._labels = labels
        self._popups = popups
        # Task 9 的预览开关要用它。启动时也得把这个路径写进子进程的命令行, 所以
        # 这里就要收下, 不能等到 Task 9 再加。
        self._preview_enable_file = preview_enable_file

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        """最大化 / 还原。

        用 toggle_fullscreen 而不是 maximize()+restore(), 理由是实测出来的: 对
        FormBorderStyle=None 的无边框窗口, WinForms 的 maximize() 也是铺满整块
        屏幕 (2560x1440), 跟 fullscreen 的结果一模一样 —— 两者都盖住任务栏,
        没有哪个更「正确」。那就选状态不会走偏的那个: is_fullscreen 由 pywebview
        自己记, 而 maximize/restore 得我们自己记一个 bool, 用户按 Win+↑ 时就对不
        上了 (那条系统快捷键在无边框窗口上照样有效)。
        """
        if self._window is not None:
            self._window.toggle_fullscreen()

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def answer_prompt(self, token: str, value=None) -> None:
        """JS 回答了一个模态框。公开的 —— 页面上那几个按钮就靠它。

        Python 那边正有一条线程阻塞在这个 token 上 (见 prompts.py 的
        docstring)。token 要原样送回来, 不然两个弹窗叠起来时答案会串。
        """
        if self._prompter is not None:
            self._prompter.answer(token, value)

    # ---- 启停。这三个是故意暴露给 JS 的, 页面上那个按钮就靠它们。 ----

    def is_running(self) -> bool:
        return self._session is not None and bool(self._session.is_running)

    def start(self) -> None:
        """起推理子进程, 带预览。"""
        self._launch([])

    def run_check(self) -> None:
        """测试输入: 连一下画面源和 KMBox, 报告通不通, 然后退出。对应 gui.py:635。"""
        self._launch(["--check"])

    def run_benchmark(self) -> None:
        """模型测速: 空跑 200 次推理。对应 gui.py:638。"""
        self._launch(["--benchmark", "200"])

    def run_pipeline_benchmark(self) -> None:
        """管线测速: 500 帧走完整条链路。对应 gui.py:641。"""
        self._launch(["--pipeline-benchmark", "500"])

    def save_settings(self) -> bool:
        """把表单写回 settings.txt。页面上那个「保存设置」按钮。"""
        return self._save_settings()

    def _launch(self, arguments: list[str]) -> None:
        """起子进程。照 gui.py 的 _launch —— 启动和三条测速走的是同一条路,
        差别只在 extra 和「建不建预览」。

        三条测速不建预览: build_command 的 docstring 写着基准测试那条路一个预览
        参数都不带。建了的话测出来的数字里掺进一份没人看的渲染和 JPEG 编码, 而
        测速正是为了拿准数。
        """
        if self._session is None or self._relay is None or self._config_path is None:
            return
        if self._session.is_running:
            # 放行就是第二个子进程去抢同一个 KMBox 和同一个预览端口。
            return
        # 先存再起: 子进程读的是 settings.txt, 不是界面。不先存的话, 用户改完
        # 直接按启动, 跑起来的是上一次存的那套设置 —— 而界面上明明是新的。
        # 存不下去 (端口打成了 "80a") 就别起: 那时根本没有一份能跑的配置。
        if not self._save_settings(quiet=True):
            return
        self._config_path.parent.joinpath(".cache").mkdir(parents=True, exist_ok=True)
        # 三盏灯归零。上一轮 (尤其是上一次「测试输入」) 留下的结论不能当成这一次的。
        self._reset_lamps()
        self._last_arguments = list(arguments)
        # 上一轮留下的停止文件会让新子进程一起来就自己收尾 (用户看到的是「点了
        # 启动, 闪一下就停了」)。照 gui.py:_launch, 起之前先清一遍。
        self._clear_temp_files()

        preview_port: int | None = None
        preview_enable_file: Path | None = None
        trail_settings_file: Path | None = None
        latency_log: Path | None = None
        if not arguments:
            # relay.start() 必须排在 build_command 前面: 端口是 bind 完才知道的,
            # 而它要写进子进程的命令行。反了的话命令行里只能是 None, 子进程一帧
            # 都不会发, 而界面这边什么错都看不到。可重入, 第二次启动沿用同一个
            # 端口。
            preview_port = self._relay.start()
            preview_enable_file = self._preview_enable_file
            trail_settings_file = _cache_file(self._config_path, ".trail.json")
            # 启动前就写好: 管线一开始读到的就是当前设置, 而不是默认值。晚一步的
            # 话, 勾着「轨迹」启动会先看到几秒没有轨迹的画面。
            self._write_trail_settings()
            latency_log = self._latency_log_path()

        stop_file = _cache_file(self._config_path, ".stop")
        command = self._session.build_command(
            config_path=self._config_path,
            stop_file=stop_file,
            runtime_aim_file=_cache_file(self._config_path, ".aim.json"),
            preview_port=preview_port,
            preview_enable_file=preview_enable_file,
            trail_settings_file=trail_settings_file,
            latency_log=latency_log,
            extra=arguments,
        )
        process = self._session.start(
            command,
            cwd=self._config_path.parent,
            on_line=self._push_log,
            on_exit=self._on_exit,
            # 漏了这一行, GuiSession.stop() 里 self._stop_file 就是 None, 停止
            # 文件根本不写: 子进程收不到「请收尾」的信号, 四秒后被 terminate
            # 硬杀 —— KMBox 不会正常关闭, 延迟日志也不落盘。
            stop_file=stop_file,
        )
        if process is None:
            # session 已经通过 prompter 把「无法启动」写进运行状态了, 这边只管
            # 把刚铺好的东西收掉。界面留在「未运行」: 按钮写着「停止」而根本
            # 没东西在跑, 比什么都不做更糟。
            self._clear_temp_files()
            return
        # 子进程起来了, 但第一条日志还要十几秒 (TensorRT 要编译引擎)。这段时间
        # 灯写着「未启动」是假的。
        self._reset_lamps(STARTING_LAMPS)
        if not arguments:
            # 上面那句 _clear_temp_files 把预览开关也清掉了。用户明明停在预览屏
            # 上, 一按启动开关就没了, 画面再也不来 —— 而且要切走再切回才会好。
            # 放回去。
            self._apply_preview_flag()
        self._set_run_state(True)

    def _latency_log_path(self) -> Path | None:
        """勾了「记录延迟日志」就给一个带时间戳的文件名。照 gui.py:1478-1482。

        每帧一行, 一直记的话是白白的磁盘写入, 而这台机器同时在跑推理 —— 所以
        它是个开关, 不是常开。记到哪了必须说出来: 不说的话那份日志等于没有。
        """
        if self._bridge is None or not self._bridge.state.latency_log_enabled:
            return None
        target = self._config_path.parent / f"latency-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        self._push_log(f"延迟日志将记录到 {target.name}（每帧都记，停止时给出估计）。")
        return target

    # 预览页那四个开关。改了要立刻传给正在跑的管线 —— 要停一次再起一次的话,
    # 这四个就没有意义了: 它们存在的全部理由就是边跑边对比。
    _TRAIL_FIELDS = frozenset({
        "preview_frame", "trail_enabled", "trail_optimal_path", "trail_seconds",
    })

    def _write_trail_settings(self) -> None:
        """把预览页那四个开关写给管线。照 gui.py 的 _write_trail_settings_file。"""
        if self._config_path is None or self._bridge is None:
            return
        try:
            write_trail_settings(
                _cache_file(self._config_path, ".trail.json"),
                trail_settings_from_state(self._bridge.state),
            )
        except OSError as error:
            self._push_log(f"轨迹设置没能传给运行中的程序：{error}")

    def set_log_collapsed(self, collapsed: bool) -> None:
        """运行日志折起来 / 展开。页面上那个小箭头就靠它。

        只写 ui.log_collapsed 一个字段, 照 persist_trail_length ——
        走整份保存的话, 折一下日志就把表单上别的、用户还没决定保存的改动一起写
        进去了。

        self._config 也要跟着更新: form_state_to_config 是在 base.ui 上 replace,
        而 base 就是它。不同步的话, 下一次「保存设置」会拿一份旧的 base 去写,
        把刚折起来的状态又弹回展开。
        """
        self._persist_ui(log_collapsed=bool(collapsed))

    def persist_trail_length(self) -> None:
        """只把轨迹长度写回 settings.txt。照 gui.py 的 _persist_trail_settings。

        JS 在滑条的 change 事件上调它 —— 那个事件松手才发一次, 正好是旧界面
        那个 400ms 防抖要的效果, 而且不用自己管一个定时器。

        不走整份保存: 那会把表单上别的、用户还没决定保存的改动一起写进去。
        """
        if self._config_path is None or self._bridge is None:
            return
        self._persist_ui(trail_seconds=trail_settings_from_state(self._bridge.state).seconds)

    def _persist_ui(self, **values) -> None:
        """只把 ui 段里指定的那几个字段写回 settings.txt。

        读一遍再写是故意的: 表单上别的改动此刻可能还没保存, 拿内存里那份整个写
        下去就把它们一起带进文件了。

        抛出去不行 —— 这几条都是从 JS 调过来的, 异常会浮到 promise 上, 页面上多
        一条看不懂的报错, 而用户看到的那个开关明明已经动了。
        """
        if self._config_path is None:
            return
        try:
            stored = load_config(self._config_path, validate_model=False)
            save_config(replace(stored, ui=replace(stored.ui, **values)), self._config_path)
        except (OSError, ValueError) as error:
            self._push_log(f"这个设置没能保存：{error}")
            return
        if self._config is not None:
            self._config = replace(self._config, ui=replace(self._config.ui, **values))

    # 这几个字段改了要热推给正在跑的管线。其余的 (模型路径、画面输入、KMBox)
    # 只在启动时读一次, 热推白写。
    _AIM_FIELDS = frozenset({
        "enabled", "trigger", "target_class", "aim_position",
        "fov", "kp_min", "kp_max", "kp_growth", "algorithm",
    })

    def set_field(self, path: str, value=None) -> None:
        """JS 那边动了一个控件。表单的真相在这边, 这是它唯一的入口。"""
        if self._bridge is None:
            return
        try:
            self._bridge.set_field(path, value)
        except UnknownField:
            # 静默忽略的症状是「这个控件没用」, 没有任何线索指向拼写。也不能
            # 抛出去: 这是从 JS 调过来的, 异常会浮到 promise 上, 页面上多一条
            # 看不懂的报错, 而且那一次改动就丢了。
            self._push_log(f"界面内部错误：表单里没有「{path}」这个字段。")
            return
        parts = path.split(".")
        if parts[0] == "profiles" and (
            parts[-1] in self._AIM_FIELDS or parts[2:3] == ["algorithm_params"]
        ):
            self._write_runtime_aim()
        # 预览页那四个开关改了要立刻传给管线。只在跑着的时候写: 没有子进程时那个
        # 文件是纯粹的垃圾, 而下一轮启动前会先清掉它。
        elif path in self._TRAIL_FIELDS and self._session is not None and self._session.is_running:
            self._write_trail_settings()
        self._refresh_preset_marker()

    def set_algorithm(self, profile: int, label: str) -> None:
        """换控制算法。

        参数要整批换掉 —— 每个算法的参数完全不同, 留着上一个的话那些值会跟着
        保存进 settings.txt, 而新算法根本不认识它们。所以这条不走 set_field。
        """
        if self._bridge is None or profile not in (0, 1):
            return
        specs = param_payload(label, self._labels)
        current = self._bridge.state.profiles[profile]
        updated = replace(
            current,
            algorithm=label,
            algorithm_params={spec["name"]: spec["default"] for spec in specs},
        )
        profiles = list(self._bridge.state.profiles)
        profiles[profile] = updated
        self._bridge.replace_state(
            replace(self._bridge.state, profiles=(profiles[0], profiles[1]))
        )
        self._push_params(profile)
        self._write_runtime_aim()

    def browse_model(self) -> None:
        r"""开 Windows 原生的文件对话框选模型。

        比 HTML 的 <input type=file> 好, 而且是唯一可行的: 后者只给一个 File
        对象和一个假路径 (C:\fakepath\...), 而我们要往 settings.txt 里写
        真实路径。
        """
        import webview

        if self._window is None or self._config_path is None:
            return
        selected = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=str(self._config_path.parent),
            file_types=("ONNX 模型 (*.onnx)", "所有文件 (*.*)"),
        )
        if not selected:
            return
        # 存相对路径 (能相对的话): settings.txt 跟着仓库走, 绝对路径换台机器就废了。
        self.set_field(
            "model_path", display_path(Path(selected[0]), self._config_path.parent)
        )
        self._push_form()

    # ---- 算法库。五个都是公开的, 03 屏那几个按钮就靠它们。 ----

    def refresh_library(self) -> None:
        """把算法库的行推给 JS。

        读的是注册表而不是已加载的那份 (GuiSession.library_rows 的 docstring
        说了为什么): 刚导入的算法要重启才会进 available_algorithms(), 但「装没
        装上」得当场看见 —— 用户点了导入、提示说成功了, 回头列表里一行没变的话,
        只能以为坏了。
        """
        if self._window is None or self._session is None:
            return
        rows = [list(row) for row in self._session.library_rows()]
        self._window.evaluate_js(f"window.setLibrary({json.dumps(rows)})")

    def import_algorithm(self) -> None:
        """选一个 .py 装进算法库。

        选文件在这边, 检查和安装在 GuiSession 那边 —— 它会先只读文本和 ast
        (那个文件到确认为止一行都没执行过), 再问用户, 中间给一条看源码的路。
        """
        import webview

        if self._window is None or self._session is None or self._config_path is None:
            return
        selected = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=str(self._config_path.parent),
            file_types=("Python 算法 (*.py)", "所有文件 (*.*)"),
        )
        if not selected:
            return
        installed = self._session.import_algorithm(
            Path(selected[0]),
            show_source=self._show_source,
            # 装好之后、弹「导入成功」之前刷列表 —— 顺序反了用户就会隔着弹窗
            # 盯着一份没变的列表。
            on_installed=lambda _entry: self.refresh_library(),
        )
        if installed is None:
            return
        self._resync_algorithms()

    def show_algorithm_source(self, name: str) -> None:
        """看一份已装算法的源码。内置算法不在注册表里, 安静地什么都不做 ——
        界面上那个按钮对它本来就是灰的。"""
        if self._session is None:
            return
        found = self._session.algorithm_source(name)
        if found is None:
            return
        self._show_source(*found)

    def rename_algorithm(self, name: str) -> None:
        """改显示名。问话在 GuiSession 那边 (它要把标识写进提示里)。"""
        if self._session is None:
            return
        if self._session.rename_algorithm(name) is None:
            return
        self._resync_algorithms()

    def delete_algorithm(self, name: str) -> None:
        """删一个已导入的算法。问一句再删是 GuiSession 的职责 —— 界面再问一遍
        就是两个弹窗。"""
        if self._session is None:
            return
        display_name = self._session.delete_algorithm(name)
        if display_name is None:
            return
        self._push_log(f"已删除算法「{display_name}」。")
        self._resync_algorithms()

    def _show_source(self, title: str, body: str) -> None:
        """给 GuiSession.import_algorithm 用的回调, 也给「查看源码」按钮用。

        阻塞到用户关掉为止 —— 导入那条路在两次确认之间同步调它。

        开一个自己的窗口, 不用页面内的框: 那个框最宽 560px、没有高度上限也没有
        滚动, 小屏下源码一长下半截就切掉了。独立窗口能拖大、能最大化。

        开不出来 (WebView2 抽风) 就退回页面内的框 —— 小是小了, 但「导入前先看
        清楚这份 .py」那一步不能因此没了。
        """
        if self._popups is not None:
            try:
                self._popups.show_source(title, body)
                return
            except Exception as error:  # noqa: BLE001 - 开窗失败的原因五花八门
                self._push_log(f"源码窗口没能打开，改在页面里显示：{error}")
        if self._prompter is not None:
            self._prompter.show_source(title, body)

    def open_preview_window(self) -> None:
        """把实时预览放大到一个独立的方形窗口。04 屏那个按钮就靠它。"""
        if self._popups is None:
            return
        try:
            self._popups.open_preview()
        except Exception as error:  # noqa: BLE001 - 这是从 JS 调过来的, 不能抛
            self._push_log(f"预览窗口没能打开：{error}")

    def _resync_algorithms(self) -> None:
        """重载算法库, 并让两套方案的下拉框跟上。照 gui.py 的 _reload_algorithm_library。

        重载前记下每套方案用的是哪个**标识**, 重载后按标识把显示名写回去。不这么
        做的话, 存的显示名会对不上任何一个选项 (改名改的正是显示名), 之后每次读
        表单都静默失败。

        正在用的算法被删掉时, 换回比例控制并且说一声 —— 悄悄换成别的会让手感
        莫名其妙变一个样。
        """
        if self._session is None or self._bridge is None or self._labels is None:
            return
        before = [
            self._labels.algorithm.get(profile.algorithm)
            for profile in self._bridge.state.profiles
        ]
        warnings = self._session.reload_algorithms()
        # 重载之后标签表整个变了, 重新取一份。
        self._labels = replace(self._labels, algorithm=algorithm_choices())
        known = set(self._labels.algorithm.values())

        for index, name in enumerate(before):
            if name is None:
                continue
            if name in known:
                # 算法还在, 只是显示名可能被改过。写回名字就行 —— 不能走
                # set_algorithm, 那一下会把参数重置成默认值, 用户调了半天的
                # 手感会在一次「改名」之后无声地没掉。
                self._bridge.set_field(
                    f"profiles.{index}.algorithm", _label_of(self._labels.algorithm, name)
                )
                continue
            # 正在用的算法被删掉了。换回比例控制, 并且说一声 —— 悄悄换成别的会让
            # 手感莫名其妙变一个样。这一支要重建参数, 所以走 set_algorithm。
            self.set_algorithm(index, _label_of(self._labels.algorithm, "p"))
            self._push_log(f"控制方案 {index + 1} 用的算法已被删除，已改回比例控制。")

        for warning in warnings:
            self._push_log(warning)
        self.refresh_library()
        self._push_choices()
        self._push_form()

    # ---- 预设。五个都是公开的, SectionHeader 右侧那条预设条就靠它们。 ----
    #
    # 放在 SectionHeader 而不是 TitleBar: 预设是跨面板的全局操作 (一份预设同时
    # 决定 01 和 02 两屏的值), 跟 SectionHeader 同属外壳; TitleBar 只有 44px,
    # 而且已经被三个窗口按钮占满了。

    def refresh_presets(self) -> None:
        """把预设列表推给 JS, 连同「现在用的是哪个」和「改过了没有」。"""
        if self._window is None or self._session is None:
            return
        changed = self._preset_changed()
        # None = 这会儿读不出表单 (数字框打到一半)。说不出口就别改口径 ——
        # 当成「没改」会把那颗星抹掉, 当成「改了」会凭空点亮它。
        if changed is not None:
            self._preset_dirty = changed
        payload = {
            "names": list(self._session.list_presets()),
            "current": self._current_preset,
            "changed": self._preset_dirty,
        }
        self._window.evaluate_js(f"window.setPresets({json.dumps(payload)})")

    def select_preset(self, name: str) -> None:
        """载入一份预设。照 gui.py 的 _load_preset + _fill_form。"""
        if self._session is None or self._bridge is None or self._config_path is None:
            return
        if self._session.is_running:
            # 模型要重启才换得了, 而手感是热切换的 —— 一半生效一半没生效最难排查。
            self._notify("info", "正在运行", "运行中不能载入预设，先停止再切换。")
            # 下拉框上的字已经被用户拨到新名字了, 拨回来。
            self.refresh_presets()
            return
        if self._preset_changed() is not False:
            answer = self._confirm_three_way(
                "切换预设",
                f"预设「{self._current_preset}」有改动还没保存。\n\n"
                "是：先存进这个预设再切换\n否：丢掉这些改动\n取消：留在当前预设",
            )
            if answer is None or (answer and not self.save_preset()):
                self.refresh_presets()
                return
        preset = self._session.load_preset(name)
        if preset is None:
            # load_preset 读不了会返回 None 并且自己弹过窗。这边再把表单清成默认
            # 值的话, 用户会丢掉手上正在调的东西。
            self.refresh_presets()
            return
        # 先复制再灌: apply_preset_to_state 是原地改的, 直接改 bridge.state 的话,
        # 中途任何一步抛异常就停在半新半旧上 —— 那比什么都没发生更糟。
        state = replace(self._bridge.state)
        apply_preset_to_state(
            state, preset, self._config_path.parent, self._labels, display_path=display_path
        )
        self._bridge.replace_state(state)
        self._current_preset = name
        self._preset_baseline = preset
        # 顺手记住, 下次打开程序还是这个预设。
        self._save_settings(quiet=True)
        self._push_form()
        # 参数控件是按算法现建的。预设换了算法而不重建的话, 那几行还是上一个算法
        # 的参数 —— 用户在调一组新算法根本不认识的旋钮。
        self._push_params(0)
        self._push_params(1)
        self.refresh_presets()
        self._push_log(f"已载入预设「{name}」。")

    def save_preset(self) -> bool:
        """存回当前预设。一个都没选时等于「另存为…」—— 静默什么都不做的话,
        用户以为存上了。"""
        if self._current_preset is None:
            return self.save_preset_as()
        return self._store_preset(self._current_preset)

    def save_preset_as(self) -> bool:
        """起个名字存一份。照 gui.py 的 _save_preset_as。"""
        if self._session is None or self._prompter is None:
            return False
        name = self._prompter.ask_text(
            "另存为预设", "给现在这套设置起个名字：", initial=self._current_preset or ""
        )
        if name is None:
            # 取消。当成空名字存下去的话会冒出一个叫「」的预设。
            return False
        try:
            name = validate_name(name)
        except PresetError as error:
            # 预设名会变成文件名: 斜杠会变成子目录, CON 是 Windows 的保留名。
            # 先拦下来, 而不是等写盘时抛一个路径看不懂的 OSError。
            self._notify("error", "这个名字不能用", str(error))
            return False
        existing = self._session.find_preset(name)
        if (
            existing is not None
            and existing != self._current_preset
            # 存回自己身上不是覆盖别人, 那种也问一句的话这句话就没人看了。
            # danger 换来警告图标和落在取消的默认按钮。
            and not self._prompter.confirm(
                "覆盖预设",
                f"已经有一个叫「{existing}」的预设了。\n\n要用现在的设置覆盖它吗？",
                danger=True,
            )
        ):
            return False
        return self._store_preset(name)

    def delete_preset(self) -> None:
        """删掉当前预设。问一句再删是 GuiSession 的职责 (那边传的是 danger=True),
        界面再问一遍就是两个弹窗。"""
        name = self._current_preset
        if self._session is None or name is None:
            return
        if not self._session.delete_preset(name):
            return
        # 删完还记着它的话, 下一次「保存」会往一个已经不存在的预设里写。
        self._current_preset = None
        self._preset_baseline = None
        self.refresh_presets()
        self._push_log(f"预设「{name}」已删除。")

    def _store_preset(self, name: str) -> bool:
        try:
            config = self._read_form_config()
        except (OSError, ValueError, KeyError) as error:
            # 端口打成了 "80a"、模型路径指着一个不存在的文件 —— 读表单这一步就
            # 会抛。跟旧界面走同一个弹窗。
            self._notify("error", "预设无法保存", str(error))
            return False
        stored = self._session.store_preset(name, config)
        if stored is None:
            return False
        self._current_preset = stored
        # 基准换成刚存下去的这一份, 那颗「改过了」的星才熄得掉。
        self._preset_baseline = preset_from_config(config)
        self._save_settings(quiet=True)
        self.refresh_presets()
        self._push_log(f"预设「{stored}」已保存。")
        return True

    def _preset_changed(self) -> bool | None:
        """当前表单跟当前预设还一不一样。None = 这会儿读不出来。

        没选预设时是 False 而不是 None: 那条路上没有「基准」这回事, 而调用方
        select_preset 会把 None 当成「说不准, 先问一句」—— 每次切预设都问一遍
        一个没有意义的问题。
        """
        if self._current_preset is None or self._preset_baseline is None:
            return False
        try:
            form = preset_from_config(self._read_form_config())
        except (OSError, ValueError, KeyError):
            return None
        # same_settings 而不是 ==: 框内位置界面上是百分比, 0.029 进界面再读回来是
        # 0.029000000000000005, 严格比的话刚载入的预设当场就被标成「有改动」。
        return not same_settings(form, self._preset_baseline)

    def _refresh_preset_marker(self) -> None:
        """下拉框上那颗「改过了」的星。每次 set_field 都过一遍。

        只在翻面的时候才真的推给 JS —— 每动一下滑条就 evaluate_js 一次的话,
        拖一次滑条就是几十次往返, 而那颗星一秒也变不了一次。
        """
        changed = self._preset_changed()
        if changed is None or changed == self._preset_dirty:
            return
        self.refresh_presets()

    def _restore_last_preset(self) -> None:
        """恢复上次用的预设。照 gui.py 的 _restore_last_preset。

        只认名字和基准, 不往表单里灌 —— 表单已经是从 settings.txt 读出来的了,
        而那份正是上次退出时这个预设的样子 (或者用户之后改过的样子)。再灌一遍
        会把用户没保存进预设、但保存进了设置的改动抹掉。

        被删了就安静地忘掉: 那是用户自己删的, 不用在开机时提醒一遍。
        """
        if self._session is None or self._config is None:
            return
        stored = self._config.ui.preset
        name = self._session.find_preset(stored) if stored else None
        if name is None:
            return
        try:
            # 模型不在也照样读 (read_preset_baseline 的 require_model=False):
            # 这份快照只拿来比对有没有改动, 不往表单里填。
            self._preset_baseline = self._session.read_preset_baseline(name)
            self._current_preset = name
        except PresetError as error:
            self._push_log(f"上次用的预设「{name}」读不了：{error}")

    # ---- 设置文件 ----

    def _read_form_config(self) -> "AppConfig":
        """表单 → 配置。对应 gui.py 的 _read_form。

        非法输入在这一步抛 ValueError, 由调用方接住弹窗 —— 跟旧界面同一条路。
        """
        return form_state_to_config(
            self._bridge.state,
            self._config,
            self._config_path.parent,
            self._labels,
            current_preset=self._current_preset,
            resolve_model_path=resolve_model_path,
        )

    def _save_settings(self, *, quiet: bool = False) -> bool:
        """把表单写回 settings.txt。照 gui.py 的 _save。

        catch Exception 是照抄的, 也是对的: 这一路要经过解析、写临时文件、
        回读校验, 抛什么的都有 —— 而一次存不上的设置绝不该把窗口带走。
        """
        if self._config is None or self._config_path is None or self._bridge is None:
            return False
        try:
            candidate = self._read_form_config()
            save_config(candidate, self._config_path)
            self._config = load_config(self._config_path)
        except Exception as error:
            self._notify("error", "设置无法保存", str(error))
            return False
        if not quiet:
            self._push_log("设置已保存。")
        return True

    def _notify(self, kind: str, title: str, body: str) -> None:
        """prompter 可能还没接上 (窗口建出来之前 JS 是调不到的, 但 Python 侧的
        开机流程会走到这里)。"""
        if self._prompter is not None:
            getattr(self._prompter, f"notify_{kind}")(title, body)

    def _confirm_three_way(self, title: str, body: str):
        """没有 prompter 时按「取消」放行 —— 没地方问, 就什么都别做。"""
        if self._prompter is None:
            return None
        return self._prompter.confirm_three_way(title, body)

    def set_preview_active(self, active: bool) -> None:
        """用户切进 / 切出预览屏。由 JS 的 section-changed 调过来。

        管线每帧看一眼这个文件在不在, 不在就跳过渲染和 JPEG 编码。不接这个开关
        的话, 不看预览时子进程照样在干这两件事 —— 而这台副机同时在跑推理。
        """
        self._preview_wanted = bool(active)
        self._apply_preview_flag()

    def stop(self) -> None:
        """请子进程停下来。不阻塞 —— GuiSession.stop() 写完停止文件就返回,
        等四秒和硬杀都在它自己起的后台线程上, 「真的停了」由 _on_exit 通知。
        """
        if self._session is None:
            return
        self._session.stop()
        # relay 不关。PreviewRelay.start() 是可重入的 (见 preview_relay.py),
        # 同一个 UDP 端口在整个进程生命周期里复用就行; 停一次关一次, 下一轮就是
        # 一个新端口, 白白多一次 bind。它归 main() 的 finally 关。

    # ---- 下面这些只有 Python 侧调, 所以一律下划线开头: 见类的 docstring。 ----

    def _push_log(self, line: str) -> None:
        """往运行状态里推一行。从读取线程调过来。

        json.dumps 不是洁癖 —— 日志里有中文、引号和 Windows 路径的反斜杠, 直接
        拼进 JS 字符串, 一个带引号的模型路径就能把 evaluate_js 当场截断。

        顺路再读一遍这一行: 管线每秒打两行数字 (帧率/耗时/目标数, 和延迟), 状态栏
        和 MetricRail 就是从这里填的。不接的话那几格永远是横杠 —— 而这个项目的
        全部意义就是那个延迟数。一秒两次正则, 在界面进程里, 跟推理那条线无关。

        那两行本身不进日志。一秒两行, 一分钟就是 120 行数字, 而「模型已就绪」
        「KMBox 连不上」这种只出现一次、真正要看的话早被顶出屏幕了。它们改去刷新
        页面上那条固定的状态行 —— 原文整句送过去, 固定行放不下的分项挂成悬停提示,
        一个数都不丢。
        """
        if self._window is None:
            return
        values = parse_telemetry(line)
        kind = periodic_kind(values)
        if kind is None:
            self._window.evaluate_js(f"window.appendLog({json.dumps(line)})")
        else:
            self._window.evaluate_js(
                f"window.setStatusLine({json.dumps(kind)}, {json.dumps(line)})"
            )
        if values:
            self._window.evaluate_js(f"window.setTelemetry({json.dumps(values)})")
        # 三盏灯也从同一行里读。空更新不推 —— 每行日志都推一次的话, 一秒好几次
        # 白跑的 evaluate_js, 而那三盏灯一秒也变不了一次。
        lamps = lamp_updates(line, output_enabled=self._output_enabled())
        if lamps:
            self._window.evaluate_js(f"window.setLamps({json.dumps(lamps)})")

    def _output_enabled(self) -> bool:
        """表单说移动输出启没启用。管线在 KMBox 关着的时候照样打那条横幅
        (connect() 直接返回), 光看日志分不出「连上了」和「压根没连」。

        选了 SendInput 就算启用, 不看 kmbox.enabled: 那个开关只管 KMBox。
        """
        if self._bridge is None:
            return False
        state = self._bridge.state
        mouse_output = self._labels.mouse_output.get(state.mouse_output) if self._labels is not None else None
        return mouse_output == "sendinput" or bool(state.kmbox_enabled)

    def _reset_lamps(self, lamps: dict | None = None) -> None:
        if self._window is not None:
            self._window.evaluate_js(
                f"window.setLamps({json.dumps(IDLE_LAMPS if lamps is None else lamps)})"
            )

    def _apply_preview_flag(self) -> None:
        """把「要不要发预览帧」落到那个文件上。

        parents=True 不能省: 切到预览屏可能发生在启动之前, 那时 .cache 还不存在,
        touch 会抛 FileNotFoundError —— 而这条路是从 JS 调过来的, 异常消失在
        pywebview 里, 界面什么都不会说, 只是预览永远不出画面。
        """
        if self._preview_enable_file is None:
            return
        # 两边任何一边有人在看就要出帧。放大窗口开着的时候照旧只看主窗口的话,
        # 用户一切走 04 屏或者把主窗口收起来 —— 而放大预览多半就是为了这么做 ——
        # 大窗口就停在最后一帧上, 看起来跟「管线没发帧」一模一样。
        popout = self._popups is not None and self._popups.preview_active
        if (self._preview_wanted and not self._minimized) or popout:
            self._preview_enable_file.parent.mkdir(parents=True, exist_ok=True)
            self._preview_enable_file.touch()
        else:
            self._preview_enable_file.unlink(missing_ok=True)

    def _on_minimized(self) -> None:
        """窗口最小化 —— 预览先关掉。

        最小化时浏览器会节流渲染, 消费端一慢, 旧帧就积在内核 socket 缓冲里
        (实测慢 6 倍时画面旧约 1.38 秒; FrameBus 确实只留一帧, 积的是内核那一段,
        调 SO_RCVBUF 无效)。与其给 socket 写加非阻塞 + 丢帧逻辑, 不如在这个唯一
        现实的触发场景直接让子进程别发 —— 顺手还省了副机的 CPU。
        """
        self._minimized = True
        self._apply_preview_flag()

    def _on_restored(self) -> None:
        self._minimized = False
        self._apply_preview_flag()

    def _bind_window_events(self, window) -> None:
        """pywebview 的 Event 用 += 挂回调 (webview/util.py 的 Event.__add__)。"""
        window.events.minimized += self._on_minimized
        window.events.restored += self._on_restored

    def _push_initial_state(self) -> None:
        """页面加载完之后把选项和表单一次性推过去。挂在 window.events.loaded 上。"""
        # 恢复要排在推之前: refresh_presets 推的是「现在用的是哪个」。
        self._restore_last_preset()
        self._push_choices()
        self._push_form()
        # 参数控件是现建的, 初次也要建一遍 —— 不然两套方案的参数区是空的。
        self._push_params(0)
        self._push_params(1)
        self.refresh_presets()
        # 算法库那张表也是现填的。不推的话 03 屏打开是一张只有表头的空表, 而
        # 算法明明都装着 —— 用户只能以为算法库坏了, 或者得自己先按一下刷新。
        self.refresh_library()
        # 日志折没折起来。记住了但开窗时不推, 等于没记住。
        if self._window is not None and self._config is not None:
            collapsed = json.dumps(bool(self._config.ui.log_collapsed))
            self._window.evaluate_js(f"window.setLogCollapsed({collapsed})")

    def _push_form(self) -> None:
        """把整份表单推给 JS。载入预设、切算法、启动时都走这条。"""
        if self._window is None or self._bridge is None:
            return
        self._window.evaluate_js(f"window.setForm({json.dumps(self._bridge.as_payload())})")

    def _push_choices(self) -> None:
        """每个下拉框的选项。导入算法之后要重推 —— 不重推的话算法库里看得见
        新算法, 回到控制方案却选不到。"""
        if self._window is None or self._labels is None:
            return
        choices = choice_payload(self._labels)
        if self._bridge is not None:
            # 目标标签的候选照旧界面: 至少 0-6, 配置里用了更大的就跟上。
            # 真正的类别名要等模型契约读出来, 那是另一条路。
            highest = max(int(p.target_class) for p in self._bridge.state.profiles)
            choices["target_class"] = [str(i) for i in range(max(7, highest + 1))]
        self._window.evaluate_js(f"window.setChoices({json.dumps(choices)})")

    def _push_params(self, profile: int) -> None:
        """把某一套方案的算法参数契约推给 JS, 由它建控件。"""
        if self._window is None or self._bridge is None:
            return
        label = self._bridge.state.profiles[profile].algorithm
        specs = param_payload(label, self._labels)
        values = dict(self._bridge.state.profiles[profile].algorithm_params)
        self._window.evaluate_js(
            f"window.renderParams({profile}, {json.dumps(specs)}, {json.dumps(values)})"
        )

    def _write_runtime_aim(self) -> None:
        """把瞄准设置热推给正在跑的管线。

        先写临时文件再 replace, 照 gui.py 的做法 —— 管线每帧读一次, 读到半截
        JSON 就是一次解析失败。
        """
        if (
            self._session is None
            or not self._session.is_running
            or self._config_path is None
            or self._bridge is None
        ):
            return
        values = runtime_aim_payload(self._bridge.state, self._labels)
        target = _cache_file(self._config_path, ".aim.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(values), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _set_run_state(self, running: bool) -> None:
        """翻界面上的「在不在跑」: 按钮文案、状态标签、那一块信号黄。"""
        if self._window is None:
            return
        self._window.evaluate_js(f"window.setRunState({json.dumps(running)})")
        if not running:
            # 子进程没了之后状态栏还挂着最后一秒的帧率 —— 那是在撒谎。
            self._window.evaluate_js("window.clearTelemetry()")

    def _on_exit(self, code: int) -> None:
        """子进程退出了。跟 _push_log 一样跑在读取线程上, 不在主线程。

        直接调 evaluate_js 是对的: 线程调度归 pywebview 自己做 (它把脚本递给
        WebView2 所在的 UI 线程), 这里不需要再排一次队。
        """
        self._push_log(f"已停止（退出码 {code}）。")
        self._clear_temp_files()
        # 「测试输入」跑几秒就退出, 而它的结论正是用户按那个按钮要看的东西 ——
        # 跟普通运行一样清掉的话, 三盏灯会在结果出来的同一刻闪回「未验证」,
        # 那比没有反馈更气人。普通运行停了就得还原: 管线没了还亮着「已连接」
        # 是在撒谎。
        if self._last_arguments != ["--check"]:
            self._reset_lamps()
        self._set_run_state(False)

    def _clear_temp_files(self) -> None:
        """把这一轮的临时文件收掉。

        停止文件留着, 下一轮子进程一起来就自己收尾; .aim.json / .trail.json
        留着只是垃圾。预览开关文件归 Task 9, 但管线停了之后它也没有意义了。
        """
        if self._config_path is None:
            return
        for suffix in (".stop", ".aim.json", ".trail.json"):
            _cache_file(self._config_path, suffix).unlink(missing_ok=True)
        if self._preview_enable_file is not None:
            self._preview_enable_file.unlink(missing_ok=True)


def main() -> None:
    import webview

    from ..gui_core.session import GuiSession
    from ..gui_core.state import config_to_form_state, default_labels
    from .forms import FormBridge
    from .popups import Popups
    from .preview_relay import FrameBus, PreviewRelay
    from .server import WebServer

    bus = FrameBus()
    # 中继先建起来但不 start: 端口是要写进推理子进程命令行的, 等 Api.start()
    # 真的去起管线时再绑。close() 在 start() 之前调用是安全的 (PreviewRelay 的
    # docstring 写了原因), 所以下面的 finally 不用分情况。
    relay = PreviewRelay(bus)
    server = WebServer(WEB_ROOT, bus=bus)
    server.start()

    api = Api()
    window = webview.create_window(
        "Endfield",
        server.url,
        js_api=api,
        frameless=True,
        # 整窗可拖会让用户想拖滑条时把窗口拖走 —— 开着的话 pywebview 给整个
        # window 挂一个 mousedown, 按在哪都能拖。
        #
        # 可拖区域改由 HTML 点名: 给元素加 class="pywebview-drag-region"
        # (pywebview 在页面加载时给这些元素挂 mousedown, 见 js/customize.js)。
        # 不是 CSS 的 -webkit-app-region —— 这台机器上实测过: WebView2 里
        # CSS.supports('-webkit-app-region','drag') 返回 true、computed 值也是
        # "drag", 但按住那块区域拖, 窗口纹丝不动 (位移 0,0); 同一次运行里换成
        # pywebview-drag-region, 窗口跟着走了 (位移 120,80)。app-region 是
        # Electron / PWA 窗口那一层实现的, WebView2 只是认得这个属性名。
        easy_drag=False,
        width=1280,
        height=800,
        min_size=(1024, 680),
        # 页面加载出来之前这块底色是系统画的。默认是纯白, 比设计系统的页面底色
        # (--surface-page = --paper-2) 亮一截, 开窗时会闪一下。
        background_color="#F1F1EB",
    )
    api._attach(window)
    # 窗口要先回填: prompter 的每一句话都经过 api._push_log 送到这个窗口上。
    # 配置路径照旧界面 (gui.py 的 main 传 Path("settings.txt"), RhodesFastGui
    # 再 resolve 一次): 工作目录下的 settings.txt, 绝对路径, 子进程的 cwd 就是
    # 它的父目录。
    config_path = Path("settings.txt").resolve()
    prompter = ModalPrompter(push=api._push_log)
    prompter.attach(window)
    session = GuiSession(config_path, prompter)
    # validate_model=False: 模型文件不在也要把界面画出来 —— 那正是用户要
    # 进来改路径的时候。旧界面同样的做法。
    labels = default_labels()
    config = _load_or_create_config(config_path)
    bridge = FormBridge(
        config_to_form_state(config, config_path.parent, labels, display_path=display_path)
    )
    # 两种独立窗口 (源码、放大预览)。放大窗口开关、最小化都要去翻管线的预览开关。
    popups = Popups(webview.create_window, server.url, on_change=api._apply_preview_flag)
    api._wire(
        session=session,
        relay=relay,
        config_path=config_path,
        config=config,
        popups=popups,
        preview_enable_file=_cache_file(config_path, ".preview"),
        prompter=prompter,
        bridge=bridge,
        labels=labels,
    )
    api._bind_window_events(window)
    # 页面加载完才推: window.setForm / setChoices 是 forms.js 定义的, 早一步推
    # 过去就是一个 ReferenceError —— 而它消失在 evaluate_js 里, 界面只是空着。
    window.events.loaded += api._push_initial_state
    # webview.start() 要等**所有**窗口都关了才返回。主窗口关了而放大预览还开着的话,
    # start() 一直不返回, 下面 finally 里的停管线也就一直不执行 —— 留下一个孤零零的
    # 预览窗口, 和一条还在跑、还占着 KMBox 的推理。所以主窗口一关就连带关掉它们。
    # 走 api._popups 而不是直接挂 popups.close_all: 测试里要能把它换掉。
    window.events.closed += lambda: api._popups is not None and api._popups.close_all()
    try:
        # 阻塞到最后一个窗口销毁为止。用户点自己画的关闭按钮 (api.close →
        # window.destroy) 和走系统的关闭路径 (Alt+F4 / WM_CLOSE) 都收在这里。
        webview.start()
    finally:
        # 挂着的问题先放行。窗口已经没了, 那几条线程会各自等满五分钟的超时,
        # 而进程正在等它们 —— 表现是「点了关闭, 窗口没了, 进程还在」。
        prompter.cancel_all()
        # 子进程先停: 窗口已经没了, 再没人读得到它的日志, 也再没人停得了它 ——
        # 留下来就是个孤儿, 还占着 KMBox 和鼠标。这一下不阻塞 (写完停止文件就
        # 返回, 等待和硬杀在 GuiSession 自己的后台线程上), 所以不会把下面这条
        # 收尾路径堵住; 进程退出后子进程读到停止文件会自己收尾。
        api.stop()
        # 余下三个的顺序是死的, 反了就是一条泄漏的线程。
        # relay 先关: 它还在往 bus 上推帧, 反过来的话中间那段时间白收。
        # bus 再关: /preview.mjpg 那条响应线程阻塞在 bus.subscribe() 里, 而它是
        #   daemon —— server.stop() 里的 _threads.join() 对 daemon 线程是空操作
        #   (见 server.py 的 stop() docstring), 叫不动它, 只有关掉总线它才会自己
        #   结束迭代。
        # server 最后: 到这时已经没有谁还挂在它的响应线程里了。
        relay.close()
        bus.close()
        server.stop()
