"""从管线的日志行里抠出界面要显示的数字。

管线每秒打两行 (pipeline.py:538-566): 一行帧率/耗时/目标数, 一行延迟。它们本来
就要流进运行状态框, 这里只是顺路再读一遍 —— 不加任何进程间通信, 也不碰子进程。
一秒两次正则, 而且是在界面进程里, 对推理那条管线没有任何影响。

两套字面量都要认: config.ui.language 决定管线打中文还是英文 (pipeline.py 的
_text), 而界面这边不该因为用户选了英文就变成一排横杠。

值一律以字符串返回, 保留管线打出来的小数位数。重新格式化只会引入第二套精度
规则, 而这两行的格式已经是管线自己定好的了。
"""

from __future__ import annotations

import re

# 中文那行里「检测=」出现了两次 —— 一次是检测耗时 (带「毫秒」), 一次是这一帧的
# 目标数 (后面跟着「自瞄标签」)。光按标签抓, 两个都会命中前一个, 于是「目标数」
# 显示成 0.42: 一个看着挺合理、永远不会有人怀疑的错数。所以这里一律连着单位或
# 后一个字段一起匹配, 不许只认标签。
_FIELDS: tuple[tuple[str, str], ...] = (
    ("capture_fps", r"(?:采集|capture)=\s*([\d.]+)\s*(?:帧/秒|fps)"),
    ("processed_fps", r"(?:处理|processed)=\s*([\d.]+)\s*(?:帧/秒|fps)"),
    ("infer_ms", r"(?:推理|infer)=\s*([\d.]+)\s*(?:毫秒|ms)"),
    ("detect_ms", r"(?:检测|detect)=\s*([\d.]+)\s*(?:毫秒|ms)"),
    ("detections", r"(?:检测=\s*(\d+)\s+自瞄标签|detections=\s*(\d+))"),
    ("dropped", r"(?:丢帧|dropped)=\s*(\d+)"),
    ("latency_ms", r"(?:平均|avg)=\s*([\d.]+)\s*(?:毫秒|ms)"),
    ("latency_p95_ms", r"(?:P95|p95)=\s*([\d.]+)"),
    # 型号和画面输入是启动时打一次的横幅, 填状态栏左边那两格。
    ("model", r"^(?:模型：|Model: )(.+?)\s*\|\s*(?:加速：|provider: )"),
    ("provider", r"\|\s*(?:加速：|provider: )(.+)$"),
    # 后半截的 KMBox 是必须的: 管线连不上画面时打的是「输入：正在等待第一帧画面」
    # (pipeline.py:397), 同一个前缀。不加这个限定, 状态栏的 INPUT 会被一句错误
    # 信息顶掉 —— 而那句话本来就在运行状态里看得见了。
    ("input", r"^(?:输入：|Input: )(.+)\s*\|\s*(?:KMBox：|KMBox: )"),
)

_PATTERNS = tuple((name, re.compile(pattern)) for name, pattern in _FIELDS)


def parse_telemetry(line: str) -> dict[str, str]:
    """这一行里能解出来的字段。解不出来就是空字典。

    纯函数: 不碰界面, 不碰文件。调用方 (Api._push_log) 拿到非空的结果才去动
    界面 —— 误命中的代价是界面上多一个假数字, 而假数字比横杠糟得多, 因为没人
    会怀疑它。
    """
    values: dict[str, str] = {}
    for name, pattern in _PATTERNS:
        match = pattern.search(line)
        if match is None:
            continue
        # detections 那条是两个分支的或, 命中哪个组取哪个。
        value = next((group for group in match.groups() if group is not None), None)
        if value is not None:
            values[name] = value.strip()
    return values


# 管线每秒重复打的那两行各自的字段。两行的字段不重合, 这是分类的全部依据 ——
# 延迟那行里也有「推理=」, 但它后面没跟单位, 上面的正则要求单位, 所以解不出
# infer_ms。靠这一点, 两行的解析结果是互斥的。
_RATE_FIELDS = frozenset(
    {"capture_fps", "processed_fps", "infer_ms", "detect_ms", "detections", "dropped"}
)
_LATENCY_FIELDS = frozenset({"latency_ms", "latency_p95_ms"})


def periodic_kind(values: dict[str, str]) -> str | None:
    """这一行是不是每秒重复的那两行之一, 是哪一行。不是就返回 None。

    收 parse_telemetry 的结果而不是原文: 判断依据和取数依据必须是同一套, 否则
    会出现「认出来了但取不到数」或者反过来 —— 而症状是固定行上少一格, 没人查得出
    是分类和解析不一致。

    开机横幅 (模型 / 输入) 解出来的是 model / provider / input, 三个都不在这两
    张表里, 所以返回 None, 照旧进日志 —— 它们只打一次, 而且是「现在跑的是哪个
    模型」的唯一记录。

    延迟先判: 它那两个字段是独占的, 而速率那张表里的字段将来可能被别的行蹭到。
    """
    if values.keys() & _LATENCY_FIELDS:
        return "latency"
    if values.keys() & _RATE_FIELDS:
        return "rate"
    return None
