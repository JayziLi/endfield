"""预览帧从管线到浏览器的那一段。整条路径一次都不解码 JPEG。

管线发过来的本来就是 JPEG (preview.py 的 _encode_datagram, 一帧一个 UDP 包,
上限 60,000 字节), 浏览器要的也是 JPEG, 中间没有任何需要 Python 看一眼像素的
理由。所以这个模块里没有 cv2、没有 numpy、没有 base64, 也不重新编码 —— 帧是
不透明的一段 bytes, 从头到尾只被引用, 不被读取。

旧的 tkinter 界面每帧要 imdecode + cvtColor + PhotoImage, 那不是因为预览需要
像素, 而是因为 tk 的画布只认位图。WebView 这边浏览器自己会解码, 那份工作就整个
从 Python 进程里消失了。
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator


class FrameBus:
    """只保留最新一帧的广播点。

    预览回答的是「现在什么样」, 不是流媒体 —— 攒一队旧帧只会让画面延迟越拖越长,
    而且那个延迟只增不减: 队列一旦涨上去, 除非发布侧停下来, 否则再也回不来。
    订阅者跟不上就跳帧, 跟旧界面 _drain_preview 里「把队列抽干只留最后一帧」是
    同一个道理。

    没有队列还有第二个好处, 而且是更要紧的那个: publish() 的全部工作就是换一个
    引用加一次 notify, 永远不会等任何订阅者。浏览器被最小化、被切走、被网络栈
    拖住, 都传不回上游 —— 而上游是 UDP 接收线程, 它慢一点就直接丢数据报。
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: bytes | None = None
        # 单调递增的帧号。订阅者靠「我看到的号跟现在的号不一样」判断有新帧,
        # 而不是靠比较帧内容 —— 两帧完全一样的画面是正常的 (静止的场景), 不该
        # 被当成没有新帧。
        self._version = 0
        self._closed = False

    @property
    def latest(self) -> bytes | None:
        with self._condition:
            return self._latest

    def publish(self, jpeg: bytes) -> None:
        """换掉最新帧。绝不阻塞, 绝不排队。

        关窗之后还可能有几个在途的数据报打进来, 那时候丢掉就是了 —— 已经没人
        在看了, 留着只会让 close() 之后的 latest 又变。
        """
        with self._condition:
            if self._closed:
                return
            self._latest = jpeg
            self._version += 1
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def subscribe(self) -> Iterator[bytes]:
        """阻塞到有新帧为止, close() 之后正常结束迭代。

        不结束的话, MJPEG 那个响应线程会在关窗之后活下去: ThreadingHTTPServer
        的 daemon_threads 是 True, 于是 server_close() 里的 _threads.join() 是
        空操作, WebServer.stop() 根本叫不动一条卡在这里的线程。关闭顺序因此是
        死的 —— 先 bus.close(), 再 server.stop()。

        seen 从 0 起步而 _version 也从 0 起步, 所以「已经有帧」的情况下新订阅者
        第一轮就不用等: 浏览器一连上就能看到当前画面, 而不是干等到下一帧。
        """
        seen = 0
        while True:
            with self._condition:
                while self._version == seen and not self._closed:
                    # 带超时是保险: notify 漏了一次也只是晚一秒, 不是永久挂死。
                    self._condition.wait(timeout=1.0)
                if self._closed:
                    return
                seen = self._version
                frame = self._latest
            # yield 放在锁外面。订阅者拿去做什么 (往 socket 上写 40KB, 然后被
            # 对端的接收窗口卡住) 都不该挡住 publish —— 这就是上面说的「不反压」
            # 具体落在哪一行。
            if frame is not None:
                yield frame


class PreviewRelay:
    """收管线发来的预览数据报, 原样推给 FrameBus。

    这是旧界面 _receive_preview 抽出来的形, 少了中间那一步: 那边收完要
    imdecode 成位图才能画进 tk 画布, 这边收到的 bytes 直接就是浏览器要的东西。
    于是每帧省掉一次 JPEG 解码和一次色彩空间转换, 而这两样本来是跑在接收线程上的
    —— 接收线程慢一点, 丢的就是数据报本身, 那是真的会在画面上看见的。

    socket 参数照抄 gui.py:_launch, 不是抄形式而是抄理由:

    - SO_RCVBUF 1MB: 一秒 30 帧、每帧上限 60,000 字节, 突发时内核缓冲要接得住。
      缓冲小了, 溢出的数据报内核直接丢, 收不到任何提示。
    - settimeout(0.25): 不是怕收不到帧, 是让 _pump 有机会看见 close()。没有超时
      的话线程会一直卡在 recvfrom 里, 每开一次预览就多一条活线程。
    - bind(("127.0.0.1", 0)): 端口号要写进子进程的命令行, 所以必须先绑上才知道
      号码; 绑 127.0.0.1 而不是 0.0.0.0, 是因为这台机器跟游戏主机在同一个网段,
      绑通配地址等于让局域网上任何人都能往预览里塞画面。
    """

    def __init__(self, bus: FrameBus) -> None:
        self._bus = bus
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def port(self) -> int | None:
        address = self.address
        return None if address is None else address[1]

    @property
    def address(self) -> tuple[str, int] | None:
        sock = self._socket
        return None if sock is None else sock.getsockname()

    def start(self) -> int:
        # 已经在跑就把现有端口还回去。界面每次启动管线都会调它 (启动→停止→再启动),
        # 不拦的话第二次会把第一个 socket 和泵线程孤儿掉 —— 它们还绑着端口、还在
        # 收帧, 但再没人能关掉它们。端口沿用也正合适: 它是进程级的, 子进程的命令行
        # 里写的就是这一个。
        if self._socket is not None:
            return self._socket.getsockname()[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_048_576)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.25)
        self._socket = sock
        # 起个名字: 测试要数「中继起了几条泵线程」, 而 threading.active_count()
        # 是进程全局的 —— 全量跑时别的用例的后台线程随时在生灭, 那个数就成了
        # 随机数。按名字数只看自己这一条。
        self._thread = threading.Thread(target=self._pump, daemon=True, name="endfield-preview-relay")
        self._thread.start()
        return sock.getsockname()[1]

    def close(self) -> None:
        """可以在 start() 之前调用, 也可以调用多次。

        窗口被关掉的时机不归我们管 —— 管线还没起来就关窗是完全正常的一路。
        """
        self._closed = True
        # 先摘掉引用再 close: 别的线程读 port 时拿到的要么是活 socket, 要么是
        # None, 不会是一个已经关掉的 socket (对它 getsockname 会抛)。
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _pump(self) -> None:
        sock = self._socket
        while not self._closed and sock is not None:
            try:
                payload, _address = sock.recvfrom(65_507)
            except socket.timeout:
                # 四分之一秒没有帧。回去看一眼 _closed, 然后接着等。
                continue
            except OSError:
                # close() 把 socket 关了, 或者它坏了。两种情况都没什么可收的了。
                return
            # 空包直接跳过。任何东西都能往一个 UDP 端口发包, 而一个坏包不该让
            # 预览从此黑屏 —— 这里不做别的校验 (比如查 JPEG 的 FFD8 头): 判断
            # 这段字节是不是图片是浏览器的活, 中继层看一眼都算多余。
            if payload:
                self._bus.publish(payload)
