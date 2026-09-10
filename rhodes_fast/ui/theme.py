"""Endfield design tokens and the application-wide Qt stylesheet."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True, slots=True)
class Theme:
    background: str = "#F1F1EB"
    surface: str = "#F7F7F2"
    surface_high: str = "#E1E3DC"
    border: str = "#BFC2BB"
    border_soft: str = "#D5D7D0"
    text: str = "#101110"
    text_muted: str = "#6E746E"
    paper: str = "#F1F1EB"
    acid: str = "#E6FF18"
    acid_hover: str = "#F0FF68"
    red: str = "#E15D55"
    radius: int = 0
    spacing: int = 12


THEME = Theme()


def build_stylesheet(theme: Theme = THEME) -> str:
    """Return the complete stylesheet so apps can extend it if necessary."""

    return f"""
    * {{
        color: {theme.text};
    }}
    QMainWindow, QWidget#appShell {{ background: {theme.background}; }}
    QFrame[panel="true"] {{
        background: {theme.surface};
        border: 1px solid {theme.border};
        border-radius: {theme.radius}px;
    }}
    QFrame[role="sidebar"] {{
        background: {theme.paper};
        border-right: 1px solid {theme.border_soft};
    }}
    QFrame[role="topbar"] {{
        background: {theme.background};
        border-bottom: 1px solid {theme.border_soft};
    }}
    QLabel[role="eyebrow"] {{ color: {theme.text_muted}; font-size: 10px; letter-spacing: 1px; }}
    QLabel[role="title"] {{ font-size: 18px; font-weight: 700; }}
    QLabel[role="display"] {{ font-size: 30px; font-weight: 700; }}
    QLabel[muted="true"] {{ color: {theme.text_muted}; }}
    QPushButton {{ background: {theme.surface_high}; border: 1px solid {theme.border}; min-height: 36px; padding: 0 14px; }}
    QPushButton:hover {{ border-color: {theme.text}; background: #E5E6DF; }}
    QPushButton:pressed {{ background: #D7D9D1; }}
    QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
        background: #FAFAF6;
        border: 1px solid {theme.border};
        border-radius: {theme.radius}px;
        min-height: 34px;
        padding: 0 10px;
        selection-background-color: {theme.acid};
        selection-color: #111416;
    }}
    QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {theme.acid}; }}
    QComboBox::drop-down {{ width: 28px; border: 0; }}
    QComboBox QAbstractItemView {{ background: #FAFAF6; border: 1px solid {theme.border}; selection-background-color: {theme.acid}; selection-color: {theme.text}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {theme.text}; }}
    QSlider::sub-page:horizontal {{ background: {theme.acid}; }}
    QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; background: {theme.paper}; border: 1px solid #0B0D0C; }}
    QPlainTextEdit {{
        background: {theme.text};
        border: 1px solid {theme.text};
        color: {theme.paper};
        font-family: "Microsoft YaHei UI";
        font-size: 11px;
        padding: 8px;
        selection-background-color: #46500B;
    }}
    QScrollBar:vertical {{ background: {theme.surface_high}; width: 8px; }}
    QScrollBar::handle:vertical {{ background: {theme.border}; min-height: 28px; border-radius: 4px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """


def apply_theme(app: QApplication, theme: Theme = THEME) -> None:
    """Apply the paper/ink palette and stylesheet once at application startup."""

    app.setStyle("Fusion")
    font_family = "Microsoft YaHei UI"
    if font_family not in QFontDatabase.families():
        for font_path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhl.ttc")):
            if not font_path.is_file():
                continue
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                font_family = families[0]
                break
    app.setFont(QFont(font_family, 10))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.background))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Base, QColor("#FAFAF6"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.surface_high))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.surface_high))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.acid))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.background))
    app.setPalette(palette)
    app.setStyleSheet(build_stylesheet(theme).replace('"Microsoft YaHei UI"', f'"{font_family}"'))
