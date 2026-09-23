"use strict";

/* 模态框。设计系统没有 Dialog（规格第 7 节的两个缺口之一），用它的基础类拼。

   每个弹窗带一个 token，回答时原样送回 Python —— 那边正有一条线程阻塞在这个
   token 上（见 gui_web/prompts.py）。不送 token 的话，两个问题叠起来时答案会
   串，而那两个很可能一个是「要保存吗」一个是「确定删除吗」。 */

const MODAL_BUTTONS = {
  error:     [{ label: "知道了", value: null, primary: true }],
  warning:   [{ label: "知道了", value: null, primary: true }],
  info:      [{ label: "知道了", value: null, primary: true }],
  confirm:   [{ label: "取消", value: false },
              { label: "确定", value: true, primary: true }],
  three_way: [{ label: "取消", value: null },
              { label: "否", value: false },
              { label: "是", value: true, primary: true }],
  /* takesInput 的按钮不带 value：值要到按下去那一刻才从输入框里取。用一个魔法
     字符串当哨兵也能做，但那等于在数据空间里挖一个洞 —— 用户真打了那个字符串
     就出错，而且永远查不到。 */
  text:      [{ label: "取消", value: null },
              { label: "确定", takesInput: true, primary: true }],
};

let modalToken = null;
let modalKind = null;

function showModal(spec) {
  modalToken = spec.token;
  modalKind = spec.kind;
  document.getElementById("modal-title").textContent = spec.title;
  document.getElementById("modal-message").textContent = spec.message;

  const field = document.getElementById("modal-field");
  const input = document.getElementById("modal-input");
  field.hidden = spec.kind !== "text";
  if (spec.kind === "text") input.value = spec.initial || "";

  const actions = document.getElementById("modal-actions");
  actions.innerHTML = "";
  (MODAL_BUTTONS[spec.kind] || MODAL_BUTTONS.info).forEach((button) => {
    const element = document.createElement("button");
    element.type = "button";
    /* 破坏性操作用 danger（红描边），其余主按钮用 secondary —— 不用 primary。
       内容区那一个填黄的块是启动/停止按钮，弹窗再填一个就有两个了，而设计
       系统的规矩是 If two things are yellow, one of them is wrong。 */
    const emphasis = button.primary
      ? (spec.danger ? "ef-btn--danger" : "ef-btn--secondary")
      : "ef-btn--outline";
    element.className = "ef-btn ef-btn--sm " + emphasis;
    element.textContent = button.label;
    element.addEventListener("click", () => {
      answerModal(button.takesInput ? input.value : button.value);
    });
    actions.appendChild(element);
  });

  document.getElementById("modal").hidden = false;

  /* 焦点：破坏性操作落在取消上（旧界面 gui.py:1244 的注释「默认按钮是取消：
     手滑按回车删不掉」），文本输入落在输入框上，其余落在主按钮上。 */
  const buttons = actions.querySelectorAll("button");
  if (spec.kind === "text") input.focus();
  else if (spec.danger) buttons[0].focus();
  else buttons[buttons.length - 1].focus();
}

function answerModal(value) {
  if (modalToken === null) return;
  const token = modalToken;
  modalToken = null;
  modalKind = null;
  document.getElementById("modal").hidden = true;
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.answer_prompt(token, value);
  }
}

/* 取消的值随类型不同：confirm 要 false，三态和文本要 null。统一成 null 的话
   confirm 会落到 Python 那边「问不出答案」的保守分支 —— 结果碰巧一样，但那是
   巧合不是设计。 */
function modalCancelValue() {
  return modalKind === "confirm" ? false : null;
}

/* Esc 等于取消。无边框窗口没有系统的关闭按钮可用，弹窗卡住就只能杀进程。
   遮罩点击不关 —— 手滑点到边上把一个「确定删除吗」关掉是无所谓，但把一个
   正在填名字的输入框关掉就白填了。 */
document.addEventListener("keydown", (event) => {
  if (modalToken === null) return;
  if (event.key === "Escape") {
    answerModal(modalCancelValue());
  } else if (event.key === "Enter" && modalKind === "text") {
    answerModal(document.getElementById("modal-input").value);
  }
});

window.showModal = showModal;
