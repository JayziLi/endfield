# Endfield GUI 组件包

组件位于 `rhodes_fast/ui/`，基于 PySide6 Qt Widgets。当前阶段只提供视觉组件与组件展厅，不替换原有 `tkinter` GUI，也不触碰推理、视频输入或 KMBox 逻辑。

项目只依赖较轻的 `PySide6-Essentials`，没有引入 Qt WebEngine、Charts 或 Multimedia 等附加模块。

## 组件

- `Sidebar`：Logo、主导航项和当前页面状态；默认五项，也可传入精简页面集合。
- `TopBar`：产品标题、系统状态、启动与停止操作。
- `IndustrialButton`：工业黄主按钮、墨黑次按钮、幽灵按钮、危险按钮和导航按钮。
- `IndustrialIconButton`：用于刷新、导出和更多操作的方形工具按钮，必须提供无障碍名称。
- `IndustrialField`：模型路径、地址和数值输入，内置默认、激活、锁定、错误四种状态。
- `SegmentedControl`：TensorRT / CUDA / CPU 等互斥模式选择。
- `TabRail`：UDP / OBS / 本地文件等输入来源页签。
- `IndustrialSelect`：模型精度等有限选项的工业下拉框。
- `SectionPanel`：带编号、标题和英文副标题的通用分区容器。
- `MetricRail` / `MetricSpec`：用带唯一名称的指标定义和一条连续测量轨道呈现 FPS、延迟与 GPU，避免通用卡片墙。
- `MetricCard`：兼容需要独立趋势线的旧指标模块。
- `StatusBadge`、`DeviceStatusRow`：系统及设备状态。
- `TechSwitch`、`TechSlider`：无动画开关与精确数值滑块。
- `PreviewCanvas`：按需启用的帧显示、检测框、准星和 FOV 覆盖层。
- `GainCurve`：动态 P 增益响应曲线。
- `LogConsole`：有最大行数限制的运行日志。

## 运行组件展厅

```powershell
.\.venv\Scripts\python.exe -m rhodes_fast.ui.gallery
```

组件展厅仅用于设计验收。正式页面应把这些组件放入页面类，并通过控制器和 Qt 信号连接现有配置与后台进程。

当前组件板的设计参考保存在 `docs/ui-reference/`，代码实际渲染结果为 `docs/ui-component-gallery.png`。推荐组装顺序是：应用外壳（`Sidebar`）→ 页面结构（`SectionPanel` / `MetricRail`）→ 输入控制（`IndustrialField` / `SegmentedControl` / `TechSlider`）→ 实时数据（`PreviewCanvas` / `LogConsole`）。

## 性能约束

- 没有持续运行的装饰动画或定时器。
- 没有模糊、阴影、半透明叠层或视频背景。
- 常规界面严格使用近黑、纸白和工业黄；红色只用于错误或停止操作。
- 趋势图与曲线使用轻量 `QPainter`，只在数据变化且控件可见时刷新。
- 趋势线刷新上限为 10 FPS；它不会跟随推理帧率重绘。
- 单点 `push()` 和批量 `set_values()` 使用同一套趋势线限流逻辑，并合并为最多一个尾随刷新，保证最终值不丢失。
- `PreviewCanvas` 默认关闭，只有页面激活后才接收帧。
- `PreviewCanvas` 默认最多重绘 30 FPS，并始终保留最新帧；可通过 `set_max_fps()` 调整，传入 `0` 才表示不限制。
- 被限流的最新帧会合并到最多一个单次延迟刷新中；视频停止时最后一帧也不会遗漏。
- `PreviewCanvas.set_frame(..., copy=False)` 默认复用调用方持有的 `QImage`，避免每帧复制；复用外部可变缓冲时才传入 `copy=True`。
- 日志默认最多保留 250 行，避免长时间运行造成内存持续增长。
- 全局样式只在应用启动时应用一次。
