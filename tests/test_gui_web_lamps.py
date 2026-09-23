"""02 屏那三盏灯。

原来是三块写死的标记, 从头到尾一个字都不变 —— 用户的原话是「这个没反馈」。

能报什么由管线实际打出来的东西决定, 不由这三盏灯想显示什么决定。最要紧的一条:
正常运行时 KMBox 连没连上**是**看得出来的, 只是不在某一句话里, 而在顺序里 ——
pipeline.py 先 controller.connect() (连不上就抛), 之后才打那两条横幅。所以横幅
出现就意味着连上了 (或者按配置根本没启用)。
"""

from __future__ import annotations

import unittest

from rhodes_fast.gui_web.lamps import lamp_updates

LOADING = "正在加载模型和加速引擎..."
MODEL_READY = "模型已就绪，正在连接画面输入和移动输出..."
MODEL_BANNER = "模型：ow2_v8s_320.onnx | 加速：TensorRT FP16"
INPUT_BANNER = "输入：UDP 单包 JPEG 0.0.0.0:4455 | 移动：KMBox 192.168.2.188:8808"
SENDINPUT_BANNER = "输入：本机屏幕 DXGI · 显示器 0 · 320x320 | 移动：SendInput"
WAITING = "输入：正在等待第一帧画面"
RATE = (
    "采集= 241.0 帧/秒  处理= 240.8 帧/秒  推理=  2.31 毫秒  检测=  0.42 毫秒  "
    "检测= 3  自瞄标签=person  方案=1  移动=120  丢帧=  7"
)
IDLE_RATE = RATE.replace("采集= 241.0", "采集=   0.0")


def state(line: str, *, output_enabled: bool = True) -> dict[str, str]:
    return {name: update["state"] for name, update in lamp_updates(line, output_enabled=output_enabled).items()}


def text(line: str, lamp: str, *, output_enabled: bool = True) -> str:
    return lamp_updates(line, output_enabled=output_enabled)[lamp]["text"]


class ModelLampTest(unittest.TestCase):
    def test_loading_is_a_state_of_its_own(self) -> None:
        """加载 TensorRT 引擎要十几秒。这段时间里灯还写着「未加载」的话, 用户
        会以为按钮没响应, 然后再按一次。"""
        self.assertEqual(state(LOADING)["model"], "connecting")

    def test_the_banner_turns_it_on_and_names_the_provider(self) -> None:
        """「已就绪」三个字等于没说: 用户想知道的是最后到底跑在哪个加速上 ——
        选了 TensorRT 而实际掉回 CPU 是真实会发生的事, 而那时帧率会差十倍。"""
        self.assertEqual(state(MODEL_BANNER)["model"], "online")
        self.assertEqual(text(MODEL_BANNER, "model"), "TensorRT FP16")

    def test_the_provider_loses_its_onnxruntime_suffix(self) -> None:
        """管线打的是 onnxruntime 的原始类名 TensorrtExecutionProvider。整串摆在
        灯上又长又像内部报错, 而后半截对每个 provider 都一样, 一个字的信息都没有。"""
        self.assertEqual(text("模型：a.onnx | 加速：TensorrtExecutionProvider", "model"), "Tensorrt")
        self.assertEqual(text("Model: a.onnx | provider: CUDAExecutionProvider", "model"), "CUDA")

    def test_a_plain_provider_name_is_left_alone(self) -> None:
        self.assertEqual(text(MODEL_BANNER, "model"), "TensorRT FP16")

    def test_other_lines_leave_it_alone(self) -> None:
        self.assertNotIn("model", state(WAITING))


class StreamLampTest(unittest.TestCase):
    def test_waiting_for_the_first_frame_is_not_an_error(self) -> None:
        """采集端还没开始推流是最常见的情况, 不是故障。写成红的会让人去查一个
        不存在的问题。"""
        self.assertEqual(state(WAITING)["stream"], "connecting")

    def test_frames_arriving_turn_it_on_with_the_rate(self) -> None:
        self.assertEqual(state(RATE)["stream"], "online")
        self.assertIn("241.0", text(RATE, "stream"))

    def test_a_zero_rate_is_not_a_live_stream(self) -> None:
        """管线在跑但一帧没收到时, 那一行照样每秒打一次, 采集是 0.0。
        当成有画面的话, 灯会在「一切正常」上撒谎, 而画面是黑的。"""
        self.assertEqual(state(IDLE_RATE)["stream"], "connecting")

    def test_a_check_failure_turns_it_red(self) -> None:
        for line in ("UDP 连接失败：no UDP frame received within 3 seconds",
                     "OBS 连接失败：[Errno 111] Connection refused"):
            with self.subTest(line=line):
                self.assertEqual(state(line)["stream"], "error")

    def test_a_check_success_turns_it_on(self) -> None:
        self.assertEqual(state("UDP 正常：画面=1920x1080")["stream"], "online")
        self.assertIn("1920x1080", text("UDP 正常：画面=1920x1080", "stream"))


class KmboxLampTest(unittest.TestCase):
    """正常运行时 KMBox 连没连上是看得出来的 —— 靠顺序, 不靠某一句话。"""

    def test_connecting_is_announced(self) -> None:
        self.assertEqual(state(MODEL_READY)["kmbox"], "connecting")

    def test_the_input_banner_means_the_box_answered(self) -> None:
        """pipeline.py 里 controller.connect() 排在这条横幅**前面**, 连不上就抛,
        根本走不到打印。所以横幅出现本身就是回执。

        这条是这次改动的关键: 原来的注释认定「运行时报不出连没连上」, 于是那盏
        灯永远写着「未验证」。报得出来, 只是信号在顺序里。
        """
        self.assertEqual(state(INPUT_BANNER)["kmbox"], "online")

    def test_a_disabled_box_is_standby_not_online(self) -> None:
        """连接那一步在禁用时直接返回, 横幅照打。当成已连接的话, 用户会对着一盏
        亮灯纳闷为什么鼠标不动。"""
        self.assertEqual(state(INPUT_BANNER, output_enabled=False)["kmbox"], "standby")
        self.assertIn("禁用", text(INPUT_BANNER, "kmbox", output_enabled=False))

    def test_the_runtime_failure_turns_it_red(self) -> None:
        """盒子没插电时最常见的那条: connect() 抛 RuntimeError, 管线当场死掉,
        而这句话跟着 traceback 流进运行状态。不认它的话, 灯会停在「正在连接」,
        而管线其实已经没了。"""
        line = "RuntimeError: KMBox did not respond after 3 attempts: [WinError 10060]"
        self.assertEqual(state(line)["kmbox"], "error")

    def test_an_explicit_verdict_is_not_overruled_by_the_form(self) -> None:
        """「KMBox 正常」只在启用时才打 (否则打的是「KMBox 已禁用」), 所以它自己
        就是权威。拿表单去推翻它的话, 一次成功的「测试输入」会显示成「已禁用」。

        只有那条横幅的「已连接」是从打印次序推出来的, 才需要表单再判一次。
        """
        self.assertEqual(state("KMBox 正常", output_enabled=False)["kmbox"], "online")

    def test_the_check_verdicts_are_read(self) -> None:
        self.assertEqual(state("KMBox 正常")["kmbox"], "online")
        self.assertEqual(state("KMBox 已禁用")["kmbox"], "standby")
        self.assertEqual(state("KMBox 连接失败：timed out")["kmbox"], "error")

    def test_the_failure_reason_is_kept(self) -> None:
        """「连接失败」四个字没法排查。IP 打错、盒子没插电、UUID 不对, 三种原因
        对应三种完全不同的动作。"""
        self.assertIn("timed out", text("KMBox 连接失败：timed out", "kmbox"))


class SendInputLampTest(unittest.TestCase):
    """选了 SendInput 时第三盏灯说的是 SendInput 的状态。"""

    def test_the_banner_means_it_is_ready(self) -> None:
        """SendInput 没有「连上」这回事, 写「已连接」是在描述一个不存在的盒子。"""
        self.assertEqual(state(SENDINPUT_BANNER)["kmbox"], "online")
        self.assertEqual(text(SENDINPUT_BANNER, "kmbox"), "就绪")

    def test_the_kmbox_switch_does_not_dim_it(self) -> None:
        """kmbox.enabled 只管 KMBox。拿它去判 SendInput 的话, 一个关着 KMBox 的
        用户选了 SendInput, 灯却写着「已禁用」, 而鼠标其实在动。"""
        self.assertEqual(state(SENDINPUT_BANNER, output_enabled=False)["kmbox"], "online")

    def test_the_check_verdicts_are_read(self) -> None:
        self.assertEqual(state("SendInput 正常")["kmbox"], "online")
        self.assertEqual(text("SendInput 正常", "kmbox"), "就绪")
        self.assertEqual(state("SendInput 连接失败：没有 user32")["kmbox"], "error")
        self.assertIn("user32", text("SendInput 连接失败：没有 user32", "kmbox"))


class StartingTest(unittest.TestCase):
    def test_starting_is_not_the_same_as_not_started(self) -> None:
        """起管线到第一条日志之间有十几秒 (TensorRT 引擎要编译)。这段时间里三盏
        灯写着「未启动」是假的 —— 它正在启动, 而用户盯着的就是这三盏灯。"""
        from rhodes_fast.gui_web.lamps import IDLE, STARTING

        self.assertEqual(set(STARTING), set(IDLE))
        for name, value in STARTING.items():
            with self.subTest(lamp=name):
                self.assertEqual(value["state"], "connecting")
                self.assertNotEqual(value["text"], IDLE[name]["text"])


class NoiseTest(unittest.TestCase):
    def test_an_ordinary_line_changes_nothing(self) -> None:
        """每一行日志都过一遍这个函数。误命中的代价是一盏灯说了假话 —— 而假话
        比不说糟得多, 因为用户会照着它去排查别的地方。"""
        for line in ("正在停止...", "已停止（退出码 0）。", "", "设置已保存。",
                     "预设「甲」已保存。"):
            with self.subTest(line=line):
                self.assertEqual(lamp_updates(line, output_enabled=True), {})

    def test_the_english_pipeline_is_read_too(self) -> None:
        """config.ui.language 决定管线打中文还是英文。只认中文的话, 选了英文的
        用户三盏灯全是死的 —— 而这正是用户报的那个问题。"""
        self.assertEqual(state("Loading model and acceleration engine...")["model"], "connecting")
        self.assertEqual(state("Model: a.onnx | provider: CUDA")["model"], "online")
        self.assertEqual(state("Input: UDP | Output: KMBox 1.2.3.4:8808")["kmbox"], "online")
        self.assertEqual(state("Input: UDP | Output: SendInput")["kmbox"], "online")
        self.assertEqual(state("SendInput OK")["kmbox"], "online")
        self.assertEqual(state("SendInput FAILED: no user32")["kmbox"], "error")
        self.assertEqual(state("KMBox FAILED: timed out")["kmbox"], "error")
        self.assertEqual(state("UDP FAILED: no frame")["stream"], "error")


if __name__ == "__main__":
    unittest.main()
