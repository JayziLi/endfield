# WebView 三个表单屏实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 WebView 界面能改设置——三个业务表单屏、预设条、算法库和模态对话框全部接上，做完之后新界面能独立完成「配置 → 启动 → 出画面 → 停止」。

**Architecture:** 表单的真相在 Python 侧的一个 `FormBridge` 里（就是 tkinter 那边 tk 变量的角色），JS 只是它的视图：控件一动就 `api.set_field(path, value)` 推上来，要整片换（载入预设、切算法）时由 Python `evaluate_js` 把整份状态推回去。业务动作一律转给已有的 `gui_core.GuiSession`，这一份计划不新写任何业务逻辑。对话框用页面内的模态框，靠「Python 阻塞等 JS 回答」的桥满足 `Prompter` 的同步语义。

**Tech Stack:** pywebview 5.4 (EdgeChromium)、vanilla JS（无框架、无构建）、Endfield UI 设计系统、Python 标准库 unittest。

## Global Constraints

- **`rhodes_fast/gui_web/web/design/` 一个字节不改。** 要覆盖样式写在 `app.css` 里，它在 `design/styles.css` 之后加载。
- **`gui_web` 不许拖进 tkinter。** 验收命令：`.venv/Scripts/python.exe -c "import sys, rhodes_fast.gui_web.app; print([n for n in sys.modules if n.startswith('tkinter')])"` → `[]`。
- **`import webview` 只能出现在函数体里**，不能在模块顶层（`tests/test_gui_web_app.py` 的 `ImportSurfaceTest` 在盯）。
- **信号黄纪律：内容区同时只有一个黄块**，即启动/停止按钮。NavRail 当前项的黄属于外壳，另算。其余按钮一律 `ef-btn--outline` / `ef-btn--secondary`。
- **不许自己发明 `ef-` class**，除非设计系统真的没有这个组件（表格、Modal 是明确的两个缺口）；自拼的一律写在 `app.css` 里，`tests/test_gui_web_shell.py` 的 `test_it_uses_the_design_systems_classes_not_invented_ones` 在盯。
- **`Api` 的公开方法就是 JS 的调用面**（pywebview 把每个不以 `_` 开头的方法都挂上去）。只给 Python 用的一律加下划线。加公开方法时 `test_only_the_intended_methods_are_visible_to_js` 会红，那正是该停一秒的地方。
- **页面里任何 `[hidden]` 的元素，只要它的 class 被设过 `display`，`app.css` 就必须把 `display: none` 写回来**（浏览器默认样式权重最低）。`test_every_hidden_element_keeps_its_display_none` 会扫，包括 JS 运行时才切的那些。
- **不许把 `src` 写进预览的 `<img>`。** `/preview.mjpg` 是一条不会结束的流，写在标记里页面的 `load` 事件就永远不触发，pywebview 会认为窗口没起来，之后每一次 `evaluate_js` 都抛异常。这条已经踩过，见 `de838f5`。**新加的任何长连接资源同理。**
- **KMBox UUID 明文显示**，不做遮罩（用户明确决定，与旧界面一致）。**但任何测试的断言失败信息里不许出现它**——需要比对时用 `assertTrue(a == b, "...")`，不要 `assertEqual`。
- **旧界面 `endfield-gui` 的行为不许变。** 唯一碰它的是 Task 1，而且先写特征测试钉住现状。
- **不切换默认入口。** 本计划结束时 `endfield-gui` 仍是 tkinter，`endfield-gui-next` 是 WebView。切换留给用户验收之后。
- 全量测试基线 **783 通过**（`.venv/Scripts/python.exe -m unittest discover -s tests`），每个任务结束时必须全绿。
- 提交信息用英文标题 + 中文正文，结尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。**不许 push**，这个分支从来没推过。
- **不许碰 `docs/projectile-prediction-research.zh-CN.md`**（用户自己的未暂存删除）。

### 做完之后 JS 能调的方法（`test_only_the_intended_methods_are_visible_to_js` 的终态）

几乎每个任务都会往这里加一两个，所以每次都要改那条测试。全做完是这 25 个，字母序：

```
answer_prompt, browse_model, close, delete_algorithm, delete_preset,
import_algorithm, is_running, minimize, refresh_library, refresh_presets,
rename_algorithm, run_benchmark, run_check, run_pipeline_benchmark,
save_preset, save_preset_as, save_settings, select_preset, set_algorithm,
set_field, set_preview_active, show_algorithm_source, start, stop,
toggle_maximize
```

**这个列表之外的一律加下划线。** JS 那边传进来的参数是 JSON 对象，一次误调就能把 `session` 换成一个 dict——实测过一次公开的 `attach`，三个窗口按钮当场全哑。

---

## 已经实测确认的三件事（别再验一遍，也别假设相反）

1. **pywebview 的 js_api 方法各跑在自己的线程上**（实测线程名 `Thread-27 (_call)`）。阻塞其中一个 1 秒，JS 侧的 `setInterval` 照跑 59/60 次（没冻），而且**阻塞期间第二个 api 调用照样被处理**。所以 `Prompter` 的同步语义可以用「阻塞等 JS 回答」实现。
2. **MJPEG 穿过 WebView2 能跑满 30fps**：推送 30.10 fps / 交付 30.10 fps，一帧不丢，无回压。
3. **Chromium 对 `multipart/x-mixed-replace` 的 `load` 事件不可靠**（`complete` 早早变 true）。要知道画面来没来，问 `naturalWidth`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `rhodes_fast/gui_core/state.py`（改） | 补三个纯函数：`algorithm_choices()`、`algorithm_param_specs()`、`runtime_aim_payload()`。前两个现在在 `gui.py` 里，而那个模块 import tkinter |
| `rhodes_fast/gui.py`（改） | 改成从 `gui_core` 取上面三个，删掉本地副本。**只有 Task 1 碰它** |
| `rhodes_fast/gui_web/forms.py`（新） | `FormBridge`：按路径改字段、按声明类型做转换、产出 JSON 负载。纯 Python，不碰 webview 不碰文件 |
| `rhodes_fast/gui_web/prompts.py`（改） | `LogOnlyPrompter` → `ModalPrompter`：阻塞等 JS 回答 |
| `rhodes_fast/gui_web/app.py`（改） | 把新的 api 方法接上 `FormBridge` / `GuiSession` / `ModalPrompter` |
| `rhodes_fast/gui_web/web/index.html`（改） | 三个屏的标记、预设条、动作行、04 控制栏、模态框的壳 |
| `rhodes_fast/gui_web/web/forms.js`（新） | 从状态渲染控件、绑定变化、动态生成算法参数 |
| `rhodes_fast/gui_web/web/modal.js`（新） | 模态框组件（设计系统没有 Dialog，用它的 token 自拼） |
| `rhodes_fast/gui_web/web/library.js`（新） | 算法库表格（设计系统没有 Table） |
| `rhodes_fast/gui_web/web/app.css`（改） | 上面几样的样式 |

按职责切，不按技术层切：`forms.py` 只管「表单现在是什么」，`prompts.py` 只管「怎么问用户」，三个 JS 文件各对着一个设计系统没给的东西。`app.js` 保持现在的职责（外壳、启停、预览、telemetry），不往里塞表单。

**新加的 JS 文件都要在 `index.html` 里点名**，而且要排在 `app.js` 之前（`app.js` 末尾会调用它们的初始化）。`pyproject.toml` 的 `package-data` 已经收 `*.js`，不用改。

---

## Task 1: 把三个纯函数搬进 `gui_core`

**Files:**
- Modify: `rhodes_fast/gui_core/state.py`
- Modify: `rhodes_fast/gui.py:84-95`（删两个函数）、`rhodes_fast/gui.py:1787-1826`（`_write_runtime_aim_settings`）
- Test: `tests/test_gui_core_state.py`（已存在，追加）

**Interfaces:**
- Consumes: 无（是这份计划的第一步）
- Produces:
  - `algorithm_choices() -> dict[str, str]` —— `{显示名: 算法标识}`
  - `algorithm_param_specs(name: str) -> tuple[Param, ...]` —— 按**标识**取，算法不存在时返回 `()`
  - `runtime_aim_payload(state: FormState, labels: Labels) -> dict` —— 热推给管线的那份 JSON 的内容（不含写文件）

`gui_web` 要用前两个来画下拉框和动态参数，但它们现在住在 `gui.py` 里，而那个模块顶上就 `import tkinter`。第三个是 `gui.py:1787` 那段搬过来的，搬完两边共用一份——留两份副本的话，将来给算法加一个字段只改一边，症状是「新界面调参数不生效」。

- [ ] **Step 1: 先写特征测试，钉住 `runtime_aim_payload` 现在产出什么**

先打开 `tests/test_gui_core_state.py`，确认里面已有的 helper 叫什么（造 `FormState` 和 `Labels` 的那两个）。下面用 `_labels()` 和 `make_config()` 指代它们，**名字不一样就用文件里现有的**，不要新造一份。

追加：

```python
class RuntimeAimPayloadTest(unittest.TestCase):
    """热推给管线的那份 JSON。

    这是特征测试: 它不判断「应该」是什么, 只钉住 gui.py:1787 现在产出的东西,
    好让搬家这一步是可证的。管线那边按键名读, 改任何一个键名都是协议变更。
    """

    def _state(self) -> FormState:
        base = config_to_form_state(default_config(), Path("."), _labels())
        first = replace(
            base.profiles[0],
            enabled=True, trigger="鼠标侧键 1", target_class="0",
            aim_position=38.0, fov=90.0, kp_min=0.02, kp_max=0.12,
            kp_growth=0.25, algorithm="比例控制", algorithm_params={"kp": 0.08},
        )
        second = replace(base.profiles[1], enabled=False, trigger="鼠标右键")
        return replace(base, profiles=(first, second))

    def test_the_payload_keeps_the_keys_the_pipeline_reads(self) -> None:
        payload = runtime_aim_payload(self._state(), _labels())
        self.assertEqual(
            sorted(payload), ["fov_radius", "profiles", "target_class", "target_y_ratio"]
        )
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertEqual(
            sorted(payload["profiles"][0]),
            ["algorithm", "algorithm_params", "enabled", "fov_radius", "kp_growth",
             "kp_max", "kp_min", "target_class", "target_y_ratio", "trigger"],
        )

    def test_the_top_level_keys_mirror_profile_one(self) -> None:
        """gui.py:1814 的注释: Top-level keys mirror profile 1 for older runtime
        consumers。老的读取方只看顶层, 去掉就等于悄悄砍了向后兼容。"""
        payload = runtime_aim_payload(self._state(), _labels())
        for key in ("target_class", "target_y_ratio", "fov_radius"):
            self.assertEqual(payload[key], payload["profiles"][0][key])

    def test_aim_position_is_a_percentage_on_the_form_and_a_ratio_in_the_payload(self) -> None:
        """表单上是 0-100 的百分比, 协议里是 0-1 的比例。漏了这一步准心会瞄到
        框外面去 —— 而且 38 和 0.38 都是「看着挺合理」的数, 没人会怀疑。"""
        payload = runtime_aim_payload(self._state(), _labels())
        self.assertAlmostEqual(payload["profiles"][0]["target_y_ratio"], 0.38)

    def test_the_ratio_is_clamped_to_zero_one(self) -> None:
        state = self._state()
        state = replace(state, profiles=(replace(state.profiles[0], aim_position=140.0),
                                         state.profiles[1]))
        self.assertEqual(
            runtime_aim_payload(state, _labels())["profiles"][0]["target_y_ratio"], 1.0
        )

    def test_labels_are_translated_to_stored_values(self) -> None:
        """界面上存的是显示标签 (「鼠标侧键 1」), 协议里要的是标识 (side1)。
        直接把标签发过去的话管线一个触发键都认不出来, 而且不报错 —— 只是永远
        不开火。"""
        payload = runtime_aim_payload(self._state(), _labels())
        self.assertEqual(payload["profiles"][0]["trigger"], "side1")
        self.assertEqual(payload["profiles"][0]["algorithm"], "p")

    def test_target_class_is_an_int_not_the_string_from_the_dropdown(self) -> None:
        payload = runtime_aim_payload(self._state(), _labels())
        self.assertIsInstance(payload["profiles"][0]["target_class"], int)

    def test_the_payload_is_json_serialisable(self) -> None:
        """它是要 json.dumps 进文件的。混进一个 tuple 或 Path 就当场炸在热推
        那条路上, 而那条路每动一下滑条走一次。"""
        json.dumps(runtime_aim_payload(self._state(), _labels()))
```

文件顶部补 `import json`、`from dataclasses import replace`，以及从 `rhodes_fast.gui_core.state` 导入 `runtime_aim_payload`。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `ImportError: cannot import name 'runtime_aim_payload' from 'rhodes_fast.gui_core.state'`

- [ ] **Step 3: 实现三个函数**

在 `rhodes_fast/gui_core/state.py` 里加（放在 `_param_defaults` 旁边，它已经在读同一份注册表）：

```python
def algorithm_choices() -> dict[str, str]:
    """界面显示名 -> 算法标识。和 Labels 里那几张映射同一个方向。

    每次现取, 不缓存: 用户从算法库导入一个 .py 之后注册表就变了, 缓存住的话
    新算法要重启才看得见。
    """
    return {
        algorithm.DISPLAY_NAME: name
        for name, algorithm in sorted(available_algorithms().items())
    }


def algorithm_param_specs(name: str) -> tuple[Param, ...]:
    """按算法**标识**取参数契约, 不是按显示标签。

    配置里指着一个已删掉的算法时界面仍要画得出来, 所以不抛异常, 返回空元组。
    """
    algorithm = available_algorithms().get(name)
    return algorithm.PARAMS if algorithm is not None else ()


def runtime_aim_payload(state: FormState, labels: Labels) -> dict:
    """热推给管线的那份设置。纯函数: 不碰文件, 不看进程在不在跑。

    从 gui.py:1787 搬过来的。两边共用一份 —— 留两份副本的话, 将来给算法加一个
    字段只改一边, 症状是「新界面调参数不生效」, 而且不报任何错。
    """
    profiles = [
        {
            "enabled": profile.enabled,
            "trigger": labels.trigger[profile.trigger],
            "kp_min": profile.kp_min,
            "kp_max": profile.kp_max,
            "kp_growth": profile.kp_growth,
            "target_class": int(profile.target_class),
            # 表单上是 0-100 的百分比, 协议里是 0-1 的比例。漏了这一步准心会瞄到
            # 框外面去, 而 38 和 0.38 都是「看着挺合理」的数。
            "target_y_ratio": max(0.0, min(1.0, profile.aim_position / 100.0)),
            "fov_radius": profile.fov,
            "algorithm": labels.algorithm[profile.algorithm],
            "algorithm_params": dict(profile.algorithm_params),
        }
        for profile in state.profiles
    ]
    return {
        # 顶层这三个是给老的读取方看的 (gui.py:1814 的注释: Top-level keys mirror
        # profile 1 for older runtime consumers)。去掉就等于悄悄砍了向后兼容,
        # 而且只有老版本管线才看得出来。
        "target_class": profiles[0]["target_class"],
        "target_y_ratio": profiles[0]["target_y_ratio"],
        "fov_radius": profiles[0]["fov_radius"],
        "profiles": profiles,
    }
```

顶部 import 补 `Param`。先确认它的真实位置：

```bash
grep -rn "class Param" rhodes_fast/aim_algorithms/
```

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_core_state -v
```

Expected: `OK`

- [ ] **Step 5: 让 `gui.py` 改用它们**

删掉 `gui.py:84-95` 那两个函数定义，改成从 `gui_core.state` 导入（`gui.py` 已经从那里导入别的东西了，加进同一条 import）。

`_write_runtime_aim_settings` 整个换成：

```python
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
            # 置信度框打到一半也会跳过这一次热推。后果一样 —— 下一次编辑补上。
            return
        values = runtime_aim_payload(state, self._labels())
        self.runtime_aim_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_aim_file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(values), encoding="utf-8")
            temporary.replace(self.runtime_aim_file)
        finally:
            temporary.unlink(missing_ok=True)
```

- [ ] **Step 6: 全量测试**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"
```

Expected: `OK`，数量 = 783 + 新增。**旧界面的测试一条都不许红**——红了说明搬家改变了行为，回头看 Step 5 注释里写的那点差别。

- [ ] **Step 7: 变异验证**

三条，每条实际改一次代码、跑一次、改回去：

1. `profile.aim_position / 100.0` → `profile.aim_position`：`test_aim_position_is_a_percentage...` 必须红
2. `labels.trigger[profile.trigger]` → `profile.trigger`：`test_labels_are_translated_to_stored_values` 必须红
3. 删掉顶层那三个键：`test_the_top_level_keys_mirror_profile_one` 必须红

- [ ] **Step 8: 提交**

```bash
git add rhodes_fast/gui_core/state.py rhodes_fast/gui.py tests/test_gui_core_state.py
```

提交信息（标题 `refactor: move the algorithm lookups and the runtime aim payload into gui_core`）正文要说清楚：两个函数原来住在 import tkinter 的模块里而 WebView 那条路不能碰；payload 留两份副本会怎么坏；以及 Step 5 注释里那点行为差别。

---

## Task 2: `FormBridge` —— 表单的真相

**Files:**
- Create: `rhodes_fast/gui_web/forms.py`
- Test: `tests/test_gui_web_forms.py`（新建）

**Interfaces:**
- Consumes: `FormState` / `ProfileFormState` / `Labels` / `config_to_form_state` / `form_state_to_config` / `algorithm_choices` / `algorithm_param_specs`（`rhodes_fast.gui_core.state`）
- Produces:
  - `class FormBridge`，构造 `FormBridge(state: FormState)`
  - `bridge.state -> FormState`（当前值）
  - `bridge.set_field(path: str, value) -> None`
  - `bridge.replace_state(state: FormState) -> None`
  - `bridge.as_payload() -> dict`（整份表单，JSON 可序列化）
  - `UnknownField(KeyError)`
  - 模块函数 `choice_payload(labels: Labels) -> dict[str, list[str]]`
  - 模块函数 `param_payload(algorithm_label: str, labels: Labels) -> list[dict]`

### 为什么真相在 Python 侧

两边都能当真相。选 Python 是因为：热推（`runtime_aim_payload`）每动一下滑条就要问一次「现在整张表单是什么」，保存、另存为预设、启动组命令行也都要；真相在 JS 的话这些全要走一次异步往返，而 `Prompter` 那条路还是同步的。JS 只是视图：控件一动 `set_field` 推上来，要整片换时 Python 把整份状态推回去。这跟 tkinter 那边 tk 变量的角色一模一样。

### 路径的形状

```
"model_path"                               -> FormState 的顶层字段
"profiles.0.kp_min"                        -> 第 1 套方案的字段
"profiles.1.algorithm_params.wind_strength"-> 第 2 套方案的某个算法参数
```

`profiles` 只有 0 和 1 两个下标（`FormState.profiles` 是个两元组）。

### 类型转换

`FormState` 的字段类型是**声明出来的**，转换按声明走。这一条不是洁癖：

- `udp_port` / `udp_width` / `kmbox_port` 在 `FormState` 里是 **`str`**，故意的——表单留着用户打的原文，非法输入要走到「设置无法保存」那个弹窗，提前转 `int` 就变成界面直接崩。JS 的 `<input type=number>` 送上来是数字，不转成 `str` 的话 `form_state_to_config` 那边的解析路径就绕过去了。
- `confidence` / `kp_min` 这些是 `float`，JS 送上来可能是整数 `1`，不转的话 `f"{value:.3f}"` 还是能跑，但 `runtime_aim_payload` 里混进 int 会让 JSON 里出现 `1` 而不是 `1.0`——管线那边读 float 无所谓，但测试对比会莫名其妙。
- `target_class` 在 `ProfileFormState` 里是 `str`（下拉框的值），而 `runtime_aim_payload` 里 `int(...)`。两处都别改。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_gui_web_forms.py`：

```python
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from rhodes_fast.config import default_config
from rhodes_fast.gui_core.state import Labels, config_to_form_state
from rhodes_fast.gui_web.forms import (
    FormBridge,
    UnknownField,
    choice_payload,
    param_payload,
)

# gui.py 里那六张映射。这里重抄一份而不是 import gui —— 那个模块 import tkinter,
# 而这整条路的前提就是不碰它。
PROVIDERS = {"自动（推荐）": "auto", "TensorRT FP16": "tensorrt", "CUDA": "cuda", "CPU": "cpu"}
OUTPUT_FORMATS = {"YOLOv5": "yolov5", "YOLOv8": "yolov8", "端到端 NMS": "end2end"}
INPUT_MODES = {
    "UDP 视频流 (MPEG-TS/H.264)": "udp_video",
    "UDP 单包 JPEG": "udp_jpeg",
    "OBS WebSocket": "obs_websocket",
}
LOG_LANGUAGES = {"中文": "zh", "English": "en"}
TRIGGERS = {
    "鼠标侧键 1": "side1",
    "鼠标侧键 2": "side2",
    "鼠标左键": "left",
    "鼠标右键": "right",
}


def _labels() -> Labels:
    from rhodes_fast.gui_core.state import algorithm_choices

    return Labels(
        provider=PROVIDERS,
        output_format=OUTPUT_FORMATS,
        input_mode=INPUT_MODES,
        language=LOG_LANGUAGES,
        trigger=TRIGGERS,
        algorithm=algorithm_choices(),
    )


def _bridge() -> FormBridge:
    return FormBridge(config_to_form_state(default_config(), Path("."), _labels()))


class SetFieldTest(unittest.TestCase):
    def test_it_sets_a_top_level_field(self) -> None:
        bridge = _bridge()
        bridge.set_field("model_path", "MODEL/a.onnx")
        self.assertEqual(bridge.state.model_path, "MODEL/a.onnx")

    def test_it_sets_a_field_on_one_profile_and_leaves_the_other_alone(self) -> None:
        """两套方案是对称的, 下标写错的话症状是「调方案 1 结果方案 2 变了」——
        而两栏长得一模一样, 用户第一反应是自己看错了。"""
        bridge = _bridge()
        before = bridge.state.profiles[1].kp_min
        bridge.set_field("profiles.0.kp_min", 0.07)
        self.assertEqual(bridge.state.profiles[0].kp_min, 0.07)
        self.assertEqual(bridge.state.profiles[1].kp_min, before)

    def test_it_sets_one_algorithm_parameter(self) -> None:
        bridge = _bridge()
        bridge.set_field("profiles.1.algorithm_params.kp", 0.123)
        self.assertEqual(bridge.state.profiles[1].algorithm_params["kp"], 0.123)

    def test_a_numeric_field_that_is_declared_str_stays_str(self) -> None:
        """udp_port 在 FormState 里是 str, 故意的: 表单留着用户打的原文, 非法
        输入要走到「设置无法保存」那个弹窗。JS 的 number 输入框送上来是数字,
        不转回字符串的话那条解析路径就被绕过去了。"""
        bridge = _bridge()
        bridge.set_field("udp_port", 4455)
        self.assertIsInstance(bridge.state.udp_port, str)
        self.assertEqual(bridge.state.udp_port, "4455")

    def test_a_field_declared_float_stays_float(self) -> None:
        bridge = _bridge()
        bridge.set_field("confidence", 1)
        self.assertIsInstance(bridge.state.confidence, float)

    def test_a_field_declared_bool_stays_bool(self) -> None:
        bridge = _bridge()
        bridge.set_field("cuda_graph", 1)
        self.assertIs(bridge.state.cuda_graph, True)

    def test_an_unknown_path_raises_instead_of_going_nowhere(self) -> None:
        """JS 里把路径打错是个很容易犯的错, 而静默忽略的症状是「这个控件没用」
        —— 没有任何线索指向拼写。"""
        for path in ("nope", "profiles.0.nope", "profiles.9.kp_min", "profiles", ""):
            with self.subTest(path=path):
                with self.assertRaises(UnknownField):
                    _bridge().set_field(path, 1)

    def test_an_unknown_algorithm_parameter_raises(self) -> None:
        with self.assertRaises(UnknownField):
            _bridge().set_field("profiles.0.algorithm_params.not_a_param", 1.0)


class PayloadTest(unittest.TestCase):
    def test_the_payload_round_trips_through_json(self) -> None:
        """它每次整片刷新都要过 evaluate_js。混进一个 tuple 或 Path 就当场炸,
        而那一下发生在载入预设之后 —— 表单会停在旧值上, 不报错。"""
        payload = _bridge().as_payload()
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_the_payload_has_every_form_field(self) -> None:
        from dataclasses import fields

        from rhodes_fast.gui_core.state import FormState

        payload = _bridge().as_payload()
        self.assertEqual(sorted(payload), sorted(field.name for field in fields(FormState)))

    def test_profiles_come_out_as_a_list_of_two(self) -> None:
        payload = _bridge().as_payload()
        self.assertIsInstance(payload["profiles"], list)
        self.assertEqual(len(payload["profiles"]), 2)

    def test_replace_state_swaps_everything_at_once(self) -> None:
        """载入预设走这条路。一个字段一个字段地 set 会让界面闪一串中间状态,
        而且中途任何一步抛异常就停在半新半旧上。"""
        bridge = _bridge()
        fresh = replace(bridge.state, model_path="MODEL/other.onnx")
        bridge.replace_state(fresh)
        self.assertEqual(bridge.state.model_path, "MODEL/other.onnx")


class ChoicesTest(unittest.TestCase):
    def test_every_dropdown_gets_its_options(self) -> None:
        choices = choice_payload(_labels())
        self.assertEqual(
            sorted(choices),
            ["algorithm", "input_mode", "language", "output_format", "provider", "trigger"],
        )
        self.assertEqual(choices["provider"][0], "自动（推荐）")

    def test_the_options_are_labels_not_stored_values(self) -> None:
        """下拉框里显示的是标签。塞存储值进去的话用户看到的是 auto / side1,
        而且选中项对不上, 每次打开都跳回第一项。"""
        self.assertNotIn("auto", choice_payload(_labels())["provider"])


class ParamPayloadTest(unittest.TestCase):
    def test_it_describes_every_parameter_of_the_algorithm(self) -> None:
        """02 屏的算法参数是动态生成的: 按 Param 的 min/max/default/label 建滑条。
        少给一个字段, 那个控件就画不出来或者范围是错的。"""
        params = param_payload("比例控制", _labels())
        self.assertTrue(params)
        for spec in params:
            self.assertEqual(
                sorted(spec),
                ["advanced", "default", "label", "maximum", "minimum", "name", "slider", "step"],
            )

    def test_an_unknown_algorithm_gives_an_empty_list_not_an_error(self) -> None:
        """settings.txt 指着一个已删掉的算法是真实场景。抛异常的话整个 02 屏
        画不出来, 用户连改回去的机会都没有。"""
        self.assertEqual(param_payload("这个算法不存在", _labels()), [])

    def test_the_payload_round_trips_through_json(self) -> None:
        params = param_payload("比例控制", _labels())
        self.assertEqual(json.loads(json.dumps(params)), params)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_forms -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_web.forms'`

- [ ] **Step 3: 实现 `rhodes_fast/gui_web/forms.py`**

```python
"""表单在 Python 侧的那份真相。

tkinter 那边真相在 tk 变量里, 这边在 FormBridge 里 —— JS 只是它的一个视图:
控件一动就 set_field 推上来, 要整片换 (载入预设、切算法) 时由 Python 把整份
状态推回去。

反过来 (JS 持有真相, 要用的时候读一遍 DOM) 也能做, 但热推每动一下滑条就要问
一次「现在整张表单是什么」, 保存、另存为、组命令行也都要 —— 那样每一次都得走
一个异步往返, 而 Prompter 那条路还是同步的。

纯 Python: 不碰 webview, 不碰文件系统。
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any, get_type_hints

from ..gui_core.state import (
    FormState,
    Labels,
    ProfileFormState,
    algorithm_param_specs,
)


class UnknownField(KeyError):
    """路径指向一个不存在的字段。

    静默忽略的症状是「这个控件没用」, 没有任何线索指向拼写错误 —— 而 JS 那边
    的路径是手写的字符串, 打错太容易了。
    """


def _coerce(declared: Any, value: Any) -> Any:
    """按 dataclass 声明的类型转换。

    JS 送上来的东西只有 number / boolean / string 三种, 而 FormState 里的类型
    是故意挑过的: udp_port 是 str (留着用户打的原文, 非法输入要走到「设置无法
    保存」那个弹窗), confidence 是 float。不按声明转的话, number 输入框会把
    端口变成 int, 那条解析路径就被绕过去了。
    """
    if declared is bool:
        return bool(value)
    if declared is float:
        return float(value)
    if declared is int:
        return int(value)
    if declared is str:
        return str(value)
    return value


# get_type_hints 而不是 dataclasses.fields(...).type: state.py 顶上有
# `from __future__ import annotations`, 注解因此是字符串 —— 实测 field.type
# 拿到的是 'str' 这个字符串, 不是 str 这个类。用它做 `is` 比较会全部落空,
# _coerce 静默返回原值, 于是端口号是个 int 而没人发现。
_FORM_TYPES = get_type_hints(FormState)
_PROFILE_TYPES = get_type_hints(ProfileFormState)


class FormBridge:
    def __init__(self, state: FormState) -> None:
        self.state = state

    def replace_state(self, state: FormState) -> None:
        """整片换。载入预设走这条路 —— 一个字段一个字段地 set 会让界面闪一串
        中间状态, 而且中途任何一步抛异常就停在半新半旧上。"""
        self.state = state

    def set_field(self, path: str, value: Any) -> None:
        parts = path.split(".")
        if parts[0] == "profiles":
            self._set_profile_field(parts, value)
            return
        if len(parts) != 1 or parts[0] not in _FORM_TYPES:
            raise UnknownField(path)
        name = parts[0]
        self.state = replace(self.state, **{name: _coerce(_FORM_TYPES[name], value)})

    def _set_profile_field(self, parts: list[str], value: Any) -> None:
        path = ".".join(parts)
        if len(parts) < 3 or parts[1] not in ("0", "1"):
            raise UnknownField(path)
        index = int(parts[1])
        profile = self.state.profiles[index]

        if parts[2] == "algorithm_params":
            if len(parts) != 4:
                raise UnknownField(path)
            name = parts[3]
            if name not in profile.algorithm_params:
                raise UnknownField(path)
            params = dict(profile.algorithm_params)
            params[name] = float(value)
            updated = replace(profile, algorithm_params=params)
        else:
            if len(parts) != 3 or parts[2] not in _PROFILE_TYPES:
                raise UnknownField(path)
            name = parts[2]
            updated = replace(profile, **{name: _coerce(_PROFILE_TYPES[name], value)})

        profiles = list(self.state.profiles)
        profiles[index] = updated
        self.state = replace(self.state, profiles=(profiles[0], profiles[1]))

    def as_payload(self) -> dict:
        """整份表单, JSON 可序列化。

        不用 dataclasses.asdict: 它会把 profiles 那个元组原样留成 tuple, 而
        json.dumps 虽然接受 tuple, 回来就变成 list —— 于是「推过去的」和「读
        回来的」不是同一个东西, 比对时会莫名其妙。这里显式转 list。
        """
        payload = {field.name: getattr(self.state, field.name) for field in fields(FormState)}
        payload["profiles"] = [
            {field.name: getattr(profile, field.name) for field in fields(ProfileFormState)}
            for profile in self.state.profiles
        ]
        for profile in payload["profiles"]:
            profile["algorithm_params"] = dict(profile["algorithm_params"])
        return payload


def choice_payload(labels: Labels) -> dict[str, list[str]]:
    """每个下拉框的选项。给的是显示标签, 不是存储值。

    塞存储值进去的话用户看到的是 auto / side1, 而且当前值 (标签) 对不上任何
    一个选项, 每次打开都跳回第一项。
    """
    return {
        name: list(getattr(labels, name))
        for name in ("provider", "output_format", "input_mode", "language", "trigger", "algorithm")
    }


def param_payload(algorithm_label: str, labels: Labels) -> list[dict]:
    """某个算法的参数契约, 给 02 屏动态生成控件用。

    收的是**显示标签** (表单里存的就是标签), 内部转成标识再查注册表。算法不
    存在时返回空列表 —— settings.txt 指着一个已删掉的算法是真实场景, 抛异常的
    话整个 02 屏画不出来, 用户连改回去的机会都没有。
    """
    name = labels.algorithm.get(algorithm_label)
    if name is None:
        return []
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "default": spec.default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "step": spec.step,
            "slider": spec.slider,
            "advanced": spec.advanced,
        }
        for spec in algorithm_param_specs(name)
    ]
```

`FormState.profiles` 的注解是 `tuple[ProfileFormState, ProfileFormState]`，`_coerce` 对它返回原值——没关系，那条路走不到：`"profiles"` 单独一段会先在 `_set_profile_field` 里抛 `UnknownField`（`len(parts) < 3`）。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_forms -v
```

- [ ] **Step 5: 确认没把 tkinter 拖进来**

```bash
.venv/Scripts/python.exe -c "import sys, rhodes_fast.gui_web.forms; print([n for n in sys.modules if n.startswith('tkinter')])"
```

Expected: `[]`

- [ ] **Step 6: 变异验证**

1. `_coerce` 的 `str` 分支删掉（直接返回 value）→ `test_a_numeric_field_that_is_declared_str_stays_str` 必须红
2. `_set_profile_field` 里 `profiles[index] = updated` 改成 `profiles[0] = updated` → `test_it_sets_a_field_on_one_profile_and_leaves_the_other_alone` 必须红
3. `set_field` 里 `raise UnknownField(path)` 改成 `return` → `test_an_unknown_path_raises...` 必须红
4. `param_payload` 里算法不存在时改成 `raise KeyError` → `test_an_unknown_algorithm_gives_an_empty_list_not_an_error` 必须红

- [ ] **Step 7: 全量测试 + 提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"
git add rhodes_fast/gui_web/forms.py tests/test_gui_web_forms.py
```

标题 `feat: hold the form state on the Python side`，正文说清楚为什么真相在 Python 侧、路径的形状、以及按声明类型转换那一条（`udp_port` 是 `str` 不是笔误）。

---

## Task 3: 模态框 + `ModalPrompter`

**Files:**
- Create: `rhodes_fast/gui_web/web/modal.js`
- Rewrite: `rhodes_fast/gui_web/prompts.py`（`LogOnlyPrompter` → `ModalPrompter`）
- Modify: `rhodes_fast/gui_web/app.py`（加 `answer_prompt`，`main()` 换 prompter）
- Modify: `rhodes_fast/gui_web/web/index.html`（模态框的壳 + `<script src="modal.js">`）
- Modify: `rhodes_fast/gui_web/web/app.css`
- Test: `tests/test_gui_web_prompts.py`（重写）、`tests/test_gui_web_shell.py`（追加）

**Interfaces:**
- Consumes: `Prompter` 协议（`rhodes_fast.gui_core.prompts`）
- Produces:
  - `class ModalPrompter`，构造 `ModalPrompter(window=None, push=None)`
  - `prompter.attach(window)` —— 窗口建出来之后回填
  - `prompter.answer(token: str, value) -> None`
  - `prompter.cancel_all() -> None` —— 关窗时叫，把还挂着的问题按「取消」放行
  - `Api.answer_prompt(token: str, value) -> None`（**公开**，JS 调）
  - JS：`window.showModal(spec)`，`spec = {token, kind, title, message, danger, initial, options}`

### 这条桥为什么成立

`Prompter` 的六个方法是同步的（`confirm` 要 `return bool`），而页面里的弹窗是异步的。已实测：**pywebview 把每个 JS→Python 的 api 调用放在自己的线程上**（线程名形如 `Thread-27 (_call)`），阻塞其中一个不会冻住 JS（阻塞 1 秒期间 `setInterval` 照跑 59/60 次），而且**阻塞期间第二个 api 调用照样被处理**。所以：

```
Python: confirm() -> evaluate_js(showModal) -> Event.wait()   [阻塞在 _call 线程上]
JS:     画出模态框 -> 用户点按钮 -> api.answer_prompt(token, true)
Python: [另一个 _call 线程] answer() -> 填答案 -> Event.set()
Python: confirm() 醒来 -> return True
```

三条护栏，少一条都会挂死：

1. **超时**。窗口在问题挂着的时候没了（Alt+F4、崩溃），那条线程会永远等下去。等 300 秒后按保守答案放行。
2. **`cancel_all()`**。`main()` 的 `finally` 里叫一次，把所有还挂着的问题立刻按取消放行，不用等满超时。
3. **notify 不阻塞**。`notify_error` / `notify_warning` / `notify_info` 只管画出来就返回。阻塞它们的话，万一哪天从读取线程调一次（日志那条路），日志就整个停了。

### 设计系统没有 Dialog

规格第 7 节写明这是两个缺口之一，token 有：`--shadow-overlay`（已确认存在于 `design/tokens/effects.css`）、遮罩 `rgba(16,17,16,.72)`。自拼的 class 一律 `ef-modal*` 前缀写在 `app.css` 里。

- [ ] **Step 1: 写失败测试（Python 侧）**

重写 `tests/test_gui_web_prompts.py`：

```python
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import Mock

from rhodes_fast.gui_core.prompts import Prompter
from rhodes_fast.gui_web.prompts import ModalPrompter


class ProtocolTest(unittest.TestCase):
    def test_it_satisfies_the_prompter_protocol(self) -> None:
        """业务侧 (GuiSession) 只认这个协议。少一个方法的话, 症状是某个流程
        走到一半 AttributeError, 而那是从 JS 调过来的 —— 异常消失在 pywebview
        里, 界面什么都不说。"""
        self.assertIsInstance(ModalPrompter(), Prompter)


class NotifyTest(unittest.TestCase):
    """三个 notify 不阻塞。

    阻塞它们的话, 万一哪天从读取线程调一次 (日志那条路就在那条线程上),
    整个运行状态就停了, 而且看起来像管线挂了。
    """

    def _prompter(self):
        window = Mock()
        pushed: list[str] = []
        return ModalPrompter(window=window, push=pushed.append), window, pushed

    def test_notifications_return_immediately(self) -> None:
        prompter, _window, _pushed = self._prompter()
        started = time.perf_counter()
        prompter.notify_error("标题", "正文")
        prompter.notify_warning("标题", "正文")
        prompter.notify_info("标题", "正文")
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_a_notification_reaches_the_page_and_the_log(self) -> None:
        prompter, window, pushed = self._prompter()
        prompter.notify_error("载入预设失败", "文件读不了")
        script = window.evaluate_js.call_args.args[0]
        self.assertIn("showModal", script)
        self.assertIn("载入预设失败", script)
        self.assertTrue(any("载入预设失败" in line for line in pushed),
                        "弹窗关掉之后就什么都不剩了, 运行状态里得留一行")

    def test_the_payload_is_json_not_string_concatenation(self) -> None:
        """标题和正文里有中文、引号、换行和 Windows 路径的反斜杠。直接拼进 JS
        字符串, 一个带引号的模型路径就能把脚本当场截断。"""
        prompter, window, _pushed = self._prompter()
        prompter.notify_error('模型', '找不到 "C:\\MODEL\\a.onnx"\n换行也要挺住')
        script = window.evaluate_js.call_args.args[0]
        payload = json.loads(script[script.index("(") + 1: script.rindex(")")])
        self.assertEqual(payload["message"], '找不到 "C:\\MODEL\\a.onnx"\n换行也要挺住')


class BlockingTest(unittest.TestCase):
    """confirm / confirm_three_way / ask_text 阻塞到 JS 回答为止。"""

    def _answer_from_another_thread(self, prompter, value, delay=0.05):
        def reply() -> None:
            time.sleep(delay)
            token = prompter._pending_tokens()[0]
            prompter.answer(token, value)

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)

    def test_confirm_returns_what_the_page_answered(self) -> None:
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, True)
        self.assertIs(prompter.confirm("删除预设", "确定吗"), True)

    def test_confirm_returns_false_when_the_page_says_no(self) -> None:
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, False)
        self.assertIs(prompter.confirm("删除预设", "确定吗"), False)

    def test_three_way_can_come_back_as_cancel(self) -> None:
        """askyesnocancel 的第三态。把它折成 False 的话「取消」会当成「否」
        执行下去 —— 而调用点正是「要不要先保存」这类问题。"""
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, None)
        self.assertIsNone(prompter.confirm_three_way("未保存", "先保存吗"))

    def test_ask_text_comes_back_as_a_string(self) -> None:
        prompter = ModalPrompter(window=Mock())
        self._answer_from_another_thread(prompter, "我的预设")
        self.assertEqual(prompter.ask_text("另存为", "名字"), "我的预设")

    def test_danger_is_carried_into_the_payload(self) -> None:
        """破坏性操作要红按钮 + 焦点落在取消。gui.py:1244 的注释:
        「默认按钮是取消: 手滑按回车删不掉」。"""
        window = Mock()
        prompter = ModalPrompter(window=window)
        self._answer_from_another_thread(prompter, False)
        prompter.confirm("删除算法", "删了找不回来", danger=True)
        script = window.evaluate_js.call_args.args[0]
        payload = json.loads(script[script.index("(") + 1: script.rindex(")")])
        self.assertIs(payload["danger"], True)


class SafetyNetTest(unittest.TestCase):
    def test_it_gives_up_after_the_timeout_instead_of_hanging_forever(self) -> None:
        """窗口在问题挂着的时候没了 (Alt+F4、崩溃), 那条线程会永远等下去。
        超时之后按保守答案放行。"""
        prompter = ModalPrompter(window=Mock(), timeout=0.2)
        started = time.perf_counter()
        self.assertIs(prompter.confirm("删除", "确定吗"), False)
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_cancel_all_releases_everyone_at_once(self) -> None:
        """关窗时叫。不叫的话每个挂着的问题都要等满超时, 而进程是在等它们。"""
        prompter = ModalPrompter(window=Mock(), timeout=30)
        answers: list[object] = []
        thread = threading.Thread(target=lambda: answers.append(prompter.confirm("a", "b")),
                                  daemon=True)
        thread.start()
        while not prompter._pending_tokens():
            time.sleep(0.01)
        prompter.cancel_all()
        thread.join(timeout=2)
        self.assertEqual(answers, [False])

    def test_answering_an_unknown_token_is_ignored(self) -> None:
        """JS 那边可能在超时之后才回答 (用户去倒了杯水)。那时 token 已经不在了,
        这一下必须是空操作 —— 抛异常的话它会浮到 JS 的 promise 上, 页面上多一条
        看不懂的报错。"""
        ModalPrompter(window=Mock()).answer("根本不存在的 token", True)

    def test_a_prompt_without_a_window_answers_conservatively(self) -> None:
        """窗口还没建出来就问了 (启动时载入预设那条路)。没地方画弹窗, 只能
        按「不做」放行 —— 而且不能卡住。"""
        self.assertIs(ModalPrompter().confirm("删除", "确定吗"), False)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_prompts -v
```

Expected: `ImportError: cannot import name 'ModalPrompter'`

- [ ] **Step 3: 实现 `rhodes_fast/gui_web/prompts.py`（整个文件替换）**

```python
"""「问用户」在 WebView 这边的实现: 页面内的模态框。

Prompter 的六个方法是同步的 (confirm 要 return bool), 而页面里的弹窗是异步的。
这中间的桥是「阻塞等 JS 回答」, 它成立的前提是实测过的:

  pywebview 把每个 JS->Python 的 api 调用放在自己的线程上 (线程名形如
  Thread-27 (_call))。阻塞其中一个不会冻住 JS —— 阻塞 1 秒期间页面的
  setInterval 照跑 59/60 次 —— 而且阻塞期间第二个 api 调用照样被处理,
  所以 answer() 进得来。

三条护栏, 少一条都会挂死:
  1. 超时。窗口在问题挂着的时候没了 (Alt+F4、崩溃), 那条线程会永远等下去。
  2. cancel_all()。关窗时叫一次, 不用等满超时。
  3. notify 不阻塞。万一哪天从读取线程调一次, 日志就整个停了。
"""

from __future__ import annotations

import json
import threading
from typing import Any
from uuid import uuid4

# 超时之后给的答案。一律是「不做」—— 问出来的都是删除、覆盖这类事。
_CONSERVATIVE: dict[str, Any] = {"confirm": False, "three_way": None, "text": None}


class ModalPrompter:
    def __init__(self, window=None, push=None, timeout: float = 300.0) -> None:
        self._window = window
        # 弹窗关掉之后就什么都不剩了, 所以每一句也往运行状态里写一行。
        self._push = push
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list]] = {}

    def attach(self, window) -> None:
        """窗口建出来之后回填。构造和建窗是两步 —— GuiSession 要 prompter,
        而 prompter 要 window, 谁都不能先有对方。"""
        self._window = window

    # ---- 给 Api 用 ----

    def answer(self, token: str, value: Any) -> None:
        """JS 回答了。token 不认识就是空操作。

        用户去倒了杯水、超时已经过去的情况下 JS 仍然会回答。抛异常的话它会浮到
        JS 的 promise 上, 页面上多一条看不懂的报错。
        """
        with self._lock:
            entry = self._pending.pop(token, None)
        if entry is None:
            return
        done, box = entry
        box.append(value)
        done.set()

    def cancel_all(self) -> None:
        """关窗时叫。把所有还挂着的问题立刻按取消放行。"""
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _token, (done, _box) in pending:
            done.set()

    def _pending_tokens(self) -> list[str]:
        """测试用。下划线开头 —— 它不是 Prompter 的一部分。"""
        with self._lock:
            return list(self._pending)

    # ---- Prompter ----

    def notify_error(self, title: str, message: str) -> None:
        self._show("error", title, message, blocking=False)

    def notify_warning(self, title: str, message: str) -> None:
        self._show("warning", title, message, blocking=False)

    def notify_info(self, title: str, message: str) -> None:
        self._show("info", title, message, blocking=False)

    def confirm(self, title: str, message: str, *, danger: bool = False) -> bool:
        answer = self._show("confirm", title, message, blocking=True, danger=danger)
        return bool(answer) if answer is not None else _CONSERVATIVE["confirm"]

    def confirm_three_way(self, title: str, message: str) -> bool | None:
        answer = self._show("three_way", title, message, blocking=True)
        return None if answer is None else bool(answer)

    def ask_text(self, title: str, message: str, *, initial: str = "") -> str | None:
        answer = self._show("text", title, message, blocking=True, initial=initial)
        return None if answer is None else str(answer)

    # ---- 内部 ----

    def _show(
        self,
        kind: str,
        title: str,
        message: str,
        *,
        blocking: bool,
        danger: bool = False,
        initial: str = "",
    ) -> Any:
        if self._push is not None:
            # 一行压平: 运行状态是一行一条的, 多行 message 会把它撑开。
            self._push(f"{title}：{' '.join(message.split())}")

        token = uuid4().hex
        spec = {
            "token": token,
            "kind": kind,
            "title": title,
            "message": message,
            "danger": danger,
            "initial": initial,
        }

        if self._window is None:
            # 窗口还没建出来就问了 (启动时载入预设那条路)。没地方画, 保守放行,
            # 而且绝不能卡住。
            return _CONSERVATIVE.get(kind)

        if blocking:
            done = threading.Event()
            box: list[Any] = []
            with self._lock:
                self._pending[token] = (done, box)

        # json.dumps 不是洁癖 —— 标题和正文里有中文、引号、换行和 Windows 路径
        # 的反斜杠, 直接拼进 JS 字符串, 一个带引号的模型路径就能把脚本截断。
        self._window.evaluate_js(f"window.showModal({json.dumps(spec)})")

        if not blocking:
            return None

        if not done.wait(self._timeout):
            with self._lock:
                self._pending.pop(token, None)
            return _CONSERVATIVE.get(kind)
        return box[0] if box else _CONSERVATIVE.get(kind)
```

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_prompts -v
```

- [ ] **Step 5: 加 `Api.answer_prompt` 并换掉 `main()` 里的 prompter**

`rhodes_fast/gui_web/app.py`：

```python
    def answer_prompt(self, token: str, value=None) -> None:
        """JS 回答了一个模态框。公开的 —— 页面上那几个按钮就靠它。"""
        if self._prompter is not None:
            self._prompter.answer(token, value)
```

`Api.__init__` 里加 `self._prompter = None`；`_wire` 多收一个 `prompter=None` 并存下来。`main()` 里把 `LogOnlyPrompter(...)` 换成：

```python
    prompter = ModalPrompter(push=api._push_log)
    prompter.attach(window)
    session = GuiSession(config_path, prompter)
```

`main()` 的 `finally` 里，**在 `api.stop()` 之前**加 `prompter.cancel_all()`：窗口已经没了，还挂着的问题要立刻放行，否则那几条线程会各等满 300 秒，而进程在等它们。

`tests/test_gui_web_app.py` 的 `test_only_the_intended_methods_are_visible_to_js` 会红——把 `answer_prompt` 加进期望列表，保持字母序。

- [ ] **Step 6: 模态框的壳（`index.html`）**

放在 `.ef-app` **里面、最后**（`</footer>` 之后、`</div>` 之前）：

```html
  <!-- 模态框。设计系统没有 Dialog (规格第 7 节的两个缺口之一)，用它的 token
       自拼：遮罩 rgba(16,17,16,.72)，面板用 --shadow-overlay。
       初始 hidden —— app.css 里必须把 [hidden] 的 display:none 写回来。 -->
  <div class="ef-modal" id="modal" hidden>
    <div class="ef-modal__scrim" id="modal-scrim"></div>
    <div class="ef-modal__panel" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="ef-modal__head">
        <span class="ef-panel__bar" aria-hidden="true"></span>
        <span class="ef-panel__title" id="modal-title">标题</span>
      </div>
      <p class="ef-modal__message" id="modal-message"></p>
      <label class="ef-field ef-modal__field" id="modal-field" hidden>
        <span class="ef-field__label">名称</span>
        <span class="ef-input"><input class="ef-input__el" type="text" id="modal-input"></span>
      </label>
      <div class="ef-modal__actions" id="modal-actions"></div>
    </div>
  </div>
```

`<script src="modal.js"></script>` 排在 `app.js` 之前。

- [ ] **Step 7: `web/modal.js`**

```javascript
"use strict";

/* 模态框。设计系统没有 Dialog（规格第 7 节的两个缺口之一），用它的基础类拼。

   每个弹窗带一个 token，回答时原样送回 Python —— Python 那边正有一条线程
   阻塞在这个 token 上。不送 token 的话，两个弹窗叠起来时答案会串。 */

const MODAL_BUTTONS = {
  error:     [{ label: "知道了", value: null, primary: true }],
  warning:   [{ label: "知道了", value: null, primary: true }],
  info:      [{ label: "知道了", value: null, primary: true }],
  confirm:   [{ label: "取消", value: false, cancel: true },
              { label: "确定", value: true, primary: true }],
  three_way: [{ label: "取消", value: null, cancel: true },
              { label: "否", value: false },
              { label: "是", value: true, primary: true }],
  /* takesInput 的按钮不带 value：值要到按下去那一刻才从输入框里取。用一个
     魔法字符串当哨兵也能做，但那等于在数据空间里挖一个洞 —— 用户真打了
     那个字符串就出错，而且永远查不到。 */
  text:      [{ label: "取消", value: null, cancel: true },
              { label: "确定", takesInput: true, primary: true }],
};

let modalToken = null;
let modalKind = null;

function showModal(spec) {
  modalToken = spec.token;
  modalKind = spec.kind;
  document.getElementById("modal-title").textContent = spec.title;
  document.getElementById("modal-message").textContent = spec.message;

  const field = document.getElementById("modal-field");
  const input = document.getElementById("modal-input");
  field.hidden = spec.kind !== "text";
  if (spec.kind === "text") input.value = spec.initial || "";

  const actions = document.getElementById("modal-actions");
  actions.innerHTML = "";
  (MODAL_BUTTONS[spec.kind] || MODAL_BUTTONS.info).forEach((button) => {
    const element = document.createElement("button");
    element.type = "button";
    /* 破坏性操作用 danger（红描边），不用 primary（填黄）：内容区那一个黄块
       是启动/停止按钮，弹窗再填一个就有两个了。 */
    const emphasis = button.primary
      ? (spec.danger ? "ef-btn--danger" : "ef-btn--secondary")
      : "ef-btn--outline";
    element.className = "ef-btn ef-btn--sm " + emphasis;
    element.textContent = button.label;
    element.addEventListener("click", () => {
      answerModal(button.takesInput ? document.getElementById("modal-input").value
                                    : button.value);
    });
    actions.appendChild(element);
  });

  document.getElementById("modal").hidden = false;

  /* 焦点：破坏性操作落在取消上（gui.py:1244 的注释「手滑按回车删不掉」），
     文本输入落在输入框上，其余落在主按钮上。 */
  const buttons = actions.querySelectorAll("button");
  if (spec.kind === "text") input.focus();
  else if (spec.danger) buttons[0].focus();
  else buttons[buttons.length - 1].focus();
}

function answerModal(value) {
  if (modalToken === null) return;
  const token = modalToken;
  modalToken = null;
  document.getElementById("modal").hidden = true;
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.answer_prompt(token, value);
  }
}

/* 取消的值随类型不同：confirm 要 false，三态和文本要 null。统一成 null 的话
   confirm 会走到 Python 那边「超时保守答案」那条路 —— 结果碰巧一样，但那是
   巧合不是设计，而且分不出「用户按了取消」和「窗口没了」。 */
function modalCancelValue() {
  return modalKind === "confirm" ? false : null;
}

/* Esc 等于取消。无边框窗口没有系统的关闭按钮可用，弹窗卡住就只能杀进程。
   遮罩点击不关 —— 手滑点到边上把一个「确定删除吗」关掉是无所谓，但把一个
   正在填名字的输入框关掉就白填了。 */
document.addEventListener("keydown", (event) => {
  if (modalToken === null) return;
  if (event.key === "Escape") {
    answerModal(modalCancelValue());
  } else if (event.key === "Enter" && modalKind === "text") {
    answerModal(document.getElementById("modal-input").value);
  }
});

window.showModal = showModal;
```


- [ ] **Step 8: `app.css` 的模态框样式**

要点（具体数值自己定，但这几条是硬的）：

```css
.ef-modal { position: fixed; inset: 0; z-index: 100; display: flex;
            align-items: center; justify-content: center; }
/* [hidden] 的 display:none 是浏览器默认样式，权重最低，上面那条 display:flex
   会把它盖掉 —— 弹窗会一直挡在界面上。这个坑在这个项目里踩过三次了。 */
.ef-modal[hidden] { display: none; }
.ef-modal__field[hidden] { display: none; }
.ef-modal__scrim { position: absolute; inset: 0; background: rgba(16, 17, 16, .72); }
.ef-modal__panel { position: relative; min-width: 380px; max-width: 560px;
                   background: var(--surface-panel); box-shadow: var(--shadow-overlay);
                   clip-path: var(--clip-br-lg); padding: var(--space-6); }
.ef-modal__message { white-space: pre-wrap; }   /* 正文里有 \n，折了就挤成一行 */
.ef-modal__actions { display: flex; justify-content: flex-end; gap: var(--space-4); }
```

- [ ] **Step 9: 追加前端测试（`tests/test_gui_web_shell.py`）**

```python
class ModalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.js = (WEB / "modal.js").read_text(encoding="utf-8")

    def test_modal_js_is_loaded_before_app_js(self) -> None:
        self.assertLess(self.html.index("modal.js"), self.html.index("app.js"))

    def test_the_answer_carries_the_token_back(self) -> None:
        """Python 那边正有一条线程阻塞在这个 token 上。不送 token 的话, 两个
        弹窗叠起来时答案会串到另一个问题上。"""
        self.assertIn("answer_prompt", self.js)
        self.assertIn("token", self.js)

    def test_escape_cancels(self) -> None:
        """无边框窗口没有系统的关闭按钮可用。弹窗卡住就只能杀进程。"""
        self.assertIn("Escape", self.js)

    def test_the_modal_does_not_add_a_second_patch_of_signal_yellow(self) -> None:
        """内容区那一个黄块是启动/停止按钮。弹窗再填一个就有两个了 ——
        设计系统的原话: If two things are yellow, one of them is wrong。"""
        self.assertNotIn("ef-btn--primary", self.js)
```

- [ ] **Step 10: 全量测试 + 变异验证 + 提交**

变异三条：
1. `_show` 里去掉 `done.wait` 的超时参数（改成 `done.wait()`）→ `test_it_gives_up_after_the_timeout...` 必须红（会挂住，用 `timeout` 命令限时跑）
2. `answer()` 里把 `self._pending.pop(token, None)` 改成 `self._pending.pop(token)` → `test_answering_an_unknown_token_is_ignored` 必须红
3. `notify_error` 改成 `blocking=True` → `test_notifications_return_immediately` 必须红

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"
git add rhodes_fast/gui_web/prompts.py rhodes_fast/gui_web/app.py \
        rhodes_fast/gui_web/web/modal.js rhodes_fast/gui_web/web/index.html \
        rhodes_fast/gui_web/web/app.css tests/test_gui_web_prompts.py tests/test_gui_web_shell.py
```

标题 `feat: ask the user through an in-page modal`，正文要说清楚那条阻塞桥为什么成立（引实测数字）和三条护栏各防什么。

---

## Task 4: 表单接线 + 01 运行设置屏

**Files:**
- Create: `rhodes_fast/gui_web/web/forms.js`
- Modify: `rhodes_fast/gui_web/app.py`、`web/index.html`、`web/app.css`
- Test: `tests/test_gui_web_app.py`（追加）、`tests/test_gui_web_shell.py`（追加）

**Interfaces:**
- Consumes: `FormBridge` / `choice_payload` / `param_payload`（Task 2）、`ModalPrompter`（Task 3）
- Produces:
  - `Api.set_field(path: str, value) -> None`（**公开**）
  - `Api.browse_model() -> None`（**公开**，开原生文件对话框）
  - `Api._push_form() -> None` —— 把整份表单推给 JS
  - JS：`window.setForm(payload)`、`window.setChoices(choices)`、`window.bindForm()`

### 接线的形状

```
启动   main() -> load_config -> config_to_form_state -> FormBridge
                -> api._push_form()  -> JS 按 payload 填每一个控件
改一下  JS change/input -> api.set_field("udp_port", "4455")
                        -> FormBridge 更新 -> 需要时热推
整片换  载入预设 -> bridge.replace_state(...) -> api._push_form() -> JS 重填
```

**控件到路径的绑定写在 HTML 里**，用 `data-field` 属性，`forms.js` 扫一遍就全接上了——一个控件一行 JS 的话，27 个字段加 20 个方案字段就是 47 行样板，而且漏一个不会报错。

```html
<input class="ef-input__el" type="text" data-field="udp_host">
<input class="ef-input__el" type="number" data-field="udp_port">
<input type="checkbox" data-field="cuda_graph">
<select class="ef-select__el" data-field="provider"></select>
```

- [ ] **Step 1: 写失败测试（Python 侧，追加到 `tests/test_gui_web_app.py`）**

```python
def _form_api():
    """一个接好线的 Api, 外加它的 window 和 bridge。

    模块级而不是某个 TestCase 的方法: 后面几个任务 (算法、预设、算法库) 的测试
    类都要用同一套接线。各自复制一份的话, 改了一处忘了另一处 —— 而症状是「某个
    测试类的假设过时了」, 它照样绿。
    """
    from pathlib import Path as _Path

    from rhodes_fast.config import default_config
    from rhodes_fast.gui_core.state import config_to_form_state
    from rhodes_fast.gui_web.app import Api
    from rhodes_fast.gui_web.forms import FormBridge

    window = Mock()
    api = Api()
    api._attach(window)
    bridge = FormBridge(config_to_form_state(default_config(), _Path("."), _web_labels()))
    api._wire(session=Mock(), relay=Mock(), config_path=_Path("settings.txt"),
              bridge=bridge, labels=_web_labels(), prompter=Mock())
    return api, window, bridge


class FormApiTest(unittest.TestCase):
    """表单的真相在 Python 侧, JS 只是视图。"""

    def test_set_field_reaches_the_bridge(self) -> None:
        api, _window, bridge = _form_api()
        api.set_field("udp_host", "10.0.0.2")
        self.assertEqual(bridge.state.udp_host, "10.0.0.2")

    def test_a_typo_in_the_path_is_reported_not_swallowed(self) -> None:
        """路径是 JS 里手写的字符串。静默忽略的症状是「这个控件没用」, 没有
        任何线索指向拼写 —— 让它在运行状态里留一行。"""
        api, window, _bridge = _form_api()
        api.set_field("这个字段不存在", 1)
        scripts = [call.args[0] for call in window.evaluate_js.call_args_list]
        self.assertTrue(any("这个字段不存在" in script for script in scripts))

    def test_pushing_the_form_sends_json_not_a_concatenated_string(self) -> None:
        """模型路径里有反斜杠和引号, 日志里有中文。直接拼进 JS 字符串, 一个
        带引号的路径就能把脚本截断 —— 之后表单再也刷不新, 而且不报错。"""
        api, window, bridge = _form_api()
        bridge.set_field("model_path", 'C:\\MODEL\\a "b".onnx')
        window.evaluate_js.reset_mock()
        api._push_form()
        script = window.evaluate_js.call_args.args[0]
        payload = json.loads(script[script.index("(") + 1: script.rindex(")")])
        self.assertEqual(payload["model_path"], 'C:\\MODEL\\a "b".onnx')

    def test_an_aim_field_is_pushed_to_a_running_pipeline(self) -> None:
        """边跑边调是这个界面存在的理由之一。滑条动了不热推的话, 用户要停一次
        再起一次才看得到效果 —— 而手感是要连着比的。"""
        api, _window, _bridge = _form_api()
        api._session.is_running = True
        with mock.patch.object(api, "_write_runtime_aim", autospec=True) as writer:
            api.set_field("profiles.0.kp_max", 0.2)
        writer.assert_called_once_with()

    def test_a_non_aim_field_does_not_touch_the_running_pipeline(self) -> None:
        """模型路径改了不该热推: 那份文件管线每帧读一次, 里面没有模型这一项,
        白写。"""
        api, _window, _bridge = _form_api()
        api._session.is_running = True
        with mock.patch.object(api, "_write_runtime_aim", autospec=True) as writer:
            api.set_field("model_path", "MODEL/b.onnx")
        writer.assert_not_called()
```

`_web_labels()` 是这个测试文件里要新加的 helper，照 `tests/test_gui_web_forms.py` 里那份抄（六张映射 + `algorithm_choices()`）。**两个文件里重复一份是有意的**：它们各自都不能 import `rhodes_fast.gui`。

`_form_api()` 是**模块级**函数，Task 5–7 的测试都用它——别在各自的测试类里再复制一份接线。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_app -v
```

Expected: `TypeError: _wire() got an unexpected keyword argument 'bridge'`

- [ ] **Step 3: `app.py` 实现**

`_wire` 多收 `bridge` 和 `labels`。新方法：

```python
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
            # 静默忽略的症状是「这个控件没用」, 没有任何线索指向拼写。
            self._push_log(f"界面内部错误：表单里没有「{path}」这个字段。")
            return
        head = path.split(".")
        if head[0] == "profiles" and (head[-1] in self._AIM_FIELDS
                                      or head[2:3] == ["algorithm_params"]):
            self._write_runtime_aim()

    def _write_runtime_aim(self) -> None:
        """把瞄准设置热推给正在跑的管线。照 gui.py:1787 的写法: 先写临时文件
        再 replace —— 管线每帧读一次, 读到半截 JSON 就是一次解析失败。"""
        if self._session is None or not self._session.is_running or self._config_path is None:
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

    def _push_form(self) -> None:
        """把整份表单推给 JS。载入预设、切算法、启动时都走这条。"""
        if self._window is None or self._bridge is None:
            return
        self._window.evaluate_js(f"window.setForm({json.dumps(self._bridge.as_payload())})")

    def _push_choices(self) -> None:
        if self._window is None or self._labels is None:
            return
        self._window.evaluate_js(f"window.setChoices({json.dumps(choice_payload(self._labels))})")

    def browse_model(self) -> None:
        """开 Windows 原生的文件对话框。

        比 HTML 的 <input type=file> 好: 后者拿不到真实路径 (浏览器只给一个
        File 对象和一个假路径), 而我们要往 settings.txt 里写路径。
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
        self.set_field("model_path", str(Path(selected[0])))
        self._push_form()
```

`import webview` 在函数体里——模块顶层会让 `ImportSurfaceTest` 红。

`main()` 里：建 `FormBridge(config_to_form_state(load_config(config_path, validate_model=False), config_path.parent, labels))`，`labels` 照 `gui.py` 的 `_labels()` 组（六张映射；**`gui_web` 不能 import `gui`**，所以那六张映射要么抄进 `forms.py` 要么单独一个 `rhodes_fast/gui_web/labels.py`。**选后者**：抄进 `forms.py` 会让它同时管两件事）。窗口 `loaded` 事件里调 `api._push_choices()` 和 `api._push_form()`——页面还没加载完就推的话 `window.setForm` 还不存在。

> `window.events.loaded` 的绑定和 `minimized`/`restored` 一样用 `+=`（`Event.__iadd__` 返回 self，已确认）。

- [ ] **Step 4: `web/forms.js`**

```javascript
"use strict";

/* 表单的视图。真相在 Python 侧的 FormBridge 里 —— 这边只做两件事：
   按推过来的状态填控件，和把用户的改动推上去。

   控件到字段的绑定写在 HTML 的 data-field 属性里，这里扫一遍全接上。
   一个控件写一行 JS 的话是 47 行样板，而且漏一个不会报错。 */

function fieldValue(element) {
  if (element.type === "checkbox") return element.checked;
  if (element.type === "number" || element.type === "range") return Number(element.value);
  return element.value;
}

function applyValue(element, value) {
  if (element.type === "checkbox") element.checked = Boolean(value);
  else element.value = value;
}

function pathValue(payload, path) {
  return path.split(".").reduce(
    (node, key) => (node === undefined || node === null ? undefined : node[key]), payload);
}

function setForm(payload) {
  document.querySelectorAll("[data-field]").forEach((element) => {
    const value = pathValue(payload, element.dataset.field);
    if (value !== undefined) applyValue(element, value);
  });
  /* 滑条旁边那个数字是派生的，不是字段。一起刷新，不然整片换之后数字还停在
     旧值上 —— 而滑条已经跳走了，两者对不上最让人不信任。 */
  document.querySelectorAll("[data-readout]").forEach(refreshReadout);
  document.dispatchEvent(new CustomEvent("form-changed", { detail: payload }));
}

function setChoices(choices) {
  document.querySelectorAll("[data-choices]").forEach((select) => {
    const options = choices[select.dataset.choices] || [];
    const current = select.value;
    select.innerHTML = "";
    options.forEach((label) => {
      const option = document.createElement("option");
      option.value = label;
      option.textContent = label;
      select.appendChild(option);
    });
    /* 选项重建之后当前值会掉。能对上就恢复 —— 对不上就随它落到第一项，
       那正是「配置里指着一个已删掉的算法」该有的样子。 */
    if (options.indexOf(current) >= 0) select.value = current;
  });
}

function refreshReadout(element) {
  const source = document.querySelector('[data-field="' + element.dataset.readout + '"]');
  if (!source) return;
  const digits = Number(element.dataset.digits || 0);
  element.textContent = Number(source.value).toFixed(digits) + (element.dataset.unit || "");
}

function pushField(element) {
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.set_field(element.dataset.field, fieldValue(element));
  }
}

function bindForm() {
  document.querySelectorAll("[data-field]").forEach((element) => {
    /* input 是「正在拖」，change 是「松手了」。滑条要 input —— 边拖边看手感
       正是这个界面存在的理由。文本框也用 input：等 change（失焦）的话，用户
       改完直接按启动，改的那一下根本没推上去。 */
    element.addEventListener("input", () => {
      pushField(element);
      document.querySelectorAll('[data-readout="' + element.dataset.field + '"]')
        .forEach(refreshReadout);
    });
    element.addEventListener("change", () => pushField(element));
  });
}

window.setForm = setForm;
window.setChoices = setChoices;
window.bindForm = bindForm;
```

> **滑条的推送频率**：`input` 在拖动时一秒能触发几十次，每一次都是一个 JS→Python 调用加一次文件写。实测 pywebview 每个调用一条线程，几十次每秒不会卡界面，而写的是本地小文件（几百字节）。**但这是要量的**：Step 7 里用真窗口拖 5 秒，数 `set_field` 被调了多少次、界面掉不掉帧。掉帧就在 `pushField` 上加 50ms 节流（`input` 节流 + `change` 必推）。

- [ ] **Step 5: 01 屏的标记**

三个 Panel，标题照旧界面（`模型` / `画面输入` / `KMBox`），字段照 `gui.py:396-470`：

| Panel | 字段 | `data-field` | 控件 |
|---|---|---|---|
| 模型 | ONNX 模型 | `model_path` | text + 「浏览…」按钮（`id="browse-model"`） |
| | 加速方式 | `provider` | select，`data-choices="provider"` |
| | 启用 CUDA Graph（TensorRT 低延迟） | `cuda_graph` | checkbox |
| | 启用 GPU 预处理（TensorRT） | `gpu_preprocess` | checkbox |
| 画面输入 | 输入方式 | `input_mode` | select，`data-choices="input_mode"` |
| | 监听地址 / 端口 | `udp_host` / `udp_port` | text / number |
| | 处理尺寸 | `udp_width` / `udp_height` | number × 2 |
| | OBS 地址 / 端口 | `obs_host` / `obs_port` | text / number |
| | 密码 / 来源名称 | `obs_password` / `obs_source` | password / text |
| KMBox | 启用 KMBox 控制 | `kmbox_enabled` | checkbox（放 Panel 标题栏右侧做 Toggle，照规格） |
| | 设备地址 / 端口 | `kmbox_host` / `kmbox_port` | text / number |
| | UUID | `kmbox_uuid` | **text，不是 password**（用户明确要明文） |

UDP 那组和 OBS 那组按 `input_mode` 互斥显示，照旧界面 `_switch_input_panel`。两个容器 `id="udp-panel"` / `id="obs-panel"`，**`[hidden]` 的 `display:none` 记得在 `app.css` 里写回来**。

- [ ] **Step 6: 前端测试（追加到 `tests/test_gui_web_shell.py`）**

```python
class RuntimeScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "forms.js").read_text(encoding="utf-8")

    def test_every_form_field_has_a_control(self) -> None:
        """漏一个字段的症状是「改了没用」—— 保存下去的是旧值, 不报错。
        这条按 FormState 的字段表来扫, 加字段时会自动红。"""
        from dataclasses import fields

        from rhodes_fast.gui_core.state import FormState

        # 这几个不在 01/02/03 三屏上: 预览那一条控制栏 (Task 8) 和动作行。
        elsewhere = {"preview_frame", "trail_enabled", "trail_optimal_path",
                     "trail_seconds", "latency_log_enabled", "profiles"}
        for field in fields(FormState):
            if field.name in elsewhere:
                continue
            with self.subTest(field=field.name):
                self.assertIn(f'data-field="{field.name}"', self.html)

    def test_the_uuid_is_not_masked(self) -> None:
        """用户明确决定明文显示, 跟旧界面一致 (gui.py:420 本来就是普通 Entry)。
        顺手做成 password 是个很容易犯的「好心」。"""
        match = re.search(r'<input[^>]*data-field="kmbox_uuid"[^>]*>', self.html)
        self.assertIsNotNone(match)
        self.assertNotIn('type="password"', match.group(0))

    def test_the_model_path_uses_the_native_dialog(self) -> None:
        """HTML 的 <input type=file> 拿不到真实路径, 而我们要往 settings.txt
        里写路径。"""
        self.assertIn("browse_model", (WEB / "app.js").read_text(encoding="utf-8")
                      + (WEB / "forms.js").read_text(encoding="utf-8"))
        self.assertNotIn('type="file"', self.html)
```

- [ ] **Step 7: 真窗口验证（这一步不能省）**

写一个临时脚本（放会话临时目录），跑真的 `main()`，用 `evaluate_js` 做：

1. 填一个文本框 → 读 `api._bridge.state.udp_host` 确认变了
2. 勾一个复选框 → 确认变了
3. 拖一个滑条 5 秒（`dispatchEvent(new Event("input"))` 循环）→ **数 `set_field` 被调了多少次**、看 `requestAnimationFrame` 的节拍掉不掉
4. `api._push_form()` → 确认控件跟着变

把第 3 步的数字记进提交信息。掉帧就加节流，然后重测。

- [ ] **Step 8: 全量测试 + 变异 + 提交**

变异两条：
1. `set_field` 里去掉 `_write_runtime_aim()` 的调用 → `test_an_aim_field_is_pushed_to_a_running_pipeline` 必须红
2. `_AIM_FIELDS` 改成包含 `model_path` → `test_a_non_aim_field_does_not_touch_the_running_pipeline` 必须红

标题 `feat: bind the runtime settings screen to the form state`。

---

## Task 5: 02 识别与控制屏

**Files:**
- Modify: `web/index.html`、`web/forms.js`、`web/app.css`、`rhodes_fast/gui_web/app.py`
- Test: `tests/test_gui_web_shell.py`、`tests/test_gui_web_app.py`（都追加）

**Interfaces:**
- Consumes: Task 4 的 `data-field` 绑定机制、`param_payload`（Task 2）
- Produces: `Api.set_algorithm(profile: int, label: str) -> None`（**公开**）、JS `window.renderParams(profile, specs, values)`

### 版面（照规格第 6 节）

```
┌ 模型输出 ────────────────────────────────────────┐
│ 输出格式 [自动 ▾]   运行信息语言 [中文 ▾]        │
│ 置信度  ━━━━●━━━  0.375                          │
│ NMS IoU ━━━━━●━━  0.500                          │
└──────────────────────────────────────────────────┘
┌ 控制方案 1 ──── [●━━] ┐ ┌ 控制方案 2 ─── [ ━━○] ┐
│ 控制算法 [卡尔曼 ▾]   │ │ 控制算法 [前馈 ▾]     │
│  ⋯ 参数动态生成       │ │  ⋯ 参数动态生成       │
│ [导出调校…][导入调校…]│ │ [导出调校…][导入调校…]│
│ 触发方式 [侧键 1 ▾]   │ │ 触发方式 [右键 ▾]     │
│ 目标标签 [0 ▾]        │ │ 目标标签 [0 ▾]        │
│ 框内位置 ━●━━  38%    │ │ 框内位置 ━●━━  38%    │
│ 视野半径 ━━●━  90     │ │ 视野半径 ━━●━  90     │
│ P 最小值 ━●━━  0.020  │ │ P 最小值 ━●━━  0.020  │
│ P 最大值 ━━●━  0.120  │ │ P 最大值 ━━●━  0.120  │
│ P 增长斜率 ━●━ 0.250  │ │ P 增长斜率 ━●━ 0.250  │
└───────────────────────┘ └───────────────────────┘
```

### 系统状态灯板（规格里有，旧界面没有）

设计稿 `ScreenDetection.jsx` 第一行右边是一块 1fr 的「系统状态」，三盏灯。旧 tkinter 界面没有这个东西，所以**没有现成的数据源**——查过了，能诚实点亮的只有下面这些，别造假灯：

| 灯 | 数据源 | 状态 |
|---|---|---|
| 视频流 | telemetry 的 `processed_fps`（每秒一行）；`输入：正在等待第一帧画面`（`pipeline.py:397`） | 没跑 `--off` / 在跑但无帧 `--connecting` / 有帧 `--online` |
| 模型 | 启动横幅 `模型：<名> \| 加速：<provider>`（`pipeline.py:360`），telemetry 已经在解析 | 没跑 `--off` / 横幅到了 `--online` |
| KMBox | **正常运行时管线只打配置里的地址**（`pipeline.py:367`），不报连没连上。连接结果只有 `--check` 那条路才有（`pipeline.py:829-834`：`KMBox 正常` / `KMBox 已禁用` / `KMBox: <错误>`） | 配置关着 `--off` / 开着但没验证过 `--standby` / `--check` 说正常 `--online` / `--check` 报错 `--error` |

第三盏灯**必须标成「未验证」而不是「已连接」**。把一个只反映配置的灯写成「已连接」是在撒谎，而且是那种用户会依赖的谎——他会以为设备好了，结果一枪不打。灯旁边放一行小字「按『测试输入』验证」。

用设计系统的 `ef-lamp` 组件（`--online` / `--standby` / `--connecting` / `--error` / `--off` 五个修饰类都现成）。

`app.js` 的 `setTelemetry` / `setRunState` 里顺手更新前两盏；第三盏由 `Api.run_check()` 的输出解析后更新（Task 8 做那个按钮，本任务先把灯和 `window.setLamp(name, state)` 建好，`--check` 的接线放在 Task 8）。

### 滑条

滑条的范围**逐字照抄 `gui.py`**，别自己定：

| 字段 | min | max | 小数位 | 出处 |
|---|---|---|---|---|
| `confidence` | 0.05 | 0.95 | 3 | gui.py:482 |
| `iou` | 0.05 | 0.95 | 3 | gui.py:485 |
| `aim_position` | 0 | 100 | 0（带 `%`） | gui.py:548 |
| `fov` | 10 | 320 | 0 | gui.py:557 |
| `kp_min` | 0.0 | 0.3 | 3 | gui.py:566 |
| `kp_max` | 0.0 | 0.3 | 3 | gui.py:575 |
| `kp_growth` | 0.0 | 0.5 | 3 | gui.py:584 |

「启用此方案」照规格挪到 Panel 标题栏右侧做 Toggle，关掉时整块 `opacity: .45` + `pointer-events: none`。

- [ ] **Step 1: 写失败测试**

`tests/test_gui_web_shell.py`：

```python
class DetectionScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "forms.js").read_text(encoding="utf-8")

    def test_both_profiles_have_every_control(self) -> None:
        """两套方案是对称的。漏了第二套的某个控件, 症状是那一栏少一行 —— 而
        两栏并排放着, 一眼就看得出来, 但漏的是 data-field 的下标就不会。"""
        for index in (0, 1):
            for name in ("enabled", "trigger", "target_class", "aim_position",
                         "fov", "kp_min", "kp_max", "kp_growth", "algorithm"):
                with self.subTest(profile=index, field=name):
                    self.assertIn(f'data-field="profiles.{index}.{name}"', self.html)

    def test_the_slider_ranges_match_the_old_gui(self) -> None:
        """范围自己定的话手感就变了, 而且是悄悄变的: 同一个位置对应的数不一样。
        逐字照抄 gui.py。"""
        for name, low, high in (("confidence", "0.05", "0.95"),
                                ("iou", "0.05", "0.95"),
                                ("profiles.0.fov", "10", "320"),
                                ("profiles.0.kp_growth", "0", "0.5")):
            with self.subTest(field=name):
                tag = re.search(rf'<input[^>]*data-field="{re.escape(name)}"[^>]*>', self.html)
                self.assertIsNotNone(tag, f"{name} 没有控件")
                self.assertIn(f'min="{low}"', tag.group(0))
                self.assertIn(f'max="{high}"', tag.group(0))

    def test_algorithm_params_are_generated_not_hard_coded(self) -> None:
        """参数是按 Param 契约动态建的。写死的话, 用户从算法库导入一个自己写的
        算法, 参数一个都出不来。"""
        self.assertIn("renderParams", self.js)
        self.assertNotIn("wind_strength", self.html)

    def test_the_kmbox_lamp_does_not_claim_a_connection_it_cannot_see(self) -> None:
        """正常运行时管线只打配置里的 KMBox 地址 (pipeline.py:367), 不报连没
        连上 —— 连接结果只有 --check 那条路才有。把一盏只反映配置的灯写成
        「已连接」是那种用户会依赖的谎: 他以为设备好了, 结果一枪不打。"""
        self.assertNotIn("已连接", self.html)
        self.assertIn("未验证", self.html)
```

`tests/test_gui_web_app.py`：

```python
class AlgorithmSwitchTest(unittest.TestCase):
    def test_switching_algorithm_replaces_the_parameters(self) -> None:
        """每个算法的参数完全不同。换了算法还留着上一个的参数, 那些值会跟着
        保存进 settings.txt, 而新算法根本不认识它们。"""
        api, window, bridge = _form_api()
        api.set_algorithm(0, "前馈")
        self.assertEqual(
            set(bridge.state.profiles[0].algorithm_params),
            {spec["name"] for spec in param_payload("前馈", _web_labels())},
        )

    def test_switching_algorithm_resets_the_values_to_the_contract_defaults(self) -> None:
        api, _window, bridge = _form_api()
        api.set_algorithm(0, "前馈")
        for spec in param_payload("前馈", _web_labels()):
            self.assertEqual(bridge.state.profiles[0].algorithm_params[spec["name"]],
                             spec["default"])

    def test_switching_algorithm_pushes_the_new_controls_to_the_page(self) -> None:
        api, window, _bridge = _form_api()
        window.evaluate_js.reset_mock()
        api.set_algorithm(1, "前馈")
        self.assertTrue(any("renderParams" in call.args[0]
                            for call in window.evaluate_js.call_args_list))
```

`_form_api()` 是 Task 4 那个 helper，提成模块级函数复用。

- [ ] **Step 2: 跑测试确认 RED**，Expected: `AttributeError: 'Api' object has no attribute 'set_algorithm'`

- [ ] **Step 3: 实现**

`app.py`：

```python
    def set_algorithm(self, profile: int, label: str) -> None:
        """换算法。参数要整批换掉 —— 每个算法的参数完全不同, 留着上一个的话
        那些值会跟着保存进 settings.txt, 而新算法根本不认识它们。"""
        if self._bridge is None or profile not in (0, 1):
            return
        specs = param_payload(label, self._labels)
        self._bridge.set_field(f"profiles.{profile}.algorithm", label)
        current = self._bridge.state.profiles[profile]
        updated = replace(current, algorithm_params={s["name"]: s["default"] for s in specs})
        profiles = list(self._bridge.state.profiles)
        profiles[profile] = updated
        self._bridge.replace_state(
            replace(self._bridge.state, profiles=(profiles[0], profiles[1]))
        )
        self._push_params(profile)
        self._write_runtime_aim()

    def _push_params(self, profile: int) -> None:
        if self._window is None or self._bridge is None:
            return
        label = self._bridge.state.profiles[profile].algorithm
        specs = param_payload(label, self._labels)
        values = dict(self._bridge.state.profiles[profile].algorithm_params)
        self._window.evaluate_js(
            f"window.renderParams({profile}, {json.dumps(specs)}, {json.dumps(values)})"
        )
```

`forms.js` 加 `renderParams(profile, specs, values)`：按 `specs` 建控件，`data-field` 是 `profiles.<i>.algorithm_params.<name>`，`advanced` 为真的收进一个默认折叠的 `<details>`（照旧界面把高级参数分开的做法），建完调一次 `bindForm()` 把新控件接上——**这一句最容易漏**，漏了的症状是新生成的滑条拖了没反应。

> `bindForm()` 重复调用会给老控件挂第二个监听器（一次改动推两次）。实现时用 `element.dataset.bound` 标记，已接过的跳过。

- [ ] **Step 4–6: GREEN、全量、变异**

变异三条：
1. `set_algorithm` 里不重置 `algorithm_params` → `test_switching_algorithm_replaces_the_parameters` 必须红
2. `renderParams` 里不调 `bindForm()` → 真窗口里拖新生成的滑条没反应（这条写成一个真窗口检查，不是单元测试）
3. 某个滑条的 `max` 改掉 → `test_the_slider_ranges_match_the_old_gui` 必须红

- [ ] **Step 7: 真窗口验证**

启动管线（**用 `--check` 这条不碰 KMBox 的路**，`session.build_command(..., extra=["--check"])`），切到 02，拖 `kp_max`，读 `.cache/webview-<pid>.aim.json` 确认内容跟着变。**这一步证明热推真的通了**，单元测试只能证明函数被调了。

- [ ] **Step 8: 提交**，标题 `feat: bind the detection and control screen`

---

## Task 6: 03 算法库屏

**Files:**
- Create: `rhodes_fast/gui_web/web/library.js`
- Modify: `web/index.html`、`web/app.css`、`rhodes_fast/gui_web/app.py`
- Test: `tests/test_gui_web_app.py`、`tests/test_gui_web_shell.py`（都追加）

**Interfaces:**
- Consumes: `GuiSession.library_rows/import_algorithm/algorithm_source/rename_algorithm/delete_algorithm/reload_algorithms`、`ModalPrompter`（Task 3）
- Produces:
  - `Api.refresh_library() -> None`、`Api.import_algorithm() -> None`、`Api.show_algorithm_source(name: str) -> None`、`Api.rename_algorithm(name: str) -> None`、`Api.delete_algorithm(name: str) -> None`（全部**公开**）
  - JS：`window.setLibrary(rows)`

设计系统没有 Table（规格第 7 节的另一个缺口）。用 `ef-` 基础类自拼，选中行按规范是**整行信号黄填充 + 前缘 2px ink 条**——这跟「内容区只有一个黄块」冲突，**按规范走**：列表选中是选择态，跟 NavRail 当前项同一类，不算「第二个信号」。在 `app.css` 里把这条判断写下来。

`library_rows()` 的六元组：显示名 / 标识 / 作者 / 源文件 / 导入时间 / 状态。

**内置算法不能改名也不能删除**——`GuiSession` 那边会拒绝，但界面要先把按钮置灰，不然用户点了才被拒。判据是第六列等于 `"内置"`。

- [ ] **Step 1: 写失败测试**

```python
class LibraryApiTest(unittest.TestCase):
    def test_refresh_pushes_the_rows_to_the_page(self) -> None:
        api, window, _bridge = _form_api()
        api._session.library_rows.return_value = [
            ("比例控制", "p", "Endfield", "—", "—", "内置"),
            ("我的算法", "my_aim", "Jayzi", "my.py", "2026-09-01", "已导入"),
        ]
        window.evaluate_js.reset_mock()
        api.refresh_library()
        script = next(c.args[0] for c in window.evaluate_js.call_args_list
                      if "setLibrary" in c.args[0])
        rows = json.loads(script[script.index("(") + 1: script.rindex(")")])
        self.assertEqual(rows[1][1], "my_aim")

    def test_importing_refreshes_the_list_and_the_algorithm_dropdowns(self) -> None:
        """刚导入的算法要当场能选。列表刷了而下拉框没刷的话, 用户在算法库里
        看得见它, 回到 02 屏却选不到 —— 只能以为坏了。"""
        api, window, _bridge = _form_api()
        api._session.import_algorithm.return_value = True
        window.evaluate_js.reset_mock()
        api.import_algorithm()
        scripts = [c.args[0] for c in window.evaluate_js.call_args_list]
        self.assertTrue(any("setLibrary" in s for s in scripts))
        self.assertTrue(any("setChoices" in s for s in scripts))

    def test_deleting_goes_through_the_session_which_asks_first(self) -> None:
        """问一句再删是 GuiSession.delete_algorithm 的职责 (它调 prompter)。
        界面再问一遍就是两个弹窗。"""
        api, _window, _bridge = _form_api()
        api.delete_algorithm("my_aim")
        api._session.delete_algorithm.assert_called_once_with("my_aim")

    def test_the_source_is_shown_not_swallowed(self) -> None:
        api, window, _bridge = _form_api()
        api._session.algorithm_source.return_value = "class Mine:\n    pass\n"
        window.evaluate_js.reset_mock()
        api.show_algorithm_source("my_aim")
        self.assertTrue(any("showModal" in c.args[0]
                            for c in window.evaluate_js.call_args_list))

    def test_a_deleted_algorithm_in_use_falls_back_and_says_so(self) -> None:
        """正在用的算法被删掉了。悄悄换成别的会让手感莫名其妙变一个样 ——
        照 gui.py:_reload_algorithm_library 的做法, 换回比例控制并写一行。"""
        api, _window, bridge = _form_api()
        bridge.set_field("profiles.0.algorithm", "我的算法")
        api._session.reload_algorithms.return_value = []
        api._resync_algorithms()
        self.assertEqual(bridge.state.profiles[0].algorithm, "比例控制")
```

前端：

```python
class LibraryScreenTest(unittest.TestCase):
    def test_builtin_rows_cannot_be_renamed_or_deleted(self) -> None:
        """内置算法随程序分发。按钮不置灰的话, 用户点了才被拒 —— 而拒绝理由
        跟「这个算法坏了」长得一样。"""
        js = (WEB / "library.js").read_text(encoding="utf-8")
        self.assertIn("内置", js)
        self.assertIn("disabled", js)

    def test_the_selected_row_is_a_block_not_an_underline(self) -> None:
        """设计系统 States 表的原话: Selection is a block, not an underline。"""
        css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.assertRegex(css, r"\.ef-libraryrow--active[^{]*\{[^}]*background")
```

- [ ] **Step 2–4: RED → 实现 → GREEN**

`app.py` 的五个方法都是薄转发，唯一有内容的是 `_resync_algorithms()`：照 `gui.py:_reload_algorithm_library`（783 行附近）搬——重载前记下每个方案用的是哪个**标识**，重载后按标识把显示名写回去。不这么做，存的名字会对不上任何一个选项，之后每次读表单都静默失败。

`library.js` 建表格：一行一个 `<div class="ef-libraryrow">`，选中行加 `ef-libraryrow--active`，底下三个按钮按选中行的第六列决定 `disabled`。

- [ ] **Step 5: 全量 + 变异 + 提交**

变异两条：
1. `import_algorithm` 里不调 `_push_choices()` → `test_importing_refreshes_the_list_and_the_algorithm_dropdowns` 必须红
2. `_resync_algorithms` 里不做标识回写 → `test_a_deleted_algorithm_in_use_falls_back_and_says_so` 必须红

标题 `feat: manage the algorithm library from the WebView shell`

---

## Task 7: 预设条

**Files:** `web/index.html`、`web/app.css`、`web/forms.js`、`rhodes_fast/gui_web/app.py`；测试 `tests/test_gui_web_app.py`

**Interfaces:**
- Consumes: `GuiSession.list_presets/load_preset/store_preset/delete_preset/read_preset_baseline`、`apply_preset_to_state`、`form_state_to_config`
- Produces: `Api.select_preset(name)`、`Api.save_preset()`、`Api.save_preset_as()`、`Api.delete_preset()`、`Api.refresh_presets()`（全部**公开**）

按规格放 SectionHeader 右侧（跨面板的全局操作，跟 SectionHeader 同属外壳；TitleBar 只有 44px 且已有窗口按钮）。四个控件全保留：下拉 + 保存 + 另存为… + 删除。

- [ ] **Step 1: 失败测试**

```python
class PresetTest(unittest.TestCase):
    def test_loading_a_preset_replaces_the_whole_form_in_one_go(self) -> None:
        """一个字段一个字段地填会让界面闪一串中间状态, 而且中途任何一步抛异常
        就停在半新半旧上 —— 那比什么都没发生更糟。"""
        api, window, bridge = _form_api()
        api._session.load_preset.return_value = _a_preset()
        window.evaluate_js.reset_mock()
        api.select_preset("我的预设")
        self.assertEqual(sum("setForm" in c.args[0]
                             for c in window.evaluate_js.call_args_list), 1)

    def test_a_preset_that_cannot_be_read_leaves_the_form_alone(self) -> None:
        """load_preset 读不了会返回 None 并且自己弹过窗。这边再把表单清成默认
        值的话, 用户会丢掉手上正在调的东西。"""
        api, _window, bridge = _form_api()
        before = bridge.state.model_path
        api._session.load_preset.return_value = None
        api.select_preset("坏掉的预设")
        self.assertEqual(bridge.state.model_path, before)

    def test_save_as_asks_for_a_name_and_stores_under_it(self) -> None:
        api, _window, _bridge = _form_api()
        api._prompter.ask_text = Mock(return_value="新预设")
        api._session.store_preset.return_value = "新预设"
        api.save_preset_as()
        self.assertEqual(api._session.store_preset.call_args.args[0], "新预设")

    def test_cancelling_the_name_saves_nothing(self) -> None:
        """ask_text 取消返回 None。当成空名字存下去的话会冒出一个叫「」的预设。"""
        api, _window, _bridge = _form_api()
        api._prompter.ask_text = Mock(return_value=None)
        api.save_preset_as()
        api._session.store_preset.assert_not_called()
```

- [ ] **Step 2–5:** RED → 实现（`select_preset` 走 `load_preset` → `apply_preset_to_state` → `bridge.replace_state` → `_push_form()` 一次）→ GREEN → 全量 → 提交

标题 `feat: switch presets from the WebView shell`

---

## Task 8: 动作行补齐 + 04 控制栏

**Files:** `web/index.html`、`web/app.js`、`web/app.css`、`rhodes_fast/gui_web/app.py`；测试 `tests/test_gui_web_shell.py`、`tests/test_gui_web_app.py`

**Interfaces:**
- Produces: `Api.save_settings()`、`Api.run_check()`、`Api.run_benchmark()`、`Api.run_pipeline_benchmark()`（全部**公开**）

动作行补上（照 `gui.py:629-648`）：保存设置 / 测试输入 / 模型测速 / 管线测速 / 记录延迟日志（复选框，`data-field="latency_log_enabled"`）。三个测速走 `session.build_command(..., extra=[...])`：

| 按钮 | `extra` | 出处 |
|---|---|---|
| 测试输入 | `["--check"]` | gui.py:635 |
| 模型测速 | `["--benchmark", "200"]` | gui.py:638 |
| 管线测速 | `["--pipeline-benchmark", "500"]` | gui.py:641 |

> **这三条不建预览 socket**（`build_command` 的注释：基准测试那条路不建预览，一个预览参数都不带）。所以它们不能走 `Api.start()`，要单独一条不传 `preview_port` 的路。

04 屏补上控制栏（照 `gui.py:360-378`）：画面 / 轨迹 / 最优路径 三个复选框 + 轨迹长度滑条（`TRAIL_MIN_SECONDS=0.2` 到 `TRAIL_MAX_SECONDS=2.0`，出自 `rhodes_fast/config.py:14-15`）。这四个改了要写 `trail_settings_file`——照 `gui.py:_write_trail_settings_file`，用 `trail_settings_from_state(state)`。

**「画面」这个复选框每次打开都是勾上的，不从设置里恢复**（`gui.py:274` 的注释：预览页的勾选框每次打开都是「只看画面」，只有轨迹长度记住）。

- [ ] 测试要点：三条测速各自的 `extra` 逐字对；`--check` 那条不带 `--preview-port`；轨迹四个字段改了会写 `trail_settings_file`；`save_settings` 失败时弹窗而不是静默。
- [ ] 提交标题 `feat: complete the action row and the preview controls`

---

## Task 9: 运行中锁定 + 端到端

**Files:** `web/app.js`、`web/forms.js`、`web/app.css`、`rhodes_fast/gui_web/app.py`；测试 `tests/test_gui_web_shell.py`

照 `gui.py:_set_running`（1838 行附近）：**运行中锁住模型路径、浏览按钮、加速方式、预设下拉**——这四样只在启动时读一次，运行中改了不生效，留着能改就是在骗人。**保存/另存为不锁**：边打边调好了，正该当场存下来。

实现：`setRunState(running)` 里给 `[data-lock-while-running]` 的元素设 `disabled`。HTML 上给那四个控件加这个属性。

- [ ] **Step 1:** 测试：四个控件都带 `data-lock-while-running`；预设的保存/另存为按钮**不带**（这条是反向断言，防止顺手全锁了）
- [ ] **Step 2–4:** 实现 → GREEN → 全量

- [ ] **Step 5: 端到端手动验证（本计划的交付物）**

```bash
.venv/Scripts/python.exe -c "from rhodes_fast.gui_web.app import main; main()"
```

逐条走，**每条都要真的做一遍**：

1. 三个屏的控件都填着 `settings.txt` 里的值（跟旧界面并排开着比一遍）
2. 改一个值 → 保存设置 → 关掉重开 → 值还在
3. 另存为一个预设 → 切到别的预设 → 切回来 → 值对
4. 删预设 → **弹的是页面内的模态框，不是 Windows 的消息框**；按 Esc 能取消
5. 导入一个算法（用 `examples/` 里的）→ 算法库列表当场多一行 → 02 屏的下拉框当场能选到
6. 启动 → 运行状态出日志 → 模型路径和预设下拉**灰掉**
7. 切到 04 → 画面在动 → 拖 `kp_max` → `.cache/webview-<pid>.aim.json` 跟着变
8. 停止 → 控件解锁
9. 关窗 → 任务管理器里没有残留的 python

- [ ] **Step 6: 提交**，标题 `feat: lock the boot-time settings while the pipeline runs`

---

## 验收标准

- 全量测试 `OK`，不低于 783 + 新增
- `.venv/Scripts/python.exe -c "import sys, rhodes_fast.gui_web.app; print([n for n in sys.modules if n.startswith('tkinter')])"` → `[]`
- 断网状态下窗口照常渲染
- 端到端九条全过
- **旧 `endfield-gui` 除 Task 1 外一行未改，手动跑一遍行为不变**
- `endfield-gui` 仍是 tkinter，`endfield-gui-next` 是 WebView——**入口没切**

## 明确不做

- **切换默认入口**（旧界面降级为 `endfield-gui-classic`）—— 用户验收之后单独做
- **删除 tkinter 界面**
- **检测框矢量化** —— 要改帧协议
- **把预览 socket 收进 `gui_core`** —— 两边形态不同，等都稳定了再看
- **中文字体自托管**
- **Windows Aero Snap**（贴边分屏）—— 需要 `WM_NCHITTEST`，记进 README 的已知限制

