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
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from .config import AppConfig, load_config, save_config


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
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build_style()
        self._create_variables()
        self._build_ui()
        self._switch_input_panel()
        if self.config.model.path.is_file():
            self.root.after(120, lambda: self._inspect_selected_model(self.config.model.path))
        self.root.after(80, self._drain_messages)
        self.root.after(PREVIEW_POLL_MS, self._drain_preview)

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
        self._last_profile_triggers = [variable.get() for variable in self.profile_trigger]
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

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        run_tab = ttk.Frame(self.notebook, padding=8)
        advanced_tab = ttk.Frame(self.notebook, padding=14)
        self.preview_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(run_tab, text="运行设置")
        self.notebook.add(advanced_tab, text="识别与控制")
        self.notebook.add(self.preview_tab, text="实时预览")

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
        model_entry = ttk.Entry(model_box, textvariable=self.model_path)
        model_entry.grid(row=0, column=1, sticky="ew", pady=5)
        model_entry.bind("<Return>", self._model_path_edited)
        model_entry.bind("<FocusOut>", self._model_path_edited)
        ttk.Button(model_box, text="浏览...", command=self._browse_model).grid(row=0, column=2, padx=(8, 0), pady=5)
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
            trigger_combo = self._combo_row(panel, 1, "触发方式", self.profile_trigger[index], TRIGGERS)
            trigger_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, profile=index: self._profile_trigger_changed(profile),
            )
            ttk.Label(panel, text="目标标签").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
            target_class_combo = ttk.Combobox(
                panel,
                textvariable=self.profile_target_class[index],
                values=initial_classes,
                state="readonly",
                width=20,
            )
            target_class_combo.grid(row=2, column=1, sticky="w", pady=4)
            target_class_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, profile=index: self._target_class_changed(profile),
            )
            self.target_class_combos.append(target_class_combo)
            self._slider_row(
                panel,
                3,
                "框内位置（顶部 0%）",
                self.profile_aim_position[index],
                self.profile_aim_position_text[index],
                0,
                100,
                lambda value, profile=index: self._aim_position_changed(profile, value),
            )
            self._slider_row(
                panel,
                4,
                "视野半径",
                self.profile_fov[index],
                self.profile_fov_text[index],
                10,
                320,
                lambda value, profile=index: self._fov_changed(profile, value),
            )
            self._slider_row(
                panel,
                5,
                "P 最小值",
                self.profile_kp_min[index],
                self.profile_kp_min_text[index],
                0.0,
                0.3,
                lambda value, profile=index: self._kp_min_changed(profile, value),
            )
            self._slider_row(
                panel,
                6,
                "P 最大值",
                self.profile_kp_max[index],
                self.profile_kp_max_text[index],
                0.0,
                0.3,
                lambda value, profile=index: self._kp_max_changed(profile, value),
            )
            self._slider_row(
                panel,
                7,
                "P 增长斜率",
                self.profile_kp_growth[index],
                self.profile_kp_growth_text[index],
                0.0,
                0.5,
                lambda value, profile=index: self._kp_growth_changed(profile, value),
            )

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
        selected = filedialog.askopenfilename(
            title="选择 ONNX 模型",
            initialdir=str(Path(self.model_path.get()).parent),
            filetypes=[("ONNX 模型", "*.onnx"), ("所有文件", "*.*")],
        )
        if selected:
            self.model_path.set(selected)
            self._inspect_selected_model(Path(selected))

    def _inspect_selected_model(self, path: Path) -> None:
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
        path = Path(self.model_path.get().strip())
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
        if Path(self.model_path.get()) != path:
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
            path=Path(self.model_path.get().strip()),
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
        )
        return replace(
            self.config,
            input=replace(self.config.input, mode=INPUT_MODES[self.input_mode.get()]),
            ui=replace(self.config.ui, language=LOG_LANGUAGES[self.log_language.get()]),
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
            if self._preview_tab_selected():
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
            self.process = None
            messagebox.showerror("无法启动", str(exc), parent=self.root)
            return
        self.preview_socket = preview_socket
        self._write_runtime_aim_settings()
        if preview_socket is not None:
            self._discard_preview_frames()
            if self._preview_tab_selected():
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
                if Path(self.model_path.get()) == path:
                    self.model_contract = None
                    self._update_target_class_choices()
                    self._set_status("模型需检查", "#b42318", "#fee4e2")
                    self._append_log(f"模型无法读取：{error}")
            else:
                code = int(payload)
                self.process = None
                self._close_preview_socket()
                self.runtime_aim_file.unlink(missing_ok=True)
                self.stop_file.unlink(missing_ok=True)
                stopped_normally = code == 0 or self.stop_requested
                self._set_running(False, "已停止" if stopped_normally else "运行出错")
                self.stop_requested = False
        self.root.after(80, self._drain_messages)

    def _display_preview(self, frame: np.ndarray) -> None:
        if not self._preview_tab_selected():
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
            if self.process is not None and self.preview_socket is not None:
                self.preview_enable_file.touch()
                self._show_preview_message("正在等待第一帧...")
            else:
                self._show_preview_message("启动后将在这里显示识别画面")
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
        state = "normal" if PROVIDERS[self.provider.get()] in {"auto", "tensorrt"} else "disabled"
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
            }
            for index in range(2)
        ]
        values = {
            # Top-level keys mirror profile 1 for older runtime consumers.
            "target_class": profiles[0]["target_class"],
            "target_y_ratio": profiles[0]["target_y_ratio"],
            "fov_radius": profiles[0]["fov_radius"],
            "profiles": profiles,
        }
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
