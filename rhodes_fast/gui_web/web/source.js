"use strict";

/* 源码窗口。内容由页面自己向 Python 拉（popups.py 的 SourcePage.get_payload），
   不是 Python 趁窗口刚建出来往里推 —— 推的话要赌 loaded 事件还没触发，而窗口
   什么时候加载完不归那边管。等 pywebview 就绪再拉，先后顺序由这边保证。 */

function render(payload) {
  const title = String(payload.title || "");
  document.getElementById("source-title").textContent = title;
  document.title = "源码 · " + title;

  /* 统一换行：Windows 上写的文件是 CRLF，按 LF 切的话每行末尾挂着一个 CR，
     在等宽字体下是一格看不见的空白。 */
  const lines = String(payload.body || "").split("\r\n").join("\n").split("\n");
  // 文件末尾的换行会切出一个空行，那不是代码的一部分。
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();

  const list = document.getElementById("source-lines");
  list.textContent = "";
  const fragment = document.createDocumentFragment();
  lines.forEach((line) => {
    const item = document.createElement("li");
    /* textContent，绝不是 innerHTML：这是一份还没审过的代码，里面写一句
       <script> 的话，前者显示那几个字符，后者执行它。 */
    item.textContent = line;
    fragment.appendChild(item);
  });
  list.appendChild(fragment);
  document.getElementById("source-count").textContent = "// " + lines.length + " 行";
  document.getElementById("source-code").focus();
}

/* 不叫 close：经典脚本里顶层的函数声明会挂到 window 上，同名就把浏览器自带的
   window.close 盖掉了。 */
function closeWindow() {
  if (window.pywebview && window.pywebview.api) window.pywebview.api.close();
}

function loadSource() {
  window.pywebview.api.get_payload().then(render);
}

document.getElementById("source-close").addEventListener("click", closeWindow);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeWindow();
});

/* pywebviewready 可能在这段脚本跑之前就已经发过了（脚本在 body 末尾，但注入的
   时机不归页面管）。已经就绪就直接拉，不然等那个事件。 */
if (window.pywebview && window.pywebview.api && window.pywebview.api.get_payload) {
  loadSource();
} else {
  window.addEventListener("pywebviewready", loadSource);
}
