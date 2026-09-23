from __future__ import annotations

import unittest

from rhodes_fast.gui_web.telemetry import parse_telemetry, periodic_kind

# 管线每秒打的两行, 逐字从 pipeline.py 的 f-string 抄下来 (对齐用的空格也照抄:
# `{value:6.1f}` 会在数字前面留出空位, 正则必须容得下)。
ZH_STATUS = (
    "采集= 241.0 帧/秒  处理= 240.8 帧/秒  "
    "推理=  2.31 毫秒  检测=  0.42 毫秒  "
    "检测= 3  自瞄标签=person  方案=1  移动=120  丢帧=  7"
)
EN_STATUS = (
    "capture= 241.0 fps  processed= 240.8 fps  "
    "infer=  2.31 ms  detect=  0.42 ms  "
    "detections= 3  aim-class=person  profile=1  moves=120  dropped=  7"
)
ZH_LATENCY = (
    "接收端总延迟 平均= 2.45 毫秒 P95= 3.10 毫秒 | "
    "重组=0.11 解码=0.62 排队=0.04 预处理=0.28 推理=1.19 后处理=0.09 KMBox=0.12 毫秒"
)
EN_LATENCY = (
    "local latency avg= 2.45 ms p95= 3.10 ms | "
    "assemble=0.11 decode=0.62 queue=0.04 pre=0.28 infer=1.19 post=0.09 kmbox=0.12 ms"
)


class StatusLineTest(unittest.TestCase):
    def test_it_reads_every_number_off_the_chinese_status_line(self) -> None:
        self.assertEqual(
            parse_telemetry(ZH_STATUS),
            {
                "capture_fps": "241.0",
                "processed_fps": "240.8",
                "infer_ms": "2.31",
                "detect_ms": "0.42",
                "detections": "3",
                "dropped": "7",
            },
        )

    def test_it_reads_every_number_off_the_english_status_line(self) -> None:
        """language=en 的用户拿到的是另一套字面量, 界面不该因此变成一排横杠。"""
        self.assertEqual(parse_telemetry(EN_STATUS), parse_telemetry(ZH_STATUS))

    def test_the_two_chinese_labels_spelled_detection_are_told_apart(self) -> None:
        """中文那行里「检测=」出现了两次: 一次是耗时 (带「毫秒」), 一次是这一帧
        的目标数。pipeline.py:547-551 就是这么打的。

        光按标签抓的话两个都会命中第一个, 界面上「目标数」会显示 0.42 —— 一个
        看着挺合理、永远不会有人怀疑的错数。靠单位区分。
        """
        values = parse_telemetry(ZH_STATUS)
        self.assertEqual(values["detect_ms"], "0.42")
        self.assertEqual(values["detections"], "3")

    def test_english_detect_and_detections_do_not_bleed_into_each_other(self) -> None:
        values = parse_telemetry(EN_STATUS)
        self.assertEqual(values["detect_ms"], "0.42")
        self.assertEqual(values["detections"], "3")

    def test_a_count_without_a_unit_is_never_read_as_a_duration(self) -> None:
        """上面那两条其实是靠运气绿的: 完整的那行里, 带「毫秒」的「检测=」恰好
        排在计数的前面, 所以就算正则不看单位也会先撞上对的那个。

        这条把单位变成承重的: 只有计数、没有耗时的时候, detect_ms 必须是缺的,
        不能拿目标数去顶 —— 界面上「检测耗时 3 毫秒」是个看不出错的错数。
        """
        values = parse_telemetry("检测= 3  自瞄标签=person")
        self.assertEqual(values, {"detections": "3"})


class LatencyLineTest(unittest.TestCase):
    def test_it_reads_the_chinese_latency_line(self) -> None:
        self.assertEqual(
            parse_telemetry(ZH_LATENCY), {"latency_ms": "2.45", "latency_p95_ms": "3.10"}
        )

    def test_it_reads_the_english_latency_line(self) -> None:
        self.assertEqual(parse_telemetry(EN_LATENCY), parse_telemetry(ZH_LATENCY))


class BannerTest(unittest.TestCase):
    def test_it_reads_the_model_banner(self) -> None:
        for line in (
            "模型：yolov5n.onnx | 加速：CUDAExecutionProvider",
            "Model: yolov5n.onnx | provider: CUDAExecutionProvider",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    parse_telemetry(line),
                    {"model": "yolov5n.onnx", "provider": "CUDAExecutionProvider"},
                )

    def test_it_reads_the_input_banner(self) -> None:
        for line in (
            "输入：UDP MPEG-TS/H.264 视频流 0.0.0.0:4455 | KMBox：192.168.1.100:8808",
            "Input: UDP MPEG-TS/H.264 stream 0.0.0.0:4455 | KMBox: 192.168.1.100:8808",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    parse_telemetry(line)["input"].endswith("0.0.0.0:4455"), True
                )

    def test_an_input_error_is_not_mistaken_for_the_banner(self) -> None:
        """管线连不上画面时打的是「输入：正在等待第一帧画面」(pipeline.py:397),
        同一个前缀。当成横幅的话状态栏的 INPUT 会被一句错误信息顶掉, 而那句话
        本来就在运行状态里看得见了。靠后半截的 KMBox 区分。
        """
        self.assertEqual(parse_telemetry("输入：正在等待第一帧画面"), {})


class NoiseTest(unittest.TestCase):
    def test_ordinary_log_lines_yield_nothing(self) -> None:
        """解出空字典的行不该惊动界面。误命中的代价是界面上多一个假数字,
        而假数字比横杠糟得多 —— 没人会怀疑它。"""
        for line in (
            "正在加载模型和加速引擎...",
            "延迟日志将记录到 latency-20260918-213000.csv（每帧都记，停止时给出估计）。",
            "GPU 预处理：已启用",
            "正在停止...",
            "",
        ):
            with self.subTest(line=line):
                self.assertEqual(parse_telemetry(line), {})



class PeriodicKindTest(unittest.TestCase):
    """哪些行是管线每秒重复打的。

    这两行一秒各来一次, 一股脑往运行状态里堆的话, 一分钟就是 120 行数字, 而
    「模型已就绪」「KMBox 连不上」这种只出现一次、真正要看的话早被顶出屏幕了。
    认出来之后它们不进日志, 改去刷新一条固定的状态行。
    """

    def test_the_rate_line_is_periodic(self) -> None:
        for line in (ZH_STATUS, EN_STATUS):
            with self.subTest(line=line):
                self.assertEqual(periodic_kind(parse_telemetry(line)), "rate")

    def test_the_latency_line_is_periodic(self) -> None:
        for line in (ZH_LATENCY, EN_LATENCY):
            with self.subTest(line=line):
                self.assertEqual(periodic_kind(parse_telemetry(line)), "latency")

    def test_the_two_are_told_apart(self) -> None:
        """混成一类的话, 后到的那行会把前一行的内容覆盖掉 —— 固定行上的帧率和
        延迟就会一秒一次地互相顶替。"""
        self.assertNotEqual(
            periodic_kind(parse_telemetry(ZH_STATUS)),
            periodic_kind(parse_telemetry(ZH_LATENCY)),
        )

    def test_the_banners_are_not_periodic(self) -> None:
        """开机横幅只打一次, 而且它是「现在跑的是哪个模型、哪个输入」的唯一记录。
        当成周期行扔掉的话, 运行状态里就再也没有这句话了。"""
        for line in (
            "模型：ow2_v8s_320.onnx | 加速：TensorRT FP16",
            "输入：UDP 单包 JPEG 0.0.0.0:4455 | KMBox：192.168.2.188:8808",
        ):
            with self.subTest(line=line):
                self.assertIsNone(periodic_kind(parse_telemetry(line)))

    def test_an_ordinary_line_is_not_periodic(self) -> None:
        for line in ("正在加载模型和加速引擎...", "已停止（退出码 0）。", ""):
            with self.subTest(line=line):
                self.assertIsNone(periodic_kind(parse_telemetry(line)))

if __name__ == "__main__":
    unittest.main()
