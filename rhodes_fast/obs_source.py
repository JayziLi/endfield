from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
import websocket

from .config import ObsConfig


class ObsProtocolError(RuntimeError):
    pass


class ObsClient:
    def __init__(self, config: ObsConfig):
        self.config = config
        self._socket: websocket.WebSocket | None = None
        self._current_program_scene: str | None = None

    def connect(self) -> None:
        self.close()
        url = f"ws://{self.config.host}:{self.config.port}"
        self._socket = websocket.create_connection(
            url,
            timeout=self.config.timeout_seconds,
            subprotocols=["obswebsocket.json"],
        )
        hello = self._recv_json()
        if hello.get("op") != 0:
            raise ObsProtocolError("OBS did not send a Hello message")

        event_subscriptions = 4 if not self.config.source_name else 0
        identify = {
            "rpcVersion": min(int(hello["d"].get("rpcVersion", 1)), 1),
            "eventSubscriptions": event_subscriptions,
        }
        auth = hello["d"].get("authentication")
        if auth:
            if not self.config.password:
                raise ObsProtocolError("OBS requires a WebSocket password")
            secret = _sha256_b64(self.config.password + auth["salt"])
            identify["authentication"] = _sha256_b64(secret + auth["challenge"])

        self._send_json({"op": 1, "d": identify})
        identified = self._recv_json()
        if identified.get("op") != 2:
            raise ObsProtocolError("OBS WebSocket identification failed")

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def request(self, request_type: str, request_data: dict | None = None) -> dict:
        request_id = uuid.uuid4().hex
        payload = {"requestType": request_type, "requestId": request_id}
        if request_data is not None:
            payload["requestData"] = request_data
        self._send_json({"op": 6, "d": payload})

        while True:
            response = self._recv_json()
            if response.get("op") == 5:
                self._handle_event(response.get("d", {}))
                continue
            if response.get("op") != 7:
                continue
            data = response.get("d", {})
            if data.get("requestId") != request_id:
                continue
            status = data.get("requestStatus", {})
            if not status.get("result", False):
                comment = status.get("comment", "unknown error")
                raise ObsProtocolError(f"{request_type} failed: {comment}")
            return data.get("responseData", {})

    def current_program_scene(self) -> str:
        data = self.request("GetCurrentProgramScene")
        self._current_program_scene = str(data["currentProgramSceneName"])
        return self._current_program_scene

    def screenshot(self, source_name: str | None = None) -> np.ndarray:
        source_name = source_name or self._current_program_scene
        if not source_name:
            source_name = self.current_program_scene()
        data = self.request(
            "GetSourceScreenshot",
            {
                "sourceName": source_name,
                "imageFormat": "jpg",
                "imageWidth": self.config.width,
                "imageHeight": self.config.height,
                "imageCompressionQuality": self.config.jpeg_quality,
            },
        )
        image_data = str(data["imageData"])
        encoded = image_data.split(",", 1)[-1]
        raw = base64.b64decode(encoded, validate=False)
        frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ObsProtocolError("OBS returned an invalid JPEG frame")
        return frame

    def _handle_event(self, data: dict) -> None:
        if data.get("eventType") != "CurrentProgramSceneChanged":
            return
        scene_name = data.get("eventData", {}).get("sceneName")
        if scene_name:
            self._current_program_scene = str(scene_name)

    def _send_json(self, payload: dict) -> None:
        if self._socket is None:
            raise ObsProtocolError("OBS WebSocket is not connected")
        self._socket.send(json.dumps(payload, separators=(",", ":")))

    def _recv_json(self) -> dict:
        if self._socket is None:
            raise ObsProtocolError("OBS WebSocket is not connected")
        payload = self._socket.recv()
        if not isinstance(payload, str):
            raise ObsProtocolError("OBS returned an unexpected binary message")
        return json.loads(payload)


def _sha256_b64(value: str) -> str:
    return base64.b64encode(hashlib.sha256(value.encode("utf-8")).digest()).decode("ascii")


@dataclass(frozen=True, slots=True)
class FrameSnapshot:
    sequence: int
    first_packet_at: float
    ready_at: float
    frame: np.ndarray
    assembly_ms: float = 0.0
    decode_ms: float = 0.0


class ObsScreenshotSource:
    def __init__(self, config: ObsConfig):
        self.config = config
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: FrameSnapshot | None = None
        self._error: str | None = None
        self._frames = 0
        self._started_at = 0.0

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def fps(self) -> float:
        elapsed = time.perf_counter() - self._started_at
        return self._frames / elapsed if elapsed > 0 else 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name="obs-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
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
        sequence = 0
        while not self._stop.is_set():
            client = ObsClient(self.config)
            try:
                client.connect()
                if not self.config.source_name:
                    client.current_program_scene()
                with self._condition:
                    self._error = None
                while not self._stop.is_set():
                    started = time.perf_counter()
                    frame = client.screenshot(self.config.source_name or None)
                    ready_at = time.perf_counter()
                    sequence += 1
                    snapshot = FrameSnapshot(
                        sequence,
                        started,
                        ready_at,
                        frame,
                        decode_ms=(ready_at - started) * 1000.0,
                    )
                    with self._condition:
                        self._latest = snapshot
                        self._frames += 1
                        self._condition.notify_all()
            except Exception as exc:
                with self._condition:
                    self._error = str(exc)
                    self._condition.notify_all()
                self._stop.wait(0.5)
            finally:
                client.close()
