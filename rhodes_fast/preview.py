from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .detector import Detection
from .process_priority import run_current_thread_below_normal


@dataclass(frozen=True, slots=True)
class _PreviewJob:
    frame: np.ndarray
    detections: list[Detection]
    target: Detection | None
    target_y_ratio: float
    fov_radius: float
    inference_ms: float
    detection_ms: float


class PreviewPublisher:
    """把画面推给 GUI, 渲染和编码全在后台线程里做。

    渲染一帧实测 0.78 毫秒 (p95 0.98), 而瞄准主循环 4.16 毫秒就得处理完一帧并把
    鼠标指令发出去。同步渲染等于每 50 毫秒给回路插一根 1 毫秒的刺。现在主循环只做
    一件事: 把帧的引用放进一个格子。帧是 imdecode 每次新分配的, 检测结果也是每帧
    新建的对象, 谁都不会回头改它们, 所以交出去不用拷贝。
    """

    def __init__(self, port: int, max_fps: float = 30.0, enabled_file: Path | None = None):
        self.address = ("127.0.0.1", port)
        self.interval = 1.0 / max_fps
        self.next_frame_at = 0.0
        self.enabled_file = enabled_file
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.dropped = 0
        self._pending: _PreviewJob | None = None
        self._idle = threading.Condition()
        self._closed = False
        self._worker: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.enabled_file is None or self.enabled_file.exists()

    @property
    def due(self) -> bool:
        """下一次 publish 是否会真正渲染。纯查询, 不推进帧率闸门。

        主循环靠它决定要不要为预览做更重的全类别检测——预览帧率远低于采集帧率,
        没必要让每一帧都付出那份代价。
        """
        return self.enabled and time.perf_counter() >= self.next_frame_at

    @property
    def is_rendering(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive()

    def publish(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        target: Detection | None,
        *,
        target_y_ratio: float,
        fov_radius: float,
        inference_ms: float,
        detection_ms: float,
    ) -> None:
        if not self.enabled:
            self.next_frame_at = 0.0
            return
        now = time.perf_counter()
        if now < self.next_frame_at:
            return
        self.next_frame_at = now + self.interval
        job = _PreviewJob(
            frame,
            detections,
            target,
            target_y_ratio,
            fov_radius,
            inference_ms,
            detection_ms,
        )
        with self._idle:
            if self._closed:
                return
            # 只留最新的一帧。渲染跟不上时丢掉过期画面, 绝不能让主循环排队等它——
            # 预览晚一帧没人看得出来, 主循环停一下准心就慢半拍。
            if self._pending is not None:
                self.dropped += 1
            self._pending = job
            self._start_worker()
            self._idle.notify()

    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._render_loop, name="preview-render", daemon=True
            )
            self._worker.start()

    def _render_loop(self) -> None:
        run_current_thread_below_normal()
        while True:
            with self._idle:
                while self._pending is None and not self._closed:
                    self._idle.wait()
                if self._closed:
                    return
                job = self._pending
                self._pending = None
            self._render_and_send(job)

    def _render_and_send(self, job: _PreviewJob) -> None:
        preview = render_preview(
            job.frame,
            job.detections,
            job.target,
            target_y_ratio=job.target_y_ratio,
            fov_radius=job.fov_radius,
            inference_ms=job.inference_ms,
            detection_ms=job.detection_ms,
        )
        payload = _encode_datagram(preview)
        if payload is None:
            return
        try:
            self.socket.sendto(payload, self.address)
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        with self._idle:
            self._closed = True
            self._pending = None
            self._idle.notify_all()
        worker = self._worker
        if worker is not None:
            # 等它把手上这帧做完再关 socket, 否则 sendto 会撞上已关闭的句柄。
            worker.join(timeout=1.0)
        self.socket.close()


def render_preview(
    frame: np.ndarray,
    detections: list[Detection],
    target: Detection | None,
    *,
    target_y_ratio: float,
    fov_radius: float,
    inference_ms: float,
    detection_ms: float,
) -> np.ndarray:
    preview = frame.copy()
    height, width = preview.shape[:2]
    center = (width // 2, height // 2)
    cv2.circle(preview, center, max(1, round(fov_radius)), (0, 210, 255), 1, cv2.LINE_AA)
    cv2.drawMarker(preview, center, (255, 255, 255), cv2.MARKER_CROSS, 12, 1, cv2.LINE_AA)

    for detection in detections:
        selected = detection is target or detection == target
        color = (40, 70, 255) if selected else (70, 220, 90)
        thickness = 2 if selected else 1
        x1 = max(0, min(width - 1, round(detection.x1)))
        y1 = max(0, min(height - 1, round(detection.y1)))
        x2 = max(0, min(width - 1, round(detection.x2)))
        y2 = max(0, min(height - 1, round(detection.y2)))
        cv2.rectangle(preview, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
        aim_point = (round(detection.center_x), round(detection.aim_y(target_y_ratio)))
        cv2.circle(preview, aim_point, 3 if selected else 2, color, -1, cv2.LINE_AA)
        label = f"ID {detection.class_id}  {detection.confidence:.2f}"
        text_y = max(13, y1 - 4)
        cv2.putText(preview, label, (x1, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)

    overlay = preview.copy()
    cv2.rectangle(overlay, (0, max(0, height - 24)), (width, height), (8, 12, 20), -1)
    cv2.addWeighted(overlay, 0.72, preview, 0.28, 0, preview)
    status = f"infer {inference_ms:.1f} ms | total {detection_ms:.1f} ms | targets {len(detections)}"
    cv2.putText(preview, status, (7, height - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (240, 245, 250), 1, cv2.LINE_AA)
    return preview


def _encode_datagram(frame: np.ndarray) -> bytes | None:
    for quality in (72, 55, 40):
        success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if success and encoded.nbytes <= 60_000:
            return encoded.tobytes()
    return None
