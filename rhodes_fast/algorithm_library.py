"""用户自己写的控制算法: 安装、改名、删除、加载。

安全边界是流程性的, 不是技术性的: 用户装的是一段会跑起来的 Python, 和双击一个
.py 没有区别。这个模块能保证的是——在用户看清作者、文件名、源码并点头之前,
那个文件一行都不会执行。

内置算法不在这里。它们随程序分发, 既不出现在 algorithms/ 目录也不进注册表,
因此不可改名、不可删除。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from .aim_algorithms.builtin import BUILTIN_ALGORITHMS
from .aim_algorithms.contract import Param, resolve_params
from .aim_algorithms.human import HUMAN_ALGORITHMS

REGISTRY_NAME = "installed.json"
_MAX_SOURCE_BYTES = 256 * 1024


class LibraryError(RuntimeError):
    pass


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


@dataclass(frozen=True, slots=True)
class InstalledAlgorithm:
    name: str
    display_name: str
    source_file: str
    imported_at: str
    author: str = "未署名"


def builtin_names() -> set[str]:
    return {
        algorithm.NAME for algorithm in (*BUILTIN_ALGORITHMS, *HUMAN_ALGORITHMS)
    }


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
    rows = payload.get("algorithms", [])
    if not isinstance(rows, list):
        return {}
    entries: dict[str, InstalledAlgorithm] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            entry = InstalledAlgorithm(
                name=str(row["name"]),
                display_name=str(row["display_name"]),
                source_file=str(row["source_file"]),
                imported_at=str(row.get("imported_at", "")),
                author=str(row.get("author", "未署名")),
            )
        except KeyError:
            # 缺字段的那一行跳过就好, 不该连累旁边装好的算法。
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
        # 只带标注不赋值的 `PARAMS: tuple` 这里 value 是 None, 下面的 isinstance 兜住。
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


def _safe_file_name(name: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in Path(name).stem).strip("_")
    if not stem or stem[0].isdigit():
        stem = "algo_" + stem
    return stem + ".py"


def _unique_file_name(entries: dict[str, InstalledAlgorithm], name: str) -> str:
    """文件名跟着标识走, 不跟着来源文件名走。

    来源文件名清洗后会撞车 (algo.py 和 algo!.py 都变成 algo.py), 撞上就会静默
    盖掉前一个算法的源码, 而注册表里两行还都指着同一个文件。标识本身是唯一的,
    拿它命名就没这个问题; 万一两个标识清洗后仍然同名, 再补个序号。
    """
    taken = {entry.source_file for key, entry in entries.items() if key != name}
    base = _safe_file_name(name)
    if base not in taken:
        return base
    stem = base[:-3]
    counter = 2
    while f"{stem}_{counter}.py" in taken:
        counter += 1
    return f"{stem}_{counter}.py"


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
        # 只认这个文件里定义的类。拿内置算法当基线来比较或兜底是很自然的写法,
        # 而 import 进来的那个类也带 NAME, 按定义顺序会先撞上它——结果是这种
        # 合法文件一概装不上, 报错还倒打作者一耙说他的 NAME 对不上。
        if getattr(value, "__module__", None) != module.__name__:
            continue
        name = getattr(value, "NAME", None)
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(getattr(value, "DISPLAY_NAME", None), str):
            raise LibraryError(f"{file_name} 里的 {name} 缺少字符串 DISPLAY_NAME。")
        specs = getattr(value, "PARAMS", None)
        if not isinstance(specs, tuple) or not all(isinstance(spec, Param) for spec in specs):
            raise LibraryError(f"{file_name} 里的 PARAMS 必须是 Param 组成的 tuple。")
        if len({spec.name for spec in specs}) != len(specs):
            raise LibraryError(f"{file_name} 里的 PARAMS 包含重复名称。")
        _require_signature(value.__init__, ["self", "params"], file_name, "__init__")
        reset = getattr(value, "reset", None)
        if not callable(reset):
            raise LibraryError(f"{file_name} 里的 {name} 缺少可调用的 reset 方法。")
        compute = getattr(value, "compute", None)
        if not callable(compute):
            raise LibraryError(f"{file_name} 里的 {name} 缺少可调用的 compute 方法。")
        _require_signature(reset, ["self"], file_name, "reset")
        _require_signature(compute, ["self", "observation"], file_name, "compute")
        try:
            instance = value(resolve_params(specs, {}))
            instance.reset()
        except Exception as error:
            raise LibraryError(f"{file_name} 里的 {name} 无法用默认参数初始化：{error}") from error
        return value
    raise LibraryError(
        f"{file_name} 里找不到合法的算法类。需要一个带 NAME、DISPLAY_NAME、PARAMS "
        "以及 reset / compute 方法的类。"
    )


def _require_signature(method, expected: list[str], file_name: str, label: str) -> None:
    try:
        parameters = list(inspect.signature(method).parameters)
    except (TypeError, ValueError) as error:
        raise LibraryError(f"{file_name} 里的 {label} 签名无法读取。") from error
    if parameters != expected:
        raise LibraryError(
            f"{file_name} 里的 {label} 签名必须是 {label}({', '.join(expected)})，"
            f"现在是 {label}({', '.join(parameters)})。"
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
    destination = directory / _unique_file_name(entries, candidate.name)
    # Validate beside the live file, then replace it in one operation. Writing
    # directly onto an existing algorithm would destroy the working copy when
    # a malformed replacement fails its import check.
    staging = directory / f"_pending_{uuid.uuid4().hex}.py"
    staging.write_text(candidate.source, encoding="utf-8")
    try:
        algorithm = _import_algorithm(staging)
        # 用户文件里 import 进来的内置类也会被 _validate 扫到, 这一步兜住:
        # 加载出来的 NAME 和源码里读到的对不上, 就不是作者想装的那个类。
        if algorithm.NAME != candidate.name:
            raise LibraryError(
                f"源码里写的 NAME 是「{candidate.name}」，实际加载出来却是「{algorithm.NAME}」。"
            )
        staging.replace(destination)
    except (LibraryError, OSError):
        staging.unlink(missing_ok=True)
        raise

    if existing is not None and existing.source_file != destination.name:
        (directory / existing.source_file).unlink(missing_ok=True)

    entry = InstalledAlgorithm(
        name=algorithm.NAME,
        # 替换时保留用户自己起的名字, 别把他改过的名字打回作者的默认值。
        display_name=existing.display_name if existing is not None else candidate.display_name,
        source_file=destination.name,
        imported_at=(now or datetime.now()).strftime("%Y-%m-%d %H:%M"),
        author=candidate.author,
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
