/* cmms 操作台共用 JS(零依賴、零 build;漸進增強 —— 沒有 JS 表單仍可用)。
   ① data-suggest 自動完成:任何 <input data-suggest="asset|part|person|task|org">
      輸入即向 /app/suggest 取建議(debounce 220ms),點選/Enter 填入,少打字(Jordan #1/#4)。
   ② FAB / dock 助理提示:點擊顯示「尚未接上 agent」小卡(不再是死按鈕)。 */
(function () {
  "use strict";

  /* ---- ① 自動完成 ---- */
  var box = null;      // 目前開啟的建議面板(全頁共用一個)
  var boxFor = null;   // 面板所屬 input
  var timer = null;
  var hot = -1;        // 鍵盤高亮 index

  function closeBox() {
    if (box) { box.remove(); box = null; boxFor = null; hot = -1; }
  }

  function fillTarget(input, value) {
    // data-fill="<id>" 的欄位(如唯讀「供應商機構代碼」)隨主欄選取自動帶入(#7g)。
    var id = input.getAttribute("data-fill");
    if (!id) return;
    var target = document.getElementById(id);
    if (target) target.value = value || "";
  }

  function pick(input, btn) {
    input.value = btn.getAttribute("data-value") || "";
    fillTarget(input, btn.getAttribute("data-extra"));  // 兩欄連動:extra → 唯讀companion
    closeBox();
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.focus();
  }

  function place(input) {
    var r = input.getBoundingClientRect();
    box.style.left = (r.left + window.scrollX) + "px";
    box.style.top = (r.bottom + window.scrollY + 4) + "px";
    box.style.minWidth = r.width + "px";
  }

  function render(input, html) {
    closeBox();
    if (!html || !html.trim()) return;
    box = document.createElement("div");
    box.className = "ac-box";
    box.innerHTML = html;
    if (!box.querySelector(".ac-item")) { box = null; return; }
    boxFor = input;
    document.body.appendChild(box);
    place(input);
    box.addEventListener("mousedown", function (e) {
      var btn = e.target.closest(".ac-item");
      if (btn) { e.preventDefault(); pick(input, btn); }
    });
  }

  var seq = 0;         // 回應排序守門:慢的舊回應不得覆蓋新查詢的建議

  function fetchSuggest(input) {
    var q = input.value.trim();
    if (!q) { closeBox(); return; }
    var my = ++seq;
    var url = "/app/suggest?kind=" + encodeURIComponent(input.dataset.suggest) +
      "&q=" + encodeURIComponent(q);
    fetch(url, { headers: { "Accept": "text/html" } })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (html) {
        if (my === seq && document.activeElement === input) render(input, html);
      })
      .catch(function () { /* 建議失敗不阻塞手動輸入 */ });
  }

  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!(input instanceof HTMLInputElement) || !input.dataset.suggest) return;
    // strict 欄一改即清除錯誤態(紅框 + 就地錯誤字):值變了就給重新驗證的機會
    if (input.hasAttribute("data-suggest-strict")) clearInvalid(input);
    // 主欄手動清空 → 一併清掉連動的唯讀 companion(供應商解除連結,#7g)
    if (input.getAttribute("data-fill") && !input.value.trim()) fillTarget(input, "");
    clearTimeout(timer);
    timer = setTimeout(function () { fetchSuggest(input); }, 220);
  });

  document.addEventListener("keydown", function (e) {
    if (!box || document.activeElement !== boxFor) return;
    var items = box.querySelectorAll(".ac-item");
    if (!items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      hot = e.key === "ArrowDown"
        ? (hot + 1) % items.length
        : (hot - 1 + items.length) % items.length;
      items.forEach(function (it, i) { it.classList.toggle("is-hot", i === hot); });
    } else if (e.key === "Enter") {
      // Enter 收斂:建議清單開啟且有項目 → 選取(有反白選反白,否則選第一筆),不誤送表單。
      // 清單關閉時本 handler 不觸發(見上方 guard)→ Enter 維持原生送出(會先過 strict 驗證)。
      e.preventDefault();
      pick(boxFor, hot >= 0 ? items[hot] : items[0]);
    } else if (e.key === "Escape") {
      closeBox();
    }
  });

  document.addEventListener("click", function (e) {
    if (box && !box.contains(e.target) && e.target !== boxFor) closeBox();
  });
  document.addEventListener("focusin", function (e) {
    if (box && e.target !== boxFor) closeBox();
  });
  window.addEventListener("resize", function () { if (box && boxFor) place(boxFor); });

  /* ---- ①b 嚴格驗證(data-suggest-strict):值必須=既存實體編號的寫入表單擋垃圾值 ----
     標記 data-suggest-strict 的欄,其所屬 form 送出時攔截:對每個非空 strict 欄打既有
     /app/suggest,檢查回傳建議有無「精確命中」(不分大小寫,比對建議的 data-value)。
     全過 → 真送出;任一查無 → 阻擋、紅框 + 就地錯誤字(文案由 data-strict-msg 屬性帶入,
     js 零硬編)、focus 首個壞欄。fetch 失敗(網路)→ 放行(伺服端仍守門,不因 JS 壞掉鎖死)。 */
  function fetchSuggestExact(input) {
    var val = input.value.trim();
    if (!val) return Promise.resolve(true);  // 空 strict 欄不在此驗(required / 伺服端處理)
    var url = "/app/suggest?kind=" + encodeURIComponent(input.dataset.suggest) +
      "&q=" + encodeURIComponent(val);
    return fetch(url, { headers: { "Accept": "text/html" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) return true;  // 伺服端非 2xx → 放行,交伺服端守門
        var tmp = document.createElement("div");
        tmp.innerHTML = html;
        var items = tmp.querySelectorAll(".ac-item");
        var needle = val.toUpperCase();
        for (var i = 0; i < items.length; i++) {
          if ((items[i].getAttribute("data-value") || "").toUpperCase() === needle) return true;
        }
        return false;
      })
      .catch(function () { return true; });  // 網路失敗 → 放行(不鎖死表單)
  }

  function setInvalid(input) {
    input.classList.add("is-invalid");
    if (!input._strictErrEl) {
      var el = document.createElement("p");
      el.className = "strict-err";
      el.textContent = input.getAttribute("data-strict-msg") || "";
      input.insertAdjacentElement("afterend", el);
      input._strictErrEl = el;
    }
    input._strictErrEl.hidden = false;
  }

  function clearInvalid(input) {
    input.classList.remove("is-invalid");
    if (input._strictErrEl) input._strictErrEl.hidden = true;
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.strictOk === "1") { form.dataset.strictOk = ""; return; }  // 驗證通過的再送
    var toCheck = [].slice.call(form.querySelectorAll("input[data-suggest-strict]"))
      .filter(function (i) { return i.value.trim(); });
    if (!toCheck.length) return;  // 無 strict 欄或全空 → 不攔
    e.preventDefault();
    closeBox();
    var btns = form.querySelectorAll("button[type=submit], input[type=submit]");
    btns.forEach(function (b) { b.disabled = true; });  // 驗證期間防雙擊
    Promise.all(toCheck.map(function (i) {
      return fetchSuggestExact(i).then(function (ok) { return { input: i, ok: ok }; });
    })).then(function (results) {
      btns.forEach(function (b) { b.disabled = false; });
      var bad = results.filter(function (r) { return !r.ok; });
      if (!bad.length) {
        form.dataset.strictOk = "1";  // 放行:再送一次時上方 guard 直接通過
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
        return;
      }
      bad.forEach(function (r) { setInvalid(r.input); });
      bad[0].input.focus();
    });
  }, true);

  /* ---- ② 助理 dock / FAB(ADR-020:dock → Hermes gateway 實接;對話落 DB,兩段式送出)----
     dock 內容(切換列 + 訊息 + 表單 + 結束鈕)由 /app/assistant/panel lazy-load,整頁導覽
     後自動還原當前對話。送出走 HTMX **兩段式**:
       phase 1(快)POST /app/assistant → 伺服端回 assistant_turn(user 泡泡 + pending 觸發器
         + OOB 切換列 + OOB conversation_id),beforeend 進 #assistant-conv;
       phase 2(慢)pending 元素 hx-trigger=load 自動 POST …/reply → outerHTML 換成回覆泡泡。
     使用者泡泡與等待動畫全由伺服端渲染(不再前端樂觀插入,避免重複);助理回覆由伺服端
     安全渲染(assistant_render)。切換 / 新對話 / 結束對話則以 hx-get/hx-post 換整個
     #assistant-panel(見模板),此處無需處理。此處只保留:開關 dock、清空輸入、捲到底。 */
  function dock() { return document.getElementById("assistant-dock"); }
  function conv() { return document.getElementById("assistant-conv"); }

  function closeDock() { var d = dock(); if (d) d.classList.remove("is-open"); }

  function scrollConvBottom() {
    var c = conv();
    if (c) c.scrollTop = c.scrollHeight;
  }

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-assistant-toggle]")) {
      var d = dock();
      if (d) d.classList.toggle("is-open");
      return;
    }
    if (e.target.closest("[data-assistant-close]")) { closeDock(); return; }
  });

  // 送出成功後清空輸入框(改在 afterRequest 做:phase 1 極快,user 泡泡由伺服端渲染;
  // 僅成功時清空,失敗保留輸入不留半殘、可重送)。只針對表單本身,不動 phase 2 觸發。
  document.addEventListener("htmx:afterRequest", function (e) {
    if (!(e.target.closest && e.target.closest("#assistant-form"))) return;
    if (e.detail && e.detail.successful === false) return;
    var input = e.target.closest("#assistant-form").querySelector("[name=message]");
    if (input) input.value = "";
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    // 捲到底讓最新訊息可見:整個 #assistant-panel 換掉(切換/新對話/結束),或 #assistant-conv
    // 內任何 swap(phase 1 beforeend 進 conv、phase 2 outerHTML 換 conv 內的 pending 泡泡)。
    var t = e.target;
    if (!t) return;
    if (t.id === "assistant-panel" || t.id === "assistant-conv" ||
        (t.closest && t.closest("#assistant-conv"))) {
      scrollConvBottom();
    }
  });

  /* ---- ③ 匯出中心:改過濾條件 → 清空預覽(提示重新試算,避免下載到舊 params 的 CSV)---- */
  function clearExportPreview(e) {
    if (!(e.target.closest && e.target.closest("[data-export-form]"))) return;
    var prev = document.getElementById("export-preview");
    if (prev) prev.innerHTML = "";
  }
  document.addEventListener("change", clearExportPreview);
  document.addEventListener("input", clearExportPreview);

  /* ---- ④ efc 確認故障碼 combobox(D6):設備故障碼清單太長無法逐一挑 → 多關鍵字搜尋。
     行為:空格分隔多關鍵字,每字須為 (code + 人話描述) 子字串(AND);下拉顯示人話描述、
     選取後存入 **code**;最終值必為清單成員或空(失焦時未選取的自由文字還原,不鑄假碼)。
     漸進增強:無 JS 時退回原生 <datalist>(基本子字串);本模組接管後移除 list 屬性。 ---- */
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function enhanceEfc(root) {
    if (root._efcReady) return;
    root._efcReady = true;
    var input = root.querySelector("input.efc-combo-input");   // 泛用:efc / storage_bin 共用
    var dataEl = root.querySelector(".efc-combo-data");
    if (!input || !dataEl) return;
    var options;
    try { options = JSON.parse(dataEl.textContent || "[]"); } catch (e) { return; }
    input.removeAttribute("list");            // 停用原生 datalist,改用自訂多關鍵字下拉
    var byCode = {};                          // 大寫 code → option(存在性/正規化查驗)
    options.forEach(function (o) {
      byCode[String(o.c).toUpperCase()] = o;
      o._h = (String(o.c) + " " + (o.d || "")).toLowerCase();  // 搜尋 haystack(code+描述)
    });

    var committed = input.value.trim();       // 已提交的有效 code(或空 / 既存退役碼)
    var menu = null, hot = -1;
    var addUrl = root.getAttribute("data-add-url");        // admin quick-add 端點(選填)
    var addLabel = root.getAttribute("data-add-label") || "";
    var addPending = false;                                // 防重複點擊

    function isValid(v) { return v === "" || byCode.hasOwnProperty(v.toUpperCase()); }

    function close() { if (menu) { menu.remove(); menu = null; } hot = -1; }

    function filtered() {
      var q = input.value.trim().toLowerCase();
      // 顯示中的值正好等於已選 code → 視為空查詢(展示全部,方便改選)
      if (q && committed && q === committed.toLowerCase()) q = "";
      if (!q) return options;
      var kws = q.split(/\s+/).filter(Boolean);
      return options.filter(function (o) {
        for (var k = 0; k < kws.length; k++) if (o._h.indexOf(kws[k]) === -1) return false;
        return true;
      });
    }

    function open() {
      close();
      var rows = filtered();
      menu = document.createElement("div");
      menu.className = "ac-box efc-menu";
      if (!rows.length) {
        var empty = document.createElement("div");
        empty.className = "efc-empty";
        empty.textContent = root.getAttribute("data-nomatch") || "";
        menu.appendChild(empty);
      } else {
        var usedLbl = root.getAttribute("data-used") || "";
        menu.innerHTML = rows.map(function (o) {
          return '<button type="button" class="ac-item" data-code="' + escHtml(o.c) + '">' +
            '<span class="ac-val">' + escHtml(o.c) + '</span>' +
            (o.d ? '<span class="ac-hint">' + escHtml(o.d) + '</span>' : '') +
            (o.u ? '<span class="efc-used">' + escHtml(usedLbl) + '</span>' : '') +
            '</button>';
        }).join("");
      }
      // admin quick-add:目前輸入值非空、非清單成員 → 末端追加「新增此值」列
      if (addUrl) {
        var q = input.value.trim();
        if (q && !byCode.hasOwnProperty(q.toUpperCase())) {
          var add = document.createElement("button");
          add.type = "button";
          add.className = "ac-item ac-add";
          add.innerHTML = escHtml(addLabel) + " “" + escHtml(q) + "”";
          menu.appendChild(add);
        }
      }
      root.appendChild(menu);
    }

    // admin quick-add:POST 新值到端點,成功即納入選項並選取;失敗於下拉內顯示錯誤(不清 input)
    function showAddError(msg) {
      if (!menu) return;
      var err = menu.querySelector(".ac-add-err");
      if (!err) { err = document.createElement("div"); err.className = "efc-empty ac-add-err"; menu.appendChild(err); }
      err.textContent = msg;
    }
    function quickAdd() {
      if (addPending || !addUrl) return;
      var q = input.value.trim();
      if (!q) return;
      addPending = true;
      fetch(addUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "code=" + encodeURIComponent(q),
        credentials: "same-origin"
      }).then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; },
          function () { return { ok: false, data: {} }; });
      }).then(function (res) {
        addPending = false;
        if (res.ok && res.data && res.data.ok) {
          var code = res.data.code;
          var o = { c: code };
          o._h = String(code).toLowerCase();
          options.push(o);
          byCode[String(code).toUpperCase()] = o;
          commit(code);
        } else {
          showAddError((res.data && res.data.error) || "error");
        }
      }).catch(function () {
        addPending = false;
        showAddError("network error");
      });
    }

    function commit(code) {
      input.value = code;
      committed = code;
      close();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    // 失焦收斂:輸入非清單成員且非空 → 還原到最後有效值(丟棄未選取的關鍵字);
    // 有效但大小寫不同 → 正規化到清單真值。保「值必為清單成員或空」不變式。
    function reconcile() {
      var v = input.value.trim();
      if (isValid(v)) {
        if (v && byCode[v.toUpperCase()]) v = byCode[v.toUpperCase()].c;
        committed = v; input.value = v;
      } else {
        input.value = committed;
      }
    }

    input.addEventListener("focus", open);
    input.addEventListener("input", open);
    input.addEventListener("blur", function () {
      // 延遲讓下拉的 mousedown 選取先發生,再收斂/收合
      setTimeout(function () {
        if (document.activeElement !== input) { close(); reconcile(); }
      }, 150);
    });
    input.addEventListener("keydown", function (e) {
      if (!menu) { if (e.key === "ArrowDown") { open(); e.preventDefault(); } return; }
      var items = menu.querySelectorAll(".ac-item");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!items.length) return;
        e.preventDefault();
        hot = e.key === "ArrowDown" ? (hot + 1) % items.length : (hot - 1 + items.length) % items.length;
        items.forEach(function (it, idx) { it.classList.toggle("is-hot", idx === hot); });
        items[hot].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        // 下拉有項目 → 選取(反白優先,否則第一筆),不誤送表單
        if (items.length) { e.preventDefault(); commit((hot >= 0 ? items[hot] : items[0]).getAttribute("data-code")); }
      } else if (e.key === "Escape") {
        e.preventDefault(); close();
      }
    });
    // 點選(mousedown 早於 blur → 選取不被失焦收斂搶先還原)
    root.addEventListener("mousedown", function (e) {
      var addBtn = e.target.closest(".ac-add");    // quick-add 列先攔(它也帶 .ac-item class)
      if (addBtn && menu && menu.contains(addBtn)) { e.preventDefault(); quickAdd(); return; }
      var btn = e.target.closest(".ac-item");
      if (btn && menu && menu.contains(btn)) { e.preventDefault(); commit(btn.getAttribute("data-code")); }
    });
  }

  function initEfcCombos() {
    var combos = document.querySelectorAll("[data-efc-combo]");
    for (var i = 0; i < combos.length; i++) enhanceEfc(combos[i]);
  }
  initEfcCombos();
  // 詳情頁 combobox 由伺服端整頁渲染;仍對 HTMX swap 再初始化(冪等:_efcReady 守門)
  document.addEventListener("htmx:afterSwap", initEfcCombos);

  /* ---- ⑤ 多值人名輸入(0031 多負責人):[data-multi] 容器內重複 <input>,「＋」複製空格、
     「×」移除(保留至少一格)。零依賴、事件委派;複製第一格為模板。 ---- */
  function multiRows(container) { return container.querySelectorAll(".multi-input__row"); }

  document.addEventListener("click", function (e) {
    var add = e.target.closest("[data-multi-add]");
    if (add) {
      var container = add.closest("[data-multi]");
      if (!container) return;
      var rows = multiRows(container);
      if (!rows.length) return;
      var clone = rows[0].cloneNode(true);
      var inp = clone.querySelector("input");
      if (inp) { inp.value = ""; if (inp.dataset) inp.dataset.autofilled = ""; }
      add.parentNode.insertBefore(clone, add);
      if (inp) inp.focus();
      return;
    }
    var rm = e.target.closest(".multi-input__rm");
    if (rm) {
      var c2 = rm.closest("[data-multi]");
      var row = rm.closest(".multi-input__row");
      if (!c2 || !row) return;
      if (multiRows(c2).length <= 1) {
        var only = row.querySelector("input");   // 最後一格不刪,只清空(保 name 送出)
        if (only) { only.value = ""; if (only.dataset) only.dataset.autofilled = ""; }
      } else {
        row.remove();
      }
      return;
    }
  });

  /* ---- ⑥ 設備負責人自動帶入(0031;報修表單)。來源欄帶 data-owner-autofill="<multi 容器 id>"。
     選定 / 改變 EID → 打 /app/asset-owner 取設備負責人清單 {"owners":[...]};**只在容器所有格皆
     空、或整組先前為自動帶入(容器 dataset.autofilled)時**,整組替換為每位負責人一格 —— 使用者
     手打的值絕不覆蓋。漸進增強:無 JS 時伺服端 report_submit 仍以設備負責人補齊(見 routes)。 ---- */
  function multiInputs(container) { return container.querySelectorAll(".multi-input__row input"); }
  function allEmptyOrAutofilled(container) {
    if (container.dataset.autofilled === "1") return true;
    var inputs = multiInputs(container);
    for (var i = 0; i < inputs.length; i++) if (inputs[i].value.trim()) return false;
    return true;
  }
  function setMultiValues(container, values) {
    var add = container.querySelector("[data-multi-add]");
    // 移除既有列,重建每值一列(複製首列樣式)
    var rows = multiRows(container);
    var template = rows.length ? rows[0].cloneNode(true) : null;
    for (var i = rows.length - 1; i >= 0; i--) rows[i].remove();
    var vals = values && values.length ? values : [""];
    vals.forEach(function (v) {
      var row = template ? template.cloneNode(true) : null;
      if (!row) return;
      var inp = row.querySelector("input");
      if (inp) { inp.value = v || ""; if (inp.dataset) inp.dataset.autofilled = "1"; }
      if (add) container.insertBefore(row, add); else container.appendChild(row);
    });
    container.dataset.autofilled = "1";
  }

  function clearAutofillFlag(e) {
    // 使用者對輸入格的任何互動 → 值歸使用者,清該格與其容器的自動帶入標記。
    var t = e.target;
    if (!(t instanceof HTMLInputElement)) return;
    if (t.dataset && t.dataset.autofilled === "1") t.dataset.autofilled = "";
    var c = t.closest("[data-multi]");
    if (c && c.dataset.autofilled === "1") c.dataset.autofilled = "";
  }
  document.addEventListener("input", clearAutofillFlag);
  document.addEventListener("change", clearAutofillFlag);

  document.addEventListener("change", function (e) {
    var src = e.target;
    if (!(src instanceof HTMLInputElement) || !src.hasAttribute("data-owner-autofill")) return;
    var container = document.getElementById(src.getAttribute("data-owner-autofill"));
    if (!container) return;
    var eid = src.value.trim();
    if (!eid) return;
    if (!allEmptyOrAutofilled(container)) return;  // 使用者已手打 → 不動
    fetch("/app/asset-owner?eid=" + encodeURIComponent(eid),
      { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (!allEmptyOrAutofilled(container)) return;  // 期間使用者已手打
        setMultiValues(container, data.owners || []);
      })
      .catch(function () { /* 自動帶入失敗不阻塞手動輸入(伺服端仍補網)*/ });
  });
})();
