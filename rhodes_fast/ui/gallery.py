"""Runnable gallery for visually reviewing the reusable UI components."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QScrollArea, QVBoxLayout, QWidget

from .theme import apply_theme
from .widgets import (
    DetectionBox,
    DeviceStatusRow,
    IndustrialButton,
    IndustrialField,
    IndustrialIconButton,
    IndustrialSelect,
    LogConsole,
    MetricRail,
    PreviewCanvas,
    SectionPanel,
    SegmentedControl,
    Sidebar,
    StatusBadge,
    TabRail,
    TechSlider,
    TechSwitch,
)


ASSET_DIR = Path(__file__).with_name("assets")


class ComponentGallery(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Endfield UI Components")
        self.resize(1600, 1100)
        shell = QWidget()
        shell.setObjectName("appShell")
        self.setCentralWidget(shell)
        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar(ASSET_DIR / "endfield-logo.jpg")
        self.sidebar.set_current("control")
        root.addWidget(self.sidebar)
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        root.addLayout(main, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        canvas = QWidget()
        content = QVBoxLayout(canvas)
        content.setContentsMargins(30, 22, 30, 28)
        content.setSpacing(16)

        header = QHBoxLayout()
        title_stack = QVBoxLayout()
        kicker = QLabel("// RHODES CONTROL SYSTEM · COMPONENT STANDARD 02")
        kicker.setProperty("role", "eyebrow")
        heading = QLabel("ENDFIELD / UI")
        heading.setStyleSheet("font-size:50px;font-weight:900;letter-spacing:-2px")
        subtitle = QLabel("数据、输入与识别控制组件  /  INDUSTRIAL INTERFACE KIT")
        subtitle.setStyleSheet("font-size:14px;font-weight:700")
        title_stack.addWidget(kicker)
        title_stack.addWidget(heading)
        title_stack.addWidget(subtitle)
        header.addLayout(title_stack)
        header.addStretch(1)
        palette = QHBoxLayout()
        palette.setSpacing(4)
        for color, label in (("#101110", "INK"), ("#E6FF18", "SIGNAL"), ("#D5D7D0", "STEEL")):
            swatch = QLabel(label)
            swatch.setFixedSize(76, 48)
            swatch.setAlignment(Qt.AlignmentFlag.AlignCenter)
            foreground = "#F1F1EB" if color == "#101110" else "#101110"
            swatch.setStyleSheet(f"background:{color};color:{foreground};font-size:9px;font-weight:800")
            palette.addWidget(swatch)
        header.addLayout(palette)
        content.addLayout(header)

        metric_data = (("FPS", "238", ""), ("推理", "2.4", "ms"), ("P95", "4.8", "ms"), ("GPU", "62", "%"))
        content.addWidget(MetricRail(metric_data))

        work = QHBoxLayout()
        work.setSpacing(12)
        fields = SectionPanel("输入状态", "01", "FIELD STATES")
        fields.addWidget(TabRail((("udp", "UDP"), ("obs", "OBS"), ("file", "本地文件")), "udp"))
        fields.addWidget(IndustrialField("模型路径", "D:/models/person_yolov5.onnx", suffix="ONNX"))
        fields.addWidget(IndustrialField("输入地址", "udp://192.168.1.20:4455", state="focused", suffix="UDP"))
        fields.addWidget(IndustrialField("设备标识", "KMBOX-A01", state="read_only", suffix="HID"))
        fields.addWidget(IndustrialField("无效端口", "99999", state="error", suffix="PORT"))
        work.addWidget(fields, 5)

        controls = SectionPanel("识别参数", "02", "DETECTION CONTROL")
        mode_label = QLabel("COMPUTE BACKEND")
        mode_label.setProperty("role", "eyebrow")
        controls.addWidget(mode_label)
        controls.addWidget(SegmentedControl((("trt", "TensorRT"), ("cuda", "CUDA"), ("cpu", "CPU")), "trt"))
        controls.addWidget(IndustrialSelect("模型精度", ("FP16", "FP32", "INT8"), "FP16"))
        controls.addWidget(TechSlider("置信度", 0.05, 0.95, 0.375))
        controls.addWidget(TechSlider("NMS IoU", 0.05, 0.95, 0.5))
        controls.addWidget(TechSlider("视野半径", 10, 320, 150, decimals=0, suffix=" px"))
        button_row = QHBoxLayout()
        button_row.addWidget(IndustrialButton("应用配置", "primary"))
        button_row.addWidget(IndustrialButton("校准设备", "secondary"))
        controls.addLayout(button_row)
        work.addWidget(controls, 6)

        states = SectionPanel("系统状态", "03", "SYSTEM STATE")
        preprocessing = TechSwitch("GPU 预处理")
        preprocessing.setChecked(True)
        tracking = TechSwitch("目标追踪")
        tracking.setChecked(True)
        telemetry = TechSwitch("调试遥测")
        states.addWidget(preprocessing)
        states.addWidget(tracking)
        states.addWidget(telemetry)
        states.addWidget(DeviceStatusRow("KMBOX", "已连接", "USB HID"))
        states.addWidget(DeviceStatusRow("CUDA", "可用", "RTX 4070"))
        states.addWidget(StatusBadge("PIPELINE READY", "online"))
        tool_row = QHBoxLayout()
        tool_row.addWidget(IndustrialIconButton("export", "导出配置", "secondary"))
        tool_row.addWidget(IndustrialIconButton("refresh", "刷新设备"))
        tool_row.addWidget(IndustrialIconButton("more", "更多操作"))
        tool_row.addStretch(1)
        states.addLayout(tool_row)
        work.addWidget(states, 4)
        content.addLayout(work)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        preview_panel = SectionPanel("识别画布", "04", "LIVE PREVIEW · 30 FPS CAP")
        self.preview = PreviewCanvas(max_fps=30)
        self.preview.set_active(True)
        self.preview.set_detections(
            (
                DetectionBox(0.16, 0.28, 0.12, 0.48, "person", 0.87),
                DetectionBox(0.46, 0.20, 0.15, 0.58, "person", 0.92, True),
                DetectionBox(0.74, 0.33, 0.11, 0.41, "person", 0.76),
            )
        )
        preview_panel.addWidget(self.preview, 1)
        lower.addWidget(preview_panel, 3)
        log_panel = SectionPanel("运行日志", "05", "RUNTIME LOG · 250 LINES")
        log = LogConsole(max_lines=250)
        for line in (
            "系统启动完成，等待视频输入…",
            "UDP 视频源已连接 192.168.1.20:4455",
            "TensorRT FP16 引擎加载成功",
            "KMBOX 设备已连接，控制通道可用",
        ):
            log.append_line(line)
        log_panel.addWidget(log, 1)
        lower.addWidget(log_panel, 2)
        content.addLayout(lower, 1)
        scroll.setWidget(canvas)
        main.addWidget(scroll, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview reusable Endfield UI components")
    parser.add_argument("--screenshot", type=Path, help="save a screenshot and exit")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_theme(app)
    window = ComponentGallery()
    window.show()
    if args.screenshot:
        target = args.screenshot.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        def capture() -> None:
            window.grab().save(str(target), "PNG")
            app.quit()

        QTimer.singleShot(350, capture)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    main()
