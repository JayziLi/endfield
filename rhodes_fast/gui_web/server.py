"""本地 HTTP server: 把 web/ 下的静态文件发给 WebView, 外加 /preview.mjpg 那条流。

只绑 127.0.0.1。绑 0.0.0.0 等于把设置界面开放给整个局域网, 而这台副机跟游戏
主机在同一个网段上。端口传 0 让系统挑, 写死会在开第二个实例时撞车。

serve_forever 跑在自己的线程上: 推理是另一个进程, 界面这边阻塞主线程虽然不会
直接拖慢它, 但窗口一卡用户就没法按停止, 那等于把控制权交出去了。
"""

from __future__ import annotations

import mimetypes
import threading
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .preview_relay import FrameBus

# multipart 的分隔串。固定值就够: 每一段都带 Content-Length, 解析器按长度取字节,
# 不会去帧内容里扫分隔串 —— 所以 JPEG 里碰巧出现这几个字符也不要紧。
_BOUNDARY = "endfieldframe"

# Windows 的 MIME 表来自注册表, 缺 .woff2, 也常有人把 .css 注册成 text/plain。
# 类型给错的话浏览器会拒绝加载字体或样式, 而且一声不吭 —— 界面只是静默退回系统
# 默认字体。这四行把结果钉死, 不看这台机器的注册表脸色。
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        """默认实现往 stderr 打每一条请求。预览是一秒 30 帧, 会把运行状态淹掉,
        还白烧一份 IO。"""

    def list_directory(self, path):
        """不列目录, 一律 404。

        translate_path 会把带分隔符的路径段整段丢掉, 于是 `/..\\..\\settings.txt`
        这种写法虽然拿不到文件, 却会规范化成 web 根目录本身, 然后原样返回一份
        目录清单 —— 相当于把界面用了哪些资产白送出去。这里没有任何需要浏览目录
        的场景: 该发的文件我们自己在 index.html 里点名。
        """
        self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
        return None

    def do_GET(self) -> None:  # noqa: N802 - 基类的命名
        # 查询串要剥掉: 浏览器为了绕开缓存常在 src 后面挂一个 ?t=<时间戳>。
        if self.path.split("?")[0] == "/preview.mjpg":
            self._serve_mjpeg()
            return
        super().do_GET()

    def _serve_mjpeg(self) -> None:
        """把帧总线上的 JPEG 原样灌进 multipart/x-mixed-replace。

        这里一个字节都不改: 收到什么发什么。不解码、不重编码、不转 base64 ——
        管线那头出来就是 JPEG, 浏览器那头要的也是 JPEG。
        """
        bus: FrameBus | None = getattr(self.server, "frame_bus", None)
        if bus is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={_BOUNDARY}")
        # 这条流永远不该被缓存: 缓存住的话画面会冻在第一帧上。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        frames = bus.subscribe()
        try:
            for frame in frames:
                header = (
                    f"--{_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii")
                # 头、帧、收尾的 CRLF 一次写完。wfile 是无缓冲的 _SocketWriter,
                # 分三次写就是三个 send: 多两次系统调用, 还可能被 Nagle 拆成三个
                # 报文, 那 40KB 的一帧就白白多等一个 ACK 往返。拼接多一次 40KB
                # 的内存拷贝, 30fps 也就 1.2MB/s, 比那个往返便宜得多。
                self.wfile.write(header + frame + b"\r\n")
        except OSError:
            # 切走预览屏、关窗、刷新页面, 浏览器都是直接断开连接, 这是正常收场
            # 不是错误。Windows 上断开可能是 ConnectionAborted(10053)、
            # ConnectionReset(10054), 也可能是 stop() 已经把 socket 关了(10038),
            # 所以整个 OSError 都接住 —— 这几条路的结局都一样: 没人在看了, 收工。
            return
        finally:
            # 迭代被异常打断时 for 不会替我们收生成器; 交给 GC 意味着那份 wait
            # 要多挂一会儿。显式关掉, 顺手也让读者看见这条流是有终点的。
            frames.close()


class WebServer:
    """web/ 目录的 loopback HTTP server。

    路径解析一律交给 SimpleHTTPRequestHandler 的 translate_path: 它会规范化
    `..` 再把结果钉在 directory 里面。自己拼路径就是在重写这段防线, 而 web/
    上面三层就是带 KMBox UUID 的 settings.txt。
    """

    def __init__(self, root: Path, *, bus: FrameBus | None = None) -> None:
        self.root = root
        # 没有 bus 时 /preview.mjpg 就是 404。第三份计划之前有些场景 (比如只想看
        # 静态界面) 用不到预览, 让它可选好过让它假装在工作。
        self.bus = bus
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server 还没 start")
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        handler = partial(_Handler, directory=str(self.root))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # 挂在 server 实例上而不是塞进 partial: 处理器每个请求新建一个, 而 bus
        # 是整个 server 共用的一件东西。赋值必须在 serve_forever 起来之前 ——
        # 构造函数已经 bind+listen 了, 之后到达的连接就能看见这个属性。
        self._server.frame_bus = self.bus  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            # poll_interval 决定 shutdown() 要等多久才返回 —— 默认 0.5 秒, 关窗时
            # 那半秒用户是感觉得到的。0.1 秒把它压掉, 代价是空闲时每秒多醒 8 次;
            # 那是个带超时的 select 不是忙等, 对推理进程毫无影响。
            target=lambda: self._server.serve_forever(poll_interval=0.1),
            name="endfield-web",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """shutdown 和 server_close 两个都要, 漏一个就漏一样东西: 前者只让
        serve_forever 返回, 监听 socket 还开着; 后者只关 socket, 那条循环会
        继续空转。join 是为了确认线程真的走完 —— 测试反复起停, 攒下来的僵尸
        线程会表现成后面某条用例莫名其妙地挂。

        join 的只是 serve_forever 那一条。ThreadingHTTPServer 的 daemon_threads
        是 True, 而 _Threads.append 对 daemon 线程直接 return, 所以 server_close
        里那句 _threads.join() 是空操作 —— 已经在跑的请求线程不会挡住 stop(),
        但也不会被它收掉。将来 /preview.mjpg 那条永不结束的响应就靠这一点:
        必须先让它自己结束 (关掉帧总线), 光 stop() 是叫不动它的。"""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
