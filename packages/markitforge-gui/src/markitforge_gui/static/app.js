/* SPDX-FileCopyrightText: 2026-present MarkitForge contributors
 * SPDX-License-Identifier: Apache-2.0
 */
/* MarkitForge GUI: vanilla JS, no dependencies.
 *
 * Safety rule for this file: text coming from documents or the server is
 * only ever inserted with textContent. The single exception is the preview
 * HTML, which the server has already sanitized (nh3 allow-list) and which the
 * page's Content-Security-Policy additionally prevents from running scripts.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const API_HEADERS = { "X-MarkitForge-Client": "1" };
  const MAX_PARALLEL = 3;
  const OPTIONS_KEY = "markitforge-gui.options";

  const state = {
    items: [],
    selectedKey: null,
    tab: "preview",
    maxBytes: 200 * 1024 * 1024,
    maxMb: 200,
  };
  let seq = 0;
  let running = 0;
  const queue = [];

  // ---------------------------------------------------------------- helpers

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children) {
      if (c === null || c === undefined) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function svgIcon(path) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", path);
    svg.append(p);
    return svg;
  }

  function formatBytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function countWords(text) {
    const m = text.match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu);
    return m ? m.length : 0;
  }

  let liveTimer = null;
  function announce(message) {
    const live = $("live");
    live.textContent = "";
    clearTimeout(liveTimer);
    liveTimer = setTimeout(() => { live.textContent = message; }, 50);
  }

  let toastTimer = null;
  function toast(message) {
    const t = $("toast");
    t.textContent = message;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2200);
  }

  async function readJson(res) {
    try {
      return await res.json();
    } catch (_) {
      if (res.status === 413) {
        return { error: `This file is too large. The limit is ${state.maxMb} MB.` };
      }
      return { error: `MarkitForge sent an unexpected answer (error ${res.status}).` };
    }
  }

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { ...API_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await readJson(res);
    return { ok: res.ok, status: res.status, data };
  }

  const OFFLINE_ERROR = {
    error: "Could not reach MarkitForge. Is it still running?",
    hint: "If you closed the MarkitForge window or terminal, start it again.",
  };

  // ---------------------------------------------------------------- options

  const optionInputs = {
    page_markers: $("opt-page-markers"),
    table_format: $("opt-table-format"),
    pdf_backend: $("opt-pdf-backend"),
    include_hidden_sheets: $("opt-hidden-sheets"),
    include_headers_footers: $("opt-headers-footers"),
    keep_data_uris: $("opt-keep-images"),
  };

  function getOptions() {
    const out = {};
    for (const [key, input] of Object.entries(optionInputs)) {
      out[key] = input.type === "checkbox" ? input.checked : input.value;
    }
    return out;
  }

  function loadOptions() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(OPTIONS_KEY) || "null"); } catch (_) { saved = null; }
    if (!saved || typeof saved !== "object") return;
    for (const [key, input] of Object.entries(optionInputs)) {
      if (!(key in saved)) continue;
      if (input.type === "checkbox") input.checked = Boolean(saved[key]);
      else if ([...input.options].some((o) => o.value === saved[key])) input.value = saved[key];
    }
  }

  function saveOptions() {
    try { localStorage.setItem(OPTIONS_KEY, JSON.stringify(getOptions())); } catch (_) { /* ignore */ }
  }

  // ---------------------------------------------------------------- items

  function addItem(kind, name, extra) {
    const item = { key: `i${++seq}`, kind, name, status: "queued", data: null, error: null, view: null, ...extra };
    state.items.push(item);
    renderList();
    return item;
  }

  function findItem(key) { return state.items.find((i) => i.key === key) || null; }
  function selectedItem() { return findItem(state.selectedKey); }

  function enqueue(item) {
    item.status = "queued";
    item.error = null;
    queue.push(item);
    pump();
  }

  function pump() {
    while (running < MAX_PARALLEL && queue.length) {
      const item = queue.shift();
      running++;
      const job = item.kind === "url" ? convertUrl(item) : convertFile(item);
      job.finally(() => { running--; pump(); });
    }
  }

  function markConverting(item) {
    item.status = "converting";
    updateItem(item);
  }

  function succeed(item, data) {
    item.status = "done";
    item.data = data;
    item.view = { markdown: data.markdown || "", html: data.html || "", pages: "" };
    announce(`${item.name} is ready.`);
    updateItem(item);
    const current = selectedItem();
    if (!current || (current !== item && current.status !== "done")) select(item.key, false);
  }

  function fail(item, err) {
    item.status = "error";
    item.error = err && err.error ? err : { error: "Something went wrong." };
    announce(`${item.name} could not be converted. ${item.error.error}`);
    updateItem(item);
    if (!selectedItem()) select(item.key, false);
  }

  async function convertFile(item) {
    if (item.file.size > state.maxBytes) {
      fail(item, {
        error: `This file is too large. The limit is ${state.maxMb} MB.`,
        hint: "Start MarkitForge with --max-upload-mb to raise the limit.",
      });
      return;
    }
    markConverting(item);
    const form = new FormData();
    form.append("file", item.file, item.file.name);
    form.append("options", JSON.stringify(item.options));
    let res;
    try {
      res = await fetch("/api/convert", { method: "POST", headers: API_HEADERS, body: form });
    } catch (_) {
      fail(item, OFFLINE_ERROR);
      return;
    }
    const data = await readJson(res);
    if (res.ok) succeed(item, data);
    else fail(item, data);
  }

  async function convertUrl(item) {
    markConverting(item);
    let r;
    try {
      r = await postJson("/api/convert-url", { url: item.url, options: item.options });
    } catch (_) {
      fail(item, OFFLINE_ERROR);
      return;
    }
    if (r.ok) {
      item.name = r.data.title || item.name;
      succeed(item, r.data);
    } else {
      fail(item, r.data);
    }
  }

  function addFiles(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    const options = getOptions();
    let first = null;
    for (const file of files) {
      const item = addItem("file", file.name || "file", { file, options });
      first = first || item;
      enqueue(item);
    }
    if (!selectedItem() || selectedItem().status !== "done") select(first.key, false);
    announce(files.length === 1 ? `Converting ${files[0].name}…` : `Converting ${files.length} files…`);
  }

  async function openMarkdownFile(file) {
    const item = addItem("md", file.name || "document.md", {});
    select(item.key, false);
    if (file.size > state.maxBytes) {
      fail(item, { error: `This file is too large. The limit is ${state.maxMb} MB.` });
      return;
    }
    markConverting(item);
    let text;
    try {
      text = await file.text();
    } catch (_) {
      fail(item, { error: "This file could not be read." });
      return;
    }
    let r;
    try {
      r = await postJson("/api/render", { markdown: text });
    } catch (_) {
      fail(item, OFFLINE_ERROR);
      return;
    }
    if (!r.ok) { fail(item, r.data); return; }
    succeed(item, {
      id: null,
      filename: item.name,
      md_filename: item.name,
      title: item.name,
      markdown: text,
      html: r.data.html,
      metadata: {},
      pages: [],
      warnings: [],
      assets: [],
    });
  }

  // ---------------------------------------------------------------- file list

  const STATUS_TEXT = { queued: "Waiting…", converting: "Converting…", done: "Ready", error: "Could not convert" };

  function statusIcon(item) {
    const wrap = el("span", { class: "status-icon", "aria-hidden": "true" });
    if (item.status === "converting") wrap.append(el("span", { class: "mini-spinner" }));
    else if (item.status === "done") wrap.append(svgIcon("M5 12.5l4.5 4.5L19 7.5"));
    else if (item.status === "error") wrap.append(svgIcon("M12 7v6m0 4h.01M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z"));
    else wrap.append(svgIcon("M12 7v5l3 2M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z"));
    return wrap;
  }

  function itemNode(item) {
    let status = STATUS_TEXT[item.status];
    if (item.status === "error" && item.error) status = item.error.error;
    if (item.status === "done" && item.kind === "md") status = "Opened";
    const btn = el(
      "button",
      {
        type: "button",
        class: `file-item ${item.status}`,
        "aria-current": item.key === state.selectedKey ? "true" : "false",
        "data-key": item.key,
        title: item.kind === "url" ? item.url : item.name,
        onclick: () => select(item.key, true),
      },
      statusIcon(item),
      el("span", { class: "file-name", text: item.name }),
      el("span", { class: "file-status", text: status }),
    );
    return el("li", { id: `item-${item.key}` }, btn);
  }

  function renderList() {
    const list = $("file-list");
    list.replaceChildren(...state.items.map(itemNode));
    $("files-empty").hidden = state.items.length > 0;
    $("clear-btn").hidden = state.items.length === 0 || running > 0 || queue.length > 0;
    updateZip();
  }

  function updateItem(item) {
    const old = $(`item-${item.key}`);
    if (old) old.replaceWith(itemNode(item));
    else renderList();
    $("clear-btn").hidden = state.items.length === 0 || running > 0 || queue.length > 0;
    updateZip();
    if (item.key === state.selectedKey) renderViewer();
  }

  function updateZip() {
    const ids = state.items.filter((i) => i.status === "done" && i.data && i.data.id).map((i) => i.data.id);
    const zip = $("zip-btn");
    if (ids.length) {
      zip.href = `/api/zip?ids=${encodeURIComponent(ids.join(","))}`;
      zip.removeAttribute("aria-disabled");
      zip.removeAttribute("tabindex");
    } else {
      zip.href = "/api/zip";
      zip.setAttribute("aria-disabled", "true");
      zip.setAttribute("tabindex", "-1");
    }
  }

  function select(key, focusViewer) {
    state.selectedKey = key;
    for (const btn of document.querySelectorAll(".file-item")) {
      btn.setAttribute("aria-current", btn.dataset.key === key ? "true" : "false");
    }
    renderViewer();
    if (focusViewer && window.matchMedia("(max-width: 860px)").matches) {
      $("viewer").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  // ---------------------------------------------------------------- viewer

  function showOnly(id) {
    for (const part of ["viewer-empty", "viewer-status", "viewer-error", "viewer-content"]) {
      $(part).hidden = part !== id;
    }
  }

  function renderViewer() {
    const item = selectedItem();
    if (!item) { showOnly("viewer-empty"); return; }
    if (item.status === "queued" || item.status === "converting") {
      showOnly("viewer-status");
      $("viewer-status-text").textContent =
        item.status === "queued" ? `Waiting to convert ${item.name}…` : `Converting ${item.name}…`;
      return;
    }
    if (item.status === "error") {
      showOnly("viewer-error");
      const err = item.error || {};
      $("error-title").textContent = `Couldn't convert ${item.name}`;
      $("error-message").textContent = err.error || "Something went wrong.";
      $("error-hint").hidden = !err.hint;
      $("error-hint").textContent = err.hint || "";
      $("error-hint").classList.toggle("code", /pip install|\n/.test(err.hint || ""));
      $("error-detail-wrap").hidden = !err.detail;
      $("error-detail").textContent = err.detail || "";
      $("retry-btn").hidden = item.kind === "md";
      return;
    }
    showOnly("viewer-content");
    renderResult(item);
  }

  function kindLabel(pages) {
    if (!pages.length) return "page";
    return pages[0].kind || "page";
  }

  function renderResult(item) {
    const data = item.data;
    const view = item.view;
    const pages = data.pages || [];

    $("doc-title").textContent = data.title || item.name;
    const bits = [];
    if (item.kind === "url") bits.push(item.url);
    else if (item.kind === "file") bits.push(`${item.name} → ${data.md_filename}`);
    else bits.push("Markdown file");
    if (pages.length) bits.push(`${pages.length} ${kindLabel(pages)}${pages.length === 1 ? "" : "s"}`);
    bits.push(`${countWords(view.markdown).toLocaleString()} words`);
    $("doc-sub").textContent = bits.join(" · ");

    // Preview (server-sanitized HTML).
    const preview = $("preview");
    if (view.markdown.trim()) {
      preview.innerHTML = view.html;
      for (const table of preview.querySelectorAll("table")) {
        if (table.parentElement && table.parentElement.classList.contains("table-wrap")) continue;
        const wrap = el("div", { class: "table-wrap" });
        table.replaceWith(wrap);
        wrap.append(table);
      }
    } else {
      preview.replaceChildren(el("p", { class: "empty-note", text: "This file did not contain any text MarkitForge could extract." }));
    }

    // Markdown source with line numbers.
    const lines = view.markdown.split("\n");
    const numbers = new Array(lines.length);
    for (let i = 0; i < lines.length; i++) numbers[i] = i + 1;
    $("gutter").textContent = numbers.join("\n");
    $("source").textContent = view.markdown;

    // Selection banner and download link.
    $("selection-banner").hidden = !view.pages;
    $("selection-text").textContent = view.pages
      ? `Showing ${kindLabel(pages)}${/^\d+$/.test(view.pages) ? "" : "s"} ${view.pages} (out of ${pages.length})`
      : "";
    const dl = $("download-btn");
    if (dl.dataset.blobUrl) { URL.revokeObjectURL(dl.dataset.blobUrl); delete dl.dataset.blobUrl; }
    if (data.id) {
      dl.href = `/api/download/${encodeURIComponent(data.id)}` + (view.pages ? `?pages=${encodeURIComponent(view.pages)}` : "");
      dl.removeAttribute("download");
    } else {
      const blobUrl = URL.createObjectURL(new Blob([view.markdown], { type: "text/markdown;charset=utf-8" }));
      dl.dataset.blobUrl = blobUrl;
      dl.href = blobUrl;
      dl.setAttribute("download", data.md_filename || "document.md");
    }
    $("download-label").textContent = view.pages ? "Download selection (.md)" : "Download .md";

    renderDetails(item);
  }

  function metaValueNode(value) {
    const none = () => el("span", { class: "muted", text: "—" });
    if (value === null || value === undefined || value === "") return none();
    if (Array.isArray(value) && value.every((v) => v === null || typeof v !== "object")) {
      return value.length ? document.createTextNode(value.join(", ")) : none();
    }
    if (typeof value === "boolean") return document.createTextNode(value ? "Yes" : "No");
    if (typeof value === "object") return el("pre", { text: JSON.stringify(value, null, 2) });
    return document.createTextNode(String(value));
  }

  function prettyKey(key) {
    const s = String(key).replace(/[_-]+/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function renderDetails(item) {
    const data = item.data;
    const warnings = data.warnings || [];
    const pages = data.pages || [];

    // Warnings, highlighted, plus a badge on the tab.
    $("warnings-section").hidden = warnings.length === 0;
    $("warnings-list").replaceChildren(...warnings.map((w) => el("li", { text: w })));
    $("warn-badge").hidden = warnings.length === 0;
    $("warn-badge").textContent = String(warnings.length);
    $("warn-badge").setAttribute("aria-label", `${warnings.length} warning${warnings.length === 1 ? "" : "s"}`);

    // Pages.
    $("pages-section").hidden = pages.length === 0 || !data.id;
    $("pages-title").textContent = pages.length ? `${prettyKey(kindLabel(pages))}s (${pages.length})` : "Pages";
    $("range-input").value = item.view.pages || "";
    $("range-error").hidden = true;
    const kind = prettyKey(kindLabel(pages));
    $("pages-list").replaceChildren(
      ...pages.map((p) =>
        el(
          "li",
          null,
          el("span", { class: "page-label", text: `${prettyKey(p.kind || kind)} ${p.index}` }),
          el("span", { class: "page-title", text: p.title || "" }),
          el("button", {
            type: "button",
            class: "btn small",
            text: "View",
            "aria-label": `View ${p.kind || "page"} ${p.index}`,
            onclick: () => applyRange(item, String(p.index), true),
          }),
        ),
      ),
    );

    // Metadata.
    const meta = data.metadata || {};
    const rows = Object.entries(meta).map(([k, v]) =>
      el("tr", null, el("th", { scope: "row", text: prettyKey(k) }), el("td", null, metaValueNode(v))),
    );
    $("meta-table").tBodies[0].replaceChildren(...rows);
    $("meta-table").hidden = rows.length === 0;
    $("meta-empty").hidden = rows.length !== 0;

    // Assets.
    const assets = data.assets || [];
    $("assets-section").hidden = assets.length === 0;
    $("assets-table").tBodies[0].replaceChildren(
      ...assets.map((a) =>
        el(
          "tr",
          null,
          el("td", { text: a.name || "" }),
          el("td", { text: a.mime_type || "" }),
          el("td", { text: formatBytes(a.size) }),
          el("td", { text: a.page === null || a.page === undefined ? "" : String(a.page) }),
          el("td", { text: a.description || "" }),
        ),
      ),
    );
  }

  async function applyRange(item, spec, switchToPreview) {
    spec = (spec || "").trim();
    const errorBox = $("range-error");
    errorBox.hidden = true;
    let r;
    try {
      r = await postJson("/api/render", { id: item.data.id, pages: spec || null });
    } catch (_) {
      errorBox.textContent = OFFLINE_ERROR.error;
      errorBox.hidden = false;
      return;
    }
    if (!r.ok) {
      errorBox.textContent = r.data.error || "That page range was not understood.";
      errorBox.hidden = false;
      return;
    }
    item.view = { markdown: r.data.markdown, html: r.data.html, pages: spec };
    renderViewer();
    announce(spec ? `Showing pages ${spec}.` : "Showing the whole document.");
    if (switchToPreview) setTab("preview", false);
  }

  // ---------------------------------------------------------------- tabs

  const TABS = ["preview", "markdown", "details"];

  function setTab(name, focus) {
    state.tab = name;
    for (const t of TABS) {
      const tab = $(`tab-${t}`);
      const on = t === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      $(`panel-${t}`).hidden = !on;
    }
    if (focus) $(`tab-${name}`).focus();
  }

  function initTabs() {
    for (const t of TABS) {
      const tab = $(`tab-${t}`);
      tab.addEventListener("click", () => setTab(t, false));
      tab.addEventListener("keydown", (e) => {
        const i = TABS.indexOf(t);
        let next = null;
        if (e.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
        else if (e.key === "ArrowLeft") next = TABS[(i - 1 + TABS.length) % TABS.length];
        else if (e.key === "Home") next = TABS[0];
        else if (e.key === "End") next = TABS[TABS.length - 1];
        if (next) { e.preventDefault(); setTab(next, true); }
      });
    }
  }

  // ---------------------------------------------------------------- clipboard

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const area = el("textarea", { readonly: true, "aria-hidden": "true" });
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      area.remove();
      return ok;
    }
  }

  // ---------------------------------------------------------------- drag & drop

  function hasFiles(e) {
    return e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  }

  function initDragDrop() {
    let depth = 0;
    const overlay = $("drop-overlay");
    const zone = $("dropzone");
    window.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      overlay.hidden = false;
      document.body.classList.add("dragging");
    });
    window.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });
    window.addEventListener("dragleave", (e) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) { overlay.hidden = true; document.body.classList.remove("dragging"); }
    });
    window.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      overlay.hidden = true;
      document.body.classList.remove("dragging");
      zone.classList.remove("over");
      addFiles(e.dataTransfer.files);
    });
  }

  // ---------------------------------------------------------------- wiring

  function init() {
    loadOptions();
    for (const input of Object.values(optionInputs)) input.addEventListener("change", saveOptions);
    initTabs();
    initDragDrop();

    fetch("/api/info", { headers: API_HEADERS })
      .then((r) => (r.ok ? r.json() : null))
      .then((info) => {
        if (!info) return;
        state.maxBytes = info.max_upload_bytes;
        state.maxMb = info.max_upload_mb;
        $("limit-mb").textContent = String(Math.round(info.max_upload_mb));
      })
      .catch(() => {});

    const fileInput = $("file-input");
    $("choose-btn").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      addFiles(fileInput.files);
      fileInput.value = "";
    });

    const mdInput = $("open-md-input");
    $("open-md-btn").addEventListener("click", () => mdInput.click());
    mdInput.addEventListener("change", () => {
      for (const f of mdInput.files) openMarkdownFile(f);
      mdInput.value = "";
    });

    $("url-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("url-input");
      const url = input.value.trim();
      if (!url) { input.focus(); return; }
      const item = addItem("url", url, { url, options: getOptions() });
      select(item.key, false);
      enqueue(item);
      input.value = "";
    });

    $("retry-btn").addEventListener("click", () => {
      const item = selectedItem();
      if (!item || item.kind === "md") return;
      item.options = getOptions();
      enqueue(item);
      updateItem(item);
    });

    $("copy-btn").addEventListener("click", async () => {
      const item = selectedItem();
      if (!item || !item.view) return;
      const ok = await copyText(item.view.markdown);
      toast(ok ? "Markdown copied to the clipboard" : "Could not copy. Select the text in the Markdown tab instead.");
    });

    $("zip-btn").addEventListener("click", (e) => {
      if ($("zip-btn").getAttribute("aria-disabled") === "true") e.preventDefault();
    });

    $("clear-btn").addEventListener("click", () => {
      state.items = state.items.filter((i) => i.status === "queued" || i.status === "converting");
      state.selectedKey = null;
      renderList();
      renderViewer();
      announce("List cleared.");
    });

    $("range-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const item = selectedItem();
      if (item && item.data && item.data.id) applyRange(item, $("range-input").value, false);
    });
    $("show-all-btn").addEventListener("click", () => {
      const item = selectedItem();
      if (item && item.data && item.data.id) applyRange(item, "", false);
    });

    // Links inside the preview open in a new browser tab, never replacing the app.
    $("preview").addEventListener("click", (e) => {
      const a = e.target.closest("a[href]");
      if (!a) return;
      const href = a.getAttribute("href") || "";
      if (href.startsWith("#")) return;
      e.preventDefault();
      if (/^(https?:|mailto:)/i.test(href)) window.open(href, "_blank", "noopener,noreferrer");
    });

    // Keyboard: Up/Down moves through the file list.
    $("file-list").addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const buttons = [...document.querySelectorAll(".file-item")];
      const i = buttons.indexOf(document.activeElement);
      if (i < 0) return;
      e.preventDefault();
      const next = buttons[e.key === "ArrowDown" ? Math.min(buttons.length - 1, i + 1) : Math.max(0, i - 1)];
      next.focus();
      select(next.dataset.key, false);
      const again = document.querySelector(`.file-item[data-key="${next.dataset.key}"]`);
      if (again) again.focus();
    });

    renderList();
    renderViewer();
  }

  init();
})();
