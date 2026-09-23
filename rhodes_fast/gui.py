from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from .aim_algorithms import Param, available_algorithms
from .config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS, AppConfig, load_config, save_config
from .gui_core.session import GuiSession
from .gui_core.state import (
    INPUT_MODES,
    LOG_LANGUAGES,
    OUTPUT_FORMATS,
    PROVIDERS,
    TRIGGERS,
    FormState,
    Labels,
    ProfileFormState,
    algorithm_choices,
    algorithm_param_specs,
    apply_preset_to_state,
    config_to_form_state,
    default_labels,
    display_path,
    form_state_to_config,
    resolve_model_path,
    runtime_aim_payload,
    trail_settings_from_state,
)
from .latency_log import MEASUREMENT_NAME, load_measurement
from .presets import (
    Preset,
    PresetError,
    preset_from_config,
    same_settings,
    validate_name,
)
from .trail import TrailSettings, write_trail_settings
from .tuning_share import Tuning, TuningError, delay_warning, dump_tuning, load_tuning


# 下拉框里显示「有改动没存进预设」的节拍。只读一遍表单, 300 毫秒足够跟手又不占事。
PRESET_POLL_MS = 300
NO_PRESET = "（未选择预设）"
# 停手这么久之后才把轨迹长度写回 settings.txt。
TRAIL_PERSIST_DELAY_MS = 400
NOTHING_TO_PREVIEW = "勾选「画面」或「轨迹」后在这里显示"


def _display_value(mapping: dict[str, str], stored: str) -> str:
    return next((label for label, value in mapping.items() if value == stored), next(iter(mapping)))


# 这两个搬去了 gui_core.state (WebView 那条路也要用, 而那边碰不到本模块)。
# 留成本模块的名字: 界面内部和测试有十几处在用 _display_path / _resolve_model_path。
_display_path = display_path
_resolve_model_path = resolve_model_path


# 预览取帧的节拍。管线按 30fps 发, 这里 16 毫秒取一次(约 60Hz), 每一帧都能赶上
# 自己那一拍。取帧慢于发帧的话画面步长会忽长忽短, 看着就是一顿一顿的。
PREVIEW_POLL_MS = 16
# 没在看预览时没必要 60Hz 空转, 但定时器链不能断。
PREVIEW_IDLE_POLL_MS = 200


def fit_preview_to_canvas(frame: np.ndarray, canvas_width: int, canvas_height: int) -> np.ndarray:
    """等比缩放到画布内并转成 RGB。

    缩放用 cv2 而不是 PIL: 实测 320->400 的双线性放大, PIL 要 1.38 毫秒, cv2 只要
    0.26 毫秒。这段跑在 Tk 主线程上, 而 Tk 和瞄准管线共用同一台机器。
    """
    height, width = frame.shape[:2]
    scale = min(canvas_width / width, canvas_height / height)
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    if target != (width, height):
        frame = cv2.resize(frame, target, interpolation=cv2.INTER_LINEAR)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


class TkPrompter:
    """Prompter 的 tkinter 实现。文案由调用方给, 这里只负责用哪个弹窗。

    一律带 parent: 少了它弹窗会挂到根窗口之外, 可能被主窗口盖住。
    """

    def __init__(self, root: tk.Misc) -> None:
        self._root = root

    def notify_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self._root)

    def notify_warning(self, title: str, message: str) -> None:
        messagebox.showwarning(title, message, parent=self._root)

    def notify_info(self, title: str, message: str) -> None:
        messagebox.showinfo(title, message, parent=self._root)

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        # danger 补的两样都不能省: 警告图标是一眼能看见的差别, 而默认按钮落在
        # 取消是防手滑 —— 少了它回车就把预设删了。
        extra = {"icon": messagebox.WARNING, "default": messagebox.CANCEL} if danger else {}
        return bool(messagebox.askokcancel(title, message, parent=self._root, **extra))

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        return messagebox.askyesnocancel(title, message, parent=self._root)

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        return simpledialog.askstring(title, message, initialvalue=initial, parent=self._root)


class RhodesFastGui:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.config = load_config(self.config_path, validate_model=False)
        self.root = tk.Tk()
        self.root.title("Endfield")
        self.root.geometry("860x800")
        self.root.minsize(800, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.process: subprocess.Popen[str] | None = None
        self.preview_socket: socket.socket | None = None
        self.preview_frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=1)
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.stop_requested = False
        self.model_contract = None
        self.stop_file = self.config_path.parent / ".cache" / f"gui-{os.getpid()}.stop"
        self.preview_enable_file = self.config_path.parent / ".cache" / f"gui-{os.getpid()}.preview"
        self.runtime_aim_file = self.config_path.parent / ".cache" / f"gui-{os.getpid()}.aim.json"
        self.trail_settings_file = self.config_path.parent / ".cache" / f"gui-{os.getpid()}.trail.json"
        self._trail_persist_job: str | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        # 碰文件系统和「问用户」的事都归 session, 这边只留控件。
        self.prompter = TkPrompter(self.root)
        self.session = GuiSession(self.config_path, self.prompter)
        # 下拉框要能列出用户自己装的算法, 所以界面一起来就得加载一次算法库。
        # 必须排在 _create_variables 之前: 算法下拉框的选项就是这一步的结果。
        self.library_warnings = self.session.reload_algorithms()
        self.current_preset: str | None = None
        # 预设文件里存的那份, 表单和它比才知道有没有改动。
        self._preset_baseline: Preset | None = None
        self._build_style()
        self._create_variables()
        self._build_ui()
        self._switch_input_panel()
        self._restore_last_preset()
        if self.config.model.path.is_file():
            self.root.after(120, lambda: self._inspect_selected_model(self.config.model.path))
        self.root.after(80, self._drain_messages)
        self.root.after(PREVIEW_POLL_MS, self._drain_preview)
        self.root.after(PRESET_POLL_MS, self._watch_preset)

    @property
    def algorithms_dir(self) -> Path:
        """算法库目录只有 session 那一份。

        界面这边留个别名而不是自己再算一次 config_path.parent / "algorithms":
        两份相等但独立的值, 谁改了另一份都不知道, 而测试要的正是把整个算法库
        指到临时目录去 —— 所以下面的 setter 也得有。
        """
        return self.session.algorithms_dir

    @algorithms_dir.setter
    def algorithms_dir(self, directory: Path) -> None:
        self.session.algorithms_dir = directory

    def run(self) -> None:
        self.root.mainloop()

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", foreground="#17202a", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 20), foreground="#111827")
        style.configure("Subtle.TLabel", foreground="#667085")
        style.configure("TLabelframe", background="#f4f6f8", bordercolor="#cfd6df", relief="solid")
        style.configure("TLabelframe.Label", background="#f4f6f8", foreground="#344054", font=("Segoe UI Semibold", 10))
        # 蓝边蓝标题: 控制算法是方案里最该先看到的一项, 底色保持一致免得内部控件露出色差。
        style.configure("Focus.TLabelframe", background="#f4f6f8", bordercolor="#1677ff")
        style.configure(
            "Focus.TLabelframe.Label",
            background="#f4f6f8",
            foreground="#1677ff",
            font=("Segoe UI Semibold", 10),
        )
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#1677ff", foreground="white")
        style.map("Accent.TButton", background=[("active", "#095ec9"), ("disabled", "#a6c8f5")])
        style.configure("Stop.TButton", foreground="#b42318")

    def _labels(self) -> Labels:
        """六张 {显示标签: 存储值} 映射, 打包给 gui_core。

        映射本身住在 gui_core.state —— 两个界面共用一份。这里留一层薄包装, 是
        因为界面内部有十几处调 self._labels()。
        """
        return default_labels()

    def _create_variables(self) -> None:
        # 取哪个值、怎么换算全归 gui_core.state 管, 这里只负责把它装进 tk 变量。
        # 滑条旁边那些 *_text 不在 FormState 里 —— 它们是数值字段的格式化结果,
        # 位数照旧写死在这里。
        state = config_to_form_state(
            self.config, self.config_path.parent, self._labels(), display_path=_display_path
        )
        self.model_path = tk.StringVar(value=state.model_path)
        self.provider = tk.StringVar(value=state.provider)
        self.cuda_graph = tk.BooleanVar(value=state.cuda_graph)
        self.gpu_preprocess = tk.BooleanVar(value=state.gpu_preprocess)
        self.output_format = tk.StringVar(value=state.output_format)
        self.confidence = tk.DoubleVar(value=state.confidence)
        self.confidence_text = tk.StringVar(value=f"{state.confidence:.3f}")
        self.iou = tk.DoubleVar(value=state.iou)
        self.iou_text = tk.StringVar(value=f"{state.iou:.3f}")
        self.input_mode = tk.StringVar(value=state.input_mode)
        self.log_language = tk.StringVar(value=state.log_language)
        self.udp_host = tk.StringVar(value=state.udp_host)
        self.udp_port = tk.StringVar(value=state.udp_port)
        self.udp_width = tk.StringVar(value=state.udp_width)
        self.udp_height = tk.StringVar(value=state.udp_height)
        self.obs_host = tk.StringVar(value=state.obs_host)
        self.obs_port = tk.StringVar(value=state.obs_port)
        self.obs_password = tk.StringVar(value=state.obs_password)
        self.obs_source = tk.StringVar(value=state.obs_source)
        self.kmbox_enabled = tk.BooleanVar(value=state.kmbox_enabled)
        self.latency_log_enabled = tk.BooleanVar(value=state.latency_log_enabled)
        # 预览页的勾选框每次打开都是「只看画面」, 不从设置里恢复; 只有轨迹长度记住。
        self.preview_frame = tk.BooleanVar(value=state.preview_frame)
        self.trail_enabled = tk.BooleanVar(value=state.trail_enabled)
        self.trail_optimal_path = tk.BooleanVar(value=state.trail_optimal_path)
        self.trail_seconds = tk.DoubleVar(value=state.trail_seconds)
        self.kmbox_host = tk.StringVar(value=state.kmbox_host)
        self.kmbox_port = tk.StringVar(value=state.kmbox_port)
        self.kmbox_uuid = tk.StringVar(value=state.kmbox_uuid)
        self.profile_enabled = [tk.BooleanVar(value=profile.enabled) for profile in state.profiles]
        self.profile_trigger = [tk.StringVar(value=profile.trigger) for profile in state.profiles]
        self.profile_target_class = [
            tk.StringVar(value=profile.target_class) for profile in state.profiles
        ]
        self.profile_aim_position = [
            tk.DoubleVar(value=profile.aim_position) for profile in state.profiles
        ]
        self.profile_aim_position_text = [
            tk.StringVar(value=f"{profile.aim_position:.0f}%") for profile in state.profiles
        ]
        self.profile_fov = [tk.DoubleVar(value=profile.fov) for profile in state.profiles]
        self.profile_fov_text = [tk.StringVar(value=f"{profile.fov:.0f}") for profile in state.profiles]
        self.profile_kp_min = [tk.DoubleVar(value=profile.kp_min) for profile in state.profiles]
        self.profile_kp_min_text = [tk.StringVar(value=f"{profile.kp_min:.3f}") for profile in state.profiles]
        self.profile_kp_max = [tk.DoubleVar(value=profile.kp_max) for profile in state.profiles]
        self.profile_kp_max_text = [tk.StringVar(value=f"{profile.kp_max:.3f}") for profile in state.profiles]
        self.profile_kp_growth = [tk.DoubleVar(value=profile.kp_growth) for profile in state.profiles]
        self.profile_kp_growth_text = [
            tk.StringVar(value=f"{profile.kp_growth:.3f}") for profile in state.profiles
        ]
        self.profile_algorithm = [tk.StringVar(value=profile.algorithm) for profile in state.profiles]
        # FormState 里的 algorithm_params 已经按算法契约补齐了默认值, 顺序也是
        # PARAMS 的顺序 —— 控件按这个顺序摆, 所以这里直接照着建, 不再查一遍契约。
        self.profile_algorithm_params: list[dict[str, tk.DoubleVar]] = [
            {name: tk.DoubleVar(value=value) for name, value in profile.algorithm_params.items()}
            for profile in state.profiles
        ]
        self._last_profile_triggers = [variable.get() for variable in self.profile_trigger]
        self.preset_choice = tk.StringVar(value=NO_PRESET)
        self.status = tk.StringVar(value="已就绪")

    def _build_ui(self) -> None:
        root = ttk.Frame(self.root, padding=(18, 12))
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Endfield", style="Header.TLabel").pack(side="left")
        self.status_badge = tk.Label(
            header,
            textvariable=self.status,
            font=("Segoe UI Semibold", 10),
            fg="#067647",
            bg="#d1fadf",
            padx=12,
            pady=5,
        )
        self.start_button = ttk.Button(header, text="启动", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(header, text="停止", style="Stop.TButton", command=self._stop, state="disabled")
        self.stop_button.pack(side="right", padx=(8, 8))
        self.status_badge.pack(side="right")

        # 预设横跨两个页签(模型在「运行设置」, 算法在「识别与控制」), 所以放在页签外面。
        preset_bar = ttk.Frame(root)
        preset_bar.pack(fill="x", pady=(0, 10))
        ttk.Label(preset_bar, text="预设").pack(side="left", padx=(0, 8))
        self.preset_combo = ttk.Combobox(
            preset_bar, textvariable=self.preset_choice, state="readonly", width=30
        )
        self.preset_combo.pack(side="left")
        self.preset_combo.bind("<<ComboboxSelected>>", self._preset_selected)
        ttk.Button(preset_bar, text="保存", command=self._save_preset).pack(side="left", padx=(8, 0))
        ttk.Button(preset_bar, text="另存为…", command=self._save_preset_as).pack(side="left", padx=(8, 0))
        self.delete_preset_button = ttk.Button(preset_bar, text="删除", command=self._delete_preset)
        self.delete_preset_button.pack(side="left", padx=(8, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        run_tab = ttk.Frame(self.notebook, padding=8)
        advanced_tab = ttk.Frame(self.notebook, padding=14)
        library_tab = ttk.Frame(self.notebook, padding=14)
        self.preview_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(run_tab, text="运行设置")
        self.notebook.add(advanced_tab, text="识别与控制")
        self.notebook.add(library_tab, text="算法库")
        self.notebook.add(self.preview_tab, text="实时预览")

        trail_bar = ttk.Frame(self.preview_tab)
        trail_bar.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(trail_bar, text="画面", variable=self.preview_frame).pack(side="left")
        ttk.Checkbutton(trail_bar, text="轨迹", variable=self.trail_enabled).pack(side="left", padx=(12, 0))
        self.trail_optimal_check = ttk.Checkbutton(
            trail_bar, text="最优路径", variable=self.trail_optimal_path
        )
        self.trail_optimal_check.pack(side="left", padx=(12, 0))
        ttk.Label(trail_bar, text="轨迹长度").pack(side="left", padx=(18, 6))
        self.trail_seconds_scale = ttk.Scale(
            trail_bar,
            from_=TRAIL_MIN_SECONDS,
            to=TRAIL_MAX_SECONDS,
            variable=self.trail_seconds,
            length=200,
        )
        self.trail_seconds_scale.pack(side="left")
        self.trail_seconds_label = ttk.Label(trail_bar, width=6)
        self.trail_seconds_label.pack(side="left", padx=(6, 0))
        for variable in (self.preview_frame, self.trail_enabled, self.trail_optimal_path):
            variable.trace_add("write", self._trail_view_changed)
        self.trail_seconds.trace_add("write", self._trail_seconds_changed)
        self._show_trail_seconds()
        self._sync_trail_controls()

        self.preview_canvas = tk.Canvas(
            self.preview_tab,
            width=640,
            height=400,
            bg="#090d14",
            highlightthickness=0,
        )
        self.preview_canvas.pack(fill="both", expand=True)
        self._show_preview_message("启动后将在这里显示识别画面")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        model_box = ttk.LabelFrame(run_tab, text="模型", padding=12)
        model_box.pack(fill="x", pady=(0, 6))
        model_box.columnconfigure(1, weight=1)
        ttk.Label(model_box, text="ONNX 模型").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        self.model_entry = ttk.Entry(model_box, textvariable=self.model_path)
        self.model_entry.grid(row=0, column=1, sticky="ew", pady=5)
        self.model_entry.bind("<Return>", self._model_path_edited)
        self.model_entry.bind("<FocusOut>", self._model_path_edited)
        self.browse_model_button = ttk.Button(model_box, text="浏览...", command=self._browse_model)
        self.browse_model_button.grid(row=0, column=2, padx=(8, 0), pady=5)
        ttk.Label(model_box, text="加速方式").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        self.provider_combo = ttk.Combobox(
            model_box, textvariable=self.provider, values=list(PROVIDERS), state="readonly", width=22
        )
        self.provider_combo.grid(row=1, column=1, sticky="w", pady=5)
        self.provider_combo.bind("<<ComboboxSelected>>", self._provider_changed)
        self.cuda_graph_check = ttk.Checkbutton(
            model_box,
            text="启用 CUDA Graph（TensorRT 低延迟）",
            variable=self.cuda_graph,
        )
        self.cuda_graph_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self.gpu_preprocess_check = ttk.Checkbutton(
            model_box,
            text="启用 GPU 预处理（TensorRT）",
            variable=self.gpu_preprocess,
        )
        self.gpu_preprocess_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self._sync_cuda_graph_control()

        input_box = ttk.LabelFrame(run_tab, text="画面输入", padding=12)
        input_box.pack(fill="x", pady=(0, 6))
        input_box.columnconfigure(1, weight=1)
        ttk.Label(input_box, text="输入方式").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        input_combo = ttk.Combobox(
            input_box, textvariable=self.input_mode, values=list(INPUT_MODES), state="readonly", width=30
        )
        input_combo.grid(row=0, column=1, sticky="w", pady=5)
        input_combo.bind("<<ComboboxSelected>>", lambda _event: self._switch_input_panel())

        self.udp_panel = ttk.Frame(input_box)
        self.udp_panel.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.udp_panel.columnconfigure(1, weight=1)
        ttk.Label(self.udp_panel, text="监听地址").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(self.udp_panel, textvariable=self.udp_host, width=24).grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(self.udp_panel, text="端口").grid(row=0, column=2, sticky="w", padx=(18, 8), pady=5)
        ttk.Entry(self.udp_panel, textvariable=self.udp_port, width=9).grid(row=0, column=3, sticky="w", pady=5)
        ttk.Label(self.udp_panel, text="处理尺寸").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        size = ttk.Frame(self.udp_panel)
        size.grid(row=1, column=1, columnspan=3, sticky="w", pady=5)
        ttk.Entry(size, textvariable=self.udp_width, width=8).pack(side="left")
        ttk.Label(size, text=" × ").pack(side="left")
        ttk.Entry(size, textvariable=self.udp_height, width=8).pack(side="left")

        self.obs_panel = ttk.Frame(input_box)
        self.obs_panel.columnconfigure(1, weight=1)
        ttk.Label(self.obs_panel, text="OBS 地址").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(self.obs_panel, textvariable=self.obs_host, width=24).grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(self.obs_panel, text="端口").grid(row=0, column=2, sticky="w", padx=(18, 8), pady=5)
        ttk.Entry(self.obs_panel, textvariable=self.obs_port, width=9).grid(row=0, column=3, sticky="w", pady=5)
        ttk.Label(self.obs_panel, text="密码").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(self.obs_panel, textvariable=self.obs_password, show="•", width=24).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(self.obs_panel, text="来源名称").grid(row=1, column=2, sticky="w", padx=(18, 8), pady=5)
        ttk.Entry(self.obs_panel, textvariable=self.obs_source, width=18).grid(row=1, column=3, sticky="w", pady=5)

        kmbox = ttk.LabelFrame(run_tab, text="KMBox", padding=12)
        kmbox.pack(fill="x")
        kmbox.columnconfigure(1, weight=1)
        ttk.Checkbutton(kmbox, text="启用 KMBox 控制", variable=self.kmbox_enabled).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 5)
        )
        ttk.Label(kmbox, text="设备地址").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(kmbox, textvariable=self.kmbox_host, width=24).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(kmbox, text="端口").grid(row=1, column=2, sticky="w", padx=(18, 8), pady=5)
        ttk.Entry(kmbox, textvariable=self.kmbox_port, width=9).grid(row=1, column=3, sticky="w", pady=5)
        ttk.Label(kmbox, text="UUID").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(kmbox, textvariable=self.kmbox_uuid, width=24).grid(row=2, column=1, sticky="w", pady=5)

        detection = ttk.LabelFrame(advanced_tab, text="模型输出", padding=12)
        advanced_tab.columnconfigure(0, weight=1, uniform="advanced")
        advanced_tab.columnconfigure(1, weight=1, uniform="advanced")
        advanced_tab.rowconfigure(1, weight=1)
        detection.grid(row=0, column=0, columnspan=2, sticky="ew")
        detection.columnconfigure(1, weight=1)
        format_combo = self._combo_row(detection, 0, "输出格式", self.output_format, OUTPUT_FORMATS)
        format_combo.bind("<<ComboboxSelected>>", self._output_format_changed)
        self._slider_row(
            detection, 1, "置信度", self.confidence, self.confidence_text, 0.05, 0.95, self._confidence_changed
        )
        self._slider_row(detection, 2, "NMS IoU", self.iou, self.iou_text, 0.05, 0.95, self._iou_changed)
        self._combo_row(detection, 3, "运行信息语言", self.log_language, LOG_LANGUAGES)

        profiles = ttk.Frame(advanced_tab)
        profiles.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        profiles.columnconfigure(0, weight=1, uniform="profile")
        profiles.columnconfigure(1, weight=1, uniform="profile")
        highest_target_class = max(profile.target_class for profile in self.config.aim_profiles)
        initial_classes = [str(value) for value in range(max(7, highest_target_class + 1))]
        self.target_class_combos: list[ttk.Combobox] = []
        self.algorithm_combos: list[ttk.Combobox] = []
        self.algorithm_param_frames: list[ttk.Frame] = []
        for index in range(2):
            panel = ttk.LabelFrame(profiles, text=f"控制方案 {index + 1}", padding=12)
            panel.grid(row=0, column=index, sticky="nsew", padx=(0, 5) if index == 0 else (5, 0))
            panel.columnconfigure(1, weight=1)
            ttk.Checkbutton(
                panel,
                text="启用此方案",
                variable=self.profile_enabled[index],
                command=lambda profile=index: self._profile_enabled_changed(profile),
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
            # 控制算法排在最前且单独成框: 它决定准心怎么动, 是这一栏里最该先看到的东西。
            algorithm_box = ttk.LabelFrame(
                panel, text="控制算法", padding=10, style="Focus.TLabelframe"
            )
            algorithm_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 10))
            algorithm_box.columnconfigure(1, weight=1)
            algorithm_combo = self._combo_row(
                algorithm_box, 0, "算法", self.profile_algorithm[index], algorithm_choices()
            )
            algorithm_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, profile=index: self._algorithm_changed(profile),
            )
            self.algorithm_combos.append(algorithm_combo)
            params_frame = ttk.Frame(algorithm_box)
            params_frame.grid(row=1, column=0, columnspan=2, sticky="ew")
            params_frame.columnconfigure(1, weight=1)
            self.algorithm_param_frames.append(params_frame)
            self._rebuild_algorithm_params(index)
            share = ttk.Frame(algorithm_box)
            share.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
            ttk.Button(
                share,
                text="导出调校…",
                command=lambda profile=index: self._export_tuning(profile),
            ).pack(side="left")
            ttk.Button(
                share,
                text="导入调校…",
                command=lambda profile=index: self._import_tuning(profile),
            ).pack(side="left", padx=(8, 0))
            trigger_combo = self._combo_row(panel, 2, "触发方式", self.profile_trigger[index], TRIGGERS)
            trigger_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, profile=index: self._profile_trigger_changed(profile),
            )
            ttk.Label(panel, text="目标标签").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=6)
            target_class_combo = ttk.Combobox(
                panel,
                textvariable=self.profile_target_class[index],
                values=initial_classes,
                state="readonly",
                width=20,
            )
            target_class_combo.grid(row=3, column=1, sticky="w", pady=4)
            target_class_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, profile=index: self._target_class_changed(profile),
            )
            self.target_class_combos.append(target_class_combo)
            self._slider_row(
                panel,
                4,
                "框内位置（顶部 0%）",
                self.profile_aim_position[index],
                self.profile_aim_position_text[index],
                0,
                100,
                lambda value, profile=index: self._aim_position_changed(profile, value),
            )
            self._slider_row(
                panel,
                5,
                "视野半径",
                self.profile_fov[index],
                self.profile_fov_text[index],
                10,
                320,
                lambda value, profile=index: self._fov_changed(profile, value),
            )
            self._slider_row(
                panel,
                6,
                "P 最小值",
                self.profile_kp_min[index],
                self.profile_kp_min_text[index],
                0.0,
                0.3,
                lambda value, profile=index: self._kp_min_changed(profile, value),
            )
            self._slider_row(
                panel,
                7,
                "P 最大值",
                self.profile_kp_max[index],
                self.profile_kp_max_text[index],
                0.0,
                0.3,
                lambda value, profile=index: self._kp_max_changed(profile, value),
            )
            self._slider_row(
                panel,
                8,
                "P 增长斜率",
                self.profile_kp_growth[index],
                self.profile_kp_growth_text[index],
                0.0,
                0.5,
                lambda value, profile=index: self._kp_growth_changed(profile, value),
            )

        self._build_library_tab(library_tab)

        log_box = ttk.LabelFrame(root, text="运行状态", padding=8)
        log_box.pack(fill="both", expand=True, pady=(12, 10))
        self.log = tk.Text(
            log_box,
            height=3,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            bg="#111827",
            fg="#d1d5db",
            insertbackground="white",
            relief="flat",
            padx=10,
            pady=8,
        )
        self.log.pack(fill="both", expand=True)
        self._append_log(f"准备就绪。当前输入：{_display_value(INPUT_MODES, self.config.input.mode)}。")
        for warning in self.library_warnings:
            self._append_log(warning)

        actions = ttk.Frame(root)
        actions.pack(side="bottom", fill="x", before=self.notebook)
        log_box.pack_configure(side="bottom", fill="x", expand=False, before=self.notebook, pady=(12, 10))
        ttk.Button(actions, text="保存设置", command=self._save).pack(side="left")
        ttk.Button(actions, text="测试输入", command=lambda: self._launch(["--check"], "正在测试")).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="模型测速", command=lambda: self._launch(["--benchmark", "200"], "正在测速")).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            actions,
            text="管线测速",
            command=lambda: self._launch(["--pipeline-benchmark", "500"], "正在进行管线测速"),
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            actions,
            text="记录延迟日志",
            variable=self.latency_log_enabled,
        ).pack(side="left", padx=(18, 0))

    def _rebuild_algorithm_params(self, profile: int) -> None:
        frame = self.algorithm_param_frames[profile]
        for child in frame.winfo_children():
            child.destroy()
        name = algorithm_choices()[self.profile_algorithm[profile].get()]
        variables: dict[str, tk.DoubleVar] = {}
        specs = algorithm_param_specs(name)
        advanced = ttk.Frame(frame)
        advanced.columnconfigure(1, weight=1)
        rows = {False: 0, True: 0}
        for spec in specs:
            parent = advanced if spec.advanced else frame
            row = rows[spec.advanced]
            # 换算法时沿用同名参数的当前值, 免得来回切就被打回默认。
            previous = self.profile_algorithm_params[profile].get(spec.name)
            variable = tk.DoubleVar(
                value=previous.get() if previous is not None else spec.default
            )
            variables[spec.name] = variable
            ttk.Label(parent, text=spec.label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=4
            )
            ttk.Spinbox(
                parent,
                textvariable=variable,
                from_=spec.minimum,
                to=spec.maximum,
                increment=spec.step,
                width=10,
            ).grid(row=row, column=1, sticky="w", pady=4)
            if spec.slider:
                ttk.Scale(
                    parent, variable=variable, from_=spec.minimum, to=spec.maximum,
                    orient="horizontal", length=160,
                ).grid(row=row + 1, column=0, columnspan=2, sticky="ew")
                row += 1
            if spec.presets:
                buttons = ttk.Frame(parent)
                buttons.grid(row=row + 1, column=0, columnspan=2, sticky="w")
                for value in spec.presets:
                    ttk.Button(
                        buttons, text=f"{value:g}", width=3,
                        command=lambda v=value, target=variable: target.set(v),
                    ).pack(side="left", padx=(0, 2))
                row += 1
            rows[spec.advanced] = row + 1
        if any(spec.advanced for spec in specs):
            toggle = ttk.Button(frame, text="高级参数 ▸")
            toggle.grid(row=rows[False], column=0, columnspan=2, sticky="w", pady=(4, 0))
            advanced.grid(row=rows[False] + 1, column=0, columnspan=2, sticky="ew")
            advanced.grid_remove()

            def toggle_advanced():
                if advanced.winfo_manager():
                    advanced.grid_remove()
                    toggle.configure(text="高级参数 ▸")
                else:
                    advanced.grid()
                    toggle.configure(text="高级参数 ▾")

            toggle.configure(command=toggle_advanced)
        else:
            advanced.destroy()
        self.profile_algorithm_params[profile] = variables
        # 监听要在字典换上去之后再挂。挂早了, 重建途中触发的那一下会读到上一个
        # 算法的参数名, 把半份配置推给管线。
        for variable in variables.values():
            variable.trace_add(
                "write", lambda *_args: self._write_runtime_aim_settings()
            )

    def _algorithm_changed(self, profile: int) -> None:
        self._rebuild_algorithm_params(profile)
        self._write_runtime_aim_settings()
        if self.process is not None:
            self._append_log(
                f"控制方案 {profile + 1} 正在切换为「{self.profile_algorithm[profile].get()}」…"
            )

    _LIBRARY_COLUMNS = ("显示名", "标识", "作者", "来源文件", "导入日期", "类别")

    def _build_library_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        ttk.Label(
            parent,
            text="这里列出所有能在控制方案里选的算法。导入别人写的 .py 之前会先让你看清楚它是什么。",
            style="Subtle.TLabel",
            wraplength=760,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.library_tree = ttk.Treeview(
            parent, columns=self._LIBRARY_COLUMNS, show="headings", height=9
        )
        for column, width in zip(self._LIBRARY_COLUMNS, (190, 120, 100, 140, 120, 65)):
            self.library_tree.heading(column, text=column)
            self.library_tree.column(column, width=width, anchor="w")
        self.library_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.library_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.library_tree.configure(yscrollcommand=scroll.set)
        self.library_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._library_selection_changed()
        )

        buttons = ttk.Frame(parent)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(buttons, text="导入算法…", command=self._import_algorithm).pack(side="left")
        self.library_source_button = ttk.Button(
            buttons, text="查看源码", command=self._show_algorithm_source, state="disabled"
        )
        self.library_source_button.pack(side="left", padx=(8, 0))
        self.library_rename_button = ttk.Button(
            buttons, text="重命名", command=self._rename_algorithm, state="disabled"
        )
        self.library_rename_button.pack(side="left", padx=(8, 0))
        self.library_delete_button = ttk.Button(
            buttons, text="删除", command=self._delete_algorithm, state="disabled"
        )
        self.library_delete_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            parent,
            text="内置算法随程序分发，不能改名也不能删除。刚导入的算法不用重启，在控制方案里直接选就行。",
            style="Subtle.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._refresh_library()

    def _reload_algorithm_library(self) -> None:
        """重新读一遍算法库, 并让控制方案的下拉框跟上。

        下拉框存的是显示名, 而改名改的正是显示名。所以重载前先记下每个方案用的是
        哪个标识, 重载后按标识把显示名写回去——不这么做, 存的名字会对不上任何一个
        选项, 之后每次读表单都静默失败。
        """
        before = [
            algorithm_choices().get(self.profile_algorithm[index].get()) for index in range(2)
        ]
        warnings = self.session.reload_algorithms()
        choices = algorithm_choices()
        names = set(choices.values())
        for index, name in enumerate(before):
            self.algorithm_combos[index].configure(values=list(choices))
            if name is None or name in names:
                if name is not None:
                    self.profile_algorithm[index].set(_display_value(choices, name))
                continue
            # 正在用的算法被删掉了。悄悄换成别的会让手感莫名其妙变一个样, 得说出来。
            self.profile_algorithm[index].set(_display_value(choices, "p"))
            self._rebuild_algorithm_params(index)
            self._append_log(f"控制方案 {index + 1} 用的算法已被删除，已改回比例控制。")
        for warning in warnings:
            self._append_log(warning)
        self._write_runtime_aim_settings()

    def _library_rows(self) -> list[tuple[str, str, str, str, str, str]]:
        return self.session.library_rows()

    def _refresh_library(self) -> None:
        self.library_tree.delete(*self.library_tree.get_children())
        for row in self._library_rows():
            self.library_tree.insert("", "end", iid=row[1], values=row)
        self._library_selection_changed()

    def _selected_algorithm(self) -> tuple[str, str] | None:
        """返回 (标识, 类别), 没选中返回 None。"""
        selection = self.library_tree.selection()
        if not selection:
            return None
        values = self.library_tree.item(selection[0], "values")
        return values[1], values[5]

    def _library_selection_changed(self) -> None:
        selected = self._selected_algorithm()
        installed = selected is not None and selected[1] == "已导入"
        state = "normal" if installed else "disabled"
        self.library_rename_button.configure(state=state)
        self.library_delete_button.configure(state=state)
        self.library_source_button.configure(state=state)

    def _import_algorithm(self) -> None:
        source = filedialog.askopenfilename(
            title="选择算法源码", filetypes=[("Python 源码", "*.py")], parent=self.root
        )
        if not source:
            return
        # 选文件和显示源码是界面的事, 读文件、问用户、装进算法库归 session。
        entry = self.session.import_algorithm(
            Path(source),
            show_source=self._show_source_window,
            on_installed=self._algorithm_installed,
        )
        if entry is None:
            return
        self._append_log(f"算法库新增「{entry.display_name}」（{entry.name}）。")

    def _algorithm_installed(self, _entry) -> None:
        """装好之后、「导入成功」弹出来之前跑: 弹窗背后的列表必须已经是新的。"""
        self._reload_algorithm_library()
        self._refresh_library()

    def _show_algorithm_source(self) -> None:
        selected = self._selected_algorithm()
        if selected is None:
            return
        found = self.session.algorithm_source(selected[0])
        if found is None:
            return
        self._show_source_window(*found)

    def _show_source_window(self, title: str, source: str) -> None:
        window = tk.Toplevel(self.root)
        window.title(f"源码：{title}")
        window.geometry("880x620")
        text = tk.Text(window, wrap="none", font=("Consolas", 10))
        text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", source)
        text.configure(state="disabled")
        window.transient(self.root)
        window.grab_set()
        self.root.wait_window(window)

    def _rename_algorithm(self) -> None:
        selected = self._selected_algorithm()
        if selected is None or selected[1] != "已导入":
            return
        new_name = self.session.rename_algorithm(selected[0])
        if new_name is None:
            return
        self._reload_algorithm_library()
        self._refresh_library()
        self._append_log(f"算法「{selected[0]}」的显示名已改为「{new_name}」。")

    def _delete_algorithm(self) -> None:
        selected = self._selected_algorithm()
        if selected is None or selected[1] != "已导入":
            return
        # 问一句和真删都在 session 里; 它返回的是删掉那个的显示名, 因为删完
        # 注册表里就查不到了, 日志那行拿不到名字。
        display_name = self.session.delete_algorithm(selected[0])
        if display_name is None:
            return
        self._reload_algorithm_library()
        self._refresh_library()
        self._append_log(f"算法「{display_name}」已删除。")

    def _measurement_path(self) -> Path:
        return self.config_path.parent / MEASUREMENT_NAME

    def _export_tuning(self, profile: int) -> None:
        try:
            config = self._read_form()
        except (ValueError, KeyError) as error:
            messagebox.showerror("导出失败", f"设置里有填错的地方：{error}", parent=self.root)
            return
        target = filedialog.asksaveasfilename(
            title=f"导出控制方案 {profile + 1} 的调校",
            defaultextension=".json",
            initialfile=f"tuning-{config.aim_profiles[profile].algorithm}.json",
            filetypes=[("调校文件", "*.json")],
            parent=self.root,
        )
        if not target:
            return
        text = dump_tuning(
            config.aim_profiles[profile],
            measured_loop_ms=load_measurement(self._measurement_path()),
        )
        try:
            Path(target).write_text(text, encoding="utf-8")
        except OSError as error:
            messagebox.showerror("导出失败", str(error), parent=self.root)
            return
        self._append_log(f"控制方案 {profile + 1} 的调校已导出到 {target}。")

    def _import_tuning(self, profile: int) -> None:
        source = filedialog.askopenfilename(
            title=f"给控制方案 {profile + 1} 导入调校",
            filetypes=[("调校文件", "*.json")],
            parent=self.root,
        )
        if not source:
            return
        try:
            text = Path(source).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            messagebox.showerror("导入失败", f"读不了这个文件：{error}", parent=self.root)
            return
        try:
            tuning = load_tuning(text, known_algorithms=set(available_algorithms()))
        except TuningError as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            return

        lines = [
            f"要把控制方案 {profile + 1} 换成这份调校吗？",
            "",
            f"算法：{_display_value(algorithm_choices(), tuning.algorithm)}",
        ]
        lines += [f"　{name} = {value:g}" for name, value in sorted(tuning.params.items())]
        lines += [
            f"P 范围 {tuning.kp_min:g} – {tuning.kp_max:g}，增长斜率 {tuning.kp_growth:g}",
            f"视野半径 {tuning.fov_radius:g}，框内位置 {tuning.target_y_ratio * 100:.0f}%",
        ]
        warning = delay_warning(tuning.measured_loop_ms, load_measurement(self._measurement_path()))
        if warning:
            lines += ["", "⚠ " + warning]
        lines += ["", "触发键和目标标签不会被改动。"]
        if not messagebox.askokcancel("导入调校", "\n".join(lines), parent=self.root):
            return

        self._apply_tuning_to_form(profile, tuning)
        self._append_log(
            f"控制方案 {profile + 1} 已套用 {Path(source).name}。点「保存设置」才会写进配置。"
        )
        if self.process is not None:
            self._append_log("控制算法要停止后重新启动才会生效。")

    def _apply_tuning_to_form(self, profile: int, tuning: Tuning) -> None:
        # 顺序要紧: 先设算法, 再重建参数控件, 最后才填参数值。颠倒了参数会被打回默认。
        self.profile_algorithm[profile].set(_display_value(algorithm_choices(), tuning.algorithm))
        self._rebuild_algorithm_params(profile)
        for name, variable in self.profile_algorithm_params[profile].items():
            if name in tuning.params:
                variable.set(tuning.params[name])
        for variables, texts, value in (
            (self.profile_kp_min, self.profile_kp_min_text, tuning.kp_min),
            (self.profile_kp_max, self.profile_kp_max_text, tuning.kp_max),
            (self.profile_kp_growth, self.profile_kp_growth_text, tuning.kp_growth),
        ):
            variables[profile].set(value)
            texts[profile].set(f"{value:.3f}")
        # 界面上这个滑块是百分比, 配置里是比例。
        percent = tuning.target_y_ratio * 100.0
        self.profile_aim_position[profile].set(percent)
        self.profile_aim_position_text[profile].set(f"{percent:.0f}%")
        self.profile_fov[profile].set(tuning.fov_radius)
        self.profile_fov_text[profile].set(f"{tuning.fov_radius:.0f}")
        # kp 和视野是热更新的, 立刻推给正在跑的管线。
        self._write_runtime_aim_settings()

    def _restore_last_preset(self) -> None:
        # 上次的预设被删了就安静地忘掉, 那是用户自己删的, 不用提醒。
        name = self.session.find_preset(self.config.ui.preset) if self.config.ui.preset else None
        if name is not None:
            try:
                # 模型不在也照样读: 这里只拿来比较有没有改动, 不往表单里填。
                self._preset_baseline = self.session.read_preset_baseline(name)
                self.current_preset = name
            except PresetError as error:
                self._append_log(f"上次用的预设「{name}」读不了：{error}")
        self._refresh_preset_bar()

    def _watch_preset(self) -> None:
        self._refresh_preset_marker()
        self.root.after(PRESET_POLL_MS, self._watch_preset)

    def _preset_changed(self) -> bool | None:
        """None = 表单这会儿读不出来(数字框打到一半)。"""
        if self.current_preset is None or self._preset_baseline is None:
            return False
        try:
            form = preset_from_config(self._read_form())
        except (tk.TclError, ValueError, KeyError):
            return None
        return not same_settings(form, self._preset_baseline)

    def _refresh_preset_bar(self) -> None:
        self.preset_combo.configure(values=self.session.list_presets())
        self.delete_preset_button.configure(state="normal" if self.current_preset else "disabled")
        self._refresh_preset_marker()

    def _refresh_preset_marker(self) -> None:
        # 轮询而不是给每个控件挂监听: 以后加一个设置项不用记得来这里登记。
        changed = self._preset_changed()
        if changed is None:
            return
        if self.current_preset is None:
            text = NO_PRESET
        else:
            text = f"{self.current_preset} *" if changed else self.current_preset
        if self.preset_choice.get() != text:
            self.preset_choice.set(text)

    def _preset_selected(self, _event=None) -> None:
        # 重选当前预设也照常载入: 有改动会先问, 选「否」就等于撤回改动。
        self._load_preset(self.preset_choice.get())
        # 取消或失败时把下拉框上的字改回当前预设。
        self._refresh_preset_bar()

    def _load_preset(self, name: str) -> bool:
        if self.process is not None:
            # 模型要重启才换得了, 手感却是热切换的: 一半生效一半没生效最难排查。
            self.prompter.notify_info("正在运行", "运行中不能载入预设，先停止再切换。")
            return False
        if self._preset_changed() is not False:
            answer = self.prompter.confirm_three_way(
                "切换预设",
                f"预设「{self.current_preset}」有改动还没保存。\n\n"
                "是：先存进这个预设再切换\n否：丢掉这些改动\n取消：留在当前预设",
            )
            if answer is None or (answer and not self._save_preset()):
                return False
        preset = self.session.load_preset(name)
        if preset is None:
            return False
        self._fill_form(preset)
        self.current_preset = name
        self._preset_baseline = preset
        # 顺手写进配置: 下次打开程序还是这个预设。
        self._save(quiet=True)
        self._refresh_preset_bar()
        self._append_log(f"已载入预设「{name}」。")
        return True

    def _fill_form(self, preset: Preset) -> None:
        # 旧模型路径必须在灌新值之前取: apply_preset_to_state 第一件事就是盖掉
        # state.model_path, 取晚了就恒等于预设里的新路径, 模型永远不再重新探测,
        # 于是新模型的类别数出不来, 合法的标签会被旧契约判越界、悄悄改成 0。
        previous_model = _resolve_model_path(self.model_path.get().strip(), self.config_path.parent)

        # 算法参数不读: 那几个框是能手打的, 打到一半 .get() 会抛 TclError, 而这里
        # 读到的参数下面整个会被预设的覆盖掉, 白白多一条崩溃路径。
        state = self._form_state(algorithm_params=False)
        apply_preset_to_state(
            state, preset, self.config_path.parent, self._labels(), display_path=_display_path
        )
        self._load_form_state(state)
        self._sync_cuda_graph_control()
        self._switch_input_panel()
        for index, profile in enumerate(preset.aim_profiles):
            # 先清掉旧参数: 文件里没写的参数该用默认值, 不该沿用上一个预设留下的。
            self.profile_algorithm_params[index] = {}
            # 原样调 _apply_tuning_to_form, 不要拆开重写: 它不只是重建参数控件,
            # 还负责设算法 / 算法参数 / kp 三件套 / 瞄准位置 / 视野这七个字段, 末尾
            # 把 kp 和视野热推给正在跑的管线。拆开就把「载入预设不重启就生效」
            # 退化成「要停了重启」。
            self._apply_tuning_to_form(
                index,
                Tuning(
                    algorithm=profile.algorithm,
                    params=dict(profile.algorithm_params),
                    kp_min=profile.kp_min,
                    kp_max=profile.kp_max,
                    kp_growth=profile.kp_growth,
                    target_y_ratio=profile.target_y_ratio,
                    fov_radius=profile.fov_radius,
                ),
            )
        # 触发键冲突时要退回「上一个合法值」, 这个值得跟着预设走。
        self._last_profile_triggers = [variable.get() for variable in self.profile_trigger]
        # 标签得等新模型的类别数出来再校验。拿旧模型的类别数去卡, 合法的标签会被悄悄改成 0。
        if previous_model != preset.model.path or self.model_contract is None:
            self._inspect_selected_model(preset.model.path)
        else:
            self._update_target_class_choices()

    def _save_preset(self) -> bool:
        if self.current_preset is None:
            return self._save_preset_as()
        return self._store_preset(self.current_preset)

    def _save_preset_as(self) -> bool:
        name = self.prompter.ask_text(
            "另存为预设",
            "给现在这套设置起个名字：",
            initial=self.current_preset or "",
        )
        if name is None:
            return False
        try:
            name = validate_name(name)
        except PresetError as error:
            self.prompter.notify_error("这个名字不能用", str(error))
            return False
        existing = self.session.find_preset(name)
        if (
            existing is not None
            and existing != self.current_preset
            # 覆盖是破坏性的: danger 换来警告图标和落在取消的默认按钮。
            and not self.prompter.confirm(
                "覆盖预设",
                f"已经有一个叫「{existing}」的预设了。\n\n要用现在的设置覆盖它吗？",
                danger=True,
            )
        ):
            return False
        return self._store_preset(name)

    def _store_preset(self, name: str) -> bool:
        # 读表单归界面: 只有这边接得住 tk 的 TclError, 而且非法输入要和以前一样
        # 走到同一个「预设无法保存」弹窗。
        try:
            config = self._read_form()
        except (OSError, tk.TclError, ValueError, KeyError) as error:
            self.prompter.notify_error("预设无法保存", str(error))
            return False
        stored = self.session.store_preset(name, config)
        if stored is None:
            return False
        self.current_preset = stored
        self._preset_baseline = preset_from_config(config)
        # 和载入一样顺手记住, 下次打开还是这个预设。
        self._save(quiet=True)
        self._refresh_preset_bar()
        self._append_log(f"预设「{stored}」已保存。")
        return True

    def _delete_preset(self) -> None:
        name = self.current_preset
        if name is None:
            return
        # 问一句和真删都在 session 里, 那边传的是 danger=True: 警告图标 + 默认
        # 按钮落在取消, 手滑按回车删不掉。
        if not self.session.delete_preset(name):
            return
        self.current_preset = None
        self._preset_baseline = None
        self._refresh_preset_bar()
        self._append_log(f"预设「{name}」已删除。")

    def _entry_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Entry(parent, textvariable=variable, width=24).grid(row=row, column=1, sticky="w", pady=6)

    def _combo_row(
        self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, values: dict[str, str]
    ) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        combo = ttk.Combobox(parent, textvariable=variable, values=list(values), state="readonly", width=20)
        combo.grid(row=row, column=1, sticky="w", pady=4)
        return combo

    def _slider_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.DoubleVar,
        value_text: tk.StringVar,
        minimum: float,
        maximum: float,
        command,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        control = ttk.Frame(parent)
        control.grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Scale(control, from_=minimum, to=maximum, variable=variable, command=command).pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(control, textvariable=value_text, width=7, anchor="e").pack(side="left", padx=(8, 0))

    def _switch_input_panel(self) -> None:
        mode = INPUT_MODES[self.input_mode.get()]
        if mode == "obs_websocket":
            self.udp_panel.grid_remove()
            self.obs_panel.grid(row=1, column=0, columnspan=3, sticky="ew")
        elif mode == "desktop":
            # 本机屏幕的设置只在新界面里有 (这个界面不再加控件), 两组都藏起来:
            # 显示 UDP 那组的话, 用户会去改一个根本不起作用的端口。
            self.udp_panel.grid_remove()
            self.obs_panel.grid_remove()
        else:
            self.obs_panel.grid_remove()
            self.udp_panel.grid()

    def _browse_model(self) -> None:
        current_path = _resolve_model_path(self.model_path.get().strip(), self.config_path.parent)
        selected = filedialog.askopenfilename(
            title="选择 ONNX 模型",
            initialdir=str(current_path.parent),
            filetypes=[("ONNX 模型", "*.onnx"), ("所有文件", "*.*")],
        )
        if selected:
            self.model_path.set(_display_path(Path(selected), self.config_path.parent))
            self._inspect_selected_model(Path(selected))

    def _inspect_selected_model(self, path: Path) -> None:
        path = _resolve_model_path(path, self.config_path.parent)
        self.model_contract = None
        self._update_target_class_choices()
        self._set_status("正在检查模型", "#175cd3", "#dbeafe")
        output_format_hint = OUTPUT_FORMATS[self.output_format.get()]
        threading.Thread(
            target=self._inspect_model_worker,
            args=(path, output_format_hint),
            daemon=True,
        ).start()

    def _model_path_edited(self, _event=None) -> None:
        path = _resolve_model_path(self.model_path.get().strip(), self.config_path.parent)
        if path.is_file():
            self._inspect_selected_model(path)
        else:
            self.model_contract = None
            self._update_target_class_choices()

    def _inspect_model_worker(self, path: Path, output_format_hint: str) -> None:
        from .model_inspection import inspect_model

        try:
            contract = inspect_model(path, output_format_hint)
        except Exception as exc:
            self.messages.put(("model_error", (path, str(exc))))
            return
        self.messages.put(("model_contract", (path, contract)))

    def _apply_model_contract(self, path: Path, contract) -> None:
        selected_path = _resolve_model_path(self.model_path.get().strip(), self.config_path.parent)
        if selected_path != path.resolve():
            return
        try:
            output_format = contract.output_format
            self.model_contract = contract
            if output_format is not None:
                self.output_format.set(_display_value(OUTPUT_FORMATS, output_format))
            self._update_target_class_choices()
            shape = " × ".join(str(value) for value in contract.output_shape)
            detected = _display_value(OUTPUT_FORMATS, output_format) if output_format else "需要手动确认格式"
            class_count = contract.class_count_for(OUTPUT_FORMATS[self.output_format.get()])
            labels = f"标签 0–{class_count - 1}" if class_count else "标签范围未知"
            self._append_log(f"模型输出 {shape}，张量排列已自动识别；{detected}，{labels}。")
            self._set_status("模型已识别", "#067647", "#d1fadf")
        except Exception as exc:
            self._set_status("模型需检查", "#b42318", "#fee4e2")
            self._append_log(f"模型无法读取：{exc}")

    def _safe_trail_seconds(self) -> float:
        """轨迹长度的 DoubleVar 装了非数字时 .get() 会抛 TclError。这个兜底是 tk
        特有的, 所以留在适配层, 不进 gui_core。"""
        try:
            return float(self.trail_seconds.get())
        except (tk.TclError, ValueError):
            return self.config.ui.trail_seconds

    def _form_state(self, *, algorithm_params: bool = True) -> FormState:
        """把 tk 变量收成一个 FormState。

        数值输入框原样取字符串, 解析留给 form_state_to_config —— 非法输入要走到
        「设置无法保存」那个弹窗, 在这里提前转 int 就变成界面直接崩。

        algorithm_params=False 时不读算法参数框。那几个是 Spinbox, 用户能往里
        打字, 打到一半时 DoubleVar.get() 会抛 TclError (_write_runtime_aim_settings
        里那段 except 说的就是它)。保存那条路必须读、也必须让它抛; 载入预设和
        轨迹滑条今天根本不碰这些框, 不该因为它们多出一条崩溃路径。
        """
        return FormState(
            model_path=self.model_path.get(),
            provider=self.provider.get(),
            cuda_graph=self.cuda_graph.get(),
            gpu_preprocess=self.gpu_preprocess.get(),
            output_format=self.output_format.get(),
            confidence=self.confidence.get(),
            iou=self.iou.get(),
            input_mode=self.input_mode.get(),
            log_language=self.log_language.get(),
            udp_host=self.udp_host.get(),
            udp_port=self.udp_port.get(),
            udp_width=self.udp_width.get(),
            udp_height=self.udp_height.get(),
            obs_host=self.obs_host.get(),
            obs_port=self.obs_port.get(),
            obs_password=self.obs_password.get(),
            obs_source=self.obs_source.get(),
            kmbox_enabled=self.kmbox_enabled.get(),
            kmbox_host=self.kmbox_host.get(),
            kmbox_port=self.kmbox_port.get(),
            kmbox_uuid=self.kmbox_uuid.get(),
            latency_log_enabled=self.latency_log_enabled.get(),
            preview_frame=self.preview_frame.get(),
            trail_enabled=self.trail_enabled.get(),
            trail_optimal_path=self.trail_optimal_path.get(),
            trail_seconds=self._safe_trail_seconds(),
            profiles=tuple(
                ProfileFormState(
                    enabled=self.profile_enabled[index].get(),
                    trigger=self.profile_trigger[index].get(),
                    target_class=self.profile_target_class[index].get(),
                    aim_position=self.profile_aim_position[index].get(),
                    fov=self.profile_fov[index].get(),
                    kp_min=self.profile_kp_min[index].get(),
                    kp_max=self.profile_kp_max[index].get(),
                    kp_growth=self.profile_kp_growth[index].get(),
                    algorithm=self.profile_algorithm[index].get(),
                    algorithm_params=(
                        {
                            name: variable.get()
                            for name, variable in self.profile_algorithm_params[index].items()
                        }
                        if algorithm_params
                        else {}
                    ),
                )
                for index in (0, 1)
            ),
        )

    def _load_form_state(self, state: FormState) -> None:
        """把 FormState 写回 tk 变量, 顺带刷新滑条旁边那些格式化文本。

        方案里只写 启用 / 触发键 / 目标标签 三个。算法、算法参数、kp 三件套、
        瞄准位置、视野归 _apply_tuning_to_form —— 换算法要连参数控件一起重建,
        而重建的顺序 (先换字典再挂 trace) 是调过的, 在这里各设一遍会绕开它。
        """
        self.model_path.set(state.model_path)
        self.provider.set(state.provider)
        self.cuda_graph.set(state.cuda_graph)
        self.gpu_preprocess.set(state.gpu_preprocess)
        self.output_format.set(state.output_format)
        self.confidence.set(state.confidence)
        self.confidence_text.set(f"{state.confidence:.3f}")
        self.iou.set(state.iou)
        self.iou_text.set(f"{state.iou:.3f}")
        self.input_mode.set(state.input_mode)
        self.log_language.set(state.log_language)
        for variable, value in (
            (self.udp_host, state.udp_host),
            (self.udp_port, state.udp_port),
            (self.udp_width, state.udp_width),
            (self.udp_height, state.udp_height),
            (self.obs_host, state.obs_host),
            (self.obs_port, state.obs_port),
            (self.obs_password, state.obs_password),
            (self.obs_source, state.obs_source),
            (self.kmbox_host, state.kmbox_host),
            (self.kmbox_port, state.kmbox_port),
            (self.kmbox_uuid, state.kmbox_uuid),
        ):
            variable.set(value)
        self.kmbox_enabled.set(state.kmbox_enabled)
        for index, profile in enumerate(state.profiles):
            self.profile_enabled[index].set(profile.enabled)
            self.profile_trigger[index].set(profile.trigger)
            self.profile_target_class[index].set(profile.target_class)

    def _read_form(self) -> AppConfig:
        return form_state_to_config(
            self._form_state(),
            self.config,
            self.config_path.parent,
            self._labels(),
            current_preset=self.current_preset,
            resolve_model_path=_resolve_model_path,
        )

    def _save(self, quiet: bool = False) -> bool:
        try:
            candidate = self._read_form()
            save_config(candidate, self.config_path)
            self.config = load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror("设置无法保存", str(exc), parent=self.root)
            return False
        if not quiet:
            self._append_log("设置已保存。")
        return True

    def _current_trail_settings(self) -> TrailSettings:
        # 不读算法参数框: 三个调用点 (_show_trail_seconds / _write_trail_settings_file
        # / _persist_trail_settings) 都没人接住 TclError, 拖轨迹滑条不该被另一个
        # 页签上半个数字弄崩。
        return trail_settings_from_state(self._form_state(algorithm_params=False))

    def _show_trail_seconds(self) -> None:
        self.trail_seconds_label.configure(text=f"{self._current_trail_settings().seconds:.1f} 秒")

    def _sync_trail_controls(self) -> None:
        # 最优路径和轨迹长度只在看轨迹的时候才有意义。
        state = ["!disabled"] if self.trail_enabled.get() else ["disabled"]
        self.trail_optimal_check.state(state)
        self.trail_seconds_scale.state(state)

    def _trail_view_changed(self, *_args) -> None:
        self._sync_trail_controls()
        if self.process is not None:
            self._write_trail_settings_file()
        self._sync_preview_rendering()

    def _trail_seconds_changed(self, *_args) -> None:
        self._show_trail_seconds()
        if self.process is not None:
            self._write_trail_settings_file()
        # 拖滑条每动一下就触发一次, 停手之后再写 settings.txt。
        if self._trail_persist_job is not None:
            self.root.after_cancel(self._trail_persist_job)
        self._trail_persist_job = self.root.after(TRAIL_PERSIST_DELAY_MS, self._persist_trail_settings)

    def _something_to_preview(self) -> bool:
        return bool(self.preview_frame.get() or self.trail_enabled.get())

    def _sync_preview_rendering(self) -> None:
        """勾选框变了: 两个都不勾就让管线别再渲染预览, 勾回来再接着渲染。"""
        if self.process is None or self.preview_socket is None or not self._preview_tab_selected():
            return
        if not self._something_to_preview():
            self.preview_enable_file.unlink(missing_ok=True)
            self._discard_preview_frames()
            self._show_preview_message(NOTHING_TO_PREVIEW)
        elif not self.preview_enable_file.exists():
            self.preview_enable_file.touch()
            self._show_preview_message("正在等待第一帧...")

    def _write_trail_settings_file(self) -> None:
        try:
            write_trail_settings(self.trail_settings_file, self._current_trail_settings())
        except OSError as exc:
            self._append_log(f"轨迹设置没能传给运行中的程序：{exc}")

    def _persist_trail_settings(self) -> None:
        """只把轨迹长度写回 settings.txt。

        不走 _save: 那会把表单上别的、用户还没决定保存的改动一起写进去。
        """
        self._trail_persist_job = None
        values = {"trail_seconds": self._current_trail_settings().seconds}
        try:
            stored = load_config(self.config_path, validate_model=False)
            save_config(replace(stored, ui=replace(stored.ui, **values)), self.config_path)
        except (OSError, ValueError) as exc:
            self._append_log(f"轨迹设置没能保存：{exc}")
            return
        self.config = replace(self.config, ui=replace(self.config.ui, **values))

    def _start(self) -> None:
        self._launch([], "正在启动")

    def _launch(self, arguments: list[str], running_status: str) -> None:
        if self.process is not None:
            messagebox.showinfo("程序正在运行", "请先停止当前任务。", parent=self.root)
            return
        if not self._save(quiet=True):
            return
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.unlink(missing_ok=True)
        self.preview_enable_file.unlink(missing_ok=True)
        self.runtime_aim_file.unlink(missing_ok=True)
        self.trail_settings_file.unlink(missing_ok=True)
        preview_socket = None
        preview_port: int | None = None
        preview_enable_file: Path | None = None
        trail_settings_file: Path | None = None
        latency_log: Path | None = None
        if not arguments:
            # 预览 socket 留在界面这边, 没搬进 session: 端口是 bind 完才知道的,
            # 而帧的去处两套界面完全不同 (这边画进画布, WebView 那边要转成
            # MJPEG 流)。现在抽一个共用抽象只能靠猜。
            preview_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            preview_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_048_576)
            preview_socket.bind(("127.0.0.1", 0))
            preview_socket.settimeout(0.25)
            preview_port = preview_socket.getsockname()[1]
            preview_enable_file = self.preview_enable_file
            # 启动前就写好: 管线一开始读到的就是当前设置, 而不是默认值。
            self._write_trail_settings_file()
            trail_settings_file = self.trail_settings_file
            if self._preview_tab_selected() and self._something_to_preview():
                self.preview_enable_file.touch()
            if self.latency_log_enabled.get():
                stamp = time.strftime("%Y%m%d-%H%M%S")
                latency_log = self.config_path.parent / f"latency-{stamp}.csv"
                self._append_log(f"延迟日志将记录到 {latency_log.name}（每帧都记，停止时给出估计）。")
        command = self.session.build_command(
            config_path=self.config_path,
            stop_file=self.stop_file,
            runtime_aim_file=self.runtime_aim_file,
            preview_port=preview_port,
            preview_enable_file=preview_enable_file,
            trail_settings_file=trail_settings_file,
            latency_log=latency_log,
            extra=arguments,
        )
        # 拿回 Popen 自己存一份: 界面判断「在不在跑」靠 self.process, 而它要
        # 一直留到 _drain_messages 收到 done 那一刻才清 —— 收尾 (关预览 socket、
        # 删临时文件) 都挂在那一刻上, 提前清掉就漏了。
        process = self.session.start(
            command,
            cwd=self.config_path.parent,
            on_line=lambda line: self.messages.put(("log", line)),
            on_exit=lambda code: self.messages.put(("done", code)),
            stop_file=self.stop_file,
        )
        if process is None:
            # session 已经弹过「无法启动」, 这边只管把刚铺好的东西收掉。
            if preview_socket is not None:
                preview_socket.close()
            self.preview_enable_file.unlink(missing_ok=True)
            self.runtime_aim_file.unlink(missing_ok=True)
            self.trail_settings_file.unlink(missing_ok=True)
            self.process = None
            return
        self.process = process
        self.preview_socket = preview_socket
        self._write_runtime_aim_settings()
        if preview_socket is not None:
            self._discard_preview_frames()
            if self._preview_tab_selected() and not self._something_to_preview():
                self._show_preview_message(NOTHING_TO_PREVIEW)
            elif self._preview_tab_selected():
                self._show_preview_message("正在等待第一帧...")
            else:
                self._show_preview_message("切换到此页后开始预览")
            threading.Thread(target=self._receive_preview, args=(preview_socket,), daemon=True).start()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.stop_requested = False
        self._set_running(True, running_status)

    def _receive_preview(self, preview_socket: socket.socket) -> None:
        while self.preview_socket is preview_socket:
            try:
                payload, _address = preview_socket.recvfrom(65_507)
            except socket.timeout:
                continue
            except OSError:
                return
            frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            try:
                self.preview_frames.put_nowait(frame)
            except queue.Full:
                try:
                    self.preview_frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.preview_frames.put_nowait(frame)
                except queue.Full:
                    pass

    def _drain_preview(self) -> None:
        """只做预览。和日志分开跑, 是因为两者的节拍差了五倍:
        日志一秒一行, 80 毫秒够用; 画面一秒 30 帧, 80 毫秒就卡了。"""
        frame = None
        while True:
            try:
                frame = self.preview_frames.get_nowait()
            except queue.Empty:
                break
        if frame is not None:
            self._display_preview(frame)
        watching = self.preview_socket is not None and self._preview_tab_selected()
        self.root.after(
            PREVIEW_POLL_MS if watching else PREVIEW_IDLE_POLL_MS, self._drain_preview
        )

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(str(payload))
                if str(payload).startswith(("Input:", "输入：")):
                    self._set_status("等待画面", "#b54708", "#fef0c7")
                elif "capture=" in str(payload) or "采集=" in str(payload):
                    self._set_status("运行中", "#067647", "#d1fadf")
            elif kind == "model_contract":
                path, contract = payload
                self._apply_model_contract(path, contract)
            elif kind == "model_error":
                path, error = payload
                selected_path = _resolve_model_path(
                    self.model_path.get().strip(), self.config_path.parent
                )
                if selected_path == path.resolve():
                    self.model_contract = None
                    self._update_target_class_choices()
                    self._set_status("模型需检查", "#b42318", "#fee4e2")
                    self._append_log(f"模型无法读取：{error}")
            else:
                code = int(payload)
                self.process = None
                self._close_preview_socket()
                self.runtime_aim_file.unlink(missing_ok=True)
                self.trail_settings_file.unlink(missing_ok=True)
                self.stop_file.unlink(missing_ok=True)
                stopped_normally = code == 0 or self.stop_requested
                self._set_running(False, "已停止" if stopped_normally else "运行出错")
                self.stop_requested = False
        self.root.after(80, self._drain_messages)

    def _display_preview(self, frame: np.ndarray) -> None:
        # 两个都不勾之后, 管线还在路上的最后几帧不能把提示文字盖掉。
        if not self._preview_tab_selected() or not self._something_to_preview():
            return
        canvas_width = max(1, self.preview_canvas.winfo_width())
        canvas_height = max(1, self.preview_canvas.winfo_height())
        fitted = fit_preview_to_canvas(frame, canvas_width, canvas_height)
        self.preview_photo = ImageTk.PhotoImage(Image.fromarray(fitted))
        self.preview_canvas.delete("preview")
        self.preview_canvas.create_image(
            canvas_width // 2,
            canvas_height // 2,
            image=self.preview_photo,
            anchor="center",
            tags="preview",
        )

    def _show_preview_message(self, message: str) -> None:
        self.preview_photo = None
        self.preview_canvas.delete("preview")
        self.preview_canvas.create_text(
            320,
            200,
            text=message,
            fill="#98a2b3",
            font=("Segoe UI", 11),
            tags="preview",
        )

    def _discard_preview_frames(self) -> None:
        while True:
            try:
                self.preview_frames.get_nowait()
            except queue.Empty:
                return

    def _preview_tab_selected(self) -> bool:
        return self.notebook.select() == str(self.preview_tab)

    def _on_tab_changed(self, _event=None) -> None:
        if self._preview_tab_selected():
            if self.process is None or self.preview_socket is None:
                self._show_preview_message("启动后将在这里显示识别画面")
            elif not self._something_to_preview():
                self._show_preview_message(NOTHING_TO_PREVIEW)
            else:
                self.preview_enable_file.touch()
                self._show_preview_message("正在等待第一帧...")
            return
        self.preview_enable_file.unlink(missing_ok=True)
        self._discard_preview_frames()

    def _close_preview_socket(self) -> None:
        self.preview_enable_file.unlink(missing_ok=True)
        preview_socket = self.preview_socket
        self.preview_socket = None
        if preview_socket is not None:
            preview_socket.close()

    def _profile_enabled_changed(self, profile: int) -> None:
        self._write_runtime_aim_settings()
        if self.process is not None:
            state = "启用" if self.profile_enabled[profile].get() else "禁用"
            self._append_log(f"控制方案 {profile + 1} 已{state}。")

    def _profile_trigger_changed(self, profile: int) -> None:
        other = 1 - profile
        if self.profile_trigger[profile].get() == self.profile_trigger[other].get():
            self.profile_trigger[profile].set(self._last_profile_triggers[profile])
            messagebox.showwarning(
                "触发键冲突",
                "两个控制方案不能使用同一个触发键。",
                parent=self.root,
            )
            return
        self._last_profile_triggers[profile] = self.profile_trigger[profile].get()
        self._write_runtime_aim_settings()
        if self.process is not None:
            self._append_log(f"控制方案 {profile + 1} 的触发键已切换为：{self.profile_trigger[profile].get()}。")

    def _provider_changed(self, _event=None) -> None:
        self._sync_cuda_graph_control()

    def _sync_cuda_graph_control(self) -> None:
        usable = self.process is None and PROVIDERS[self.provider.get()] in {"auto", "tensorrt"}
        state = "normal" if usable else "disabled"
        self.cuda_graph_check.configure(state=state)
        self.gpu_preprocess_check.configure(state=state)

    def _target_class_changed(self, profile: int) -> None:
        self._write_runtime_aim_settings()
        if self.process is not None:
            self._append_log(f"控制方案 {profile + 1} 的自瞄标签已切换为：{self.profile_target_class[profile].get()}。")

    def _output_format_changed(self, _event=None) -> None:
        self._update_target_class_choices()

    def _update_target_class_choices(self) -> None:
        count = None
        if self.model_contract is not None:
            count = self.model_contract.class_count_for(OUTPUT_FORMATS[self.output_format.get()])
        for index, combo in enumerate(self.target_class_combos):
            current = int(self.profile_target_class[index].get())
            limit = count if count is not None else max(7, current + 1)
            choices = [str(value) for value in range(limit)]
            combo.configure(values=choices)
            if current >= limit:
                self.profile_target_class[index].set("0")
                self._write_runtime_aim_settings()

    def _confidence_changed(self, value: str) -> None:
        rounded = round(float(value), 3)
        self.confidence.set(rounded)
        self.confidence_text.set(f"{rounded:.3f}")

    def _iou_changed(self, value: str) -> None:
        rounded = round(float(value), 3)
        self.iou.set(rounded)
        self.iou_text.set(f"{rounded:.3f}")

    def _kp_min_changed(self, profile: int, value: str) -> None:
        rounded = round(float(value), 3)
        self.profile_kp_min[profile].set(rounded)
        self.profile_kp_min_text[profile].set(f"{rounded:.3f}")
        if rounded > self.profile_kp_max[profile].get():
            self.profile_kp_max[profile].set(rounded)
            self.profile_kp_max_text[profile].set(f"{rounded:.3f}")
        self._write_runtime_aim_settings()

    def _kp_max_changed(self, profile: int, value: str) -> None:
        rounded = round(float(value), 3)
        self.profile_kp_max[profile].set(rounded)
        self.profile_kp_max_text[profile].set(f"{rounded:.3f}")
        if rounded < self.profile_kp_min[profile].get():
            self.profile_kp_min[profile].set(rounded)
            self.profile_kp_min_text[profile].set(f"{rounded:.3f}")
        self._write_runtime_aim_settings()

    def _kp_growth_changed(self, profile: int, value: str) -> None:
        rounded = round(float(value), 3)
        self.profile_kp_growth[profile].set(rounded)
        self.profile_kp_growth_text[profile].set(f"{rounded:.3f}")
        self._write_runtime_aim_settings()

    def _aim_position_changed(self, profile: int, value: str) -> None:
        rounded = round(float(value))
        self.profile_aim_position[profile].set(rounded)
        self.profile_aim_position_text[profile].set(f"{rounded:.0f}%")
        self._write_runtime_aim_settings()

    def _fov_changed(self, profile: int, value: str) -> None:
        rounded = round(float(value))
        self.profile_fov[profile].set(rounded)
        self.profile_fov_text[profile].set(f"{rounded:.0f}")
        self._write_runtime_aim_settings()

    def _write_runtime_aim_settings(self) -> None:
        if self.process is None:
            return
        try:
            state = self._form_state()
        except (tk.TclError, ValueError, KeyError):
            # 参数框是可以手打的, 打到一半时里面可能是空的或者半个数字。这一下
            # 跳过, 下一次有效的编辑会把完整设置推过去。
            #
            # 搬家带来的一点差别: 现在读的是整张表单, 不只是瞄准那几个字段, 所以
            # 置信度框打到一半也会跳过这一次热推。后果和原来一样 —— 下一次有效的
            # 编辑补上。
            return
        values = runtime_aim_payload(state, self._labels())
        self.runtime_aim_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_aim_file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(values), encoding="utf-8")
            temporary.replace(self.runtime_aim_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _stop(self) -> None:
        if self.process is None:
            return
        self.stop_requested = True
        self._set_status("正在停止", "#344054", "#eaecf0")
        # 写停止文件、超时才硬杀那一套归 session。这边只留看得见的两件事:
        # 记下「是我让它停的」(决定待会儿显示「已停止」还是「运行出错」),
        # 和把状态牌改成「正在停止」。
        self.session.stop()

    def _set_running(self, running: bool, status: str) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        # 模型和加速方式只在启动时读一次, 运行中改了也不生效, 干脆锁住; 载入预设会换模型, 一起锁。
        # 保存/另存为不锁: 边打边调好了, 正该当场存下来。
        self.preset_combo.configure(state="disabled" if running else "readonly")
        self.provider_combo.configure(state="disabled" if running else "readonly")
        self.model_entry.configure(state="disabled" if running else "normal")
        self.browse_model_button.configure(state="disabled" if running else "normal")
        self._sync_cuda_graph_control()
        if running:
            self._set_status(status, "#175cd3", "#dbeafe")
        else:
            color = "#b42318" if status == "运行出错" else "#344054"
            background = "#fee4e2" if status == "运行出错" else "#eaecf0"
            self._set_status(status, color, background)

    def _set_status(self, text: str, foreground: str, background: str) -> None:
        self.status.set(text)
        self.status_badge.configure(fg=foreground, bg=background)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        if self.process is not None:
            self.root.withdraw()
            self._stop()
            self._close_when_stopped()
            return
        self._close_preview_socket()
        self.root.destroy()

    def _close_when_stopped(self) -> None:
        if self.process is None:
            self.root.destroy()
        else:
            self.root.after(100, self._close_when_stopped)


def run_gui(config_path: Path, *, auto_start: bool = False) -> None:
    application = RhodesFastGui(config_path)
    if auto_start:
        application.root.after(300, application._start)
    application.run()


def main() -> None:
    run_gui(Path("settings.txt"))
