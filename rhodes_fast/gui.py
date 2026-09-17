from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from .aim_algorithms import Param, available_algorithms, set_installed_algorithms
from .algorithm_library import (
    DuplicateAlgorithm,
    LibraryError,
    inspect_candidate,
    install,
    load_installed,
    read_registry,
    rename,
    uninstall,
)
from .config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS, AppConfig, load_config, save_config
from .latency_log import MEASUREMENT_NAME, load_measurement
from .presets import (
    DIRECTORY_NAME as PRESETS_DIRECTORY,
    Preset,
    PresetError,
    delete_preset,
    find_preset,
    list_presets,
    preset_from_config,
    read_preset,
    same_settings,
    validate_name,
    write_preset,
)
from .trail import TrailSettings, write_trail_settings
from .tuning_share import Tuning, TuningError, delay_warning, dump_tuning, load_tuning


# 下拉框里显示「有改动没存进预设」的节拍。只读一遍表单, 300 毫秒足够跟手又不占事。
PRESET_POLL_MS = 300
NO_PRESET = "（未选择预设）"
# 停手这么久之后才把轨迹长度写回 settings.txt。
TRAIL_PERSIST_DELAY_MS = 400
NOTHING_TO_PREVIEW = "勾选「画面」或「轨迹」后在这里显示"

INPUT_MODES = {
    "UDP 视频流 (MPEG-TS/H.264)": "udp_video",
    "UDP 单包 JPEG": "udp_jpeg",
    "OBS WebSocket": "obs_websocket",
}
PROVIDERS = {"自动（推荐）": "auto", "TensorRT FP16": "tensorrt", "CUDA": "cuda", "CPU": "cpu"}
OUTPUT_FORMATS = {"YOLOv5": "yolov5", "YOLOv8": "yolov8", "端到端 NMS": "end2end"}
TRIGGERS = {
    "鼠标侧键 1": "side1",
    "鼠标侧键 2": "side2",
    "鼠标左键": "left",
    "鼠标右键": "right",
}
LOG_LANGUAGES = {"中文": "zh", "English": "en"}


def _display_value(mapping: dict[str, str], stored: str) -> str:
    return next((label for label, value in mapping.items() if value == stored), next(iter(mapping)))


def _display_path(path: Path, base_directory: Path) -> str:
    try:
        return path.resolve().relative_to(base_directory.resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolve_model_path(path: str | Path, base_directory: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    return candidate.resolve()


def algorithm_choices() -> dict[str, str]:
    """界面显示名 -> 算法标识。和 TRIGGERS 那几个映射同一个写法。"""
    return {
        algorithm.DISPLAY_NAME: name
        for name, algorithm in sorted(available_algorithms().items())
    }


def algorithm_param_specs(name: str) -> tuple[Param, ...]:
    algorithm = available_algorithms().get(name)
    # 配置里指着一个已删掉的算法时界面仍要画得出来, 所以不抛异常。
    return algorithm.PARAMS if algorithm is not None else ()


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


class RhodesFastGui:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.config = load_config(self.config_path, validate_model=False)
        # 下拉框要能列出用户自己装的算法, 所以界面也得加载一次算法库。
        self.algorithms_dir = self.config_path.parent / "algorithms"
        installed, self.library_warnings = load_installed(self.algorithms_dir)
        set_installed_algorithms(installed)
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
        self.presets_dir = self.config_path.parent / PRESETS_DIRECTORY
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

    def _create_variables(self) -> None:
        cfg = self.config
        self.model_path = tk.StringVar(value=_display_path(cfg.model.path, self.config_path.parent))
        self.provider = tk.StringVar(value=_display_value(PROVIDERS, cfg.model.provider))
        self.cuda_graph = tk.BooleanVar(value=cfg.model.cuda_graph)
        self.gpu_preprocess = tk.BooleanVar(value=cfg.model.gpu_preprocess)
        self.output_format = tk.StringVar(value=_display_value(OUTPUT_FORMATS, cfg.model.output_format))
        self.confidence = tk.DoubleVar(value=cfg.model.confidence)
        self.confidence_text = tk.StringVar(value=f"{cfg.model.confidence:.3f}")
        self.iou = tk.DoubleVar(value=cfg.model.iou)
        self.iou_text = tk.StringVar(value=f"{cfg.model.iou:.3f}")
        self.input_mode = tk.StringVar(value=_display_value(INPUT_MODES, cfg.input.mode))
        self.log_language = tk.StringVar(value=_display_value(LOG_LANGUAGES, cfg.ui.language))
        self.udp_host = tk.StringVar(value=cfg.udp.host)
        self.udp_port = tk.StringVar(value=str(cfg.udp.port))
        self.udp_width = tk.StringVar(value=str(cfg.udp.width))
        self.udp_height = tk.StringVar(value=str(cfg.udp.height))
        self.obs_host = tk.StringVar(value=cfg.obs.host)
        self.obs_port = tk.StringVar(value=str(cfg.obs.port))
        self.obs_password = tk.StringVar(value=cfg.obs.password)
        self.obs_source = tk.StringVar(value=cfg.obs.source_name)
        self.kmbox_enabled = tk.BooleanVar(value=cfg.kmbox.enabled)
        self.latency_log_enabled = tk.BooleanVar(value=False)
        # 预览页的勾选框每次打开都是「只看画面」, 不从设置里恢复; 只有轨迹长度记住。
        defaults = TrailSettings()
        self.preview_frame = tk.BooleanVar(value=defaults.show_frame)
        self.trail_enabled = tk.BooleanVar(value=defaults.enabled)
        self.trail_optimal_path = tk.BooleanVar(value=defaults.optimal_path)
        self.trail_seconds = tk.DoubleVar(value=cfg.ui.trail_seconds)
        self.kmbox_host = tk.StringVar(value=cfg.kmbox.host)
        self.kmbox_port = tk.StringVar(value=str(cfg.kmbox.port))
        self.kmbox_uuid = tk.StringVar(value=cfg.kmbox.uuid)
        self.profile_enabled = [tk.BooleanVar(value=profile.enabled) for profile in cfg.aim_profiles]
        self.profile_trigger = [
            tk.StringVar(value=_display_value(TRIGGERS, profile.trigger)) for profile in cfg.aim_profiles
        ]
        self.profile_target_class = [tk.StringVar(value=str(profile.target_class)) for profile in cfg.aim_profiles]
        self.profile_aim_position = [
            tk.DoubleVar(value=profile.target_y_ratio * 100.0) for profile in cfg.aim_profiles
        ]
        self.profile_aim_position_text = [
            tk.StringVar(value=f"{profile.target_y_ratio * 100.0:.0f}%") for profile in cfg.aim_profiles
        ]
        self.profile_fov = [tk.DoubleVar(value=profile.fov_radius) for profile in cfg.aim_profiles]
        self.profile_fov_text = [tk.StringVar(value=f"{profile.fov_radius:.0f}") for profile in cfg.aim_profiles]
        self.profile_kp_min = [tk.DoubleVar(value=profile.kp_min) for profile in cfg.aim_profiles]
        self.profile_kp_min_text = [tk.StringVar(value=f"{profile.kp_min:.3f}") for profile in cfg.aim_profiles]
        self.profile_kp_max = [tk.DoubleVar(value=profile.kp_max) for profile in cfg.aim_profiles]
        self.profile_kp_max_text = [tk.StringVar(value=f"{profile.kp_max:.3f}") for profile in cfg.aim_profiles]
        self.profile_kp_growth = [tk.DoubleVar(value=profile.kp_growth) for profile in cfg.aim_profiles]
        self.profile_kp_growth_text = [
            tk.StringVar(value=f"{profile.kp_growth:.3f}") for profile in cfg.aim_profiles
        ]
        self.profile_algorithm = [
            tk.StringVar(value=_display_value(algorithm_choices(), profile.algorithm))
            for profile in cfg.aim_profiles
        ]
        self.profile_algorithm_params: list[dict[str, tk.DoubleVar]] = [
            {
                spec.name: tk.DoubleVar(
                    value=profile.algorithm_params.get(spec.name, spec.default)
                )
                for spec in algorithm_param_specs(profile.algorithm)
            }
            for profile in cfg.aim_profiles
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
        installed, warnings = load_installed(self.algorithms_dir)
        set_installed_algorithms(installed)
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
        """内置 ∪ 注册表。

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
        # 第一步只读文本和 ast。这个文件到这里为止一行都没有执行过。
        try:
            candidate = inspect_candidate(Path(source))
        except LibraryError as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            return
        if not candidate.name:
            messagebox.showerror("导入失败", "源码里找不到带 NAME 的算法类。", parent=self.root)
            return
        if not self._confirm_import(candidate):
            return
        self._install_candidate(candidate, replace_existing=False)

    def _confirm_import(self, candidate) -> bool:
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
            answer = messagebox.askyesnocancel("导入算法", message, parent=self.root)
            if answer is None:
                return False
            if not answer:
                return True
            self._show_source_window(candidate.path.name, candidate.source)

    def _install_candidate(self, candidate, *, replace_existing: bool) -> None:
        try:
            entry = install(
                self.algorithms_dir, candidate.path, replace_existing=replace_existing
            )
        except DuplicateAlgorithm as clash:
            replace_it = messagebox.askokcancel(
                "已有同名算法",
                f"已存在同名算法「{clash.name}」（来自 {clash.existing_file}）。要替换吗？\n\n"
                "替换会保留你给它起的显示名。",
                parent=self.root,
            )
            if replace_it:
                self._install_candidate(candidate, replace_existing=True)
            return
        except LibraryError as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            return
        self._reload_algorithm_library()
        self._refresh_library()
        messagebox.showinfo(
            "导入成功",
            f"「{entry.display_name}」已装进算法库，现在就能在控制方案里选它。",
            parent=self.root,
        )
        self._append_log(f"算法库新增「{entry.display_name}」（{entry.name}）。")

    def _show_algorithm_source(self) -> None:
        selected = self._selected_algorithm()
        if selected is None:
            return
        entry = read_registry(self.algorithms_dir).get(selected[0])
        if entry is None:
            return
        path = self.algorithms_dir / entry.source_file
        try:
            self._show_source_window(entry.source_file, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            messagebox.showerror("打不开源码", str(error), parent=self.root)

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
        entry = read_registry(self.algorithms_dir).get(selected[0])
        if entry is None:
            return
        new_name = simpledialog.askstring(
            "重命名算法",
            f"给「{entry.display_name}」起个新的显示名。\n\n"
            f"标识 {entry.name} 不会变——别人发来的调校认的是标识。",
            initialvalue=entry.display_name,
            parent=self.root,
        )
        if new_name is None:
            return
        try:
            rename(self.algorithms_dir, entry.name, new_name)
        except LibraryError as error:
            messagebox.showerror("改名失败", str(error), parent=self.root)
            return
        self._reload_algorithm_library()
        self._refresh_library()
        self._append_log(
            f"算法「{entry.name}」的显示名已改为「{new_name.strip()}」。"
        )

    def _delete_algorithm(self) -> None:
        selected = self._selected_algorithm()
        if selected is None or selected[1] != "已导入":
            return
        entry = read_registry(self.algorithms_dir).get(selected[0])
        if entry is None:
            return
        if not messagebox.askokcancel(
            "删除算法",
            f"要删掉「{entry.display_name}」吗？\n\n"
            f"{entry.source_file} 会被删除。正指着它的控制方案会当场改回比例控制。",
            parent=self.root,
        ):
            return
        try:
            uninstall(self.algorithms_dir, entry.name)
        except LibraryError as error:
            messagebox.showerror("删除失败", str(error), parent=self.root)
            return
        self._reload_algorithm_library()
        self._refresh_library()
        self._append_log(f"算法「{entry.display_name}」已删除。")

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
        name = find_preset(self.presets_dir, self.config.ui.preset) if self.config.ui.preset else None
        if name is not None:
            try:
                # 模型不在也照样读: 这里只拿来比较有没有改动, 不往表单里填。
                self._preset_baseline = read_preset(
                    self.presets_dir, name, base_directory=self.config_path.parent, require_model=False
                )
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
        self.preset_combo.configure(values=list_presets(self.presets_dir))
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
            messagebox.showinfo("正在运行", "运行中不能载入预设，先停止再切换。", parent=self.root)
            return False
        if self._preset_changed() is not False:
            answer = messagebox.askyesnocancel(
                "切换预设",
                f"预设「{self.current_preset}」有改动还没保存。\n\n"
                "是：先存进这个预设再切换\n否：丢掉这些改动\n取消：留在当前预设",
                parent=self.root,
            )
            if answer is None or (answer and not self._save_preset()):
                return False
        try:
            preset = read_preset(
                self.presets_dir,
                name,
                base_directory=self.config_path.parent,
                known_algorithms=set(available_algorithms()),
            )
        except PresetError as error:
            messagebox.showerror("载入预设失败", str(error), parent=self.root)
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
        model = preset.model
        self.provider.set(_display_value(PROVIDERS, model.provider))
        self.cuda_graph.set(model.cuda_graph)
        self.gpu_preprocess.set(model.gpu_preprocess)
        self._sync_cuda_graph_control()
        self.output_format.set(_display_value(OUTPUT_FORMATS, model.output_format))
        for variable, text, value in (
            (self.confidence, self.confidence_text, model.confidence),
            (self.iou, self.iou_text, model.iou),
        ):
            variable.set(value)
            text.set(f"{value:.3f}")
        previous_model = _resolve_model_path(self.model_path.get().strip(), self.config_path.parent)
        self.model_path.set(_display_path(model.path, self.config_path.parent))
        self.input_mode.set(_display_value(INPUT_MODES, preset.input.mode))
        self._switch_input_panel()
        for variable, value in (
            (self.udp_host, preset.udp.host),
            (self.udp_port, str(preset.udp.port)),
            (self.udp_width, str(preset.udp.width)),
            (self.udp_height, str(preset.udp.height)),
            (self.obs_host, preset.obs.host),
            (self.obs_port, str(preset.obs.port)),
            (self.obs_password, preset.obs.password),
            (self.obs_source, preset.obs.source_name),
            (self.kmbox_host, preset.kmbox.host),
            (self.kmbox_port, str(preset.kmbox.port)),
            (self.kmbox_uuid, preset.kmbox.uuid),
        ):
            variable.set(value)
        self.kmbox_enabled.set(preset.kmbox.enabled)
        for index, profile in enumerate(preset.aim_profiles):
            self.profile_enabled[index].set(profile.enabled)
            self.profile_trigger[index].set(_display_value(TRIGGERS, profile.trigger))
            self.profile_target_class[index].set(str(profile.target_class))
            # 先清掉旧参数: 文件里没写的参数该用默认值, 不该沿用上一个预设留下的。
            self.profile_algorithm_params[index] = {}
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
        if previous_model != model.path or self.model_contract is None:
            self._inspect_selected_model(model.path)
        else:
            self._update_target_class_choices()

    def _save_preset(self) -> bool:
        if self.current_preset is None:
            return self._save_preset_as()
        return self._store_preset(self.current_preset)

    def _save_preset_as(self) -> bool:
        name = simpledialog.askstring(
            "另存为预设",
            "给现在这套设置起个名字：",
            initialvalue=self.current_preset or "",
            parent=self.root,
        )
        if name is None:
            return False
        try:
            name = validate_name(name)
        except PresetError as error:
            messagebox.showerror("这个名字不能用", str(error), parent=self.root)
            return False
        existing = find_preset(self.presets_dir, name)
        if (
            existing is not None
            and existing != self.current_preset
            and not messagebox.askokcancel(
                "覆盖预设",
                f"已经有一个叫「{existing}」的预设了。\n\n要用现在的设置覆盖它吗？",
                icon=messagebox.WARNING,
                default=messagebox.CANCEL,
                parent=self.root,
            )
        ):
            return False
        return self._store_preset(name)

    def _store_preset(self, name: str) -> bool:
        try:
            preset = preset_from_config(self._read_form())
            stored = write_preset(self.presets_dir, name, preset, base_directory=self.config_path.parent)
        except (OSError, tk.TclError, ValueError, KeyError) as error:
            messagebox.showerror("预设无法保存", str(error), parent=self.root)
            return False
        self.current_preset = stored
        self._preset_baseline = preset
        # 和载入一样顺手记住, 下次打开还是这个预设。
        self._save(quiet=True)
        self._refresh_preset_bar()
        self._append_log(f"预设「{stored}」已保存。")
        return True

    def _delete_preset(self) -> None:
        name = self.current_preset
        if name is None:
            return
        if not messagebox.askokcancel(
            "删除预设",
            f"确定删除预设「{name}」吗？\n\n删除后找不回来。界面上现在的设置不会变。",
            icon=messagebox.WARNING,
            # 默认按钮是取消: 手滑按回车删不掉。
            default=messagebox.CANCEL,
            parent=self.root,
        ):
            return
        try:
            delete_preset(self.presets_dir, name)
        except OSError as error:
            messagebox.showerror("删除失败", str(error), parent=self.root)
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
        if INPUT_MODES[self.input_mode.get()] == "obs_websocket":
            self.udp_panel.grid_remove()
            self.obs_panel.grid(row=1, column=0, columnspan=3, sticky="ew")
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

    def _read_form(self) -> AppConfig:
        model = replace(
            self.config.model,
            path=_resolve_model_path(self.model_path.get().strip(), self.config_path.parent),
            provider=PROVIDERS[self.provider.get()],
            cuda_graph=self.cuda_graph.get(),
            gpu_preprocess=self.gpu_preprocess.get(),
            output_format=OUTPUT_FORMATS[self.output_format.get()],
            output_layout="auto",
            confidence=self.confidence.get(),
            iou=self.iou.get(),
        )
        udp = replace(
            self.config.udp,
            host=self.udp_host.get().strip(),
            port=int(self.udp_port.get()),
            width=int(self.udp_width.get()),
            height=int(self.udp_height.get()),
        )
        obs = replace(
            self.config.obs,
            host=self.obs_host.get().strip(),
            port=int(self.obs_port.get()),
            password=self.obs_password.get(),
            source_name=self.obs_source.get().strip(),
        )
        kmbox = replace(
            self.config.kmbox,
            enabled=self.kmbox_enabled.get(),
            host=self.kmbox_host.get().strip(),
            port=int(self.kmbox_port.get()),
            uuid=self.kmbox_uuid.get().strip().upper(),
        )
        aim = replace(
            self.config.aim,
            # Kept in sync with profile 1 so older config consumers and the
            # pipeline benchmark still see meaningful values.
            target_class=int(self.profile_target_class[0].get()),
            target_y_ratio=max(0.0, min(1.0, self.profile_aim_position[0].get() / 100.0)),
            fov_radius=self.profile_fov[0].get(),
        )
        profile_1 = replace(
            self.config.aim_profile_1,
            enabled=self.profile_enabled[0].get(),
            trigger=TRIGGERS[self.profile_trigger[0].get()],
            kp_min=self.profile_kp_min[0].get(),
            kp_max=self.profile_kp_max[0].get(),
            kp_growth=self.profile_kp_growth[0].get(),
            target_class=int(self.profile_target_class[0].get()),
            target_y_ratio=max(0.0, min(1.0, self.profile_aim_position[0].get() / 100.0)),
            fov_radius=self.profile_fov[0].get(),
            algorithm=algorithm_choices()[self.profile_algorithm[0].get()],
            algorithm_params={
                name: variable.get()
                for name, variable in self.profile_algorithm_params[0].items()
            },
        )
        profile_2 = replace(
            self.config.aim_profile_2,
            enabled=self.profile_enabled[1].get(),
            trigger=TRIGGERS[self.profile_trigger[1].get()],
            kp_min=self.profile_kp_min[1].get(),
            kp_max=self.profile_kp_max[1].get(),
            kp_growth=self.profile_kp_growth[1].get(),
            target_class=int(self.profile_target_class[1].get()),
            target_y_ratio=max(0.0, min(1.0, self.profile_aim_position[1].get() / 100.0)),
            fov_radius=self.profile_fov[1].get(),
            algorithm=algorithm_choices()[self.profile_algorithm[1].get()],
            algorithm_params={
                name: variable.get()
                for name, variable in self.profile_algorithm_params[1].items()
            },
        )
        trail = self._current_trail_settings()
        return replace(
            self.config,
            input=replace(self.config.input, mode=INPUT_MODES[self.input_mode.get()]),
            ui=replace(
                self.config.ui,
                language=LOG_LANGUAGES[self.log_language.get()],
                preset=self.current_preset or "",
                trail_seconds=trail.seconds,
            ),
            udp=udp,
            obs=obs,
            model=model,
            kmbox=kmbox,
            aim=aim,
            aim_profile_1=profile_1,
            aim_profile_2=profile_2,
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
        try:
            seconds = float(self.trail_seconds.get())
        except (tk.TclError, ValueError):
            seconds = self.config.ui.trail_seconds
        # 滑条是连续的, 存 0.1 秒一档: 设置文件里不该出现 1.2749 这种数。
        seconds = round(min(TRAIL_MAX_SECONDS, max(TRAIL_MIN_SECONDS, seconds)) * 10) / 10
        return TrailSettings(
            show_frame=bool(self.preview_frame.get()),
            enabled=bool(self.trail_enabled.get()),
            seconds=seconds,
            optimal_path=bool(self.trail_optimal_path.get()),
        )

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
        python = Path(sys.executable).with_name("python.exe")
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.unlink(missing_ok=True)
        self.preview_enable_file.unlink(missing_ok=True)
        self.runtime_aim_file.unlink(missing_ok=True)
        self.trail_settings_file.unlink(missing_ok=True)
        command = [
            str(python),
            "-u",
            "-m",
            "rhodes_fast",
            "--config",
            str(self.config_path),
            "--stop-file",
            str(self.stop_file),
            "--runtime-aim-file",
            str(self.runtime_aim_file),
        ]
        preview_socket = None
        if not arguments:
            preview_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            preview_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_048_576)
            preview_socket.bind(("127.0.0.1", 0))
            preview_socket.settimeout(0.25)
            command.extend(["--preview-port", str(preview_socket.getsockname()[1])])
            command.extend(["--preview-enable-file", str(self.preview_enable_file)])
            # 启动前就写好: 管线一开始读到的就是当前设置, 而不是默认值。
            self._write_trail_settings_file()
            command.extend(["--trail-settings-file", str(self.trail_settings_file)])
            if self._preview_tab_selected() and self._something_to_preview():
                self.preview_enable_file.touch()
            if self.latency_log_enabled.get():
                stamp = time.strftime("%Y%m%d-%H%M%S")
                latency_log = self.config_path.parent / f"latency-{stamp}.csv"
                command.extend(["--latency-log", str(latency_log)])
                self._append_log(f"延迟日志将记录到 {latency_log.name}（每帧都记，停止时给出估计）。")
        command.extend(arguments)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.config_path.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
                env=environment,
            )
        except Exception as exc:
            if preview_socket is not None:
                preview_socket.close()
            self.preview_enable_file.unlink(missing_ok=True)
            self.runtime_aim_file.unlink(missing_ok=True)
            self.trail_settings_file.unlink(missing_ok=True)
            self.process = None
            messagebox.showerror("无法启动", str(exc), parent=self.root)
            return
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
        threading.Thread(target=self._read_process, args=(self.process,), daemon=True).start()

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self.messages.put(("log", line.rstrip()))
        self.messages.put(("done", process.wait()))

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
            profiles = [
                {
                    "enabled": self.profile_enabled[index].get(),
                    "trigger": TRIGGERS[self.profile_trigger[index].get()],
                    "kp_min": self.profile_kp_min[index].get(),
                    "kp_max": self.profile_kp_max[index].get(),
                    "kp_growth": self.profile_kp_growth[index].get(),
                    "target_class": int(self.profile_target_class[index].get()),
                    "target_y_ratio": max(0.0, min(1.0, self.profile_aim_position[index].get() / 100.0)),
                    "fov_radius": self.profile_fov[index].get(),
                    "algorithm": algorithm_choices()[self.profile_algorithm[index].get()],
                    "algorithm_params": {
                        name: variable.get()
                        for name, variable in self.profile_algorithm_params[index].items()
                    },
                }
                for index in range(2)
            ]
        except (tk.TclError, ValueError, KeyError):
            # 参数框是可以手打的, 打到一半时里面可能是空的或者半个数字。这一下跳过,
            # 下一次有效的编辑会把完整设置推过去。
            return
        values = {
            # Top-level keys mirror profile 1 for older runtime consumers.
            "target_class": profiles[0]["target_class"],
            "target_y_ratio": profiles[0]["target_y_ratio"],
            "fov_radius": profiles[0]["fov_radius"],
            "profiles": profiles,
        }
        self.runtime_aim_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_aim_file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(values), encoding="utf-8")
            temporary.replace(self.runtime_aim_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _stop(self) -> None:
        process = self.process
        if process is None:
            return
        self.stop_requested = True
        self._set_status("正在停止", "#344054", "#eaecf0")
        self.stop_file.touch()

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
