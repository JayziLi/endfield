from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from .config import AppConfig
from .detector import YoloDetector
from .pipeline import create_source, select_target, source_label


@dataclass(frozen=True, slots=True)
class PipelineTiming:
    total: float
    assembly: float
    decode: float
    queue: float
    preprocess: float
    inference: float
    postprocess: float
    target: float


_STAGES = tuple(PipelineTiming.__dataclass_fields__)


def summarize_timings(samples: list[PipelineTiming]) -> dict[str, dict[str, float]]:
    if not samples:
        raise ValueError("pipeline benchmark requires at least one timing sample")
    summary: dict[str, dict[str, float]] = {}
    for stage in _STAGES:
        values = np.asarray([getattr(sample, stage) for sample in samples], dtype=np.float64)
        summary[stage] = {
            "mean_ms": round(float(values.mean()), 6),
            "p50_ms": round(float(np.percentile(values, 50)), 6),
            "p95_ms": round(float(np.percentile(values, 95)), 6),
            "p99_ms": round(float(np.percentile(values, 99)), 6),
            "max_ms": round(float(values.max()), 6),
        }
    return summary


def run_pipeline_benchmark(
    config: AppConfig,
    iterations: int,
    *,
    stop_file: Path | None = None,
    output_path: Path | None = None,
    warmup_frames: int = 30,
) -> Path | None:
    text = lambda zh, en: zh if config.ui.language == "zh" else en
    print(text("正在加载模型和加速引擎...", "Loading model and acceleration engine..."))
    detector = YoloDetector(config.model)
    detector.warmup()
    source = create_source(config)
    source.start()
    print(
        text(
            f"管线测速：{source_label(config)}，预热 {warmup_frames} 帧，采样 {iterations} 帧",
            f"Pipeline benchmark: {source_label(config)}, {warmup_frames} warmup frames, {iterations} samples",
        )
    )
    print(text("安全模式：不启用预览，不连接或发送 KMBox。", "Safe mode: preview and KMBox are disabled."))

    sequence = 0
    skipped = 0
    samples: list[PipelineTiming] = []
    measurement_started = 0.0
    measurement_completed = 0.0
    capture_fps = 0.0
    no_frame_deadline = time.monotonic() + max(5.0, config.udp.timeout_seconds * 2.0)
    try:
        while len(samples) < iterations:
            if stop_file is not None and stop_file.exists():
                print(text("管线测速已停止。", "Pipeline benchmark stopped."))
                return None
            snapshot = source.wait_next(sequence, timeout=0.25)
            if snapshot is None:
                if time.monotonic() >= no_frame_deadline:
                    raise RuntimeError(source.error or "no input frame received during pipeline benchmark")
                continue
            no_frame_deadline = time.monotonic() + max(5.0, config.udp.timeout_seconds * 2.0)
            if sequence:
                skipped += max(0, snapshot.sequence - sequence - 1)
            sequence = snapshot.sequence
            processing_started = time.perf_counter()
            queue_ms = max(0.0, (processing_started - snapshot.ready_at) * 1000.0)
            detections = detector.detect(snapshot.frame, target_class=config.aim.target_class)
            target_started = time.perf_counter()
            select_target(
                detections,
                snapshot.frame.shape[1],
                snapshot.frame.shape[0],
                config.aim.target_y_ratio,
                config.aim.fov_radius,
                config.aim.target_class,
            )
            completed_at = time.perf_counter()
            if warmup_frames > 0:
                warmup_frames -= 1
                skipped = 0
                continue
            if not samples:
                measurement_started = processing_started
            samples.append(
                PipelineTiming(
                    total=max(0.0, (completed_at - snapshot.first_packet_at) * 1000.0),
                    assembly=snapshot.assembly_ms,
                    decode=snapshot.decode_ms,
                    queue=queue_ms,
                    preprocess=detector.last_preprocess_ms,
                    inference=detector.last_inference_ms,
                    postprocess=detector.last_postprocess_ms,
                    target=(completed_at - target_started) * 1000.0,
                )
            )
        measurement_completed = time.perf_counter()
        capture_fps = source.fps
    finally:
        source.stop()

    elapsed = max(measurement_completed - measurement_started, 1e-9)
    summary = summarize_timings(samples)
    report = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": {
            "path": str(config.model.path.resolve()),
            "sha256": _file_sha256(config.model.path),
            "provider_requested": config.model.provider,
            "providers_active": detector.session.get_providers(),
            "cuda_graph": detector.cuda_graph_enabled,
            "gpu_preprocess": detector.gpu_preprocess_enabled,
            "input_width": detector.input_width,
            "input_height": detector.input_height,
        },
        "input": {
            "mode": config.input.mode,
            "source": source_label(config),
            "width": config.udp.width if config.input.mode != "obs_websocket" else config.obs.width,
            "height": config.udp.height if config.input.mode != "obs_websocket" else config.obs.height,
            "capture_fps": round(capture_fps, 3),
        },
        "benchmark": {
            "samples": len(samples),
            "processed_fps": round(len(samples) / elapsed, 3),
            "skipped_frames": skipped,
            "target_class": config.aim.target_class,
            "preview_enabled": False,
            "kmbox_enabled": False,
        },
        "environment": _environment_info(),
        "summary": summary,
        "samples_ms": [asdict(sample) for sample in samples],
    }
    saved_path = _save_report(report, output_path)
    _print_summary(config, summary, report["benchmark"], saved_path)
    return saved_path


def _save_report(report: dict, output_path: Path | None) -> Path:
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = Path(".cache") / "benchmarks" / f"pipeline-{timestamp}.json"
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def _print_summary(config: AppConfig, summary: dict, benchmark: dict, saved_path: Path) -> None:
    text = lambda zh, en: zh if config.ui.language == "zh" else en
    print(
        text(
            f"管线测速完成：{benchmark['samples']} 帧，处理={benchmark['processed_fps']:.1f} 帧/秒，跳过={benchmark['skipped_frames']} 帧",
            f"Pipeline benchmark complete: {benchmark['samples']} frames, processed={benchmark['processed_fps']:.1f} fps, skipped={benchmark['skipped_frames']}",
        )
    )
    labels = {
        "total": ("接收端总计", "receiver total"),
        "assembly": ("UDP 重组", "UDP assembly"),
        "decode": ("图像解码", "image decode"),
        "queue": ("最新帧等待", "latest-frame queue"),
        "preprocess": ("模型预处理", "model preprocess"),
        "inference": ("模型推理", "model inference"),
        "postprocess": ("YOLO/NMS 后处理", "YOLO/NMS postprocess"),
        "target": ("目标选择", "target selection"),
    }
    for stage in _STAGES:
        values = summary[stage]
        label = text(*labels[stage])
        print(
            f"{label}: avg={values['mean_ms']:.3f} ms  p50={values['p50_ms']:.3f} ms  "
            f"p95={values['p95_ms']:.3f} ms  p99={values['p99_ms']:.3f} ms  max={values['max_ms']:.3f} ms"
        )
    print(text(f"报告：{saved_path}", f"Report: {saved_path}"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _environment_info() -> dict:
    gpu = "unavailable"
    driver = "unavailable"
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        line = result.stdout.strip().splitlines()[0]
        gpu, driver = (item.strip() for item in line.split(",", 1))
    except (FileNotFoundError, IndexError, OSError, subprocess.SubprocessError, ValueError):
        pass
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "onnxruntime": ort.__version__,
        "gpu": gpu,
        "driver": driver,
    }
