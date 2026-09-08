from __future__ import annotations

import socket
import time
from pathlib import Path

import cv2
import numpy as np

from .detector import Detection


class PreviewPublisher:
    def __init__(self, port: int, max_fps: float = 20.0, enabled_file: Path | None = None):
        self.address = ("127.0.0.1", port)
        self.interval = 1.0 / max_fps
        self.next_frame_at = 0.0
        self.enabled_file = enabled_file
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)

    @property
    def enabled(self) -> bool:
        return self.enabled_file is None or self.enabled_file.exists()

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
        preview = render_preview(
            frame,
            detections,
            target,
            target_y_ratio=target_y_ratio,
            fov_radius=fov_radius,
            inference_ms=inference_ms,
            detection_ms=detection_ms,
        )
        payload = _encode_datagram(preview)
        if payload is None:
            return
        try:
            self.socket.sendto(payload, self.address)
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
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
