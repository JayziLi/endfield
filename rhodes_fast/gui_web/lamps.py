"""02 屏那三盏灯的状态, 从管线打出来的每一行里读。

能报什么由管线实际打出来的东西决定, 不由这三盏灯想显示什么决定。逐条说清楚
每盏灯的依据, 因为一盏说假话的灯比一盏不动的灯糟得多 —— 用户会照着它去排查
别的地方。

**视频流**: 运行时靠每秒那行的采集帧率 (0.0 说明还没收到画面, 不是有画面);
「测试输入」那条路另有 UDP/OBS 的成败两句话。

**模型**: 「正在加载」到「模型：X | 加速：Y」那条横幅。横幅里的 provider 是
真正生效的那个 —— 选了 TensorRT 而实际掉回 CPU 是真实会发生的事, 帧率差十倍,
所以灯上写的是它而不是一句「已就绪」。

**KMBox**: 正常运行时连没连上**是**看得出来的, 只是不在某一句话里, 而在顺序里。
pipeline.py 的次序是 controller.connect() → 打「模型：…」→ 打「输入：… | KMBox：…」,
而 connect() 连不上就抛 RuntimeError, 管线当场死掉, 根本走不到打印。所以那条
横幅出现本身就是回执。唯一的例外是配置里把 KMBox 关了: 那时 connect() 直接返回,
横幅照打 —— 所以要把「启没启用」一起传进来, 否则会对着一个禁用的盒子亮绿灯。

纯函数: 不碰界面, 不碰文件, 不留状态。一行进, 这一行改变的那几盏灯出。
没改变的灯不出现在结果里 —— 返回一份「全部状态」的话, 每一行普通日志都会把
另外两盏灯按它此刻并不知道的值重写一遍。
"""

from __future__ import annotations

import re

# (灯名, 状态, 正则, 文字模板, 是否靠顺序推断)。模板里的 {0} 是正则第一个捕获组。
# 顺序有意义: 命中即停, 每盏灯一行只取第一条。
#
# 最后一栏只有 KMBox 那条横幅是 True: 它的「已连接」是从打印次序推出来的, 而
# 禁用时那一步会被跳过、横幅照打, 所以要拿表单再判一次。其余每条都是管线明说的
# 结论 (「KMBox 正常」只在启用时才打, 否则打的是「KMBox 已禁用」), 明说的不许
# 被表单推翻 —— 推翻的话, 一次成功的「测试输入」会显示成「已禁用」。
_RULES: tuple[tuple[str, str, str, str, bool], ...] = (
    # ---- 模型 ----
    ("model", "connecting", r"^(?:正在加载模型和加速引擎|Loading model and acceleration engine)", "正在加载", False),
    ("model", "online", r"^(?:模型：|Model: ).+?\s*\|\s*(?:加速：|provider: )(.+)$", "{0}", False),
    # ---- 视频流 ----
    # 采集帧率。0.0 是「管线在跑但一帧没收到」, 那一行照样每秒打一次。
    ("stream", "online", r"(?:采集|capture)=\s*(0*[1-9][\d.]*)\s*(?:帧/秒|fps)", "{0} 帧/秒", False),
    ("stream", "connecting", r"(?:采集|capture)=\s*[\d.]+\s*(?:帧/秒|fps)", "等待画面", False),
    ("stream", "connecting", r"^(?:输入：|Input: )(?:正在等待第一帧画面|waiting for the first input frame)", "等待画面", False),
    ("stream", "error", r"^(?:UDP|OBS|本机屏幕|Desktop) (?:连接失败：|FAILED: )(.+)$", "{0}", False),
    ("stream", "online", r"^UDP (?:正常：画面=|OK: frame=)(\S+)", "{0}", False),
    ("stream", "online", r"^(?:本机屏幕 正常：画面=|Desktop OK: frame=)(\S+)", "{0}", False),
    ("stream", "online", r"^OBS (?:正常：|OK: ).*?(?:画面=|frame=)(\S+)", "{0}", False),
    # ---- 移动输出 (灯的 id 还叫 kmbox, 标题按输出方式显示 KMBox 或 SendInput) ----
    # SendInput 没有「连上」这回事, 写「就绪」。这几条排在 KMBox 那几条前面:
    # 横幅那条通用规则也会命中 SendInput 的横幅, 而且是按表单推断的。
    ("kmbox", "online", r"^SendInput (?:正常|OK)$", "就绪", False),
    ("kmbox", "error", r"^SendInput (?:连接失败：|FAILED: )(.+)$", "{0}", False),
    ("kmbox", "online", r"^(?:输入：|Input: ).+\|\s*(?:移动：|Output: )SendInput$", "就绪", False),
    # 运行中失败: connect() 抛的 RuntimeError 跟着 traceback 流进运行状态。
    # 不认它的话灯会停在「正在连接」, 而管线其实已经没了。
    ("kmbox", "error", r"KMBox did not respond after \d+ attempts:\s*(.+)$", "{0}", False),
    ("kmbox", "error", r"^KMBox (?:连接失败：|FAILED: )(.+)$", "{0}", False),
    ("kmbox", "standby", r"^(?:KMBox 已禁用|KMBox disabled)$", "已禁用", False),
    ("kmbox", "online", r"^(?:KMBox 正常|KMBox OK)$", "已连接", False),
    ("kmbox", "connecting", r"^(?:模型已就绪|Model ready)", "正在连接", False),
    # 这一条的依据是顺序不是字面: 见模块 docstring。kmbox_enabled 在下面另行处理。
    ("kmbox", "online", r"^(?:输入：|Input: ).+\|\s*(?:移动：|Output: )KMBox ", "已连接", True),
)

_PATTERNS = tuple(
    (lamp, lamp_state, re.compile(pattern), template, inferred)
    for lamp, lamp_state, pattern, template, inferred in _RULES
)

# 三盏灯没在跑的时候各自写什么。跑完一轮之后要还原成这个 —— 停下来之后还亮着
# 「已连接」是在撒谎。
IDLE: dict[str, dict[str, str]] = {
    "stream": {"state": "off", "text": "未启动"},
    "model": {"state": "off", "text": "未加载"},
    "kmbox": {"state": "off", "text": "未验证"},
}

# 刚起子进程、第一条日志还没到的那一段。TensorRT 要编译引擎, 这段能有十几秒 ——
# 期间三盏灯写着「未启动」是假的: 它正在启动, 而用户盯着的就是这三盏灯。
STARTING: dict[str, dict[str, str]] = {
    name: {"state": "connecting", "text": "正在启动"} for name in IDLE
}


def lamp_updates(line: str, *, output_enabled: bool) -> dict[str, dict[str, str]]:
    """这一行改变了哪几盏灯。没改变的不出现在结果里。

    output_enabled (移动输出启用了没有) 来自表单, 不来自日志: 管线在 KMBox 关着
    的时候照样打那条横幅 (connect() 直接返回), 光看日志分不出「连上了」和
    「压根没连」。
    """
    updates: dict[str, dict[str, str]] = {}
    for lamp, lamp_state, pattern, template, inferred in _PATTERNS:
        if lamp in updates:
            continue  # 每盏灯一行只取第一条命中的规则
        match = pattern.search(line)
        if match is None:
            continue
        text = template.format(*(group or "" for group in match.groups()))
        if lamp == "model":
            # 管线打的是 onnxruntime 的原始类名 (TensorrtExecutionProvider)。
            # 后半截对每个 provider 都一样, 一个字的信息都没有, 而整串摆在灯上
            # 又长又像内部报错。
            text = re.sub(r"ExecutionProvider$", "", text.strip())
        if inferred and not output_enabled:
            # 禁用的盒子不该亮成已连接: 用户会对着一盏亮灯纳闷为什么鼠标不动。
            lamp_state, text = "standby", "已禁用"
        updates[lamp] = {"state": lamp_state, "text": text.strip()}
    return updates
