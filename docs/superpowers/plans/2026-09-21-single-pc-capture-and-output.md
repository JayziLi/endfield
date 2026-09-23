# 单机模式：本机采集 + 移动输出 实施计划

> 规格：[docs/superpowers/specs/2026-09-21-single-pc-capture-and-output-design.md](../specs/2026-09-21-single-pc-capture-and-output-design.md)。
> 本计划只写任务怎么拆、接口叫什么、按什么顺序做；每条要求的理由都在规格里，不重复。
> 执行方式：本会话内联执行，每个任务都是 RED → GREEN → 提交。

**目标**：采集部分加「本机屏幕」（DXcam，可选 DXGI 或 WGC），移动部分加「本机 SendInput」。两部分可以任意组合，KMBox 仍是主要适配对象。

## 全局约束

- 测试：`.venv/Scripts/python.exe -m unittest discover -s tests`，全部通过才能提交。不要同时跑两个 unittest 进程。
- 不碰 `docs/projectile-prediction-research.zh-CN.md`（用户自己删掉、还没暂存的）；不 `git add -A`，不推送。
- 提交信息结尾加 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。
- 实机脚本放会话临时目录，在隔离的工作区里跑，不改用户的 `settings.txt`。
- 新模块不在导入时 import `dxcam`，也不在导入时加载 `user32`：测试机上可能没有 DXcam，而且单元测试不能真动鼠标。
- 不做任何绕过检测的事（规格 §11）。

## 任务 1：配置、预设、表单状态

**文件**：`config.py`、`presets.py`、`gui_core/state.py`、`gui_web/forms.py`；测试放 `test_config.py`、`test_presets.py`、`test_gui_core_state.py`、`test_gui_web_forms.py`。

- `DesktopConfig(backend="dxgi", monitor=0, width=320, height=320)`、`MouseConfig(output="kmbox")`。`AppConfig` 在末尾加这两个字段，都有默认值。`_read_config` 的 section_types 加上这两节，并加校验。
- `input.mode` 的合法值加 `"desktop"`（`config.py` 和 `presets._INPUT_MODES`）。
- 预设：`Preset` 在末尾加 `desktop` 和 `mouse` 两个字段，都有默认值。`_WIRING_FIELDS` 加 `desktop: (backend, monitor, width, height)` 和 `mouse: (output,)`。**老预设缺这两节时按默认值读**，所以不能用 `_section`，要用一个允许缺失的读法。
- `gui_core/state.py`：
  - 标签映射：`INPUT_MODES` 加 `"本机屏幕": "desktop"`，新增 `DESKTOP_BACKENDS` 和 `MOUSE_OUTPUTS`。
  - `Labels` 加 `desktop_backend` 和 `mouse_output` 两个字段。
  - `FormState` 在末尾加 `desktop_backend / desktop_monitor / desktop_width / desktop_height / mouse_output`，类型都是 `str | None`，默认 `None`。**`None` 的意思是「这个界面没有这些控件」**：tkinter 自己构造 `FormState`，不传这几个字段。
  - `config_to_form_state` 填上这几个字段；`form_state_to_config` 遇到 `None` 就保留 base 里的值；`apply_preset_to_state` 也要填。
- `gui_web/forms.py`：`_coerce` 要能拆开 `Optional[X]`；`choice_payload` 加 `desktop_backend` 和 `mouse_output`。

## 任务 2：本机采集源

**文件**：新建 `rhodes_fast/desktop_source.py`；改 `pipeline.py`（`create_source`、`source_label`、`--check`）、`pipeline_benchmark.py`（记录采集尺寸）、`gui_web/lamps.py`（视频流灯）。测试新建 `tests/test_desktop_source.py`，另外改 `test_gui_web_lamps.py`。

```python
def capture_region(screen_w: int, screen_h: int, width: int, height: int) -> tuple[int, int, int, int]
class DesktopSource:  # 实现 FrameSource
    def __init__(self, config: DesktopConfig, *, create_camera: Callable | None = None, retry_seconds: float = 0.5)
    error / fps / label / start() / stop() / wait_next(after_sequence, timeout)
```

- `create_camera(output_idx=, output_color="BGR", backend=)` 返回一个带 `width`、`height`、`grab(region=, new_frame_only=True)` 和 `release()` 的对象。默认值是一个延迟 `import dxcam` 的函数；导入失败时抛出的 `RuntimeError` 里写明安装命令。
- `--check` 的输出：`本机屏幕 正常：画面=WxH · 显示器=WxH · 后端=dxgi`，失败时是 `本机屏幕 连接失败：…`。英文分别是 `Desktop OK: frame=… · screen=… · backend=…` 和 `Desktop FAILED: …`。

## 任务 3：本机鼠标（SendInput）与控制器

**文件**：新建 `rhodes_fast/local_mouse.py`；改 `kmbox_control.py`（`connect` 和 `_send`）、`pipeline.py`（横幅、`--check`、两处构造都传 `output`）、`gui_web/lamps.py`、`gui_web/app.py`（`_output_enabled`）。测试新建 `tests/test_local_mouse.py`，另外改 `test_kmbox_control.py`、`test_gui_web_lamps.py`、`test_gui_web_app.py`、`test_pipeline*.py`。

```python
class LocalMouseClient:
    def __init__(self, *, user32=None, hand_motion: HandMotion | None = None, raw_monitor_factory=None)
    move(dx, dy) / isdown_left() / isdown_right() / isdown_side1() / isdown_side2() / close()
def check_local_mouse(user32=None) -> None      # 给 --check 用：不发位移，只读一次按键
KmboxController(..., output: str = "kmbox")
lamp_updates(line, *, output_enabled: bool)
```

## 任务 4：用 Raw Input 读手的移动

**文件**：`local_mouse.py`（`RawMouseMonitor`、`parse_raw_mouse`）、`kmbox_control.py`（`HandMotion.add`）。测试放 `tests/test_local_mouse.py` 和 `tests/test_hand_motion.py`。

```python
def parse_raw_mouse(buffer: bytes) -> tuple[int, int] | None   # 忽略注入事件、绝对移动和非鼠标数据
class RawMouseMonitor:
    def __init__(self, on_move: Callable[[int, int], None])
    start() -> None   # 注册失败时抛 OSError
    stop() -> None
```

## 任务 5：界面

**文件**：`gui_web/web/index.html`、`forms.js`、`app.css`、`gui.py`（只改 `_switch_input_panel`）。测试放 `test_gui_web_shell.py`，另外 `node --check` 所有 js 文件。

- 画面输入面板加 `#desktop-panel`；「KMBox」面板改名「移动输出」，加输出方式下拉框，KMBox 的字段收进 `#kmbox-fields`，SendInput 时显示一行提示。
- 第三盏状态灯的标题按输出方式切换。
- 在真窗口里验证。

## 任务 6：依赖、实机测量、文档

- `pyproject.toml` 加 `local = ["dxcam[winrt]>=0.3,<0.4"]`，装进 venv。
- 按规格 §8 的六项测量，结果写回规格的「实测记录」一节，并据此定下默认后端和抓帧方式。
- README 加一段单机模式说明。
