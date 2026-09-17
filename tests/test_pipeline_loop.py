from __future__ import annotations

import io
import tempfile
from dataclasses import replace
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from rhodes_fast.config import AimProfileConfig, load_config
from rhodes_fast.detector import Detection
from rhodes_fast.latency_log import LatencySample, read_latency_log
from rhodes_fast.pipeline import run_pipeline
from rhodes_fast.trail import Calibration

_CONFIG = Path(__file__).parents[1] / "config.example.toml"


class _Harness:
    """驱动 run_pipeline 跑指定的几帧, 记录主循环里各步骤的先后顺序。"""

    def __init__(
        self,
        sequences: list[int],
        warnings: list[str] | None = None,
        notices: list[str] | None = None,
        profiles: list[AimProfileConfig] | None = None,
    ) -> None:
        self.sequences = sequences
        self.warnings = warnings or []
        self.notices = notices or []
        # 真实的 KmboxController.active_profile 永远返回 AimProfileConfig。桩要跟上,
        # 否则延迟日志里那几列拿到的是 Mock, json.dumps 当场抛。
        # 传多个就是模拟跑的过程中热切换算法。
        self.profiles = list(profiles or [AimProfileConfig()])
        self.frame = 0
        self.order: list[str] = []

    def run(
        self,
        *,
        preview_enabled: bool = True,
        latency_log: Path | None = None,
        trigger_active: bool = True,
        trail_settings_file: Path | None = None,
        kmbox_enabled: bool | None = None,
        shows_frame: bool = True,
    ) -> None:
        frame = np.zeros((320, 320, 3), dtype=np.uint8)

        detector = Mock()
        detector.detect.return_value = [Detection(150, 150, 170, 170, 0.9, 0)]
        detector.last_inference_ms = 1.0
        detector.last_detection_ms = 1.2
        detector.last_preprocess_ms = 0.0
        detector.last_postprocess_ms = 0.2

        controller = Mock()
        controller.trigger_active.return_value = trigger_active
        controller.take_hand_motion.return_value = (2, -1)
        controller.target_class = 0
        controller.target_y_ratio = 0.5
        controller.fov_radius = 150.0
        controller.last_send_ms = 0.2
        controller.active_profile_number = 1
        controller.algorithm_warnings = list(self.warnings)
        controller.algorithm_notices = list(self.notices)
        controller.active_profile = self.profiles[0]
        # 暴露出来给别的测试断言管线有没有在该 reset 的时候 reset。
        self.controller = controller

        def move_toward(*_args, **_kwargs):
            self.order.append("move")
            # 管线在 move_toward 之后才读 active_profile, 所以这里换上的方案
            # 就是本帧记进日志的那个。第 n 帧用 profiles[n], 给完了就一直用最后一个。
            controller.active_profile = self.profiles[
                min(self.frame, len(self.profiles) - 1)
            ]
            self.frame += 1
            return (3, 0)

        controller.move_toward.side_effect = move_toward

        preview = Mock()
        preview.enabled = preview_enabled
        preview.due = preview_enabled
        preview.shows_frame = shows_frame
        preview.publish.side_effect = lambda *_a, **_k: self.order.append("preview")

        pending = list(self.sequences)

        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop"

            def wait_next(_after, timeout=0.25):
                if not pending:
                    stop_file.touch()
                    return None
                now = time.perf_counter()
                return SimpleNamespace(
                    sequence=pending.pop(0),
                    first_packet_at=now,
                    ready_at=now,
                    frame=frame,
                    assembly_ms=1.0,
                    decode_ms=0.5,
                )

            source = Mock()
            source.fps = 240.0
            source.error = None
            source.wait_next.side_effect = wait_next

            config = load_config(_CONFIG, validate_model=False)
            if kmbox_enabled is not None:
                config = replace(config, kmbox=replace(config.kmbox, enabled=kmbox_enabled))
            with (
                patch("rhodes_fast.pipeline.YoloDetector", return_value=detector),
                patch("rhodes_fast.pipeline.KmboxController", return_value=controller),
                patch("rhodes_fast.pipeline.create_source", return_value=source),
                patch("rhodes_fast.pipeline.PreviewPublisher", return_value=preview) as publisher_class,
            ):
                run_pipeline(
                    config,
                    stop_file=stop_file,
                    preview_port=54321,
                    latency_log=latency_log,
                    trail_settings_file=trail_settings_file,
                )
            self.preview = preview
            self.detector = detector
            self.publisher_options = publisher_class.call_args.kwargs
            self.trail_overlay = publisher_class.call_args.kwargs["trail_overlay"]


class PipelineLoopTests(unittest.TestCase):
    def _order(self, **kwargs) -> list[str]:
        harness = _Harness([1])
        harness.run(**kwargs)
        return harness.order

    def _samples(self, sequences: list[int]) -> list[LatencySample]:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "latency.csv"
            _Harness(sequences).run(latency_log=log)
            return read_latency_log(log)

    def test_mouse_command_is_sent_before_the_preview_is_rendered(self) -> None:
        # 预览渲染做两次全帧拷贝 + addWeighted + 最多三次 JPEG 编码, 同步阻塞。
        # 它夹在检测和发指令之间的话, 每一帧的鼠标指令都被白白推迟。
        self.assertEqual(self._order(), ["move", "preview"])

    def test_the_frame_is_still_previewed_when_the_preview_tab_is_open(self) -> None:
        self.assertIn("preview", self._order())

    def test_records_how_many_frames_the_loop_skipped(self) -> None:
        # 主循环跟不上时 wait_next 直接跳到最新帧, 中间那些帧被静默丢掉。
        # 生产运行时完全看不到丢了多少, 只有 pipeline_benchmark 统计过。
        self.assertEqual([sample.skipped for sample in self._samples([1, 5, 6])], [0, 3, 0])

    def test_the_first_frame_never_counts_as_a_skip(self) -> None:
        # 管线可能在画面流跑了一阵之后才启动, 首帧序号本来就不是 1。
        self.assertEqual([sample.skipped for sample in self._samples([50, 51])], [0, 0])

    def test_a_runtime_algorithm_switch_reaches_the_console(self) -> None:
        # 界面靠读管线的 stdout 才能告诉用户「切好了」。不打印的话下拉框改完
        # 界面上只剩一句「正在切换…」, 永远等不到回音。
        harness = _Harness([1], notices=["控制方案 1 的算法已切换为 inflight_ff"])
        printed = io.StringIO()
        with redirect_stdout(printed):
            harness.run()
        self.assertIn("inflight_ff", printed.getvalue())

    def test_a_switch_notice_is_printed_once_not_every_frame(self) -> None:
        harness = _Harness([1, 2, 3, 4], notices=["控制方案 1 的算法已切换为 pd"])
        printed = io.StringIO()
        with redirect_stdout(printed):
            harness.run()
        self.assertEqual(printed.getvalue().count("已切换为 pd"), 1)

    def test_the_algorithm_fallback_warning_reaches_the_console(self) -> None:
        # 这条警告在生产代码里躺了一整个版本没人调用——收集了然后被扔掉。
        # 光测那个格式化函数测不出这个毛病, 必须断言它真的走到了 stdout。
        harness = _Harness([1], warnings=["算法 my_kalman 找不到，已回退到 p"])
        printed = io.StringIO()
        with redirect_stdout(printed):
            harness.run()
        output = printed.getvalue()
        self.assertIn("my_kalman", output)
        self.assertIn("!!", output)


class PipelineTrailTests(unittest.TestCase):
    """主循环每帧往轨迹缓冲里写一行, 预览线程拿行号去画。"""

    def test_every_frame_records_what_moved_and_where_the_target_was(self) -> None:
        harness = _Harness([1, 2, 3])
        harness.run()

        recorder = harness.trail_overlay.recorder
        window = recorder.window(recorder.written - 1, 10)
        self.assertEqual(len(window), 3)
        np.testing.assert_array_equal(window.command, [[3, 0]] * 3)
        np.testing.assert_array_equal(window.hand, [[2, -1]] * 3)
        np.testing.assert_array_equal(window.error, [[0.0, 0.0]] * 3)
        self.assertTrue(np.all(window.track != 0))
        np.testing.assert_array_equal(window.profile, [0, 0, 0])

    def test_each_preview_frame_carries_its_own_row(self) -> None:
        harness = _Harness([1, 2, 3])
        harness.run()

        rows = [call.kwargs["trail_row"] for call in harness.preview.publish.call_args_list]
        self.assertEqual(rows, [0, 1, 2])

    def test_frames_without_the_trigger_record_no_profile_but_still_the_hand(self) -> None:
        # 松开触发键时手照样在动, 轨迹要画出来(灰白色)。
        harness = _Harness([1, 2])
        harness.run(trigger_active=False)

        window = harness.trail_overlay.recorder.window(1, 2)
        np.testing.assert_array_equal(window.profile, [-1, -1])
        np.testing.assert_array_equal(window.command, [[0, 0], [0, 0]])
        np.testing.assert_array_equal(window.hand, [[2, -1], [2, -1]])

    def test_the_preview_follows_the_settings_file_from_the_gui(self) -> None:
        settings = Path("gui-1.trail.json")
        harness = _Harness([1])
        harness.run(trail_settings_file=settings)

        self.assertEqual(harness.publisher_options["settings_file"], settings)

    def test_every_class_is_detected_for_a_preview_that_shows_the_frame(self) -> None:
        # 画面上要画出所有识别框, 所以预览那一帧做全类别检测。
        harness = _Harness([1])
        harness.run(shows_frame=True)
        self.assertIsNone(harness.detector.detect.call_args.kwargs["target_class"])

    def test_trail_only_skips_the_extra_detection_work(self) -> None:
        # 只看轨迹时识别框根本不画, 多检测的那些类别白算。
        harness = _Harness([1])
        harness.run(shows_frame=False)
        self.assertEqual(harness.detector.detect.call_args.kwargs["target_class"], 0)

    def test_without_kmbox_the_trail_says_it_needs_one(self) -> None:
        harness = _Harness([1])
        harness.run(kmbox_enabled=False)
        self.assertFalse(harness.trail_overlay.available)

        harness = _Harness([1])
        harness.run(kmbox_enabled=True)
        self.assertTrue(harness.trail_overlay.available)

    def test_a_finished_calibration_is_reported_in_the_log(self) -> None:
        harness = _Harness([1])
        harness.run()
        printed = io.StringIO()
        with redirect_stdout(printed):
            harness.trail_overlay.on_calibrated(
                Calibration(
                    lag_frames=7, px_per_command=0.0, px_per_hand=0.634,
                    hand_includes_commands=True, correlation=0.6, pairs=900.0,
                )
            )
        output = printed.getvalue()
        self.assertIn("0.63", output)
        self.assertIn("7", output)


if __name__ == "__main__":
    unittest.main()
