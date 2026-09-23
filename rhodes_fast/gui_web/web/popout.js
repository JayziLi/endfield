"use strict";

/* 放大预览窗口。只做一件事：把 /preview.mjpg 接到 <img> 上，没画面时说一声。
   帧上已经烧好了检测框、FOV 圈、准心和底部那条状态文字（preview.py 的
   render_preview），这里不再叠任何东西。 */

const POPOUT_STREAM = "/preview.mjpg";
const media = document.getElementById("popout-media");
const empty = document.getElementById("popout-empty");

function attachStream() {
  // 带时间戳：重连时别拿到缓存里那条已经断掉的流。
  media.src = POPOUT_STREAM + "?t=" + Date.now();
}

/* 画面到底来没来，问 <img> 自己：multipart 流还没出第一帧时 naturalWidth 是 0。
   不用 load 事件 —— Chromium 对 multipart/x-mixed-replace 的 load 不可靠，
   主窗口那边实测过，改成轮询。半秒一次，零成本。 */
function syncEmpty() {
  empty.hidden = media.naturalWidth > 0;
}

/* 流断了（server 重启、连接被掐）之后 <img> 不会自己重连，画面会永远停在
   最后一帧上 —— 看起来跟「管线没发帧」一模一样。隔一秒重接一次。 */
media.addEventListener("error", () => { setTimeout(attachStream, 1000); });

/* 必须等 load 之后再接流：写在标记里（或者 load 之前接上）的话，那条不会结束的
   流会让 load 事件永远不触发，pywebview 就一直当这个窗口没起来。 */
window.addEventListener("load", () => {
  attachStream();
  setInterval(syncEmpty, 500);
});
