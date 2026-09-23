from __future__ import annotations

import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from rhodes_fast.gui_web.server import WebServer

WEB_ROOT = Path(__file__).resolve().parent.parent / "rhodes_fast" / "gui_web" / "web"


class StaticFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = WebServer(WEB_ROOT)
        self.server.start()
        self.addCleanup(self.server.stop)

    def _get(self, path: str):
        return urllib.request.urlopen(self.server.url + path, timeout=5)

    def test_it_binds_loopback_only(self) -> None:
        """绑 0.0.0.0 就等于把设置界面开放给整个局域网。"""
        self.assertTrue(self.server.url.startswith("http://127.0.0.1:"))

    def test_it_picks_a_free_port_itself(self) -> None:
        """端口写死会在第二个实例上撞车, 而用户完全可能开两个。"""
        self.assertGreater(self.server.port, 0)
        other = WebServer(WEB_ROOT)
        other.start()
        self.addCleanup(other.stop)
        self.assertNotEqual(self.server.port, other.port)

    def test_it_serves_the_design_system(self) -> None:
        response = self._get("/design/styles.css")
        self.assertEqual(response.status, 200)
        self.assertIn("css", response.headers["Content-Type"])
        self.assertIn(b"@import", response.read())

    def test_woff2_gets_the_right_content_type(self) -> None:
        """类型给错的话浏览器会拒绝加载字体, 界面就退回系统默认字体 ——
        肉眼可见但不会报任何错。"""
        name = next(p.name for p in (WEB_ROOT / "fonts").glob("*.woff2"))
        response = self._get(f"/fonts/{name}")
        self.assertEqual(response.headers["Content-Type"], "font/woff2")

    def test_it_refuses_to_walk_out_of_the_web_root(self) -> None:
        """settings.txt 就在 web/ 上面三层, 里面有 KMBox 的 UUID。

        三种写法都要挡住: 明文的 ../, 百分号编码的 (translate_path 先 unquote
        后 normpath, 光看原始路径里有没有 '..' 是拦不住的), 以及 Windows 的
        反斜杠 —— posixpath 不认它, 那一段会当成一个整体的文件名走过去。

        断言的是「web/ 外面的东西一个字节都出不去」, 不是「一定报错」。反斜杠
        那种写法在 translate_path 里被整段丢掉 (os.path.dirname(word) 非空就
        跳过), 路径于是规范化成 web 根目录本身, 走的是目录索引那条路 —— 有了
        index.html 之后它返回 200 + 我们自己的首页。那同样是安全的, 测成
        HTTPError 只是碰巧: index.html 一落地这条断言就翻了。
        """
        index = (WEB_ROOT / "index.html").read_bytes()
        for attempt in (
            "/../../../settings.txt",
            "/%2e%2e/%2e%2e/%2e%2e/settings.txt",
            "/..%5c..%5c..%5csettings.txt",
        ):
            with self.subTest(attempt=attempt):
                try:
                    body = self._get(attempt).read()
                except urllib.error.HTTPError as caught:
                    self.assertIn(caught.code, (400, 403, 404))
                else:
                    # 能返回 200 的只有目录索引落回首页这一条路。
                    # 这里刻意不用 assertEqual: 它失败时会把两边的内容整个打进
                    # 测试输出, 而「失败」的含义正是 body 里装着 KMBox 的 UUID。
                    self.assertTrue(body == index, f"{attempt} 服务了 web/ 外面的东西")

    def test_an_unknown_path_is_a_404_not_a_crash(self) -> None:
        """路径按浏览器的做法百分号编码。裸的中文过不了 http.client 那关 ——
        它把请求行 encode('ascii'), 直接 UnicodeEncodeError, 连 server 都没碰到。"""
        quoted = urllib.parse.quote("/没有这个文件.js")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get(quoted)
        self.assertEqual(caught.exception.code, 404)


class MjpegTest(unittest.TestCase):
    # 帧里塞满全部 256 个字节值。任何一处把帧当文本处理 (转码、转义、base64)
    # 都会在这上面露馅, 而纯 ASCII 的假帧看不出来。
    PAYLOAD = b"\xff\xd8" + bytes(range(256)) + b"\xff\xd9"

    def _open_stream(self, bus) -> object:
        """连上 /preview.mjpg, 并且让帧持续地发。

        关闭顺序是死的: bus.close() 必须在 server.stop() 之前。MJPEG 那条响应
        线程阻塞在 bus.subscribe() 里, 而 ThreadingHTTPServer 的 daemon_threads
        是 True, server_close() 里的 _threads.join() 对 daemon 线程直接 return ——
        stop() 根本叫不动它。addCleanup 是 LIFO, 所以注册顺序要反过来写。
        """
        server = WebServer(WEB_ROOT, bus=bus)
        server.start()
        self.addCleanup(server.stop)
        self.addCleanup(bus.close)

        stop = threading.Event()

        def keep_publishing() -> None:
            # 持续发, 而不是定时器打一发。下面要按字节数读, 而一段 multipart 才
            # 三百来字节 —— 只发一帧的话, 读到一半就没有下文了, 只能挂到 socket
            # 超时。跟「客户端连上之前发的帧」无关: 那一帧订阅者照样拿得到, 因为
            # subscribe() 的 seen 从 0 起步, 一连上就先把当前画面交出去。
            while not stop.wait(0.02):
                bus.publish(self.PAYLOAD)

        publisher = threading.Thread(target=keep_publishing, daemon=True)
        publisher.start()

        def shut_down_publisher() -> None:
            # 置位和 join 必须是同一个 cleanup。分成两个的话 LIFO 会先 join 再
            # 置位, 于是每个用例白等满一个 join 超时。
            stop.set()
            publisher.join(timeout=5)

        self.addCleanup(shut_down_publisher)

        response = urllib.request.urlopen(server.url + "/preview.mjpg", timeout=5)
        self.addCleanup(response.close)
        return response

    def _read_at_least(self, response, size: int) -> bytes:
        """攒够 size 个字节就返回。

        不能用 response.read(size): 底下是 BufferedReader.read(n), 它会一直等到
        正好 n 个字节或者 EOF —— 而一条直播流永远不 EOF。一个 part 才三百来字节,
        读 400 就会挂到 socket 超时为止, 报 TimeoutError 而不是断言失败。read1
        给多少收多少。
        """
        buffer = b""
        while len(buffer) < size:
            chunk = response.read1(4096)
            self.assertTrue(chunk, "流提前结束了")
            buffer += chunk
        return buffer

    def test_the_stream_carries_the_published_bytes(self) -> None:
        """整条路径不解码。浏览器拿到的必须跟管线发出来的一模一样。"""
        from rhodes_fast.gui_web.preview_relay import FrameBus

        response = self._open_stream(FrameBus())
        self.assertIn("multipart/x-mixed-replace", response.headers["Content-Type"])
        self.assertIn("boundary=", response.headers["Content-Type"])

        chunk = self._read_at_least(response, 400)
        self.assertIn(b"Content-Type: image/jpeg", chunk)
        self.assertIn(f"Content-Length: {len(self.PAYLOAD)}".encode(), chunk)
        self.assertIn(self.PAYLOAD, chunk)

    def test_it_keeps_sending_parts_instead_of_stopping_after_one(self) -> None:
        """multipart/x-mixed-replace 是一条不会结束的流。只发一段的话画面会冻在
        第一帧上, 而且看起来跟「管线没发帧」一模一样。"""
        from rhodes_fast.gui_web.preview_relay import FrameBus

        response = self._open_stream(FrameBus())
        boundary = response.headers["Content-Type"].split("boundary=")[1].strip()
        # 三段的量, 再宽一点余量。
        chunk = self._read_at_least(response, 4 * (len(self.PAYLOAD) + 80))
        self.assertGreaterEqual(chunk.count(f"--{boundary}".encode()), 3)

    def test_the_stream_is_404_when_no_bus_is_wired(self) -> None:
        server = WebServer(WEB_ROOT)
        server.start()
        self.addCleanup(server.stop)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(server.url + "/preview.mjpg", timeout=5)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
