"""让运动看起来像人手的那几个算法。

现有算法都是「误差 x 动态 kp」: 路径笔直, 越近越慢地指数收敛。观感很机械。

这里的实现全部是**增量式**的 —— 每帧重新看误差, 只决定这一帧走多少, 绝不预先
规划整条路径。浏览器自动化那边流行的贝塞尔做法(ghost-cursor 等)是先把 A 到 B
的轨迹算完再走, 对静止的按钮完全合理; 但我们的目标 482px/s 在动且永不结束,
预规划有两个致命问题: 一是开环, 提交 N 帧路径就是 N 帧不看画面, 正是这个项目
一直在打的死区延迟; 二是规划时的终点本来就是 7 帧前的位置, 走完弧线目标早就
不在那儿了。

框架继续负责死区、EMA 平滑、亚像素累加、限幅、发送, 这里一项都不碰。
"""

from __future__ import annotations

import math
import random
from typing import Mapping

from .builtin import Feedforward, _landed_command, _smoothed_target_velocity
from .contract import Observation, Param, dynamic_kp

# 风每帧先除以这个数再补随机量。相邻两帧因此相关(理论上 1/sqrt(3) ≈ 0.577),
# 才称得上「风」; 每帧独立取随机数只是抖动, 路径不会弯, 只会毛糙。
_WIND_DECAY = math.sqrt(3.0)
# 原版给新注入的随机量除以 sqrt(5)。保留原比例, 免得照抄参数时手感对不上。
_WIND_SPREAD = math.sqrt(5.0)
# lead_frames 用的速度平滑系数。不做成参数: WindMouse 的前馈是个附加选项,
# 再多一个旋钮不值得, 而 0.25 是 feedforward 那边实测下来好用的值。
_LEAD_SMOOTHING = 0.25
# 观测距离比这一次拉枪的起点还远这么多倍, 就当成新的一次拉枪, 重新定起点和弧形。
# 留出余量是因为检测噪声(实测 σ=0.8px)会让距离偶尔小幅回升, 不能一回升就重置。
_NEW_MOVE = 1.2


def _make_rng(seed: float) -> random.Random:
    """seed 为 0 表示每次不同。参数只能是 float, 所以用 0 当这个哨兵。"""
    return random.Random(int(seed)) if seed else random.Random()


class FeedforwardWind(Feedforward):
    """前馈 + 风。

    基类挑的是 feedforward: 仿真和实机都确认它是现有算法里最好的一个, 在它上面
    叠效果才能分清「风」的贡献和「基础算法」的贡献。

    风垂直于误差方向。沿误差方向加只是在「快一点慢一点」上撒噪声 —— 路径不会弯,
    白掉精度还换不来观感。
    """

    NAME = "feedforward_wind"
    DISPLAY_NAME = "前馈 + 风（人味）"
    PARAMS: tuple[Param, ...] = Feedforward.PARAMS + (
        Param("wind_strength", 0.35, 0.0, 1.5, "风力强度"),
        # 0 = 不归零: 风到了目标跟前也是满的。跟踪精度会掉, 但那是用户的取舍,
        # 而不是一个拖不到底的滑条该替他做的决定。除零由下面的 max(1e-6, ...) 挡。
        Param("wind_decay_px", 40.0, 0.0, 200.0, "风力归零距离（像素，0=不归零）"),
        Param("seed", 0.0, 0.0, 999999.0, "随机种子（0=每次不同）"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        # 必须在 super().__init__ 之前: 基类的构造函数里会调 reset(), 而 reset()
        # 已经被这里重写, 会用到下面这几个字段。
        self._wind_strength = params["wind_strength"]
        self._wind_scale = max(1e-6, params["wind_decay_px"])
        self._seed = params["seed"]
        super().__init__(params)

    def reset(self) -> None:
        super().reset()
        self._wind = 0.0
        self._rng = _make_rng(self._seed)

    def compute(self, observation: Observation) -> tuple[float, float]:
        step_x, step_y = super().compute(observation)
        if self._wind_strength <= 0.0:
            # 关着的时候必须和 feedforward 逐位相同。悄悄改掉现有手感是最难查的
            # 那种坏法: 人会以为是自己参数调错了。
            return step_x, step_y
        distance = math.hypot(observation.error_x, observation.error_y)
        self._wind /= _WIND_DECAY
        self._wind += self._rng.uniform(-1.0, 1.0)
        if distance < 1e-9:
            return step_x, step_y
        # 近了归零(用户要求: 跟踪精度不许受影响)。和 dynamic_kp 的增长项同一个形状 ——
        # 注意方向: 距离为 0 时这一项是 0, 写成 exp(-d/s) 就正好反了。
        #
        # WindMouse 原版还有一道「离目标近了就不再注入新随机量」的闸门, 这里故意
        # 不要: 那道闸门会把这一项挤成测不出效果的冗余(闸门一关, 风 15 帧就衰减到
        # 千分之一, 剩下的活没了)。两套机制干一件事、其中一套还量不出来, 比一套
        # 清晰的机制差。只留这一项, 过渡也从「阈值切换」变成平滑淡出。
        growth = 1.0 - math.exp(-distance / self._wind_scale)
        lateral = self._wind * self._wind_strength * growth * math.hypot(step_x, step_y)
        return (
            step_x - observation.error_y / distance * lateral,
            step_y + observation.error_x / distance * lateral,
        )


class WindMouse:
    """WindMouse 弹道。

    光标当成有惯性的质点, 受两个力: 重力(大小恒定, 指向目标)和风(随机方向, 平滑
    变化)。速度是累积的, 到了近处收紧速度上限。

    和现有算法的本质区别在**归一化重力**: 大小恒为 gravity, 不随距离变。所以它是
    「匀速冲过去再刹一脚」——人手的弹道+修正两阶段, 而不是越近越慢的指数收敛。
    机械感的根源正是后者。

    对原版做了三处改动, 都是因为原版假设「一次移动、到了就结束」, 而我们连续运行:

    1. **速度上限按距离算, 不再是可变状态。** 原版把 M_0 一路除以 sqrt(5) 永久调小,
       对一次性移动没问题; 我们跑不完, 那样最终会把自己冻住。
    2. **去掉末端 3 像素下限。** 那个下限在原版里是为了保证离散循环能终止, 我们要的
       不是终止而是稳定 —— 留着它会在 ±3 像素上永久振荡。去掉后末端退化成比例收敛,
       而近距离本来就该是比例控制说话, 弹道那一段的价值在远处。
    3. **去掉速度截断里的那次随机。** 原版 v_clip 在 50%~100% 上限之间随机。241fps 下
       逐帧的速度抖动快到读不出「人味」(人的速度起伏在 100 毫秒即 24 帧的尺度上),
       而风本身是时间相关的, 那份可读的起伏已经由它提供。去掉之后 wind 成为唯一的
       随机来源, wind=0 的轨迹完全确定 —— 否则没法把风的贡献单独隔离出来。
    """

    NAME = "windmouse"
    DISPLAY_NAME = "WindMouse 弹道"
    # 默认值是在 241fps + 7 帧延迟下标定出来的, 不是原版那一套(G_0=9, W_0=3,
    # M_0=15, D_0=12)。原版的单位是「每步」, 而它假定的步频远低于 241fps: 直接照搬
    # 实测跟随均值 79px、在靶率 0%, 完全不可用。
    #
    # max_step 是主导参数, 而它的合理量级有个很直白的来源: 241fps x 3px/帧 = 723px/s,
    # 和目标本身的横移速度(实测 482px/s)同一个数量级。原版的 15px/帧 相当于 3600px/s。
    #
    # gravity 在 4 附近就到平台(2.0 → 44%, 32.0 → 45%)。原因值得记一下: gravity 越大,
    # 速度越快饱和到上限, 也就是**惯性越不起作用**。换句话说在有死区延迟的闭环里,
    # WindMouse 的动量项是负担, 最好的配置恰恰是让它几乎不起作用的那个。
    #
    # lead_frames 默认给 8 而不是 0(纯 WindMouse): 0 的收敛要 249ms 而 8 只要 138ms,
    # 拿一个明显更差的默认值去代表这个算法会误导手感判断。想试纯版本就填 0。
    PARAMS: tuple[Param, ...] = (
        # 三个下限都放到 0。前两个到 0 会让这个算法不再瞄准 (重力 0 = 没有指向
        # 目标的力, 只剩风在乱推; 速度上限 0 = 一帧都不动), 留着能拧是因为「纯风
        # 长什么样」本来就是这个算法想让人亲手感受的事 —— commit_frames 那个旋钮
        # 也是同一个道理。滑条旁边有读数, 拧到 0 看得见。
        Param("gravity", 4.0, 0.0, 40.0, "重力（像素/帧²）"),
        Param("wind", 2.0, 0.0, 20.0, "风力"),
        Param("max_step", 3.0, 0.0, 15.0, "单帧速度上限（像素）"),
        Param("damp_px", 25.0, 0.0, 100.0, "末端阻尼距离（像素，0=不阻尼）"),
        Param("lead_frames", 8.0, 0.0, 30.0, "速度前馈帧数（0=纯 WindMouse）"),
        Param("seed", 0.0, 0.0, 999999.0, "随机种子（0=每次不同）"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        self._gravity = params["gravity"]
        self._wind_magnitude = params["wind"]
        self._max_step = params["max_step"]
        self._damp = max(1e-6, params["damp_px"])
        self._lag = max(0, int(round(params["lead_frames"])))
        self._seed = params["seed"]
        self.reset()

    def reset(self) -> None:
        self._vx = 0.0
        self._vy = 0.0
        self._wind_x = 0.0
        self._wind_y = 0.0
        self._previous: tuple[float, float] | None = None
        self._velocity_x = 0.0
        self._velocity_y = 0.0
        self._rng = _make_rng(self._seed)

    def _lead(self, observation: Observation) -> tuple[float, float]:
        if self._lag <= 0:
            return 0.0, 0.0
        # 和 Feedforward 同一套: 把刚生效的那条指令加回去, 剩下的才是目标自己的位移。
        # 直接复用 _landed_command 而不是再写一遍 —— 那个窗口的下标很容易写错, 而且
        # 一旦两份实现分叉, 症状只是「手感怪」, 几乎查不出来。
        landed_x, landed_y = _landed_command(observation.recent_commands, self._lag)
        self._velocity_x, self._velocity_y = _smoothed_target_velocity(
            self._previous,
            (observation.error_x, observation.error_y),
            (landed_x, landed_y),
            (self._velocity_x, self._velocity_y),
            _LEAD_SMOOTHING,
        )
        self._previous = (observation.error_x, observation.error_y)
        return self._velocity_x * self._lag, self._velocity_y * self._lag

    def compute(self, observation: Observation) -> tuple[float, float]:
        lead_x, lead_y = self._lead(observation)
        aim_x = observation.error_x + lead_x
        aim_y = observation.error_y + lead_y
        distance = math.hypot(aim_x, aim_y)
        if distance < 1e-9:
            return 0.0, 0.0
        if self._wind_magnitude > 0.0:
            # min(风力, 距离) 是原版的写法: 越近风越小, 到了自然归零。
            magnitude = min(self._wind_magnitude, distance) / _WIND_SPREAD
            self._wind_x = self._wind_x / _WIND_DECAY + self._rng.uniform(-1.0, 1.0) * magnitude
            self._wind_y = self._wind_y / _WIND_DECAY + self._rng.uniform(-1.0, 1.0) * magnitude
        # 归一化重力: 除以 distance 之后这一项的大小恒为 gravity。
        self._vx += self._wind_x + self._gravity * aim_x / distance
        self._vy += self._wind_y + self._gravity * aim_y / distance
        speed = math.hypot(self._vx, self._vy)
        limit = (
            self._max_step
            if distance >= self._damp
            else self._max_step * distance / self._damp
        )
        if speed > limit:
            self._vx = self._vx / speed * limit
            self._vy = self._vy / speed * limit
        return self._vx, self._vy


def _bezier(aim_x: float, aim_y: float, offset: float, t: float) -> tuple[float, float]:
    """三次贝塞尔在 t 处的点。P0=原点, P3=aim, 两个控制点横向偏 offset。

    控制点沿轴取在 aim/3 和 aim*2/3 是刻意的 —— 这样 offset=0 时

        3(1-t)²t·(1/3) + 3(1-t)t²·(2/3) + t³ = t[(1-t)+t]² = t

    也就是 B(t) 代数上恒等于 t·aim, 完全退化成直线比例步长。所以这条弧线是**纯横向**
    的叠加: 沿轴的速度曲线和 feedforward 一模一样, 加的只是弯, 不是快慢。

    横向分量同理收成 3(1-t)²t·o + 3(1-t)t²·o = 3t(1-t)·o。它在 t∈[0,1] 上恒不换号,
    所以弧线只往一侧鼓、鼓出去再回来, 绝不穿到另一侧 —— 这正是 ghost-cursor 强调的
    「控制点只取一侧」, 两侧随机取会出扭曲的 S 形怪曲线。
    """
    distance = math.hypot(aim_x, aim_y)
    lateral = 3.0 * t * (1.0 - t) * offset
    return (
        aim_x * t - aim_y / distance * lateral,
        aim_y * t + aim_x / distance * lateral,
    )


class FeedforwardBezier(Feedforward):
    """前馈 + 贝塞尔弧线。

    浏览器自动化那边(ghost-cursor 等)的做法是先把 A 到 B 的整条路径算完再走。对静止
    的按钮完全合理, 对我们是灾难: 一是开环, 提交 N 帧路径就是 N 帧不看画面, 正是这个
    项目一直在打的死区延迟; 二是规划时的终点本来就是 7 帧前的位置, 走完弧线目标早就
    不在那儿了。

    所以这里把那个取舍做成旋钮:

    - ``commit_frames = 0``(默认): 终点每帧重取, 只走弧线上的第一步。仍是闭环, 不加
      死区。**实话说, 这种模式下它和 feedforward_wind 在数学上非常接近** —— 小 t 处
      横向量 ≈ 3t·offset, 也就是一个正比于 kp 的横向速度。视觉上都是弧线, 区别只在
      是否预先承诺。
    - ``commit_frames = N > 0``: 终点和弧形一起冻结 N 帧, 沿冻结的曲线走。这才是真正
      的 ghost-cursor 行为, 开环。做成旋钮是为了让那个代价能被亲手感受到, 而不是只能
      听我说。
    """

    NAME = "feedforward_bezier"
    DISPLAY_NAME = "前馈 + 贝塞尔弧线"
    PARAMS: tuple[Param, ...] = Feedforward.PARAMS + (
        Param("arc_strength", 0.25, 0.0, 1.0, "弧线幅度（占距离比例）"),
        Param("commit_frames", 0.0, 0.0, 15.0, "承诺帧数（0=每帧重规划）"),
        Param("seed", 0.0, 0.0, 999999.0, "随机种子（0=每次不同）"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        self._arc_strength = params["arc_strength"]
        self._commit = max(0, int(round(params["commit_frames"])))
        self._seed = params["seed"]
        super().__init__(params)

    def reset(self) -> None:
        super().reset()
        self._lateral = 0.0
        self._span = 0.0
        self._axis = (1.0, 0.0)
        self._offset = 0.0
        self._plan: tuple[float, float, float, float] | None = None
        self._plan_left = 0
        self._travelled = 0.0
        self._rng = _make_rng(self._seed)

    def _next_offset(self, distance: float) -> float:
        # 和风同一套演化: 横向偏移跨帧平滑变化。每帧独立取随机数的话弧形每帧换向,
        # 那是抖动不是弧线。
        self._lateral = self._lateral / _WIND_DECAY + self._rng.uniform(-1.0, 1.0)
        return self._lateral * self._arc_strength * distance

    def compute(self, observation: Observation) -> tuple[float, float]:
        self._track(observation)
        lead_x, lead_y = self._lead()
        aim_x = observation.error_x + lead_x
        aim_y = observation.error_y + lead_y
        distance = math.hypot(aim_x, aim_y)
        if self._arc_strength <= 0.0 and self._commit <= 0:
            # 上面那个恒等式说明 B(t) 此时就是 t·aim, 但浮点算不出逐位相等, 所以显式
            # 走直线 —— 关着的时候必须和 feedforward 逐位相同。
            kp = dynamic_kp(
                distance, observation.kp_min, observation.kp_max, observation.kp_growth
            )
            return aim_x * kp, aim_y * kp
        if distance < 1e-9:
            return 0.0, 0.0
        if self._commit > 0:
            return self._committed_step(observation, aim_x, aim_y, distance)
        # 进度必须用**误差**距离量, 不能用 aim 距离。aim 里含前馈量, 而前馈在拉枪的
        # 头几帧会把误差变化整个当成目标位移(那时指令历史还不够长, _landed_command
        # 返回 0), 前馈量大到能让 aim 翻号 —— 拿它算进度会忽前忽后。
        travel = math.hypot(observation.error_x, observation.error_y)
        if travel < 1e-9:
            return 0.0, 0.0
        if self._span <= 0.0 or travel > self._span * _NEW_MOVE:
            # 新的一次拉枪(或目标在远离): 重新定这次移动的「弦」—— 起点到目标的那条
            # 直线。弧形相对这条弦定死, 整段移动里不再变, 真贝塞尔就是这么定义的。
            self._span = travel
            self._axis = (observation.error_x / travel, observation.error_y / travel)
            self._offset = self._rng.uniform(-1.0, 1.0) * self._arc_strength * travel
        # 进度**投影到弦上**量, 不能用总距离。
        #
        # 用总距离会死锁: 弧线故意把准心横向偏开, 这个偏离本身让总距离降不下去, 于是
        # 进度卡住 → 弧线不收 → 距离更降不下去。实测进度卡在 0.87 再也不动。
        # 投影到弦上就断开了这个循环 —— 横向偏移垂直于弦, 对投影的贡献是二阶的。
        along = observation.error_x * self._axis[0] + observation.error_y * self._axis[1]
        progress = min(1.0, max(0.0, 1.0 - along / self._span))
        # 关键: 弧线是**设定点的轨迹**, 不是横向速度前馈。
        #
        # 一开始我按 d/dp[3·offset·p(1-p)] 往输出里注入横向速度, 指望它积分成那条弧。
        # 积不出来 —— 控制器每帧都在往目标修正, 横向偏离刚出来就被 aim*kp 拉回去了
        # (时间常数约 1/kp ≈ 8 帧)。被控对象不是自由积分器, 有回复力。
        #
        # 所以把瞄准点本身横向偏到曲线上: 控制器自然会去追那个偏出去的点, 而 p→1 时
        # 偏移量归零, 它就收敛到目标本身。到达时横向自动归零, 不需要额外的距离衰减。
        bulge = 3.0 * self._offset * progress * (1.0 - progress)
        aim_x -= self._axis[1] * bulge
        aim_y += self._axis[0] * bulge
        distance = math.hypot(aim_x, aim_y)
        kp = dynamic_kp(
            distance, observation.kp_min, observation.kp_max, observation.kp_growth
        )
        return aim_x * kp, aim_y * kp

    def _committed_step(
        self, observation: Observation, aim_x: float, aim_y: float, distance: float
    ) -> tuple[float, float]:
        if self._plan is None or self._plan_left <= 0:
            kp = dynamic_kp(
                distance, observation.kp_min, observation.kp_max, observation.kp_growth
            )
            self._plan = (aim_x, aim_y, self._next_offset(distance), kp)
            self._plan_left = self._commit
            self._travelled = 0.0
        plan_x, plan_y, offset, kp = self._plan
        # 这一段刻意不看 observation: 承诺了就是承诺了, 开环的代价要真的付出来。
        before = _bezier(plan_x, plan_y, offset, self._travelled)
        self._travelled = min(1.0, self._travelled + kp)
        after = _bezier(plan_x, plan_y, offset, self._travelled)
        self._plan_left -= 1
        return after[0] - before[0], after[1] - before[1]


HUMAN_ALGORITHMS: tuple[type, ...] = (FeedforwardWind, WindMouse, FeedforwardBezier)
