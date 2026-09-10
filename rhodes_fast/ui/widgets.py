"""Reusable, low-overhead widgets for the Endfield control interface."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .theme import THEME


def _cut_path(rect: QRectF, cut: float = 10.0) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(rect.left(), rect.top())
    path.lineTo(rect.right() - cut, rect.top())
    path.lineTo(rect.right(), rect.top() + cut)
    path.lineTo(rect.right(), rect.bottom())
    path.lineTo(rect.left() + cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom() - cut)
    path.closeSubpath()
    return path


class CutSurface(QFrame):
    """Matte six-sided surface inspired by fabricated metal panels."""

    def __init__(self, parent: QWidget | None = None, *, accented: bool = False):
        super().__init__(parent)
        self.accented = accented
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = _cut_path(rect)
        painter.fillPath(path, QColor(THEME.surface))
        painter.setPen(QPen(QColor(THEME.border), 1))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(THEME.acid if self.accented else THEME.text), 3))
        painter.drawLine(QPointF(rect.left() + 1, rect.top() + 1), QPointF(rect.left() + 38, rect.top() + 1))


class IndustrialButton(QPushButton):
    """Static, cut-corner action button with no animation or effects."""

    VARIANTS = {"primary", "secondary", "ghost", "danger", "nav"}

    def __init__(self, text: str, variant: str = "secondary", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.variant = variant
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40 if variant != "nav" else 70)

    @property
    def variant(self) -> str:
        return self._variant

    @variant.setter
    def variant(self, value: str) -> None:
        if value not in self.VARIANTS:
            raise ValueError(f"unknown button variant: {value}")
        self._variant = value
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(max(108, hint.width() + 26), max(self.minimumHeight(), hint.height()))

    def _paint_chrome(self, painter: QPainter, rect: QRectF) -> tuple[QColor, bool]:
        path = _cut_path(rect, 8)
        hover = self.underMouse()
        active = self.isDown() or self.isChecked()

        background = QColor(THEME.surface_high)
        border = QColor(THEME.border)
        foreground = QColor(THEME.text)
        rail = QColor(THEME.acid)
        if self.variant == "primary":
            background = QColor(THEME.acid_hover if hover else THEME.acid)
            if self.isDown():
                background = QColor("#C7DB16")
            border = QColor(THEME.acid)
            foreground = QColor(THEME.background)
            rail = QColor(THEME.background)
        elif self.variant == "ghost":
            background = QColor("transparent")
            border = QColor(THEME.text if hover else THEME.border)
            foreground = QColor(THEME.text)
        elif self.variant == "secondary":
            background = QColor("#202220" if hover else THEME.text)
            border = QColor(THEME.text)
            foreground = QColor(THEME.paper)
        elif self.variant == "danger":
            background = QColor("#FBE9E7" if hover else "transparent")
            border = QColor(THEME.red)
            foreground = QColor(THEME.red)
            rail = QColor(THEME.red)
        elif self.variant == "nav":
            background = QColor(THEME.acid if active else THEME.surface_high if hover else "transparent")
            border = QColor(THEME.text if active else "transparent")
            foreground = QColor(THEME.text if active else THEME.text_muted)

        if not self.isEnabled():
            background = QColor(THEME.surface)
            border = QColor(THEME.border_soft)
            foreground = QColor("#5E655F")

        painter.fillPath(path, background)
        painter.setPen(QPen(border, 1))
        painter.drawPath(path)
        painter.setPen(QPen(rail, 3))
        painter.drawLine(QPointF(rect.left() + 1, rect.top() + 7), QPointF(rect.left() + 1, rect.bottom() - 7))
        return foreground, active

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        foreground, active = self._paint_chrome(painter, rect)

        font = self.font()
        font.setBold(self.variant == "primary" or active)
        painter.setFont(font)
        painter.setPen(foreground)
        alignment = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignLeft if self.variant == "nav" else Qt.AlignmentFlag.AlignHCenter
        )
        text_rect = rect.adjusted(15, 0, -25 if self.variant != "nav" else -10, 0)
        painter.drawText(text_rect, alignment, self.text().replace("&&", "&"))

        if self.variant in {"primary", "secondary"}:
            x = rect.right() - 14
            y = rect.center().y()
            painter.setPen(QPen(foreground, 1.5))
            painter.drawLine(QPointF(x - 4, y - 4), QPointF(x, y))
            painter.drawLine(QPointF(x, y), QPointF(x - 4, y + 4))


class SectionPanel(CutSurface):
    """Titled content panel used as the primary layout primitive."""

    def __init__(self, title: str, index: str = "", subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent, accented=True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        if index:
            number = QLabel(index)
            number.setStyleSheet(f"color:{THEME.acid};font-weight:800;font-size:15px")
            header.addWidget(number)
        heading = QLabel(title)
        heading.setProperty("role", "title")
        header.addWidget(heading)
        if subtitle:
            hint = QLabel(subtitle.upper())
            hint.setProperty("role", "eyebrow")
            header.addWidget(hint)
        header.addStretch(1)
        outer.addLayout(header)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{THEME.border_soft};border:0")
        outer.addWidget(divider)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:  # noqa: N802 - mirrors Qt
        self.body.addWidget(widget, stretch)

    def addLayout(self, layout, stretch: int = 0) -> None:  # noqa: ANN001, N802
        self.body.addLayout(layout, stretch)


class StatusBadge(QFrame):
    TONES = {
        "online": (THEME.text, THEME.acid),
        "warning": (THEME.text, THEME.surface_high),
        "error": (THEME.red, "transparent"),
        "neutral": (THEME.text_muted, THEME.surface_high),
    }

    def __init__(self, text: str, tone: str = "online", parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(7)
        self.dot = QFrame()
        self.dot.setFixedSize(9, 9)
        self.label = QLabel(text)
        self.label.setStyleSheet("font-size:11px;font-weight:700;letter-spacing:1px")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self.set_status(text, tone)

    def set_status(self, text: str, tone: str = "online") -> None:
        foreground, background = self.TONES.get(tone, self.TONES["neutral"])
        self.label.setText(text)
        self.dot.setStyleSheet(f"background:{foreground};border:0")
        self.setStyleSheet(
            f"StatusBadge{{background:{background};border-left:1px solid {foreground};border-right:1px solid {foreground};border-radius:0}}"
        )


class Sparkline(QWidget):
    """Tiny paint-only chart; updates only while visible."""

    def __init__(self, values: Iterable[float] = (), parent: QWidget | None = None):
        super().__init__(parent)
        self._values: deque[float] = deque(values, maxlen=32)
        self._last_repaint = 0.0
        self._repaint_pending = False
        self.setMinimumSize(70, 28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values: Iterable[float]) -> None:
        self._values = deque(values, maxlen=32)
        self._request_repaint()

    def push(self, value: float) -> None:
        self._values.append(float(value))
        self._request_repaint()

    def _request_repaint(self) -> None:
        now = time.monotonic()
        if self.isVisible() and now - self._last_repaint >= 0.1:
            self._last_repaint = now
            self.update()
        elif self.isVisible() and not self._repaint_pending:
            remaining = max(1, round((0.1 - (now - self._last_repaint)) * 1000))
            self._repaint_pending = True
            QTimer.singleShot(remaining, self._flush_pending_repaint)

    def _flush_pending_repaint(self) -> None:
        self._repaint_pending = False
        if self.isVisible():
            self._last_repaint = time.monotonic()
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        super().paintEvent(event)
        if len(self._values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        values = list(self._values)
        low, high = min(values), max(values)
        span = max(high - low, 1e-9)
        width, height = self.width() - 2, self.height() - 2
        path = QPainterPath()
        for index, value in enumerate(values):
            x = 1 + width * index / (len(values) - 1)
            y = 1 + height * (1 - (value - low) / span)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(QColor(THEME.text), 1.5))
        painter.drawPath(path)


class MetricCard(CutSurface):
    def __init__(self, label: str, value: str, unit: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(92)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        text = QVBoxLayout()
        caption = QLabel(label.upper())
        caption.setProperty("muted", True)
        row = QHBoxLayout()
        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "display")
        self.unit_label = QLabel(unit)
        self.unit_label.setProperty("muted", True)
        row.addWidget(self.value_label)
        row.addWidget(self.unit_label, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        text.addWidget(caption)
        text.addLayout(row)
        layout.addLayout(text, 1)
        self.sparkline = Sparkline([2, 4, 3, 7, 5, 8, 7, 9, 8, 10])
        layout.addWidget(self.sparkline)

    def set_value(self, value: str, unit: str | None = None) -> None:
        self.value_label.setText(value)
        if unit is not None:
            self.unit_label.setText(unit)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    label: str
    value: str
    unit: str = ""


class MetricRail(CutSurface):
    """A shared measurement rail that avoids a grid of generic dashboard cards."""

    def __init__(
        self,
        metrics: Iterable[MetricSpec | tuple[str, str, str]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setMinimumHeight(100)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.value_labels: dict[str, QLabel] = {}
        data = tuple(item if isinstance(item, MetricSpec) else MetricSpec(*item) for item in metrics)
        if not data:
            raise ValueError("metric rail requires at least one metric")
        labels = [item.label for item in data]
        if len(labels) != len(set(labels)):
            raise ValueError("metric labels must be unique")
        for index, metric in enumerate(data):
            if index:
                separator = QFrame()
                separator.setFixedWidth(1)
                separator.setStyleSheet(f"background:{THEME.border_soft};border:0")
                layout.addWidget(separator)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(18, 14, 18, 12)
            cell_layout.setSpacing(4)
            rail = QFrame()
            rail.setFixedSize(34, 3)
            rail.setStyleSheet(f"background:{THEME.acid};border:0")
            caption = QLabel(metric.label.upper())
            caption.setProperty("role", "eyebrow")
            value_row = QHBoxLayout()
            value_row.setSpacing(6)
            value_label = QLabel(metric.value)
            value_label.setStyleSheet("font-size:32px;font-weight:800")
            unit_label = QLabel(metric.unit)
            unit_label.setProperty("muted", True)
            value_row.addWidget(value_label)
            value_row.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignBottom)
            value_row.addStretch(1)
            cell_layout.addWidget(rail)
            cell_layout.addWidget(caption)
            cell_layout.addLayout(value_row)
            layout.addWidget(cell, 1)
            self.value_labels[metric.label] = value_label

    def set_value(self, label: str, value: str) -> None:
        if label not in self.value_labels:
            raise KeyError(f"unknown metric: {label}")
        self.value_labels[label].setText(value)


class IndustrialField(QWidget):
    """Labeled input with explicit operational states and no hidden animation."""

    STATES = {"default", "focused", "read_only", "error"}

    def __init__(
        self,
        label: str,
        value: str = "",
        *,
        state: str = "default",
        suffix: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        header = QHBoxLayout()
        caption = QLabel(label.upper())
        caption.setProperty("role", "eyebrow")
        self.state_label = QLabel()
        self.state_label.setProperty("role", "eyebrow")
        header.addWidget(caption)
        header.addStretch(1)
        header.addWidget(self.state_label)
        field_row = QHBoxLayout()
        field_row.setSpacing(0)
        self._base_state = "default"
        self.line_edit = QLineEdit(value)
        self.line_edit.setMinimumHeight(40)
        self.line_edit.installEventFilter(self)
        field_row.addWidget(self.line_edit, 1)
        self.suffix_label = QLabel(suffix)
        self.suffix_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.suffix_label.setMinimumWidth(46)
        self.suffix_label.setVisible(bool(suffix))
        field_row.addWidget(self.suffix_label)
        layout.addLayout(header)
        layout.addLayout(field_row)
        self.set_state(state)

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001, N802
        if watched is self.line_edit and self._base_state not in {"error", "read_only"}:
            if event.type() == QEvent.Type.FocusIn:
                self._apply_state("focused")
            elif event.type() == QEvent.Type.FocusOut:
                self._apply_state(self._base_state)
        return super().eventFilter(watched, event)

    def text(self) -> str:
        return self.line_edit.text()

    def setText(self, value: str) -> None:  # noqa: N802
        self.line_edit.setText(value)

    def set_state(self, state: str) -> None:
        if state not in self.STATES:
            raise ValueError(f"unknown field state: {state}")
        self._base_state = "default" if state == "focused" else state
        self._apply_state(state)

    def _apply_state(self, state: str) -> None:
        self.state = state
        self.line_edit.setReadOnly(state == "read_only")
        border = THEME.border
        background = "#FAFAF6"
        foreground = THEME.text
        state_text = "READY"
        if state == "focused":
            border = THEME.acid
            state_text = "ACTIVE"
        elif state == "read_only":
            background = THEME.surface_high
            foreground = THEME.text_muted
            state_text = "LOCKED"
        elif state == "error":
            border = THEME.red
            state_text = "INVALID"
        self.line_edit.setStyleSheet(
            f"background:{background};color:{foreground};border:1px solid {border};"
            "border-right:0;padding:0 10px;"
        )
        self.suffix_label.setStyleSheet(
            f"background:{THEME.text};color:{THEME.paper};border:1px solid {THEME.text};font-weight:700;"
        )
        self.state_label.setText(state_text)
        self.state_label.setStyleSheet(f"color:{THEME.red if state == 'error' else THEME.text_muted}")


class SegmentedControl(QWidget):
    """Compact mutually-exclusive selector using the system's ink/acid states."""

    currentChanged = Signal(str)

    def __init__(
        self,
        options: Iterable[tuple[str, str]],
        current: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        data = tuple(options)
        if not data:
            raise ValueError("segmented control requires at least one option")
        keys = [key for key, _label in data]
        if len(keys) != len(set(keys)):
            raise ValueError("segment keys must be unique")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, IndustrialButton] = {}
        self._current_key = ""
        for key, label in data:
            button = IndustrialButton(label, "ghost")
            button.setCheckable(True)
            button.setMinimumHeight(38)
            button.clicked.connect(lambda _checked=False, value=key: self.set_current(value))
            self.group.addButton(button)
            self.buttons[key] = button
            layout.addWidget(button, 1)
        self.set_current(current or data[0][0], emit=False)

    @property
    def current(self) -> str:
        return self._current_key

    def set_current(self, key: str, *, emit: bool = True) -> None:
        if key not in self.buttons:
            raise KeyError(f"unknown segment: {key}")
        changed = self._current_key != key
        for item_key, button in self.buttons.items():
            selected = item_key == key
            button.variant = "primary" if selected else "ghost"
            button.update()
        self.buttons[key].setChecked(True)
        self._current_key = key
        if emit and changed:
            self.currentChanged.emit(key)


class TabRail(SegmentedControl):
    """Section-level tabs with a quieter ink selection than parameter segments."""

    def set_current(self, key: str, *, emit: bool = True) -> None:
        super().set_current(key, emit=emit)
        self.buttons[key].variant = "secondary"
        self.buttons[key].update()


class IndustrialSelect(QWidget):
    """Labeled select box for model precision, providers and other finite choices."""

    currentChanged = Signal(str)

    def __init__(
        self,
        label: str,
        options: Iterable[str],
        current: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        values = tuple(options)
        if not values:
            raise ValueError("industrial select requires at least one option")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        caption = QLabel(label.upper())
        caption.setProperty("role", "eyebrow")
        self.combo = QComboBox()
        self.combo.addItems(values)
        self.combo.setMinimumHeight(40)
        self.combo.setStyleSheet(
            f"background:#FAFAF6;color:{THEME.text};border:1px solid {THEME.text};"
            f"selection-background-color:{THEME.acid};selection-color:{THEME.text};font-weight:700;"
        )
        if current is not None:
            if current not in values:
                raise ValueError(f"unknown select option: {current}")
            self.combo.setCurrentText(current)
        self.combo.currentTextChanged.connect(self.currentChanged.emit)
        layout.addWidget(caption)
        layout.addWidget(self.combo)

    @property
    def current(self) -> str:
        return self.combo.currentText()


class IndustrialIconButton(IndustrialButton):
    """Square tool action with an explicit accessible name."""

    def __init__(
        self,
        glyph: str,
        accessible_name: str,
        variant: str = "ghost",
        parent: QWidget | None = None,
    ):
        self.glyph = glyph
        super().__init__("", variant, parent)
        self.setAccessibleName(accessible_name)
        self.setFixedSize(42, 42)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(42, 42)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        foreground, _active = self._paint_chrome(painter, rect)
        font = self.font()
        font.setBold(True)
        font.setPointSize(13)
        painter.setFont(font)
        painter.setPen(QColor(foreground))
        center = rect.center()
        if self.glyph == "export":
            painter.drawRect(QRectF(center.x() - 10, center.y() - 3, 18, 14))
            painter.drawLine(QPointF(center.x() - 2, center.y() + 2), QPointF(center.x() + 9, center.y() - 9))
            painter.drawLine(QPointF(center.x() + 3, center.y() - 9), QPointF(center.x() + 9, center.y() - 9))
            painter.drawLine(QPointF(center.x() + 9, center.y() - 9), QPointF(center.x() + 9, center.y() - 3))
        elif self.glyph == "refresh":
            arc = QRectF(center.x() - 10, center.y() - 10, 20, 20)
            painter.drawArc(arc, 35 * 16, 285 * 16)
            painter.drawLine(QPointF(center.x() + 9, center.y() - 8), QPointF(center.x() + 3, center.y() - 9))
            painter.drawLine(QPointF(center.x() + 9, center.y() - 8), QPointF(center.x() + 8, center.y() - 2))
        elif self.glyph == "more":
            for offset in (-7, 0, 7):
                painter.fillRect(QRectF(center.x() + offset - 1.5, center.y() - 1.5, 3, 3), QColor(foreground))
        else:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.glyph)


class TechSwitch(QCheckBox):
    """Rectangular industrial ON/OFF plate with no animation overhead."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(34)

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(max(230, hint.width() + 138), 34)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        control_width = min(126, self.width() * 0.48)
        track = QRectF(self.width() - control_width, (self.height() - 30) / 2, control_width, 30)
        active = self.isChecked()
        half = track.width() / 2
        painter.fillRect(QRectF(track.left(), track.top(), half, track.height()), QColor(THEME.acid if active else THEME.paper))
        painter.fillRect(
            QRectF(track.left() + half, track.top(), half, track.height()),
            QColor(THEME.paper if active else THEME.surface_high),
        )
        painter.setPen(QPen(QColor(THEME.border), 1))
        painter.drawRect(track)
        painter.drawLine(QPointF(track.center().x(), track.top()), QPointF(track.center().x(), track.bottom()))
        if active:
            painter.setPen(QPen(QColor(THEME.text), 3))
            painter.drawLine(QPointF(track.center().x() - 2, track.top() + 4), QPointF(track.center().x() - 2, track.bottom() - 4))
        painter.setPen(QColor(THEME.text))
        painter.drawText(QRectF(0, 0, track.left() - 10, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        bold = self.font()
        bold.setBold(True)
        painter.setFont(bold)
        painter.drawText(QRectF(track.left(), track.top(), half, track.height()), Qt.AlignmentFlag.AlignCenter, "ON")
        painter.drawText(QRectF(track.left() + half, track.top(), half, track.height()), Qt.AlignmentFlag.AlignCenter, "OFF")
        if self.hasFocus():
            painter.setPen(QPen(QColor(THEME.acid), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class TechSlider(QWidget):
    valueChanged = Signal(float)

    def __init__(
        self,
        label: str,
        minimum: float,
        maximum: float,
        value: float,
        *,
        decimals: int = 3,
        suffix: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.steps = 1000
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        caption = QLabel(label)
        caption.setMinimumWidth(100)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.steps)
        self.slider.setTickPosition(QSlider.TickPosition.TicksAbove)
        self.slider.setTickInterval(250)
        self.slider.setMinimumHeight(34)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(self.minimum, self.maximum)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(suffix)
        self.spin.setFixedWidth(88)
        self.spin.setStyleSheet(
            f"background:{THEME.text};color:{THEME.paper};border:1px solid {THEME.text};font-weight:700;"
        )
        layout.addWidget(caption)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.setValue(value)

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, value: float) -> None:  # noqa: N802
        value = min(max(float(value), self.minimum), self.maximum)
        slider_value = round((value - self.minimum) / (self.maximum - self.minimum) * self.steps)
        self.slider.blockSignals(True)
        self.spin.blockSignals(True)
        self.slider.setValue(slider_value)
        self.spin.setValue(value)
        self.slider.blockSignals(False)
        self.spin.blockSignals(False)

    def _from_slider(self, raw: int) -> None:
        value = self.minimum + (self.maximum - self.minimum) * raw / self.steps
        self.spin.blockSignals(True)
        self.spin.setValue(value)
        self.spin.blockSignals(False)
        self.valueChanged.emit(self.spin.value())

    def _from_spin(self, value: float) -> None:
        raw = round((value - self.minimum) / (self.maximum - self.minimum) * self.steps)
        self.slider.blockSignals(True)
        self.slider.setValue(raw)
        self.slider.blockSignals(False)
        self.valueChanged.emit(float(value))


class DeviceStatusRow(QFrame):
    def __init__(self, name: str, status: str, detail: str = "", tone: str = "online", parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"border-bottom:1px solid {THEME.border_soft};")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("font-weight:700")
        self.status_badge = StatusBadge(status, tone)
        detail_label = QLabel(detail)
        detail_label.setProperty("muted", True)
        layout.addWidget(self.name_label)
        layout.addStretch(1)
        layout.addWidget(self.status_badge)
        layout.addWidget(detail_label)

    def set_status(self, status: str, tone: str = "online") -> None:
        self.status_badge.set_status(status, tone)


class LogConsole(QPlainTextEdit):
    def __init__(self, max_lines: int = 250, parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(max_lines)
        self.setMinimumHeight(120)

    def append_line(self, message: str, level: str = "INFO") -> None:
        self.appendPlainText(f"[{level.upper():>5}]  {message}")


@dataclass(frozen=True, slots=True)
class DetectionBox:
    x: float
    y: float
    width: float
    height: float
    label: str
    confidence: float
    selected: bool = False


class PreviewCanvas(QWidget):
    """Frame display and overlay painter with explicit activation control."""

    def __init__(self, parent: QWidget | None = None, *, max_fps: float = 30.0):
        super().__init__(parent)
        self._frame: QImage | None = None
        self._detections: tuple[DetectionBox, ...] = ()
        self._active = False
        self._fov_ratio = 0.32
        self._last_repaint = 0.0
        self._frame_interval = 0.0
        self._repaint_pending = False
        self.set_max_fps(max_fps)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            self.update()

    @property
    def max_fps(self) -> float:
        return 0.0 if self._frame_interval == 0 else 1.0 / self._frame_interval

    def set_max_fps(self, max_fps: float) -> None:
        max_fps = float(max_fps)
        if max_fps < 0:
            raise ValueError("max_fps must be zero or greater")
        self._frame_interval = 0.0 if max_fps == 0 else 1.0 / max_fps

    def set_frame(self, frame: QImage, *, copy: bool = False) -> bool:
        if not self._active:
            return False
        self._frame = frame.copy() if copy else frame
        now = time.monotonic()
        if self.isVisible() and (self._frame_interval == 0 or now - self._last_repaint >= self._frame_interval):
            self._last_repaint = now
            self.update()
        elif self.isVisible() and not self._repaint_pending:
            remaining = max(1, round((self._frame_interval - (now - self._last_repaint)) * 1000))
            self._repaint_pending = True
            QTimer.singleShot(remaining, self._flush_pending_repaint)
        return True

    def _flush_pending_repaint(self) -> None:
        self._repaint_pending = False
        if self._active and self.isVisible():
            self._last_repaint = time.monotonic()
            self.update()

    def set_detections(self, detections: Iterable[DetectionBox]) -> None:
        self._detections = tuple(detections)

    def set_fov_ratio(self, value: float) -> None:
        self._fov_ratio = min(max(float(value), 0.05), 0.49)
        if self._active and self.isVisible():
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0A0E10"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        target = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._frame is not None and not self._frame.isNull():
            source = QRectF(self._frame.rect())
            scale = min(target.width() / source.width(), target.height() / source.height())
            size = QSize(round(source.width() * scale), round(source.height() * scale))
            draw = QRectF(0, 0, size.width(), size.height())
            draw.moveCenter(target.center())
            painter.drawImage(draw, self._frame, source)
            viewport = draw
        else:
            viewport = target
            painter.setPen(QPen(QColor(THEME.border_soft), 1))
            step = 48
            for x in range(0, self.width(), step):
                painter.drawLine(x, 0, x, self.height())
            for y in range(0, self.height(), step):
                painter.drawLine(0, y, self.width(), y)
            painter.setPen(QColor(THEME.text_muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "LIVE PREVIEW / WAITING FOR FRAME")

        center = viewport.center()
        radius = min(viewport.width(), viewport.height()) * self._fov_ratio
        painter.setPen(QPen(QColor("#829097"), 1, Qt.PenStyle.DashLine))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(QPen(QColor(THEME.acid), 1))
        painter.drawLine(QPointF(center.x() - 14, center.y()), QPointF(center.x() + 14, center.y()))
        painter.drawLine(QPointF(center.x(), center.y() - 14), QPointF(center.x(), center.y() + 14))

        for detection in self._detections:
            box = QRectF(
                viewport.left() + detection.x * viewport.width(),
                viewport.top() + detection.y * viewport.height(),
                detection.width * viewport.width(),
                detection.height * viewport.height(),
            )
            color = QColor(THEME.acid if detection.selected else THEME.paper)
            painter.setPen(QPen(color, 2 if detection.selected else 1))
            painter.drawRect(box)
            painter.fillRect(QRectF(box.left(), max(viewport.top(), box.top() - 20), min(box.width(), 120), 20), color)
            painter.setPen(QColor(THEME.background))
            painter.drawText(
                QRectF(box.left() + 4, max(viewport.top(), box.top() - 20), min(box.width() - 4, 116), 20),
                Qt.AlignmentFlag.AlignVCenter,
                f"{detection.label} {detection.confidence:.2f}",
            )


class GainCurve(QWidget):
    """Paint-only dynamic P response chart; recalculates only on value changes."""

    def __init__(self, kp_min: float = 0.1, kp_max: float = 0.164, growth: float = 0.167, parent=None):
        super().__init__(parent)
        self.kp_min = kp_min
        self.kp_max = kp_max
        self.growth = growth
        self.setMinimumHeight(150)

    def set_parameters(self, kp_min: float, kp_max: float, growth: float) -> None:
        values = (float(kp_min), float(kp_max), float(growth))
        if values == (self.kp_min, self.kp_max, self.growth):
            return
        self.kp_min, self.kp_max, self.growth = values
        if self.isVisible():
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0B1012"))
        chart = QRectF(self.rect()).adjusted(44, 14, -18, -28)
        painter.setPen(QPen(QColor(THEME.border_soft), 1))
        for step in range(6):
            y = chart.top() + chart.height() * step / 5
            painter.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
        path = QPainterPath()
        for step in range(101):
            distance = 320 * step / 100
            kp = self.kp_min + (self.kp_max - self.kp_min) * (1 - math.exp(-self.growth * distance / 10))
            normalized = (kp - self.kp_min) / max(self.kp_max - self.kp_min, 1e-9)
            point = QPointF(chart.left() + chart.width() * step / 100, chart.bottom() - chart.height() * normalized)
            if step == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(THEME.acid), 2))
        painter.drawPath(path)
        painter.setPen(QColor(THEME.text_muted))
        painter.drawText(QRectF(4, chart.top(), 36, 18), Qt.AlignmentFlag.AlignRight, "Kp")
        painter.drawText(QRectF(chart.left(), chart.bottom() + 5, chart.width(), 20), Qt.AlignmentFlag.AlignRight, "目标距离 (px)")


class LogoWidget(QLabel):
    def __init__(
        self,
        logo_path: str | Path | None = None,
        parent: QWidget | None = None,
        *,
        invert: bool = True,
    ):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(100)
        source = QImage(str(logo_path)) if logo_path else QImage()
        if invert and not source.isNull():
            alpha = source.convertToFormat(QImage.Format.Format_Grayscale8)
            pixels = alpha.bits()
            for index in range(alpha.sizeInBytes()):
                luminance = pixels[index]
                pixels[index] = 0 if luminance < 96 else min(255, (luminance - 96) * 2)
            ink = QImage(source.size(), QImage.Format.Format_ARGB32_Premultiplied)
            ink.fill(QColor(THEME.text))
            ink.setAlphaChannel(alpha)
            source = ink
        self._source = QPixmap.fromImage(source) if not source.isNull() else QPixmap()
        self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if not self._source.isNull():
            self.setPixmap(
                self._source.scaled(
                    max(1, self.width() - 24),
                    max(1, self.height() - 8),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.setText("ENDFIELD\nINDUSTRIES")


class Sidebar(QFrame):
    pageSelected = Signal(str)

    DEFAULT_ITEMS = (
        ("overview", "01", "总览", "OVERVIEW"),
        ("input", "02", "输入与模型", "INPUT & MODEL"),
        ("control", "03", "识别控制", "DETECTION"),
        ("preview", "04", "实时预览", "LIVE PREVIEW"),
        ("diagnostics", "05", "诊断", "DIAGNOSTICS"),
    )

    def __init__(
        self,
        logo_path: str | Path | None = None,
        parent: QWidget | None = None,
        *,
        items: Iterable[tuple[str, str, str, str]] | None = None,
    ):
        super().__init__(parent)
        self.setProperty("role", "sidebar")
        self.setFixedWidth(190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 16)
        layout.setSpacing(4)
        layout.addWidget(LogoWidget(logo_path))
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, IndustrialButton] = {}
        navigation_items = tuple(items) if items is not None else self.DEFAULT_ITEMS
        if not navigation_items:
            raise ValueError("sidebar requires at least one navigation item")
        for page_id, index, title, subtitle in navigation_items:
            button = IndustrialButton(f"{index}    {title}\n       {subtitle.replace('&', '&&')}", "nav")
            button.setCheckable(True)
            button.setFixedHeight(72)
            button.clicked.connect(lambda _checked=False, key=page_id: self.pageSelected.emit(key))
            self.group.addButton(button)
            self.buttons[page_id] = button
            layout.addWidget(button)
        layout.addStretch(1)
        version = QLabel("STABILITY // TECHNOLOGY\n\nv0.3.1")
        version.setProperty("role", "eyebrow")
        version.setContentsMargins(18, 0, 12, 0)
        layout.addWidget(version)
        self.set_current(navigation_items[0][0])

    def set_current(self, page_id: str) -> None:
        if page_id not in self.buttons:
            raise KeyError(f"unknown page: {page_id}")
        self.buttons[page_id].setChecked(True)


class TopBar(QFrame):
    startRequested = Signal()
    stopRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("role", "topbar")
        self.setFixedHeight(76)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 10)
        title = QLabel("ENDFIELD // CONTROL STATION")
        title.setStyleSheet("font-size:22px;font-weight:700")
        subtitle = QLabel("LOCAL VISION SYSTEM")
        subtitle.setProperty("role", "eyebrow")
        self.status = StatusBadge("SYSTEM ONLINE", "online")
        self.start_button = IndustrialButton("启动系统", "primary")
        self.start_button.setMinimumWidth(148)
        self.stop_button = IndustrialButton("停止", "danger")
        self.start_button.clicked.connect(self.startRequested)
        self.stop_button.clicked.connect(self.stopRequested)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(self.status)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
