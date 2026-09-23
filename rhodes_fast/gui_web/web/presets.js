"use strict";

/* 预设条。放在 SectionHeader 右侧 —— 一份预设同时决定 01 和 02 两屏的值，
   是跨面板的全局操作，跟 SectionHeader 同属外壳；TitleBar 只有 44px，而且
   已经被三个窗口按钮占满了。

   四个控件：下拉 + 保存 + 另存为… + 删除。真相在 Python 侧（app.py 的
   _current_preset / _preset_baseline），这里只是把它画出来。 */

const NO_PRESET = "（未选择预设）";

function setPresets(payload) {
  const select = document.getElementById("preset-select");
  if (!select) return;

  const names = payload.names || [];
  const current = payload.current === undefined ? null : payload.current;
  select.innerHTML = "";

  /* 「未选择预设」只在真的没选时才进列表。一直留着的话它是个点了没反应的死
     选项 —— 旧界面同样的做法：下拉里只有预设名，这句话只是没选时的显示文字。 */
  if (current === null) {
    const none = document.createElement("option");
    none.value = "";
    none.textContent = NO_PRESET;
    select.appendChild(none);
  }

  names.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    /* 星号只进显示文字，不进 value —— 进了 value 的话，重选这一项送给 Python
       的就是一个带星号的名字，而那个预设不存在。 */
    option.textContent = name === current && payload.changed ? name + " *" : name;
    select.appendChild(option);
  });

  select.value = current === null ? "" : current;
  document.getElementById("preset-delete").disabled = current === null;
}

function wirePresets() {
  const call = (name, argument) => {
    if (!window.pywebview || !window.pywebview.api) return;
    if (argument === undefined) window.pywebview.api[name]();
    else window.pywebview.api[name](argument);
  };

  const select = document.getElementById("preset-select");
  if (select) {
    /* change 而不是 input：原生 select 两个都发，但键盘上下键在 Windows 上会
       一路发 input，等于每按一次方向键就载入一份预设。 */
    select.addEventListener("change", () => {
      // 重选当前这一项也照常载入：有改动会先问，选「否」就等于撤回改动。
      if (select.value) call("select_preset", select.value);
    });
  }

  [
    ["preset-save", "save_preset"],
    ["preset-save-as", "save_preset_as"],
    ["preset-delete", "delete_preset"],
  ].forEach((pair) => {
    const button = document.getElementById(pair[0]);
    if (button) button.addEventListener("click", () => call(pair[1]));
  });
}

window.setPresets = setPresets;
window.wirePresets = wirePresets;
