# WebView 界面重构设计

> 面向实施者：不需要参与过本次讨论，照本文即可动手。

**日期**：2026-09-17
**状态**：已确认，待写实施计划

## 1. 背景与目标

现在的界面是 `rhodes_fast/gui.py`（1975 行，tkinter/ttk）。交互设计没有问题，问题是难看。

用户在 Claude Design 上做了一整套设计系统 **Endfield UI**（项目 `b25c7c29-f9d6-4bf4-96dd-221d453ee3b6`），并且它是**照着本应用的截图**设计的——设计系统自己的 `readme.md` 写明来源之一是 `assets/reference/source-app-win32.png`，「The real Win32 app being restyled … Drove the UI kit's information architecture」。

**目标**：把界面换成这套设计系统的样子。**交互逻辑、信息架构一律不变。**

### 这套设计系统是什么

- 23 个 React 组件 + 一个程序化装饰层，全部 CSS 自定义属性 + 普通 CSS class
- **零图片、零 SVG 插画、零运行时依赖**。所有纹理、危险条纹、网格、括号、条码都是 CSS 渐变
- 8 个 token 文件（颜色/字体/排版/间距/材质/纹理/动效/基线）
- `ui_kits/vision-console/` 四屏参考实现，就是本应用的重制
- 四色：`--ink #101110` / `--paper #F1F1EB` / `--signal #EBFF00` / `--structure #BFC2BB`，加 `--alarm #D9201C`。**没有第二个色相**

### 定义外观的五条规则

1. **圆角恒为 0。** 「柔和」只来自右下角 45° 切角（`clip-path`），五档 4/6/10/14/22px
2. **边框是 inset ring 不是 border**（`box-shadow: inset 0 0 0 1px`），这样聚焦时加粗到 2px 不会重排，而且能跟着切角走
3. **信号黄是动词不是装饰。** 一个视图里只有一个黄块回答「系统现在在哪」
4. **材质是次感知的。** 凸起面 1px 顶部亮边 + 50% 高度淡出的 sheen；凹陷面 2–3px 内阴影；大面积暗底 3px 颗粒。永不超过底色的 10%，永不用投影
5. **装饰是程序化的，且永远 `aria-hidden`**

## 2. 技术路线：WebView

### 为什么不是原生控件

设计系统的 `readme.md` 结论：

> **This system is built for HTML.** The look depends on `clip-path` corner cuts, variable-weight display type, CSS masks for icons and blend-modes over video — all one-liners in CSS and all painful in WinForms/WPF/Qt. … If you must ship native, WPF is the only realistic host … **budget roughly 3× the effort.**

它说的 WPF 是 .NET；本项目是 Python，原生路线实际是 Qt/QSS，而 **QSS 没有 `clip-path`**——每个切角要自定义 `paintEvent` + `QPainterPath`，装饰层要手画 `QPainter`。比 WPF 更差。tkinter 主题化则完全做不到。

| | WebView | Qt/QSS | tkinter 换皮 |
|---|---|---|---|
| 视觉还原 | 100% | 60–70%，装饰层基本丢失 | 20%，等于没做 |
| 设计系统更新 | 换 CSS 文件 | 27 个组件手工同步 | — |

### 性能不是判据

GUI 和推理是**两个进程**：`gui.py:_launch()` 用 `subprocess.Popen` 起推理子进程，预览帧走 socket。`PreviewPublisher`（`pipeline.py:28`）在**子进程自己的工作线程**里渲染 + `cv2.imencode(".jpg")`，`max_fps=30` 节流。**GUI 用什么 toolkit 对瞄准延迟的影响是 0。**

反过来，WebView 的预览路径比现在**更轻**：现在 tk 主线程每帧要做 `cv2.cvtColor` + 缩放（`gui.py:123`）+ `Image.fromarray()` → `ImageTk.PhotoImage()`（`gui.py:1714`）；WebView 是把 JPEG 字节直接交给 `<img>`，解码和缩放由渲染引擎在自己的线程上做。

唯一真实耦合：GUI 和推理在同一台副机上抢 CPU。WebView 多出的渲染进程（~200MB）在这个场景里是噪声。

## 3. 架构

```
┌─ endfield-gui-next (Python 主进程) ────────────────┐
│                                                     │
│  pywebview 窗口 (frameless) ──> http://127.0.0.1:<0>│
│                                                     │
│  本地 HTTP server（只绑 127.0.0.1，端口 0 自动分配）│
│    ├─ /              静态文件：web/ 下的 html/css/js│
│    └─ /preview.mjpg  multipart/x-mixed-replace      │
│                                                     │
│  JS 桥 (pywebview.api)   ← 按钮/表单/预设，低频      │
│  gui_core                ← 跟 toolkit 无关的状态层  │
└─────────────────────────────────────────────────────┘
         │ subprocess.Popen（不变）
         ▼
    推理子进程 ──socket JPEG──> 原样转发进 MJPEG 流
```

**预览走 MJPEG，不走 JS 桥。** 子进程发来的 JPEG 字节**在 Python 侧一次都不解码**，直接塞进 `multipart/x-mixed-replace` 响应流，前端一行 `<img src="/preview.mjpg">`。走 `evaluate_js` 推 base64 的话每帧多一次字符串编码 + 序列化，30fps 下纯浪费。

控制类交互（启停、表单、预设、算法库）频率低，走 pywebview 的 JS 桥。日志一秒一行，从 Python 侧 `evaluate_js` 推。

**不用 React、不用 Babel、不用打包器。** `HANDOFF.md` 写明 `ef-*` 是 production-quality vanilla CSS，React 文件只是「reference implementations — markup + class names」。纯 HTML + class + 原生 JS，`.jsx` 当 markup 结构参考读。这对要离线跑、要走 pip 打包的桌面程序是决定性简化。

**双入口并存。** `endfield-gui` 保持 tkinter，新增 `endfield-gui-next`。两个都调 `gui_core`，逻辑只有一份。新界面没做完不影响使用，出问题随时退回。

依赖新增 `pywebview`（Windows 下带 `pythonnet` + `clr-loader`，都是 pip 装得上的小包）。渲染内核用系统自带的 **WebView2 Runtime**，不打包 Chromium。Win11 与 Win10 1803+ 预装；README 需注明老机器要装一次微软的独立安装器。

## 4. 文件结构

```
rhodes_fast/
  gui.py              旧 tkinter 界面 — 改成调 gui_core，其余不动
  gui_core/           新 · 跟 toolkit 无关，两个界面共用
    state.py          FormState + 它跟 AppConfig / Preset 的纯函数互转
    session.py        启停子进程、收日志、预览 socket、预设与算法库增删改
    prompts.py        「问用户」的回调协议（confirm / ask_text / notify）
  gui_web/            新 · WebView 界面
    app.py            pywebview 窗口 + JS API
    server.py         本地 HTTP：静态文件 + /preview.mjpg
    web/
      index.html
      app.js
      app.css         只放我们自己的布局与覆盖
      design/         设计系统原样拷贝
      fonts/          自托管的拉丁/等宽字体
```

## 5. 状态层怎么切

`RhodesFastGui` 现在揉了三样东西，按这个界切：

| 归属 | 内容 | 现在在哪 |
|---|---|---|
| `gui_core` | `FormState ↔ AppConfig`、预设读写、算法库装删改名、起停子进程、日志收集 | `_read_form` `_fill_form` `_store_preset` `_launch` `_install_candidate` … |
| tkinter 适配层 | 31 个 `tk.StringVar`/`DoubleVar`/`BooleanVar` ↔ `FormState`、`messagebox` | `_create_variables` 与各 `_*_changed` |
| WebView 适配层 | JSON ↔ `FormState`、前端弹窗 | 新写 |

绊脚的是 **31 处 `messagebox` + 2 处 `simpledialog`** 散在业务逻辑里。要抽成 `prompts.py` 的回调协议——tkinter 那边接 `messagebox`，WebView 那边接前端弹窗。不这么做 `gui_core` 就又跟 tk 绑上了。

**验收是硬的：这一步做完，旧界面行为一个像素不变，544 个测试全绿。**

## 6. 界面布局

### 应用外壳（四个面板共用）

```
┌─ TitleBar 44px ────────────────────────────────────────────────┐
│ // ENDFIELD VISION    v1.2.0 / 方案1·右键        ─  □  ×       │
├────────┬───────────────────────────────────────────────────────┤
│NavRail │  01  运行设置          预设 [默认 ▾] [保存][另存为][删除]│
│ 132px  │      RUNTIME SETTINGS                                  │
│        │ ──────────────────────────────────────────────────────│
│ ▮01 ⬚  │                                                        │
│  02 ✛  │        ← 只有这一列滚动 →                              │
│  03 ▤  │                                                        │
│  04 ▶  │                                                        │
│        │ ──────────────────────────────────────────────────────│
│        │  运行状态  RUNTIME LOG                      ← 常驻     │
│ ⚙ 设置  │ ──────────────────────────────────────────────────────│
│ ? 帮助  │  [启动系统 ACT-01]  [应用配置]  [保存设置]    ● RUNNING│
├────────┴───────────────────────────────────────────────────────┤
│ DEVICE CUDA:0 / RTX4070   MODEL yolov5n.onnx │ FPS 238  延迟 2.4│
└────────────────────────────────────────────────────────────────┘
```

对照现有结构，是一一搬过去的：

| 现在 | 新位置 |
|---|---|
| `header` 的「Endfield」+ 状态徽章（gui.py:267） | TitleBar |
| `header` 的启动/停止按钮（gui.py:277-280） | 底部动作行 |
| `preset_bar`：下拉 + 保存 / 另存为… / 删除（gui.py:284-294） | SectionHeader 右侧，四个控件全保留 |
| `notebook` 四个 tab（gui.py:303-306） | NavRail |
| `log_box` 运行状态（gui.py:558，挂在 root 上、四个 tab 之外） | 常驻 RuntimeLog，位置不变 |

预设条放 SectionHeader 右侧的理由：跟现在一样在顶部；它是跨面板的全局操作，跟 SectionHeader 同属外壳；TitleBar 只有 44px 且已有窗口按钮。

NavRail 四项：

| 索引 | 标题 | 拉丁 | 图标 |
|---|---|---|---|
| 01 | 运行设置 | RUNTIME SETTINGS | `box` |
| 02 | 识别与控制 | DETECTION & CONTROL | `crosshair` |
| 03 | 算法库 | ALGORITHM LIBRARY | `layout-grid` |
| 04 | 实时预览 | LIVE PREVIEW | `monitor-play` |

设计稿多画的「01 总览」屏**不做**——纯展示面板，不产生新操作。

### 01 运行设置

设计稿 `ui_kits/vision-console/ScreenRuntime.jsx` 可直接用，三个 Panel 跟现有三个 `ttk.LabelFrame` 名字一致（gui.py:344 / 374 / 409）。

```
┌ 模型  MODEL ───────────────────────── [READY] ┐
│ ONNX 模型   [D:/models/yolov5n.onnx    📁]    │  ← 📁 开原生对话框
│ 加速方式    [自动（推荐）              ▾]     │
│ ☑ 启用 CUDA Graph    ☑ 启用 GPU 预处理        │
└───────────────────────────────────────────────┘
┌ 画面输入  VIDEO INPUT ──────────────── [↻]   ┐
│ ┃UDP H.264┃ OBS WEBSOCKET │                  │  ← Tabs
│ 监听地址 [0.0.0.0    ]   端口   [4455  TCP]  │  ← grid 1.6fr / 1fr
│ 处理尺寸 [320 × 320  ]   缓冲帧 [2     FRM]  │
└───────────────────────────────────────────────┘
┌ KMBOX  DEVICE LINK ───────────────── [ ●━━ ] ┐  ← 开关在 Panel 标题栏右侧
│ 设备地址 [192.168.1.100]  端口 [8808      ]  │
│ UUID     [明文 Input   ]  心跳 [1000    MS]  │
└───────────────────────────────────────────────┘
```

### 02 识别与控制

设计稿 `ScreenDetection.jsx` 的两行网格，第二行换成本应用特有的两套控制方案。

```
┌ 模型输出 1.45fr ─────────┐ ┌ 系统状态 1fr ──────┐
│ 置信度  ━━━━●━━━  0.375  │ │ [●] 视频流正常      │
│ NMS IoU ━━━━━●━━  0.500  │ │ [●] 模型已加载      │
│ 输出格式 [自动  ▾]       │ │ [○] 设备已连接      │
└──────────────────────────┘ └─────────────────────┘
┌ 控制方案 1 ────── [●━━] ┐ ┌ 控制方案 2 ─ [ ━━○] ┐
│ 控制算法 [kalman ▾]     │ │ 控制算法 [feedfwd ▾]│
│  响应   ━━●━━━━  15     │ │  响应   ━━━●━━━  25 │
│  最大提前 ━━━●━  160px  │ │  ⋯ 动态生成         │
│ 目标标签 [person   ▾]   │ │ 目标标签 [person ▾] │
│ [导入调校…] [导出调校…] │ │ [导入调校…] [导出…] │
└─────────────────────────┘ └─────────────────────┘
```

「启用此方案」复选框（gui.py:451）挪到 Panel 标题栏右侧做 Toggle，照 KMBOX Panel 的 aside 模式；关掉时整块 45% 透明 + `pointer-events: none`。

算法参数是**动态生成**的（`_rebuild_algorithm_params`，按 `algorithm_param_specs(name)` 返回的 `Param` 列表建控件），新界面同样按 `Param` 的 `min/max/default/label` 生成 Slider。

### 03 算法库

设计系统没有这一屏（它照旧截图做的），用 `ef-*` 基础类拼表格。数据是 `_library_rows()` 的 6 元组：显示名 / 内部名 / 作者 / 源文件 / 导入时间 / 状态。

```
┌ 算法库  ALGORITHM LIBRARY ────── [导入算法…] [↻] ┐
│ 显示名        内部名      作者     源文件   状态  │
│ ─────────────────────────────────────────────────│
│ 比例控制      p          Endfield   —      内置   │
│▌卡尔曼弹道    kalman_pr  Endfield   —      内置   │ ← 选中：黄底
│ 我的算法      my_aim     Jayzi   my.py   已导入   │
│ ─────────────────────────────────────────────────│
│                  [查看源码] [改名…] [删除]        │
└──────────────────────────────────────────────────┘
```

选中行按规范是**整行信号黄填充 + 前缘 2px ink 条**（readme States 表：「Selection is a block, not an underline」）。

### 04 实时预览

设计稿 `ScreenPreview.jsx` 直接用。

```
┌ 预览叠加层  PREVIEW OVERLAY ─── [UDP|OBS] [📷] [⛶] ┐
│ ┌──────────────────────────────────────────────┐  │
│ │ ⌐         <img src="/preview.mjpg">        ¬ │  │
│ │ AIC              │                            │  │
│ │ ⌊  LATENCY 2.4ms  RES 640×360  FPS 238     ⌋ │  │
│ └──────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────┘
  延迟 2.4ms   目标数 3   丢帧 0.2%   带宽 18.6Mb/s   ← MetricRail
```

## 7. 组件映射

| 现在 | 用量 | 设计系统 |
|---|---|---|
| `ttk.Entry` | 13 | `Input` |
| `ttk.Combobox` | 8 | `Select` |
| `ttk.Checkbutton` | 8 | `Checkbox` |
| `ttk.Button` | 18 | `Button` / `IconButton` |
| `ttk.Scale` | 3 | `Slider` |
| `ttk.LabelFrame` | 7 | `Panel` + `SectionHeader` |
| `ttk.Notebook` | 1 | `NavRail` |
| `tk.Text`（日志） | 2 | `RuntimeLog` |
| 预览 canvas | 1 | `PreviewOverlay` |
| `filedialog` | 4 | pywebview `create_file_dialog()` |

`filedialog` 那条是升级：pywebview 开的是 **Windows 原生对话框，返回真实路径**，比 HTML `<input type=file>` 好——后者拿不到真实路径。

### 两个缺口（设计系统没给，用 `ef-*` 基础类自拼）

1. **表格** —— 算法库的 6 列列表，设计系统没有 Table 组件
2. **Modal** —— 33 处对话框要落地，组件清单里没有 Dialog。token 有（`--shadow-overlay`、modal scrim `rgba(16,17,16,.72)`），拼出来符合规范

## 8. 使用设计系统的纪律

- **`design/` 目录一个字节不改。** 要覆盖样式写在 `app.css` 里（它在 `design/styles.css` 之后加载）。这样设计系统更新时整个替换，不做三方合并
- **唯一例外：`design/tokens/fonts.css`。** 该文件整个只有一行 Google Fonts 的 `@import`，是全套 CSS 里唯一碰网络的地方——离线时未必快速失败，遇到需要认证的网络可能把样式表加载吊住。替换成本地 `@font-face`
- **信号黄纪律：内容区同时只有一个黄块**，即启动/停止按钮（它回答「系统在不在跑」）。NavRail 当前项的黄属于外壳，另算。其余按钮一律 secondary / outline

## 9. 已定的决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 技术路线 | WebView (pywebview) | 视觉 100% 还原；性能不是判据 |
| 窗口 | **frameless** | 设计系统的 TitleBar/StatusBar 就是为此而做；readme：「the app is frameless Windows software, so it must own its own caption bar and footer」 |
| 中文字体 | **系统字体（微软雅黑）** | 零体积、任何 Win 机器都有、不怕生僻字。日志/模型路径/报错里的中文是动态的，子集化会出豆腐块 |
| 拉丁/等宽字体 | 自托管子集 | Archivo / Archivo Black / Barlow / Barlow Condensed / IBM Plex Mono，只需拉丁字符，每个几十 KB。工业风的味道主要在这几个上面 |
| KMBox UUID | **明文显示** | 用户明确决定。与现状一致（gui.py:420 本来就是普通 `ttk.Entry`） |
| 检测框 | **继续烧在 JPEG 里** | 框已由子进程 `render_preview` 画进帧。改矢量框要改帧协议，收益不值。只用 `PreviewOverlay` 的外壳——角括号、垂直水印、telemetry 条、scanline |
| 「01 总览」屏 | 不做 | 纯展示，不产生新操作 |

字体覆盖点（`tokens/typography.css` 已确认的变量）：`--font-display` / `--font-ui` / `--font-condensed` / `--font-mono` / `--font-cjk`，五个里都写着 `"Noto Sans SC"` 兜底，在 `app.css` 里换成 `"Microsoft YaHei UI","Microsoft YaHei"` 即可。

## 10. 分阶段落地

| 阶段 | 做什么 | 做完能验证什么 |
|---|---|---|
| 0 | 设计系统拉进 `gui_web/web/design/` | — |
| 1 | **抽 `gui_core`**，旧 tkinter 改成调它 | ✅ **已完成** — 见 `docs/superpowers/plans/2026-09-17-gui-core-extraction.md`。667 测试全绿（原 544 + 新增 123），`gui_core` 导入时零 tkinter 依赖 |
| 2 | WebView 骨架：frameless 窗口 + 本地 HTTP + NavRail + 四个空屏 | 窗口能拖能最大化能贴边、设计系统渲染正确 |
| 3 | **实时预览屏** + MJPEG | 画面在动、帧率不掉 |
| 4 | 运行设置屏 + 原生文件对话框 + 启停 + RuntimeLog | 能真的跑起来一次推理 |
| 5 | 识别与控制屏：Slider / Toggle / 两套方案 / 算法参数动态生成 | 参数能调、能存 |
| 6 | 算法库屏：自拼表格 + Modal | 导入/改名/删除走通 |
| 7 | 切默认入口，旧界面降级为 `endfield-gui-classic` | — |

**两个刻意的顺序安排：**

**阶段 1 单独做完、单独提交。** 它是唯一碰现有代码的一步，风险全在这儿。对用户零可见变化，所以验收可以定得很硬。走稳了之后全是新增文件，删了重来不心疼。

**阶段 3（预览）排在所有业务屏前面。** 技术未知最多：MJPEG 穿过 pywebview 能不能跑满 30fps、frameless 窗口跟视频流一起会不会有合成问题。有坑要在写了 1 个屏时发现，不是 5 个屏之后。它也最独立——不依赖 `gui_core` 的任何表单逻辑。

阶段 2 里真正花时间的是 frameless 窗口管理（拖动、双击最大化、Windows 贴边分屏、多显示器 DPI），不是 CSS。按独立可验证交付处理。

## 11. 验收标准

- 阶段 1：`.venv/Scripts/python.exe -m unittest discover -s tests` 全绿（当前 544），旧界面手动跑一遍行为不变
- 阶段 3：预览稳定 30fps，无撕裂无累积延迟
- 阶段 4：新界面能完成一次完整的「配置 → 启动 → 出画面 → 停止」
- 全程：`gui_core` 有单元测试，不依赖 tk 也不依赖 pywebview

## 12. 明确不做

- **不改任何交互逻辑、不改信息架构。** 四个面板、字段分组、按钮含义全部照旧
- **不动推理子进程。** `pipeline.py` / `preview.py` / `kmbox_control.py` 一行不碰
- **不改帧协议。** 检测框继续烧录，不改成矢量
- **不做「01 总览」屏**
- **不在阶段 7 之前删除 tkinter 界面**

## 附：设计系统里值得先读的文件

| 路径 | 内容 |
|---|---|
| `readme.md` | 全系统规格：每张 token 表、几何规则、材质规则、do/don't |
| `HANDOFF.md` | 移植说明、五条外观规则、材质 token 表 |
| `styles.css` | 唯一入口，按序 `@import` 8 个 token + 2 个组件样式表 |
| `ui_kits/vision-console/` | 四屏参考实现 + `App.jsx` 外壳 |
| `components/**/*.d.ts` | 组件的权威 props 契约（读它，别从 JSX 推断） |
| `components/**/*.prompt.md` | 何时该用这个组件 |
| `guidelines/*.html` | 21 张单一规则样本卡 |

---

## 阶段 1 完成记录（2026-09-18）

`rhodes_fast/gui_core/` 已建成，955 行，四个模块：

| 模块 | 行数 | 内容 |
|---|---|---|
| `state.py` | 362 | `FormState` / `ProfileFormState` / `Labels` + 四个纯函数互转 |
| `session.py` | 481 | `GuiSession`：预设、算法库、子进程启停与日志 |
| `prompts.py` | 86 | `Prompter` 协议 + `RecordingPrompter` 测试替身 |
| `__init__.py` | 26 | 导出面 |

旧 tkinter 界面（`gui.py`，1889 行）改成了它的适配层，行为零可见变化。

### 实施中发现的、规格里没写的既有行为

这几条是从源码里挖出来的，第二份计划做 WebView 时必须一并搬过去，否则会丢功能：

1. **载入预设不用重启就生效。** `_apply_tuning_to_form` 末尾调 `_write_runtime_aim_settings()`，把 kp 和视野热推给正在跑的管线。它同时承担了方案七个字段的取值，不是纯粹的控件副作用。
2. **算法参数变量上挂着写入 trace**，每次改参数都热推给管线。`_rebuild_algorithm_params` 里的注释说明了挂载时机是调过的：挂早了会把半份配置推出去。
3. **删除一个正在被某套方案选中的算法**会当场把该方案改回比例控制并热推，删除确认的文案里承诺了这件事。
4. **算法库列表读注册表而不是已加载表**，这样刚导入的算法当场可见；而且列表刷新必须排在「导入成功」弹窗**之前**，否则用户隔着模态框看到一份没变的列表。
5. **`_restore_last_preset` 和 `_load_preset` 读预设的参数不同**（`require_model`、`known_algorithms`、报错方式三处），这是有意的：前者只拿来比对有没有改动，永远不填表单。抹平会导致引用未安装算法的预设每次开机报错且无法选中。
6. **两个破坏性确认框传 `icon=WARNING` + `default=CANCEL`**（覆盖预设、删除预设），算法库的三个确认框都不传。`Prompter.confirm` 的 `danger` 参数就是为前者存在的。
7. **子进程解释器取 `python.exe` 而不是 `sys.executable`**：界面可能跑在 `pythonw.exe` 下，那时起出来的子进程没有 stdout。
8. **停止是先写停止文件让管线自己收尾**（关 KMBox、落盘延迟日志），超时才硬杀。
9. **`current_preset` 必须在 `_save(quiet=True)` 之前赋值**，否则「下次打开还是这个预设」会丢。

### 留给第二份计划的已知债务

- **预览 socket 没有搬进 `gui_core`**：WebView 要把这些帧转成 MJPEG，形态跟 tkinter 完全不同，抽共用抽象只能靠猜。等有了真正的第二个消费者再设计。
- **`stop_requested` 仍在 `gui.py`**：它决定退出后显示「已停止」还是「运行出错」，WebView 适配层得复制一份，或者在第二份计划里一并收进 session。
- **`RhodesFastGui.algorithms_dir` 的 setter** 只为 `tests/test_gui_library.py` 存在（6 处直接写它来指向临时目录）。那套测试改成注入 session 之后即可删除。
- **`GuiSession._process` 跨线程无锁写**。现在安全是因为所有读者只做一次 `is None` 判断；将来加「读-改-写」式的状态要先补锁。
