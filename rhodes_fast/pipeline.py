from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import AppConfig
from .detector import Detection, YoloDetector
from .kmbox_control import KmboxController
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


def select_target(
    detections: list[Detection],
    frame_width: int,
    frame_height: int,
    target_y_ratio: float,
    fov_radius: float,
    target_class: int,
) -> Detection | None:
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
    return min(eligible, key=lambda item: item[0])[1] if eligible else None


def run_pipeline(
    config: AppConfig,
    stop_file: Path | None = None,
    preview_port: int | None = None,
    preview_enable_file: Path | None = None,
    runtime_aim_file: Path | None = None,
) -> None:
    print(_text(config, "正在加载模型和加速引擎...", "Loading model and acceleration engine..."))
    detector = YoloDetector(config.model)
    detector.warmup()
    print(_cuda_graph_status(config, detector))
    print(_gpu_preprocess_status(config, detector))
    print(_text(config, "模型已就绪，正在连接画面输入和 KMBox...", "Model ready. Connecting input and KMBox..."))
    controller = KmboxController(config.kmbox, config.aim, runtime_aim_file)
    controller.connect()
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

    sequence = 0
    frames = 0
    moved = 0
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
            sequence = snapshot.sequence
            processing_started = time.perf_counter()
            queue_ms = max(0.0, (processing_started - snapshot.ready_at) * 1000.0)
            controller.refresh_runtime_settings()
            show_all_classes = preview is not None and preview.enabled
            detections = detector.detect(
                snapshot.frame,
                target_class=None if show_all_classes else controller.target_class,
            )
            target = select_target(
                detections,
                snapshot.frame.shape[1],
                snapshot.frame.shape[0],
                controller.target_y_ratio,
                controller.fov_radius,
                controller.target_class,
            )
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
            send_ms = 0.0
            if target is not None and controller.trigger_active():
                if controller.move_toward(target, snapshot.frame.shape[1], snapshot.frame.shape[0]) != (0, 0):
                    moved += 1
                send_ms = controller.last_send_ms
            else:
                controller.reset()
            completed_at = time.perf_counter()
            total_ms = max(0.0, (completed_at - snapshot.first_packet_at) * 1000.0)
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
            frames += 1

            now = time.perf_counter()
            if now - report_at >= 1.0:
                interval = now - report_at
                english = (
                    f"capture={source.fps:6.1f} fps  processed={frames / interval:6.1f} fps  "
                    f"infer={detector.last_inference_ms:6.2f} ms  detect={detector.last_detection_ms:6.2f} ms  "
                    f"detections={len(detections):2d}  aim-class={controller.target_class}  moves={moved:3d}"
                )
                chinese = (
                    f"采集={source.fps:6.1f} 帧/秒  处理={frames / interval:6.1f} 帧/秒  "
                    f"推理={detector.last_inference_ms:6.2f} 毫秒  检测={detector.last_detection_ms:6.2f} 毫秒  "
                    f"检测={len(detections):2d}  自瞄标签={controller.target_class}  移动={moved:3d}"
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

    controller = KmboxController(config.kmbox, config.aim)
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
