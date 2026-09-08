from __future__ import annotations

import socket
import threading
import time

import cv2
import numpy as np

from .config import UdpConfig
from .obs_source import FrameSnapshot


class UdpSource:
    def __init__(self, config: UdpConfig, mode: str):
        self.config = config
        self.mode = mode
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: FrameSnapshot | None = None
        self._error: str | None = None
        self._frames = 0
        self._started_at = 0.0
        self._socket: socket.socket | None = None
        self._container = None

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def fps(self) -> float:
        elapsed = time.perf_counter() - self._started_at
        return self._frames / elapsed if elapsed > 0 else 0.0

    @property
    def label(self) -> str:
        kind = "video" if self.mode == "udp_video" else "JPEG"
        return f"UDP {kind} {self.config.host}:{self.config.port}"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name="udp-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.mode == "udp_jpeg" and self._socket is not None:
            self._socket.close()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=max(3.0, self.config.timeout_seconds + 1.0))
            self._thread = None

    def wait_next(self, after_sequence: int, timeout: float = 3.0) -> FrameSnapshot | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._stop.is_set():
                if self._latest is not None and self._latest.sequence > after_sequence:
                    return self._latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def _run(self) -> None:
        runner = self._run_video if self.mode == "udp_video" else self._run_jpeg
        while not self._stop.is_set():
            try:
                runner()
            except Exception as exc:
                if not self._stop.is_set():
                    message = str(exc)
                    if self.mode == "udp_video" and (
                        "Immediate exit requested" in message or "timed out" in message.lower()
                    ):
                        message = "waiting for UDP MPEG-TS/H.264 video frames"
                    self._set_error(message)
                    self._stop.wait(0.5)
            finally:
                sock = self._socket
                container = self._container
                self._socket = None
                self._container = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if container is not None:
                    try:
                        container.close()
                    except OSError:
                        pass

    def _run_video(self) -> None:
        import av

        url = f"udp://@{self.config.host}:{self.config.port}"
        options = {
            "fflags": "nobuffer",
            "flags": "low_delay",
            "probesize": "131072",
            "analyzeduration": "100000",
            "fifo_size": str(self.config.fifo_packets),
            "overrun_nonfatal": "1",
        }
        self._container = av.open(
            url,
            mode="r",
            options=options,
            timeout=(self.config.timeout_seconds, self.config.timeout_seconds),
        )
        self._set_error(None)
        for decoded in self._container.decode(video=0):
            if self._stop.is_set():
                break
            decode_started = time.perf_counter()
            self._publish(
                decoded.to_ndarray(format="bgr24"),
                first_packet_at=decode_started,
                decode_started=decode_started,
            )
        if not self._stop.is_set():
            raise RuntimeError("UDP video stream ended")

    def _run_jpeg(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.config.socket_buffer_bytes)
        sock.settimeout(self.config.timeout_seconds)
        sock.bind((self.config.host, self.config.port))
        self._set_error(None)
        frame_bytes = bytearray()
        frame_started_at = 0.0
        while not self._stop.is_set():
            try:
                payload, _ = sock.recvfrom(65_507)
            except TimeoutError:
                self._set_error("waiting for UDP JPEG frames")
                continue
            received_at = time.perf_counter()
            if payload.startswith(b"\xff\xd8"):
                frame_bytes.clear()
                frame_started_at = received_at
            if not frame_bytes and not payload.startswith(b"\xff\xd8"):
                continue
            frame_bytes.extend(payload)
            if len(frame_bytes) > 8 * 1024 * 1024:
                frame_bytes.clear()
                frame_started_at = 0.0
                self._set_error("discarded an oversized UDP JPEG frame")
                continue
            end = frame_bytes.rfind(b"\xff\xd9")
            if end < 0:
                continue
            encoded = bytes(frame_bytes[: end + 2])
            frame_bytes.clear()
            decode_started = time.perf_counter()
            assembly_ms = (decode_started - frame_started_at) * 1000.0
            frame = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                frame_started_at = 0.0
                self._set_error("received an invalid fragmented UDP JPEG frame")
                continue
            self._publish(
                frame,
                first_packet_at=frame_started_at,
                assembly_ms=assembly_ms,
                decode_started=decode_started,
            )
            frame_started_at = 0.0

    def _publish(
        self,
        frame: np.ndarray,
        *,
        first_packet_at: float | None = None,
        assembly_ms: float = 0.0,
        decode_started: float | None = None,
    ) -> None:
        if frame.shape[1] != self.config.width or frame.shape[0] != self.config.height:
            frame = cv2.resize(frame, (self.config.width, self.config.height), interpolation=cv2.INTER_LINEAR)
        ready_at = time.perf_counter()
        first_packet_at = first_packet_at if first_packet_at is not None else ready_at
        decode_ms = (ready_at - decode_started) * 1000.0 if decode_started is not None else 0.0
        with self._condition:
            sequence = 1 if self._latest is None else self._latest.sequence + 1
            self._latest = FrameSnapshot(sequence, first_packet_at, ready_at, frame, assembly_ms, decode_ms)
            self._frames += 1
            self._error = None
            self._condition.notify_all()

    def _set_error(self, error: str | None) -> None:
        with self._condition:
            self._error = error
            self._condition.notify_all()
