"""跟 GUI 工具包无关的状态与业务逻辑, 供 tkinter 与 WebView 两套界面共用。"""

from .prompts import Prompter, RecordingPrompter
from .session import GuiSession
from .state import (
    FormState,
    Labels,
    ProfileFormState,
    apply_preset_to_state,
    config_to_form_state,
    form_state_to_config,
    trail_settings_from_state,
)

__all__ = [
    "FormState",
    "GuiSession",
    "Labels",
    "ProfileFormState",
    "Prompter",
    "RecordingPrompter",
    "apply_preset_to_state",
    "config_to_form_state",
    "form_state_to_config",
    "trail_settings_from_state",
]
