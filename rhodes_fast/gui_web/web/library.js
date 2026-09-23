"use strict";

/* 算法库的表格。设计系统没有 Table（规格第 7 节的两个缺口之一），用它的基础
   类拼：整行信号黄填充 + 前缘 2px ink 条表示选中 —— 规范 States 表的原话是
   「Selection is a block, not an underline」。

   列就是 GuiSession.library_rows() 那个六元组：
   显示名 / 标识 / 作者 / 源文件 / 导入时间 / 状态。 */

const LIBRARY_COLUMNS = ["显示名", "标识", "作者", "来源文件", "导入日期", "类别"];

/* 选中的是哪一行，按**标识**记，不按下标 —— 导入或删除之后表会重排，
   下标指向的就是另一个算法了。 */
let librarySelected = null;

function setLibrary(rows) {
  const body = document.getElementById("library-rows");
  if (!body) return;
  body.innerHTML = "";

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "ef-placeholder";
    empty.textContent = "算法库是空的。";
    body.appendChild(empty);
  }

  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = "ef-libraryrow";
    line.dataset.name = row[1];
    line.dataset.builtin = row[5] === "内置" ? "1" : "";
    row.forEach((cell, column) => {
      const box = document.createElement("span");
      box.className = "ef-libraryrow__cell";
      box.textContent = cell;
      box.title = cell;          /* 源文件名会被截断，悬停看全名 */
      if (column === 0) box.classList.add("ef-libraryrow__cell--name");
      line.appendChild(box);
    });
    line.addEventListener("click", () => selectAlgorithm(row[1]));
    body.appendChild(line);
  });

  /* 选中的那个可能刚被删掉。对不上就清掉选择，不然三个按钮会对着一个不存在
     的算法。 */
  if (librarySelected && !rows.some((row) => row[1] === librarySelected)) {
    librarySelected = null;
  }
  syncLibrarySelection();
}

function selectAlgorithm(name) {
  librarySelected = name;
  syncLibrarySelection();
}

function syncLibrarySelection() {
  let builtin = true;
  document.querySelectorAll(".ef-libraryrow").forEach((line) => {
    const on = line.dataset.name === librarySelected;
    line.classList.toggle("ef-libraryrow--active", on);
    if (on) builtin = Boolean(line.dataset.builtin);
  });

  /* 内置算法随程序分发，不能改名也不能删除。GuiSession 那边会拒绝，但按钮
     不置灰的话用户点了才被拒 —— 而拒绝理由跟「这个算法坏了」长得一样。
     查看源码同理：内置的不在注册表里，根本没有源文件可看。 */
  const nothing = librarySelected === null;
  ["library-source", "library-rename", "library-delete"].forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = nothing || builtin;
  });
}

function wireLibrary() {
  const head = document.getElementById("library-head");
  if (head) {
    head.innerHTML = "";
    LIBRARY_COLUMNS.forEach((title, column) => {
      const box = document.createElement("span");
      box.className = "ef-libraryrow__cell";
      box.textContent = title;
      if (column === 0) box.classList.add("ef-libraryrow__cell--name");
      head.appendChild(box);
    });
  }

  const call = (name, withSelection) => {
    if (!window.pywebview || !window.pywebview.api) return;
    if (withSelection) {
      if (librarySelected === null) return;
      window.pywebview.api[name](librarySelected);
    } else {
      window.pywebview.api[name]();
    }
  };

  const wire = (id, name, withSelection) => {
    const button = document.getElementById(id);
    if (button) button.addEventListener("click", () => call(name, withSelection));
  };
  wire("library-import", "import_algorithm", false);
  wire("library-refresh", "refresh_library", false);
  wire("library-source", "show_algorithm_source", true);
  wire("library-rename", "rename_algorithm", true);
  wire("library-delete", "delete_algorithm", true);

  syncLibrarySelection();
}

window.setLibrary = setLibrary;
window.wireLibrary = wireLibrary;
