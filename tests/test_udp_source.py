from __future__ import annotations

import socket
import time
import unittest

import cv2
import numpy as np
import av

from rhodes_fast.config import UdpConfig
from rhodes_fast.udp_source import UdpSource


class UdpSourceTests(unittest.TestCase):
    def test_receives_and_resizes_complete_jpeg_datagram(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        source = UdpSource(UdpConfig(host="127.0.0.1", port=port, width=320, height=320), "udp_jpeg")
        source.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            time.sleep(0.1)
            image = np.full((120, 160, 3), 80, dtype=np.uint8)
            encoded, payload = cv2.imencode(".jpg", image)
            self.assertTrue(encoded)
            sender.sendto(payload.tobytes(), ("127.0.0.1", port))
            snapshot = source.wait_next(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.frame.shape, (320, 320, 3))
            self.assertLessEqual(snapshot.first_packet_at, snapshot.ready_at)
            self.assertGreaterEqual(snapshot.decode_ms, 0)
        finally:
            sender.close()
            source.stop()

    def test_reassembles_fragmented_jpeg_datagrams(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        source = UdpSource(UdpConfig(host="127.0.0.1", port=port, width=160, height=120), "udp_jpeg")
        source.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            time.sleep(0.1)
            image = np.full((120, 160, 3), 120, dtype=np.uint8)
            encoded, payload = cv2.imencode(".jpg", image)
            self.assertTrue(encoded)
            raw = payload.tobytes()
            for offset in range(0, len(raw), 700):
                sender.sendto(raw[offset : offset + 700], ("127.0.0.1", port))
            snapshot = source.wait_next(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.frame.shape, (120, 160, 3))
            self.assertGreaterEqual(snapshot.assembly_ms, 0)
            self.assertGreater(snapshot.decode_ms, 0)
        finally:
            sender.close()
            source.stop()

    def test_decodes_mpegts_video_stream(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        source = UdpSource(
            UdpConfig(host="127.0.0.1", port=port, width=160, height=120, timeout_seconds=1.0),
            "udp_video",
        )
        source.start()
        time.sleep(0.1)
        output = av.open(f"udp://127.0.0.1:{port}", mode="w", format="mpegts")
        stream = output.add_stream("mpeg2video", rate=30)
        stream.width = 160
        stream.height = 120
        stream.pix_fmt = "yuv420p"
        try:
            for value in range(12):
                image = np.full((120, 160, 3), value * 10, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(image, format="bgr24")
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
            snapshot = source.wait_next(0, timeout=3.0)
            self.assertIsNotNone(snapshot, source.error)
            assert snapshot is not None
            self.assertEqual(snapshot.frame.shape, (120, 160, 3))
        finally:
            output.close()
            source.stop()


if __name__ == "__main__":
    unittest.main()
