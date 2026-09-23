"use strict";

/* 表单的视图。真相在 Python 侧的 FormBridge 里 —— 这边只做两件事：按推过来的
   状态填控件，和把用户的改动推上去。

   控件到字段的绑定写在 HTML 的 data-field 属性里，这里扫一遍全接上。一个控件
   写一行 JS 的话是四十来行样板，而且漏一个不会报错 —— 那个控件只是「没用」。 */

function fieldValue(element) {
  if (element.type === "checkbox") return element.checked;
  if (element.type === "number" || element.type === "range") return Number(element.value);
  return element.value;
}

function applyValue(element, value) {
  if (element.type === "checkbox") {
    element.checked = Boolean(value);
    syncCheck(element);
  } else {
    element.value = value;
    if (element.type === "range") syncSlider(element);
  }
}

/* design 的滑条是一个 opacity:0 的原生 range 盖在轨道上，看得见的填充和滑块
   是两个绝对定位的 div —— 位置得我们自己算。不同步的话滑块永远停在最左边，
   而值其实在变：最让人不信任的那种坏法。 */
function syncSlider(input) {
  const rail = input.closest(".ef-slider__rail");
  if (!rail) return;
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  const ratio = max > min ? (Number(input.value) - min) / (max - min) : 0;
  const percent = (Math.max(0, Math.min(1, ratio)) * 100).toFixed(2) + "%";
  const fill = rail.querySelector(".ef-slider__fill");
  const thumb = rail.querySelector(".ef-slider__thumb");
  if (fill) fill.style.width = percent;
  if (thumb) thumb.style.left = percent;
}

/* design 的复选框状态在 class 上（.ef-check--on），因为设计稿那边是 React
   组件、根本没有原生控件。我们用真的 <input>（表单语义、键盘可达都是白给的），
   所以勾选态要自己同步过去。 */
function syncCheck(input) {
  const label = input.closest(".ef-check");
  if (label) label.classList.toggle("ef-check--on", input.checked);
}

function pathValue(payload, path) {
  return path.split(".").reduce(
    (node, key) => (node === undefined || node === null ? undefined : node[key]), payload);
}

function setForm(payload) {
  document.querySelectorAll("[data-field]").forEach((element) => {
    const value = pathValue(payload, element.dataset.field);
    if (value !== undefined) applyValue(element, value);
  });
  /* 算法下拉没有 data-field：换算法要连参数一起换掉，走的是 set_algorithm
     而不是 set_field。所以它得单独填。 */
  document.querySelectorAll("[data-algorithm]").forEach((select) => {
    const profile = payload.profiles && payload.profiles[Number(select.dataset.algorithm)];
    if (profile && profile.algorithm !== undefined) select.value = profile.algorithm;
  });

  /* 滑条旁边那个数字是派生的，不是字段。一起刷新，不然整片换之后数字还停在
     旧值上 —— 而滑条已经跳走了，两者对不上最让人不信任。 */
  document.querySelectorAll("[data-readout]").forEach(refreshReadout);
  document.dispatchEvent(new CustomEvent("form-changed", { detail: payload }));
}

function setChoices(choices) {
  document.querySelectorAll("[data-choices]").forEach((select) => {
    const options = choices[select.dataset.choices] || [];
    const current = select.value;
    select.innerHTML = "";
    options.forEach((label) => {
      const option = document.createElement("option");
      option.value = label;
      option.textContent = label;
      select.appendChild(option);
    });
    /* 选项重建之后当前值会掉。能对上就恢复 —— 对不上就随它落到第一项，那正是
       「配置里指着一个已删掉的算法」该有的样子。 */
    if (options.indexOf(current) >= 0) select.value = current;
  });
}

function refreshReadout(element) {
  const source = document.querySelector('[data-field="' + element.dataset.readout + '"]');
  if (!source) return;
  const digits = Number(element.dataset.digits || 0);
  element.textContent = Number(source.value).toFixed(digits) + (element.dataset.unit || "");
}

function pushField(element) {
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.set_field(element.dataset.field, fieldValue(element));
  }
}

function bindForm() {
  document.querySelectorAll("[data-field]").forEach((element) => {
    /* 重复调用会给老控件挂第二个监听器，一次改动推两次。renderParams 建完新
       控件要再调一次 bindForm，所以这个标记不是可选的。 */
    if (element.dataset.bound) return;
    element.dataset.bound = "1";

    /* input 是「正在拖」，change 是「松手了」。滑条要 input —— 边拖边看手感
       正是这个界面存在的理由。文本框也用 input：等 change（失焦）的话，用户
       改完直接按启动，改的那一下根本没推上去。 */
    element.addEventListener("input", () => {
      if (element.type === "checkbox") syncCheck(element);
      if (element.type === "range") syncSlider(element);
      pushField(element);
      document.querySelectorAll('[data-readout="' + element.dataset.field + '"]')
        .forEach(refreshReadout);
    });
    element.addEventListener("change", () => pushField(element));
  });
}

/* 画面输入按方式切：UDP、OBS、本机屏幕三组，同时只显示一组。照旧界面
   _switch_input_panel。都留在 DOM 里而不是重建 —— 切回来时用户填过的值
   还在，重建的话要重新从状态里填一遍，中间会闪一下空值。 */
function syncInputPanels() {
  const mode = document.querySelector('[data-field="input_mode"]');
  if (!mode) return;
  const isObs = mode.value.indexOf("OBS") >= 0;
  const isDesktop = mode.value.indexOf("本机") >= 0;
  document.getElementById("udp-panel").hidden = isObs || isDesktop;
  document.getElementById("obs-panel").hidden = !isObs;
  document.getElementById("desktop-panel").hidden = !isDesktop;
}

/* 移动输出：KMBox 那几项只在选 KMBox 时显示，选 SendInput 时换成那行提示。
   02 屏第三盏灯的标题跟着换 —— 灯上写着 KMBox 而实际用的是 SendInput 的话，
   用户会去查一个根本没在用的盒子。 */
function syncOutputPanels() {
  const output = document.querySelector('[data-field="mouse_output"]');
  if (!output) return;
  const isSendInput = output.value.indexOf("SendInput") >= 0;
  document.getElementById("kmbox-fields").hidden = isSendInput;
  document.getElementById("sendinput-note").hidden = !isSendInput;
  const title = document.getElementById("lamp-kmbox-title");
  if (title) title.textContent = isSendInput ? "SendInput" : "KMBox";
}

/* design 的方块和下拉箭头都期望一个图标子元素（.ef-check__box > * 在未勾选时
   opacity:0，.ef-select__caret 是个空的 flex 容器）。不塞图标的话勾上是一块
   纯黄、下拉框没有任何「这是下拉框」的提示。 */
function paintFormIcons() {
  document.querySelectorAll(".ef-check__box").forEach((box) => {
    if (!box.firstChild) box.innerHTML = window.efIcon("check", 12);
  });
  document.querySelectorAll(".ef-select__caret").forEach((caret) => {
    if (!caret.firstChild) caret.innerHTML = window.efIcon("chevron-down", 14);
  });
}

/* 算法参数按 Param 契约现建。写死的话，用户从算法库导入一个自己写的算法，
   参数一个都出不来 —— 而那正是算法库存在的理由。 */
function renderParams(profile, specs, values) {
  const host = document.getElementById("params-" + profile);
  if (!host) return;
  host.innerHTML = "";

  const plain = specs.filter((spec) => !spec.advanced);
  const advanced = specs.filter((spec) => spec.advanced);
  plain.forEach((spec) => host.appendChild(paramRow(profile, spec, values)));

  /* 高级参数默认折起来，照旧界面把它们分开的做法。一股脑全铺开的话，常用的
     那两三个会被淹在十几行里。 */
  if (advanced.length) {
    const box = document.createElement("details");
    box.className = "ef-params__advanced";
    const summary = document.createElement("summary");
    summary.textContent = "高级参数（" + advanced.length + "）";
    box.appendChild(summary);
    advanced.forEach((spec) => box.appendChild(paramRow(profile, spec, values)));
    host.appendChild(box);
  }

  /* 新建的控件要接上，不然拖了没反应。bindForm 自己有去重标记，老控件不会
     被挂第二个监听器。 */
  bindForm();
}

function paramRow(profile, spec, values) {
  const path = "profiles." + profile + ".algorithm_params." + spec.name;
  const value = values[spec.name] !== undefined ? values[spec.name] : spec.default;
  /* 步长决定读数的小数位：step 0.01 要两位，1 要零位。写死三位的话「单帧速度
     上限 15.000 px」读着像个精密仪器，其实它是整数。 */
  const digits = String(spec.step).indexOf(".") >= 0
    ? String(spec.step).split(".")[1].length : 0;

  const field = document.createElement("div");
  field.className = "ef-field";
  field.innerHTML =
    '<div class="ef-field__row">' +
      '<span class="ef-field__label">' + spec.label + "</span>" +
      '<div class="ef-slider">' +
        '<div class="ef-slider__main"><div class="ef-slider__rail">' +
          '<div class="ef-slider__track" aria-hidden="true"></div>' +
          '<div class="ef-slider__fill" aria-hidden="true"></div>' +
          '<div class="ef-slider__thumb" aria-hidden="true"></div>' +
          '<input class="ef-slider__input" type="range" data-field="' + path + '"' +
            ' min="' + spec.minimum + '" max="' + spec.maximum + '" step="' + spec.step + '">' +
        "</div></div>" +
        '<div class="ef-slider__value">' +
          '<span data-readout="' + path + '" data-digits="' + digits + '">—</span>' +
        "</div>" +
      "</div>" +
    "</div>";

  const input = field.querySelector("input");
  input.value = value;
  syncSlider(input);
  const readout = field.querySelector("[data-readout]");
  readout.textContent = Number(value).toFixed(digits);
  return field;
}

/* 轨迹关掉时，「最优路径」和「轨迹长度」都没有意义。照旧界面 _sync_trail_controls。
   留着能拖的话，用户会调半天一个根本没在画的东西。 */
function syncTrailControls() {
  const on = document.querySelector('[data-field="trail_enabled"]');
  const optimal = document.getElementById("trail-optimal");
  const length = document.getElementById("trail-length");
  if (!on || !optimal || !length) return;
  optimal.classList.toggle("ef-check--disabled", !on.checked);
  optimal.querySelector("input").disabled = !on.checked;
  /* design 的 .ef-slider--disabled 是 pointer-events:none —— 拖不动了，但键盘
     还能聚焦上去调。原生 disabled 才真的关掉它。 */
  length.classList.toggle("ef-slider--disabled", !on.checked);
  length.querySelector("input").disabled = !on.checked;
}

/* 方案关掉时整块压暗 + 不可点，照规格。留着能点的话，用户会调半天一个根本
   不生效的方案。 */
function syncProfileEnabled() {
  [0, 1].forEach((index) => {
    const panel = document.getElementById("profile-" + index);
    const toggle = document.querySelector('[data-field="profiles.' + index + '.enabled"]');
    if (panel && toggle) panel.classList.toggle("ef-profile--off", !toggle.checked);
  });
}

function wireForms() {
  paintFormIcons();
  bindForm();
  document.querySelectorAll('.ef-check input[type="checkbox"]').forEach(syncCheck);
  const browse = document.getElementById("browse-model");
  if (browse) {
    browse.addEventListener("click", () => {
      if (window.pywebview && window.pywebview.api) window.pywebview.api.browse_model();
    });
  }
  const mode = document.querySelector('[data-field="input_mode"]');
  if (mode) mode.addEventListener("change", syncInputPanels);
  const output = document.querySelector('[data-field="mouse_output"]');
  if (output) output.addEventListener("change", syncOutputPanels);

  /* 算法下拉不走 set_field：换算法要连参数一起换掉（每个算法的参数完全不同），
     那是 Python 侧 set_algorithm 的事。 */
  document.querySelectorAll("[data-algorithm]").forEach((select) => {
    select.addEventListener("change", () => {
      if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.set_algorithm(Number(select.dataset.algorithm), select.value);
      }
    });
  });

  document.querySelectorAll('[data-field$=".enabled"]').forEach((toggle) => {
    toggle.addEventListener("input", syncProfileEnabled);
  });

  document.querySelectorAll('[data-field="trail_enabled"]').forEach((toggle) => {
    toggle.addEventListener("input", syncTrailControls);
  });

  /* 轨迹长度顺手记进 settings.txt，只记这一个字段。用 change 而不是 input：
     原生 range 的 change 是松手才发一次，正好是旧界面那个 400ms 防抖要的效果，
     而且不用自己管一个定时器。每次 input 都写盘的话，拖一下滑条就是上百次写。 */
  const trail = document.querySelector('[data-field="trail_seconds"]');
  if (trail) {
    trail.addEventListener("change", () => {
      if (window.pywebview && window.pywebview.api) window.pywebview.api.persist_trail_length();
    });
  }

  document.addEventListener("form-changed", () => {
    syncInputPanels();
    syncOutputPanels();
    syncProfileEnabled();
    syncTrailControls();
  });
  syncInputPanels();
  syncOutputPanels();
  syncProfileEnabled();
  syncTrailControls();
}

window.setForm = setForm;
window.setChoices = setChoices;
window.bindForm = bindForm;
window.wireForms = wireForms;
window.syncCheck = syncCheck;
window.renderParams = renderParams;
window.syncSlider = syncSlider;
window.syncTrailControls = syncTrailControls;
