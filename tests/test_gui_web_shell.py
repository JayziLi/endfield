from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "rhodes_fast" / "gui_web" / "web"


def _strip_css_comments(text: str) -> str:
    """「某个字符串不许出现」这类断言必须先剥注释。

    解释「为什么不用 X」的注释里一定会写到 X —— 不剥的话测试会被自己的
    文档绊倒, 而且报的错完全看不出是误报。这个坑踩过三次了 (@import、
    Noto Sans SC、ef-preview__box), 所以 HTML 那边也有一份。
    """
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _strip_html_comments(text: str) -> str:
    """同上, HTML 版。见 _strip_css_comments 的说明。"""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _strip_js_comments(text: str) -> str:
    """同上, JS 版。块注释和行注释都剥。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


class ShellTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = (WEB / "app.css").read_text(encoding="utf-8")
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_it_links_the_design_system_before_our_own_sheet(self) -> None:
        """app.css 靠加载顺序覆盖 design/ 的 token。顺序反了中文字体就不生效,
        界面会去找一个我们没有自托管的 Noto Sans SC, 然后掉回系统默认。"""
        self.assertLess(self.html.index("design/styles.css"), self.html.index("app.css"))

    def test_nothing_is_loaded_from_the_network(self) -> None:
        """离线是硬要求。一个 CDN script 标签就能让界面在没网时白屏。"""
        remote = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', self.html)
        self.assertEqual(remote, [], "index.html 里有远程资源")

    def test_it_does_not_pull_in_react_or_babel(self) -> None:
        """设计系统自带的 demo 从 unpkg 拉 React + Babel。那条路离线跑不起来,
        而且我们用的是 vanilla 的 ef-* class, 根本不需要它们。"""
        for banned in ("react", "babel", "unpkg", "cdn."):
            self.assertNotIn(banned, self.html.lower())

    def test_all_four_sections_are_present(self) -> None:
        for index, title in (("01", "运行设置"), ("02", "识别与控制"),
                             ("03", "算法库"), ("04", "实时预览")):
            self.assertIn(f'data-section="{index}"', self.html)
            self.assertIn(f'data-screen="{index}"', self.html)
            self.assertIn(title, self.html)
            self.assertIn(title, self.js)

    def test_only_the_first_section_starts_visible(self) -> None:
        screens = re.findall(r'<section class="ef-screen" data-screen="(\d+)"( hidden)?', self.html)
        self.assertEqual([s for s, h in screens if not h], ["01"])


class FramelessWindowTest(unittest.TestCase):
    """无边框窗口的拖动与窗口按钮 —— 系统什么都不给, 全是我们自己接的。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_the_drag_region_uses_pywebviews_own_class(self) -> None:
        """-webkit-app-region 在 WebView2 里不工作 —— CSS.supports 返回 true、
        computed 值也是 "drag", 但窗口纹丝不动。实测过, 见 app.css 的注释。"""
        self.assertIn("pywebview-drag-region", self.html)
        self.assertNotIn("-webkit-app-region: drag", self.css)
        self.assertNotIn("data-drag", self.html)

    def test_the_window_buttons_are_outside_the_drag_region(self) -> None:
        """pywebview 把 mousedown 挂在可拖元素上, 子元素的 mousedown 会冒泡。
        按钮包在可拖区里的话, 想点最小化会变成拖窗户。"""
        drag = self.html.index("pywebview-drag-region")
        drag_end = self.html.index("</div>", drag)
        for control in ("minimize", "maximize", "close"):
            position = self.html.index(f'data-window="{control}"')
            self.assertGreater(position, drag_end, f"{control} 按钮落在可拖区里面了")

    def test_all_three_window_controls_are_wired(self) -> None:
        """无边框窗口没有系统按钮, 这三个是唯一的出路 —— 漏一个窗口就关不掉。"""
        for control, method in (("minimize", "minimize"),
                                ("maximize", "toggle_maximize"),
                                ("close", "close")):
            self.assertIn(f'data-window="{control}"', self.html)
            self.assertIn(method, self.js)

    def test_double_clicking_the_title_bar_maximises(self) -> None:
        self.assertIn("dblclick", self.js)


class DesignDisciplineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))

    def test_cjk_falls_back_to_a_system_face(self) -> None:
        """我们没有自托管中文字族。app.css 不把这几个变量改掉的话, 界面会去找
        Noto Sans SC, 找不到就掉回系统默认 —— 拉丁和中文会是两种风格。"""
        self.assertIn("Microsoft YaHei", self.css)
        self.assertNotIn("Noto Sans SC", self.css)
        for token in ("--font-display", "--font-ui", "--font-mono", "--font-cjk"):
            self.assertIn(token, self.css, f"{token} 没被覆盖")

    def test_only_one_signal_filled_element_in_the_content_area(self) -> None:
        """设计系统的规矩: 「If two things are yellow, one of them is wrong」。
        NavRail 当前项的黄属于外壳, 内容区那一个是启动/停止按钮。"""
        body = self.html.split("</nav>")[-1]
        self.assertEqual(body.count("ef-btn--primary"), 0, "初始状态不该有填黄的按钮")
        self.assertEqual(body.count("ef-tag--signal"), 0)

    def test_the_rail_marks_the_current_item_with_a_block_not_an_underline(self) -> None:
        """规范原话: Selection is a block, not an underline。整行信号黄填充 +
        前缘 2px ink 竖条, 那条竖条是 design 的 ef-railitem--active::before。"""
        self.assertIn("ef-railitem--active", self.html)

    def test_it_uses_the_design_systems_classes_not_invented_ones(self) -> None:
        """自己发明 class 名等于绕过设计系统。允许的例外是设计系统没有的东西
        (动作行、面板区的滚动容器、占位文案), 它们都带 ef- 前缀但在 app.css 里。"""
        design = (WEB / "design" / "components" / "endfield-ui.css").read_text(encoding="utf-8")
        deco = (WEB / "design" / "components" / "endfield-deco.css").read_text(encoding="utf-8")
        known = set(re.findall(r"\.(ef-[a-z0-9_-]+)", design + deco))
        ours = set(re.findall(r"\.(ef-[a-z0-9_-]+)", self.css))
        used = set(re.findall(r'class="([^"]+)"', self.html))
        used = {c for group in used for c in group.split() if c.startswith("ef-")}
        invented = used - known - ours
        self.assertEqual(invented, set(), f"这些 class 哪儿都没定义: {sorted(invented)}")


class HiddenElementsTest(unittest.TestCase):
    """[hidden] 的 display:none 来自浏览器默认样式, 权重最低。

    元素的 class 上任何一条 display 声明都会盖掉它。症状是本该藏起来的东西一直
    显示着, 而单看 HTML 完全看不出问题 (hidden 属性明明在)。第一次是四个面板
    同时显示, 截图才发现的; 预览屏的 .ef-preview__empty 是同一个形状的坑
    (design 给它设了 display:flex)。

    所以不再一条条盯, 改成扫: index.html 里每个带 hidden 的元素, 它的每个
    ef- class 只要在任何一张表里被设过 display, app.css 就必须把 [hidden] 的
    display:none 写回来。
    """

    def _rules(self, css: str):
        """(选择器组, 声明块) 的粗切分。

        设计系统那几张表是压缩过的, 但结构简单。@media 会让外层的选择器粘在第一
        条内层规则前面 —— 不要紧, 下面只看选择器是不是以 .CLASS 结尾。
        """
        return re.findall(r"([^{}]+)\{([^{}]*)\}", css)

    def _sets_display(self, css: str, klass: str) -> bool:
        for selectors, body in self._rules(css):
            if "display" not in body:
                continue
            if any(part.strip().endswith(f".{klass}") for part in selectors.split(",")):
                return True
        return False

    def _toggled_tags(self, html: str, js: str) -> list[str]:
        """要检查的元素: 标记里就带 hidden 的, 加上 app.js 在运行时切 .hidden 的。

        只扫静态标记是不够的 —— .ef-preview__empty 初始是显示的 (没跑就该看见
        NO SIGNAL), hidden 是 JS 后来加上去的。那条规则一样是承重的, 只是 HTML
        上看不见。
        """
        tags = re.findall(r"<[a-z]+[^>]*\shidden(?:\s[^>]*)?>", html)
        for element_id in re.findall(r'getElementById\("([\w-]+)"\)\.hidden\s*=', js):
            found = re.search(rf'<[a-z]+[^>]*\sid="{re.escape(element_id)}"[^>]*>', html)
            self.assertIsNotNone(found, f"app.js 切 #{element_id} 的 hidden, 但 HTML 里没有它")
            tags.append(found.group(0))
        return tags

    def test_every_hidden_element_keeps_its_display_none(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        js = (WEB / "app.js").read_text(encoding="utf-8")
        app_css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        design = "".join(
            path.read_text(encoding="utf-8") for path in sorted(WEB.glob("design/**/*.css"))
        )

        hidden_tags = self._toggled_tags(html, js)
        self.assertTrue(hidden_tags, "一个 hidden 都没有, 这条测试就是空转的")

        for tag in hidden_tags:
            classes = re.search(r'class="([^"]*)"', tag)
            for klass in (classes.group(1).split() if classes else []):
                if not klass.startswith("ef-"):
                    continue
                if not self._sets_display(design + app_css, klass):
                    continue
                with self.subTest(klass=klass):
                    self.assertRegex(
                        app_css,
                        rf"\.{re.escape(klass)}\[hidden\]\s*\{{[^}}]*display\s*:\s*none",
                        f".{klass} 被设了 display, 但 app.css 没把 [hidden] 的 display:none 写回来",
                    )


class PreviewScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_the_preview_is_an_img_fed_by_the_mjpeg_endpoint(self) -> None:
        """整条路径不解码就靠这一行: 管线 imencode 出来的 JPEG 原样到浏览器。
        换成 canvas + base64 推送, 前面所有的功夫就白费了。"""
        self.assertIn("/preview.mjpg", self.js)
        self.assertIn('id="preview-media"', self.html)

    def test_the_stream_is_not_wired_into_the_markup(self) -> None:
        """src 写死在 HTML 里, 整个界面就哑了。

        /preview.mjpg 是一条不会结束的 multipart 流。页面一加载就挂着它, window
        的 load 事件就永远不触发 —— 而 pywebview 等的正是 load: 等不到它就认为
        窗口没起来, 之后每一次 evaluate_js 都抛 WebViewException。

        症状极难往这儿想: 窗口画得好好的, 但日志一行不出、数字全是占位、按了
        启动按钮状态也不翻, 而 Python 那边只是在读取线程里静静地炸。实测过:
        server 带 bus 时窗口 30 秒都起不来, 不带 bus (预览 404, 立刻失败) 1.3 秒
        就可用 —— 差别只有这一个属性。

        所以流由 JS 在切到 04 时才接上。启动时停在 01, 页面因此是能加载完的。
        """
        media = re.search(r'<img[^>]*id="preview-media"[^>]*>', self.html)
        self.assertIsNotNone(media, "找不到预览的 <img>")
        self.assertNotIn("src=", media.group(0), "预览的 src 不能写在标记里")

    def test_leaving_the_preview_detaches_the_stream(self) -> None:
        """切走不断开的话, server 那条响应线程会一直挂着, 而且浏览器还在解码
        一份没人看的画面。"""
        self.assertIn("removeAttribute", self.js)

    def test_the_frame_is_shown_in_colour_and_uncropped(self) -> None:
        """design 的 .ef-preview__media 自带 grayscale(1) 和 object-fit:cover。

        两条在这里都是错的: 检测框是彩色的 —— 选中的目标画红框, 其余画绿框
        (preview.py:218), 灰掉就分不出自瞄锁的是哪一个; cover 会把画面裁掉,
        而框的位置只有对着完整画面才说得通。
        """
        self.assertRegex(self.css, r"\.ef-preview__media\s*\{[^}]*filter\s*:\s*none")
        self.assertRegex(self.css, r"\.ef-preview__media\s*\{[^}]*object-fit\s*:\s*contain")

    def test_it_does_not_draw_detection_boxes_itself(self) -> None:
        """框已经由子进程烧进 JPEG 了 (preview.py:224)。再用 .ef-preview__box
        画一层就是双重框, 而且那一层的坐标我们根本没有。"""
        self.assertNotIn("ef-preview__box", _strip_html_comments(self.html))

    def test_switching_sections_toggles_the_preview_enable_flag(self) -> None:
        """不接这个开关, 不看预览时子进程照样渲染 + 编码 —— 而这台副机同时在跑
        推理。section-changed 是 Task 7 就埋好的。"""
        self.assertIn("set_preview_active", self.js)
        self.assertIn("section-changed", self.js)

    def test_the_metric_rail_is_fed_by_the_pipelines_own_numbers(self) -> None:
        """四格指标要有真数字。全是横杠的 MetricRail 比没有更糟 —— 它看着像坏了。"""
        for field in ("capture_fps", "processed_fps", "infer_ms", "dropped"):
            self.assertIn(field, self.js, f"{field} 没接到界面上")
        self.assertIn("clearTelemetry", self.js)

    def test_the_metrics_do_not_add_a_second_patch_of_signal_yellow(self) -> None:
        """design 的 .ef-metric__cap 是一块 34x5 的信号黄。四格就是四块, 再加上
        跑起来填黄的启动按钮 —— 「If two things are yellow, one of them is wrong」。
        这一屏的那一个黄留给按钮: 它回答「系统在不在跑」。"""
        self.assertNotIn("ef-metric__cap", self.html)


class ModalTest(unittest.TestCase):
    """设计系统没有 Dialog（规格第 7 节的两个缺口之一），这一套是自拼的。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.js = _strip_js_comments((WEB / "modal.js").read_text(encoding="utf-8"))

    def test_modal_js_is_loaded_before_app_js(self) -> None:
        """app.js 起来时就可能收到 showModal（启动时载入预设那条路会弹窗）。

        先剥注释: 预览那个 <img> 的注释里提到了 app.js, 而它排在模态框前面 ——
        不剥的话 index() 命中的是注释, 这条断言就成了随机数。
        """
        markup = _strip_html_comments(self.html)
        self.assertLess(markup.index("modal.js"), markup.index("app.js"))

    def test_the_answer_carries_the_token_back(self) -> None:
        """Python 那边正有一条线程阻塞在这个 token 上。不送 token 的话, 两个
        弹窗叠起来时答案会串 —— 而那两个很可能一个是「要保存吗」一个是
        「确定删除吗」。"""
        self.assertIn("answer_prompt", self.js)
        self.assertIn("token", self.js)

    def test_escape_cancels(self) -> None:
        """无边框窗口没有系统的关闭按钮可用。弹窗卡住就只能杀进程。"""
        self.assertIn("Escape", self.js)

    def test_all_six_prompter_kinds_have_buttons(self) -> None:
        """Prompter 有六个方法。少接一种的话, 那一类问题弹出来是一个没有任何
        按钮的框 —— 只能按 Esc, 而用户不知道。"""
        for kind in ("error", "warning", "info", "confirm", "three_way", "text"):
            self.assertIn(kind, self.js, f"{kind} 没有按钮")

    def test_the_modal_does_not_add_a_second_patch_of_signal_yellow(self) -> None:
        """内容区那一个填黄的块是启动/停止按钮。弹窗再填一个就有两个了 ——
        设计系统的原话: If two things are yellow, one of them is wrong。"""
        self.assertNotIn("ef-btn--primary", self.js)

    def test_the_message_keeps_its_line_breaks(self) -> None:
        """正文里有 
（「…吗？

删除后找不回来。」这类）。折掉的话两段挤成
        一行, 而第二段往往正是「删除后找不回来」。"""
        self.assertRegex(self.css, r"\.ef-modal__message\s*\{[^}]*white-space\s*:\s*pre-wrap")


class RuntimeScreenTest(unittest.TestCase):
    """01 运行设置屏。控件到字段的绑定全在 data-field 上。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "forms.js").read_text(encoding="utf-8")

    def test_every_form_field_has_a_control(self) -> None:
        """漏一个字段的症状是「改了没用」—— 保存下去的是旧值, 而且不报错。

        按 FormState 的字段表扫, 将来加字段时这条会自动红。不在这三屏上的
        几个单列出来, 而不是从表里删掉 —— 那样加字段时它们会被静默漏掉。
        """
        from dataclasses import fields

        from rhodes_fast.gui_core.state import FormState

        # profiles 不是控件, 是两套方案的容器 —— 它底下每个字段都有自己的
        # data-field, DetectionScreenTest 按方案扫。
        elsewhere = {"profiles"}
        for field in fields(FormState):
            if field.name in elsewhere:
                continue
            with self.subTest(field=field.name):
                self.assertIn(f'data-field="{field.name}"', self.html)

    def test_the_uuid_is_not_masked(self) -> None:
        """用户明确决定明文显示, 跟旧界面一致 (那边本来就是普通 Entry)。
        顺手做成密码框是个很容易犯的「好心」—— 而它会让用户核对不了设备。"""
        match = re.search(r'<input[^>]*data-field="kmbox_uuid"[^>]*>', self.html)
        self.assertIsNotNone(match)
        self.assertNotIn('type="password"', match.group(0))

    def test_the_model_path_uses_the_native_dialog(self) -> None:
        """HTML 的文件输入框只给一个假路径 (C:/fakepath/...), 而我们要往
        settings.txt 里写真实路径。"""
        self.assertIn("browse_model", self.js)
        self.assertNotIn('type="file"', self.html)

    def test_the_two_input_groups_are_mutually_exclusive(self) -> None:
        """UDP 和 OBS 同时显示的话, 用户会填错一组然后奇怪为什么没生效。"""
        self.assertIn('id="udp-panel"', self.html)
        self.assertIn('id="obs-panel"', self.html)
        self.assertIn("syncInputPanels", self.js)

    def test_the_local_screen_has_its_own_input_group(self) -> None:
        """本机屏幕是第三组, 跟 UDP / OBS 三选一显示。它的几个字段放在别的组里的
        话, 选 UDP 时用户会看到一个不起作用的「显示器编号」。"""
        group = self._element('id="desktop-panel"')
        for name in ("desktop_backend", "desktop_monitor", "desktop_width", "desktop_height"):
            with self.subTest(field=name):
                self.assertIn(f'data-field="{name}"', group)
        self.assertIn("desktop-panel", self.js)

    def test_the_local_screen_warns_that_our_own_windows_get_captured(self) -> None:
        """抓的是合成之后的整块屏幕。预览窗口盖在正中央的话会被当成游戏画面去
        识别 —— 而用户只会看到「识别出一堆奇怪的框」, 猜不到原因。"""
        group = self._element('id="desktop-panel"')
        self.assertIn("正中央", group)
        self.assertIn("抓进去", group)

    def test_the_output_panel_picks_who_moves_the_mouse(self) -> None:
        self.assertIn('data-field="mouse_output"', self.html)
        self.assertIn("syncOutputPanels", self.js)

    def test_every_kmbox_setting_lives_in_the_kmbox_group(self) -> None:
        """选 SendInput 时 KMBox 那几项整组藏起来。「启用 KMBox 控制」也在组里:
        它只管 KMBox, 留在外面的话看起来像是 SendInput 的总开关。"""
        group = self._element('id="kmbox-fields"')
        for name in ("kmbox_enabled", "kmbox_host", "kmbox_port", "kmbox_uuid"):
            with self.subTest(field=name):
                self.assertIn(f'data-field="{name}"', group)

    def test_sendinput_says_it_can_be_recognised(self) -> None:
        """SendInput 注入的事件带 LLMHF_INJECTED 标记, 这是它的固有属性。
        选它的人应该在选的那一刻就知道。"""
        note = self._element('id="sendinput-note"')
        self.assertIn("注入", note)

    def test_the_third_lamp_is_titled_after_the_output(self) -> None:
        """灯上写着 KMBox 而实际用的是 SendInput, 用户会去查一个根本没在用的盒子。"""
        self.assertIn('id="lamp-kmbox-title"', self.html)
        self.assertIn("lamp-kmbox-title", self.js)

    def _element(self, marker: str) -> str:
        """marker 所在那个元素的完整 HTML (按同名标签配对, 不做真解析)。"""
        position = self.html.index(marker)
        start = self.html.rindex("<", 0, position)
        tag = re.match(r"<(\w+)", self.html[start:]).group(1)
        depth = 0
        for match in re.finditer(rf"<(/?){tag}\b[^>]*>", self.html[start:]):
            depth += -1 if match.group(1) else 1
            if depth == 0:
                return self.html[start : start + match.end()]
        self.fail(f"{marker} 没有配对的结束标签")

    def test_the_boot_time_settings_are_locked_while_running(self) -> None:
        """模型、浏览、加速方式、预设下拉只在启动时读一次。运行中留着能改就是
        在骗人 —— 改了不生效, 而界面上看不出来。预设一起锁是因为载入预设会换
        模型。照 gui.py 的 _set_running。"""
        for marker in ('data-field="model_path"', 'id="browse-model"',
                       'data-field="provider"', 'id="preset-select"'):
            position = self.html.index(marker)
            tag_start = self.html.rindex("<", 0, position)
            tag_end = self.html.index(">", position)
            with self.subTest(marker=marker):
                self.assertIn("data-lock-while-running", self.html[tag_start:tag_end])

    def test_saving_a_preset_stays_available_while_running(self) -> None:
        """反向断言, 防的是顺手全锁了: 保存和另存为在运行中正该能用 —— 边打边
        调好了, 那一刻正是想存下来的时候。"""
        for marker in ('id="preset-save"', 'id="preset-save-as"', 'id="action-save"'):
            position = self.html.index(marker)
            tag_start = self.html.rindex("<", 0, position)
            tag_end = self.html.index(">", position)
            with self.subTest(marker=marker):
                self.assertNotIn("data-lock-while-running", self.html[tag_start:tag_end])

    def test_the_bindings_are_not_attached_twice(self) -> None:
        """renderParams 建完新控件要再调一次 bindForm。没有这个标记的话, 老控件
        会挂上第二个监听器, 一次改动推两次 —— 而两次的值一样, 完全看不出来。"""
        self.assertIn("dataset.bound", self.js)


class DetectionScreenTest(unittest.TestCase):
    """02 识别与控制屏。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "forms.js").read_text(encoding="utf-8")

    def test_both_profiles_have_every_control(self) -> None:
        """两套方案是对称的。下标写错的症状是「调方案 1 结果方案 2 变了」——
        而两栏长得一模一样, 用户第一反应是自己看错了。"""
        for index in (0, 1):
            for name in ("enabled", "trigger", "target_class", "aim_position",
                         "fov", "kp_min", "kp_max", "kp_growth"):
                with self.subTest(profile=index, field=name):
                    self.assertIn(f'data-field="profiles.{index}.{name}"', self.html)

    def test_both_profiles_have_an_algorithm_picker(self) -> None:
        """算法下拉没有 data-field —— 换算法要连参数一起换, 走的是 set_algorithm。
        所以它按 data-algorithm 认。"""
        for index in (0, 1):
            self.assertIn(f'data-algorithm="{index}"', self.html)

    def test_the_slider_ranges_match_the_old_gui(self) -> None:
        """范围自己定的话手感就变了, 而且是悄悄变的: 同一个位置对应的数不一样。"""
        for name, low, high in (
            ("confidence", "0.05", "0.95"),
            ("iou", "0.05", "0.95"),
            ("profiles.0.aim_position", "0", "100"),
            ("profiles.0.fov", "10", "320"),
            ("profiles.1.kp_min", "0", "0.3"),
            ("profiles.1.kp_max", "0", "0.3"),
            ("profiles.1.kp_growth", "0", "0.5"),
        ):
            with self.subTest(field=name):
                tag = re.search(
                    rf'<input[^>]*data-field="{re.escape(name)}"[^>]*>', self.html, re.S
                )
                self.assertIsNotNone(tag, f"{name} 没有控件")
                self.assertIn(f'min="{low}"', tag.group(0))
                self.assertIn(f'max="{high}"', tag.group(0))

    def test_the_slider_thumb_is_driven_by_script(self) -> None:
        """design 的滑条是一个 opacity:0 的原生 range 盖在轨道上, 看得见的填充
        和滑块是两个绝对定位的 div —— 位置得自己算。不同步的话滑块永远停在最
        左边, 而值其实在变。"""
        self.assertIn("syncSlider", self.js)

    def test_algorithm_params_are_generated_not_hard_coded(self) -> None:
        """参数按 Param 契约现建。写死的话, 用户从算法库导入一个自己写的算法,
        参数一个都出不来 —— 而那正是算法库存在的理由。"""
        self.assertIn("renderParams", self.js)
        for invented in ("wind_strength", "kp_growth_rate", "max_step"):
            self.assertNotIn(invented, _strip_html_comments(self.html))

    def test_the_kmbox_lamp_does_not_claim_a_connection_it_cannot_see(self) -> None:
        """正常运行时管线只打配置里的 KMBox 地址, 不报连没连上 —— 连接结果只有
        「测试输入」那条路才有。把一盏只反映配置的灯写成「已连接」, 是那种用户
        会依赖的谎: 他以为设备好了, 结果一枪不打。"""
        markup = _strip_html_comments(self.html)
        self.assertIn("未验证", markup)
        self.assertNotIn("已连接", markup)

    def test_a_disabled_profile_is_dimmed_and_not_clickable(self) -> None:
        """留着能点的话, 用户会调半天一个根本不生效的方案。"""
        css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.assertIn("syncProfileEnabled", self.js)
        self.assertRegex(css, r"\.ef-profile--off[^{]*\{[^}]*pointer-events\s*:\s*none")


class LibraryScreenTest(unittest.TestCase):
    """03 算法库屏。设计系统没有 Table，这一套是自拼的。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.js = _strip_js_comments((WEB / "library.js").read_text(encoding="utf-8"))

    def test_library_js_is_loaded(self) -> None:
        self.assertIn("library.js", self.html)

    def test_the_columns_match_what_the_session_returns(self) -> None:
        """GuiSession.library_rows() 返回六元组。列数对不上的话, 表格要么少一列
        要么多一个空格子, 而按下标取的那几列全错位。"""
        columns = re.search(r"LIBRARY_COLUMNS\s*=\s*\[([^\]]*)\]", self.js)
        self.assertIsNotNone(columns)
        self.assertEqual(len(columns.group(1).split(",")), 6)

    def test_builtin_rows_cannot_be_renamed_or_deleted(self) -> None:
        """内置算法随程序分发。GuiSession 会拒绝, 但按钮不置灰的话用户点了才
        被拒 —— 而拒绝理由跟「这个算法坏了」长得一样。"""
        self.assertIn("内置", self.js)
        self.assertIn("disabled", self.js)
        for button in ("library-source", "library-rename", "library-delete"):
            tag = re.search(rf'<button[^>]*id="{button}"[^>]*>', self.html)
            self.assertIsNotNone(tag, button)
            self.assertIn("disabled", tag.group(0), f"{button} 初始应当是灰的")

    def test_the_selection_is_tracked_by_identifier_not_by_row_index(self) -> None:
        """导入或删除之后表会重排。按下标记的话, 三个按钮会对着另一个算法 ——
        而「删除」那个后果是不可逆的。"""
        self.assertIn("dataset.name", self.js)
        self.assertNotIn("selectedIndex", self.js)

    def test_the_selected_row_is_a_block_not_an_underline(self) -> None:
        """规范 States 表的原话: Selection is a block, not an underline。"""
        self.assertRegex(
            self.css, r"\.ef-libraryrow--active[^{]*\{[^}]*background-color\s*:\s*var\(--signal"
        )

    def test_importing_and_refreshing_do_not_need_a_selection(self) -> None:
        """这两个是对整个库的操作。要求先选一行的话, 空库里连导入都点不了。"""
        self.assertIn('"import_algorithm", false', self.js)
        self.assertIn('"refresh_library", false', self.js)



class ScriptSyntaxTest(unittest.TestCase):
    """页面上那几个 .js 真的能被解析。

    这条是补出来的: 有一次 app.js 里多了一个没闭合的字符串, 整份脚本当场死掉 ——
    界面画出来了但一个按钮都不响应, 而全量测试一条没红。上面那些测试读的是 .js
    的**文本**, 做子串匹配, 语法坏不坏它们根本看不见。

    没装 node 就跳过。为这一条去引一个 JS 解析器不值得, 而这台机器上有。
    """

    def test_every_script_parses(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("没有 node, 跳过语法检查")
        for path in sorted(WEB.glob("*.js")):
            with self.subTest(script=path.name):
                done = subprocess.run(
                    [node, "--check", str(path)], capture_output=True, text=True
                )
                self.assertEqual(done.returncode, 0, done.stderr)





class StandaloneWindowPagesTest(unittest.TestCase):
    """源码窗口和放大预览窗口的两张页面。"""

    def setUp(self) -> None:
        self.source_html = (WEB / "source.html").read_text(encoding="utf-8")
        self.source_js = _strip_js_comments((WEB / "source.js").read_text(encoding="utf-8"))
        self.popout_html = _strip_html_comments((WEB / "popout.html").read_text(encoding="utf-8"))
        self.popout_js = _strip_js_comments((WEB / "popout.js").read_text(encoding="utf-8"))
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))

    def test_the_source_never_goes_through_innerhtml(self) -> None:
        """这是一份别人写的、还没审过的代码。源码里写一句 <script> 的话,
        textContent 显示那几个字符, innerHTML 执行它 —— 而这个窗口存在的理由
        正是「执行之前先看一眼」。"""
        self.assertNotIn("innerHTML", self.source_js)
        self.assertIn("textContent", self.source_js)

    def test_the_source_does_not_wrap(self) -> None:
        """Python 的缩进就是语法。折行之后缩进看不清, 用户读到的就不是那份代码的
        结构了。"""
        self.assertRegex(self.css, r"\.ef-sourcepage__lines li\s*\{[^}]*white-space:\s*pre;")

    def test_the_source_scrolls_instead_of_being_cut_off(self) -> None:
        """原来那个弹框的毛病: 没有高度上限也没有滚动, 下半截直接切掉。"""
        self.assertRegex(self.css, r"\.ef-sourcepage__code\s*\{[^}]*overflow:\s*auto")

    def test_the_popout_stream_is_not_in_the_markup(self) -> None:
        """/preview.mjpg 是一条不会结束的流。写在标记里的话 load 事件永远不触发,
        pywebview 一直当这个窗口没起来 —— 主窗口踩过这个坑。"""
        tag = re.search(r"<img[^>]*>", self.popout_html)
        self.assertIsNotNone(tag)
        self.assertNotIn("src=", tag.group(0))
        self.assertIn("preview.mjpg", self.popout_js)
        self.assertIn('addEventListener("load"', self.popout_js)

    def test_the_popout_waiting_text_can_actually_hide(self) -> None:
        """.ef-popout__empty 设了 display:flex, 会盖掉 [hidden] 的 display:none。
        不写回来的话画面来了「等待画面」还压在上面。"""
        self.assertRegex(self.css, r"\.ef-popout__empty\[hidden\]\s*\{[^}]*display:\s*none")

    def test_both_pages_use_only_classes_that_exist(self) -> None:
        design = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(WEB.glob("design/components/*.css"))
        )
        known = set(re.findall(r"\.(ef-[a-z0-9_-]+)", design + self.css))
        for name, html in (("source.html", self.source_html), ("popout.html", self.popout_html)):
            used = {
                klass
                for group in re.findall(r'class="([^"]+)"', html)
                for klass in group.split()
                if klass.startswith("ef-")
            }
            with self.subTest(page=name):
                self.assertEqual(used - known, set())

    def test_the_preview_screen_has_the_button(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        js = _strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))
        self.assertIn('id="preview-popout"', html)
        self.assertIn('"preview-popout": "open_preview_window"', js)

class LogCollapseTest(unittest.TestCase):
    """运行日志能折起来, 把高度让给预览画面。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))
        self.js = _strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))

    def test_there_is_a_toggle(self) -> None:
        self.assertIn('id="log-toggle"', self.html)
        self.assertIn('aria-expanded', self.html)

    def test_collapsing_hides_the_scrolling_log(self) -> None:
        self.assertRegex(
            self.css, r"\.ef-logwrap--collapsed \.ef-log\s*\{[^}]*display\s*:\s*none"
        )

    def test_the_fixed_status_line_survives_the_fold(self) -> None:
        """折起来之后那条固定行是唯一还看得到的实时数字, 而它只有 28px。
        跟着一起藏掉的话, 折叠就等于「运行中什么都看不见」。"""
        self.assertNotRegex(
            self.css, r"\.ef-logwrap--collapsed[^{]*\.ef-statusline\s*\{[^}]*display\s*:\s*none"
        )

    def test_the_fold_is_remembered(self) -> None:
        """折叠是为了腾地方。不记住的话每次打开都要再折一次, 这个开关就没意义了。"""
        self.assertIn("set_log_collapsed", self.js)
        self.assertIn("setLogCollapsed", self.js)

class PreviewFitTest(unittest.TestCase):
    """04 屏在矮窗口下不许叠在一起。

    真 WebView2 里量出来的原形: .ef-preview 上写着 min-height:220px, 而它外层的
    .ef-previewwrap 是 flex:1 + min-height:0, 窗口一矮就被挤到 180px。孩子不肯
    跟着缩, 于是溢出到外层外面 95px, 向下盖住 MetricRail —— 画面的黑底从四张
    指标卡的缝隙里透出来。

    下限要放在外层: 空间够就一起伸缩, 谁都不会越界; 空间不够时外层停在下限上,
    由 .ef-screens 的 overflow:auto 出滚动条。挤不下是滚动, 不是盖住。
    """

    def setUp(self) -> None:
        self.css = _strip_css_comments((WEB / "app.css").read_text(encoding="utf-8"))

    def _rule(self, selector: str) -> str:
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, f"{selector} 的规则没了")
        return match.group(1)

    def test_the_preview_can_shrink_with_its_parent(self) -> None:
        body = self._rule(".ef-previewwrap .ef-preview")
        self.assertIn("flex: 1", body)
        self.assertRegex(body, r"min-height:\s*0;")

    def test_the_floor_lives_on_the_wrapper(self) -> None:
        """下限得有, 不然矮到一定程度预览会缩成一条线。只是它归外层管。"""
        body = self._rule(".ef-previewwrap")
        found = re.search(r"min-height:\s*(\d+)px", body)
        self.assertIsNotNone(found, "外层没有下限")
        self.assertGreater(int(found.group(1)), 0)

    def test_the_scroll_container_can_actually_scroll(self) -> None:
        """挤不下时要有地方滚。.ef-screens 不是 auto 的话, 溢出就又变成盖住。"""
        self.assertRegex(self._rule(".ef-screens"), r"overflow:\s*auto")

class SystemLampTest(unittest.TestCase):
    """02 屏那三盏灯。原来是写死的标记, 从头到尾一个字不变。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = _strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))

    def test_each_lamp_has_the_id_the_script_looks_up(self) -> None:
        from rhodes_fast.gui_web.lamps import IDLE

        for name in IDLE:
            with self.subTest(lamp=name):
                self.assertIn(f'id="lamp-{name}"', self.html)
                self.assertIn(f'id="lamp-{name}-sub"', self.html)

    def test_the_title_and_the_sub_are_stacked(self) -> None:
        """design 的 .ef-lamp__text 是 flex column。不套这个类的话, 标题和副标题
        会并排挤在一行 —— 用户那张截图上就是这样。"""
        self.assertEqual(self.html.count("ef-lamp__text"), 3)

    def test_every_design_state_is_handled(self) -> None:
        """设计系统给了五个状态类。脚本少认一个的话, 那个状态挂上去是一盏没有
        样式的灯 —— 看起来跟熄灭的一模一样。

        而且切状态时必须先把旧的摘掉: 五个类叠在一起会出一盏又亮黄又闪烁的灯。
        """
        design = (WEB / "design" / "components" / "endfield-ui.css").read_text(encoding="utf-8")
        states = set(re.findall(r"\.ef-lamp--([a-z]+)", design)) - {"inverse"}
        self.assertEqual(states, {"off", "standby", "connecting", "online", "error"})
        for state in states:
            with self.subTest(state=state):
                self.assertIn(f'"{state}"', self.js)
        self.assertIn("classList.remove", self.js)

class RunLockTest(unittest.TestCase):
    """运行中锁住那四个只在启动时读一次的控件。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = _strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))

    def test_the_run_state_is_what_applies_the_lock(self) -> None:
        """锁和解锁必须是同一段代码的两个方向。分成两处写的话, 停止那一下漏掉
        某个控件, 用户就得重启程序才能再改它。"""
        self.assertIn("data-lock-while-running", self.js)
        # 锁定这件事只能有一处: 分成「启动时锁」和「停止时解锁」两段的话, 停止
        # 那一下漏掉某个控件, 用户就得重启程序才能再改它。
        self.assertEqual(self.js.count("data-lock-while-running"), 1)
        # 而且那一处要跟着运行状态走, 不是两个写死的 true / false。
        self.assertIn("element.disabled = locked", self.js)
        self.assertIn("setLocks(running)", self.js)

    def test_the_locked_control_also_looks_locked(self) -> None:
        """原生 disabled 只让控件不可点。design 的外框是包在外面那一层上的
        (.ef-input / .ef-select), 不跟着改的话, 一个点不动的输入框看起来跟能用的
        一模一样 —— 用户只会以为界面卡了。"""
        self.assertIn("ef-input--disabled", self.js)
        self.assertIn("ef-select--disabled", self.js)


class ActionRowTest(unittest.TestCase):
    """动作行: 保存设置 / 测试输入 / 模型测速 / 管线测速 / 记录延迟日志。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = _strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))

    def test_every_action_has_a_button(self) -> None:
        for element_id in ("action-save", "action-check", "action-benchmark", "action-pipeline"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_the_benchmarks_do_not_go_through_start(self) -> None:
        """start() 会绑预览 socket。测速走它的话, 测出来的数字里掺着一份没人看的
        渲染和 JPEG 编码 —— 而测速正是为了拿准数。"""
        for name in ("run_check", "run_benchmark", "run_pipeline_benchmark", "save_settings"):
            with self.subTest(name=name):
                self.assertIn(name, self.js)

    def test_the_latency_switch_is_a_form_field(self) -> None:
        """启动时读一次决定给不给 --latency-log。不接字段的话那个勾选框谁都看不见。"""
        self.assertIn('data-field="latency_log_enabled"', self.html)

    def test_the_action_buttons_do_not_add_a_second_patch_of_signal_yellow(self) -> None:
        """内容区同时只有一个黄块, 就是启动/停止按钮 —— 它回答「系统在不在跑」。
        动作行上再来四个填黄的, 那个问题就没人看得出答案了。"""
        row = self.html.split('class="ef-actions"')[-1].split("</div>")[0]
        self.assertNotIn("ef-btn--primary", row)


class PreviewControlsTest(unittest.TestCase):
    """04 屏的控制栏: 画面 / 轨迹 / 最优路径 / 轨迹长度。"""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = _strip_js_comments((WEB / "forms.js").read_text(encoding="utf-8"))

    def test_all_four_controls_are_present(self) -> None:
        for name in ("preview_frame", "trail_enabled", "trail_optimal_path", "trail_seconds"):
            with self.subTest(name=name):
                self.assertIn(f'data-field="{name}"', self.html)

    def test_the_length_range_matches_the_pipeline(self) -> None:
        """范围跟管线对不上的话, 滑条能拖到的值会被那边再夹一次, 而界面上显示的
        是夹之前的数 —— 两个地方各说各话。"""
        from rhodes_fast.config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS

        tag = re.search(r'<input[^>]*data-field="trail_seconds"[^>]*>', self.html, re.S)
        self.assertIsNotNone(tag)
        self.assertIn(f'min="{TRAIL_MIN_SECONDS}"', tag.group(0))
        self.assertIn(f'max="{TRAIL_MAX_SECONDS:g}"', tag.group(0))

    def test_the_length_and_the_optimal_path_follow_the_trail_switch(self) -> None:
        """轨迹关着的时候这两个都没有意义。留着能拖的话, 用户会调半天一个根本
        没在画的东西。"""
        self.assertIn("syncTrailControls", self.js)
        self.assertIn("ef-slider--disabled", self.js)

    def test_the_length_is_persisted_on_release_not_on_every_tick(self) -> None:
        """每次 input 都写盘的话, 拖一下滑条就是上百次写 settings.txt。原生 range
        的 change 是松手才发一次 —— 正好是旧界面那个防抖要的效果。"""
        match = re.search(r'addEventListener\("(\w+)", \(\) => \{\s*if \(window\.pywebview'
                          r'[^}]*persist_trail_length', self.js)
        self.assertIsNotNone(match, "persist_trail_length 没接在事件上")
        self.assertEqual(match.group(1), "change")

if __name__ == "__main__":
    unittest.main()
