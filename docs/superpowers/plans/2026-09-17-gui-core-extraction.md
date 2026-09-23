# gui_core 抽取 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `rhodes_fast/gui.py` 里跟 GUI 工具包无关的状态转换与业务动作抽成 `rhodes_fast/gui_core/` 包，让旧 tkinter 界面和未来的 WebView 界面共用同一份逻辑。

**Architecture:** 新增三个模块——`state.py` 放表单状态与 `AppConfig`/`Preset` 的纯函数互转，`session.py` 放子进程启停、日志收集、预设与算法库操作，`prompts.py` 放「问用户」的回调协议（让业务逻辑不再直接调 `messagebox`）。旧 `gui.py` 保留全部控件代码，改成这三个模块的适配层：tk 变量 ↔ `FormState`，`messagebox` ↔ `Prompter`。

**Tech Stack:** Python 3.11+，标准库 `unittest`，现有 `dataclasses` 风格（项目里 `AppConfig` / `Preset` / `TrailSettings` 都是 dataclass）。

## Global Constraints

- **旧界面行为零可见变化。** 这是本计划唯一的硬验收标准。任何让用户察觉到的差异都算失败。
- **全量测试必须保持 544 个原有用例全绿**：`.venv/Scripts/python.exe -m unittest discover -s tests`
  计划里每一步写的累计数字（570 / 574 / 578 / 586）是按本计划给出的用例数推的，**仅供对照**。真正的判据是两条：`OK`，且总数不低于 544 + 你实际新增的数量。多写几个用例导致数字对不上不是问题；少了或者出现 `FAILED` 才是。
- **不碰推理子进程**：`rhodes_fast/pipeline.py`、`preview.py`、`kmbox_control.py`、`detector.py` 一行不改。
- **不碰 WebView**：本计划不新增 `gui_web/`，不加 `pywebview` 依赖。那是第二份计划的事。
- **`gui_core/` 不许 import tkinter。** 这是整个计划的意义所在；每个模块顶部加测试断言它导入时不需要 tk。
- **数值字段保持字符串。** `udp_port` 等在 tk 里是 `StringVar`，`_read_form` 用 `int()` 解析、解析失败由 `_save` 的 `except` 兜成一个错误弹窗。`FormState` 必须同样存字符串，否则「用户输入非法值」的行为会变。
- **提交信息结尾**：`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **不提交 `MODEL/`**，不动工作树里已被用户删除的 `docs/projectile-prediction-research.zh-CN.md`。

> **关于规格里的「阶段 0」**：规格的阶段 0 是「设计系统拉进 `gui_web/web/design/`」。本计划**不做**它——`gui_web/` 在本计划里根本不存在，一堆 CSS 孤零零躺在仓库里没有任何东西能验证它。它归第二份计划的第一个任务。本计划 = 规格的阶段 1。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `rhodes_fast/gui_core/__init__.py` | 导出 `FormState`、`ProfileFormState`、`Prompter`、`GuiSession` |
| `rhodes_fast/gui_core/prompts.py` | `Prompter` 协议 + `RecordingPrompter` 测试替身 |
| `rhodes_fast/gui_core/state.py` | `FormState` / `ProfileFormState` + 四个纯函数互转 |
| `rhodes_fast/gui_core/session.py` | `GuiSession`：子进程启停、日志、预设 CRUD、算法库 CRUD |
| `rhodes_fast/gui.py` | 保留全部控件代码；改为上述模块的适配层 |
| `tests/test_gui_core_prompts.py` | Task 1 |
| `tests/test_gui_core_state.py` | Task 2–4 |
| `tests/test_gui_core_session.py` | Task 6–8 |

**为什么 `state.py` 和 `session.py` 分开**：前者是纯函数、无 I/O、无副作用，测起来只要构造 dataclass；后者碰文件系统和子进程，测起来要 mock。混在一个文件里会让纯函数那部分的测试也被迫背上 mock 的包袱。

---

## 背景：读代码时必须先知道的三件事

**一、tk 变量里存的是「显示值」不是「存储值」。**

`gui.py` 顶部有 `PROVIDERS`、`OUTPUT_FORMATS`、`INPUT_MODES`、`LOG_LANGUAGES`、`TRIGGERS` 五个 `dict[str, str]`，键是界面上显示的中文标签，值是配置文件里存的英文标识。`_display_value(mapping, stored)` 做反查（存储值 → 显示标签），`mapping[label]` 做正查（显示标签 → 存储值）。

```python
# gui.py:73
def _display_value(mapping: dict[str, str], stored: str) -> str:
```

`FormState` 存的是**显示标签**，因为它要直接喂给控件。转换发生在 `form_state_to_config()` 里。

**二、`_fill_form` 不是纯函数。**

`gui.py:1129-1186` 的 `_fill_form(preset)` 中间夹着五处控件副作用：

| 行 | 调用 | 干什么 |
|---|---|---|
| 1134 | `self._sync_cuda_graph_control()` | 按 provider 启用/禁用 CUDA Graph 复选框 |
| 1145 | `self._switch_input_panel()` | 按输入模式切换 UDP / OBS 面板可见性 |
| **1167** | **`self._apply_tuning_to_form(index, Tuning(...))`** | **不只是重建控件——它还设七个字段并热推给管线，见下** |
| **1180** | **`self._last_profile_triggers = [...]`** | **刷新触发键快照** |
| 1183 | `self._inspect_selected_model(model.path)` | 起后台线程读模型契约 |
| 1185 | `self._update_target_class_choices()` | 重填目标标签下拉框 |

**`_apply_tuning_to_form` 不是一个「纯副作用」，这是本计划最容易出事的地方。** 它（gui.py:1022-1043）承担了方案的**七个字段的取值**——算法、算法参数、`kp_min/max/growth`、瞄准位置、视野——`_fill_form` 自己只直接设了 `profile_enabled` / `profile_trigger` / `profile_target_class` 三个。而且它末尾有：

```python
        # kp 和视野是热更新的, 立刻推给正在跑的管线。
        self._write_runtime_aim_settings()
```

**所以「载入预设不重启就生效」是现有行为。** 把它拆开重写就丢了，用户立刻能察觉。Task 5 必须原样调用它，见那一节的代码。

**第 1180 行那个快照最容易漏，漏了是功能性 bug。** `_last_profile_triggers` 是 `profile_trigger` 的缓存，触发键冲突时用来回退到「上一个合法值」（`gui.py:1775` 读它、`gui.py:1782` 更新它）。载入预设换了触发键之后不刷新这份快照，冲突检测就拿旧值比——用户会看到触发键被莫名其妙改回去。`_fill_form` 原本就在 1180 行刷新它，注释也写了「这个值得跟着预设走」。它不是表单状态（不进 `FormState` 是对的），但适配层必须在 `_load_form_state()` 之后补上这一行。

所以本计划抽出的 `preset_to_form_state()` 只负责**取值**，这五处副作用留在 `gui.py` 里，由适配层在拿到新 `FormState` 之后照原顺序调用。

**三、`*_text` 变量不是状态。**

`confidence_text`、`iou_text`、`profile_fov_text`、`profile_kp_min_text` 等是滑条旁边那行数字的格式化结果（`f"{value:.3f}"`），由对应的数值变量派生。**不进 `FormState`**，适配层格式化即可。同理 `preset_choice`、`status` 是界面装饰，不是表单状态。

---

## Task 1: Prompter 协议

**Files:**
- Create: `rhodes_fast/gui_core/__init__.py`
- Create: `rhodes_fast/gui_core/prompts.py`
- Test: `tests/test_gui_core_prompts.py`

**Interfaces:**
- Consumes: 无
- Produces: `Prompter`（Protocol，四个方法）、`RecordingPrompter`（测试替身，带 `errors` / `infos` / `asked` 三个记录列表和 `answers` 队列）

`gui.py` 里共 **29 个对话框调用点**——27 个 `messagebox.show*/ask*` 加 2 个 `simpledialog.askstring`。（别用 `grep -c "messagebox\."` 数，那会把 `messagebox.WARNING` / `messagebox.CANCEL` 这 4 处常量引用也算进去，得 31。用 `grep -c "messagebox\.\(show\|ask\)"`。）

| 调用 | 次数 | 语义 |
|---|---|---|
| `messagebox.showerror` | 16 | 报错 |
| `messagebox.askokcancel` | 5 | 确认，默认取消 |
| `messagebox.showinfo` | 3 | 告知 |
| `messagebox.askyesnocancel` | 2 | 三态：是 / 否 / 取消 |
| `messagebox.showwarning` | 1 | 警告 |
| `simpledialog.askstring` | 2 | 要一段文字 |

映射成六个方法：`notify_error` / `notify_warning` / `notify_info` / `confirm` / `confirm_three_way` / `ask_text`。

**两处细节不能丢，否则「零可见变化」就破了：**

**一、`confirm` 要能表达「这是破坏性操作」。** 五个 `askokcancel` 里有两个传了额外参数（[gui.py:1210-1216](rhodes_fast/gui.py#L1210-L1216) 覆盖预设、[gui.py:1240-1247](rhodes_fast/gui.py#L1240-L1247) 删除预设）：

```python
        if not messagebox.askokcancel(
            "删除预设",
            f"确定删除预设「{name}」吗？\n\n删除后找不回来。界面上现在的设置不会变。",
            icon=messagebox.WARNING,
            # 默认按钮是取消: 手滑按回车删不掉。
            default=messagebox.CANCEL,
            parent=self.root,
        ):
```

`default=messagebox.CANCEL` 是有意的防手滑设计，代码里写了注释。所以签名是 `confirm(title, message, *, danger: bool = False) -> bool`，`danger=True` 时 tkinter 那边补上 `icon=WARNING, default=CANCEL`，WebView 那边映射成红色按钮 + 焦点落在取消。

**二、`showwarning` 单独一个方法，不要折进 `notify_error` 或 `notify_info`。** 折进去图标会变（警告 → 错误 / 信息），而它是用户能一眼看见的差异。为一个调用点多一个方法看着不划算，但这个计划的验收标准就是「零可见变化」，省这一个方法等于给自己挖一个必须在验收时解释的坑。

- [ ] **Step 1: 建包目录与空的 `__init__.py`**

```bash
mkdir "rhodes_fast/gui_core"
```

`rhodes_fast/gui_core/__init__.py` 先写成：

```python
"""跟 GUI 工具包无关的状态与业务逻辑, 供 tkinter 与 WebView 两套界面共用。"""

from .prompts import Prompter, RecordingPrompter

__all__ = ["Prompter", "RecordingPrompter"]
```

- [ ] **Step 2: 写失败测试**

`tests/test_gui_core_prompts.py`：

```python
import unittest

from rhodes_fast.gui_core.prompts import RecordingPrompter


class RecordingPrompterTest(unittest.TestCase):
    def test_records_errors_and_infos(self) -> None:
        prompter = RecordingPrompter()
        prompter.notify_error("设置无法保存", "端口不是数字")
        prompter.notify_info("完成", "设置已保存。")
        self.assertEqual(prompter.errors, [("设置无法保存", "端口不是数字")])
        self.assertEqual(prompter.infos, [("完成", "设置已保存。")])

    def test_confirm_defaults_to_false_when_no_answer_queued(self) -> None:
        """没排答案就当用户点了取消。这样忘记 mock 的测试会走「不做」那条路,
        而不是悄悄把删除操作跑完。"""
        prompter = RecordingPrompter()
        self.assertFalse(prompter.confirm("删除预设", "确定删除「默认」？"))

    def test_confirm_consumes_queued_answers_in_order(self) -> None:
        prompter = RecordingPrompter(answers=[True, False])
        self.assertTrue(prompter.confirm("一", "第一次"))
        self.assertFalse(prompter.confirm("二", "第二次"))
        self.assertFalse(prompter.confirm("三", "队列空了"))
        self.assertEqual([title for title, _ in prompter.asked], ["一", "二", "三"])

    def test_confirm_records_which_prompts_were_dangerous(self) -> None:
        """破坏性确认要能跟普通确认区分开 —— tkinter 那边靠它补 icon=WARNING
        和 default=CANCEL, 少传一个 danger 就是可见的安全性回退。"""
        prompter = RecordingPrompter(answers=[True, True])
        prompter.confirm("覆盖预设", "要用现在的设置覆盖它吗？", danger=True)
        prompter.confirm("重新探测", "重新读一次模型？")
        self.assertEqual(prompter.dangerous, ["覆盖预设"])

    def test_warnings_are_recorded_separately_from_errors(self) -> None:
        prompter = RecordingPrompter()
        prompter.notify_warning("触发键冲突", "两套方案不能用同一个键。")
        self.assertEqual(prompter.warnings, [("触发键冲突", "两套方案不能用同一个键。")])
        self.assertEqual(prompter.errors, [])

    def test_confirm_three_way_returns_none_for_cancel(self) -> None:
        prompter = RecordingPrompter(answers=[None])
        self.assertIsNone(prompter.confirm_three_way("保存", "先保存当前预设？"))

    def test_ask_text_returns_none_when_queue_empty(self) -> None:
        prompter = RecordingPrompter()
        self.assertIsNone(prompter.ask_text("重命名", "新名字", initial="旧名字"))


class ImportPurityTest(unittest.TestCase):
    def test_gui_core_does_not_import_tkinter(self) -> None:
        """gui_core 不许碰 tkinter —— WebView 界面要在没有 tk 的环境里 import 它。"""
        import subprocess
        import sys
        from pathlib import Path

        # 显式给 cwd: rhodes_fast 不是 pip 装的, 子进程要从仓库根目录才 import 得到。
        # 不给的话换个目录跑测试会得到一个跟 tkinter 毫无关系的 ModuleNotFoundError。
        repo_root = Path(__file__).resolve().parent.parent
        code = (
            "import sys; import rhodes_fast.gui_core; "
            "leaked = sorted(n for n in sys.modules if n == 'tkinter' or n.startswith('tkinter.')); "
            "assert not leaked, leaked"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_prompts -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_core.prompts'`

- [ ] **Step 4: 实现 `prompts.py`**

```python
"""「问用户」的回调协议。

业务逻辑不该知道自己跑在 tkinter 还是浏览器里, 所以它不直接调 messagebox,
而是调这里的 Prompter。tkinter 界面用 messagebox 实现它, WebView 界面用前端
弹窗实现它, 测试用 RecordingPrompter 实现它。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Prompter(Protocol):
    def notify_error(self, title: str, message: str) -> None:
        """报错。对应 messagebox.showerror。"""

    def notify_warning(self, title: str, message: str) -> None:
        """警告。对应 messagebox.showwarning。

        只有一个调用点 (触发键冲突), 但折进 notify_error 或 notify_info 图标就变了,
        而这个计划的验收标准是零可见变化。
        """

    def notify_info(self, title: str, message: str) -> None:
        """告知。对应 messagebox.showinfo。"""

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        """确认。对应 messagebox.askokcancel —— 默认是「不做」。

        danger=True 表示这是破坏性操作: tkinter 那边补上 icon=WARNING 和
        default=CANCEL (手滑按回车删不掉, 见 gui.py:1244 的注释),
        WebView 那边给红色按钮 + 焦点落在取消。
        """

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        """三态确认。对应 messagebox.askyesnocancel: True 是, False 否, None 取消。"""

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        """要一段文字。对应 simpledialog.askstring, 取消返回 None。"""


class RecordingPrompter:
    """测试替身: 记下问过什么, 按队列回答。

    队列空时一律答「不做」(confirm → False, ask_text → None)。忘记排答案的测试
    会走保守分支, 不会把删除操作真跑完。
    """

    def __init__(self, answers: list[object] | None = None) -> None:
        self.errors: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []
        self.asked: list[tuple[str, str]] = []
        self.dangerous: list[str] = []      # 标了 danger=True 的确认框标题
        self._answers = list(answers or [])

    def _next(self, default: object) -> object:
        if not self._answers:
            return default
        return self._answers.pop(0)

    def notify_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def notify_warning(self, title: str, message: str) -> None:
        self.warnings.append((title, message))

    def notify_info(self, title: str, message: str) -> None:
        self.infos.append((title, message))

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        self.asked.append((title, message))
        if danger:
            self.dangerous.append(title)
        return bool(self._next(False))

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        self.asked.append((title, message))
        answer = self._next(None)
        return None if answer is None else bool(answer)

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        self.asked.append((title, message))
        answer = self._next(None)
        return None if answer is None else str(answer)
```

- [ ] **Step 5: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_prompts -v
```

Expected: `Ran 8 tests` / `OK`

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/gui_core/__init__.py rhodes_fast/gui_core/prompts.py tests/test_gui_core_prompts.py
git commit -F - <<'MSG'
feat: add a toolkit-free prompter protocol for gui_core

The GUI's business logic calls messagebox in 33 places, which is what ties
it to tkinter. A Prompter protocol lets the same logic run behind a browser
dialog later, and lets tests assert on what was asked.

RecordingPrompter answers "don't do it" when a test forgets to queue an
answer, so a missing mock fails safe rather than running a delete.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 2: FormState 与 config → state

**Files:**
- Create: `rhodes_fast/gui_core/state.py`
- Modify: `rhodes_fast/gui_core/__init__.py`
- Test: `tests/test_gui_core_state.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `ProfileFormState` dataclass：`enabled: bool`、`trigger: str`、`target_class: str`、`aim_position: float`、`fov: float`、`kp_min: float`、`kp_max: float`、`kp_growth: float`、`algorithm: str`、`algorithm_params: dict[str, float]`
  - `FormState` dataclass（字段见下）
  - `Labels` frozen dataclass：六张 `{显示标签: 存储值}` 映射绑成一个对象
  - `config_to_form_state(config: AppConfig, config_dir: Path, labels: Labels, *, display_path) -> FormState`

字段来源是 `gui.py:191-259` 的 `_create_variables`，一一对应。

- [ ] **Step 1: 写失败测试**

`tests/test_gui_core_state.py`：

```python
import unittest
from pathlib import Path

from rhodes_fast.config import AppConfig
from rhodes_fast.gui import (
    INPUT_MODES, LOG_LANGUAGES, OUTPUT_FORMATS, PROVIDERS, TRIGGERS,
    _display_path, _resolve_model_path, algorithm_choices, algorithm_param_specs,
)
from rhodes_fast.gui_core.state import FormState, ProfileFormState, config_to_form_state


LABELS = Labels(
    provider=PROVIDERS,
    output_format=OUTPUT_FORMATS,
    input_mode=INPUT_MODES,
    language=LOG_LANGUAGES,
    trigger=TRIGGERS,
    algorithm=algorithm_choices(),
)


def make_config(**overrides) -> AppConfig:
    """造一个测试用的 AppConfig。

    不能写 AppConfig() —— 它是 frozen dataclass, 七个段都没有默认值, 空构造
    直接 TypeError。仓库里的入口是 default_config() (tests/test_presets.py
    就是这么用的)。

    每个字段都刻意偏离默认值: 转换函数漏掉某个字段时, 断言才有机会红。
    方案 1 用带参数的算法, 否则「参数回落默认值」那条测的是 {} == {}。
    """
    config = default_config()
    ...  # 逐段 dataclasses.replace, 把标量都改成非默认值
    return config


class ConfigToFormStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config()
        self.state = config_to_form_state(
            self.config, Path("C:/app"), LABELS, display_path=_display_path
        )

    def test_numeric_text_fields_stay_strings(self) -> None:
        """端口等字段在界面上是文本框, 用户能输入任何东西。提前转 int 会把
        「非法输入 → 弹窗报错」变成「构造 FormState 就崩」。"""
        self.assertIsInstance(self.state.udp_port, str)
        self.assertEqual(self.state.udp_port, str(self.config.udp.port))
        self.assertIsInstance(self.state.kmbox_port, str)

    def test_aim_position_is_a_percentage(self) -> None:
        """配置里存 0..1 的比例, 界面上是 0..100 的百分比滑条。"""
        expected = self.config.aim_profile_1.target_y_ratio * 100.0
        self.assertAlmostEqual(self.state.profiles[0].aim_position, expected)

    def test_algorithm_is_stored_as_its_display_label(self) -> None:
        stored = self.config.aim_profile_1.algorithm
        label = next(k for k, v in algorithm_choices().items() if v == stored)
        self.assertEqual(self.state.profiles[0].algorithm, label)

    def test_missing_algorithm_params_fall_back_to_defaults(self) -> None:
        config = make_config(profile_1_algorithm_params={})
        state = config_to_form_state(config, Path("C:/app"), LABELS, display_path=_display_path)
        expected = {spec.name: spec.default for spec in algorithm_param_specs(config.aim_profile_1.algorithm)}
        self.assertTrue(expected, "挑个带参数的算法, 否则这条测了个空")
        self.assertEqual(state.profiles[0].algorithm_params, expected)

    def test_trail_toggles_ignore_the_config_and_start_from_defaults(self) -> None:
        """预览页的三个勾选框每次打开都回到默认, 只有轨迹长度从配置恢复。
        对应 gui.py:214 的注释。"""
        self.assertFalse(self.state.trail_enabled)
        self.assertEqual(self.state.trail_seconds, self.config.ui.trail_seconds)

    def test_latency_log_always_starts_off(self) -> None:
        self.assertFalse(self.state.latency_log_enabled)

    def test_two_profiles(self) -> None:
        self.assertEqual(len(self.state.profiles), 2)
        self.assertIsInstance(self.state.profiles[0], ProfileFormState)

    def test_form_state_is_a_plain_dataclass(self) -> None:
        """能直接 asdict 成 JSON —— WebView 界面靠这个跟前端通信。"""
        import dataclasses
        self.assertTrue(dataclasses.is_dataclass(FormState))
        dataclasses.asdict(self.state)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_core.state'`

- [ ] **Step 3: 实现 `state.py` 的 dataclass 与 `config_to_form_state`**

```python
"""表单状态: 界面上那些格子里装的东西, 跟用什么控件画无关。

FormState 里存的是「显示值」而不是「存储值」—— 下拉框里是「自动（推荐）」,
配置文件里是 "auto"。两者的映射表在 gui.py 顶部 (PROVIDERS / OUTPUT_FORMATS /
INPUT_MODES / LOG_LANGUAGES / TRIGGERS), 由调用方传进来, 这样本模块不需要
知道界面的中文文案。

数值输入框一律存字符串, 因为用户能往里打任何东西; 解析放到
form_state_to_config, 让「输入非法 → 弹窗报错」这条路保持原样。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AppConfig
from ..trail import TrailSettings


@dataclass
class ProfileFormState:
    enabled: bool
    trigger: str
    target_class: str
    aim_position: float
    fov: float
    kp_min: float
    kp_max: float
    kp_growth: float
    algorithm: str
    algorithm_params: dict[str, float] = field(default_factory=dict)


@dataclass
class FormState:
    model_path: str
    provider: str
    cuda_graph: bool
    gpu_preprocess: bool
    output_format: str
    confidence: float
    iou: float
    input_mode: str
    log_language: str
    udp_host: str
    udp_port: str
    udp_width: str
    udp_height: str
    obs_host: str
    obs_port: str
    obs_password: str
    obs_source: str
    kmbox_enabled: bool
    kmbox_host: str
    kmbox_port: str
    kmbox_uuid: str
    latency_log_enabled: bool
    preview_frame: bool
    trail_enabled: bool
    trail_optimal_path: bool
    trail_seconds: float
    profiles: tuple[ProfileFormState, ProfileFormState]


def _label(mapping: dict[str, str], stored: str) -> str:
    """存储值 → 显示标签。逐字等价于 gui.py:73 的 _display_value。

    兜底是「第一个标签」不是 stored 本身 —— 这条容易写错, 而且后果是可见的。
    settings.txt 指着一个已删掉的算法是真实场景 (gui.py:101 那句注释
    「配置里指着一个已删掉的算法时界面仍要画得出来」就是为它写的), 这时返回
    stored 会把一个候选之外的字符串塞进下拉框。
    """
    return next((label for label, value in mapping.items() if value == stored), next(iter(mapping)))


@dataclass(frozen=True, slots=True)
class Labels:
    """界面文案 ↔ 配置存储值的六张映射, 方向一律 {显示标签: 存储值}。

    绑成一个对象而不是散着传六个参数: 它们类型全是 dict[str, str], 散着传时
    provider 和 output_format 调了位置类型检查一声不吭, 症状是某个下拉框显示
    错内容 —— 而测试两边传的是同一份 dict, 照样绿。
    """

    provider: dict[str, str]
    output_format: dict[str, str]
    input_mode: dict[str, str]
    language: dict[str, str]
    trigger: dict[str, str]
    algorithm: dict[str, str]


def _param_defaults(algorithm: str) -> dict[str, float]:
    """算法标识 → {参数名: 默认值}。

    直接读注册表, 不走注入: aim_algorithms 不碰 tkinter, gui_core 可以直接
    import 它, 而注入版唯一的实现就是它自己 —— 那不叫可测性, 叫多一个参数。
    照 gui.py:101 的做法, 算法不存在时返回空而不是抛异常。
    """
    algorithm_type = available_algorithms().get(algorithm)
    return {} if algorithm_type is None else {spec.name: spec.default for spec in algorithm_type.PARAMS}


def _profile_from_config(profile: AimProfileConfig, labels: Labels) -> ProfileFormState:
    defaults = _param_defaults(profile.algorithm)
    return ProfileFormState(
        enabled=profile.enabled,
        trigger=_label(labels.trigger, profile.trigger),
        target_class=str(profile.target_class),
        aim_position=profile.target_y_ratio * 100.0,
        fov=profile.fov_radius,
        kp_min=profile.kp_min,
        kp_max=profile.kp_max,
        kp_growth=profile.kp_growth,
        algorithm=_label(labels.algorithm, profile.algorithm),
        algorithm_params={
            name: profile.algorithm_params.get(name, default)
            for name, default in defaults.items()
        },
    )


def config_to_form_state(
    config: AppConfig,
    config_dir: Path,
    labels: Labels,
    *,
    display_path: Callable[[Path, Path], str],
) -> FormState:
    """AppConfig → FormState。对应 gui.py:191-259 的 _create_variables。"""
    trail_defaults = TrailSettings()
    return FormState(
        model_path=display_path(config.model.path, config_dir),
        provider=_label(labels.provider, config.model.provider),
        cuda_graph=config.model.cuda_graph,
        gpu_preprocess=config.model.gpu_preprocess,
        output_format=_label(labels.output_format, config.model.output_format),
        confidence=config.model.confidence,
        iou=config.model.iou,
        input_mode=_label(labels.input_mode, config.input.mode),
        log_language=_label(labels.language, config.ui.language),
        udp_host=config.udp.host,
        udp_port=str(config.udp.port),
        udp_width=str(config.udp.width),
        udp_height=str(config.udp.height),
        obs_host=config.obs.host,
        obs_port=str(config.obs.port),
        obs_password=config.obs.password,
        obs_source=config.obs.source_name,
        kmbox_enabled=config.kmbox.enabled,
        kmbox_host=config.kmbox.host,
        kmbox_port=str(config.kmbox.port),
        kmbox_uuid=config.kmbox.uuid,
        # 延迟日志是一次性的运行开关, 不从配置恢复。对应 gui.py:213。
        latency_log_enabled=False,
        # 预览页的勾选框每次打开都回到默认, 只有轨迹长度记住。对应 gui.py:214-219。
        preview_frame=trail_defaults.show_frame,
        trail_enabled=trail_defaults.enabled,
        trail_optimal_path=trail_defaults.optimal_path,
        trail_seconds=config.ui.trail_seconds,
        profiles=tuple(
            _profile_from_config(profile, labels) for profile in config.aim_profiles
        ),
    )
```

文件头补 `from ..aim_algorithms import available_algorithms`、`from ..config import AimProfileConfig`。

> **映射方向别搞反**：`gui.py` 的 `PROVIDERS` / `OUTPUT_FORMATS` / `INPUT_MODES` / `LOG_LANGUAGES` / `TRIGGERS` 和 `algorithm_choices()` 都是 **`{显示标签: 存储值}`**。`config_to_form_state` 要的是反向查找，所以用 `_label()` 遍历；`form_state_to_config`（Task 3）要的是正向，直接 `mapping[label]`。两个方向共用同一份 dict，不要另建一份反向表——两份会漂。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `Ran 8 tests` / `OK`

- [ ] **Step 5: 导出并提交**

`rhodes_fast/gui_core/__init__.py` 改为：

```python
"""跟 GUI 工具包无关的状态与业务逻辑, 供 tkinter 与 WebView 两套界面共用。"""

from .prompts import Prompter, RecordingPrompter
from .state import FormState, ProfileFormState, config_to_form_state

__all__ = [
    "FormState",
    "ProfileFormState",
    "Prompter",
    "RecordingPrompter",
    "config_to_form_state",
]
```

```bash
git add rhodes_fast/gui_core/state.py rhodes_fast/gui_core/__init__.py tests/test_gui_core_state.py
git commit -F - <<'MSG'
feat: add FormState and the config-to-state conversion

FormState holds what the form shows -- display labels, not stored values --
so a widget can bind to it directly. Numeric inputs stay strings because
users can type anything into them; parsing them early would turn today's
"invalid input pops an error" into "constructing the state crashes".

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 3: state → config

**Files:**
- Modify: `rhodes_fast/gui_core/state.py`
- Test: `tests/test_gui_core_state.py`

**Interfaces:**
- Consumes: `FormState`、`ProfileFormState`（Task 2）
- Produces: `form_state_to_config(state, base, config_dir, labels: Labels, *, current_preset, resolve_model_path) -> AppConfig`、`trail_settings_from_state(state) -> TrailSettings`

对应 `gui.py:1359-1449` 的 `_read_form` 和 `gui.py:1463-1475` 的 `_current_trail_settings`。

四个**必须保留**的语义（读原代码时容易漏）：

1. `aim` 段跟方案 1 同步（`gui.py:1394-1395` 的注释：旧配置消费者和 pipeline benchmark 还要读它）
2. `target_y_ratio` 要 `max(0.0, min(1.0, aim_position / 100.0))` 夹紧
3. `output_layout` 硬写 `"auto"`（`gui.py:1367`）
4. `kmbox.uuid` 要 `.strip().upper()`；`udp_host` / `obs_source` / `kmbox_host` 要 `.strip()`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_core_state.py`）**

```python
class FormStateToConfigTest(unittest.TestCase):
    def _state(self) -> FormState:
        return config_to_form_state(make_config(), Path("C:/app"), LABELS, display_path=_display_path)

    def test_round_trip_preserves_the_config(self) -> None:
        """改一圈再转回去, 除了显式重写的字段外应该原样还原。"""
        base = make_config()
        state = config_to_form_state(base, Path("C:/app"), LABELS, display_path=_display_path)
        result = form_state_to_config(
            state, base, Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.udp.port, base.udp.port)
        self.assertEqual(result.kmbox.host, base.kmbox.host)
        self.assertEqual(result.aim_profile_1.algorithm, base.aim_profile_1.algorithm)

    def test_invalid_port_raises_valueerror(self) -> None:
        """这是「输入非法 → 弹窗报错」那条路的入口, 必须抛, 不能吞。"""
        state = self._state()
        state.udp_port = "不是数字"
        with self.assertRaises(ValueError):
            form_state_to_config(
                state, make_config(), Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
            )

    def test_aim_section_mirrors_profile_one(self) -> None:
        """gui.py:1394 —— 旧配置消费者和 pipeline benchmark 还在读 aim 段。"""
        state = self._state()
        state.profiles[0].target_class = "7"
        state.profiles[0].fov = 123.0
        result = form_state_to_config(
            state, make_config(), Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.aim.target_class, 7)
        self.assertEqual(result.aim.fov_radius, 123.0)

    def test_aim_position_is_clamped_to_zero_one(self) -> None:
        state = self._state()
        state.profiles[0].aim_position = 250.0
        result = form_state_to_config(
            state, make_config(), Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.aim_profile_1.target_y_ratio, 1.0)

    def test_uuid_is_upper_cased_and_stripped(self) -> None:
        state = self._state()
        state.kmbox_uuid = "  abc123  "
        result = form_state_to_config(
            state, make_config(), Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.kmbox.uuid, "ABC123")

    def test_output_layout_is_always_auto(self) -> None:
        result = form_state_to_config(
            self._state(), make_config(), Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.model.output_layout, "auto")

    def test_current_preset_lands_in_ui_section(self) -> None:
        result = form_state_to_config(
            self._state(), make_config(), Path("C:/app"), LABELS,
                current_preset="夜间", resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.ui.preset, "夜间")

    def test_no_preset_becomes_empty_string(self) -> None:
        result = form_state_to_config(
            self._state(), make_config(), Path("C:/app"), LABELS,
                current_preset=None, resolve_model_path=_resolve_model_path,
        )
        self.assertEqual(result.ui.preset, "")


class TrailSettingsFromStateTest(unittest.TestCase):
    def test_seconds_are_clamped_and_rounded_to_one_decimal(self) -> None:
        """滑条是连续的, 存 0.1 秒一档: 设置文件里不该出现 1.2749 这种数。
        对应 gui.py:1468。"""
        from rhodes_fast.config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS

        state = config_to_form_state(make_config(), Path("C:/app"), LABELS, display_path=_display_path)
        state.trail_seconds = 1.2749
        self.assertEqual(trail_settings_from_state(state).seconds, 1.3)

        state.trail_seconds = TRAIL_MAX_SECONDS + 100
        self.assertEqual(trail_settings_from_state(state).seconds, TRAIL_MAX_SECONDS)

        state.trail_seconds = TRAIL_MIN_SECONDS - 100
        self.assertEqual(trail_settings_from_state(state).seconds, TRAIL_MIN_SECONDS)
```

> `LABELS` 和 `make_config()` 在 Task 2 的测试文件里已经定义好了，直接用。**不要写 `AppConfig()`** —— 它是 frozen dataclass 且七个段都没默认值，空构造会 `TypeError`。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `ImportError: cannot import name 'form_state_to_config'`

- [ ] **Step 3: 实现**

在 `state.py` 追加：

```python
from ..config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS


def trail_settings_from_state(state: FormState) -> TrailSettings:
    """对应 gui.py:1463-1475。tk 的 TclError 分支留在适配层, 这里只管夹紧与取整。"""
    seconds = round(min(TRAIL_MAX_SECONDS, max(TRAIL_MIN_SECONDS, state.trail_seconds)) * 10) / 10
    return TrailSettings(
        show_frame=bool(state.preview_frame),
        enabled=bool(state.trail_enabled),
        seconds=seconds,
        optimal_path=bool(state.trail_optimal_path),
    )


def _profile_to_config(profile: ProfileFormState, base: AimProfileConfig, labels: Labels) -> AimProfileConfig:
    return dataclasses.replace(
        base,
        enabled=profile.enabled,
        trigger=labels.trigger[profile.trigger],
        kp_min=profile.kp_min,
        kp_max=profile.kp_max,
        kp_growth=profile.kp_growth,
        target_class=int(profile.target_class),
        target_y_ratio=max(0.0, min(1.0, profile.aim_position / 100.0)),
        fov_radius=profile.fov,
        algorithm=labels.algorithm[profile.algorithm],
        algorithm_params=dict(profile.algorithm_params),
    )


def form_state_to_config(
    state: FormState,
    base: AppConfig,
    config_dir: Path,
    labels: Labels,
    *,
    current_preset: str | None,
    resolve_model_path: Callable[[str, Path], Path],
) -> AppConfig:
    """FormState → AppConfig。对应 gui.py:1359-1449 的 _read_form。

    非法输入在这里抛: 数字格式不对是 ValueError, 下拉框标签不在映射里是
    KeyError (labels.provider[...] 直接索引, 跟 gui.py 的 PROVIDERS[...] 一样)。
    两种都不接住 —— 调用方接住它弹窗, 这正是
    现在的行为 (gui.py:1451-1458)。
    """
    model = dataclasses.replace(
        base.model,
        path=resolve_model_path(state.model_path.strip(), config_dir),
        provider=labels.provider[state.provider],
        cuda_graph=state.cuda_graph,
        gpu_preprocess=state.gpu_preprocess,
        output_format=labels.output_format[state.output_format],
        output_layout="auto",
        confidence=state.confidence,
        iou=state.iou,
    )
    udp = dataclasses.replace(
        base.udp,
        host=state.udp_host.strip(),
        port=int(state.udp_port),
        width=int(state.udp_width),
        height=int(state.udp_height),
    )
    obs = dataclasses.replace(
        base.obs,
        host=state.obs_host.strip(),
        port=int(state.obs_port),
        password=state.obs_password,
        source_name=state.obs_source.strip(),
    )
    kmbox = dataclasses.replace(
        base.kmbox,
        enabled=state.kmbox_enabled,
        host=state.kmbox_host.strip(),
        port=int(state.kmbox_port),
        uuid=state.kmbox_uuid.strip().upper(),
    )
    first = state.profiles[0]
    # aim 段跟方案 1 同步: 旧配置消费者和 pipeline benchmark 还在读它。
    aim = dataclasses.replace(
        base.aim,
        target_class=int(first.target_class),
        target_y_ratio=max(0.0, min(1.0, first.aim_position / 100.0)),
        fov_radius=first.fov,
    )
    return dataclasses.replace(
        base,
        input=dataclasses.replace(base.input, mode=labels.input_mode[state.input_mode]),
        ui=dataclasses.replace(
            base.ui,
            language=labels.language[state.log_language],
            preset=current_preset or "",
            trail_seconds=trail_settings_from_state(state).seconds,
        ),
        udp=udp,
        obs=obs,
        model=model,
        kmbox=kmbox,
        aim=aim,
        aim_profile_1=_profile_to_config(state.profiles[0], base.aim_profile_1, labels),
        aim_profile_2=_profile_to_config(state.profiles[1], base.aim_profile_2, labels),
    )
```

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `Ran 17 tests` / `OK`

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/gui_core/state.py tests/test_gui_core_state.py
git commit -F - <<'MSG'
feat: convert FormState back into an AppConfig

Mirrors _read_form, including the four things that are easy to drop when
re-reading it: the aim section stays in sync with profile 1 for the older
config consumers, target_y_ratio is clamped to 0..1, output_layout is
pinned to "auto", and the KMBox UUID is stripped and upper-cased.

Parsing raises ValueError rather than swallowing it -- that is the path
that currently reaches the "settings could not be saved" dialog.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 4: preset → state

**Files:**
- Modify: `rhodes_fast/gui_core/state.py`
- Test: `tests/test_gui_core_state.py`

**Interfaces:**
- Consumes: `FormState`、`ProfileFormState`
- Produces: `apply_preset_to_state(state, preset, config_dir, labels: Labels, *, display_path) -> None`（原地改 `state`）

对应 `gui.py:1129-1186` 的 `_fill_form` 的**取值部分**。五处控件副作用（见前面「背景」第二条）留在 `gui.py`。

一个**必须保留**的语义：`gui.py:1165-1166` 的注释——切预设时先把 `algorithm_params` 清空再灌，否则文件里没写的参数会沿用上一个预设的值，而不是回到默认。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_core_state.py`）**

```python
class ApplyPresetToStateTest(unittest.TestCase):
    def test_stale_algorithm_params_do_not_leak_across_presets(self) -> None:
        """gui.py:1165 —— 文件里没写的参数该回到默认, 不该沿用上一个预设的值。"""
        state = config_to_form_state(make_config(), Path("C:/app"), LABELS, display_path=_display_path)
        state.profiles[0].algorithm_params = {"gain": 9.9, "lead": 9.9}

        preset = make_preset(algorithm="feedforward", algorithm_params={"gain": 2.0})
        apply_preset_to_state(state, preset, Path("C:/app"), LABELS, display_path=_display_path)

        self.assertEqual(state.profiles[0].algorithm_params["gain"], 2.0)
        self.assertEqual(state.profiles[0].algorithm_params["lead"], 4.0)  # 回到默认, 不是 9.9

    def test_ports_come_back_as_strings(self) -> None:
        state = config_to_form_state(make_config(), Path("C:/app"), LABELS, display_path=_display_path)
        preset = make_preset()
        apply_preset_to_state(state, preset, Path("C:/app"), LABELS, display_path=_display_path)
        self.assertIsInstance(state.udp_port, str)

    def test_latency_and_trail_toggles_are_left_alone(self) -> None:
        """预设不该动预览页的临时开关。"""
        state = config_to_form_state(make_config(), Path("C:/app"), LABELS, display_path=_display_path)
        state.trail_enabled = True
        state.latency_log_enabled = True
        apply_preset_to_state(state, make_preset(), Path("C:/app"), LABELS, display_path=_display_path)
        self.assertTrue(state.trail_enabled)
        self.assertTrue(state.latency_log_enabled)
```

> `make_preset(**overrides)` 是本测试文件里的辅助函数，用 `rhodes_fast.presets.Preset` 造一个预设并覆盖指定字段。跟 `make_config` 一样，每个字段都要偏离默认值，否则「灌进去了没有」测不出来。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `ImportError: cannot import name 'apply_preset_to_state'`

- [ ] **Step 3: 实现**

在 `state.py` 追加：

```python
def apply_preset_to_state(
    state: FormState,
    preset: Preset,
    config_dir: Path,
    labels: Labels,
    *,
    display_path: Callable[[Path, Path], str],
) -> None:
    """把预设灌进 state。对应 gui.py:1129-1186 的取值部分。

    只改预设覆盖的字段: 预览页的临时开关 (trail_*, latency_log_enabled) 和
    当前预设名不属于预设内容, 原样留着。

    控件副作用 (_sync_cuda_graph_control / _switch_input_panel /
    _apply_tuning_to_form / _inspect_selected_model / _update_target_class_choices)
    留在 gui.py 的适配层, 由它在本函数返回后照原顺序调用。
    """
    model = preset.model
    state.model_path = display_path(model.path, config_dir)
    state.provider = _label(labels.provider, model.provider)
    state.cuda_graph = model.cuda_graph
    state.gpu_preprocess = model.gpu_preprocess
    state.output_format = _label(labels.output_format, model.output_format)
    state.confidence = model.confidence
    state.iou = model.iou
    state.input_mode = _label(labels.input_mode, preset.input.mode)
    state.udp_host = preset.udp.host
    state.udp_port = str(preset.udp.port)
    state.udp_width = str(preset.udp.width)
    state.udp_height = str(preset.udp.height)
    state.obs_host = preset.obs.host
    state.obs_port = str(preset.obs.port)
    state.obs_password = preset.obs.password
    state.obs_source = preset.obs.source_name
    state.kmbox_enabled = preset.kmbox.enabled
    state.kmbox_host = preset.kmbox.host
    state.kmbox_port = str(preset.kmbox.port)
    state.kmbox_uuid = preset.kmbox.uuid
    state.profiles = tuple(
        _profile_from_config(profile, labels) for profile in preset.aim_profiles
    )
```

> `_profile_from_config` 每次重建 `algorithm_params`，天然满足「不沿用上一个预设」——这就是 `gui.py:1166` 那行 `= {}` 的等价物。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `Ran 20 tests` / `OK`

- [ ] **Step 5: 导出并提交**

`__init__.py` 的 `from .state import ...` 补上 `apply_preset_to_state`、`form_state_to_config`、`trail_settings_from_state`，`__all__` 同步。

```bash
git add rhodes_fast/gui_core/state.py rhodes_fast/gui_core/__init__.py tests/test_gui_core_state.py
git commit -F - <<'MSG'
feat: apply a preset onto a FormState

Covers the value half of _fill_form. The widget side effects it interleaves
-- syncing the CUDA Graph control, swapping the input panel, rebuilding the
algorithm params, inspecting the model, refilling the target class list --
stay in gui.py, because they are tkinter, not state.

Rebuilding the profiles wholesale is what keeps a previous preset's
algorithm params from leaking into one that does not list them.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 5: 旧 tkinter 界面接上 state.py

**Files:**
- Modify: `rhodes_fast/gui.py:191-259`（`_create_variables`）、`gui.py:1129-1186`（`_fill_form`）、`gui.py:1359-1449`（`_read_form`）、`gui.py:1463-1475`（`_current_trail_settings`）
- Test: 现有 `tests/test_gui_presets.py`、`tests/test_gui_projectile.py`、`tests/test_gui_library.py`

**Interfaces:**
- Consumes: `config_to_form_state`、`form_state_to_config`、`apply_preset_to_state`、`trail_settings_from_state`
- Produces: `RhodesFastGui._form_state() -> FormState`（从 tk 变量收出一个 FormState）、`RhodesFastGui._load_form_state(state: FormState) -> None`（把 FormState 写回 tk 变量）

这一步**不新增行为**，纯接线。tk 变量继续存在、继续是控件的数据源；`FormState` 只在读写的那一刻作为中间层出现。

- [ ] **Step 1: 先跑一遍基线，记下数字**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | Select-String -Pattern "^Ran |^OK|^FAILED"
```

Expected: `Ran 572 tests` / `OK`（544 原有 + Task 1-4 新增 28）

- [ ] **Step 2: 加 `_form_state()` 和 `_load_form_state()`**

在 `RhodesFastGui` 里新增这两个方法。`_form_state()` 逐个读 tk 变量填 `FormState`；`_load_form_state()` 反过来 `.set()` 回去，并负责格式化那些 `*_text` 变量：

```python
    def _form_state(self, *, algorithm_params: bool = True) -> FormState:
        """把 tk 变量收成一个 FormState。数值输入框原样取字符串, 解析留给
        form_state_to_config —— 非法输入要走到「设置无法保存」那个弹窗。

        algorithm_params=False 时跳过算法参数框。那几个框是能手打的 DoubleVar,
        打到一半时 .get() 会抛 TclError; 而 _current_trail_settings 和 _fill_form
        今天根本不读它们, 它们的调用方也没一个接得住 TclError
        (_show_trail_seconds 连 try 都没有)。不跳过就等于给这两条路新增了一种崩法。
        _read_form 保持 True —— 保存那条路必须读, 也必须让它抛。
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
                    enabled=self.profile_enabled[i].get(),
                    trigger=self.profile_trigger[i].get(),
                    target_class=self.profile_target_class[i].get(),
                    aim_position=self.profile_aim_position[i].get(),
                    fov=self.profile_fov[i].get(),
                    kp_min=self.profile_kp_min[i].get(),
                    kp_max=self.profile_kp_max[i].get(),
                    kp_growth=self.profile_kp_growth[i].get(),
                    algorithm=self.profile_algorithm[i].get(),
                    algorithm_params={
                        name: variable.get()
                        for name, variable in self.profile_algorithm_params[i].items()
                    } if algorithm_params else {},
                )
                for i in (0, 1)
            ),
        )

    def _safe_trail_seconds(self) -> float:
        """tk 的 DoubleVar 装了非数字时 .get() 会抛 TclError。这个分支是 tk
        特有的, 所以留在适配层, 不进 gui_core。对应 gui.py:1464-1467。"""
        try:
            return float(self.trail_seconds.get())
        except (tk.TclError, ValueError):
            return self.config.ui.trail_seconds

    def _load_form_state(self, state: FormState) -> None:
        """把 FormState 写回 tk 变量, 顺带刷新滑条旁边那些格式化文本。

        算法参数变量字典不在这里重建 —— 换算法要连控件一起重建, 那是
        _apply_tuning_to_form 的活, 由调用方在本方法之后调。
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
        # 只设 _fill_form 自己直接设的那三个 (gui.py:1162-1164)。算法、算法参数、
        # kp 三件套、瞄准位置、视野归 _apply_tuning_to_form —— 它要按调过的顺序
        # 重建控件再挂 trace, 在这里抢着设会把半份配置推给管线。
        for index, profile in enumerate(state.profiles):
            self.profile_enabled[index].set(profile.enabled)
            self.profile_trigger[index].set(profile.trigger)
            self.profile_target_class[index].set(profile.target_class)
```

> 格式化串（`:.3f`、`:.0f`、`%`）逐个照 `gui.py:191-259` 抄，**别凭感觉写**——位数改了用户一眼就能看出来。

- [ ] **Step 3: 把四个方法改成委托**

```python
    def _read_form(self) -> AppConfig:
        return form_state_to_config(
            self._form_state(),
            self.config,
            self.config_path.parent,
            self._labels(),
            current_preset=self.current_preset,
            resolve_model_path=_resolve_model_path,
        )

    def _labels(self) -> Labels:
        """把 gui.py 的六张映射打包。algorithm_choices() 每次现取: 导入算法后
        注册表会变, 缓存下来的话新算法的显示名就查不到了。"""
        return Labels(
            provider=PROVIDERS,
            output_format=OUTPUT_FORMATS,
            input_mode=INPUT_MODES,
            language=LOG_LANGUAGES,
            trigger=TRIGGERS,
            algorithm=algorithm_choices(),
        )

    def _current_trail_settings(self) -> TrailSettings:
        return trail_settings_from_state(self._form_state(algorithm_params=False))
```

`_fill_form` 改成下面这样。**三个陷阱都在这段代码里，逐条看注释：**

```python
    def _fill_form(self, preset: Preset) -> None:
        # 陷阱一: previous_model 必须在灌新值之前取。apply_preset_to_state 第一行
        # 就把 state.model_path 盖掉了, 之后再取就恒等于新路径, 模型永远不再重新
        # 探测 —— 目标标签会被旧模型的类别数判越界, 悄悄改成 0 (gui.py:1181 的注释)。
        previous_model = _resolve_model_path(self.model_path.get().strip(), self.config_path.parent)

        state = self._form_state()
        apply_preset_to_state(
            state, preset, self.config_path.parent, self._labels(), display_path=_display_path
        )
        self._load_form_state(state)      # 不含方案的那七个字段, 见下
        self._sync_cuda_graph_control()
        self._switch_input_panel()

        # 陷阱二: 不要把 _apply_tuning_to_form 拆开重写。它不只是「重建参数控件」,
        # 它还设算法/算法参数/kp_min/kp_max/kp_growth/瞄准位置/视野这七个字段,
        # 末尾调 _write_runtime_aim_settings() 把 kp 和视野热推给正在跑的管线
        # (gui.py:1042)。拆开等于让「载入预设不重启就生效」退化成「要停了重启」,
        # 用户能立刻察觉 —— 直接破本计划唯一的硬验收标准。
        for index, profile in enumerate(preset.aim_profiles):
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

        # 陷阱三: 触发键快照要跟着预设走 (gui.py:1180)。漏了它, 冲突检测会拿旧值比,
        # 用户会看到触发键被莫名其妙改回去。
        self._last_profile_triggers = [variable.get() for variable in self.profile_trigger]

        if previous_model != preset.model.path or self.model_contract is None:
            self._inspect_selected_model(preset.model.path)
        else:
            self._update_target_class_choices()
```

> 最后那个 `if/else` 是**二选一**，不是顺序执行的两处副作用。

**`_load_form_state()` 不要设方案的那七个字段**（`profile_algorithm` / `profile_kp_min` / `profile_kp_max` / `profile_kp_growth` / `profile_aim_position` / `profile_fov` / 算法参数字典），它们归 `_apply_tuning_to_form`。它只设 `profile_enabled` / `profile_trigger` / `profile_target_class` 这三个——正是 `gui.py:1162-1164` 直接设的那三个。

**为什么这条界必须守住**：`_rebuild_algorithm_params`（gui.py:662-668）给每个参数变量挂了 `trace_add("write", … _write_runtime_aim_settings())`，代码里的注释写着「挂早了, 重建途中触发的那一下会读到上一个算法的参数名, 把半份配置推给管线」。这条链的顺序是调过的，别动。

顺带一提：`profile_algorithm` 的变化走的是 `<<ComboboxSelected>>`（gui.py:465），**是用户交互事件，`.set()` 不触发**。所以 `_load_form_state` 就算设了算法标签也不会连带重建参数字典——这正是 `_apply_tuning_to_form` 不可省的原因。

`_create_variables` 改成先 `state = config_to_form_state(self.config, self.config_path.parent, ...)`，再用 `state` 的字段建 tk 变量。

- [ ] **Step 4: 跑全量测试**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | Select-String -Pattern "^Ran |^OK|^FAILED"
```

Expected: `Ran 572 tests` / `OK`

**如果有失败**：不要改测试去迁就实现。这些测试锁的就是「旧界面行为不变」，红了说明接线接错了。用 `superpowers:systematic-debugging` 定位。

- [ ] **Step 5: 手动验证旧界面**

```bash
.venv/Scripts/python.exe -m rhodes_fast.gui
```

逐项确认：

1. 四个页签都能打开，控件初值跟改动前一致
2. 改一个端口为 `abc` → 点保存 → 弹「设置无法保存」，不是崩溃
3. 换预设 → 表单跟着变，算法参数控件重建，目标标签下拉框重填
4. 滑条拖动 → 旁边的数字跟着变（`*_text` 变量）
5. 切 provider → CUDA Graph 复选框跟着启用/禁用
6. 切输入模式 → UDP / OBS 面板跟着换
7. 保存设置 → `settings.txt` 内容跟改动前逐字节一致（先备份一份再比）

> **别写「保存前后 config 全等」的自动化测试。** `target_y_ratio` 在界面上是 0..100 的百分比、配置里是 0..1 的比例，来回换算必然拖浮点噪声（`0.27 * 100.0 / 100.0 == 0.27000000000000005`）。这是 tk 今天就有的行为（`DoubleVar` 装的也是百分比），不是本次重构引入的回归，但严格相等的断言会在这里撞墙。要比就比 `settings.txt` 的文本，或者对这个字段用 `assertAlmostEqual`。

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/gui.py
git commit -F - <<'MSG'
refactor: drive the tkinter form through gui_core.state

The tk variables stay -- they are still what the widgets bind to -- but
reading and writing the form now goes through FormState, so the same
conversions will serve the WebView UI without a second copy.

_fill_form keeps its five widget side effects and still runs them in the
original order; only the value half moved. The TclError fallback on the
trail slider stays here too, since that failure mode is tkinter's.

Test suite: 572 tests, OK.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 6: GuiSession — 预设操作

**Files:**
- Create: `rhodes_fast/gui_core/session.py`
- Modify: `rhodes_fast/gui.py:1045-1128`、`gui.py:1187-1258`
- Test: `tests/test_gui_core_session.py`

**Interfaces:**
- Consumes: `Prompter`（Task 1）、`FormState`（Task 2）
- Produces: `GuiSession` 类，本任务提供：
  - `GuiSession(config_path: Path, prompter: Prompter)`
  - `.list_presets() -> list[str]`
  - `.load_preset(name: str) -> Preset | None`
  - `.store_preset(name: str, config: AppConfig) -> bool`
  - `.delete_preset(name: str) -> bool`

搬的是 `gui.py` 这几个方法的**非控件部分**：`_restore_last_preset`（1045）、`_load_preset`（1096）、`_save_preset`（1187）、`_save_preset_as`（1192）、`_store_preset`（1221）、`_delete_preset`（1236）。

改动规则（逐处）：
- `messagebox.showerror(标题, 正文, parent=self.root)` → `self.prompter.notify_error(标题, 正文)`
- `messagebox.askokcancel(...)` → `self.prompter.confirm(...)`
- `messagebox.askyesnocancel(...)` → `self.prompter.confirm_three_way(...)`
- `simpledialog.askstring(...)` → `self.prompter.ask_text(..., initial=...)`
- 所有 `self._append_log(...)` 换成返回值或 `self.log(...)` 回调，由界面决定怎么显示

- [ ] **Step 1: 写失败测试**

`tests/test_gui_core_session.py`：

```python
import tempfile
import unittest
from pathlib import Path

from rhodes_fast.config import AppConfig
from rhodes_fast.gui_core.prompts import RecordingPrompter
from rhodes_fast.gui_core.session import GuiSession


class PresetOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.prompter = RecordingPrompter()
        self.session = GuiSession(self.root / "settings.txt", self.prompter)
        self.addCleanup(self._dir.cleanup)

    def test_store_then_list_then_load(self) -> None:
        self.assertTrue(self.session.store_preset("夜间", make_config()))
        self.assertIn("夜间", self.session.list_presets())
        self.assertIsNotNone(self.session.load_preset("夜间"))

    def test_loading_a_missing_preset_reports_an_error(self) -> None:
        self.assertIsNone(self.session.load_preset("不存在"))
        self.assertEqual(len(self.prompter.errors), 1)

    def test_delete_asks_before_removing(self) -> None:
        self.session.store_preset("夜间", make_config())
        self.prompter = RecordingPrompter()          # 队列空 → 答「取消」
        session = GuiSession(self.root / "settings.txt", self.prompter)
        self.assertFalse(session.delete_preset("夜间"))
        self.assertIn("夜间", session.list_presets())   # 还在
        self.assertEqual(len(self.prompter.asked), 1)

    def test_delete_removes_when_confirmed(self) -> None:
        self.session.store_preset("夜间", make_config())
        session = GuiSession(self.root / "settings.txt", RecordingPrompter(answers=[True]))
        self.assertTrue(session.delete_preset("夜间"))
        self.assertNotIn("夜间", session.list_presets())
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_session -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_core.session'`

- [ ] **Step 3: 实现 `session.py` 的预设部分**

新建 `rhodes_fast/gui_core/session.py`。骨架如下，四个方法的**方法体**照 `gui.py:1096-1258` 的原逻辑搬，`messagebox` 按上面的规则替换成 `self.prompter`。**中文文案一字不改**——用户看到的弹窗内容必须一样。

```python
"""界面的业务动作: 预设、算法库、子进程。不碰控件, 不碰 tkinter。

要问用户的地方一律走 self.prompter (见 prompts.py), 要往运行状态里写字的
地方一律走回调 —— 怎么显示是界面的事。
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig
from ..presets import Preset
from .prompts import Prompter


class GuiSession:
    def __init__(self, config_path: Path, prompter: Prompter) -> None:
        self.config_path = config_path
        self.prompter = prompter          # 公开: 测试要读它的记录
        self.base_dir = config_path.parent
        self.presets_dir = self.base_dir / "presets"
        self.algorithms_dir = self.base_dir / "algorithms"
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None

    # ---- 预设 (Task 6) ----
    def list_presets(self) -> list[str]: ...
    def load_preset(self, name: str) -> Preset | None: ...
    def store_preset(self, name: str, config: AppConfig) -> bool: ...
    def delete_preset(self, name: str) -> bool: ...
```

`presets_dir` / `algorithms_dir` 的实际取法照 `gui.py` 里现在的算法（`RhodesFastGui.__init__` 里怎么定的就怎么定），别自己发明路径。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_session -v
```

Expected: `Ran 4 tests` / `OK`

- [ ] **Step 5: 旧 GUI 接线**

在 `gui.py` 里加一个 `TkPrompter` 类（实现 `Prompter`，内部调 `messagebox` / `simpledialog`，一律带 `parent=self.root`），`RhodesFastGui.__init__` 里建 `self.prompter = TkPrompter(self.root)` 和 `self.session = GuiSession(self.config_path, self.prompter)`。上面六个方法改成调 `self.session.*`，控件相关的部分（刷新下拉框、更新预设标记、写运行状态）留在原地。

```python
class TkPrompter:
    """Prompter 的 tkinter 实现。文案由调用方给, 这里只负责用哪个弹窗。"""

    def __init__(self, root: tk.Misc) -> None:
        self._root = root

    def notify_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self._root)

    def notify_warning(self, title: str, message: str) -> None:
        messagebox.showwarning(title, message, parent=self._root)

    def notify_info(self, title: str, message: str) -> None:
        messagebox.showinfo(title, message, parent=self._root)

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        extra = {"icon": messagebox.WARNING, "default": messagebox.CANCEL} if danger else {}
        return bool(messagebox.askokcancel(title, message, parent=self._root, **extra))

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        return messagebox.askyesnocancel(title, message, parent=self._root)

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        return simpledialog.askstring(title, message, initialvalue=initial, parent=self._root)
```

> **搬 `_save_preset` 的「覆盖预设」和 `_delete_preset` 的「删除预设」时，`confirm` 必须传 `danger=True`。** 漏了的话警告图标和「默认焦点在取消」都会丢，后者是代码里写了注释的防手滑设计（`gui.py:1244`），丢了就是用户能察觉的安全性回退。

- [ ] **Step 6: 跑全量测试并提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | Select-String -Pattern "^Ran |^OK|^FAILED"
```

Expected: `Ran 576 tests` / `OK`

```bash
git add rhodes_fast/gui_core/session.py rhodes_fast/gui_core/__init__.py rhodes_fast/gui.py tests/test_gui_core_session.py
git commit -F - <<'MSG'
refactor: move preset operations into GuiSession

Preset load, save and delete no longer reach for messagebox; they ask a
Prompter, so the same code can run behind a browser dialog. The dialog
wording is copied across unchanged -- users must see the same strings.

Test suite: 576 tests, OK.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 7: GuiSession — 算法库操作

**Files:**
- Modify: `rhodes_fast/gui_core/session.py`、`rhodes_fast/gui.py:726-949`
- Test: `tests/test_gui_core_session.py`

**Interfaces:**
- Consumes: `GuiSession`（Task 6）、`Prompter`
- Produces: `GuiSession` 新增：
  - `.library_rows() -> list[tuple[str, str, str, str, str, str]]`（显示名 / 内部名 / 作者 / 源文件 / 导入时间 / 状态）
  - `.import_algorithm(source: Path) -> bool`
  - `.rename_algorithm(name: str, new_display_name: str) -> bool`
  - `.delete_algorithm(name: str) -> bool`
  - `.algorithm_source(name: str) -> str | None`

搬 `gui.py` 的 `_reload_algorithm_library`（726）、`_library_rows`（754）、`_import_algorithm`（803）、`_confirm_import`（822）、`_install_candidate`（845）、`_show_algorithm_source`（872）、`_rename_algorithm`（900）、`_delete_algorithm`（927）。

**必须保留的语义**（`gui.py:755-760` 的注释）：列表读的是**注册表**不是已加载的算法表。刚导入的算法要重启才会进 `available_algorithms()`，但用户点完导入就得在列表里看见它，否则会以为坏了。

文件选择（`filedialog.askopenfilename`）**留在 `gui.py`**——它是界面的事，`GuiSession.import_algorithm` 收一个已经选好的 `Path`。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_core_session.py`）**

```python
class AlgorithmLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)

    def _session(self, answers=None) -> GuiSession:
        return GuiSession(self.root / "settings.txt", RecordingPrompter(answers=answers))

    def test_builtins_show_up_as_six_column_rows(self) -> None:
        rows = self._session().library_rows()
        self.assertTrue(rows)
        self.assertTrue(all(len(row) == 6 for row in rows))
        self.assertTrue(any(row[5] == "内置" for row in rows))

    def test_imported_algorithm_appears_before_a_restart(self) -> None:
        """gui.py:755 —— 列表读注册表, 不读已加载那份。点完导入就得看见,
        否则用户以为导入失败了。"""
        source = self.root / "my_aim.py"
        source.write_text(ALGORITHM_SOURCE, encoding="utf-8")
        session = self._session(answers=[True])
        self.assertTrue(session.import_algorithm(source))
        self.assertTrue(any(row[5] == "已导入" for row in session.library_rows()))

    def test_delete_asks_first(self) -> None:
        source = self.root / "my_aim.py"
        source.write_text(ALGORITHM_SOURCE, encoding="utf-8")
        session = self._session(answers=[True])
        session.import_algorithm(source)

        refusing = GuiSession(self.root / "settings.txt", RecordingPrompter())
        self.assertFalse(refusing.delete_algorithm("my_aim"))
        self.assertTrue(any(row[1] == "my_aim" for row in refusing.library_rows()))

    def test_builtin_cannot_be_deleted(self) -> None:
        prompter = RecordingPrompter(answers=[True])
        session = GuiSession(self.root / "settings.txt", prompter)
        self.assertFalse(session.delete_algorithm("p"))
        self.assertEqual(len(prompter.errors), 1)
```

> `ALGORITHM_SOURCE` 是测试文件里的一个最小合法算法源码字符串，照 `rhodes_fast/aim_algorithms/contract.py` 的契约写（`NAME`、`DISPLAY_NAME`、`PARAMS`、`compute`）。参照现有的 `tests/test_algorithm_library.py` 里已有的样例，直接复用它的常量而不是重写一份。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_session -v
```

Expected: `AttributeError: 'GuiSession' object has no attribute 'library_rows'`

- [ ] **Step 3: 实现**

把上述八个方法的非控件部分搬进 `GuiSession`。`_show_algorithm_source` 拆成两半：读源码进 `GuiSession.algorithm_source()`，弹窗显示（`gui.py:885` 的 `_show_source_window`）留在 `gui.py`。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_session -v
```

Expected: `Ran 8 tests` / `OK`

- [ ] **Step 5: 旧 GUI 接线、跑全量、提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | Select-String -Pattern "^Ran |^OK|^FAILED"
```

Expected: `Ran 580 tests` / `OK`

```bash
git add rhodes_fast/gui_core/session.py rhodes_fast/gui.py tests/test_gui_core_session.py
git commit -F - <<'MSG'
refactor: move algorithm library operations into GuiSession

Import, rename, delete and source reading now live off the widgets. The
file picker stays in gui.py -- picking a file is the UI's job; the session
takes a path that has already been chosen.

The listing still reads the registry rather than the loaded algorithms, so
a freshly imported algorithm shows up before the restart that actually
loads it. Reading the loaded table instead would leave users staring at an
unchanged list after a successful import.

Test suite: 580 tests, OK.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 8: GuiSession — 子进程启停与日志

**Files:**
- Modify: `rhodes_fast/gui_core/session.py`、`rhodes_fast/gui.py:1537-1670`
- Test: `tests/test_gui_core_session.py`

**Interfaces:**
- Consumes: `GuiSession`（Task 6-7）
- Produces: `GuiSession` 新增：
  - `.build_command(*, config_path, stop_file, runtime_aim_file, preview_port=None, preview_enable_file=None, trail_settings_file=None, latency_log=None, extra=()) -> list[str]`（纯函数）
  - `.start(command, *, cwd, on_line, on_exit) -> bool`
  - `.stop() -> None`
  - `.is_running -> bool`

**先读 `gui.py:1540-1629` 再动手。** `_launch` 比名字看起来重得多，它把五件事揉在一起：

| 行 | 干什么 | 归谁 |
|---|---|---|
| 1541-1545 | 已在运行则拒绝；先 `self._save(quiet=True)` | 留 `gui.py`（要 `_read_form`） |
| 1546-1563 | 组 argv 的固定部分 | → `build_command` |
| 1564-1571 | **建预览 UDP socket**，端口写进 argv | 留 `gui.py`（见下） |
| 1572-1581 | 写轨迹设置文件、按页签 touch 预览开关文件、延迟日志 | 留 `gui.py`（碰控件状态）；`--latency-log` 的路径由 `gui.py` 算好传进 `build_command` |
| 1583-1623 | `Popen` + 两个线程 + 清空日志框 + `_set_running` | `Popen`/读线程 → `start()`；其余留 `gui.py` |

**预览 socket 不搬**（`_receive_preview` 1631、`_drain_preview` 1654）。它在 `_launch` 内部创建、端口号直接进 argv，而 WebView 那边要把这些帧转成 MJPEG，形态完全不同。现在抽一个共用抽象只能靠猜。留到第二份计划，那时才有真正的第二个消费者可以对着设计。所以 `build_command` 收一个**已经绑好的端口号**，socket 本身仍由 `gui.py` 创建和持有。

`_drain_messages`（1670）用 `root.after` 轮询队列，是 tk 的事件循环机制，留在 `gui.py`；`GuiSession` 只把行喂进 `on_line` 回调。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_core_session.py`）**

测试文件顶部补上 `import sys`、`import threading`。

```python
class BuildCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.session = GuiSession(self.root / "settings.txt", RecordingPrompter())
        self.addCleanup(self._dir.cleanup)

    def _base(self, **overrides):
        kwargs = dict(
            config_path=self.root / "settings.txt",
            stop_file=self.root / "stop",
            runtime_aim_file=self.root / "aim",
        )
        kwargs.update(overrides)
        return self.session.build_command(**kwargs)

    def test_always_runs_the_package_unbuffered(self) -> None:
        """-u 不能掉: 掉了子进程的输出会卡在缓冲区里, 运行状态就一片空白。"""
        command = self._base()
        self.assertEqual(command[1:4], ["-u", "-m", "rhodes_fast"])
        self.assertIn(str(self.root / "settings.txt"), command)

    def test_no_preview_flags_when_no_port_given(self) -> None:
        """跑基准测试时不开预览 —— 对应 gui.py:1565 的 `if not arguments`。"""
        command = self._base()
        self.assertNotIn("--preview-port", command)
        self.assertNotIn("--preview-enable-file", command)

    def test_preview_flags_appear_together(self) -> None:
        command = self._base(
            preview_port=54321,
            preview_enable_file=self.root / "preview",
            trail_settings_file=self.root / "trail",
        )
        self.assertIn("--preview-port", command)
        self.assertEqual(command[command.index("--preview-port") + 1], "54321")
        self.assertIn("--preview-enable-file", command)
        self.assertIn("--trail-settings-file", command)

    def test_latency_log_only_when_a_path_is_given(self) -> None:
        self.assertNotIn("--latency-log", self._base())
        command = self._base(latency_log=self.root / "latency-x.csv")
        self.assertEqual(command[command.index("--latency-log") + 1], str(self.root / "latency-x.csv"))

    def test_extra_arguments_go_last(self) -> None:
        """基准测试那条路会追加自己的参数 —— 对应 gui.py:1582。"""
        command = self._base(extra=["--benchmark", "30"])
        self.assertEqual(command[-2:], ["--benchmark", "30"])


class SubprocessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.session = GuiSession(self.root / "settings.txt", RecordingPrompter())
        self.addCleanup(self._dir.cleanup)

    def test_lines_reach_the_callback_and_exit_is_reported(self) -> None:
        lines: list[str] = []
        exits: list[int] = []
        done = threading.Event()
        started = self.session.start(
            [sys.executable, "-u", "-c", "print('准备就绪。')"],
            cwd=self.root,
            on_line=lines.append,
            on_exit=lambda code: (exits.append(code), done.set()),
        )
        self.assertTrue(started)
        self.assertTrue(done.wait(timeout=10))
        self.assertIn("准备就绪。", [line.strip() for line in lines])
        self.assertEqual(exits, [0])
        self.assertFalse(self.session.is_running)

    def test_starting_twice_is_refused(self) -> None:
        self.session.start(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.root, on_line=lambda _: None, on_exit=lambda _: None,
        )
        self.addCleanup(self.session.stop)
        self.assertFalse(
            self.session.start(
                [sys.executable, "-c", "pass"],
                cwd=self.root, on_line=lambda _: None, on_exit=lambda _: None,
            )
        )

    def test_failure_to_spawn_reports_an_error_and_leaves_nothing_running(self) -> None:
        prompter = RecordingPrompter()
        session = GuiSession(self.root / "settings.txt", prompter)
        self.assertFalse(
            session.start(
                [str(self.root / "没有这个程序.exe")],
                cwd=self.root, on_line=lambda _: None, on_exit=lambda _: None,
            )
        )
        self.assertEqual(len(prompter.errors), 1)
        self.assertFalse(session.is_running)
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_session -v
```

Expected: `AttributeError: 'GuiSession' object has no attribute 'build_command'`

- [ ] **Step 3: 实现**

在 `GuiSession` 追加。`build_command` 逐行对照 `gui.py:1546-1582`：

```python
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
        """组子进程的命令行。对应 gui.py:1546-1582。

        -u 不能掉: 子进程的输出要实时流进运行状态框, 有缓冲就成了一坨一坨地出。
        预览相关的三个开关要么一起给要么一起不给 —— 基准测试那条路不开预览。
        """
        python = Path(sys.executable).with_name("python.exe")
        command = [
            str(python), "-u", "-m", "rhodes_fast",
            "--config", str(config_path),
            "--stop-file", str(stop_file),
            "--runtime-aim-file", str(runtime_aim_file),
        ]
        if preview_port is not None:
            command += ["--preview-port", str(preview_port)]
            if preview_enable_file is not None:
                command += ["--preview-enable-file", str(preview_enable_file)]
            if trail_settings_file is not None:
                command += ["--trail-settings-file", str(trail_settings_file)]
            if latency_log is not None:
                command += ["--latency-log", str(latency_log)]
        command.extend(extra)
        return command

    @property
    def is_running(self) -> bool:
        return self._process is not None

    def start(
        self,
        command: list[str],
        *,
        cwd: Path,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> bool:
        """起子进程并把它的输出喂给 on_line。对应 gui.py:1583-1597, 1625-1629。

        PYTHONUTF8=1 和 errors="replace" 都不能掉: 中文日志在 GBK 控制台上会炸,
        而一行解不出来的字节不该让整个读取线程死掉。
        """
        if self._process is not None:
            return False
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
                env=environment,
            )
        except Exception as exc:
            self.prompter.notify_error("无法启动", str(exc))
            return False
        self._process = process

        def _pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                on_line(line.rstrip())
            code = process.wait()
            self._process = None
            on_exit(code)

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()
        return True

    def stop(self) -> None:
        """照 gui.py 的 _stop 搬: 先写停止文件让子进程自己收尾, 超时才硬杀。"""
```

> `stop()` 的方法体照 `gui.py` 现有的 `_stop` 搬（用 `grep -n "_stop" rhodes_fast/gui.py` 找）。**不要改成直接 `kill()`**——现在是先写停止文件让管线自己收尾（关 KMBox、落盘延迟日志），硬杀会丢这些。
>
> `session.py` 顶部补 `import os`、`import sys`，`from collections.abc import Sequence`。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_session -v
```

Expected: `Ran 16 tests` / `OK`

- [ ] **Step 5: 旧 GUI 接线、跑全量、提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | Select-String -Pattern "^Ran |^OK|^FAILED"
```

Expected: `Ran 588 tests` / `OK`

```bash
git add rhodes_fast/gui_core/session.py rhodes_fast/gui.py tests/test_gui_core_session.py
git commit -F - <<'MSG'
refactor: move subprocess start/stop and log collection into GuiSession

build_command is pure, so the argv the GUI hands the inference process is
finally testable on its own -- including that -u never gets dropped, which
is what keeps the subprocess's output streaming into the status box instead
of arriving in clumps. Output lines reach a callback; the tk event-loop
polling that drains them stays in gui.py, because root.after is tkinter's.

The preview socket deliberately did not move. The WebView UI will relay
those frames into an MJPEG stream rather than decode them, so a shared
abstraction now would be guesswork -- it gets designed in the second plan,
against a real second consumer.

Test suite: 588 tests, OK.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 9: 验收与文档

**Files:**
- Modify: `docs/superpowers/specs/2026-09-17-webview-ui-design.md`（阶段 1 打勾并记实际测试数）
- Modify: `README.md`（如果它列了模块结构，补上 `gui_core/`）

**Interfaces:**
- Consumes: 全部
- Produces: 无代码

- [ ] **Step 1: 确认 `gui_core` 真的不依赖 tk**

```bash
.venv/Scripts/python.exe -c "import sys, rhodes_fast.gui_core; print([n for n in sys.modules if n.startswith('tkinter')])"
```

Expected: `[]`

- [ ] **Step 2: 全量测试**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | Select-String -Pattern "^Ran |^OK|^FAILED"
```

Expected: `Ran 588 tests` / `OK`

- [ ] **Step 3: 手动跑一遍完整流程**

```bash
.venv/Scripts/python.exe -m rhodes_fast.gui
```

在 Task 5 Step 5 那七项之外，再确认：

8. 保存预设 / 另存为 / 删除预设 → 弹窗文案跟改动前一字不差
9. 导入一个算法 → 列表立刻出现（不重启）
10. 改名 / 删除算法 → 确认弹窗出现，取消时什么都不变
11. 点「启动」→ 子进程起来、运行状态里有日志、点「停止」能停

- [ ] **Step 4: 更新文档并提交**

```bash
git add docs/superpowers/specs/2026-09-17-webview-ui-design.md README.md
git commit -F - <<'MSG'
docs: record that the gui_core extraction is done

Phase 1 of the WebView design is complete: the tkinter GUI now drives the
same toolkit-free core the WebView UI will, and it behaves exactly as it
did before.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## 明确不做

- **不搬预览 socket。** 形态会变（MJPEG），留给第二份计划按真实需要设计
- **不改任何界面外观。** 本计划结束时旧界面看起来和用起来跟开始时一模一样
- **不加 `pywebview` 依赖**，不建 `gui_web/`
- **不重排 `gui.py` 里的控件代码。** 只改它怎么取值和怎么问用户
- **不动 `rhodes_fast/config.py`、`presets.py`、`algorithm_library.py`、`tuning_share.py` 的公开接口**
