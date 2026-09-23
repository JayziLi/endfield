from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "rhodes_fast" / "gui_web" / "web"
DESIGN = WEB / "design"


class DesignSystemAssetsTest(unittest.TestCase):
    def test_styles_css_exists(self) -> None:
        self.assertTrue((DESIGN / "styles.css").is_file(), f"没拉到 {DESIGN / 'styles.css'}")

    def test_every_import_in_styles_css_resolves(self) -> None:
        """styles.css 就是一串 @import。少一个文件, 界面上少一整层样式,
        而浏览器对解析不了的 @import 是静默失败的 —— 不会报错, 只会难看。"""
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
        """离线是硬要求。一个远程引用就能让界面在没网时缺一层样式或掉回系统字体。

        自从字体自托管之后 fonts.css 不再是例外 —— 这里一个文件都不放过。"""
        sheets = [s for s in DESIGN.rglob("*.css")]
        # 没有这行守卫的话, design/ 整个不存在时 rglob 给一个空迭代器,
        # offenders 恒为 [] —— 这条测试会在「一个文件都没拉」时照样变绿。
        self.assertGreaterEqual(len(sheets), 11, f"只扫到 {len(sheets)} 个样式表, 这条测试在空转")
        offenders: list[str] = []
        for sheet in sheets:
            for url in re.findall(r'url\(\s*["\']?([^"\')]+)', sheet.read_text(encoding="utf-8")):
                if url.startswith(("http://", "https://", "//")):
                    offenders.append(f"{sheet.relative_to(DESIGN)} → {url}")
        self.assertEqual(offenders, [], "样式表里有远程引用")

    def test_every_local_url_a_stylesheet_names_actually_exists(self) -> None:
        """上一条只拦远程引用, 拦不住「引了一个本地但不存在的文件」。
        那种情况浏览器同样静默失败 —— 界面上少一块东西, 控制台一声不吭。"""
        missing: list[str] = []
        for sheet in DESIGN.rglob("*.css"):
            for url in re.findall(r'url\(\s*["\']?([^"\')]+)', sheet.read_text(encoding="utf-8")):
                if url.startswith(("http://", "https://", "//", "data:", "#")):
                    continue
                if not (sheet.parent / url).resolve().is_file():
                    missing.append(f"{sheet.relative_to(DESIGN)} → {url}")
        self.assertEqual(missing, [], "样式表引用了不存在的本地文件")


class IconsTest(unittest.TestCase):
    """图标走 icons.js 的 inline SVG, 不走 design/assets/icons/ 下的独立文件。

    设计系统的 Icon 渲染的是 <svg stroke="currentColor">, 图标继承所在底色;
    用 <img src="box.svg"> 引外部文件的话 currentColor 染不进去, 而 NavRail
    选中项是黄底黑字 —— 图标会留在错误的颜色上。
    """

    def setUp(self) -> None:
        self.source = (WEB / "icons.js").read_text(encoding="utf-8")

    def test_the_nav_rail_glyphs_are_present(self) -> None:
        for slug in ("box", "crosshair", "layout-grid", "monitor-play", "settings", "file-text"):
            self.assertIn(f'"{slug}"', self.source, slug)

    def test_the_window_control_glyphs_are_present(self) -> None:
        """无边框窗口的最小化/最大化/关闭三个按钮要用。"""
        for slug in ("minus", "square", "x"):
            self.assertIn(f'"{slug}"', self.source, slug)

    def test_glyphs_inherit_the_ground_colour(self) -> None:
        self.assertIn('stroke="currentColor"', self.source)




class SelfHostedFontsTest(unittest.TestCase):
    """字体自托管。原版 fonts.css 整个就是一行 Google Fonts 的 @import。"""

    def setUp(self) -> None:
        raw = (DESIGN / "tokens" / "fonts.css").read_text(encoding="utf-8")
        # 剥掉注释再查: 文件头的说明里就写着「原版是一行 @import」, 那是文档不是规则。
        self.css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        self.raw = raw
        self.fonts = WEB / "fonts"

    def test_it_no_longer_reaches_the_network(self) -> None:
        """这是全套 CSS 里唯一碰过网络的文件。离线时它未必快速失败 ——
        在需要认证的网络下, 那条 @import 会把整张样式表的加载吊住。"""
        self.assertNotIn("@import", self.css)
        self.assertNotIn("http://", self.css)
        self.assertNotIn("https://", self.css)

    def test_all_four_families_are_declared(self) -> None:
        for family in ("Archivo", "Barlow", "Barlow Condensed", "IBM Plex Mono"):
            self.assertIn(f'font-family: "{family}"', self.css, f"缺 {family}")

    def test_the_display_face_covers_the_whole_weight_range(self) -> None:
        """Archivo 是可变字体, 一个文件覆盖 100..900。写死成单个字重的话,
        --weight-black (900) 会退化成合成加粗, 大数字会糊。"""
        self.assertIn("font-weight: 100 900", self.css)

    def test_no_font_file_is_orphaned(self) -> None:
        """磁盘上有、却没有任何 @font-face 引用的文件 = 白占仓库体积。
        Archivo 去重时最容易在这里出错。"""
        named = {Path(u).name for u in re.findall(r'url\("([^"]+\.woff2)"\)', self.css)}
        on_disk = {f.name for f in self.fonts.glob("*.woff2")}
        self.assertEqual(on_disk - named, set(), "有没人引用的字体文件")
        self.assertEqual(named - on_disk, set(), "引用了不存在的字体文件")

    def test_no_cjk_font_binary_was_vendored(self) -> None:
        """中文走系统字体。Noto Sans SC 全量一个字重约 5MB, 不该进仓库;
        子集化又会在日志和报错的动态中文上出豆腐块。"""
        heavy = [f.name for f in self.fonts.glob("*.woff2") if f.stat().st_size > 1_000_000]
        self.assertEqual(heavy, [], "有超过 1MB 的字体文件, 八成是误拉了中文字族")

if __name__ == "__main__":
    unittest.main()
