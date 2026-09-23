"""表单状态: 界面上那些格子里装的东西, 跟用什么控件画无关。

FormState 里存的是「显示值」而不是「存储值」—— 下拉框里是「自动（推荐）」,
配置文件里是 "auto"。两者的映射表就在本模块下面 (PROVIDERS / OUTPUT_FORMATS /
INPUT_MODES / LOG_LANGUAGES / TRIGGERS), 由调用方打包成 Labels 传进来, 这样
本模块不需要知道界面的中文文案。

数值输入框一律存字符串, 因为用户能往里打任何东西; 解析放到
form_state_to_config, 让「输入非法 → 弹窗报错」这条路保持原样。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..aim_algorithms import available_algorithms
from ..aim_algorithms.contract import Param
from ..config import TRAIL_MAX_SECONDS, TRAIL_MIN_SECONDS, AimProfileConfig, AppConfig
from ..presets import Preset
from ..trail import TrailSettings


@dataclass(frozen=True, slots=True)
class Labels:
    """界面文案 ↔ 配置存储值的映射, 方向一律 {显示标签: 存储值}。

    绑成一个对象而不是散着传六个参数: 它们类型全是 dict[str, str], 散着传时
    provider 和 output_format 调了位置类型检查一声不吭, 症状是某个下拉框显示
    错内容 —— 而测试两边传的是同一份 dict, 照样绿。
    """

    provider: dict[str, str]
    output_format: dict[str, str]
    input_mode: dict[str, str]
    language: dict[str, str]
    trigger: dict[str, str]
    algorithm: dict[str, str]
    desktop_backend: dict[str, str]
    mouse_output: dict[str, str]


# 界面文案 ↔ 配置存储值。方向一律 {显示标签: 存储值}。
#
# 住在这里而不是某个界面模块里: tkinter 和 WebView 两个界面都要用同一份。各拿
# 一份副本的话, 将来加一个 provider 只会改到一边 —— 而另一边不会报错, 只是那个
# 选项不出现在下拉框里。
PROVIDERS = {"自动（推荐）": "auto", "TensorRT FP16": "tensorrt", "CUDA": "cuda", "CPU": "cpu"}
OUTPUT_FORMATS = {"YOLOv5": "yolov5", "YOLOv8": "yolov8", "端到端 NMS": "end2end"}
INPUT_MODES = {
    "UDP 视频流 (MPEG-TS/H.264)": "udp_video",
    "UDP 单包 JPEG": "udp_jpeg",
    "OBS WebSocket": "obs_websocket",
    "本机屏幕": "desktop",
}
# 两个都是 Windows 自带的采集接口, 由 DXcam 提供。推荐 DXGI: 实测「画面出现在屏幕上
# → 拿到数组」p50 1.6ms, 而且只有它的出帧时间可信 (WGC 的不可信, 延迟量不出来)。
# 见规格 2026-09-21-single-pc-capture-and-output-design.md 的实测记录。
DESKTOP_BACKENDS = {"DXGI 桌面复制（推荐）": "dxgi", "WGC（Windows.Graphics.Capture）": "winrt"}
MOUSE_OUTPUTS = {"KMBox": "kmbox", "本机 SendInput": "sendinput"}
LOG_LANGUAGES = {"中文": "zh", "English": "en"}
TRIGGERS = {
    "鼠标侧键 1": "side1",
    "鼠标侧键 2": "side2",
    "鼠标左键": "left",
    "鼠标右键": "right",
}


def display_path(path: Path, base_directory: Path) -> str:
    """模型路径在界面上的样子: 能相对就相对, 不能就绝对。

    跟 resolve_model_path 是一对。两个都住在这里而不是某个界面模块里 —— tkinter
    和 WebView 都要用同一套, 而且 config_to_form_state 本来就收它当参数。
    """
    try:
        return path.resolve().relative_to(base_directory.resolve()).as_posix()
    except ValueError:
        return str(path)


def resolve_model_path(path: str | Path, base_directory: Path) -> Path:
    """界面上的字符串 → 实际路径。相对路径按 settings.txt 所在目录算。"""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    return candidate.resolve()


def default_labels() -> Labels:
    """把全部映射打包成一个 Labels。

    algorithm 那张每次现取, 不缓存: 用户从算法库导入一个 .py 之后注册表就变了,
    缓存下来的话新算法的显示名查不到, 下拉框会退回第一个候选。
    """
    return Labels(
        provider=PROVIDERS,
        output_format=OUTPUT_FORMATS,
        input_mode=INPUT_MODES,
        language=LOG_LANGUAGES,
        trigger=TRIGGERS,
        algorithm=algorithm_choices(),
        desktop_backend=DESKTOP_BACKENDS,
        mouse_output=MOUSE_OUTPUTS,
    )


@dataclass
class ProfileFormState:
    """一套控制方案在界面上的样子。两套方案各绑一份。"""

    enabled: bool
    trigger: str
    target_class: str
    aim_position: float
    fov: float
    kp_min: float
    kp_max: float
    kp_growth: float
    algorithm: str
    algorithm_params: dict[str, float] = field(default_factory=dict)


@dataclass
class FormState:
    """整张表单。字段跟 gui.py:191-259 的 _create_variables 一一对应, 除了三类:

    滑条旁边的 *_text 是数值字段的格式化结果, 由适配层现算; preset_choice 和
    status 是界面装饰; _last_profile_triggers 是触发键冲突检测用的快照, 从
    profile_trigger 派生, 都不是用户填进来的东西。
    """

    model_path: str
    provider: str
    cuda_graph: bool
    gpu_preprocess: bool
    output_format: str
    confidence: float
    iou: float
    input_mode: str
    log_language: str
    udp_host: str
    udp_port: str
    udp_width: str
    udp_height: str
    obs_host: str
    obs_port: str
    obs_password: str
    obs_source: str
    kmbox_enabled: bool
    kmbox_host: str
    kmbox_port: str
    kmbox_uuid: str
    latency_log_enabled: bool
    preview_frame: bool
    trail_enabled: bool
    trail_optimal_path: bool
    trail_seconds: float
    profiles: tuple[ProfileFormState, ProfileFormState]
    # 单机模式的几个字段。None 的意思是「这个界面没有这些控件」: 旧的 tkinter
    # 界面自己一个字段一个字段地构造 FormState, 不传它们。form_state_to_config
    # 遇到 None 就保留 settings.txt 里的值 —— 否则在新界面里选了 SendInput,
    # 到旧界面点一下保存就悄悄变回 KMBox。
    desktop_backend: str | None = None
    desktop_monitor: str | None = None
    desktop_width: str | None = None
    desktop_height: str | None = None
    mouse_output: str | None = None


def _label(mapping: dict[str, str], stored: str) -> str:
    """存储值 → 显示标签。逐字等价于 gui.py:73 的 _display_value。

    兜底是「第一个标签」不是 stored 本身 —— 这条容易写错, 而且后果是可见的。
    settings.txt 指着一个已删掉的算法是真实场景 (gui.py:101 那句注释
    「配置里指着一个已删掉的算法时界面仍要画得出来」就是为它写的), 这时返回
    stored 会把一个候选之外的字符串塞进下拉框。
    """
    return next((label for label, value in mapping.items() if value == stored), next(iter(mapping)))


def _param_defaults(algorithm: str) -> dict[str, float]:
    """算法标识 → {参数名: 默认值}。

    直接读注册表, 不走注入: aim_algorithms 不碰 tkinter, gui_core 可以直接
    import 它, 而注入版唯一的实现就是它自己 —— 那不叫可测性, 叫多一个参数。
    照 gui.py:101 的做法, 算法不存在时返回空而不是抛异常。
    """
    algorithm_type = available_algorithms().get(algorithm)
    return (
        {} if algorithm_type is None else {spec.name: spec.default for spec in algorithm_type.PARAMS}
    )


def algorithm_choices() -> dict[str, str]:
    """界面显示名 -> 算法标识。和 Labels 里那几张映射同一个方向。

    每次现取, 不缓存: 用户从算法库导入一个 .py 之后注册表就变了, 缓存住的话
    新算法要重启才看得见。
    """
    return {
        algorithm.DISPLAY_NAME: name
        for name, algorithm in sorted(available_algorithms().items())
    }


def algorithm_param_specs(name: str) -> tuple[Param, ...]:
    """按算法**标识**取参数契约, 不是按显示标签。

    配置里指着一个已删掉的算法时界面仍要画得出来, 所以不抛异常, 返回空元组。
    """
    algorithm = available_algorithms().get(name)
    return algorithm.PARAMS if algorithm is not None else ()


def runtime_aim_payload(state: FormState, labels: Labels) -> dict:
    """热推给正在跑的管线的那份瞄准设置。纯函数: 不碰文件, 不看进程在不在跑。

    从 gui.py 的 _write_runtime_aim_settings 搬过来的, 两边共用一份。留两份
    副本的话, 将来给算法加一个字段只改一边 —— 症状是「新界面调参数不生效」,
    而且不报任何错。
    """
    profiles = [
        {
            "enabled": profile.enabled,
            "trigger": labels.trigger[profile.trigger],
            "kp_min": profile.kp_min,
            "kp_max": profile.kp_max,
            "kp_growth": profile.kp_growth,
            "target_class": int(profile.target_class),
            # 表单上是 0-100 的百分比, 协议里是 0-1 的比例。漏了这一步准心会瞄到
            # 框外面去, 而 38 和 0.38 都是「看着挺合理」的数。
            "target_y_ratio": max(0.0, min(1.0, profile.aim_position / 100.0)),
            "fov_radius": profile.fov,
            "algorithm": labels.algorithm[profile.algorithm],
            "algorithm_params": dict(profile.algorithm_params),
        }
        for profile in state.profiles
    ]
    return {
        # 顶层这三个是给老的读取方看的 (gui.py 原注释: Top-level keys mirror
        # profile 1 for older runtime consumers)。去掉就等于悄悄砍了向后兼容,
        # 而且只有老版本的管线才看得出来。
        "target_class": profiles[0]["target_class"],
        "target_y_ratio": profiles[0]["target_y_ratio"],
        "fov_radius": profiles[0]["fov_radius"],
        "profiles": profiles,
    }


def _profile_from_config(profile: AimProfileConfig, labels: Labels) -> ProfileFormState:
    # 按存储的算法名取参数契约, 不按显示标签: 算法没了的时候标签会退回第一个,
    # 但参数得跟着配置里真正写的那个走, 这是 gui.py:248-256 的做法。
    defaults = _param_defaults(profile.algorithm)
    return ProfileFormState(
        enabled=profile.enabled,
        trigger=_label(labels.trigger, profile.trigger),
        target_class=str(profile.target_class),
        aim_position=profile.target_y_ratio * 100.0,
        fov=profile.fov_radius,
        kp_min=profile.kp_min,
        kp_max=profile.kp_max,
        kp_growth=profile.kp_growth,
        algorithm=_label(labels.algorithm, profile.algorithm),
        algorithm_params={
            name: profile.algorithm_params.get(name, default) for name, default in defaults.items()
        },
    )


def config_to_form_state(
    config: AppConfig,
    config_dir: Path,
    labels: Labels,
    *,
    display_path: Callable[[Path, Path], str],
) -> FormState:
    """AppConfig → FormState。对应 gui.py:191-259 的 _create_variables。"""
    trail_defaults = TrailSettings()
    return FormState(
        model_path=display_path(config.model.path, config_dir),
        provider=_label(labels.provider, config.model.provider),
        cuda_graph=config.model.cuda_graph,
        gpu_preprocess=config.model.gpu_preprocess,
        output_format=_label(labels.output_format, config.model.output_format),
        confidence=config.model.confidence,
        iou=config.model.iou,
        input_mode=_label(labels.input_mode, config.input.mode),
        log_language=_label(labels.language, config.ui.language),
        udp_host=config.udp.host,
        udp_port=str(config.udp.port),
        udp_width=str(config.udp.width),
        udp_height=str(config.udp.height),
        obs_host=config.obs.host,
        obs_port=str(config.obs.port),
        obs_password=config.obs.password,
        obs_source=config.obs.source_name,
        kmbox_enabled=config.kmbox.enabled,
        kmbox_host=config.kmbox.host,
        kmbox_port=str(config.kmbox.port),
        kmbox_uuid=config.kmbox.uuid,
        # 延迟日志是一次性的运行开关, 不从配置恢复。对应 gui.py:213。
        latency_log_enabled=False,
        # 预览页的勾选框每次打开都回到默认, 只有轨迹长度记住。对应 gui.py:214-219。
        preview_frame=trail_defaults.show_frame,
        trail_enabled=trail_defaults.enabled,
        trail_optimal_path=trail_defaults.optimal_path,
        trail_seconds=config.ui.trail_seconds,
        profiles=tuple(_profile_from_config(profile, labels) for profile in config.aim_profiles),
        desktop_backend=_label(labels.desktop_backend, config.desktop.backend),
        desktop_monitor=str(config.desktop.monitor),
        desktop_width=str(config.desktop.width),
        desktop_height=str(config.desktop.height),
        mouse_output=_label(labels.mouse_output, config.mouse.output),
    )


def trail_settings_from_state(state: FormState) -> TrailSettings:
    """对应 gui.py:1463-1475 的 _current_trail_settings。

    tk 的 DoubleVar 装了非数字时 .get() 会抛 TclError, 那个 except 分支是 tk
    特有的, 留在适配层; 到这里 trail_seconds 已经是个 float 了。
    """
    # 滑条是连续的, 存 0.1 秒一档: 设置文件里不该出现 1.2749 这种数。
    seconds = round(min(TRAIL_MAX_SECONDS, max(TRAIL_MIN_SECONDS, state.trail_seconds)) * 10) / 10
    return TrailSettings(
        show_frame=bool(state.preview_frame),
        enabled=bool(state.trail_enabled),
        seconds=seconds,
        optimal_path=bool(state.trail_optimal_path),
    )


def _profile_to_config(
    profile: ProfileFormState, base: AimProfileConfig, labels: Labels
) -> AimProfileConfig:
    """一套方案的表单值 → 配置。基准是同一套方案的旧配置, 不是默认值:
    界面上没有的字段要原样留着。
    """
    return replace(
        base,
        enabled=profile.enabled,
        trigger=labels.trigger[profile.trigger],
        kp_min=profile.kp_min,
        kp_max=profile.kp_max,
        kp_growth=profile.kp_growth,
        target_class=int(profile.target_class),
        # 界面上是 0..100 的百分比滑条, 配置里是 0..1 的比例。夹紧是因为滑条
        # 的范围将来可能改, 而越界的比例会让瞄准点跑到框外面去。
        target_y_ratio=max(0.0, min(1.0, profile.aim_position / 100.0)),
        fov_radius=profile.fov,
        algorithm=labels.algorithm[profile.algorithm],
        # 拷一份: AppConfig 是 frozen 的, 但这个 dict 不是。直接塞进去的话
        # 用户之后动一下参数滑条就会改到已经保存的那份配置。
        algorithm_params=dict(profile.algorithm_params),
    )


def form_state_to_config(
    state: FormState,
    base: AppConfig,
    config_dir: Path,
    labels: Labels,
    *,
    current_preset: str | None,
    resolve_model_path: Callable[[str | Path, Path], Path],
) -> AppConfig:
    """FormState → AppConfig。对应 gui.py:1359-1449 的 _read_form。

    base 是「现在这份配置」: 界面上没有的字段 (aim 段的 smoothing/deadzone/
    max_step 等) 全靠它继承, 所以每个段都是在 base 的对应段上 replace, 而不是
    新造一个。

    数值解析在这里发生, 非法输入抛 ValueError —— 调用方接住它弹窗, 这正是
    现在的行为 (gui.py:1451-1458)。
    """
    model = replace(
        base.model,
        # 从资源管理器拖进来的路径两头常带空格, 而 Windows 不会替我们剪掉:
        # "C:/app/  MODEL" 是个合法的、另外的目录。
        path=resolve_model_path(state.model_path.strip(), config_dir),
        provider=labels.provider[state.provider],
        cuda_graph=state.cuda_graph,
        gpu_preprocess=state.gpu_preprocess,
        output_format=labels.output_format[state.output_format],
        # 界面上没有这个选项, 每次保存都钉回 auto: 旧设置文件里遗留的布局跟
        # 新选的输出格式对不上时, 让探测去决定比让它沿用错值强。
        output_layout="auto",
        confidence=state.confidence,
        iou=state.iou,
    )
    udp = replace(
        base.udp,
        host=state.udp_host.strip(),
        port=int(state.udp_port),
        width=int(state.udp_width),
        height=int(state.udp_height),
    )
    obs = replace(
        base.obs,
        host=state.obs_host.strip(),
        port=int(state.obs_port),
        # 密码不 strip: 空格可能是密码的一部分, 剪掉的话用户只会看到连不上,
        # 看不出为什么。
        password=state.obs_password,
        source_name=state.obs_source.strip(),
    )
    kmbox = replace(
        base.kmbox,
        enabled=state.kmbox_enabled,
        host=state.kmbox_host.strip(),
        # 盒子的 UUID 印在机身上是大写的, 用户照着抄容易打成小写。
        uuid=state.kmbox_uuid.strip().upper(),
        port=int(state.kmbox_port),
    )
    desktop = replace(
        base.desktop,
        backend=(
            base.desktop.backend
            if state.desktop_backend is None
            else labels.desktop_backend[state.desktop_backend]
        ),
        monitor=base.desktop.monitor if state.desktop_monitor is None else int(state.desktop_monitor),
        width=base.desktop.width if state.desktop_width is None else int(state.desktop_width),
        height=base.desktop.height if state.desktop_height is None else int(state.desktop_height),
    )
    mouse = replace(
        base.mouse,
        output=base.mouse.output if state.mouse_output is None else labels.mouse_output[state.mouse_output],
    )
    first = state.profiles[0]
    aim = replace(
        base.aim,
        # 跟方案 1 同步: 旧配置消费者和 pipeline benchmark 还在读 aim 段。
        target_class=int(first.target_class),
        target_y_ratio=max(0.0, min(1.0, first.aim_position / 100.0)),
        fov_radius=first.fov,
    )
    trail = trail_settings_from_state(state)
    return replace(
        base,
        input=replace(base.input, mode=labels.input_mode[state.input_mode]),
        ui=replace(
            base.ui,
            language=labels.language[state.log_language],
            preset=current_preset or "",
            # 存夹紧取整之后的秒数, 不是滑条上的原始值。
            trail_seconds=trail.seconds,
        ),
        udp=udp,
        obs=obs,
        model=model,
        kmbox=kmbox,
        desktop=desktop,
        mouse=mouse,
        aim=aim,
        aim_profile_1=_profile_to_config(state.profiles[0], base.aim_profile_1, labels),
        aim_profile_2=_profile_to_config(state.profiles[1], base.aim_profile_2, labels),
    )


def apply_preset_to_state(
    state: FormState,
    preset: Preset,
    config_dir: Path,
    labels: Labels,
    *,
    display_path: Callable[[Path, Path], str],
) -> None:
    """把预设灌进 state。对应 gui.py:1129-1186 的 _fill_form 的取值部分。

    原地改而不是返回新的一份: 适配层手里那个 FormState 就是要被写回控件的那个,
    换成新对象的话调用方很容易写回旧的那份。

    只动预设真的带着的字段。预设里没有 ui 段, 所以日志语言、轨迹长度、当前预设名
    原样留着 —— 载入别人给的预设不该顺手把界面语言换掉。预览页那三个勾选框和延迟
    日志同理, 它们是运行期开关, 不是配置。

    控件副作用留在 gui.py 的适配层, 由它在本函数返回后照 _fill_form 的原顺序调用:
    _sync_cuda_graph_control、_switch_input_panel、按算法重建参数控件、刷新
    _last_profile_triggers 快照, 以及最后那个二选一的
    _inspect_selected_model / _update_target_class_choices。

    注意 gui.py:1142 的 previous_model: 它在写新路径之前读旧路径, 用来决定最后
    要不要重新探测模型。本函数一进来就把 state.model_path 盖掉了, 所以适配层必须
    在调用本函数之前把旧路径取出来 —— 之后再取只会拿到预设的新路径, 于是
    previous_model == model.path 恒成立, 模型永远不再重新探测, 新模型的类别数
    出不来, 合法的目标标签会被旧契约判成越界。
    """
    model = preset.model
    state.model_path = display_path(model.path, config_dir)
    state.provider = _label(labels.provider, model.provider)
    state.cuda_graph = model.cuda_graph
    state.gpu_preprocess = model.gpu_preprocess
    state.output_format = _label(labels.output_format, model.output_format)
    state.confidence = model.confidence
    state.iou = model.iou
    state.input_mode = _label(labels.input_mode, preset.input.mode)
    state.udp_host = preset.udp.host
    state.udp_port = str(preset.udp.port)
    state.udp_width = str(preset.udp.width)
    state.udp_height = str(preset.udp.height)
    state.obs_host = preset.obs.host
    state.obs_port = str(preset.obs.port)
    # 密码和 UUID 都原样填。strip / upper 是保存那一侧的事 (form_state_to_config);
    # 在这里也做一遍, 界面上显示的就跟预设文件里存的对不上了。
    state.obs_password = preset.obs.password
    state.obs_source = preset.obs.source_name
    state.kmbox_enabled = preset.kmbox.enabled
    state.kmbox_host = preset.kmbox.host
    state.kmbox_port = str(preset.kmbox.port)
    state.kmbox_uuid = preset.kmbox.uuid
    state.desktop_backend = _label(labels.desktop_backend, preset.desktop.backend)
    state.desktop_monitor = str(preset.desktop.monitor)
    state.desktop_width = str(preset.desktop.width)
    state.desktop_height = str(preset.desktop.height)
    state.mouse_output = _label(labels.mouse_output, preset.mouse.output)
    # 整个换掉而不是逐字段改: _profile_from_config 每次按新算法的契约重建
    # algorithm_params, 这正是 gui.py:1166 那行 `= {}` 要的效果 —— 预设文件里
    # 没写的参数回到默认, 不沿用上一个预设留下的值。
    state.profiles = tuple(_profile_from_config(profile, labels) for profile in preset.aim_profiles)
