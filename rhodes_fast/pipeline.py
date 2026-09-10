from __future__ import annotations

import math
import statistics
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import AppConfig
from .detector import Detection, YoloDetector
from .kmbox_control import KmboxController
from .latency_log import LatencyLogWriter, LatencySample, estimate_loop_delay, read_latency_log
from .process_priority import keep_running_at_full_speed
from .obs_source import ObsClient, ObsScreenshotSource
from .preview import PreviewPublisher
from .udp_source import UdpSource


class FrameSource(Protocol):
    @property
    def error(self) -> str | None: ...

    @property
    def fps(self) -> float: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wait_next(self, after_sequence: int, timeout: float = 3.0): ...


class TargetSelector:
    def __init__(
        self,
        *,
        association_radius: float = 50.0,
        switch_ratio: float = 0.8,
        switch_frames: int = 3,
    ) -> None:
        self.association_radius = association_radius
        self.switch_ratio = switch_ratio
        self.switch_frames = switch_frames
        self._target: Detection | None = None
        self._pending_target: Detection | None = None
        self._pending_frames = 0
        self._selection_key: tuple[int, float] | None = None
        self.changed = False

    def reset(self) -> None:
        self._target = None
        self._clear_pending()
        self._selection_key = None
        self.changed = False

    def select(
        self,
        detections: list[Detection],
        frame_width: int,
        frame_height: int,
        target_y_ratio: float,
        fov_radius: float,
        target_class: int,
    ) -> Detection | None:
        selection_key = (target_class, target_y_ratio)
        if selection_key != self._selection_key:
            self._target = None
            self._clear_pending()
            self._selection_key = selection_key
        eligible = _eligible_targets(
            detections,
            frame_width,
            frame_height,
            target_y_ratio,
            fov_radius,
            target_class,
        )
        if not eligible:
            self.changed = self._target is not None
            self._target = None
            self._clear_pending()
            return None

        best_distance_sq, best = min(eligible, key=lambda item: item[0])
        if self._target is None:
            self._target = best
            self._clear_pending()
            self.changed = True
            return best

        previous_x = self._target.center_x
        previous_y = self._target.aim_y(target_y_ratio)
        _, matched = min(
            eligible,
            key=lambda item: (item[1].center_x - previous_x) ** 2
            + (item[1].aim_y(target_y_ratio) - previous_y) ** 2,
        )
        association_distance_sq = (
            (matched.center_x - previous_x) ** 2
            + (matched.aim_y(target_y_ratio) - previous_y) ** 2
        )
        target_width = abs(self._target.x2 - self._target.x1)
        target_height = abs(self._target.y2 - self._target.y1)
        association_limit = min(
            self.association_radius,
            max(12.0, math.hypot(target_width, target_height)),
        )
        if association_distance_sq > association_limit * association_limit:
            self._target = best
            self._clear_pending()
            self.changed = True
            return best

        center_x = frame_width * 0.5
        center_y = frame_height * 0.5
        matched_distance_sq = (
            (matched.center_x - center_x) ** 2
            + (matched.aim_y(target_y_ratio) - center_y) ** 2
        )
        should_switch = (
            best is not matched
            and best_distance_sq < matched_distance_sq * self.switch_ratio * self.switch_ratio
        )
        if not should_switch:
            self._target = matched
            self._clear_pending()
            self.changed = False
            return self._target

        if self._pending_target is None:
            self._pending_target = best
            self._pending_frames = 1
        else:
            pending_distance_sq = (
                (best.center_x - self._pending_target.center_x) ** 2
                + (best.aim_y(target_y_ratio) - self._pending_target.aim_y(target_y_ratio)) ** 2
            )
            if pending_distance_sq <= self.association_radius * self.association_radius:
                self._pending_target = best
                self._pending_frames += 1
            else:
                self._pending_target = best
                self._pending_frames = 1

        if self._pending_frames >= self.switch_frames:
            self._target = best
            self._clear_pending()
            self.changed = True
        else:
            self._target = matched
            self.changed = False
        return self._target

    def _clear_pending(self) -> None:
        self._pending_target = None
        self._pending_frames = 0


def select_target(
    detections: list[Detection],
    frame_width: int,
    frame_height: int,
    target_y_ratio: float,
    fov_radius: float,
    target_class: int,
) -> Detection | None:
    eligible = _eligible_targets(
        detections,
        frame_width,
        frame_height,
        target_y_ratio,
        fov_radius,
        target_class,
    )
    return min(eligible, key=lambda item: item[0])[1] if eligible else None


def _eligible_targets(
    detections: list[Detection],
    frame_width: int,
    frame_height: int,
    target_y_ratio: float,
    fov_radius: float,
    target_class: int,
) -> list[tuple[float, Detection]]:
    center_x = frame_width * 0.5
    center_y = frame_height * 0.5
    eligible: list[tuple[float, Detection]] = []
    for detection in detections:
        if detection.class_id != target_class:
            continue
        dx = detection.center_x - center_x
        dy = detection.aim_y(target_y_ratio) - center_y
        distance_sq = dx * dx + dy * dy
        if distance_sq <= fov_radius * fov_radius:
            eligible.append((distance_sq, detection))
    return eligible


def run_pipeline(
    config: AppConfig,
    stop_file: Path | None = None,
    preview_port: int | None = None,
    preview_enable_file: Path | None = None,
    runtime_aim_file: Path | None = None,
    latency_log: Path | None = None,
) -> None:
    for note in keep_running_at_full_speed():
        print(note)
    print(_text(config, "正在加载模型和加速引擎...", "Loading model and acceleration engine..."))
    detector = YoloDetector(config.model)
    detector.warmup()
    print(_cuda_graph_status(config, detector))
    print(_gpu_preprocess_status(config, detector))
    print(_text(config, "模型已就绪，正在连接画面输入和 KMBox...", "Model ready. Connecting input and KMBox..."))
    controller = KmboxController(config.kmbox, config.aim, runtime_aim_file, profiles=config.aim_profiles)
    controller.connect()
    target_selector = TargetSelector()
    source = create_source(config)
    preview = (
        PreviewPublisher(preview_port, enabled_file=preview_enable_file)
        if preview_port is not None
        else None
    )
    source.start()
    print(
        _text(
            config,
            f"模型：{config.model.path.name} | 加速：{detector.provider}",
            f"Model: {config.model.path.name} | provider: {detector.provider}",
        )
    )
    print(
        _text(
            config,
            f"输入：{source_label(config)} | KMBox：{config.kmbox.host}:{config.kmbox.port}",
            f"Input: {source_label(config)} | KMBox: {config.kmbox.host}:{config.kmbox.port}",
        )
    )

    latency_writer = LatencyLogWriter(latency_log) if latency_log is not None else None
    if latency_writer is not None:
        print(
            _text(
                config,
                f"延迟日志：正在记录到 {latency_log}",
                f"Latency log: recording to {latency_log}",
            )
        )

    sequence = 0
    frames = 0
    moved = 0
    dropped = 0
    track_counter = 1
    latency_samples: list[tuple[float, float, float, float, float, float, float, float]] = []
    report_at = time.perf_counter()
    wait_report_at = 0.0
    try:
        while stop_file is None or not stop_file.exists():
            snapshot = source.wait_next(sequence, timeout=0.25)
            if snapshot is None:
                now = time.perf_counter()
                if now - wait_report_at >= 1.0:
                    error = source.error or _text(config, "正在等待第一帧画面", "waiting for the first input frame")
                    print(_text(config, f"输入：{_source_error(error)}", f"Input: {error}"))
                    wait_report_at = now
                continue
            # 主循环跟不上时 wait_next 直接跳到最新帧, 中间那些帧被静默丢掉。
            # 首帧不算: 管线可能在画面流跑了一阵之后才启动, 序号本来就不是 1。
            skipped = max(0, snapshot.sequence - sequence - 1) if sequence else 0
            dropped += skipped
            sequence = snapshot.sequence
            processing_started = time.perf_counter()
            queue_ms = max(0.0, (processing_started - snapshot.ready_at) * 1000.0)
            controller.refresh_runtime_settings()
            show_all_classes = preview is not None and preview.due
            detections = detector.detect(
                snapshot.frame,
                target_class=None if show_all_classes else controller.target_class,
            )
            target = target_selector.select(
                detections,
                snapshot.frame.shape[1],
                snapshot.frame.shape[0],
                controller.target_y_ratio,
                controller.fov_radius,
                controller.target_class,
            )
            if target_selector.changed:
                track_counter += 1
                controller.reset()
            send_ms = 0.0
            movement = (0, 0)
            trigger_active = controller.trigger_active()
            if target is not None and trigger_active:
                movement = controller.move_toward(
                    target, snapshot.frame.shape[1], snapshot.frame.shape[0]
                )
                if movement != (0, 0):
                    moved += 1
                send_ms = controller.last_send_ms
            else:
                controller.reset()
            completed_at = time.perf_counter()
            total_ms = max(0.0, (completed_at - snapshot.first_packet_at) * 1000.0)
            if latency_writer is not None:
                # 每帧都记。发指令需要按住瞄准键, 但看响应不需要——准心已经动了,
                # 目标还在画面里, 误差照样能测。只记按键帧会把拉枪的响应丢掉。
                latency_writer.write(
                    LatencySample(
                        monotonic_ms=processing_started * 1000.0,
                        sequence=snapshot.sequence,
                        error_x=(
                            target.center_x - snapshot.frame.shape[1] * 0.5
                            if target is not None
                            else 0.0
                        ),
                        error_y=(
                            target.aim_y(controller.target_y_ratio)
                            - snapshot.frame.shape[0] * 0.5
                            if target is not None
                            else 0.0
                        ),
                        dx=movement[0],
                        dy=movement[1],
                        trigger=trigger_active,
                        track_id=track_counter if target is not None else 0,
                        skipped=skipped,
                        total_ms=total_ms,
                        assembly_ms=snapshot.assembly_ms,
                        decode_ms=snapshot.decode_ms,
                        queue_ms=queue_ms,
                        preprocess_ms=detector.last_preprocess_ms,
                        inference_ms=detector.last_inference_ms,
                        postprocess_ms=detector.last_postprocess_ms,
                        send_ms=send_ms,
                    )
                )
            latency_samples.append(
                (
                    total_ms,
                    snapshot.assembly_ms,
                    snapshot.decode_ms,
                    queue_ms,
                    detector.last_preprocess_ms,
                    detector.last_inference_ms,
                    detector.last_postprocess_ms,
                    send_ms,
                )
            )
            # 预览渲染放到本帧最后: 它做两次全帧拷贝 + addWeighted + 最多三次 JPEG
            # 编码, 而且是同步阻塞的。夹在检测和发指令之间会白白推迟鼠标指令。
            if preview is not None:
                preview.publish(
                    snapshot.frame,
                    detections,
                    target,
                    target_y_ratio=controller.target_y_ratio,
                    fov_radius=controller.fov_radius,
                    inference_ms=detector.last_inference_ms,
                    detection_ms=detector.last_detection_ms,
                )
            frames += 1

            now = time.perf_counter()
            if now - report_at >= 1.0:
                interval = now - report_at
                english = (
                    f"capture={source.fps:6.1f} fps  processed={frames / interval:6.1f} fps  "
                    f"infer={detector.last_inference_ms:6.2f} ms  detect={detector.last_detection_ms:6.2f} ms  "
                    f"detections={len(detections):2d}  aim-class={controller.target_class}  "
                    f"profile={controller.active_profile_number or '-'}  moves={moved:3d}  "
                    f"dropped={dropped:3d}"
                )
                chinese = (
                    f"采集={source.fps:6.1f} 帧/秒  处理={frames / interval:6.1f} 帧/秒  "
                    f"推理={detector.last_inference_ms:6.2f} 毫秒  检测={detector.last_detection_ms:6.2f} 毫秒  "
                    f"检测={len(detections):2d}  自瞄标签={controller.target_class}  "
                    f"方案={controller.active_profile_number or '-'}  移动={moved:3d}  "
                    f"丢帧={dropped:3d}"
                )
                print(_text(config, chinese, english))
                means = tuple(statistics.mean(values) for values in zip(*latency_samples))
                total_p95 = _percentile([sample[0] for sample in latency_samples], 0.95)
                latency_en = (
                    f"local latency avg={means[0]:5.2f} ms p95={total_p95:5.2f} ms | "
                    f"assemble={means[1]:.2f} decode={means[2]:.2f} queue={means[3]:.2f} "
                    f"pre={means[4]:.2f} infer={means[5]:.2f} post={means[6]:.2f} kmbox={means[7]:.2f} ms"
                )
                latency_zh = (
                    f"接收端总延迟 平均={means[0]:5.2f} 毫秒 P95={total_p95:5.2f} 毫秒 | "
                    f"重组={means[1]:.2f} 解码={means[2]:.2f} 排队={means[3]:.2f} "
                    f"预处理={means[4]:.2f} 推理={means[5]:.2f} 后处理={means[6]:.2f} KMBox={means[7]:.2f} 毫秒"
                )
                print(_text(config, latency_zh, latency_en))
                frames = 0
                moved = 0
                dropped = 0
                latency_samples.clear()
                report_at = now
    except KeyboardInterrupt:
        print(_text(config, "正在停止...", "Stopping..."))
    finally:
        if stop_file is not None and stop_file.exists():
            print(_text(config, "正在停止...", "Stopping..."))
        source.stop()
        controller.close()
        if preview is not None:
            preview.close()
        if latency_writer is not None:
            latency_writer.close()
            print(_latency_report(config, latency_writer))


def compare_latency_logs(config: AppConfig, paths: list[Path]) -> str:
    """把几份延迟日志并排列出来, 用于 A/B 对比不同的采集方式或参数。"""
    lines = [
        _text(
            config,
            f"{'文件':<26}{'行数':>8}{'回路帧':>8}{'回路ms':>9}"
            f"{'帧间隔':>8}{'AI机侧':>8}{'游戏机+网络':>12}{'相关':>7}{'配对':>7}{'丢帧':>7}",
            f"{'file':<26}{'rows':>8}{'frames':>8}{'loop ms':>9}"
            f"{'interval':>10}{'local':>8}{'remote':>9}{'corr':>7}{'pairs':>7}{'dropped':>9}",
        )
    ]
    for path in paths:
        try:
            samples = read_latency_log(path)
        except OSError as exc:
            lines.append(f"{path.name:<26}  {exc.strerror or exc}")
            continue
        dropped = sum(sample.skipped for sample in samples)
        estimate = estimate_loop_delay(samples)
        if estimate is None:
            lines.append(
                _text(
                    config,
                    f"{path.name:<26}{len(samples):>8}   样本不足或准心几乎没动",
                    f"{path.name:<26}{len(samples):>8}   not enough aiming movement",
                )
            )
            continue
        lines.append(
            f"{path.name:<26}{len(samples):>8}{estimate.frames:>8}"
            f"{estimate.loop_ms:>9.1f}{estimate.frame_interval_ms:>8.2f}"
            f"{estimate.ai_side_ms:>8.2f}{estimate.remote_ms:>12.1f}"
            f"{estimate.correlation:>7.3f}{estimate.pairs:>7}{dropped:>7}"
        )
    return "\n".join(lines)


def _latency_report(config: AppConfig, writer: LatencyLogWriter) -> str:
    lines = [
        _text(
            config,
            f"延迟日志已保存：{writer.path}（{writer.rows} 行）",
            f"Latency log saved to {writer.path} ({writer.rows} rows)",
        )
    ]
    try:
        estimate = estimate_loop_delay(read_latency_log(writer.path))
    except OSError:
        return lines[0]
    if estimate is None:
        lines.append(
            _text(
                config,
                "  样本不足或准心几乎没动，无法估计回路延迟。多打几场再看。",
                "  Not enough aiming movement to estimate the loop delay yet.",
            )
        )
        return "\n".join(lines)
    trust = (
        _text(config, "可信", "reliable")
        if estimate.correlation >= 0.6
        else _text(config, "偏弱，建议多打几场", "weak, collect more engagements")
    )
    lines.append(
        _text(
            config,
            f"  回路延迟 ≈ {estimate.frames} 帧 / {estimate.loop_ms:.1f} 毫秒"
            f"（帧间隔 {estimate.frame_interval_ms:.2f} 毫秒，"
            f"相关系数 {estimate.correlation:.3f}，{trust}）",
            f"  loop delay ~ {estimate.frames} frames / {estimate.loop_ms:.1f} ms"
            f" (frame interval {estimate.frame_interval_ms:.2f} ms,"
            f" correlation {estimate.correlation:.3f}, {trust})",
        )
    )
    # 峰往往是平的: 真实数据里相邻两个滞后的相关系数可能只差 0.003。
    # 只报一个整数会让人以为精度比实际高, 所以把次高一并列出。
    lines.append(
        _text(
            config,
            f"  次高是 {estimate.runner_up_frames} 帧"
            f"（相关系数 {estimate.runner_up_correlation:.3f}）"
            f"——两者越接近, 说明延迟本身在这两个值之间抖动",
            f"  runner-up {estimate.runner_up_frames} frames"
            f" (correlation {estimate.runner_up_correlation:.3f})"
            f" - the closer these are, the more the delay itself jitters",
        )
    )
    lines.append(
        _text(
            config,
            f"  其中 AI 机侧 {estimate.ai_side_ms:.1f} 毫秒，"
            f"游戏机+网络 ≈ {estimate.remote_ms:.1f} 毫秒"
            f"（含约 {estimate.frame_interval_ms / 2:.1f} 毫秒的系统性高估）",
            f"  local {estimate.ai_side_ms:.1f} ms,"
            f" remote ~ {estimate.remote_ms:.1f} ms"
            f" (overstated by about {estimate.frame_interval_ms / 2:.1f} ms)",
        )
    )
    lines.append(
        _text(
            config,
            f"  依据 {estimate.pairs} 组拉枪配对"
            f"（指令幅度 ≥ {estimate.command_threshold:.0f} 像素）",
            f"  from {estimate.pairs} flick pairs"
            f" (command >= {estimate.command_threshold:.0f} px)",
        )
    )
    return "\n".join(lines)


def benchmark_model(config: AppConfig, iterations: int, stop_file: Path | None = None) -> None:
    print(_text(config, "正在加载模型和加速引擎...", "Loading model and acceleration engine..."))
    detector = YoloDetector(config.model)
    detector.warmup()
    print(_cuda_graph_status(config, detector))
    print(_gpu_preprocess_status(config, detector))
    blank = np.zeros((detector.input_height, detector.input_width, 3), dtype=np.uint8)
    timings: list[float] = []
    total_timings: list[float] = []
    for _ in range(iterations):
        if stop_file is not None and stop_file.exists():
            print(_text(config, "模型测速已停止。", "Benchmark stopped."))
            return
        detector.detect(blank)
        timings.append(detector.last_inference_ms)
        total_timings.append(detector.last_detection_ms)
    ordered = sorted(timings)
    total_ordered = sorted(total_timings)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    total_p95 = total_ordered[min(len(total_ordered) - 1, round(0.95 * (len(total_ordered) - 1)))]
    print(_text(config, f"加速方式：{detector.provider}", f"Provider: {detector.provider}"))
    print(_text(config, f"模型：{config.model.path}", f"Model: {config.model.path}"))
    print(
        _text(
            config,
            f"推理耗时：平均={statistics.mean(timings):.3f} 毫秒，p50={statistics.median(timings):.3f} 毫秒，p95={p95:.3f} 毫秒",
            f"Inference: mean={statistics.mean(timings):.3f} ms, p50={statistics.median(timings):.3f} ms, p95={p95:.3f} ms",
        )
    )
    print(
        _text(
            config,
            f"完整检测：平均={statistics.mean(total_timings):.3f} 毫秒，"
            f"p50={statistics.median(total_timings):.3f} 毫秒，p95={total_p95:.3f} 毫秒",
            f"Full detect: mean={statistics.mean(total_timings):.3f} ms, "
            f"p50={statistics.median(total_timings):.3f} ms, p95={total_p95:.3f} ms",
        )
    )


def check_connections(config: AppConfig, stop_file: Path | None = None) -> None:
    failures: list[str] = []
    if config.input.mode == "obs_websocket":
        client = ObsClient(config.obs)
        try:
            client.connect()
            source_name = config.obs.source_name or client.current_program_scene()
            frame = client.screenshot(source_name)
            print(
                _text(
                    config,
                    f"OBS 正常：来源={source_name!r}，画面={frame.shape[1]}x{frame.shape[0]}",
                    f"OBS OK: source={source_name!r}, frame={frame.shape[1]}x{frame.shape[0]}",
                )
            )
        except Exception as exc:
            failures.append(f"OBS: {exc}")
            print(_text(config, f"OBS 连接失败：{exc}", f"OBS FAILED: {exc}"))
        finally:
            client.close()
    else:
        source = create_source(config)
        source.start()
        try:
            snapshot = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and (stop_file is None or not stop_file.exists()):
                snapshot = source.wait_next(0, timeout=0.25)
                if snapshot is not None:
                    break
            if stop_file is not None and stop_file.exists():
                print(_text(config, "连接测试已停止。", "Connection check stopped."))
                return
            if snapshot is None:
                raise RuntimeError(source.error or "no UDP frame received within 3 seconds")
            print(
                _text(
                    config,
                    f"UDP 正常：画面={snapshot.frame.shape[1]}x{snapshot.frame.shape[0]}",
                    f"UDP OK: frame={snapshot.frame.shape[1]}x{snapshot.frame.shape[0]}",
                )
            )
        except Exception as exc:
            failures.append(f"UDP: {exc}")
            print(_text(config, f"UDP 连接失败：{exc}", f"UDP FAILED: {exc}"))
        finally:
            source.stop()

    controller = KmboxController(config.kmbox, config.aim, profiles=config.aim_profiles)
    try:
        controller.connect()
        print(
            _text(config, "KMBox 正常", "KMBox OK")
            if config.kmbox.enabled
            else _text(config, "KMBox 已禁用", "KMBox disabled")
        )
    except Exception as exc:
        failures.append(f"KMBox: {exc}")
        print(_text(config, f"KMBox 连接失败：{exc}", f"KMBox FAILED: {exc}"))
    finally:
        controller.close()
    if failures:
        raise RuntimeError("; ".join(failures))


def create_source(config: AppConfig) -> FrameSource:
    if config.input.mode == "obs_websocket":
        return ObsScreenshotSource(config.obs)
    return UdpSource(config.udp, config.input.mode)


def source_label(config: AppConfig) -> str:
    if config.input.mode == "obs_websocket":
        return f"OBS WebSocket {config.obs.host}:{config.obs.port}"
    if config.input.mode == "udp_video":
        kind = _text(config, "MPEG-TS/H.264 视频流", "MPEG-TS/H.264 video stream")
    else:
        kind = _text(config, "JPEG 分片", "JPEG datagrams")
    return f"UDP {kind} {config.udp.host}:{config.udp.port}"


def _text(config: AppConfig, chinese: str, english: str) -> str:
    return chinese if config.ui.language == "zh" else english


def _cuda_graph_status(config: AppConfig, detector: YoloDetector) -> str:
    if detector.cuda_graph_enabled:
        return _text(config, "CUDA Graph：已启用", "CUDA Graph: enabled")
    if not config.model.cuda_graph:
        return _text(config, "CUDA Graph：已关闭", "CUDA Graph: disabled")
    reason = detector.cuda_graph_fallback_reason or "unknown error"
    return _text(
        config,
        f"CUDA Graph：不可用，已自动回退（{reason}）",
        f"CUDA Graph: unavailable; fell back automatically ({reason})",
    )


def _gpu_preprocess_status(config: AppConfig, detector: YoloDetector) -> str:
    if detector.gpu_preprocess_enabled:
        return _text(config, "GPU 预处理：已启用", "GPU preprocessing: enabled")
    if not config.model.gpu_preprocess:
        return _text(config, "GPU 预处理：已关闭", "GPU preprocessing: disabled")
    reason = detector.gpu_preprocess_fallback_reason or "unknown error"
    return _text(
        config,
        f"GPU 预处理：不可用，已自动回退（{reason}）",
        f"GPU preprocessing: unavailable; fell back automatically ({reason})",
    )


def _source_error(error: str) -> str:
    translations = {
        "waiting for UDP JPEG frames": "正在等待 UDP JPEG 画面",
        "waiting for UDP MPEG-TS/H.264 video frames": "正在等待 UDP MPEG-TS/H.264 画面",
        "waiting for the first input frame": "正在等待第一帧画面",
        "discarded an oversized UDP JPEG frame": "已丢弃过大的 UDP JPEG 画面",
        "received an invalid fragmented UDP JPEG frame": "收到的 UDP JPEG 分片无法解码",
    }
    return translations.get(error, error)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(quantile * (len(ordered) - 1)))]
