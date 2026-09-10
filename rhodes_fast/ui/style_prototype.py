"""PROTOTYPE — three disposable layout directions for the Endfield UI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .theme import THEME, apply_theme
from .widgets import (
    DeviceStatusRow,
    IndustrialButton,
    IndustrialField,
    IndustrialSelect,
    LogConsole,
    LogoWidget,
    MetricRail,
    SegmentedControl,
    StatusBadge,
    TabRail,
    TechSlider,
    TechSwitch,
)


ASSET_DIR = Path(__file__).with_name("assets")
VARIANTS = ("A", "B", "C")


def _label(text: str, size: int = 12, *, bold: bool = False, muted: bool = False) -> QLabel:
    label = QLabel(text)
    color = THEME.text_muted if muted else THEME.text
    label.setStyleSheet(f"font-size:{size}px;font-weight:{800 if bold else 400};color:{color}")
    return label


def _display(text: str, size: int) -> QLabel:
    label = QLabel(text)
    font = QFont("Arial Black")
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.Black)
    font.setStretch(82)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -2)
    label.setFont(font)
    label.setMinimumHeight(round(size * 1.12))
    return label


class SectionTitle(QWidget):
    def __init__(self, index: str, title: str, subtitle: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(7)
        row = QHBoxLayout()
        number = _label(index, 13, bold=True)
        number.setStyleSheet(f"font-size:13px;font-weight:900;color:{THEME.acid}")
        row.addWidget(number)
        row.addWidget(_label(title, 18, bold=True))
        row.addWidget(_label(subtitle.upper(), 9, muted=True))
        row.addStretch(1)
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background:{THEME.border};border:0")
        layout.addLayout(row)
        layout.addWidget(rule)


class PosterHeader(QWidget):
    def __init__(self, headline: str, section: str, title: str, english: str):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        hero = QHBoxLayout()
        logo = LogoWidget(ASSET_DIR / "endfield-logo.jpg")
        logo.setFixedWidth(115)
        hero.addWidget(logo)
        statement = _label("A CLEANER TOMORROW\nTHROUGH INDUSTRY\nFOR A LARGER WORLD.", 9, muted=True)
        statement.setFixedWidth(190)
        hero.addWidget(statement)
        hero.addSpacing(20)
        hero.addWidget(_display(f"// {headline}", 76), 1)
        hero.addWidget(_label("TERRA\nINDUSTRY\nPEOPLE\nA SHARED\nTOMORROW.", 8), 0, Qt.AlignmentFlag.AlignRight)
        outer.addLayout(hero)
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background:{THEME.border};border:0")
        outer.addWidget(rule)
        band = QHBoxLayout()
        band.addWidget(_display(section, 56))
        band.addWidget(_label(title, 26, bold=True))
        band.addWidget(_label(english.upper(), 14, muted=True))
        band.addStretch(1)
        band.addWidget(_label("// INDUSTRIAL INTERFACE\n// OPERATIONAL TOOLS\n// BUILT FOR A LARGER TOMORROW.", 8, muted=True))
        outer.addLayout(band)


class ScenePreview(QWidget):
    """Static scene used only to judge overlay composition in this prototype."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(520, 260)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#7D817C"))
        painter.setPen(QPen(QColor("#AEB1AB"), 1))
        horizon = self.height() * 0.58
        for x in range(0, self.width(), 54):
            painter.drawLine(x, int(horizon), x, self.height())
        for y in range(int(horizon), self.height(), 34):
            painter.drawLine(0, y, self.width(), y)
        painter.fillRect(QRectF(0, horizon, self.width(), self.height() - horizon), QColor("#555954"))
        painter.fillRect(QRectF(55, 70, 100, horizon - 70), QColor("#454945"))
        painter.fillRect(QRectF(self.width() - 170, 48, 130, horizon - 48), QColor("#383C39"))
        painter.setPen(QPen(QColor("#252825"), 5))
        painter.drawLine(210, 22, 210, int(horizon))
        painter.drawLine(210, 28, 385, 74)
        center = QPointF(self.width() * 0.58, self.height() * 0.5)
        radius = min(self.width(), self.height()) * 0.28
        painter.setPen(QPen(QColor("#F1F1EB"), 1, Qt.PenStyle.DashLine))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(QPen(QColor(THEME.acid), 2))
        target = QRectF(center.x() - 42, center.y() - 68, 84, 150)
        painter.drawRect(target)
        painter.fillRect(QRectF(target.left(), target.top() - 23, 95, 23), QColor(THEME.acid))
        painter.setPen(QColor(THEME.text))
        painter.drawText(QRectF(target.left() + 5, target.top() - 23, 90, 23), Qt.AlignmentFlag.AlignVCenter, "person 0.92")
        painter.setPen(QPen(QColor(THEME.paper), 1))
        for x in (self.width() * 0.27, self.width() * 0.82):
            painter.drawRect(QRectF(x - 32, center.y() - 52, 64, 125))
        painter.drawLine(center.x() - 12, center.y(), center.x() + 12, center.y())
        painter.drawLine(center.x(), center.y() - 12, center.x(), center.y() + 12)


def _fields() -> QWidget:
    block = QWidget()
    layout = QVBoxLayout(block)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(SectionTitle("1.", "输入字段", "Input fields"))
    layout.addWidget(IndustrialField("ONNX 模型路径", "D:/models/endfield/person_yolov5.onnx", suffix="FILE"))
    layout.addWidget(IndustrialField("监听地址", "0.0.0.0", state="focused", suffix="IP"))
    layout.addWidget(IndustrialField("只读端口", "8080", state="read_only", suffix="UDP"))
    layout.addWidget(IndustrialField("错误端口", "abc", state="error", suffix="!"))
    layout.addStretch(1)
    return block


def _controls() -> QWidget:
    block = QWidget()
    layout = QVBoxLayout(block)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(9)
    layout.addWidget(SectionTitle("2.", "下拉选择 / 分段选择", "Dropdown & segmented"))
    row = QHBoxLayout()
    row.addWidget(IndustrialSelect("模型精度", ("TensorRT FP16", "FP32", "INT8"), "TensorRT FP16"))
    row.addWidget(SegmentedControl((("cuda", "CUDA"), ("cpu", "CPU"), ("trt", "TensorRT")), "cuda"))
    layout.addLayout(row)
    layout.addWidget(SectionTitle("3.", "精度滑条", "Precision sliders"))
    layout.addWidget(TechSlider("置信度", 0.0, 1.0, 0.375))
    layout.addWidget(TechSlider("NMS IoU", 0.0, 1.0, 0.5))
    layout.addWidget(TechSlider("视野半径", 0, 300, 150, decimals=0, suffix=" px"))
    layout.addStretch(1)
    return block


def _states() -> QWidget:
    block = QWidget()
    layout = QVBoxLayout(block)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(9)
    layout.addWidget(SectionTitle("4.", "开关与状态", "Toggles & status"))
    for text, checked in (("GPU 预处理", True), ("CUDA Graph", True), ("启用目标控制", False)):
        switch = TechSwitch(text)
        switch.setChecked(checked)
        layout.addWidget(switch)
    layout.addWidget(DeviceStatusRow("CONLIOX", "ONLINE", "视频流正常"))
    layout.addWidget(DeviceStatusRow("MODEL", "READY", "引擎已加载", "neutral"))
    layout.addWidget(StatusBadge("SYSTEM ONLINE", "online"))
    layout.addStretch(1)
    return block


class VariantA(QWidget):
    """Closest translation of the approved component-board composition."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 18, 28, 24)
        layout.setSpacing(12)
        layout.addWidget(PosterHeader("CONTROL MODULES", "02", "数据与控制组件", "Data & control components"))
        top = QHBoxLayout()
        top.setSpacing(18)
        top.addWidget(_fields(), 5)
        top.addWidget(_controls(), 6)
        top.addWidget(_states(), 4)
        layout.addLayout(top, 3)
        lower = QHBoxLayout()
        lower.setSpacing(18)
        left = QVBoxLayout()
        left.addWidget(SectionTitle("6.", "指标显示", "Metric rail"))
        left.addWidget(MetricRail((("FPS", "238", ""), ("推理", "2.4", "ms"), ("P95", "4.8", "ms"), ("GPU", "62", "%"))))
        left.addWidget(SectionTitle("8.", "运行日志", "Runtime log"))
        log = LogConsole()
        for message in ("系统启动完成，视频输入已连接", "ONNX 模型加载成功", "TensorRT FP16 初始化完成", "CUDA 环境检查通过"):
            log.append_line(message)
        left.addWidget(log)
        lower.addLayout(left, 3)
        right = QVBoxLayout()
        right.addWidget(SectionTitle("7.", "预览叠加层", "Preview overlay"))
        right.addWidget(ScenePreview(), 1)
        lower.addLayout(right, 2)
        layout.addLayout(lower, 2)


class VariantB(QWidget):
    """Preview-first operational console with a strong black navigation mast."""

    def __init__(self):
        super().__init__()
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        mast = QFrame()
        mast.setFixedWidth(310)
        mast.setStyleSheet(f"background:{THEME.text}")
        nav = QVBoxLayout(mast)
        nav.setContentsMargins(30, 30, 30, 30)
        logo = LogoWidget(ASSET_DIR / "endfield-logo.jpg", invert=False)
        nav.addWidget(logo)
        title = _display("LIVE\nCONTROL", 62)
        title.setStyleSheet(f"color:{THEME.paper}")
        nav.addWidget(title)
        nav.addSpacing(20)
        for index, text in (("01", "总览"), ("02", "输入与模型"), ("03", "识别控制")):
            nav.addWidget(IndustrialButton(f"{index}  {text}", "primary" if index == "03" else "secondary"))
        nav.addStretch(1)
        nav.addWidget(_label("STABILITY // TECHNOLOGY\nOPERATION STATE 03", 9, muted=True))
        root.addWidget(mast)
        page = QVBoxLayout()
        page.setContentsMargins(32, 24, 32, 24)
        page.addWidget(_display("// TARGET ACQUISITION", 50))
        body = QHBoxLayout()
        preview = QVBoxLayout()
        preview.addWidget(ScenePreview(), 1)
        preview.addWidget(MetricRail((("FPS", "238", ""), ("LATENCY", "2.4", "ms"), ("TARGETS", "03", ""))))
        body.addLayout(preview, 3)
        side = QVBoxLayout()
        side.addWidget(_controls(), 1)
        side.addWidget(_states(), 1)
        side.addWidget(IndustrialButton("启动系统", "primary"))
        body.addLayout(side, 2)
        page.addLayout(body, 1)
        root.addLayout(page, 1)


class VariantC(QWidget):
    """Type-led calibration workspace with inputs as the primary affordance."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 22, 34, 28)
        layout.setSpacing(14)
        layout.addWidget(PosterHeader("MODEL / INPUT", "01", "输入与模型", "Acquisition configuration"))
        tabs = TabRail((("udp", "01  UDP JPEG"), ("h264", "02  UDP H.264"), ("obs", "03  OBS WEBSOCKET")), "udp")
        layout.addWidget(tabs)
        body = QHBoxLayout()
        body.setSpacing(24)
        left = QVBoxLayout()
        left.addWidget(_fields())
        actions = QHBoxLayout()
        actions.addWidget(IndustrialButton("加载模型", "primary"))
        actions.addWidget(IndustrialButton("测试连接", "secondary"))
        left.addLayout(actions)
        body.addLayout(left, 2)
        right = QVBoxLayout()
        right.addWidget(_display("238", 100))
        right.addWidget(_label("DISPLAY NUMERAL / 实时吞吐 FPS", 10, muted=True))
        right.addWidget(MetricRail((("INFERENCE", "2.4", "ms"), ("P95", "4.8", "ms"), ("GPU", "62", "%"))))
        right.addWidget(_controls(), 1)
        body.addLayout(right, 3)
        layout.addLayout(body, 1)


class PrototypeWindow(QMainWindow):
    NAMES = {"A": "REFERENCE SHEET", "B": "LIVE OPERATIONS", "C": "MODEL CALIBRATION"}

    def __init__(self, variant: str):
        super().__init__()
        self.resize(1600, 1000)
        self.stack = QStackedWidget()
        for page in (VariantA(), VariantB(), VariantC()):
            self.stack.addWidget(page)
        shell = QWidget()
        shell.setObjectName("appShell")
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.stack, 1)
        switcher = QFrame()
        switcher.setStyleSheet(f"background:{THEME.text};border-top:3px solid {THEME.acid}")
        row = QHBoxLayout(switcher)
        row.setContentsMargins(12, 5, 12, 5)
        previous = IndustrialButton("←", "secondary")
        next_button = IndustrialButton("→", "secondary")
        previous.setFixedWidth(70)
        next_button.setFixedWidth(70)
        self.variant_label = QLabel()
        self.variant_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.variant_label.setStyleSheet(f"color:{THEME.paper};font-weight:800;letter-spacing:2px")
        previous.clicked.connect(lambda: self.cycle(-1))
        next_button.clicked.connect(lambda: self.cycle(1))
        row.addStretch(1)
        row.addWidget(previous)
        row.addWidget(self.variant_label, 0)
        row.addWidget(next_button)
        row.addStretch(1)
        layout.addWidget(switcher)
        self.setCentralWidget(shell)
        self.set_variant(variant)

    def set_variant(self, variant: str) -> None:
        index = VARIANTS.index(variant)
        self.stack.setCurrentIndex(index)
        self.variant_label.setText(f"PROTOTYPE {variant}  /  {self.NAMES[variant]}")

    def cycle(self, step: int) -> None:
        self.set_variant(VARIANTS[(self.stack.currentIndex() + step) % len(VARIANTS)])

    def keyPressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.key() == Qt.Key.Key_Left:
            self.cycle(-1)
        elif event.key() == Qt.Key.Key_Right:
            self.cycle(1)
        else:
            super().keyPressEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Disposable Endfield layout prototype")
    parser.add_argument("--variant", choices=VARIANTS, default="A")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/ariblk.ttf")
    apply_theme(app)
    window = PrototypeWindow(args.variant)
    window.show()
    if args.screenshot:
        target = args.screenshot.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        def capture() -> None:
            window.grab().save(str(target), "PNG")
            app.quit()

        QTimer.singleShot(400, capture)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    main()
