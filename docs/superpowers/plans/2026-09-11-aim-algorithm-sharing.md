# 瞄准算法分享与算法库 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户能把一套手感调校导出成 JSON 发给别人，并能把别人写的 `.py` 算法装进本机、改显示名、删掉。

**Architecture:** 两条互不依赖的链路。调校走纯数据 JSON，**永不执行代码**；算法实现走文件安装，**先看后装**——选文件只读文本和 ast，用户点「确定导入」之后才 import 校验。已装算法通过一个进程级注册表并进 `available_algorithms()`，界面和管线在启动时各加载一次。

**Tech Stack:** Python 3.11、标准库（`json` / `ast` / `importlib.util` / `inspect` / `dataclasses`）、tkinter/ttk、unittest。

## Global Constraints

- 设计文档：`docs/superpowers/specs/2026-09-11-aim-algorithm-plugins-design.md`，本计划实现其中「分享」「算法库管理」两节。
- 调校 JSON 的 `format` 版本号为 **1**；不认识的版本**报错，不猜测**。
- 调校 JSON **不含** `trigger` 和 `target_class`——按键习惯因人而异，目标标签取决于用哪个模型。
- 导入调校时算法名解析不到就**明确报错**，绝不静默回退到默认算法。
- 导入 `.py` 的顺序不可调换：选文件（不执行）→ 确认框（可看源码）→ 用户确认 → 才复制并 import 校验 → 失败则删除并报错。
- 改名**只改显示名**，不动 `.py` 里的 `NAME`——改了 `NAME`，别人发来的调校就对不上。
- 内置算法不出现在 `algorithms/` 和注册表里，**不可删除、不可改名**，且已装算法不得顶掉内置 `NAME`。
- 算法在管线**启动时**加载；启动时算法不存在则**响亮地**回退到 `p`，不阻止启动。
- `algorithms/` 目录与 `settings.txt` 同级（即 `config_path.parent / "algorithms"`）。
- 测试命令一律 `.venv/Scripts/python.exe -m unittest ...`，基线 159 个测试必须保持通过。
- 提交信息结尾必须是 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。

## File Structure

| 文件 | 职责 |
|---|---|
| `rhodes_fast/tuning_share.py`（新建） | 调校 JSON 的序列化/反序列化/套用/延迟比对。纯数据，不碰文件系统。 |
| `rhodes_fast/algorithm_library.py`（新建） | `algorithms/` 目录与 `installed.json` 注册表：检查候选、安装、改名、删除、加载。 |
| `rhodes_fast/aim_algorithms/registry.py`（改） | 把已装算法并进 `available_algorithms()`。 |
| `rhodes_fast/latency_log.py`（改） | 落盘/读回本机最近一次实测回路延迟。 |
| `rhodes_fast/pipeline.py`（改） | 启动时加载算法库；打印回退警告；实测延迟落盘。 |
| `rhodes_fast/__main__.py`（改） | 把 `algorithms/` 目录传给管线。 |
| `rhodes_fast/gui.py`（改） | 方案面板的导出/导入按钮；新增「算法库」标签页。 |
| `.gitignore`（改） | 忽略 `algorithms/` 和 `.loop-latency.json`。 |

---

### Task 1: 让回退到 `p` 的警告真的被看见

`rhodes_fast/kmbox_control.py:74-84` 已经把「算法 X 找不到，已回退到 p」收进
`self.algorithm_warnings`，但全项目没有第二处引用它——警告收集了然后被扔掉。
设计文档要求这个回退必须响亮，否则用户会在游戏里才发现自瞄行为完全不是自己选的那个。

**Files:**
- Modify: `rhodes_fast/pipeline.py`（新增 `_algorithm_warning_text`，在两处构造 `KmboxController` 之后调用）
- Test: `tests/test_pipeline_algorithm_warning.py`（新建）

**Interfaces:**
- Consumes: `KmboxController.algorithm_warnings: list[str]`、`pipeline._text(config, zh, en)`
- Produces: `pipeline._algorithm_warning_text(config: AppConfig, warnings: list[str]) -> str`（无警告时返回 `""`）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_pipeline_algorithm_warning.py`：

```python
from __future__ import annotations

import unittest

from dataclasses import replace

from rhodes_fast.config import AppConfig, UiConfig, default_config
from rhodes_fast.pipeline import _algorithm_warning_text


class AlgorithmWarningTextTests(unittest.TestCase):
    def _config(self, language: str) -> AppConfig:
        # AppConfig 的 7 个字段全是必填, 只能从 default_config() 改一处出来。
        return replace(default_config(), ui=UiConfig(language=language))

    def test_no_warnings_produces_nothing_to_print(self) -> None:
        self.assertEqual(_algorithm_warning_text(self._config("zh"), []), "")

    def test_each_warning_gets_its_own_marked_line(self) -> None:
        text = _algorithm_warning_text(self._config("zh"), ["算法 a 找不到", "算法 b 找不到"])
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("算法 a 找不到", lines[0])
        self.assertIn("算法 b 找不到", lines[1])
        # 必须显眼: 这行混在一堆启动信息里, 不标出来等于没打印。
        for line in lines:
            self.assertTrue(line.startswith("!!"), line)

    def test_english_console_still_gets_the_algorithm_name(self) -> None:
        text = _algorithm_warning_text(self._config("en"), ["算法 my_kalman 找不到，已回退到 p"])
        self.assertIn("my_kalman", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_pipeline_algorithm_warning -v`
Expected: FAIL — `ImportError: cannot import name '_algorithm_warning_text' from 'rhodes_fast.pipeline'`

（已核对：`UiConfig` 只有 `language` 一个字段，`default_config()` 可用，
`BUILTIN_ALGORITHMS` 有 5 个：`p` / `pd` / `feedforward` / `inflight` / `inflight_ff`。）

- [ ] **Step 3: 实现**

在 `rhodes_fast/pipeline.py` 里 `_latency_report` 定义之前加：

```python
def _algorithm_warning_text(config: AppConfig, warnings: list[str]) -> str:
    """把控制算法的回退警告排成醒目的几行。

    回退本身是有意的——启动失败会让人在游戏里才发现自瞄整个不工作, 更糟。
    但回退必须响亮: 用户选的算法没生效, 手感和他预期的完全是两回事。
    """
    if not warnings:
        return ""
    return "\n".join(
        _text(config, f"!! {warning}", f"!! {warning} (falling back to p)")
        for warning in warnings
    )
```

- [ ] **Step 4: 接到两处构造点**

`rhodes_fast/pipeline.py` 里搜 `KmboxController(` 共两处（约 220 行的 `run_pipeline`，
以及约 620 行的另一处）。在每一处 `controller = KmboxController(...)` 的**下一行**插入：

```python
    warning_text = _algorithm_warning_text(config, controller.algorithm_warnings)
    if warning_text:
        print(warning_text)
```

- [ ] **Step 5: 跑测试**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `OK`，159 + 3 = 162 个。

- [ ] **Step 6: 提交**

```bash
git add tests/test_pipeline_algorithm_warning.py rhodes_fast/pipeline.py
git commit -m "fix: actually print the algorithm fallback warning

算法找不到时回退到 p 的警告一直收集在 algorithm_warnings 里, 但没有任何地方
读它——用户选的算法没生效, 而他在启动信息里看不到半个字。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 记下本机实测回路延迟

分享调校时要带上作者机器的实测回路延迟，导入方才能判断「扣在途」「前馈」的补偿量适不适合自己。
仿真显示假设 L=8 而真实 L=4 时稳定帧数从 15 涨到 35，这个比对不是锦上添花。
目前实测值只在停止时打印一次就没了，需要落盘。

**Files:**
- Modify: `rhodes_fast/latency_log.py`（新增 `MEASUREMENT_NAME`、`save_measurement`、`load_measurement`）
- Modify: `rhodes_fast/pipeline.py`（`_latency_report` 里算出 estimate 后落盘）
- Modify: `.gitignore`
- Test: `tests/test_latency_measurement.py`（新建）

**Interfaces:**
- Consumes: `latency_log.LoopDelayEstimate`（字段 `frames`、`correlation`、`runner_up_frames`、`runner_up_correlation`、`frame_interval_ms`、`loop_ms`、`ai_side_ms`、`remote_ms`、`samples`、`command_threshold`）
- Produces:
  - `latency_log.MEASUREMENT_NAME: str = ".loop-latency.json"`
  - `latency_log.save_measurement(path: Path, estimate: LoopDelayEstimate) -> None`
  - `latency_log.load_measurement(path: Path) -> float | None`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_latency_measurement.py`：

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhodes_fast.latency_log import (
    LoopDelayEstimate,
    load_measurement,
    save_measurement,
)


def _estimate(loop_ms: float) -> LoopDelayEstimate:
    return LoopDelayEstimate(
        frames=8,
        correlation=0.71,
        runner_up_frames=7,
        runner_up_correlation=0.70,
        frame_interval_ms=4.16,
        loop_ms=loop_ms,
        ai_side_ms=4.4,
        remote_ms=22.6,
        samples=80405,
        command_threshold=3.0,
    )


class MeasurementTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".loop-latency.json"
            save_measurement(path, _estimate(33.3))
            self.assertAlmostEqual(load_measurement(path), 33.3, places=3)

    def test_missing_file_reads_as_no_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(load_measurement(Path(folder) / "nope.json"))

    def test_corrupt_file_reads_as_no_measurement_rather_than_raising(self) -> None:
        # 这个文件只是个便利, 坏了不该让导出功能整个失败。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".loop-latency.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_measurement(path))

    def test_a_later_run_replaces_the_earlier_number(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".loop-latency.json"
            save_measurement(path, _estimate(33.3))
            save_measurement(path, _estimate(25.0))
            self.assertAlmostEqual(load_measurement(path), 25.0, places=3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_latency_measurement -v`
Expected: FAIL — `ImportError: cannot import name 'load_measurement'`

- [ ] **Step 3: 实现**

在 `rhodes_fast/latency_log.py` 末尾加（文件顶部若无 `import json` 则补上）：

```python
MEASUREMENT_NAME = ".loop-latency.json"


def save_measurement(path: Path, estimate: LoopDelayEstimate) -> None:
    """把最近一次实测的回路延迟记下来, 供分享调校时比对。

    只有 loop_ms 是必须的, 其余几项是为了人打开这个文件时能看懂。
    """
    payload = {
        "loop_ms": round(estimate.loop_ms, 2),
        "frames": estimate.frames,
        "frame_interval_ms": round(estimate.frame_interval_ms, 3),
        "correlation": round(estimate.correlation, 4),
        "samples": estimate.samples,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 记不下来就算了, 不值得让一局打完的日志汇总因此报错。
        pass


def load_measurement(path: Path) -> float | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = payload.get("loop_ms") if isinstance(payload, dict) else None
    if not isinstance(value, (int, float)):
        return None
    return float(value)
```

- [ ] **Step 4: 接到管线**

`rhodes_fast/pipeline.py` 的 `_latency_report` 里，在 `if estimate is None:` 那个分支
`return` 之后、计算 `trust` 之前插入：

```python
    save_measurement(writer.path.parent / MEASUREMENT_NAME, estimate)
```

并把 `pipeline.py` 顶部的 latency_log 导入改为：

```python
from .latency_log import (
    MEASUREMENT_NAME,
    LatencyLogWriter,
    LatencySample,
    estimate_loop_delay,
    read_latency_log,
    save_measurement,
)
```

- [ ] **Step 5: 忽略这个运行时文件**

在 `.gitignore` 的「延迟日志」那一段下面加：

```
.loop-latency.json
```

- [ ] **Step 6: 跑测试并提交**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `OK`，166 个。

```bash
git add tests/test_latency_measurement.py rhodes_fast/latency_log.py rhodes_fast/pipeline.py .gitignore
git commit -m "feat: remember this machine's measured loop delay

分享调校时要带上作者机器的实测回路延迟, 导入方才能判断扣在途和前馈的补偿量
适不适合自己——仿真里假设 L=8 而真实 L=4, 稳定帧数从 15 涨到 35。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: 调校 JSON 的读写

**Files:**
- Create: `rhodes_fast/tuning_share.py`
- Test: `tests/test_tuning_share.py`（新建）

**Interfaces:**
- Consumes: `config.AimProfileConfig`（字段 `enabled`、`trigger`、`kp_min`、`kp_max`、`kp_growth`、`target_class`、`target_y_ratio`、`fov_radius`、`algorithm`、`algorithm_params`）
- Produces:
  - `tuning_share.FORMAT: int = 1`
  - `tuning_share.TuningError(ValueError)`
  - `tuning_share.TuningPreset`（frozen dataclass：`algorithm`、`params`、`kp_min`、`kp_max`、`kp_growth`、`target_y_ratio`、`fov_radius`、`measured_loop_ms`）
  - `dump_preset(profile: AimProfileConfig, *, measured_loop_ms: float | None = None) -> str`
  - `load_preset(text: str, *, known_algorithms: Collection[str] | None = None) -> TuningPreset`
  - `apply_preset(profile: AimProfileConfig, preset: TuningPreset) -> AimProfileConfig`
  - `delay_warning(preset_ms: float | None, local_ms: float | None, frame_interval_ms: float = 4.16) -> str | None`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tuning_share.py`：

```python
from __future__ import annotations

import json
import unittest

from rhodes_fast.config import AimProfileConfig
from rhodes_fast.tuning_share import (
    TuningError,
    apply_preset,
    delay_warning,
    dump_preset,
    load_preset,
)


def _profile() -> AimProfileConfig:
    return AimProfileConfig(
        enabled=True,
        trigger="side1",
        kp_min=0.14,
        kp_max=0.35,
        kp_growth=0.031,
        target_class=3,
        target_y_ratio=0.08,
        fov_radius=150.0,
        algorithm="inflight_ff",
        algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25},
    )


class DumpTests(unittest.TestCase):
    def test_round_trip_preserves_every_feel_setting(self) -> None:
        preset = load_preset(dump_preset(_profile()))
        self.assertEqual(preset.algorithm, "inflight_ff")
        self.assertEqual(preset.params["loop_delay_frames"], 8.0)
        self.assertAlmostEqual(preset.kp_max, 0.35)
        self.assertAlmostEqual(preset.kp_growth, 0.031)
        self.assertAlmostEqual(preset.target_y_ratio, 0.08)
        self.assertAlmostEqual(preset.fov_radius, 150.0)

    def test_trigger_and_target_class_are_left_out_on_purpose(self) -> None:
        # 按键习惯因人而异, 目标标签取决于用哪个模型。
        payload = json.loads(dump_preset(_profile()))
        self.assertNotIn("trigger", payload)
        self.assertNotIn("target_class", payload)
        self.assertNotIn("enabled", payload)

    def test_measured_delay_is_included_only_when_known(self) -> None:
        self.assertNotIn("measured_loop_ms", json.loads(dump_preset(_profile())))
        payload = json.loads(dump_preset(_profile(), measured_loop_ms=33.3))
        self.assertAlmostEqual(payload["measured_loop_ms"], 33.3)


class LoadTests(unittest.TestCase):
    def test_unknown_format_version_is_an_error_not_a_guess(self) -> None:
        text = json.dumps({"format": 99, "algorithm": "p", "params": {}})
        with self.assertRaises(TuningError) as caught:
            load_preset(text)
        self.assertIn("99", str(caught.exception))

    def test_missing_algorithm_is_named_in_the_error(self) -> None:
        with self.assertRaises(TuningError) as caught:
            load_preset(dump_preset(_profile()), known_algorithms={"p", "pd"})
        self.assertIn("inflight_ff", str(caught.exception))

    def test_a_known_algorithm_passes_the_same_check(self) -> None:
        preset = load_preset(dump_preset(_profile()), known_algorithms={"p", "inflight_ff"})
        self.assertEqual(preset.algorithm, "inflight_ff")

    def test_garbage_text_is_an_error_not_a_crash(self) -> None:
        with self.assertRaises(TuningError):
            load_preset("this is not json")

    def test_a_missing_field_is_named(self) -> None:
        payload = json.loads(dump_preset(_profile()))
        del payload["kp_growth"]
        with self.assertRaises(TuningError) as caught:
            load_preset(json.dumps(payload))
        self.assertIn("kp_growth", str(caught.exception))

    def test_a_non_numeric_value_is_rejected(self) -> None:
        payload = json.loads(dump_preset(_profile()))
        payload["kp_max"] = "很大"
        with self.assertRaises(TuningError):
            load_preset(json.dumps(payload))


class ApplyTests(unittest.TestCase):
    def test_apply_overwrites_feel_but_keeps_local_habits(self) -> None:
        mine = AimProfileConfig(
            enabled=False, trigger="right", target_class=0, algorithm="p", algorithm_params={}
        )
        updated = apply_preset(mine, load_preset(dump_preset(_profile())))
        self.assertEqual(updated.algorithm, "inflight_ff")
        self.assertAlmostEqual(updated.kp_max, 0.35)
        # 触发键、目标标签、启用状态是本机的事, 别人的文件不该动它们。
        self.assertEqual(updated.trigger, "right")
        self.assertEqual(updated.target_class, 0)
        self.assertFalse(updated.enabled)


class DelayWarningTests(unittest.TestCase):
    def test_no_warning_when_the_two_machines_are_within_a_frame(self) -> None:
        self.assertIsNone(delay_warning(33.3, 31.0))

    def test_no_warning_when_either_side_is_unknown(self) -> None:
        self.assertIsNone(delay_warning(None, 31.0))
        self.assertIsNone(delay_warning(33.3, None))

    def test_a_gap_over_a_frame_names_both_numbers(self) -> None:
        message = delay_warning(33.3, 16.0)
        self.assertIsNotNone(message)
        self.assertIn("33.3", message)
        self.assertIn("16.0", message)

    def test_the_warning_fires_in_both_directions(self) -> None:
        self.assertIsNotNone(delay_warning(16.0, 33.3))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_tuning_share -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rhodes_fast.tuning_share'`

- [ ] **Step 3: 实现**

新建 `rhodes_fast/tuning_share.py`：

```python
"""调校的导出与导入。

这里从头到尾只搬数据, 不 import 任何东西、不执行任何东西。别人发来的调校文件
最坏的情况只是一组难用的数字, 不可能是一段代码——算法实现走 algorithm_library
那条完全独立的路, 那条路上每一步都要用户点头。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Collection

from .config import AimProfileConfig

FORMAT = 1

_NUMBERS = ("kp_min", "kp_max", "kp_growth", "target_y_ratio", "fov_radius")
_REQUIRED = ("algorithm", "params", *_NUMBERS)


class TuningError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TuningPreset:
    algorithm: str
    params: dict[str, float]
    kp_min: float
    kp_max: float
    kp_growth: float
    target_y_ratio: float
    fov_radius: float
    measured_loop_ms: float | None = None


def dump_preset(profile: AimProfileConfig, *, measured_loop_ms: float | None = None) -> str:
    payload: dict[str, object] = {
        "format": FORMAT,
        "algorithm": profile.algorithm,
        # 字典而不是固定字段: 插件自己声明参数, 这里不需要知道有哪些。
        "params": {name: float(value) for name, value in sorted(profile.algorithm_params.items())},
    }
    for key in _NUMBERS:
        payload[key] = float(getattr(profile, key))
    if measured_loop_ms is not None:
        payload["measured_loop_ms"] = round(float(measured_loop_ms), 2)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def load_preset(text: str, *, known_algorithms: Collection[str] | None = None) -> TuningPreset:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise TuningError(f"这不是一份合法的调校文件：{error}") from error
    if not isinstance(payload, dict):
        raise TuningError("调校文件的最外层必须是一个对象。")

    version = payload.get("format")
    if version != FORMAT:
        raise TuningError(f"不认识的调校格式版本 {version!r}，本程序只认 {FORMAT}。")

    missing = [key for key in _REQUIRED if key not in payload]
    if missing:
        raise TuningError("调校文件缺少这些字段：" + "、".join(missing))

    algorithm = payload["algorithm"]
    if not isinstance(algorithm, str) or not algorithm:
        raise TuningError("algorithm 必须是算法标识字符串。")
    if known_algorithms is not None and algorithm not in known_algorithms:
        # 静默回退到默认算法会让人以为在用对方的调校, 实际手感完全是另一回事。
        raise TuningError(f"这份调校需要算法「{algorithm}」，你还没装。先去算法库里导入它。")

    raw_params = payload["params"]
    if not isinstance(raw_params, dict):
        raise TuningError("params 必须是一个对象。")

    try:
        params = {str(name): float(value) for name, value in raw_params.items()}
        numbers = {key: float(payload[key]) for key in _NUMBERS}
    except (TypeError, ValueError) as error:
        raise TuningError(f"调校文件里有不能当数字用的值：{error}") from error

    measured = payload.get("measured_loop_ms")
    if not isinstance(measured, (int, float)):
        measured = None

    return TuningPreset(
        algorithm=algorithm,
        params=params,
        measured_loop_ms=None if measured is None else float(measured),
        **numbers,
    )


def apply_preset(profile: AimProfileConfig, preset: TuningPreset) -> AimProfileConfig:
    """只覆盖手感那几项。

    trigger / target_class / enabled 是本机的习惯和本机用的模型决定的, 别人的
    文件不该动它们。
    """
    return replace(
        profile,
        algorithm=preset.algorithm,
        algorithm_params=dict(preset.params),
        kp_min=preset.kp_min,
        kp_max=preset.kp_max,
        kp_growth=preset.kp_growth,
        target_y_ratio=preset.target_y_ratio,
        fov_radius=preset.fov_radius,
    )


def delay_warning(
    preset_ms: float | None, local_ms: float | None, frame_interval_ms: float = 4.16
) -> str | None:
    """作者和本机的回路延迟差一帧以上就提醒。

    「扣在途」和「速度前馈」的补偿量是按作者那台机器的延迟调的。差一帧就够把
    补偿量从刚好变成过冲或不足。
    """
    if preset_ms is None or local_ms is None:
        return None
    gap = abs(preset_ms - local_ms)
    if gap < frame_interval_ms:
        return None
    return (
        f"作者机器的回路延迟是 {preset_ms:.1f} 毫秒，你这台是 {local_ms:.1f} 毫秒，"
        f"差 {gap:.1f} 毫秒（约 {gap / frame_interval_ms:.1f} 帧）。"
        "扣在途和前馈的补偿量是按作者的延迟调的，套用后可能过冲或不足，"
        "手感不对就先动 loop_delay_frames。"
    )
```

- [ ] **Step 4: 跑测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_tuning_share -v`
Expected: 全部 PASS（15 个）

- [ ] **Step 5: 变异验证**

在 `apply_preset` 的 `replace(...)` 参数里**临时**加一行 `trigger="left",`，跑
`.venv/Scripts/python.exe -m unittest tests.test_tuning_share.ApplyTests -v`，
必须看到 `test_apply_overwrites_feel_but_keeps_local_habits` 变红。确认后把这行删掉。

- [ ] **Step 6: 全量测试并提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
git add tests/test_tuning_share.py rhodes_fast/tuning_share.py
git commit -m "feat: read and write shareable tuning files

调校文件只搬数字, 从头到尾不 import 也不执行任何东西。算法名解析不到就明确
报错, 不静默回退——静默回退会让人以为在用对方的调校, 实际手感完全是另一回事。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 算法库的注册表与改名、删除

**Files:**
- Create: `rhodes_fast/algorithm_library.py`（本任务只写注册表那半）
- Test: `tests/test_algorithm_library_registry.py`（新建）

**Interfaces:**
- Consumes: `aim_algorithms.builtin.BUILTIN_ALGORITHMS`
- Produces:
  - `algorithm_library.REGISTRY_NAME: str = "installed.json"`
  - `algorithm_library.LibraryError(RuntimeError)`
  - `algorithm_library.InstalledAlgorithm`（frozen dataclass：`name`、`display_name`、`source_file`、`imported_at`）
  - `builtin_names() -> set[str]`
  - `registry_path(directory: Path) -> Path`
  - `read_registry(directory: Path) -> dict[str, InstalledAlgorithm]`
  - `write_registry(directory: Path, entries: dict[str, InstalledAlgorithm]) -> None`
  - `rename(directory: Path, name: str, display_name: str) -> None`
  - `uninstall(directory: Path, name: str) -> None`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_algorithm_library_registry.py`：

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rhodes_fast.algorithm_library import (
    InstalledAlgorithm,
    LibraryError,
    read_registry,
    rename,
    uninstall,
    write_registry,
)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.directory = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _seed(self) -> InstalledAlgorithm:
        entry = InstalledAlgorithm(
            name="my_kalman",
            display_name="卡尔曼（老王版）",
            source_file="my_kalman.py",
            imported_at="2026-09-11 20:00",
        )
        (self.directory / entry.source_file).write_text("# stub\n", encoding="utf-8")
        write_registry(self.directory, {entry.name: entry})
        return entry

    def test_an_empty_directory_reads_as_an_empty_registry(self) -> None:
        self.assertEqual(read_registry(self.directory), {})

    def test_round_trip(self) -> None:
        entry = self._seed()
        self.assertEqual(read_registry(self.directory), {"my_kalman": entry})

    def test_a_corrupt_registry_reads_as_empty_rather_than_raising(self) -> None:
        # 注册表坏了不该让整个界面起不来。
        (self.directory / "installed.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(read_registry(self.directory), {})

    def test_a_row_missing_fields_is_skipped_not_fatal(self) -> None:
        payload = {"format": 1, "algorithms": [{"name": "broken"}]}
        (self.directory / "installed.json").write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(read_registry(self.directory), {})

    def test_rename_changes_the_display_name_only(self) -> None:
        self._seed()
        rename(self.directory, "my_kalman", "卡尔曼 v2")
        entry = read_registry(self.directory)["my_kalman"]
        self.assertEqual(entry.display_name, "卡尔曼 v2")
        # 标识不能动: 改了 NAME, 别人发来的调校就对不上。
        self.assertEqual(entry.name, "my_kalman")
        self.assertEqual(entry.source_file, "my_kalman.py")

    def test_rename_refuses_a_blank_display_name(self) -> None:
        self._seed()
        with self.assertRaises(LibraryError):
            rename(self.directory, "my_kalman", "   ")

    def test_rename_refuses_a_builtin(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            rename(self.directory, "p", "我的比例控制")
        self.assertIn("内置", str(caught.exception))

    def test_uninstall_removes_both_the_file_and_the_row(self) -> None:
        entry = self._seed()
        uninstall(self.directory, "my_kalman")
        self.assertEqual(read_registry(self.directory), {})
        self.assertFalse((self.directory / entry.source_file).exists())

    def test_uninstall_refuses_a_builtin(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            uninstall(self.directory, "p")
        self.assertIn("内置", str(caught.exception))

    def test_uninstall_survives_a_file_that_is_already_gone(self) -> None:
        entry = self._seed()
        (self.directory / entry.source_file).unlink()
        uninstall(self.directory, "my_kalman")
        self.assertEqual(read_registry(self.directory), {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_algorithm_library_registry -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rhodes_fast.algorithm_library'`

- [ ] **Step 3: 实现**

新建 `rhodes_fast/algorithm_library.py`：

```python
"""用户自己写的控制算法: 安装、改名、删除、加载。

安全边界是流程性的, 不是技术性的: 用户装的是一段会跑起来的 Python, 和双击一个
.py 没有区别。这个模块能保证的是——在用户看清作者、文件名、源码并点头之前,
那个文件一行都不会执行。

内置算法不在这里。它们随程序分发, 既不出现在 algorithms/ 目录也不进注册表,
因此不可改名、不可删除。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .aim_algorithms.builtin import BUILTIN_ALGORITHMS

REGISTRY_NAME = "installed.json"


class LibraryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstalledAlgorithm:
    name: str
    display_name: str
    source_file: str
    imported_at: str


def builtin_names() -> set[str]:
    return {algorithm.NAME for algorithm in BUILTIN_ALGORITHMS}


def registry_path(directory: Path) -> Path:
    return directory / REGISTRY_NAME


def read_registry(directory: Path) -> dict[str, InstalledAlgorithm]:
    try:
        payload = json.loads(registry_path(directory).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # 注册表坏了或者还没有, 都当成空的。让界面起不来是更糟的失败。
        return {}
    if not isinstance(payload, dict):
        return {}
    entries: dict[str, InstalledAlgorithm] = {}
    for row in payload.get("algorithms", []):
        if not isinstance(row, dict):
            continue
        try:
            entry = InstalledAlgorithm(
                name=str(row["name"]),
                display_name=str(row["display_name"]),
                source_file=str(row["source_file"]),
                imported_at=str(row.get("imported_at", "")),
            )
        except KeyError:
            continue
        entries[entry.name] = entry
    return entries


def write_registry(directory: Path, entries: dict[str, InstalledAlgorithm]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"format": 1, "algorithms": [asdict(entry) for entry in entries.values()]}
    registry_path(directory).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def rename(directory: Path, name: str, display_name: str) -> None:
    """只改显示名。

    .py 里的 NAME 是标识, 动不得——别人发来的调校认的就是它。
    """
    display_name = display_name.strip()
    if not display_name:
        raise LibraryError("显示名不能是空的。")
    entries = read_registry(directory)
    if name not in entries:
        if name in builtin_names():
            raise LibraryError(f"「{name}」是内置算法，不能改名。")
        raise LibraryError(f"算法库里没有「{name}」。")
    entries[name] = replace(entries[name], display_name=display_name)
    write_registry(directory, entries)


def uninstall(directory: Path, name: str) -> None:
    entries = read_registry(directory)
    entry = entries.pop(name, None)
    if entry is None:
        if name in builtin_names():
            raise LibraryError(f"「{name}」是内置算法，不能删除。")
        raise LibraryError(f"算法库里没有「{name}」。")
    (directory / entry.source_file).unlink(missing_ok=True)
    write_registry(directory, entries)
```

- [ ] **Step 4: 跑测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_algorithm_library_registry -v`
Expected: 全部 PASS（11 个）

- [ ] **Step 5: 全量测试并提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
git add tests/test_algorithm_library_registry.py rhodes_fast/algorithm_library.py
git commit -m "feat: keep a registry of installed algorithms

改名只改注册表里的显示名, 不动 .py 里的 NAME——别人发来的调校认的是 NAME,
改了就对不上。内置算法不进注册表, 因此改名和删除都会被拒。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: 先看后装——候选检查与安装校验

这一步是整个导入流程的安全基点。`inspect_candidate` 只读文本再用 `ast` 看一眼，
**不执行里面任何一行**；真正的 import 发生在 `install`，而 `install` 只在用户点了
「确定导入」之后才被调用。

**Files:**
- Modify: `rhodes_fast/algorithm_library.py`
- Test: `tests/test_algorithm_library_install.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 `read_registry` / `write_registry` / `builtin_names` / `InstalledAlgorithm` / `LibraryError`
- Produces:
  - `algorithm_library.DuplicateAlgorithm(LibraryError)`（属性 `name`、`existing_file`）
  - `algorithm_library.Candidate`（frozen dataclass：`path`、`size_bytes`、`name`、`display_name`、`author`、`source`）
  - `inspect_candidate(path: Path) -> Candidate`
  - `install(directory: Path, source_path: Path, *, replace_existing: bool = False, now: datetime | None = None) -> InstalledAlgorithm`
  - `load_installed(directory: Path) -> tuple[dict[str, type], list[str]]`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_algorithm_library_install.py`：

```python
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from rhodes_fast.algorithm_library import (
    Candidate,
    DuplicateAlgorithm,
    LibraryError,
    inspect_candidate,
    install,
    load_installed,
    read_registry,
    rename,
)

GOOD = '''
__author__ = "老王"

from rhodes_fast.aim_algorithms import Param


class MyKalman:
    NAME = "my_kalman"
    DISPLAY_NAME = "卡尔曼（老王版）"
    PARAMS = (Param("q", 0.1, 0.0, 1.0, "过程噪声"),)

    def __init__(self, params):
        self.q = params["q"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.q, observation.error_y * self.q
'''

SIDE_EFFECT = '''
raise RuntimeError("这个文件一跑起来就炸")


class Boom:
    NAME = "boom"
    DISPLAY_NAME = "会炸的"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, observation):
        return 0.0, 0.0
'''

NO_CLASS = '''
NAME = "not_a_class"
'''

BAD_SIGNATURE = '''
class WrongShape:
    NAME = "wrong_shape"
    DISPLAY_NAME = "签名不对"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, error_x, error_y):
        return 0.0, 0.0
'''

COLLIDES_WITH_BUILTIN = '''
class Impostor:
    NAME = "p"
    DISPLAY_NAME = "假冒比例控制"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, observation):
        return 0.0, 0.0
'''


class LibraryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._library = tempfile.TemporaryDirectory()
        self._incoming = tempfile.TemporaryDirectory()
        self.directory = Path(self._library.name)
        self.incoming = Path(self._incoming.name)

    def tearDown(self) -> None:
        self._library.cleanup()
        self._incoming.cleanup()

    def _drop(self, name: str, source: str) -> Path:
        path = self.incoming / name
        path.write_text(source, encoding="utf-8")
        return path


class InspectTests(LibraryTestCase):
    def test_inspect_reports_what_the_confirmation_box_needs(self) -> None:
        candidate = inspect_candidate(self._drop("my_kalman.py", GOOD))
        self.assertIsInstance(candidate, Candidate)
        self.assertEqual(candidate.name, "my_kalman")
        self.assertEqual(candidate.display_name, "卡尔曼（老王版）")
        self.assertEqual(candidate.author, "老王")
        self.assertGreater(candidate.size_bytes, 0)
        self.assertIn("class MyKalman", candidate.source)

    def test_inspect_does_not_execute_the_file(self) -> None:
        # 这个文件在模块层就 raise。能读出信息就证明它没有被执行过。
        candidate = inspect_candidate(self._drop("boom.py", SIDE_EFFECT))
        self.assertEqual(candidate.name, "boom")

    def test_an_unsigned_file_still_inspects(self) -> None:
        source = GOOD.replace('__author__ = "老王"', "")
        self.assertEqual(inspect_candidate(self._drop("a.py", source)).author, "未署名")

    def test_a_file_that_is_not_python_is_rejected_before_anything_else(self) -> None:
        with self.assertRaises(LibraryError):
            inspect_candidate(self._drop("broken.py", "def (((("))

    def test_a_missing_file_is_an_error(self) -> None:
        with self.assertRaises(LibraryError):
            inspect_candidate(self.incoming / "nope.py")


class InstallTests(LibraryTestCase):
    def test_a_good_algorithm_lands_in_the_library_and_the_registry(self) -> None:
        entry = install(
            self.directory,
            self._drop("my_kalman.py", GOOD),
            now=datetime(2026, 9, 11, 20, 0),
        )
        self.assertEqual(entry.name, "my_kalman")
        self.assertEqual(entry.display_name, "卡尔曼（老王版）")
        self.assertEqual(entry.imported_at, "2026-09-11 20:00")
        self.assertTrue((self.directory / entry.source_file).is_file())
        self.assertIn("my_kalman", read_registry(self.directory))

    def test_a_file_that_blows_up_on_import_is_deleted_again(self) -> None:
        with self.assertRaises(LibraryError):
            install(self.directory, self._drop("boom.py", SIDE_EFFECT))
        self.assertEqual(list(self.directory.glob("*.py")), [])
        self.assertEqual(read_registry(self.directory), {})

    def test_a_file_with_no_algorithm_class_is_refused(self) -> None:
        with self.assertRaises(LibraryError):
            install(self.directory, self._drop("nope.py", NO_CLASS))

    def test_a_wrong_compute_signature_is_refused_and_says_so(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("wrong.py", BAD_SIGNATURE))
        self.assertIn("compute", str(caught.exception))
        self.assertEqual(list(self.directory.glob("*.py")), [])

    def test_a_name_that_collides_with_a_builtin_is_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("impostor.py", COLLIDES_WITH_BUILTIN))
        self.assertIn("内置", str(caught.exception))

    def test_a_duplicate_name_is_reported_not_silently_overwritten(self) -> None:
        install(self.directory, self._drop("my_kalman.py", GOOD))
        with self.assertRaises(DuplicateAlgorithm) as caught:
            install(self.directory, self._drop("my_kalman.py", GOOD))
        self.assertEqual(caught.exception.name, "my_kalman")
        self.assertEqual(caught.exception.existing_file, "my_kalman.py")

    def test_replacing_on_purpose_keeps_the_name_the_user_gave_it(self) -> None:
        install(self.directory, self._drop("my_kalman.py", GOOD))
        rename(self.directory, "my_kalman", "我改过的名字")
        install(self.directory, self._drop("my_kalman.py", GOOD), replace_existing=True)
        self.assertEqual(read_registry(self.directory)["my_kalman"].display_name, "我改过的名字")


class LoadTests(LibraryTestCase):
    def test_installed_algorithms_load_back_under_their_renamed_display_name(self) -> None:
        install(self.directory, self._drop("my_kalman.py", GOOD))
        rename(self.directory, "my_kalman", "卡尔曼 v2")
        algorithms, warnings = load_installed(self.directory)
        self.assertEqual(warnings, [])
        self.assertIn("my_kalman", algorithms)
        self.assertEqual(algorithms["my_kalman"].DISPLAY_NAME, "卡尔曼 v2")
        # 标识不变: 别人发来的调校认的是 NAME。
        self.assertEqual(algorithms["my_kalman"].NAME, "my_kalman")

    def test_a_registry_row_whose_file_vanished_warns_instead_of_crashing(self) -> None:
        entry = install(self.directory, self._drop("my_kalman.py", GOOD))
        (self.directory / entry.source_file).unlink()
        algorithms, warnings = load_installed(self.directory)
        self.assertEqual(algorithms, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("卡尔曼", warnings[0])

    def test_an_empty_library_loads_nothing_without_complaining(self) -> None:
        self.assertEqual(load_installed(self.directory), ({}, []))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_algorithm_library_install -v`
Expected: FAIL — `ImportError: cannot import name 'Candidate' from 'rhodes_fast.algorithm_library'`

- [ ] **Step 3: 补导入与两个新类型**

把 `rhodes_fast/algorithm_library.py` 顶部的导入区改成：

```python
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from .aim_algorithms.builtin import BUILTIN_ALGORITHMS

REGISTRY_NAME = "installed.json"
_MAX_SOURCE_BYTES = 256 * 1024
```

在 `class LibraryError` 之后加：

```python
class DuplicateAlgorithm(LibraryError):
    def __init__(self, name: str, existing_file: str) -> None:
        super().__init__(f"已存在同名算法「{name}」（来自 {existing_file}）。")
        self.name = name
        self.existing_file = existing_file


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    size_bytes: int
    name: str
    display_name: str
    author: str
    source: str
```

- [ ] **Step 4: 实现「只看不跑」的检查**

在 `rhodes_fast/algorithm_library.py` 末尾加：

```python
def inspect_candidate(path: Path) -> Candidate:
    """读文本 + ast 看一眼, 从头到尾不执行里面任何一行。

    这是整个导入流程的安全基点: 在用户看到作者、大小、源码并点头之前, 这个文件
    不该有机会跑起来。真正的 import 在 install 里, 而 install 只在用户确认之后调用。
    """
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise LibraryError("这个文件不是 UTF-8 文本，不像是算法源码。") from error
    except OSError as error:
        raise LibraryError(f"读不了这个文件：{error}") from error

    size_bytes = len(source.encode("utf-8"))
    if size_bytes > _MAX_SOURCE_BYTES:
        raise LibraryError("算法源码超过 256 KB，多半不是一份算法实现。")

    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise LibraryError(f"这个文件不是合法的 Python：{error}") from error

    name = ""
    display_name = ""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields = _class_strings(node)
        if fields.get("NAME"):
            name = fields["NAME"]
            display_name = fields.get("DISPLAY_NAME") or name
            break

    return Candidate(
        path=path,
        size_bytes=size_bytes,
        name=name,
        display_name=display_name,
        author=_module_string(tree, "__author__") or "未署名",
        source=source,
    )


def _class_strings(node: ast.ClassDef) -> dict[str, str]:
    found: dict[str, str] = {}
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            targets = [statement.target.id]
        else:
            continue
        value = statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                found[target] = value.value
    return found


def _module_string(tree: ast.Module, name: str) -> str:
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return value.value
    return ""
```

- [ ] **Step 5: 实现导入校验与安装**

继续在文件末尾加：

```python
def _safe_file_name(name: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in Path(name).stem).strip("_")
    if not stem or stem[0].isdigit():
        stem = "algo_" + stem
    return stem + ".py"


def _import_algorithm(path: Path) -> type:
    module_name = f"rhodes_fast_user_algorithms.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise LibraryError(f"加载不了 {path.name}。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # 用户代码什么都可能抛, 包括文件已经不在了
        sys.modules.pop(module_name, None)
        raise LibraryError(f"{path.name} 一加载就报错：{error}") from error
    try:
        return _validate(module, path.name)
    except LibraryError:
        sys.modules.pop(module_name, None)
        raise


def _validate(module, file_name: str) -> type:
    for value in vars(module).values():
        if not isinstance(value, type):
            continue
        name = getattr(value, "NAME", None)
        if not isinstance(name, str) or not name:
            continue
        if not hasattr(value, "PARAMS") or not isinstance(getattr(value, "DISPLAY_NAME", None), str):
            continue
        if not callable(getattr(value, "compute", None)) or not callable(getattr(value, "reset", None)):
            continue
        parameters = list(inspect.signature(value.compute).parameters)
        if parameters[:2] != ["self", "observation"]:
            raise LibraryError(
                f"{file_name} 里的 compute 签名必须是 compute(self, observation)，"
                f"现在是 compute({', '.join(parameters)})。"
            )
        return value
    raise LibraryError(
        f"{file_name} 里找不到合法的算法类。需要一个带 NAME、DISPLAY_NAME、PARAMS "
        "以及 reset / compute 方法的类。"
    )


def install(
    directory: Path,
    source_path: Path,
    *,
    replace_existing: bool = False,
    now: datetime | None = None,
) -> InstalledAlgorithm:
    """复制进算法库并 import 校验。**只应在用户确认之后调用。**"""
    candidate = inspect_candidate(source_path)
    if not candidate.name:
        raise LibraryError("源码里找不到带 NAME 的算法类。")
    if candidate.name in builtin_names():
        raise LibraryError(f"「{candidate.name}」是内置算法的标识，请让作者换一个 NAME。")

    entries = read_registry(directory)
    existing = entries.get(candidate.name)
    if existing is not None and not replace_existing:
        raise DuplicateAlgorithm(candidate.name, existing.source_file)

    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / _safe_file_name(source_path.name)
    destination.write_text(candidate.source, encoding="utf-8")
    try:
        algorithm = _import_algorithm(destination)
        if algorithm.NAME != candidate.name:
            raise LibraryError(
                f"源码里写的 NAME 是「{candidate.name}」，实际加载出来却是「{algorithm.NAME}」。"
            )
    except LibraryError:
        destination.unlink(missing_ok=True)
        raise

    if existing is not None and existing.source_file != destination.name:
        (directory / existing.source_file).unlink(missing_ok=True)

    entry = InstalledAlgorithm(
        name=algorithm.NAME,
        # 替换时保留用户自己起的名字, 别把他改过的名字打回作者的默认值。
        display_name=existing.display_name if existing is not None else candidate.display_name,
        source_file=destination.name,
        imported_at=(now or datetime.now()).strftime("%Y-%m-%d %H:%M"),
    )
    entries[entry.name] = entry
    write_registry(directory, entries)
    return entry


def load_installed(directory: Path) -> tuple[dict[str, type], list[str]]:
    algorithms: dict[str, type] = {}
    warnings: list[str] = []
    for entry in read_registry(directory).values():
        try:
            algorithm = _import_algorithm(directory / entry.source_file)
        except LibraryError as error:
            warnings.append(f"算法「{entry.display_name}」加载失败：{error}")
            continue
        # 改名只改显示名。NAME 留给别人发来的调校去认。
        algorithm.DISPLAY_NAME = entry.display_name
        algorithms[algorithm.NAME] = algorithm
    return algorithms, warnings
```

- [ ] **Step 6: 跑测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_algorithm_library_install -v`
Expected: 全部 PASS（15 个）

- [ ] **Step 7: 变异验证——证明「不执行」这条测得住**

把 `inspect_candidate` 临时改成在 `return Candidate(...)` 之前多一行
`_import_algorithm(path)`，跑
`.venv/Scripts/python.exe -m unittest tests.test_algorithm_library_install.InspectTests -v`，
必须看到 `test_inspect_does_not_execute_the_file` 变红。确认后改回来。
**这是这个模块唯一真正重要的性质，必须亲眼看到它测得住。**

- [ ] **Step 8: 全量测试并提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
git add tests/test_algorithm_library_install.py rhodes_fast/algorithm_library.py
git commit -m "feat: install user algorithms only after the user has looked

选文件时只读文本和 ast, 一行都不执行; import 校验发生在用户点过确认之后。
校验不过就把复制进来的文件删掉。和内置重名、和已装重名都明确报错, 不静默覆盖。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 把已装算法并进 `available_algorithms()`

**Files:**
- Modify: `rhodes_fast/aim_algorithms/registry.py`
- Modify: `rhodes_fast/aim_algorithms/__init__.py`
- Modify: `rhodes_fast/pipeline.py`
- Modify: `rhodes_fast/__main__.py`
- Modify: `rhodes_fast/gui.py`
- Modify: `.gitignore`
- Test: `tests/test_algorithm_registry_merge.py`（新建）

**Interfaces:**
- Produces:
  - `aim_algorithms.set_installed_algorithms(mapping: Mapping[str, type]) -> None`
  - `RhodesFastGui.algorithms_dir: Path`（Task 8 要用）
- Changed: `available_algorithms()` 返回内置 + 已装的合并结果；内置永不被顶掉。
- Changed: `run_pipeline(..., algorithms_dir: Path | None = None)`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_algorithm_registry_merge.py`：

```python
from __future__ import annotations

import unittest

from rhodes_fast.aim_algorithms import (
    Param,
    available_algorithms,
    create_algorithm,
    set_installed_algorithms,
)


class Fake:
    NAME = "fake_kalman"
    DISPLAY_NAME = "假卡尔曼"
    PARAMS = (Param("q", 0.5, 0.0, 1.0, "过程噪声"),)

    def __init__(self, params):
        self.q = params["q"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.q, observation.error_y * self.q


class Impostor:
    NAME = "p"
    DISPLAY_NAME = "假冒比例控制"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, observation):
        return 0.0, 0.0


class RegistryMergeTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_installed_algorithms({})

    def test_builtins_are_there_with_nothing_installed(self) -> None:
        set_installed_algorithms({})
        self.assertIn("p", available_algorithms())
        self.assertNotIn("fake_kalman", available_algorithms())

    def test_an_installed_algorithm_shows_up_and_can_be_constructed(self) -> None:
        set_installed_algorithms({Fake.NAME: Fake})
        self.assertIn("fake_kalman", available_algorithms())
        self.assertAlmostEqual(create_algorithm("fake_kalman", {"q": 0.25}).q, 0.25)

    def test_a_missing_param_falls_back_to_the_declared_default(self) -> None:
        set_installed_algorithms({Fake.NAME: Fake})
        self.assertAlmostEqual(create_algorithm("fake_kalman", {}).q, 0.5)

    def test_an_installed_algorithm_can_never_shadow_a_builtin(self) -> None:
        set_installed_algorithms({"p": Impostor})
        self.assertIsNot(available_algorithms()["p"], Impostor)

    def test_setting_the_installed_set_again_replaces_the_previous_one(self) -> None:
        set_installed_algorithms({Fake.NAME: Fake})
        set_installed_algorithms({})
        self.assertNotIn("fake_kalman", available_algorithms())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_algorithm_registry_merge -v`
Expected: FAIL — `ImportError: cannot import name 'set_installed_algorithms'`

- [ ] **Step 3: 实现合并**

把 `rhodes_fast/aim_algorithms/registry.py` 改成：

```python
from __future__ import annotations

from typing import Mapping

from .builtin import BUILTIN_ALGORITHMS
from .contract import Algorithm, resolve_params


class UnknownAlgorithm(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"未知的控制算法：{name}")
        self.name = name


# 用户自己装的算法。进程级的一份, 由界面和管线在启动时各填一次。
_INSTALLED: dict[str, type] = {}


def set_installed_algorithms(mapping: Mapping[str, type]) -> None:
    _INSTALLED.clear()
    _INSTALLED.update(mapping)


def available_algorithms() -> dict[str, type]:
    merged = dict(_INSTALLED)
    # 内置最后写入: 安装时已经挡掉了和内置重名的 NAME, 这里再兜一层, 内置永不被顶掉。
    merged.update({algorithm.NAME: algorithm for algorithm in BUILTIN_ALGORITHMS})
    return merged


def create_algorithm(name: str, params: Mapping[str, float]) -> Algorithm:
    algorithms = available_algorithms()
    if name not in algorithms:
        raise UnknownAlgorithm(name)
    factory = algorithms[name]
    return factory(resolve_params(factory.PARAMS, params))
```

把 `rhodes_fast/aim_algorithms/__init__.py` 改成：

```python
from .contract import Algorithm, Observation, Param, dynamic_kp, resolve_params
from .registry import (
    UnknownAlgorithm,
    available_algorithms,
    create_algorithm,
    set_installed_algorithms,
)

__all__ = [
    "Algorithm",
    "Observation",
    "Param",
    "UnknownAlgorithm",
    "available_algorithms",
    "create_algorithm",
    "dynamic_kp",
    "resolve_params",
    "set_installed_algorithms",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_algorithm_registry_merge -v`
Expected: 全部 PASS（5 个）

- [ ] **Step 5: 管线在启动时加载算法库**

`rhodes_fast/pipeline.py` 的 import 区加：

```python
from .aim_algorithms import set_installed_algorithms
from .algorithm_library import load_installed
```

给 `run_pipeline` 的签名末尾加一个参数：

```python
def run_pipeline(
    config: AppConfig,
    stop_file: Path | None = None,
    preview_port: int | None = None,
    preview_enable_file: Path | None = None,
    runtime_aim_file: Path | None = None,
    latency_log: Path | None = None,
    algorithms_dir: Path | None = None,
) -> None:
```

在函数开头 `configure_console_output()` 之后、加载模型之前插入：

```python
    if algorithms_dir is not None:
        installed, library_warnings = load_installed(algorithms_dir)
        set_installed_algorithms(installed)
        for warning in library_warnings:
            print(_text(config, f"!! {warning}", f"!! {warning}"))
```

**必须在构造 `KmboxController` 之前**——控制器在构造时就按名字查算法，晚一步就查不到。

- [ ] **Step 6: 入口传目录**

`rhodes_fast/__main__.py` 里调用 `run_pipeline(` 的那处（约 101 行），在参数列表末尾加：

```python
            algorithms_dir=args.config.parent / "algorithms",
```

- [ ] **Step 7: 界面也加载一次**

`rhodes_fast/gui.py` 的 import 区，把
`from .aim_algorithms import Param, available_algorithms` 改成：

```python
from .aim_algorithms import Param, available_algorithms, set_installed_algorithms
from .algorithm_library import load_installed
```

在 `RhodesFastGui.__init__` 里，`self.config = load_config(self.config_path, validate_model=False)`
之后、`self._create_variables()` 之前插入：

```python
        # 下拉框要能列出用户自己装的算法, 所以界面也得加载一次算法库。
        self.algorithms_dir = self.config_path.parent / "algorithms"
        installed, self.library_warnings = load_installed(self.algorithms_dir)
        set_installed_algorithms(installed)
```

在 `_build_ui` 末尾那句 `self._append_log(f"准备就绪...")` 之后加：

```python
        for warning in self.library_warnings:
            self._append_log(warning)
```

- [ ] **Step 8: 忽略算法目录，全量测试**

`.gitignore` 里加：

```
# 用户自己装的控制算法
algorithms/
```

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `OK`

再手动确认界面还能起来：

```bash
.venv/Scripts/python.exe -c "import pathlib; from rhodes_fast.gui import RhodesFastGui; app = RhodesFastGui(pathlib.Path('settings.example.txt')); app.root.update_idletasks(); print('ok', len(app.algorithm_combos)); app.root.destroy()"
```
Expected: `ok 2`

- [ ] **Step 9: 提交**

```bash
git add tests/test_algorithm_registry_merge.py rhodes_fast/aim_algorithms/ rhodes_fast/pipeline.py rhodes_fast/__main__.py rhodes_fast/gui.py .gitignore
git commit -m "feat: make installed algorithms selectable alongside the builtins

界面和管线在启动时各加载一次算法库。内置算法最后写入合并结果, 已装的永远顶
不掉它们——安装时已经挡过一次重名, 这里是第二层。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: 方案面板的导出/导入调校按钮

**Files:**
- Modify: `rhodes_fast/gui.py`
- Test: `tests/test_gui_tuning_buttons.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `dump_preset` / `load_preset` / `delay_warning` / `TuningError` / `TuningPreset`；Task 2 的 `load_measurement` / `MEASUREMENT_NAME`
- Produces: `RhodesFastGui._apply_preset_to_form(profile: int, preset: TuningPreset) -> None`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_gui_tuning_buttons.py`。这个测试真的把 Tk 界面建起来再驱动它——
GUI 的字段映射（百分比、字符串、显示名）写错了只有真跑一遍才看得出来：

```python
from __future__ import annotations

import pathlib
import unittest

from rhodes_fast.config import AimProfileConfig
from rhodes_fast.gui import RhodesFastGui
from rhodes_fast.tuning_share import dump_preset, load_preset


class TuningFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()

    def tearDown(self) -> None:
        self.app.root.destroy()

    def _preset(self):
        return load_preset(
            dump_preset(
                AimProfileConfig(
                    kp_min=0.14,
                    kp_max=0.35,
                    kp_growth=0.031,
                    target_y_ratio=0.08,
                    fov_radius=123.0,
                    algorithm="inflight_ff",
                    algorithm_params={
                        "loop_delay_frames": 8.0,
                        "gain": 1.0,
                        "velocity_smoothing": 0.25,
                    },
                )
            )
        )

    def test_applying_a_preset_lands_in_the_saved_config(self) -> None:
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        profile = self.app._read_form().aim_profiles[1]
        self.assertEqual(profile.algorithm, "inflight_ff")
        self.assertEqual(profile.algorithm_params["loop_delay_frames"], 8.0)
        self.assertAlmostEqual(profile.kp_max, 0.35, places=3)
        self.assertAlmostEqual(profile.kp_growth, 0.031, places=3)
        # 百分比滑块容易搞反: 0.08 的比例对应 8%。
        self.assertAlmostEqual(profile.target_y_ratio, 0.08, places=2)
        self.assertAlmostEqual(profile.fov_radius, 123.0, places=0)

    def test_applying_to_one_profile_leaves_the_other_alone(self) -> None:
        before = self.app._read_form().aim_profiles[0]
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        after = self.app._read_form().aim_profiles[0]
        self.assertEqual(after.algorithm, before.algorithm)
        self.assertAlmostEqual(after.kp_max, before.kp_max, places=4)

    def test_the_preset_does_not_touch_local_habits(self) -> None:
        before = self.app._read_form().aim_profiles[1]
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        after = self.app._read_form().aim_profiles[1]
        self.assertEqual(after.trigger, before.trigger)
        self.assertEqual(after.target_class, before.target_class)

    def test_the_parameter_fields_get_rebuilt_for_the_new_algorithm(self) -> None:
        self.app._apply_preset_to_form(1, self._preset())
        self.app.root.update_idletasks()
        self.assertEqual(
            list(self.app.profile_algorithm_params[1]),
            ["loop_delay_frames", "gain", "velocity_smoothing"],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_gui_tuning_buttons -v`
Expected: FAIL — `AttributeError: 'RhodesFastGui' object has no attribute '_apply_preset_to_form'`

- [ ] **Step 3: 加按钮**

`rhodes_fast/gui.py` 的方案面板循环里，`self._rebuild_algorithm_params(index)` 那一行
**之后**、`trigger_combo = ...` 那一行**之前**插入：

```python
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
```

- [ ] **Step 4: 实现三个方法**

在 `_algorithm_changed` 之后插入：

```python
    def _measurement_path(self) -> Path:
        return self.config_path.parent / MEASUREMENT_NAME

    def _export_tuning(self, profile: int) -> None:
        try:
            config = self._read_form()
        except ValueError as error:
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
        text = dump_preset(
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
            preset = load_preset(text, known_algorithms=set(available_algorithms()))
        except TuningError as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            return

        lines = [
            f"要把控制方案 {profile + 1} 换成这份调校吗？",
            "",
            f"算法：{_display_value(algorithm_choices(), preset.algorithm)}",
        ]
        lines += [f"　{name} = {value:g}" for name, value in sorted(preset.params.items())]
        lines += [
            f"P 范围 {preset.kp_min:g} – {preset.kp_max:g}，增长斜率 {preset.kp_growth:g}",
            f"视野半径 {preset.fov_radius:g}，框内位置 {preset.target_y_ratio * 100:.0f}%",
        ]
        warning = delay_warning(preset.measured_loop_ms, load_measurement(self._measurement_path()))
        if warning:
            lines += ["", "⚠ " + warning]
        lines += ["", "触发键和目标标签不会被改动。"]
        if not messagebox.askokcancel("导入调校", "\n".join(lines), parent=self.root):
            return

        self._apply_preset_to_form(profile, preset)
        self._append_log(
            f"控制方案 {profile + 1} 已套用 {Path(source).name}。点「保存设置」才会写进配置。"
        )
        if self.process is not None:
            self._append_log("控制算法要停止后重新启动才会生效。")

    def _apply_preset_to_form(self, profile: int, preset: TuningPreset) -> None:
        self.profile_algorithm[profile].set(_display_value(algorithm_choices(), preset.algorithm))
        self._rebuild_algorithm_params(profile)
        for name, variable in self.profile_algorithm_params[profile].items():
            if name in preset.params:
                variable.set(preset.params[name])
        for variables, texts, value in (
            (self.profile_kp_min, self.profile_kp_min_text, preset.kp_min),
            (self.profile_kp_max, self.profile_kp_max_text, preset.kp_max),
            (self.profile_kp_growth, self.profile_kp_growth_text, preset.kp_growth),
        ):
            variables[profile].set(value)
            texts[profile].set(f"{value:.3f}")
        percent = preset.target_y_ratio * 100.0
        self.profile_aim_position[profile].set(percent)
        self.profile_aim_position_text[profile].set(f"{percent:.0f}%")
        self.profile_fov[profile].set(preset.fov_radius)
        self.profile_fov_text[profile].set(f"{preset.fov_radius:.0f}")
        # kp 和视野是热更新的, 立刻推给正在跑的管线。
        self._write_runtime_aim_settings()
```

`gui.py` 的 import 区补上：

```python
from .latency_log import MEASUREMENT_NAME, load_measurement
from .tuning_share import TuningError, TuningPreset, delay_warning, dump_preset, load_preset
```

- [ ] **Step 5: 跑测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_gui_tuning_buttons -v`
Expected: 全部 PASS（4 个）

若 `_read_form()` 抛的不是 `ValueError`，按实际异常类型改 `_export_tuning` 里的 `except`。

- [ ] **Step 6: 人工走一遍往返**

```bash
.venv/Scripts/python.exe -m rhodes_fast --config settings.txt
```

方案 2 点「导出调校…」存到桌面叫 `t.json`；把方案 2 的 kp 和算法随便改掉；再点
「导入调校…」选 `t.json`，确认框里的数字应当和导出时一致，确定后各控件回到原值。
**看完把 `t.json` 删掉，别留在项目里。**

- [ ] **Step 7: 全量测试并提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
git add tests/test_gui_tuning_buttons.py rhodes_fast/gui.py
git commit -m "feat: export and import a profile's tuning from the panel

导出带上本机实测的回路延迟, 导入时和对方差一帧以上就提醒——扣在途和前馈的
补偿量是按作者那台机器的延迟调的。触发键和目标标签不在文件里, 也不会被改动。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: 算法库界面

**Files:**
- Modify: `rhodes_fast/gui.py`
- Test: `tests/test_gui_library.py`（新建）

**Interfaces:**
- Consumes: Task 5 的 `inspect_candidate` / `install` / `rename` / `uninstall` / `read_registry` / `Candidate` / `DuplicateAlgorithm` / `LibraryError`；Task 6 的 `RhodesFastGui.algorithms_dir`
- Produces: `RhodesFastGui._library_rows() -> list[tuple[str, str, str, str, str]]`（每行：显示名、标识、来源文件、导入日期、类别）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_gui_library.py`：

```python
from __future__ import annotations

import pathlib
import unittest

from rhodes_fast.gui import RhodesFastGui


class LibraryRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()

    def tearDown(self) -> None:
        self.app.root.destroy()

    def test_every_builtin_is_listed_and_marked_as_builtin(self) -> None:
        rows = self.app._library_rows()
        builtin = [row for row in rows if row[4] == "内置"]
        self.assertGreaterEqual(len(builtin), 5)
        self.assertIn("p", [row[1] for row in builtin])

    def test_builtins_have_no_source_file_or_date(self) -> None:
        for row in self.app._library_rows():
            if row[4] == "内置":
                self.assertEqual(row[2], "—")
                self.assertEqual(row[3], "—")

    def test_the_tree_shows_exactly_those_rows(self) -> None:
        self.assertEqual(
            len(self.app.library_tree.get_children()), len(self.app._library_rows())
        )

    def test_rename_and_delete_start_disabled_with_nothing_selected(self) -> None:
        self.assertEqual(str(self.app.library_rename_button["state"]), "disabled")
        self.assertEqual(str(self.app.library_delete_button["state"]), "disabled")

    def test_selecting_a_builtin_keeps_rename_and_delete_disabled(self) -> None:
        for item in self.app.library_tree.get_children():
            if self.app.library_tree.item(item, "values")[4] == "内置":
                self.app.library_tree.selection_set(item)
                self.app._library_selection_changed()
                break
        else:
            self.fail("列表里没有内置算法")
        self.assertEqual(str(self.app.library_rename_button["state"]), "disabled")
        self.assertEqual(str(self.app.library_delete_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_gui_library -v`
Expected: FAIL — `AttributeError: 'RhodesFastGui' object has no attribute '_library_rows'`

- [ ] **Step 3: 加标签页**

`rhodes_fast/gui.py` 的 `_build_ui` 里（约 224-231 行），把标签页那几行改成：

```python
        run_tab = ttk.Frame(self.notebook, padding=8)
        advanced_tab = ttk.Frame(self.notebook, padding=14)
        library_tab = ttk.Frame(self.notebook, padding=14)
        self.preview_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(run_tab, text="运行设置")
        self.notebook.add(advanced_tab, text="识别与控制")
        self.notebook.add(library_tab, text="算法库")
        self.notebook.add(self.preview_tab, text="实时预览")
```

在方案面板那个 `for index in range(2):` 循环结束之后、`log_box = ttk.LabelFrame(root, ...)`
之前，插入一行调用：

```python
        self._build_library_tab(library_tab)
```

- [ ] **Step 4: 实现列表**

在 `_algorithm_changed` 之后（`_measurement_path` 之前）插入：

```python
    _LIBRARY_COLUMNS = ("显示名", "标识", "来源文件", "导入日期", "类别")

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
        for column, width in zip(self._LIBRARY_COLUMNS, (210, 130, 160, 130, 70)):
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
            text="内置算法随程序分发，不能改名也不能删除。刚导入的算法要停止后重新启动才能选。",
            style="Subtle.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._refresh_library()

    def _library_rows(self) -> list[tuple[str, str, str, str, str]]:
        installed = read_registry(self.algorithms_dir)
        rows: list[tuple[str, str, str, str, str]] = []
        for name, algorithm in sorted(available_algorithms().items()):
            entry = installed.get(name)
            if entry is None:
                rows.append((algorithm.DISPLAY_NAME, name, "—", "—", "内置"))
            else:
                rows.append(
                    (entry.display_name, name, entry.source_file, entry.imported_at or "—", "已导入")
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
        return values[1], values[4]

    def _library_selection_changed(self) -> None:
        selected = self._selected_algorithm()
        installed = selected is not None and selected[1] == "已导入"
        state = "normal" if installed else "disabled"
        self.library_rename_button.configure(state=state)
        self.library_delete_button.configure(state=state)
        self.library_source_button.configure(state=state)
```

- [ ] **Step 5: 实现导入流程**

紧接着插入（顺序就是这个流程唯一的意义所在）：

```python
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
        self._refresh_library()
        messagebox.showinfo(
            "导入成功",
            f"「{entry.display_name}」已装进算法库。停止后重新启动就能在控制方案里选它。",
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
```

- [ ] **Step 6: 实现改名与删除**

紧接着插入：

```python
    def _rename_algorithm(self) -> None:
        from tkinter import simpledialog

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
        self._refresh_library()
        self._append_log(
            f"算法「{entry.name}」的显示名已改为「{new_name.strip()}」。重启后下拉框里生效。"
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
            f"{entry.source_file} 会被删除。正指着它的控制方案下次启动会响亮地回退到 p。",
            parent=self.root,
        ):
            return
        try:
            uninstall(self.algorithms_dir, entry.name)
        except LibraryError as error:
            messagebox.showerror("删除失败", str(error), parent=self.root)
            return
        self._refresh_library()
        self._append_log(f"算法「{entry.display_name}」已删除。")
```

`gui.py` 的 import 区，把 Task 6 加的那行 `from .algorithm_library import load_installed`
扩成：

```python
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
```

- [ ] **Step 7: 跑测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_gui_library -v`
Expected: 全部 PASS（5 个）

- [ ] **Step 8: 人工走一遍完整导入**

把下面这段存成 `%TEMP%\test_only.py`：

```python
__author__ = "测试用"

from rhodes_fast.aim_algorithms import Param, dynamic_kp


class TestOnly:
    NAME = "test_only"
    DISPLAY_NAME = "临时测试算法"
    PARAMS = (Param("gain", 1.0, 0.0, 3.0, "整体增益"),)

    def __init__(self, params):
        self.gain = params["gain"]

    def reset(self):
        pass

    def compute(self, observation):
        distance = (observation.error_x ** 2 + observation.error_y ** 2) ** 0.5
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return observation.error_x * kp * self.gain, observation.error_y * kp * self.gain
```

然后 `.venv/Scripts/python.exe -m rhodes_fast --config settings.txt`，在「算法库」页：

1. 点「导入算法…」选那个文件 → 确认框里应当写着作者「测试用」、标识 `test_only`
2. 点「是」应当弹出源码窗口，关掉后回到确认框
3. 点「否」→ 提示导入成功
4. 列表里出现一行「临时测试算法 / test_only / test_only.py / 今天 / 已导入」
5. 选中它 → 三个按钮变可用；选中 `p` → 三个按钮变灰
6. 重命名成「改过的名字」→ 列表更新
7. **关掉界面重开** → 控制方案的算法下拉框里能选到「改过的名字」
8. 回算法库页选中它点「删除」→ 列表里消失，`algorithms/test_only.py` 不见了

**第 7 步是关键**：它同时验证了「重启后生效」和「改名只改显示名」两条。
走完把 `algorithms/` 目录清干净。

- [ ] **Step 9: 全量测试并提交**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
git add tests/test_gui_library.py rhodes_fast/gui.py
git commit -m "feat: add an algorithm library tab

导入的顺序是这个界面唯一的意义: 选文件只读 ast, 确认框里能看作者和源码,
点过确认才复制并 import 校验, 校验不过就删掉。内置算法列出来但改名和删除都
点不动。改名只改显示名, 标识留给别人发来的调校去认。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 验收

1. `.venv/Scripts/python.exe -m unittest discover -s tests` 全绿（预计 159 + 约 58 = 217 上下）。
2. `.venv/Scripts/python.exe -m rhodes_fast --config settings.txt --check` → UDP 与 KMBox 正常
   （管线或界面在跑时会占端口，需先停止）。
3. 方案 2 导出调校 → 改乱参数 → 导回来，各控件回到原值，触发键和目标标签没变。
4. 导入一个算法 → 重启 → 在方案里选中它 → 进游戏拉枪，手感确实和内置的不同。
5. 删掉那个 .py 但配置还指着它 → 启动时控制台必须出现
   `!! 算法 test_only 找不到，已回退到 p`。

## 已知不做

- **热切换算法。** 算法在管线启动时构造，运行中改了不生效，所以下拉框在运行中就是禁用的。
- **沙箱。** 装一个算法等于运行一段 Python，和双击 .py 没区别。这里给的是流程保障
  （先看后装），不是技术隔离，界面上的措辞必须诚实反映这一点。
- **调校 JSON 带上 trigger / target_class。** 见设计文档，按键习惯和模型标签因人而异。
- **参数的帧制换算。** `loop_delay_frames` 是帧数，作者和导入方的采集帧率不同就对不上。
  当前靠 `measured_loop_ms` 的提醒兜住，真正的换算列在设计文档的后续工作里。
- **发布到公开仓库。** 与本计划无关，但重申：这个项目只放私有仓库。
