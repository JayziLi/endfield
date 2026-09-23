"use strict";

/* 应用外壳的行为：图标注入、面板切换、窗口控制。
   业务逻辑一概不在这里 —— 启停走 pywebview.api，真正干活的是 Python 侧的
   rhodes_fast.gui_core。 */

const SECTIONS = {
  "01": { title: "运行设置", en: "RUNTIME SETTINGS" },
  "02": { title: "识别与控制", en: "DETECTION & CONTROL" },
  "03": { title: "算法库", en: "ALGORITHM LIBRARY" },
  "04": { title: "实时预览", en: "LIVE PREVIEW" },
};

const WINDOW_GLYPHS = { minimize: "minus", maximize: "square", close: "x" };

/* 图标是 inline SVG 不是 <img>：设计系统的字形用 stroke="currentColor" 继承底色，
   而 NavRail 选中项是黄底黑字 —— 外部文件染不上色，会留在错误的颜色上。 */
function paintIcons() {
  document.querySelectorAll("[data-icon]").forEach((host) => {
    const slot = host.querySelector(".ef-railitem__icon");
    if (slot) {
      slot.innerHTML = window.efIcon(host.dataset.icon, host.classList.contains("ef-railitem--quiet") ? 14 : 20);
      return;
    }
    /* 没有内层槽的（比如 IconButton）自己就是宿主。不接这一支的话，那个按钮
       是个空方块 —— 点得动，但看不出是干什么的。 */
    host.innerHTML = window.efIcon(host.dataset.icon, 16);
  });
  document.querySelectorAll("[data-window]").forEach((button) => {
    button.innerHTML = window.efIcon(WINDOW_GLYPHS[button.dataset.window], 14);
  });
}

function showSection(id) {
  const meta = SECTIONS[id];
  if (!meta) return;

  document.querySelectorAll("[data-screen]").forEach((screen) => {
    screen.hidden = screen.dataset.screen !== id;
  });
  document.querySelectorAll("[data-section]").forEach((item) => {
    item.classList.toggle("ef-railitem--active", item.dataset.section === id);
  });

  document.getElementById("section-index").textContent = id;
  document.getElementById("section-title").textContent = meta.title;
  document.getElementById("section-en").textContent = meta.en;
  // 切屏时把面板区滚回顶部 —— 上一屏滚到一半的位置带到下一屏是没道理的。
  document.getElementById("screens").scrollTop = 0;

  /* Task 9 听这个事件来开关 preview_enable_file：不看预览时让子进程省下渲染
     和 JPEG 编码 —— 那台副机同时在跑推理。 */
  document.dispatchEvent(new CustomEvent("section-changed", { detail: { id } }));
}

function appendLog(line) {
  const log = document.getElementById("runtime-log");
  const row = document.createElement("div");
  row.className = "ef-log__line";
  /* 文字必须裹在 .ef-log__msg 里。design 给颜色的是这个类，不裹的话文字继承
     body 的 --ink-1，深色字落在深色终端底上 —— 日志在那儿，但一个字都读不出来。
     截图才看出来的。 */
  const message = document.createElement("span");
  message.className = "ef-log__msg";
  message.textContent = line;
  row.appendChild(message);
  log.appendChild(row);
  // 只在本来就贴着底的时候自动跟随：用户往上翻是为了看某一行，
  // 新日志把他拽回底部只会让人抓狂。
  const pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
  if (pinned) log.scrollTop = log.scrollHeight;
}

/* 「在不在跑」这件事以 Python 侧为准：这里只是它回填进来的一份副本，
   点击时拿它决定按下去是启动还是停止。 */
let running = false;

function setRunState(next) {
  running = Boolean(next);
  const button = document.getElementById("start-stop");
  /* 信号黄纪律：内容区同时只有一个黄块，就是这个按钮 —— 它回答「系统在不在跑」。
     index.html 的初始标记里因此没有 ef-btn--primary（那是安静的 outline），
     跑起来才由这里换上。两个类都用 toggle：留着上一个状态的类会叠出一个
     又填黄又有描边的按钮。 */
  button.classList.toggle("ef-btn--primary", running);
  button.classList.toggle("ef-btn--outline", !running);
  document.getElementById("start-stop-label").textContent = running ? "停止" : "启动系统";
  document.getElementById("run-state").textContent = running ? "RUNNING" : "IDLE";

  document.getElementById("preview-badge").hidden = !running;
  syncPreviewSignal();
  setLocks(running);
}

/* 模型路径、浏览按钮、加速方式、预设下拉只在启动时读一次。运行中留着能改就是
   在骗人 —— 改了不生效，而界面上看不出来。照旧界面 _set_running。
   保存和另存为不锁：边打边调好了，那一刻正是想存下来的时候。

   锁和解锁是同一段代码的两个方向。分成两处写的话，停止那一下漏掉某个控件，
   用户就得重启程序才能再改它。 */
function setLocks(locked) {
  document.querySelectorAll("[data-lock-while-running]").forEach((element) => {
    element.disabled = locked;
    /* 原生 disabled 只让控件不可点。design 的外框画在包着它的那一层上
       （.ef-input / .ef-select），不跟着改的话，一个点不动的输入框看起来跟能用
       的一模一样 —— 用户只会以为界面卡了。 */
    const box = element.closest(".ef-input, .ef-select");
    if (box) {
      box.classList.toggle("ef-input--disabled", locked && box.classList.contains("ef-input"));
      box.classList.toggle("ef-select--disabled", locked && box.classList.contains("ef-select"));
    }
  });
}

/* 画面到底来没来，问 <img> 自己：multipart 流还没出第一帧时 naturalWidth 是 0。
   不用 load 事件 —— 实测 Chromium 对 multipart/x-mixed-replace 只在第一帧时
   complete 就变 true，而监听器挂上去之前那一下已经过去了，分辨率会永远停在横杠。
   改成每次 telemetry 来的时候顺手问一遍（一秒一次，零成本）。 */
function syncPreviewSignal() {
  const media = document.getElementById("preview-media");
  const empty = document.getElementById("preview-empty");
  const live = running && media.naturalWidth > 0;

  empty.hidden = live;
  /* 「没启动」和「启动了但画面没来」是两回事：前者等用户按按钮，后者多半是
     采集端没推流。盖着同一句话的话，用户会一直等一个永远不来的画面。 */
  empty.textContent = running ? "等待画面 · WAITING FOR FRAMES" : "未启动 · NO SIGNAL";
  document.getElementById("tele-res").textContent =
    live ? media.naturalWidth + "×" + media.naturalHeight : "—";
}

/* 管线每秒打两行数字，Python 侧解析完按字段名送过来（gui_web/telemetry.py）。
   这里只做一件事：有哪个字段就填哪一格，没有的不动。两行的字段并不重合，
   把缺的当成 0 会让延迟那一格每秒闪一次。 */
const TELEMETRY_SLOTS = {
  capture_fps: "metric-capture",
  processed_fps: "metric-processed",
  infer_ms: "metric-infer",
  dropped: "metric-dropped",
  detections: "tele-targets",
  latency_ms: "status-latency",
  model: "status-model",
  input: "status-input",
};

/* 02 屏那三盏灯。状态在 Python 侧从日志里读（gui_web/lamps.py），这里只负责画。

   design 的灯有五个状态类，同时只能挂一个 —— 留着上一个的话会叠出一盏又亮黄
   又闪烁的灯。所以每次先把五个都摘掉再挂一个。 */
const LAMP_STATES = ["off", "standby", "connecting", "online", "error"];

function setLamps(updates) {
  Object.keys(updates).forEach((name) => {
    const lamp = document.getElementById("lamp-" + name);
    const sub = document.getElementById("lamp-" + name + "-sub");
    if (!lamp || !sub) return;
    LAMP_STATES.forEach((state) => lamp.classList.remove("ef-lamp--" + state));
    lamp.classList.add("ef-lamp--" + updates[name].state);
    sub.textContent = updates[name].text;
    /* 失败原因常常比灯上放得下的长（一整条 WinError）。截断的那部分挂 title，
       而「连接失败」四个字本身是没法排查的。 */
    sub.title = updates[name].text;
  });
}

/* 管线每秒重复打的那两行合成一条固定行。两行分别到达，所以这里留一份合并状态 ——
   只拿最后一行渲染的话，帧率和延迟会一秒一次地互相顶替。 */
const TELEMETRY = {};
const STATUS_RAW = { rate: "", latency: "" };
const STATUS_IDLE = "未运行";

/* 固定行上放哪几个数。标签写在这里而不是抄管线的原文：原文一行两百来字符，
   在这条里会被截断，而截断点正好落在延迟上。完整原文挂 title。 */
const STATUS_PARTS = [
  ["capture_fps", "采集", ""],
  ["processed_fps", "处理", " 帧/秒"],
  ["infer_ms", "推理", ""],
  ["detect_ms", "检测", " 毫秒"],
  ["detections", "目标", ""],
  ["dropped", "丢帧", ""],
  ["latency_ms", "延迟", ""],
  ["latency_p95_ms", "P95", " 毫秒"],
];

function setStatusLine(kind, raw) {
  STATUS_RAW[kind] = raw;
  renderStatusLine();
}

function renderStatusLine() {
  const box = document.getElementById("runtime-status");
  if (!box) return;
  const parts = STATUS_PARTS
    .filter((part) => TELEMETRY[part[0]] !== undefined)
    .map((part) => part[1] + " " + TELEMETRY[part[0]] + part[2]);
  box.textContent = parts.length ? parts.join("  ·  ") : STATUS_IDLE;
  box.title = [STATUS_RAW.rate, STATUS_RAW.latency].filter(Boolean).join(String.fromCharCode(10));
}

/* 一格数字。有数就是 Archivo Black，没数就摘掉那个字重 —— 占位符用那个字重排
   出来是一块实心方块，看着像缺字（见 app.css 的 .ef-num--idle）。 */
function setSlot(id, value) {
  const slot = document.getElementById(id);
  if (!slot) return;
  slot.textContent = value;
  slot.classList.toggle("ef-num--idle", value === PLACEHOLDERS[id]);
}

function setTelemetry(values) {
  Object.keys(values).forEach((field) => { TELEMETRY[field] = values[field]; });
  renderStatusLine();
  Object.keys(TELEMETRY_SLOTS).forEach((field) => {
    if (values[field] === undefined) return;
    setSlot(TELEMETRY_SLOTS[field], values[field]);
  });
  // 状态栏的 FPS 和画面上的 LATENCY 跟别处共用同一个数，带上单位读着才像句话。
  if (values.processed_fps !== undefined) setSlot("status-fps", values.processed_fps);
  if (values.latency_ms !== undefined) setSlot("tele-latency", values.latency_ms + " ms");
  syncPreviewSignal();
}

/* 占位符由 HTML 自己定义，这里只是在启动时抄一份下来：不用在 JS 里再维护一份
   一模一样的横杠表，而且 setSlot 要靠它判断「现在这一格是不是还没有数」。 */
const PLACEHOLDERS = {};

function rememberPlaceholders() {
  Object.values(TELEMETRY_SLOTS)
    .concat(["status-fps", "tele-latency", "tele-res"])
    .forEach((id) => { PLACEHOLDERS[id] = document.getElementById(id).textContent; });
}

function clearTelemetry() {
  Object.keys(PLACEHOLDERS).forEach((id) => setSlot(id, PLACEHOLDERS[id]));
  /* 固定行也要清。子进程都没了还挂着最后一秒的帧率，那是在撒谎 —— 而这一行
     比那几格更显眼。 */
  Object.keys(TELEMETRY).forEach((field) => { delete TELEMETRY[field]; });
  STATUS_RAW.rate = "";
  STATUS_RAW.latency = "";
  renderStatusLine();
}

/* 动作行上那四个按钮。三条测速不能走 start()：那一条会绑预览 socket，而
   测出来的数字里就掺进了一份没人看的渲染和 JPEG 编码。 */
const ACTIONS = {
  "action-save": "save_settings",
  "action-check": "run_check",
  "action-benchmark": "run_benchmark",
  "action-pipeline": "run_pipeline_benchmark",
  // 放大预览不在动作行上（在 04 屏的面板头里），但接法一模一样。
  "preview-popout": "open_preview_window",
};

function wireStartStop() {
  document.getElementById("start-stop").addEventListener("click", () => {
    if (!window.pywebview || !window.pywebview.api) return;
    /* 按钮上写的就是这一下要做的事。Python 侧还会自己拦一道（已经在跑就不再起），
       所以这里点快了也不会起出第二个子进程。 */
    window.pywebview.api[running ? "stop" : "start"]();
  });

  Object.keys(ACTIONS).forEach((id) => {
    const button = document.getElementById(id);
    if (button) {
      button.addEventListener("click", () => {
        if (window.pywebview && window.pywebview.api) window.pywebview.api[ACTIONS[id]]();
      });
    }
  });
}

/* 预览流只在 04 屏上接着。

   不能把 src 写进 HTML：/preview.mjpg 是一条不会结束的 multipart 流，页面一
   加载就挂着它，window 的 load 事件就永远不触发 —— 而 pywebview 等的正是 load，
   等不到就认为窗口没起来，之后每一次 evaluate_js 都抛异常。界面画得好好的，
   但日志、数字、启停状态一个都推不上去。实测过：带 bus 的 server 下窗口 30 秒
   都起不来，不带 bus 的 1.3 秒就可用，差别只有这一个属性。

   切走就断开：省下 server 那条一直挂着的响应线程，也省下浏览器解码一份没人
   看的画面。 */
const PREVIEW_STREAM = "/preview.mjpg";
let previewOnScreen = false;

function setPreviewOnScreen(on) {
  previewOnScreen = on;
  const media = document.getElementById("preview-media");
  if (on) {
    // 带时间戳：切回来时别拿到缓存里那条已经断掉的流。
    media.src = PREVIEW_STREAM + "?t=" + Date.now();
  } else {
    media.removeAttribute("src");
  }
  syncPreviewSignal();
}

function wirePreview() {
  const media = document.getElementById("preview-media");

  /* 流断了（server 重启、连接被中间层掐掉）之后 <img> 不会自己重连，画面就
     永远停在最后一帧上 —— 而且看起来跟「管线没发帧」一模一样。重新接一次；
     server 那边比较路径前会先剥掉查询串。 */
  media.addEventListener("error", () => {
    if (previewOnScreen && running) {
      setTimeout(() => { if (previewOnScreen) setPreviewOnScreen(true); }, 1000);
    }
  });

  /* 切进 04 才让子进程渲染 + 编码 JPEG，切走就停 —— 这台副机同时在跑推理。
     事件是 showSection 派发的。 */
  document.addEventListener("section-changed", (event) => {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.set_preview_active(event.detail.id === "04");
    }
    setPreviewOnScreen(event.detail.id === "04");
    /* 再补一次：刚打开开关时，子进程要下一帧才看得到那个文件，第一帧还没到。 */
    if (event.detail.id === "04") setTimeout(syncPreviewSignal, 600);
  });
}

/* 运行日志折起来 / 展开。折的是滚动的那一段，下面那条固定状态行留着 ——
   它只有 28px，而且正是折起来之后唯一还看得到的实时数字。

   状态存在 Python 侧（settings.txt 的 ui.log_collapsed），这里只是它的视图：
   开窗时由 setLogCollapsed 回填，用户点一下再推回去。 */
let logCollapsed = false;

function setLogCollapsed(on) {
  logCollapsed = Boolean(on);
  const wrap = document.getElementById("logwrap");
  const button = document.getElementById("log-toggle");
  if (!wrap || !button) return;
  wrap.classList.toggle("ef-logwrap--collapsed", logCollapsed);
  button.setAttribute("aria-expanded", String(!logCollapsed));
  button.setAttribute("aria-label", logCollapsed ? "展开运行状态" : "折叠运行状态");
}

function wireLogToggle() {
  const button = document.getElementById("log-toggle");
  if (!button) return;
  button.addEventListener("click", () => {
    setLogCollapsed(!logCollapsed);
    /* 记住它。折叠是为了腾地方，每次打开都要再折一次的话这个开关就没意义了。 */
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.set_log_collapsed(logCollapsed);
    }
  });
}

function wireWindowControls() {
  const call = (name) => {
    if (window.pywebview && window.pywebview.api) window.pywebview.api[name]();
  };
  document.querySelectorAll("[data-window]").forEach((button) => {
    button.addEventListener("click", () => {
      call({ minimize: "minimize", maximize: "toggle_maximize", close: "close" }[button.dataset.window]);
    });
  });
  // 双击标题栏最大化，跟 Windows 的习惯一致。
  document.getElementById("titlebar-drag").addEventListener("dblclick", () => call("toggle_maximize"));
}

paintIcons();
rememberPlaceholders();
renderStatusLine();
document.querySelectorAll("[data-section]").forEach((item) => {
  item.addEventListener("click", () => showSection(item.dataset.section));
});
wireWindowControls();
wireStartStop();
wireLogToggle();
wirePreview();
// 表单的接线归 forms.js。它在 index.html 里排在 app.js 前面。
if (window.wireForms) window.wireForms();
if (window.wireLibrary) window.wireLibrary();
if (window.wirePresets) window.wirePresets();
showSection("01");
// 初始状态落定在这里，而不是写死在 HTML 里：按钮的文案、状态标签和那块信号黄
// 只有一处定义，界面起来是什么样跟跑起来又停下来之后是什么样，走的是同一段代码。
setRunState(false);

window.appendLog = appendLog;
window.showSection = showSection;
window.setRunState = setRunState;
window.setTelemetry = setTelemetry;
window.setStatusLine = setStatusLine;
window.setLamps = setLamps;
window.setLogCollapsed = setLogCollapsed;
window.clearTelemetry = clearTelemetry;
