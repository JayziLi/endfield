# WebView 骨架与实时预览 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做出一个无边框的 WebView 窗口，用 Endfield UI 设计系统渲染，能启动推理管线、看到实时画面、再停下来。

**Architecture:** Python 主进程开一个 pywebview 窗口，指向自己起的本地 HTTP server（只绑 `127.0.0.1`，端口 0 自动分配）。该 server 既发 `web/` 下的静态文件，也发 `/preview.mjpg` 这条 `multipart/x-mixed-replace` 流。推理子进程发来的预览帧本来就是 JPEG，中继层原样转发，**Python 侧一次都不解码**。业务逻辑全部复用阶段 1 做好的 `rhodes_fast.gui_core`。

**Tech Stack:** Python 3.11+、`pywebview`（Windows 下走系统自带的 WebView2 Runtime）、标准库 `http.server` / `socket` / `threading`、Endfield UI 设计系统（vanilla CSS，无 React 无构建）、`unittest`。

## Global Constraints

- **`design/` 目录一个字节不改**，唯一例外是 `design/tokens/fonts.css`（Task 2）。要覆盖样式写在 `app.css` 里，它在 `design/styles.css` 之后加载。
- **不用 React、不用 Babel、不用打包器。** 设计系统的 `HANDOFF.md` 写明 `ef-*` 是 production-quality vanilla CSS，`components/**` 的 React 文件只是「reference implementations — markup + class names」。它自带的 demo 从 unpkg 加载 React+Babel，**那条路离线跑不起来**，不要照抄。
- **离线可用。** 除 `127.0.0.1` 外不许有任何网络请求：没有 CDN、没有 Google Fonts `@import`、没有远程字体。
- **中文用系统字体**（`Microsoft YaHei UI` / `Microsoft YaHei`），拉丁与等宽字体自托管子集。
- **信号黄纪律**：内容区同时只有一个黄块，即启动/停止按钮。NavRail 当前项的黄属于外壳，另算。其余按钮一律 secondary / outline。
- **圆角恒为 0**，「柔和」只来自右下角 45° 切角（`clip-path`）；描边用 `box-shadow: inset 0 0 0 1px`，不用 `border`。
- **不碰推理子进程**：`pipeline.py` / `preview.py` / `kmbox_control.py` / `detector.py` 一行不改。
- **不碰旧 tkinter 界面**：`rhodes_fast/gui.py` 一行不改。本计划全是新增文件，加上 `pyproject.toml` 的两处（依赖 + 入口）。
- **全量测试基线 668 全绿**：`.venv/Scripts/python.exe -m unittest discover -s tests`（汇总在 stderr，重定向要 `2>&1`）
- **提交信息结尾**：`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **不提交 `MODEL/`**；不动工作树里已被用户删除的 `docs/projectile-prediction-research.zh-CN.md`；**不 push**。

> **范围外**：三个业务表单屏（运行设置 / 识别与控制 / 算法库）和切换默认入口留给第三份计划。本计划做完，`endfield-gui-next` 能跑能看画面，但那三个屏是空的，配置仍然靠旧界面或手改 `settings.txt`。

---

## 文件结构

```
rhodes_fast/gui_web/
  __init__.py          导出 main
  app.py               pywebview 窗口 + JS API (Api 类)
  server.py            本地 HTTP：静态文件 + /preview.mjpg
  preview_relay.py     UDP socket → JPEG 帧总线 → MJPEG 订阅者
  web/
    index.html         应用外壳
    app.css            我们自己的布局与 token 覆盖
    app.js             面板切换、窗口控制、启停、日志
    design/            设计系统原样拷贝（fonts.css 除外）
    fonts/             自托管的拉丁/等宽 woff2
tests/
  test_gui_web_design_assets.py   Task 1–2
  test_gui_web_server.py          Task 3–4
  test_gui_web_preview_relay.py   Task 5
  test_gui_web_shell.py           Task 7
```

**为什么 `preview_relay.py` 单独一个文件**：它是唯一需要跟 socket 打交道的地方，而 `server.py` 只该关心 HTTP。两者靠一个 `FrameBus` 对接，测试各测各的，不用为了测中继去起一个 HTTP server。

---

## 背景：动手前必须知道的四件事

**一、预览帧是裸 JPEG，没有任何协议头。**

发送侧 `rhodes_fast/preview.py` 的 `_encode_datagram`：

```python
def _encode_datagram(frame: np.ndarray) -> bytes | None:
    for quality in (72, 55, 40):
        success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if success and encoded.nbytes <= 60_000:
            return encoded.tobytes()
    return None
```

一帧一个 UDP 数据报，上限 60,000 字节（留了 MTU 余量），接收侧 `recvfrom(65_507)`。**所以中继层收到什么就往 MJPEG 里塞什么，不需要 `cv2`，不需要 numpy。** 这是整个方案比现在的 tkinter 路径更轻的原因——旧界面每帧要 `cv2.imdecode` + `cvtColor` + `ImageTk.PhotoImage`。

**二、`preview_enable_file` 是「我在看预览」的开关。**

`gui.py` 在 `.cache/gui-<pid>.preview` 放一个空文件；管线看到它才发帧，删掉就停发。这是为了不看预览时省掉子进程那边的渲染和 JPEG 编码。WebView 这边要接上同样的逻辑：切到预览屏 touch 它，切走 unlink 它。

**三、`gui_core` 的公开接口已经齐了**，直接用：

```python
from rhodes_fast.gui_core import GuiSession, Prompter
```

本计划要用到的三个：

- `GuiSession(config_path: Path, prompter: Prompter)`
- `.build_command(*, config_path, stop_file, runtime_aim_file, preview_port=None, preview_enable_file=None, trail_settings_file=None, latency_log=None, extra=()) -> list[str]`
- `.start(command, *, cwd, on_line, on_exit) -> subprocess.Popen | None` / `.stop()` / `.is_running`

**`start()` 的 `on_line` / `on_exit` 跑在读取线程上，不是主线程。** 回调里不要直接碰窗口，往队列里塞或者用 pywebview 的 `evaluate_js`（它自己做了线程调度）。

**四、设计系统的 `styles.css` 就是 10 行 `@import`：**

```css
@import url("tokens/fonts.css");
@import url("tokens/colors.css");
@import url("tokens/typography.css");
@import url("tokens/spacing.css");
@import url("tokens/effects.css");
@import url("tokens/textures.css");
@import url("tokens/motion.css");
@import url("tokens/base.css");
@import url("components/endfield-ui.css");
@import url("components/endfield-deco.css");
```

顺序要紧（token 在组件之前）。我们只 `<link>` 这一个文件。

**视觉参考**：四个面板的布局稿在 https://claude.ai/artifact/GFEy3JNv6kDxQFQEguWiFX ——那是按设计系统 token 画的，不是设计系统自己的渲染结果，当排版参考用，class 名以本地 `design/components/endfield-ui.css` 为准。

---

## Task 1: 把设计系统拉进仓库

**Files:**
- Create: `rhodes_fast/gui_web/__init__.py`、`rhodes_fast/gui_web/web/design/**`（11 个 CSS + 21 个 SVG）
- Test: `tests/test_gui_web_design_assets.py`

**Interfaces:**
- Consumes: 无
- Produces: `rhodes_fast/gui_web/web/design/styles.css` 及其全部依赖，路径结构跟设计系统项目里一模一样

设计系统在 Claude Design 上，项目 id `b25c7c29-f9d6-4bf4-96dd-221d453ee3b6`，用 `DesignSync` 工具的 `get_file` 逐个读，方法 `get_file`、参数 `projectId` + `path`。

**要拉的文件（32 个，路径原样保留）：**

```
styles.css
tokens/fonts.css  tokens/colors.css  tokens/typography.css  tokens/spacing.css
tokens/effects.css  tokens/textures.css  tokens/motion.css  tokens/base.css
components/endfield-ui.css  components/endfield-deco.css
assets/icons/{box,camera,check,chevron-down,chevron-right,crosshair,file-text,
              folder-open,folder,gauge,layout-grid,maximize,minus,monitor-play,
              more-horizontal,play,refresh-cw,rotate-cw,settings,square,x}.svg
```

**不要拉** `components/**/*.jsx`、`*.d.ts`、`*.prompt.md`、`guidelines/`、`ui_kits/`、`templates/`、`_ds_bundle.js`、`uploads/`——那些是参考资料不是运行时资产。需要看某个组件的 markup 结构时，用 `get_file` 当场读，不要落盘。

**如果 `DesignSync` 报授权错误**：停下来报告，不要自己想办法绕过。授权是账号级的，需要用户在交互式会话里跑 `/design-login`。

- [ ] **Step 1: 写失败测试**

`tests/test_gui_web_design_assets.py`：

```python
from __future__ import annotations

import re
import unittest
from pathlib import Path

DESIGN = Path(__file__).resolve().parent.parent / "rhodes_fast" / "gui_web" / "web" / "design"


class DesignSystemAssetsTest(unittest.TestCase):
    def test_styles_css_exists(self) -> None:
        self.assertTrue((DESIGN / "styles.css").is_file(), f"没拉到 {DESIGN / 'styles.css'}")

    def test_every_import_in_styles_css_resolves(self) -> None:
        """styles.css 就是一串 @import。少一个文件, 界面上少一整层样式,
        而浏览器对缺失的 @import 是静默失败的 —— 不会报错, 只会难看。"""
        text = (DESIGN / "styles.css").read_text(encoding="utf-8")
        imports = re.findall(r'@import\s+url\("([^"]+)"\)', text)
        self.assertEqual(len(imports), 10, f"期望 10 条 @import, 实际 {len(imports)}")
        for relative in imports:
            self.assertTrue((DESIGN / relative).is_file(), f"{relative} 没拉到")

    def test_the_two_component_sheets_come_after_the_tokens(self) -> None:
        """token 必须先于组件 —— 组件样式引用的就是那些自定义属性。"""
        text = (DESIGN / "styles.css").read_text(encoding="utf-8")
        imports = re.findall(r'@import\s+url\("([^"]+)"\)', text)
        first_component = next(i for i, p in enumerate(imports) if p.startswith("components/"))
        last_token = max(i for i, p in enumerate(imports) if p.startswith("tokens/"))
        self.assertLess(last_token, first_component)

    def test_no_stylesheet_reaches_outside_the_design_folder(self) -> None:
        """离线是硬要求。fonts.css 是唯一允许碰网络的文件, Task 2 会把它换掉,
        在那之前它是已知的例外。"""
        offenders: list[str] = []
        for sheet in DESIGN.rglob("*.css"):
            if sheet.name == "fonts.css":
                continue
            for url in re.findall(r'url\(\s*["\']?([^"\')]+)', sheet.read_text(encoding="utf-8")):
                if url.startswith(("http://", "https://", "//")):
                    offenders.append(f"{sheet.relative_to(DESIGN)} → {url}")
        self.assertEqual(offenders, [], "样式表里有远程引用")

    def test_the_icons_used_by_the_nav_rail_are_present(self) -> None:
        for slug in ("box", "crosshair", "layout-grid", "monitor-play", "settings", "file-text"):
            self.assertTrue((DESIGN / "assets" / "icons" / f"{slug}.svg").is_file(), slug)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_design_assets -v
```

Expected: 五条全红，第一条报 `没拉到 ...\design\styles.css`

- [ ] **Step 3: 建目录并拉文件**

`rhodes_fast/gui_web/__init__.py` 先写成：

```python
"""WebView 界面。业务逻辑全部来自 rhodes_fast.gui_core。"""
```

然后用 `DesignSync` 的 `get_file` 逐个读上面列的 32 个路径，用 Write 工具原样落到 `rhodes_fast/gui_web/web/design/<同样的路径>`。**一个字节都不要改**（`tokens/fonts.css` 也先原样落，Task 2 才换）。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_design_assets -v
```

Expected: `Ran 5 tests` / `OK`

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/gui_web/ tests/test_gui_web_design_assets.py
git commit -F - <<'MSG'
feat: vendor the Endfield UI design system

Copied verbatim from the Claude Design project so the desktop app can run
offline. Only the stylesheets and the icon set come across; the React
files, guidelines and ui_kits are reference material and would just rot
here.

The test walks styles.css's ten @imports and fails if any of them is
missing, because a browser skips an @import it cannot resolve without
saying anything -- the page just quietly loses a layer of styling.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 2: 字体自托管

**Files:**
- Modify: `rhodes_fast/gui_web/web/design/tokens/fonts.css`（**唯一允许改的 design 文件**）
- Create: `rhodes_fast/gui_web/web/fonts/*.woff2`
- Modify: `tests/test_gui_web_design_assets.py`

**Interfaces:**
- Consumes: Task 1 的 `design/`
- Produces: `design/tokens/fonts.css` 只含本地 `@font-face`；`web/fonts/` 下的 woff2

设计系统原版的 `tokens/fonts.css` 整个文件就是一行 Google Fonts `@import`。它是全套 CSS 里唯一碰网络的地方，离线时未必快速失败——遇到需要认证的网络会把整张样式表的加载吊住。

**要自托管的四个字族（只要拉丁字符）：**

| 字族 | 用途 | CSS 变量 |
|---|---|---|
| Archivo（400/700/900） | display，大数字、区段序号 | `--font-display` |
| Barlow（400/500/600/700） | UI，标签与正文 | `--font-ui` |
| Barlow Condensed（400/600） | 密排 chrome | `--font-condensed` |
| IBM Plex Mono（400/500） | 遥测、路径、时间戳 | `--font-mono` |

**中文不自托管**，`--font-cjk` 覆盖成系统的微软雅黑（Task 7 在 `app.css` 里做）。理由：日志、模型路径、报错里的中文是动态的，子集化一旦漏字就是豆腐块；全量 Noto Sans SC 一个字重约 5MB，两个字重就是 10MB 进仓库。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_web_design_assets.py`）**

```python
FONTS = DESIGN.parent / "fonts"


class SelfHostedFontsTest(unittest.TestCase):
    def test_fonts_css_no_longer_reaches_the_network(self) -> None:
        """这是全套 CSS 里唯一碰过网络的文件。离线时它未必快速失败 ——
        遇到需要认证的网络会把整张样式表的加载吊住。"""
        text = (DESIGN / "tokens" / "fonts.css").read_text(encoding="utf-8")
        self.assertNotIn("@import", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("http://", text)

    def test_every_font_file_it_names_exists(self) -> None:
        text = (DESIGN / "tokens" / "fonts.css").read_text(encoding="utf-8")
        urls = re.findall(r'url\(\s*["\']?([^"\')]+)', text)
        self.assertTrue(urls, "fonts.css 里一个 @font-face 都没有")
        for url in urls:
            self.assertTrue((DESIGN / "tokens" / url).resolve().is_file(), f"{url} 不存在")

    def test_all_four_families_are_declared(self) -> None:
        text = (DESIGN / "tokens" / "fonts.css").read_text(encoding="utf-8")
        for family in ("Archivo", "Barlow", "Barlow Condensed", "IBM Plex Mono"):
            self.assertIn(f'font-family: "{family}"', text, f"缺 {family}")

    def test_no_cjk_font_binary_was_vendored(self) -> None:
        """中文走系统字体。Noto Sans SC 全量一个字重约 5MB, 不该进仓库;
        子集化又会在日志和报错的动态中文上出豆腐块。"""
        heavy = [f.name for f in FONTS.glob("*.woff2") if f.stat().st_size > 1_000_000]
        self.assertEqual(heavy, [], "有超过 1MB 的字体文件, 八成是误拉了中文字族")
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_design_assets -v
```

Expected: `SelfHostedFontsTest` 四条红，第一条报 `'@import' unexpectedly found`

- [ ] **Step 3: 下载 woff2**

Google Fonts 的 css2 接口按 User-Agent 决定返回格式；给一个现代浏览器的 UA 才拿到 woff2。先取 CSS，从里面挑出 `latin` 那段的 url，再下载：

```bash
mkdir -p "rhodes_fast/gui_web/web/fonts"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
curl -sL -A "$UA" \
  "https://fonts.googleapis.com/css2?family=Archivo:wght@400;700;900&family=Barlow:wght@400;500;600;700&family=Barlow+Condensed:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap" \
  -o /tmp/gf.css
grep -c "font-face" /tmp/gf.css
```

`/tmp/gf.css` 里每个 `@font-face` 前面有一行 `/* latin */` 之类的注释标明 unicode-range 分段。**只要 `latin` 和 `latin-ext` 两段**，别的语言段（cyrillic、greek、vietnamese）一律跳过——我们的界面里不会出现那些字符。

逐个 `curl -sL -o rhodes_fast/gui_web/web/fonts/<family>-<weight>.woff2 <url>`，命名用 `archivo-400.woff2` 这种小写连字符形式。

**如果没有网络**：停下来报告。不要用别的字体凑数——工业风的味道主要就在这几个字族上。

- [ ] **Step 4: 重写 `design/tokens/fonts.css`**

```css
/* ENDFIELD UI — webfonts, 自托管版。
   原版是一行 Google Fonts @import, 是全套 CSS 里唯一碰网络的地方; 桌面应用要
   离线跑, 而且遇到需要认证的网络时那条 @import 会把整张样式表吊住。
   只收拉丁字符: 中文由 app.css 覆盖成系统的微软雅黑 (动态中文子集化会出豆腐块,
   全量中文字族又是每字重 5MB)。 */

@font-face {
  font-family: "Archivo";
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url("../../fonts/archivo-400.woff2") format("woff2");
}
/* …其余按实际下到的文件逐个写。每个字族的每个字重一个块。 */
```

写全部四个字族的全部字重（Archivo 3 个、Barlow 4 个、Barlow Condensed 2 个、IBM Plex Mono 2 个，共 11 个 `@font-face`）。路径是 `../../fonts/`，因为 fonts.css 在 `design/tokens/` 下。

- [ ] **Step 5: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_design_assets -v
```

Expected: `Ran 9 tests` / `OK`

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/gui_web/web/ tests/test_gui_web_design_assets.py
git commit -F - <<'MSG'
feat: self-host the latin faces, leave CJK to the system

tokens/fonts.css was the one file in the design system that reached the
network, and on a captive network its @import can hang the whole stylesheet
rather than failing fast. It is now eleven local @font-face rules.

Chinese stays on Microsoft YaHei. The log lines, model paths and error
messages carry arbitrary characters, so a subset would show tofu, and a
full CJK family is about 5MB per weight.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 3: 本地 HTTP server（静态文件）

**Files:**
- Create: `rhodes_fast/gui_web/server.py`
- Test: `tests/test_gui_web_server.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `WebServer(root: Path)`：`.start() -> None`、`.stop() -> None`、`.port -> int`、`.url -> str`
  - 只绑 `127.0.0.1`，端口传 0 让系统分配
  - （`bus` 关键字参数由 Task 4 追加，本任务先不要加）

- [ ] **Step 1: 写失败测试**

`tests/test_gui_web_server.py`：

```python
from __future__ import annotations

import unittest
import urllib.error
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
        """settings.txt 就在上面两层, 里面有 KMBox 的 UUID。"""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/../../../settings.txt")
        self.assertIn(caught.exception.code, (400, 403, 404))

    def test_an_unknown_path_is_a_404_not_a_crash(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/没有这个文件.js")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_server -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_web.server'`

- [ ] **Step 3: 实现 `server.py` 的静态文件部分**

```python
"""本地 HTTP server: 发静态文件, 以及 /preview.mjpg 那条流。

只绑 127.0.0.1。绑 0.0.0.0 等于把设置界面开放给整个局域网, 而这台副机跟
游戏主机在同一个网段上。端口传 0 让系统挑, 写死会在开第二个实例时撞车。
"""

from __future__ import annotations

import mimetypes
import threading
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        """默认实现往 stderr 打每一条请求。预览是一秒 30 帧, 会把运行状态淹掉。"""

    def do_GET(self) -> None:  # noqa: N802 - 基类的命名
        super().do_GET()


class WebServer:
    def __init__(self, root: Path) -> None:
        self.root = root
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
        handler = partial(_Handler, directory=str(self.root))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
```

> `SimpleHTTPRequestHandler` 自带 `translate_path`，它会把 `..` 规范化掉、把结果钉在 `directory` 里面——目录穿越那条测试靠的就是它。**不要自己拼路径**去绕开它。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_server -v
```

Expected: `Ran 6 tests` / `OK`

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/gui_web/server.py tests/test_gui_web_server.py
git commit -F - <<'MSG'
feat: serve the web assets from a loopback http server

Binds 127.0.0.1 on a port the OS picks. Binding 0.0.0.0 would put the
settings UI on the LAN, and this machine shares a segment with the gaming
box; a fixed port would collide the moment someone opens a second window.

Request logging is off because the preview runs at 30fps and would bury
everything else in stderr.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 4: 帧总线与 `/preview.mjpg`

**Files:**
- Create: `rhodes_fast/gui_web/preview_relay.py`（本任务只写 `FrameBus`）
- Modify: `rhodes_fast/gui_web/server.py`
- Modify: `tests/test_gui_web_server.py`
- Test: `tests/test_gui_web_preview_relay.py`

**Interfaces:**
- Consumes: `WebServer`（Task 3）
- Produces:
  - `FrameBus()`：`.publish(jpeg: bytes) -> None`、`.subscribe() -> Iterator[bytes]`、`.close() -> None`
  - `WebServer(root, *, bus: FrameBus | None = None)`；有 bus 时 `/preview.mjpg` 发 `multipart/x-mixed-replace`

> **关闭顺序不能反：先 `bus.close()`，再 `server.stop()`。** Task 3 实测确认：`ThreadingHTTPServer.daemon_threads` 是 `True`，而 `_Threads.append` 对 daemon 线程直接 `return`，所以 `server_close()` 里那句 `_threads.join()` 是空操作——**`stop()` 叫不动那条阻塞在 `bus.subscribe()` 里的 MJPEG 线程**，它会活到进程结束。计划里 `main()` 的 finally 和测试的 `addCleanup`（LIFO）顺序碰巧都是对的，但写的人得知道为什么。

**只保留最新一帧。** 预览是「现在什么样」，不是流媒体——积压一队旧帧只会让画面延迟越拖越长。订阅者慢了就跳帧，这跟旧界面 `_drain_preview` 里那个「把队列抽干只留最后一帧」的做法是同一个道理。

- [ ] **Step 1: 写失败测试**

`tests/test_gui_web_preview_relay.py`：

```python
from __future__ import annotations

import threading
import unittest

from rhodes_fast.gui_web.preview_relay import FrameBus


class FrameBusTest(unittest.TestCase):
    def test_a_subscriber_gets_the_frame_bytes_unchanged(self) -> None:
        """整条路径不解码。收到什么字节就发什么字节, 这是它比 tkinter 那条
        路径轻的全部理由。"""
        bus = FrameBus()
        self.addCleanup(bus.close)
        got: list[bytes] = []
        ready = threading.Event()

        def reader() -> None:
            ready.set()
            for frame in bus.subscribe():
                got.append(frame)
                return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        ready.wait(timeout=5)
        bus.publish(b"\xff\xd8not-really-a-jpeg\xff\xd9")
        thread.join(timeout=5)
        self.assertEqual(got, [b"\xff\xd8not-really-a-jpeg\xff\xd9"])

    def test_only_the_newest_frame_survives_a_slow_subscriber(self) -> None:
        """预览要的是「现在什么样」。攒一队旧帧只会让延迟越拖越长。"""
        bus = FrameBus()
        self.addCleanup(bus.close)
        for i in range(10):
            bus.publish(f"frame-{i}".encode())
        self.assertEqual(bus.latest, b"frame-9")

    def test_close_ends_the_iteration(self) -> None:
        """不结束的话 MJPEG 那个响应线程会在关窗后活下去。"""
        bus = FrameBus()
        done = threading.Event()

        def reader() -> None:
            for _frame in bus.subscribe():
                pass
            done.set()

        threading.Thread(target=reader, daemon=True).start()
        bus.close()
        self.assertTrue(done.wait(timeout=5), "close() 之后订阅迭代没有结束")


if __name__ == "__main__":
    unittest.main()
```

追加到 `tests/test_gui_web_server.py`：

```python
class MjpegTest(unittest.TestCase):
    def test_the_stream_carries_the_published_bytes(self) -> None:
        from rhodes_fast.gui_web.preview_relay import FrameBus

        bus = FrameBus()
        server = WebServer(WEB_ROOT, bus=bus)
        server.start()
        self.addCleanup(server.stop)
        self.addCleanup(bus.close)

        response = urllib.request.urlopen(server.url + "/preview.mjpg", timeout=5)
        self.assertIn("multipart/x-mixed-replace", response.headers["Content-Type"])
        self.assertIn("boundary=", response.headers["Content-Type"])

        payload = b"\xff\xd8" + b"x" * 64 + b"\xff\xd9"
        threading.Timer(0.1, lambda: bus.publish(payload)).start()
        chunk = response.read(400)
        self.assertIn(b"Content-Type: image/jpeg", chunk)
        self.assertIn(payload, chunk)

    def test_the_stream_is_404_when_no_bus_is_wired(self) -> None:
        server = WebServer(WEB_ROOT)
        server.start()
        self.addCleanup(server.stop)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(server.url + "/preview.mjpg", timeout=5)
        self.assertEqual(caught.exception.code, 404)
```

`tests/test_gui_web_server.py` 顶部补 `import threading`。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_preview_relay tests.test_gui_web_server -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_web.preview_relay'`

- [ ] **Step 3: 实现 `FrameBus`**

`rhodes_fast/gui_web/preview_relay.py`：

```python
"""预览帧从 UDP 到 MJPEG 的那一段。整条路径不解码 JPEG。

管线发过来的本来就是 JPEG (preview.py 的 _encode_datagram, 一帧一个 UDP 包,
上限 60,000 字节), 浏览器要的也是 JPEG, 中间没有任何需要 Python 看一眼像素的
理由。旧的 tkinter 界面要 imdecode + cvtColor + PhotoImage, 那是因为 tk 的
画布只认位图。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator


class FrameBus:
    """只保留最新一帧的广播点。

    预览回答的是「现在什么样」, 不是流媒体 —— 攒一队旧帧只会让画面延迟越拖
    越长。订阅者跟不上就跳帧, 跟旧界面 _drain_preview 里「把队列抽干只留最后
    一帧」是同一个道理。
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: bytes | None = None
        self._version = 0
        self._closed = False

    @property
    def latest(self) -> bytes | None:
        with self._condition:
            return self._latest

    def publish(self, jpeg: bytes) -> None:
        with self._condition:
            self._latest = jpeg
            self._version += 1
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def subscribe(self) -> Iterator[bytes]:
        """阻塞到有新帧为止。close() 之后正常结束迭代 —— 不结束的话, MJPEG
        那个响应线程会在关窗之后活下去。"""
        seen = 0
        while True:
            with self._condition:
                while self._version == seen and not self._closed:
                    self._condition.wait(timeout=1.0)
                if self._closed:
                    return
                seen = self._version
                frame = self._latest
            if frame is not None:
                yield frame
```

- [ ] **Step 4: 给 `server.py` 加 MJPEG 路由**

`WebServer.__init__` 加 `bus: FrameBus | None = None` 并存下来；`_Handler` 要拿到它，用 `partial` 多传一个关键字参数，在 `do_GET` 里判断路径：

```python
    def do_GET(self) -> None:  # noqa: N802 - 基类的命名
        if self.path.split("?")[0] == "/preview.mjpg":
            self._serve_mjpeg()
            return
        super().do_GET()

    def _serve_mjpeg(self) -> None:
        bus = self.server.frame_bus  # type: ignore[attr-defined]
        if bus is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        boundary = "endfieldframe"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        # 这条流永远不该被缓存: 缓存住的话画面会冻在第一帧上。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            for frame in bus.subscribe():
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # 切走预览屏或者关窗时浏览器直接断开, 这是正常收场不是错误。
            return
```

`ThreadingHTTPServer` 的实例上挂 `frame_bus` 属性（在 `WebServer.start()` 里赋值）。`ThreadingHTTPServer` 每个连接一个线程，所以这条长连接不会挡住静态文件请求。

- [ ] **Step 5: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_preview_relay tests.test_gui_web_server -v
```

Expected: `Ran 11 tests` / `OK`

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/gui_web/preview_relay.py rhodes_fast/gui_web/server.py tests/test_gui_web_preview_relay.py tests/test_gui_web_server.py
git commit -F - <<'MSG'
feat: relay preview frames as an MJPEG stream

The pipeline already hands us JPEG, one frame per UDP datagram, and the
browser wants JPEG -- nothing in between needs to look at a pixel. The old
tkinter path decodes only because a tk canvas takes bitmaps.

The bus keeps just the newest frame. Preview answers "what does it look
like now", so a backlog would only stretch the delay; a subscriber that
falls behind skips frames instead, which is what _drain_preview does today
by draining its queue down to the last entry.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 5: 预览中继（UDP → 帧总线）

**Files:**
- Modify: `rhodes_fast/gui_web/preview_relay.py`
- Modify: `tests/test_gui_web_preview_relay.py`

**Interfaces:**
- Consumes: `FrameBus`（Task 4）
- Produces: `PreviewRelay(bus: FrameBus)`：`.start() -> int`（返回绑到的端口）、`.close() -> None`、`.port -> int | None`

这是从旧界面的 `_receive_preview` 抽出来的形，但**不解码**。socket 参数照抄 `gui.py:_launch` 里那几行：`SO_RCVBUF` 设 1MB、`settimeout(0.25)`、`bind(("127.0.0.1", 0))`。

- [ ] **Step 1: 写失败测试（追加）**

```python
class PreviewRelayTest(unittest.TestCase):
    def test_a_datagram_lands_on_the_bus_byte_for_byte(self) -> None:
        import socket
        import time

        from rhodes_fast.gui_web.preview_relay import PreviewRelay

        bus = FrameBus()
        relay = PreviewRelay(bus)
        port = relay.start()
        self.addCleanup(bus.close)
        self.addCleanup(relay.close)

        payload = b"\xff\xd8" + bytes(range(256)) * 4 + b"\xff\xd9"
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(payload, ("127.0.0.1", port))

        deadline = time.monotonic() + 5
        while bus.latest is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(bus.latest, payload)

    def test_it_binds_loopback_and_reports_the_port(self) -> None:
        from rhodes_fast.gui_web.preview_relay import PreviewRelay

        bus = FrameBus()
        relay = PreviewRelay(bus)
        self.addCleanup(bus.close)
        self.addCleanup(relay.close)
        port = relay.start()
        self.assertGreater(port, 0)
        self.assertEqual(relay.port, port)

    def test_close_is_safe_before_start(self) -> None:
        from rhodes_fast.gui_web.preview_relay import PreviewRelay

        PreviewRelay(FrameBus()).close()

    def test_an_oversized_datagram_does_not_kill_the_loop(self) -> None:
        """管线把帧压到 60,000 字节以下, 但别的东西也可能往这个端口发包。
        一个坏包不该让预览从此黑屏。"""
        import socket
        import time

        from rhodes_fast.gui_web.preview_relay import PreviewRelay

        bus = FrameBus()
        relay = PreviewRelay(bus)
        port = relay.start()
        self.addCleanup(bus.close)
        self.addCleanup(relay.close)

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(b"", ("127.0.0.1", port))          # 空包
        sender.sendto(b"\xff\xd8ok\xff\xd9", ("127.0.0.1", port))

        deadline = time.monotonic() + 5
        while bus.latest != b"\xff\xd8ok\xff\xd9" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(bus.latest, b"\xff\xd8ok\xff\xd9")
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_preview_relay -v
```

Expected: `ImportError: cannot import name 'PreviewRelay'`

- [ ] **Step 3: 实现**

```python
class PreviewRelay:
    """收管线发来的预览数据报, 原样推给 FrameBus。

    socket 参数照抄 gui.py:_launch: 1MB 接收缓冲 (30fps × 60KB 的突发不能丢),
    0.25 秒超时 (让读取线程有机会看见 close), 绑 127.0.0.1 的随机端口 —— 端口号
    要写进子进程的命令行, 所以必须先绑上才知道。
    """

    def __init__(self, bus: FrameBus) -> None:
        self._bus = bus
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def port(self) -> int | None:
        return None if self._socket is None else self._socket.getsockname()[1]

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_048_576)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.25)
        self._socket = sock
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return sock.getsockname()[1]

    def close(self) -> None:
        self._closed = True
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _pump(self) -> None:
        sock = self._socket
        while not self._closed and sock is not None:
            try:
                payload, _address = sock.recvfrom(65_507)
            except socket.timeout:
                continue
            except OSError:
                return
            # 空包和垃圾包直接跳过: 一个坏包不该让预览从此黑屏。
            if payload:
                self._bus.publish(payload)
```

文件顶部补 `import socket`。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_preview_relay -v
```

Expected: `Ran 7 tests` / `OK`

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/gui_web/preview_relay.py tests/test_gui_web_preview_relay.py
git commit -F - <<'MSG'
feat: receive preview datagrams and publish them unchanged

Same socket settings the tkinter GUI uses -- 1MB receive buffer for the
30fps bursts, a quarter-second timeout so the reader notices close(), and a
loopback bind on port 0 because the port has to go into the child's command
line, which means binding first.

An empty or malformed datagram is skipped rather than raised. Anything can
send to a UDP port, and one bad packet should not black out the preview for
the rest of the session.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 6: pywebview 无边框窗口

**Files:**
- Create: `rhodes_fast/gui_web/app.py`
- Modify: `rhodes_fast/gui_web/__init__.py`、`pyproject.toml`
- Test: `tests/test_gui_web_app.py`

**Interfaces:**
- Consumes: `WebServer`（Task 3）、`FrameBus` / `PreviewRelay`（Task 4-5）
- Produces:
  - `Api`：暴露给 JS 的对象，本任务只有 `minimize()` / `toggle_maximize()` / `close()`
  - `main() -> None`：入口点 `endfield-gui-next`

**依赖**：`pyproject.toml` 的 `[project.optional-dependencies]` 加一个 `webview = ["pywebview>=5.0,<6"]`，`[project.scripts]` 加 `endfield-gui-next = "rhodes_fast.gui_web.app:main"`。**先别加进主 `dependencies`**——第三份计划切默认入口时再说，在那之前旧界面不该被迫装 pywebview。

先装上：`.venv/Scripts/python.exe -m pip install "pywebview>=5.0,<6"`

**无边框窗口要自己实现的东西**（`webview.create_window(..., frameless=True)` 之后系统什么都不给）：

| 行为 | 怎么做 |
|---|---|
| 拖动 | TitleBar 元素加 `style="-webkit-app-region: drag"`；窗口按钮那块要 `no-drag`，否则点不到 |
| 最小化 / 最大化 / 关闭 | JS 调 `pywebview.api.minimize()` 等，Python 侧用 `window.minimize()` / `window.toggle_fullscreen()` / `window.destroy()` |
| 双击标题栏最大化 | JS 的 `dblclick` 监听调 `toggle_maximize()` |

**不做**：Windows 贴边分屏（Aero Snap）。无边框窗口要拿到它得走 Win32 的 `WM_NCHITTEST`，超出本计划范围。第三份计划或之后按需要再说；先在 README 里记一条已知限制。

- [ ] **Step 1: 写失败测试**

`tests/test_gui_web_app.py`：

```python
from __future__ import annotations

import unittest
from unittest.mock import Mock


class ApiTest(unittest.TestCase):
    def test_window_controls_reach_the_window(self) -> None:
        """无边框窗口没有系统按钮, 这三个是唯一的出路 —— 接错了窗口就关不掉。"""
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api.attach(window)

        api.minimize()
        window.minimize.assert_called_once_with()

        api.close()
        window.destroy.assert_called_once_with()

    def test_toggle_maximize_flips_back_and_forth(self) -> None:
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api.attach(window)
        api.toggle_maximize()
        api.toggle_maximize()
        self.assertEqual(window.toggle_fullscreen.call_count, 2)

    def test_calls_before_attach_do_not_raise(self) -> None:
        """JS 那边可能在窗口就绪之前就点了按钮。炸在这里等于白屏。"""
        from rhodes_fast.gui_web.app import Api

        Api().minimize()


class ImportSurfaceTest(unittest.TestCase):
    def test_importing_app_does_not_need_a_display(self) -> None:
        """测试机上没人开窗口。import 时就 create_window 的话全量测试会挂住。"""
        import rhodes_fast.gui_web.app as module

        self.assertTrue(callable(module.main))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_app -v
```

Expected: `ModuleNotFoundError: No module named 'rhodes_fast.gui_web.app'`

- [ ] **Step 3: 实现**

```python
"""WebView 界面的入口。

窗口是无边框的 —— 设计系统的 TitleBar / StatusBar 组件就是为这个做的,
它的 HANDOFF 写着「the app is frameless Windows software, so it must own its
own caption bar and footer」。代价是拖动、最大化、关闭全要自己接。
"""

from __future__ import annotations

from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent / "web"


class Api:
    """暴露给 JS 的对象。pywebview 把它的公开方法挂到 window.pywebview.api 上。

    attach() 分两步是因为 create_window 需要 js_api, 而 api 又需要 window ——
    先把对象建出来, 窗口造好之后再回填。
    """

    def __init__(self) -> None:
        self._window = None
        self._session = None
        self._relay = None
        self._config_path = None
        self._preview_enable_file = None

    def attach(self, window) -> None:
        self._window = window

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._window is not None:
            self._window.toggle_fullscreen()

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()


def main() -> None:
    import webview

    from .preview_relay import FrameBus, PreviewRelay
    from .server import WebServer

    bus = FrameBus()
    relay = PreviewRelay(bus)
    server = WebServer(WEB_ROOT, bus=bus)
    server.start()

    api = Api()
    window = webview.create_window(
        "Endfield",
        server.url,
        js_api=api,
        frameless=True,
        easy_drag=False,   # 靠 CSS 的 -webkit-app-region 指定可拖区域, 不要整窗可拖
        width=1280,
        height=800,
        min_size=(1024, 680),
    )
    api.attach(window)
    try:
        webview.start()
    finally:
        relay.close()
        bus.close()
        server.stop()
```

`rhodes_fast/gui_web/__init__.py` 改成：

```python
"""WebView 界面。业务逻辑全部来自 rhodes_fast.gui_core。"""

from .app import main

__all__ = ["main"]
```

> `import webview` 放在 `main()` 里面，不放模块顶部：测试机上没人开窗口，而 `pywebview` 是可选依赖，顶部 import 会让没装它的环境连测试都跑不起来。

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_app -v
```

Expected: `Ran 4 tests` / `OK`

- [ ] **Step 5: 手动确认窗口能开**

```bash
.venv/Scripts/python.exe -c "from rhodes_fast.gui_web.app import main; main()"
```

应该弹出一个 1280×800、**没有系统标题栏**的窗口，里面是 404（还没有 `index.html`，Task 7 才有）。确认窗口能出现、能用任务管理器关掉，然后关掉它。

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/gui_web/app.py rhodes_fast/gui_web/__init__.py pyproject.toml tests/test_gui_web_app.py
git commit -F - <<'MSG'
feat: open a frameless pywebview window on the local server

The design system's TitleBar and StatusBar exist because the app owns its
own chrome, so the window is frameless and drag, maximize and close are
ours to wire. easy_drag is off: CSS marks which regions drag, otherwise the
whole window moves when you try to use a control.

pywebview is an optional extra and imported inside main(), so a checkout
without it still runs the tests. Aero Snap is not implemented -- that needs
WM_NCHITTEST, and it is noted as a limitation rather than half-done.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 7: 应用外壳

**Files:**
- Create: `rhodes_fast/gui_web/web/index.html`、`app.css`、`app.js`
- Test: `tests/test_gui_web_shell.py`

**Interfaces:**
- Consumes: Task 1-2 的 `design/`、Task 6 的 `Api`
- Produces: 一个能跑的外壳——TitleBar / NavRail / SectionHeader / RuntimeLog / 动作行 / StatusBar，四个空屏用 NavRail 切换

**布局照 `docs/superpowers/specs/2026-09-17-webview-ui-design.md` 第 6 节的 ASCII 图**，class 名以本地 `design/components/endfield-ui.css` 为准——**动手前 grep 一遍那个文件**看有哪些 `ef-*` 可用，别自己发明。视觉参考：https://claude.ai/artifact/GFEy3JNv6kDxQFQEguWiFX

NavRail 四项：

| 索引 | 标题 | 拉丁 | 图标 |
|---|---|---|---|
| 01 | 运行设置 | RUNTIME SETTINGS | `box` |
| 02 | 识别与控制 | DETECTION & CONTROL | `crosshair` |
| 03 | 算法库 | ALGORITHM LIBRARY | `layout-grid` |
| 04 | 实时预览 | LIVE PREVIEW | `monitor-play` |

本任务四个屏都是空的，各放一行「本页在第三份计划里实现」的占位文字——**不要放 lorem ipsum，也不要假装有控件**。

`app.css` 里必须有的覆盖：

```css
/* 中文走系统字体。design/ 里这几个变量的兜底是 Noto Sans SC, 我们没有自托管它。 */
:root {
  --font-display: "Archivo", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
  --font-ui: "Barlow", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
  --font-condensed: "Barlow Condensed", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", "Microsoft YaHei UI", "Microsoft YaHei", ui-monospace, monospace;
  --font-cjk: "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
}

/* 无边框窗口的拖动区 —— 注意这里不用 -webkit-app-region。

   Task 6 实测过: WebView2 里 CSS.supports('-webkit-app-region','drag') 返回
   true、computed 值也是 "drag", 但按住拖窗口纹丝不动 —— 那个属性的拖动行为是
   Electron / PWA 那一层实现的, WebView2 只是认得属性名。光查支持度会被骗过去。

   pywebview 自己的机制在 webview/js/customize.js: 给 .pywebview-drag-region
   的元素挂 mousedown, 回调里调 pywebviewMoveWindow。所以可拖元素用那个 class,
   不需要反向的 no-drag —— 只给标题栏挂, 按钮本来就不在可拖区里。

   两个坑: easy_drag 必须是 False (开着的话 pywebview 给整个 window 挂
   mousedown, 按哪都能拖); 监听器是页面加载时一次性挂的, 加载后动态插进 DOM
   的元素不会自动可拖。 */
.pywebview-drag-region { cursor: default; }
```

- [ ] **Step 1: 写失败测试**

`tests/test_gui_web_shell.py`：

```python
from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "rhodes_fast" / "gui_web" / "web"


class ShellTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_it_links_the_design_system_before_our_own_sheet(self) -> None:
        """app.css 靠加载顺序覆盖 design/ 的 token。顺序反了中文字体就不生效。"""
        design = self.html.index("design/styles.css")
        ours = self.html.index("app.css")
        self.assertLess(design, ours)

    def test_nothing_is_loaded_from_the_network(self) -> None:
        """离线是硬要求。一个 CDN script 标签就能让界面在没网时白屏。"""
        remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:|//)[^"\']*', self.html)
        self.assertEqual(remote, [], "index.html 里有远程资源")

    def test_it_does_not_pull_in_react_or_babel(self) -> None:
        """设计系统自带的 demo 从 unpkg 拉 React + Babel。那条路离线跑不起来,
        而且我们用的是 vanilla 的 ef-* class, 根本不需要它们。"""
        for banned in ("react", "babel", "unpkg", "cdn"):
            self.assertNotIn(banned, self.html.lower())

    def test_all_four_sections_are_present(self) -> None:
        for title in ("运行设置", "识别与控制", "算法库", "实时预览"):
            self.assertIn(title, self.html)

    def test_the_title_bar_is_draggable_and_its_buttons_are_not(self) -> None:
        """无边框窗口全靠这个拖。按钮忘了 no-drag 的话就点不到, 窗口关不掉。"""
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("pywebview-drag-region", css)
        self.assertIn("pywebview-drag-region", self.html)
        self.assertNotIn("-webkit-app-region", css)  # WebView2 里不工作, 见 app.css 的注释

    def test_cjk_falls_back_to_a_system_face(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("Microsoft YaHei", css)
        self.assertNotIn("Noto Sans SC", css)

    def test_only_one_signal_filled_element_in_the_content_area(self) -> None:
        """设计系统的规矩: 「If two things are yellow, one of them is wrong」。
        NavRail 当前项的黄属于外壳, 内容区那一个是启动按钮。"""
        body = self.html.split("</header>")[-1]
        self.assertLessEqual(body.count('data-signal="true"'), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_shell -v
```

Expected: `FileNotFoundError: ...\web\index.html`

- [ ] **Step 3: 读设计系统的 class 名**

```bash
grep -o "^\.ef-[a-z0-9_-]*" rhodes_fast/gui_web/web/design/components/endfield-ui.css | sort -u
```

对着这份清单搭外壳。需要看某个组件的 markup 结构时，用 `DesignSync` 的 `get_file` 读 `components/<分组>/<Name>.jsx`（**当场读，不要落盘**）。

- [ ] **Step 4: 写 `index.html` / `app.css` / `app.js`**

`index.html` 的骨架：

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Endfield</title>
<link rel="stylesheet" href="design/styles.css">
<link rel="stylesheet" href="app.css">
</head>
<body>
<div class="ef-app">
  <header class="ef-titlebar pywebview-drag-region">
    <span class="ef-titlebar__mark">//</span>
    <span class="ef-titlebar__name">ENDFIELD VISION</span>
    <span class="ef-titlebar__meta" id="titlebar-meta">v1.2.0</span>
    <span class="ef-titlebar__spacer"></span>
    <button aria-label="最小化" onclick="pywebview.api.minimize()">−</button>
    <button aria-label="最大化" onclick="pywebview.api.toggle_maximize()">□</button>
    <button aria-label="关闭" onclick="pywebview.api.close()">×</button>
  </header>
  <div class="ef-app__body">
    <nav class="ef-rail">
      <!-- 四项, 结构一样, 只有 data-section / 序号 / 中文 / 拉丁 / 图标不同。
           is-current 那一项由 app.js 的 showSection 切换。 -->
      <button class="ef-rail__item is-current" data-section="01">
        <span class="ef-rail__index">01</span>
        <span class="ef-rail__label">运行设置</span>
        <span class="ef-rail__en">RUNTIME SETTINGS</span>
      </button>
      <!-- 02 识别与控制 DETECTION & CONTROL / 03 算法库 ALGORITHM LIBRARY
           / 04 实时预览 LIVE PREVIEW, 照上面那块复制 -->
    </nav>
    <main class="ef-app__main">
      <div class="ef-section-header"><!-- 序号 / 中文 / 拉丁 --></div>
      <section data-screen="01">
        <p class="ef-meta">// 本页在第三份计划里实现</p>
      </section>
      <section data-screen="02" hidden>
        <p class="ef-meta">// 本页在第三份计划里实现</p>
      </section>
      <section data-screen="03" hidden>
        <p class="ef-meta">// 本页在第三份计划里实现</p>
      </section>
      <section data-screen="04" hidden>
        <p class="ef-meta">// 实时预览在 Task 9 接上</p>
      </section>
      <div class="ef-log" id="runtime-log"></div>
      <div class="ef-actions">
        <button id="start-stop" data-signal="true">启动系统</button>
        <button>应用配置</button>
        <button>保存设置</button>
      </div>
    </main>
  </div>
  <footer class="ef-statusbar" id="statusbar"></footer>
</div>
<script src="app.js"></script>
</body>
</html>
```

`app.js` 本任务只做面板切换和双击最大化：

```js
"use strict";

// 面板切换。NavRail 当前项是唯一带信号黄的外壳元素。
function showSection(id) {
  document.querySelectorAll("[data-screen]").forEach((el) => {
    el.hidden = el.dataset.screen !== id;
  });
  document.querySelectorAll("[data-section]").forEach((el) => {
    el.classList.toggle("is-current", el.dataset.section === id);
  });
  document.dispatchEvent(new CustomEvent("section-changed", { detail: { id } }));
}

document.querySelectorAll("[data-section]").forEach((el) => {
  el.addEventListener("click", () => showSection(el.dataset.section));
});

// 双击标题栏最大化, 跟 Windows 的习惯一致。
document.querySelector(".pywebview-drag-region").addEventListener("dblclick", () => {
  window.pywebview.api.toggle_maximize();
});

showSection("01");
```

> `section-changed` 这个自定义事件 Task 9 要用（切到 04 时 touch `preview_enable_file`）。现在就发出来，免得那时候再回头改。

- [ ] **Step 5: 跑测试确认 GREEN**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_shell -v
```

Expected: `Ran 7 tests` / `OK`

- [ ] **Step 6: 手动看一眼**

```bash
.venv/Scripts/python.exe -c "from rhodes_fast.gui_web.app import main; main()"
```

确认：窗口无系统边框、四项 NavRail 能切、当前项是黄块带左侧黑竖条、字体是 Archivo/Barlow 而不是系统默认、标题栏能拖、三个窗口按钮都好使、双击标题栏能最大化。

- [ ] **Step 7: 提交**

```bash
git add rhodes_fast/gui_web/web/index.html rhodes_fast/gui_web/web/app.css rhodes_fast/gui_web/web/app.js tests/test_gui_web_shell.py
git commit -F - <<'MSG'
feat: build the application shell in the Endfield UI language

TitleBar, NavRail, SectionHeader, the persistent runtime log, the action
row and the StatusBar, with the four sections switching. The sections
themselves are empty and say so; the third plan fills them.

app.css loads after the design system and overrides only what it must: the
CJK stack, because we do not vendor a Chinese face, and the drag regions a
frameless window needs. The window buttons are explicitly no-drag -- miss
that and the window cannot be closed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 8: 启停接线

**Files:**
- Modify: `rhodes_fast/gui_web/app.py`、`web/app.js`、`web/index.html`
- Test: `tests/test_gui_web_app.py`

**Interfaces:**
- Consumes: `GuiSession`（`rhodes_fast.gui_core`）、`PreviewRelay`（Task 5）
- Produces: `Api` 新增 `start()` / `stop()` / `is_running()`；`WebPrompter`（实现 `Prompter`，先用 `webview.windows[0].create_confirmation_dialog` 之类的原生对话框）

**配置从 `settings.txt` 直接读**，本计划不做表单。`Api.start()` 里：

1. `config_path` 用跟旧界面同一套推导（`rhodes_fast/gui.py` 的 `RhodesFastGui.__init__` 怎么算的就怎么算）
2. `relay.start()` 拿预览端口
3. `session.build_command(config_path=…, stop_file=…, runtime_aim_file=…, preview_port=relay.port, preview_enable_file=…, trail_settings_file=…)`
4. `session.start(command, cwd=config_path.parent, on_line=…, on_exit=…)`

`on_line` / `on_exit` **跑在读取线程上**。往界面推日志要用 `window.evaluate_js(...)`——pywebview 自己做线程调度。日志行里有引号和中文，**必须 `json.dumps` 之后再拼进 JS 字符串**，否则一个带引号的路径就能把 `evaluate_js` 打断。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_web_app.py`）**

```python
class StartStopTest(unittest.TestCase):
    def test_start_passes_the_relay_port_into_the_command(self) -> None:
        """预览端口必须是真绑上的那个 —— 子进程要往它发帧。"""
        from rhodes_fast.gui_web.app import Api

        api = Api()
        api.attach(Mock())
        session = Mock()
        session.is_running = False
        session.build_command.return_value = ["python", "-u", "-m", "rhodes_fast"]
        session.start.return_value = Mock()
        relay = Mock()
        relay.start.return_value = 54321
        relay.port = 54321
        api.wire(session=session, relay=relay, config_path=Path("C:/app/settings.txt"))

        api.start()

        kwargs = session.build_command.call_args.kwargs
        self.assertEqual(kwargs["preview_port"], 54321)
        self.assertEqual(kwargs["config_path"], Path("C:/app/settings.txt"))

    def test_starting_twice_is_refused(self) -> None:
        from rhodes_fast.gui_web.app import Api

        api = Api()
        api.attach(Mock())
        session = Mock()
        session.is_running = True
        api.wire(session=session, relay=Mock(), config_path=Path("C:/app/settings.txt"))
        api.start()
        session.build_command.assert_not_called()

    def test_log_lines_are_json_escaped_before_reaching_js(self) -> None:
        """日志里有中文、引号和 Windows 路径的反斜杠。直接拼进 JS 字符串,
        一个带引号的模型路径就能把 evaluate_js 打断。"""
        from rhodes_fast.gui_web.app import Api

        window = Mock()
        api = Api()
        api.attach(window)
        api.push_log('模型 "C:\\MODEL\\a.onnx" 已加载')
        script = window.evaluate_js.call_args.args[0]
        self.assertNotIn('"C:\\MODEL', script)
        self.assertIn("\\\\MODEL", script)

    def test_stop_goes_through_the_session(self) -> None:
        from rhodes_fast.gui_web.app import Api

        api = Api()
        api.attach(Mock())
        session = Mock()
        api.wire(session=session, relay=Mock(), config_path=Path("C:/app/settings.txt"))
        api.stop()
        session.stop.assert_called_once_with()
```

文件顶部补 `from pathlib import Path`。

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_app -v
```

Expected: `AttributeError: 'Api' object has no attribute 'wire'`

- [ ] **Step 3: 实现**

`app.py` 顶部补 `import json`、`import os`。`Api` 加这五个方法：

```python
    def wire(
        self,
        *,
        session,
        relay,
        config_path: Path,
        preview_enable_file: Path | None = None,
    ) -> None:
        self._session = session
        self._relay = relay
        self._config_path = config_path
        # Task 9 的 set_preview_active 用它。启动时也要把路径写进子进程命令行,
        # 所以这里就得收下, 不能等到 Task 9 再加。
        self._preview_enable_file = preview_enable_file

    def is_running(self) -> bool:
        return self._session is not None and self._session.is_running

    def start(self) -> None:
        """配置直接从 settings.txt 读 —— 本计划没有表单。

        预览端口必须先绑上才知道, 所以 relay.start() 要排在 build_command 前面:
        端口号是要写进子进程命令行的。
        """
        if self._session is None or self._session.is_running:
            return
        cache = self._config_path.parent / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        port = self._relay.start()
        command = self._session.build_command(
            config_path=self._config_path,
            stop_file=cache / f"webview-{os.getpid()}.stop",
            runtime_aim_file=cache / f"webview-{os.getpid()}.aim",
            preview_port=port,
            preview_enable_file=self._preview_enable_file,
            trail_settings_file=cache / f"webview-{os.getpid()}.trail",
        )
        self._session.start(
            command,
            cwd=self._config_path.parent,
            on_line=self.push_log,
            on_exit=lambda code: self.push_log(f"已停止（退出码 {code}）。"),
        )

    def stop(self) -> None:
        if self._session is not None:
            self._session.stop()
```

`push_log` 的实现：

```python
    def push_log(self, line: str) -> None:
        """从读取线程调过来。json.dumps 不是洁癖 —— 日志里有中文、引号和
        Windows 路径的反斜杠, 直接拼进 JS 字符串会当场截断。"""
        if self._window is None:
            return
        self._window.evaluate_js(f"window.appendLog({json.dumps(line)})")
```

`main()` 里把 `GuiSession` / `PreviewRelay` / `config_path` 传进 `api.wire(...)`。`config_path` 的推导照 `rhodes_fast/gui.py` 里现有的写法抄，别自己发明一套。

`app.js` 加 `window.appendLog(line)` 和启停按钮的事件。

- [ ] **Step 4: 跑测试确认 GREEN + 全量**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_app -v
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"
```

Expected: 模块 `Ran 8 tests` / `OK`；全量 `OK`

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/gui_web/app.py rhodes_fast/gui_web/web/app.js rhodes_fast/gui_web/web/index.html tests/test_gui_web_app.py
git commit -F - <<'MSG'
feat: start and stop the pipeline from the WebView shell

Reuses GuiSession wholesale -- build_command, start, stop -- so the argv the
child gets is the same one the tkinter GUI builds. Configuration still comes
straight from settings.txt; the forms arrive in the third plan.

Log lines go through json.dumps on the way to evaluate_js. They carry
Chinese, quotes and Windows backslashes, and a model path with a quote in it
would otherwise cut the script short.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## Task 9: 实时预览屏

**Files:**
- Modify: `rhodes_fast/gui_web/web/index.html`、`app.css`、`app.js`、`rhodes_fast/gui_web/app.py`
- Test: `tests/test_gui_web_shell.py`

**Interfaces:**
- Consumes: 全部
- Produces: 04 屏显示实时画面；切进切出时 touch / unlink `preview_enable_file`

**版式照规格第 6 节的 04 屏**：`PreviewOverlay` 的外壳（角括号、垂直水印、底部 telemetry 条）叠在 `<img src="/preview.mjpg">` 上，下面一条 MetricRail。

**检测框不用 CSS 画。** 框已经由子进程的 `render_preview` 烧进 JPEG 了，overlay 只提供 chrome。改成矢量框要改帧协议，超出范围。

**窗口最小化 / 失去焦点时也要关掉预览。** Task 4 实测发现：消费端比生产端慢 6 倍时，画面会旧约 1.38 秒——`FrameBus` 确实只留一帧，但旧帧积在**内核 socket 缓冲**里（有界不发散，调 `SO_SNDBUF`/`SO_RCVBUF` 无效，显式设 `SO_RCVBUF` 反而更糟，因为关掉了 Windows 的接收窗口自动调整）。

正常情况不会碰到（WebView2 解 40KB JPEG 实测端到端 0.37 ms），唯一现实的触发场景是**窗口最小化时浏览器节流渲染**。与其给 socket 写加非阻塞 + 丢帧逻辑（实打实的复杂度），不如在那个场景直接关掉预览：`pywebview` 的 `window.events.minimized` / `restored`（没有的话退回 JS 的 `visibilitychange`）也调 `set_preview_active(false/true)`。

这样更好的地方在于：不看的时候子进程连渲染和 JPEG 编码都不做，省的是副机的 CPU —— 那台机器同时在跑推理。

**`preview_enable_file` 的开关**：Task 7 埋的 `section-changed` 事件派上用场——切到 `04` 调 `pywebview.api.set_preview_active(true)`，切走 `false`。Python 侧 touch / unlink 那个文件。不接这个的话，不看预览时子进程照样渲染 + 编码，白烧 CPU。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_gui_web_shell.py`）**

```python
class PreviewScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_the_preview_is_an_img_fed_by_the_mjpeg_endpoint(self) -> None:
        """整条路径不解码就靠这一行。换成 canvas + base64 推送就白费了。"""
        self.assertIn('src="/preview.mjpg"', self.html)

    def test_it_does_not_draw_detection_boxes_in_css(self) -> None:
        """框已经烧进 JPEG 了。再画一层就是双重框。"""
        self.assertNotIn("detection-box", self.html)

    def test_switching_sections_toggles_the_preview_enable_flag(self) -> None:
        """不接这个开关, 不看预览时子进程照样渲染 + 编码, 白烧 CPU。"""
        self.assertIn("set_preview_active", self.js)
        self.assertIn("section-changed", self.js)
```

追加到 `tests/test_gui_web_app.py`：

```python
class PreviewFlagTest(unittest.TestCase):
    def test_set_preview_active_touches_and_removes_the_file(self) -> None:
        import tempfile
        from pathlib import Path

        from rhodes_fast.gui_web.app import Api

        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "gui.preview"
            api = Api()
            api.attach(Mock())
            api.wire(
                session=Mock(), relay=Mock(),
                config_path=Path(folder) / "settings.txt",
                preview_enable_file=flag,
            )
            api.set_preview_active(True)
            self.assertTrue(flag.exists())
            api.set_preview_active(False)
            self.assertFalse(flag.exists())
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
.venv/Scripts/python.exe -m unittest tests.test_gui_web_shell tests.test_gui_web_app -v
```

Expected: `AssertionError: 'src="/preview.mjpg"' not found in ...`

- [ ] **Step 3: 实现**

`Api.wire` 加 `preview_enable_file: Path | None = None`；加 `set_preview_active(active: bool)`：

```python
    def set_preview_active(self, active: bool) -> None:
        """告诉管线要不要发预览帧。不看的时候让它省下渲染和 JPEG 编码 ——
        这台副机同时在跑推理。"""
        if self._preview_enable_file is None:
            return
        if active:
            self._preview_enable_file.parent.mkdir(parents=True, exist_ok=True)
            self._preview_enable_file.touch()
        else:
            self._preview_enable_file.unlink(missing_ok=True)
```

`app.js` 监听 `section-changed`：

```js
document.addEventListener("section-changed", (event) => {
  if (window.pywebview) {
    window.pywebview.api.set_preview_active(event.detail.id === "04");
  }
});
```

- [ ] **Step 4: 跑测试确认 GREEN + 全量**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests 2>&1 | grep -E "^Ran |^OK|^FAILED"
```

Expected: `OK`

- [ ] **Step 5: 端到端手动验证（本计划的交付物）**

```bash
.venv/Scripts/python.exe -c "from rhodes_fast.gui_web.app import main; main()"
```

逐条确认：

1. 窗口开起来，无系统边框，字体正确
2. 点「启动系统」→ 运行状态里出现日志行（跟旧界面一样的那几句）
3. 切到「04 实时预览」→ **画面在动**
4. 切走再切回 → 画面恢复，没有卡死
5. 点「停止」→ 子进程退出，日志显示停止
6. 关窗 → 进程干净退出，任务管理器里没有残留的 python

**帧率对照**：旧界面在同样输入下的预览是 30fps 上限。如果新界面明显更卡，先查 `FrameBus` 是不是把帧攒住了。

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/gui_web/ tests/
git commit -F - <<'MSG'
feat: show the live preview in the WebView shell

An <img> pointed at /preview.mjpg, with the overlay chrome laid over it.
The detection boxes stay burned into the JPEG by the pipeline -- drawing
them again in CSS would double them, and turning them into vectors means
changing the frame protocol.

Leaving the preview section clears the enable flag, so the child stops
rendering and encoding frames nobody is looking at. That matters here: the
inference process is on this same machine.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
```

---

## 验收标准

- 全量测试 `OK`，不低于 668 + 新增
- `.venv/Scripts/python.exe -c "import sys, rhodes_fast.gui_web; print([n for n in sys.modules if n.startswith('tkinter')])"` → `[]`（WebView 那条路不该拖进 tk）
- 断网状态下窗口照常渲染（字体、样式、图标全在本地）
- 端到端：启动 → 看见画面 → 停止 → 关窗无残留进程
- 旧 `endfield-gui` 一行未改，行为不变

## 明确不做

- **三个业务表单屏**（运行设置 / 识别与控制 / 算法库）—— 第三份计划
- **切换默认入口** —— 第三份计划；本计划结束时 `endfield-gui` 仍是 tkinter
- **Windows Aero Snap**（贴边分屏）—— 需要 `WM_NCHITTEST`，记进 README 的已知限制
- **检测框矢量化** —— 要改帧协议
- **把预览 socket 收进 `gui_core`** —— tkinter 那边解码、WebView 这边中继，形态不同；等两边都稳定了再看要不要抽
- **中文字体自托管**
