/* SPDX-FileCopyrightText: 2026-present MarkitForge contributors
 * SPDX-License-Identifier: Apache-2.0
 */
/* MarkitForge GUI: vanilla JS, no dependencies.
 *
 * Safety rule for this file: text coming from documents or the server is
 * only ever inserted with textContent (or as a textarea value). The single
 * exception is the preview HTML, which the server has already sanitized (nh3
 * allow-list) and which the page's Content-Security-Policy additionally
 * prevents from running scripts.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const tokenMeta = document.querySelector('meta[name="mf-token"]');
  const TOKEN = tokenMeta ? tokenMeta.getAttribute("content") || "" : "";
  const API_HEADERS = { "X-MarkitForge-Client": "1", "X-MarkitForge-Token": TOKEN };
  const MAX_PARALLEL = 3;
  const POLL_MS = 400;
  const OPTIONS_KEY = "markitforge-gui.options";
  const THEME_KEY = "markitforge-gui.theme";
  const SPLIT_KEY = "markitforge-gui.split";
  const SKIP_NAMES = new Set(["thumbs.db", "desktop.ini"]);

  const state = {
    items: [],
    selectedKey: null,
    tab: "preview",
    split: false,
    maxBytes: 200 * 1024 * 1024,
    maxMb: 200,
    maxEditBytes: 32 * 1024 * 1024,
    maxFolderFiles: 500,
    historyEnabled: false,
    llm: null,
  };
  let seq = 0;
  let running = 0;
  const queue = [];
  const wideScreen = window.matchMedia("(min-width: 1100px)");

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

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function withToken(url) {
    return url + (url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN);
  }

  function debounce(fn, ms) {
    let timer = null;
    const wrapped = (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => { timer = null; fn(...args); }, ms);
    };
    wrapped.cancel = () => { clearTimeout(timer); timer = null; };
    return wrapped;
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
    toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
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

  async function api(method, url, body) {
    const opts = { method, headers: { ...API_HEADERS } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await readJson(res);
    return { ok: res.ok, status: res.status, data };
  }
  const postJson = (url, body) => api("POST", url, body);
  const getJson = (url) => api("GET", url);

  const OFFLINE_ERROR = {
    error: "Could not reach MarkitForge. Is it still running?",
    hint: "If you closed the MarkitForge window or terminal, start it again.",
  };

  function shortDate(seconds) {
    try {
      return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch (_) {
      return "";
    }
  }

  // ---------------------------------------------------------------- theme

  function applyTheme(choice) {
    if (choice === "light" || choice === "dark") document.documentElement.setAttribute("data-theme", choice);
    else document.documentElement.removeAttribute("data-theme");
    for (const btn of document.querySelectorAll("[data-theme-choice]")) {
      const on = btn.dataset.themeChoice === (choice || "system");
      btn.setAttribute("aria-checked", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
    }
  }

  function initTheme() {
    let saved = "system";
    try { saved = localStorage.getItem(THEME_KEY) || "system"; } catch (_) { saved = "system"; }
    applyTheme(saved);
    const buttons = [...document.querySelectorAll("[data-theme-choice]")];
    const choose = (choice, focus) => {
      applyTheme(choice);
      try {
        if (choice === "system") localStorage.removeItem(THEME_KEY);
        else localStorage.setItem(THEME_KEY, choice);
      } catch (_) { /* storage blocked */ }
      if (focus) buttons.find((b) => b.dataset.themeChoice === choice).focus();
      announce(`Theme: ${choice === "system" ? "follow the system" : choice}.`);
    };
    for (const btn of buttons) {
      btn.addEventListener("click", () => choose(btn.dataset.themeChoice, false));
      btn.addEventListener("keydown", (e) => {
        const i = buttons.indexOf(btn);
        let next = null;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") next = buttons[(i + 1) % buttons.length];
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = buttons[(i - 1 + buttons.length) % buttons.length];
        if (next) { e.preventDefault(); choose(next.dataset.themeChoice, true); }
      });
    }
  }

  // ---------------------------------------------------------------- options

  const optionInputs = {
    page_markers: $("opt-page-markers"),
    table_format: $("opt-table-format"),
    pdf_backend: $("opt-pdf-backend"),
    include_hidden_sheets: $("opt-hidden-sheets"),
    include_headers_footers: $("opt-headers-footers"),
    keep_data_uris: $("opt-keep-images"),
    ocr_scanned_pages: $("opt-ocr"),
    ocr_engine: $("opt-ocr-engine"),
    describe_images: $("opt-describe-images"),
    transcribe_formulas: $("opt-formulas"),
    extract_images: $("opt-extract-images"),
  };

  function getOptions() {
    const out = {};
    for (const [key, input] of Object.entries(optionInputs)) {
      out[key] = input.type === "checkbox" ? input.checked : input.value;
    }
    return out;
  }

  function setOptions(values) {
    if (!values || typeof values !== "object") return;
    for (const [key, input] of Object.entries(optionInputs)) {
      if (!(key in values)) continue;
      if (input.type === "checkbox") input.checked = Boolean(values[key]);
      else if ([...input.options].some((o) => o.value === values[key])) input.value = values[key];
    }
  }

  function loadOptions(defaults) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(OPTIONS_KEY) || "null"); } catch (_) { saved = null; }
    setOptions(defaults || {});
    setOptions(saved);
    syncAiControls();
  }

  function saveOptions() {
    try { localStorage.setItem(OPTIONS_KEY, JSON.stringify(getOptions())); } catch (_) { /* ignore */ }
    syncAiControls();
  }

  function usesAi(options) {
    return Boolean(
      options.describe_images || options.transcribe_formulas ||
      (options.ocr_scanned_pages && options.ocr_engine !== "tesseract"),
    );
  }

  // ---------------------------------------------------------------- AI provider

  function selectedProvider() {
    return $("llm-openai").checked ? "openai" : "local";
  }

  function syncAiControls() {
    $("opt-ocr-engine").disabled = !$("opt-ocr").checked;
    const provider = selectedProvider();
    $("llm-key-row").hidden = provider !== "openai";
    const note = $("ai-privacy");
    let host = "";
    try { host = new URL($("llm-base-url").value).hostname; } catch (_) { host = ""; }
    const loopback = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|::1)$/i.test(host) || /\.localhost$/i.test(host);
    note.classList.toggle("warn", provider === "openai" || !loopback);
    if (provider === "local" && loopback) {
      note.textContent = "Local (Ollama): pages and pictures stay on this computer. AI options only run when you tick one below.";
    } else {
      note.textContent = `When an AI option is on, page images and pictures from your files are sent to ${host || "the AI service"}. Choose Local (Ollama) to keep files on this computer.`;
    }
  }

  function renderLlm(info) {
    state.llm = info;
    $(info.provider === "openai" ? "llm-openai" : "llm-local").checked = true;
    $("llm-base-url").value = info.base_url || "";
    $("llm-model").value = info.model || "";
    $("llm-install").hidden = Boolean(info.openai_available);
    const keyState = $("llm-key-state");
    if (info.key_source === "session") keyState.textContent = "Key set for this session.";
    else if (info.key_source === "env") keyState.textContent = `Key taken from the ${info.key_env} environment variable.`;
    else keyState.textContent = "No key set yet.";
    keyState.classList.toggle("muted", !info.key_source);
    $("llm-forget-key").hidden = info.key_source !== "session";
    syncAiControls();
  }

  function llmStatus(message, isError) {
    const box = $("llm-status");
    box.textContent = message;
    box.classList.toggle("field-error", Boolean(isError));
  }

  async function saveLlm(extra) {
    const body = {
      provider: selectedProvider(),
      base_url: $("llm-base-url").value.trim(),
      model: $("llm-model").value.trim(),
      ...extra,
    };
    const key = $("llm-key").value.trim();
    if (body.provider === "openai" && key) body.api_key = key;
    let r;
    try { r = await api("PUT", "/api/llm", body); } catch (_) { llmStatus(OFFLINE_ERROR.error, true); return false; }
    $("llm-key").value = "";
    if (!r.ok) { llmStatus(r.data.error || "Could not save the AI settings.", true); return false; }
    renderLlm(r.data);
    return true;
  }

  function initLlm() {
    for (const id of ["llm-local", "llm-openai"]) {
      $(id).addEventListener("change", () => {
        const defaults = (state.llm && state.llm.defaults) || {};
        if (selectedProvider() === "local" && defaults.local_base_url) {
          $("llm-base-url").value = defaults.local_base_url;
          if (!$("llm-model").value || $("llm-model").value === "gpt-4o-mini") $("llm-model").value = defaults.local_model || "";
        } else if (selectedProvider() === "openai" && /localhost:11434/.test($("llm-base-url").value)) {
          $("llm-base-url").value = "https://api.openai.com/v1";
          $("llm-model").value = "gpt-4o-mini";
        }
        syncAiControls();
      });
    }
    $("llm-base-url").addEventListener("input", syncAiControls);
    $("llm-save").addEventListener("click", async () => {
      if (await saveLlm()) llmStatus("Saved.", false);
    });
    $("llm-forget-key").addEventListener("click", async () => {
      if (await saveLlm({ clear_key: true })) llmStatus("The key was forgotten.", false);
    });
    $("llm-test").addEventListener("click", async () => {
      llmStatus("Testing…", false);
      if (!(await saveLlm())) return;
      let r;
      try { r = await postJson("/api/llm/test", {}); } catch (_) { llmStatus(OFFLINE_ERROR.error, true); return; }
      if (!r.ok) { llmStatus(r.data.error || "The test failed.", true); return; }
      llmStatus(r.data.message || "Connected.", false);
      $("llm-models").replaceChildren(...(r.data.models || []).map((m) => el("option", { value: m })));
    });
    getJson("/api/llm").then((r) => { if (r.ok) renderLlm(r.data); }).catch(() => {});
  }

  // ---------------------------------------------------------------- items

  function addItem(kind, name, extra) {
    const item = {
      key: `i${++seq}`, kind, name, status: "queued", data: null, error: null, view: null,
      jobId: null, progress: null, cancelRequested: false, original: null, ...extra,
    };
    state.items.push(item);
    renderList();
    return item;
  }

  function findItem(key) { return state.items.find((i) => i.key === key) || null; }
  function selectedItem() { return findItem(state.selectedKey); }
  const isBusy = (item) => ["queued", "uploading", "converting"].includes(item.status);

  function enqueue(item) {
    item.status = "queued";
    item.error = null;
    item.progress = null;
    item.jobId = null;
    item.cancelRequested = false;
    queue.push(item);
    pump();
  }

  function pump() {
    while (running < MAX_PARALLEL && queue.length) {
      const item = queue.shift();
      if (item.status !== "queued") continue;
      running++;
      const job = item.kind === "url" ? convertUrl(item) : convertFile(item);
      job.finally(() => { running--; pump(); updateButtons(); });
    }
  }

  function succeed(item, data) {
    item.status = "done";
    item.data = data;
    item.view = { markdown: data.markdown || "", html: data.html || "", pages: "" };
    item.original = null;
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

  function markCancelled(item) {
    item.status = "cancelled";
    item.error = { error: "The conversion was cancelled." };
    announce(`${item.name}: cancelled.`);
    updateItem(item);
  }

  async function startJob(item, request) {
    let res;
    try {
      res = await request();
    } catch (_) {
      fail(item, OFFLINE_ERROR);
      return;
    }
    const data = await readJson(res);
    if (res.status !== 202 || !data.job_id) {
      if (res.ok && data.id) succeed(item, data);
      else fail(item, data);
      return;
    }
    item.jobId = data.job_id;
    item.status = "converting";
    item.progress = data.progress;
    updateItem(item);
    if (item.cancelRequested) requestCancel(item);
    await pollJob(item);
  }

  async function pollJob(item) {
    let misses = 0;
    for (;;) {
      await sleep(POLL_MS);
      let r;
      try {
        r = await getJson(`/api/jobs/${encodeURIComponent(item.jobId)}`);
      } catch (_) {
        if (++misses >= 8) { fail(item, OFFLINE_ERROR); return; }
        continue;
      }
      misses = 0;
      if (!r.ok) { fail(item, r.data); return; }
      const job = r.data;
      item.progress = job.progress;
      if (job.status === "done") { succeed(item, job.result); return; }
      if (job.status === "error") { fail(item, job.error); return; }
      if (job.status === "cancelled") { markCancelled(item); return; }
      updateItem(item);
    }
  }

  async function requestCancel(item) {
    item.cancelRequested = true;
    if (item.status === "queued") {
      const i = queue.indexOf(item);
      if (i >= 0) queue.splice(i, 1);
      markCancelled(item);
      return;
    }
    updateItem(item);
    if (!item.jobId) return; // still uploading: cancelled as soon as the job starts
    try {
      await postJson(`/api/jobs/${encodeURIComponent(item.jobId)}/cancel`, {});
    } catch (_) { /* the poll loop reports a lost connection */ }
  }

  function convertFile(item) {
    if (item.file.size > state.maxBytes) {
      fail(item, {
        error: `This file is too large. The limit is ${state.maxMb} MB.`,
        hint: "Start MarkitForge with --max-upload-mb to raise the limit.",
      });
      return Promise.resolve();
    }
    item.status = "uploading";
    updateItem(item);
    const form = new FormData();
    form.append("file", item.file, item.file.name);
    form.append("options", JSON.stringify(item.options));
    if (item.relPath) form.append("rel_path", item.relPath);
    return startJob(item, () => fetch("/api/convert", { method: "POST", headers: API_HEADERS, body: form }));
  }

  function convertUrl(item) {
    item.status = "uploading";
    updateItem(item);
    return startJob(item, () => fetch("/api/convert-url", {
      method: "POST",
      headers: { ...API_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify({ url: item.url, options: item.options }),
    })).then(() => {
      if (item.status === "done" && item.data && item.data.title) { item.name = item.data.title; updateItem(item); }
    });
  }

  function isHiddenPath(path) {
    const parts = path.split("/");
    return parts.some((p) => p.startsWith(".")) || SKIP_NAMES.has(parts[parts.length - 1].toLowerCase());
  }

  // entries: [{file, relPath}]
  function addEntries(entries, truncated) {
    if (!entries.length) {
      if (truncated === false) toast("No files to convert were found.");
      return;
    }
    const options = getOptions();
    if (usesAi(options) && state.llm && !state.llm.is_local) {
      announce("AI options are on: pictures will be sent to the AI service.");
    }
    let first = null;
    for (const { file, relPath } of entries) {
      const name = relPath || file.name || "file";
      const item = addItem("file", name, { file, relPath: relPath || "", options });
      first = first || item;
      enqueue(item);
    }
    if (!selectedItem() || selectedItem().status !== "done") select(first.key, false);
    if (truncated) {
      toast(`Only the first ${state.maxFolderFiles} files were added.`);
      announce(`Converting ${entries.length} files. Only the first ${state.maxFolderFiles} files were added.`);
    } else {
      announce(entries.length === 1 ? `Converting ${entries[0].file.name}…` : `Converting ${entries.length} files…`);
    }
  }

  function addFiles(fileList) {
    addEntries([...fileList].map((file) => ({ file, relPath: "" })));
  }

  function addFolderFiles(fileList) {
    const entries = [];
    let truncated = false;
    for (const file of fileList) {
      const rel = file.webkitRelativePath || file.name;
      if (isHiddenPath(rel)) continue;
      if (entries.length >= state.maxFolderFiles) { truncated = true; break; }
      entries.push({ file, relPath: rel });
    }
    addEntries(entries, truncated);
  }

  // Walk dropped folders (FileSystemEntry API). Returns {entries, truncated}.
  async function walkEntries(roots) {
    const out = [];
    let truncated = false;
    const fileOf = (entry) => new Promise((resolve) => entry.file(resolve, () => resolve(null)));
    const readBatch = (reader) => new Promise((resolve) => reader.readEntries(resolve, () => resolve([])));
    async function visit(entry, depth) {
      if (truncated || depth > 32) return;
      const rel = (entry.fullPath || entry.name || "").replace(/^\/+/, "");
      if (entry.name && entry.name.startsWith(".")) return;
      if (entry.isFile) {
        if (isHiddenPath(rel || entry.name)) return;
        if (out.length >= state.maxFolderFiles) { truncated = true; return; }
        const file = await fileOf(entry);
        if (file) out.push({ file, relPath: rel.includes("/") ? rel : "" });
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        for (;;) {
          const batch = await readBatch(reader);
          if (!batch.length) break;
          for (const child of batch) {
            await visit(child, depth + 1);
            if (truncated) return;
          }
        }
      }
    }
    for (const root of roots) await visit(root, 0);
    return { entries: out, truncated };
  }

  async function openMarkdownFile(file) {
    const item = addItem("md", file.name || "document.md", {});
    select(item.key, false);
    if (file.size > state.maxBytes) {
      fail(item, { error: `This file is too large. The limit is ${state.maxMb} MB.` });
      return;
    }
    item.status = "converting";
    updateItem(item);
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
      saved_assets: [],
      edited: false,
      has_original: false,
    });
  }

  // ---------------------------------------------------------------- history

  async function loadHistory() {
    let r;
    try { r = await getJson("/api/history"); } catch (_) { return; }
    if (!r.ok || !r.data.enabled) return;
    state.historyEnabled = true;
    const known = new Set(state.items.filter((i) => i.data && i.data.id).map((i) => i.data.id));
    for (const h of r.data.items) {
      if (known.has(h.id)) continue;
      const name = h.rel_dir ? `${h.rel_dir}/${h.filename}` : h.filename;
      const item = {
        key: `i${++seq}`, kind: "history", name, status: "stored", historyId: h.id, created: h.created,
        data: null, error: null, view: null, jobId: null, progress: null, cancelRequested: false, original: null,
      };
      state.items.push(item);
    }
    renderList();
  }

  async function openHistoryItem(item) {
    item.status = "converting";
    item.progress = null;
    updateItem(item);
    let r;
    try { r = await getJson(`/api/results/${encodeURIComponent(item.historyId)}`); } catch (_) { fail(item, OFFLINE_ERROR); return; }
    if (!r.ok) { fail(item, r.data); return; }
    item.status = "done";
    item.data = r.data;
    item.view = { markdown: r.data.markdown || "", html: r.data.html || "", pages: "" };
    updateItem(item);
  }

  async function clearHistory() {
    const dialog = $("confirm-dialog");
    const answer = await new Promise((resolve) => {
      const done = () => { dialog.removeEventListener("close", done); resolve(dialog.returnValue); };
      dialog.returnValue = "cancel";
      dialog.addEventListener("close", done);
      dialog.showModal();
    });
    if (answer !== "confirm") return;
    let r;
    try { r = await api("DELETE", "/api/history"); } catch (_) { toast(OFFLINE_ERROR.error); return; }
    if (!r.ok) { toast(r.data.error || "Could not clear the history."); return; }
    state.items = state.items.filter((i) => isBusy(i) && i.kind !== "history");
    if (!findItem(state.selectedKey)) state.selectedKey = null;
    renderList();
    renderViewer();
    toast("History cleared.");
    announce("The history was cleared.");
    $("clear-history-btn").focus();
  }

  // ---------------------------------------------------------------- file list

  const STATUS_TEXT = {
    queued: "Waiting…", uploading: "Sending…", converting: "Converting…", done: "Ready",
    error: "Could not convert", cancelled: "Cancelled", stored: "Saved",
  };

  function statusIcon(item) {
    const wrap = el("span", { class: "status-icon", "aria-hidden": "true" });
    if (item.status === "converting" || item.status === "uploading") wrap.append(el("span", { class: "mini-spinner" }));
    else if (item.status === "done") wrap.append(svgIcon("M5 12.5l4.5 4.5L19 7.5"));
    else if (item.status === "error") wrap.append(svgIcon("M12 7v6m0 4h.01M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z"));
    else if (item.status === "cancelled") wrap.append(svgIcon("M6 6l12 12M18 6L6 18"));
    else if (item.status === "stored") wrap.append(svgIcon("M4 5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z M13 3v5h5"));
    else wrap.append(svgIcon("M12 7v5l3 2M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z"));
    return wrap;
  }

  function progressText(item) {
    const p = item.progress;
    if (!p) return "";
    if (p.total) return `${p.stage === "page" ? "Page" : "Step"} ${p.current} of ${p.total}`;
    return p.message || "";
  }

  function progressBar(item, cls) {
    const bar = el("progress", { class: cls, max: "100", "aria-label": `Progress for ${item.name}` });
    const p = item.progress;
    if (p && typeof p.fraction === "number") bar.value = Math.round(p.fraction * 100);
    else bar.removeAttribute("value"); // indeterminate
    return bar;
  }

  function itemNode(item) {
    let status = STATUS_TEXT[item.status];
    if (item.status === "error" && item.error) status = item.error.error;
    if (item.status === "done" && item.kind === "md") status = "Opened";
    if (item.status === "done" && item.data && item.data.edited) status = "Ready · edited";
    if (item.status === "stored" && item.created) status = `Saved ${shortDate(item.created)}`;
    if (item.status === "converting" && item.cancelRequested) status = "Cancelling…";
    else if (item.status === "converting" && progressText(item)) status = progressText(item);
    const slash = item.name.lastIndexOf("/");
    const nameNode = el("span", { class: "file-name" });
    if (slash > 0 && item.kind !== "url") {
      nameNode.append(el("span", { class: "file-path", text: item.name.slice(0, slash + 1) }), item.name.slice(slash + 1));
    } else {
      nameNode.textContent = item.name;
    }
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
      nameNode,
      el("span", { class: "file-status", text: status }),
      isBusy(item) && item.status !== "queued" ? progressBar(item, "item-progress") : null,
    );
    return el("li", { id: `item-${item.key}` }, btn);
  }

  function renderList() {
    const list = $("file-list");
    list.replaceChildren(...state.items.map(itemNode));
    $("files-empty").hidden = state.items.length > 0;
    updateButtons();
  }

  function updateButtons() {
    const busy = running > 0 || queue.length > 0;
    $("clear-btn").hidden = state.items.length === 0 || busy;
    $("clear-history-btn").hidden = !state.historyEnabled;
    updateZip();
  }

  function updateItem(item) {
    const old = $(`item-${item.key}`);
    const fresh = itemNode(item);
    if (old) {
      const hadFocus = old.contains(document.activeElement);
      old.replaceWith(fresh);
      if (hadFocus) fresh.querySelector("button").focus();
    } else {
      renderList();
    }
    updateButtons();
    if (item.key === state.selectedKey) renderViewer();
  }

  function doneIds() {
    return state.items.filter((i) => i.status === "done" && i.data && i.data.id).map((i) => i.data.id);
  }

  function updateZip() {
    const ids = doneIds();
    const zip = $("zip-btn");
    if (ids.length) {
      zip.href = withToken(`/api/zip?ids=${encodeURIComponent(ids.join(","))}`);
      zip.removeAttribute("aria-disabled");
      zip.removeAttribute("tabindex");
    } else {
      zip.href = "#";
      zip.setAttribute("aria-disabled", "true");
      zip.setAttribute("tabindex", "-1");
    }
  }

  function select(key, focusViewer) {
    const previous = selectedItem();
    if (previous && previous.key !== key) flushSave(previous);
    state.selectedKey = key;
    for (const btn of document.querySelectorAll(".file-item")) {
      btn.setAttribute("aria-current", btn.dataset.key === key ? "true" : "false");
    }
    const item = selectedItem();
    if (item && item.status === "stored") openHistoryItem(item);
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
    if (isBusy(item) || item.status === "stored") {
      showOnly("viewer-status");
      const verb = { queued: "Waiting to convert", uploading: "Sending", converting: "Converting", stored: "Opening" }[item.status];
      $("viewer-status-text").textContent = item.cancelRequested ? `Cancelling ${item.name}…` : `${verb} ${item.name}…`;
      const bar = $("job-progress");
      const p = item.progress;
      if (p && typeof p.fraction === "number") bar.value = Math.round(p.fraction * 100);
      else bar.removeAttribute("value");
      $("job-progress-text").textContent = progressText(item);
      $("cancel-btn").hidden = item.kind === "md" || item.kind === "history" || item.cancelRequested;
      return;
    }
    if (item.status === "error" || item.status === "cancelled") {
      showOnly("viewer-error");
      $("viewer-error").classList.toggle("cancelled", item.status === "cancelled");
      const err = item.error || {};
      $("error-title").textContent = item.status === "cancelled" ? `${item.name} was cancelled` : `Couldn't convert ${item.name}`;
      $("error-message").textContent = err.error || "Something went wrong.";
      $("error-hint").hidden = !err.hint;
      $("error-hint").textContent = err.hint || "";
      $("error-hint").classList.toggle("code", /pip install|\n/.test(err.hint || ""));
      $("error-detail-wrap").hidden = !err.detail;
      $("error-detail").textContent = err.detail || "";
      $("retry-btn").hidden = item.kind === "md" || item.kind === "history";
      $("retry-btn").textContent = item.status === "cancelled" ? "Convert again" : "Try again";
      return;
    }
    showOnly("viewer-content");
    renderResult(item);
  }

  function kindLabel(pages) {
    if (!pages.length) return "page";
    return pages[0].kind || "page";
  }

  function setPreviewHtml(html, markdown) {
    const preview = $("preview");
    if (markdown.trim()) {
      preview.innerHTML = html;
      for (const table of preview.querySelectorAll("table")) {
        if (table.parentElement && table.parentElement.classList.contains("table-wrap")) continue;
        const wrap = el("div", { class: "table-wrap" });
        table.replaceWith(wrap);
        wrap.append(table);
      }
    } else {
      preview.replaceChildren(el("p", { class: "empty-note", text: "This file did not contain any text MarkitForge could extract." }));
    }
  }

  function updateGutter(markdown) {
    let lines = 1;
    for (let i = 0; i < markdown.length; i++) if (markdown.charCodeAt(i) === 10) lines++;
    const gutter = $("gutter");
    if (Number(gutter.dataset.lines) === lines) return;
    gutter.dataset.lines = String(lines);
    const numbers = new Array(lines);
    for (let i = 0; i < lines; i++) numbers[i] = i + 1;
    gutter.textContent = numbers.join("\n") + "\n";
  }

  function subtitle(item) {
    const data = item.data;
    const pages = data.pages || [];
    const bits = [];
    if (item.kind === "url") bits.push(item.url);
    else if (item.kind === "file" || item.kind === "history") bits.push(`${item.name} → ${data.md_filename}`);
    else bits.push("Markdown file");
    if (pages.length) bits.push(`${pages.length} ${kindLabel(pages)}${pages.length === 1 ? "" : "s"}`);
    bits.push(`${countWords(item.view.markdown).toLocaleString()} words`);
    return bits.join(" · ");
  }

  function renderResult(item) {
    const data = item.data;
    const view = item.view;
    const pages = data.pages || [];

    $("doc-title").textContent = data.title || item.name;
    $("doc-sub").textContent = subtitle(item);
    setPreviewHtml(view.html, view.markdown);

    // Editable Markdown. Don't reset the text (and the cursor) while typing.
    const source = $("source");
    if (source.dataset.key !== item.key || source.value !== view.markdown) {
      source.value = view.markdown;
      source.dataset.key = item.key;
      source.scrollTop = 0;
      $("gutter").scrollTop = 0;
    }
    updateGutter(view.markdown);
    source.readOnly = Boolean(view.pages);
    $("editor-note").textContent = view.pages
      ? "You are looking at a page selection. Choose Show all pages to edit the whole document."
      : "You can edit the Markdown here. Copy and Download use your edited text.";
    $("edited-badge").hidden = !data.edited;
    renderSaveState(item);

    // Selection banner and download link.
    $("selection-banner").hidden = !view.pages;
    $("selection-text").textContent = view.pages
      ? `Showing ${kindLabel(pages)}${/^\d+$/.test(view.pages) ? "" : "s"} ${view.pages} (out of ${pages.length})`
      : "";
    updateDownload(item);
    renderDetails(item);
    syncSplit();
    if (state.tab === "original" || isSplit()) renderOriginal(item);
  }

  function updateDownload(item) {
    const data = item.data;
    const view = item.view;
    const dl = $("download-btn");
    if (dl.dataset.blobUrl) { URL.revokeObjectURL(dl.dataset.blobUrl); delete dl.dataset.blobUrl; }
    const withImages = data.id && !view.pages && (data.saved_assets || []).length > 0;
    if (data.id) {
      dl.href = withImages
        ? withToken(`/api/zip?ids=${encodeURIComponent(data.id)}`)
        : withToken(`/api/download/${encodeURIComponent(data.id)}` + (view.pages ? `?pages=${encodeURIComponent(view.pages)}` : ""));
      dl.removeAttribute("download");
    } else {
      const blobUrl = URL.createObjectURL(new Blob([view.markdown], { type: "text/markdown;charset=utf-8" }));
      dl.dataset.blobUrl = blobUrl;
      dl.href = blobUrl;
      dl.setAttribute("download", data.md_filename || "document.md");
    }
    $("download-label").textContent = view.pages
      ? "Download selection (.md)"
      : withImages ? "Download .zip (with images)" : "Download .md";
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

    $("warnings-section").hidden = warnings.length === 0;
    $("warnings-list").replaceChildren(...warnings.map((w) => el("li", { text: w })));
    $("warn-badge").hidden = warnings.length === 0;
    $("warn-badge").textContent = String(warnings.length);
    $("warn-badge").setAttribute("aria-label", `${warnings.length} warning${warnings.length === 1 ? "" : "s"}`);

    $("pages-section").hidden = pages.length === 0 || !data.id;
    $("pages-title").textContent = pages.length ? `${prettyKey(kindLabel(pages))}s (${pages.length})` : "Pages";
    $("range-form").hidden = Boolean(data.edited);
    $("range-edited").hidden = !data.edited;
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
          data.edited ? null : el("button", {
            type: "button",
            class: "btn small",
            text: "View",
            "aria-label": `View ${p.kind || "page"} ${p.index}`,
            onclick: () => applyRange(item, String(p.index), true),
          }),
        ),
      ),
    );

    const meta = data.metadata || {};
    const rows = Object.entries(meta).map(([k, v]) =>
      el("tr", null, el("th", { scope: "row", text: prettyKey(k) }), el("td", null, metaValueNode(v))),
    );
    $("meta-table").tBodies[0].replaceChildren(...rows);
    $("meta-table").hidden = rows.length === 0;
    $("meta-empty").hidden = rows.length !== 0;

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
    await flushSave(item);
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

  // ---------------------------------------------------------------- editing

  function renderSaveState(item) {
    const box = $("save-state");
    if (item.saveError) box.textContent = item.saveError;
    else if (item.savePending || item.saving) box.textContent = "Saving…";
    else if (item.data && item.data.edited && item.data.id) box.textContent = "Edits saved";
    else if (item.data && !item.data.id && item.dirty) box.textContent = "Edited (download to keep it)";
    else box.textContent = "";
    box.classList.toggle("field-error", Boolean(item.saveError));
  }

  const renderSoon = debounce(async (item) => {
    const markdown = item.view.markdown;
    let r;
    try {
      r = await postJson("/api/render", { markdown, for_id: item.data && item.data.id ? item.data.id : undefined });
    } catch (_) { return; }
    if (!r.ok || item.view.markdown !== markdown) return;
    item.view.html = r.data.html;
    if (selectedItem() === item) {
      setPreviewHtml(item.view.html, item.view.markdown);
      $("doc-sub").textContent = subtitle(item);
    }
  }, 450);

  async function saveNow(item) {
    item.savePending = false;
    if (!item.data || !item.data.id) return true;
    const markdown = item.view.markdown;
    if (new Blob([markdown]).size > state.maxEditBytes) {
      item.saveError = "This text is too large to save. Make it shorter to keep your edits.";
      renderSaveState(item);
      return false;
    }
    item.saving = true;
    item.saveError = null;
    if (selectedItem() === item) renderSaveState(item);
    let r;
    try {
      r = await api("PUT", `/api/results/${encodeURIComponent(item.data.id)}/markdown`, { markdown });
    } catch (_) {
      r = { ok: false, data: OFFLINE_ERROR };
    }
    item.saving = false;
    if (r.ok) {
      item.data.edited = true;
      item.data.markdown = markdown;
      item.saveError = null;
    } else {
      item.saveError = `Not saved: ${r.data.error || "error"}`;
    }
    if (selectedItem() === item) {
      renderSaveState(item);
      $("edited-badge").hidden = !item.data.edited;
      renderDetails(item);
    }
    updateItem(item);
    return r.ok;
  }

  const saveTimers = new Map();
  function scheduleSave(item) {
    item.savePending = true;
    clearTimeout(saveTimers.get(item.key));
    saveTimers.set(item.key, setTimeout(() => { saveTimers.delete(item.key); saveNow(item); }, 800));
  }

  async function flushSave(item) {
    if (!item || !item.savePending) return true;
    clearTimeout(saveTimers.get(item.key));
    saveTimers.delete(item.key);
    return saveNow(item);
  }

  async function flushAll() {
    await Promise.all(state.items.filter((i) => i.savePending).map(flushSave));
  }

  function initEditor() {
    const source = $("source");
    const gutter = $("gutter");
    source.addEventListener("input", () => {
      const item = selectedItem();
      if (!item || !item.view || source.readOnly) return;
      item.view.markdown = source.value;
      item.dirty = true;
      updateGutter(source.value);
      renderSoon(item);
      if (item.data && item.data.id) scheduleSave(item);
      else updateDownload(item);
      renderSaveState(item);
    });
    source.addEventListener("scroll", () => { gutter.scrollTop = source.scrollTop; });
    // Tab inserts a tab character; Escape then Tab leaves the editor.
    let escaped = false;
    source.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { escaped = true; return; }
      if (e.key === "Tab" && !e.shiftKey && !escaped && !source.readOnly && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        const { selectionStart: a, selectionEnd: b } = source;
        source.setRangeText("\t", a, b, "end");
        source.dispatchEvent(new Event("input"));
      }
      escaped = false;
    });
  }

  // ---------------------------------------------------------------- original

  function isSplit() { return state.split && wideScreen.matches; }

  function syncSplit() {
    const split = isSplit();
    $("panels").classList.toggle("split", split);
    $("split-btn").setAttribute("aria-pressed", state.split ? "true" : "false");
    $("tab-original").hidden = split;
    if (split && state.tab === "original") { setTab("preview", false); return; }
    for (const t of TABS) $(`panel-${t}`).hidden = !(t === state.tab || (split && t === "original"));
    const item = selectedItem();
    if (split && item && item.status === "done") renderOriginal(item);
  }

  function originalNote(text, strong) {
    const p = el("p", { class: "note" });
    if (strong) p.append(el("strong", { text: strong }), " ");
    p.append(text);
    return p;
  }

  async function renderOriginal(item) {
    const box = $("original-view");
    if (box.dataset.key === item.key && item.original && !item.original.loading) return;
    box.dataset.key = item.key;
    if (!item.data || !item.data.id) {
      box.replaceChildren(originalNote(item.kind === "md" ? "This is a Markdown file you opened; there is no other original." : "No original is available."));
      return;
    }
    if (!item.original) {
      item.original = { loading: true };
      box.replaceChildren(originalNote("Loading the original…"));
      let r;
      try { r = await getJson(`/api/original/${encodeURIComponent(item.data.id)}/info`); } catch (_) { r = { ok: false, data: OFFLINE_ERROR }; }
      item.original = r.ok ? r.data : { kind: "none", message: r.data.error };
      if (selectedItem() !== item) return;
      box.dataset.key = "";
      renderOriginal(item);
      return;
    }
    if (item.original.loading) return;
    const info = item.original;
    const src = withToken(`/api/original/${encodeURIComponent(item.data.id)}`);
    if (info.kind === "pdf") {
      const frame = el("iframe", { src, title: `Original file: ${item.name}` });
      // Chromium's PDF viewer sometimes stays blank until the frame is laid
      // out again once it has started; a one-pixel size change makes it
      // paint. The viewer starts some time after "load", so nudge a few times.
      frame.addEventListener("load", () => {
        for (const ms of [400, 1500, 3500]) setTimeout(() => frame.classList.toggle("ready"), ms);
      }, { once: true });
      box.replaceChildren(frame);
    } else if (info.kind === "image") {
      box.replaceChildren(el("img", { src, alt: `Original image: ${item.name}` }));
    } else if (info.kind === "text") {
      const nodes = [el("pre", { text: info.text || "", tabindex: "0", "aria-label": `Original text of ${item.name}` })];
      if (info.truncated) nodes.unshift(originalNote("Only the start of this file is shown."));
      box.replaceChildren(...nodes);
    } else {
      box.replaceChildren(originalNote(info.message || "Preview not available for this file type.", "Original not shown."));
    }
  }

  // ---------------------------------------------------------------- tabs

  const TABS = ["preview", "markdown", "original", "details"];

  function visibleTabs() { return TABS.filter((t) => !$(`tab-${t}`).hidden); }

  function setTab(name, focus) {
    state.tab = name;
    for (const t of TABS) {
      const tab = $(`tab-${t}`);
      const on = t === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
    }
    syncSplit();
    const item = selectedItem();
    if (name === "original" && item && item.status === "done") renderOriginal(item);
    if (focus) $(`tab-${name}`).focus();
  }

  function initTabs() {
    for (const t of TABS) {
      const tab = $(`tab-${t}`);
      tab.addEventListener("click", () => setTab(t, false));
      tab.addEventListener("keydown", (e) => {
        const tabs = visibleTabs();
        const i = tabs.indexOf(t);
        let next = null;
        if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
        else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === "Home") next = tabs[0];
        else if (e.key === "End") next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); setTab(next, true); }
      });
    }
    try { state.split = localStorage.getItem(SPLIT_KEY) === "1"; } catch (_) { state.split = false; }
    $("split-btn").addEventListener("click", () => {
      state.split = !state.split;
      try { localStorage.setItem(SPLIT_KEY, state.split ? "1" : "0"); } catch (_) { /* ignore */ }
      syncSplit();
      announce(state.split ? "Showing the original next to the result." : "Showing one view at a time.");
    });
    wideScreen.addEventListener("change", syncSplit);
  }

  // ---------------------------------------------------------------- clipboard

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const area = el("textarea", { readonly: true, "aria-hidden": "true", class: "visually-hidden" });
      area.value = text;
      document.body.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      area.remove();
      return ok;
    }
  }

  function followLink(href) {
    const a = el("a", { href, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
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
      // Entries must be read synchronously, before any await.
      const roots = [...(e.dataTransfer.items || [])]
        .filter((i) => i.kind === "file" && typeof i.webkitGetAsEntry === "function")
        .map((i) => i.webkitGetAsEntry())
        .filter(Boolean);
      if (roots.some((r) => r.isDirectory)) {
        announce("Reading the folder…");
        walkEntries(roots).then(({ entries, truncated }) => addEntries(entries, truncated));
      } else {
        addFiles(e.dataTransfer.files);
      }
    });
  }

  // ---------------------------------------------------------------- wiring

  function init() {
    initTheme();
    loadOptions(null);
    for (const input of Object.values(optionInputs)) input.addEventListener("change", saveOptions);
    initTabs();
    initDragDrop();
    initEditor();
    initLlm();

    getJson("/api/info")
      .then((r) => {
        if (!r.ok) {
          if (r.data && r.data.code === "token") toast(r.data.error);
          return;
        }
        const info = r.data;
        state.maxBytes = info.max_upload_bytes;
        state.maxMb = info.max_upload_mb;
        state.maxEditBytes = info.max_edit_bytes || state.maxEditBytes;
        state.maxFolderFiles = info.max_folder_files || state.maxFolderFiles;
        $("limit-mb").textContent = String(Math.round(info.max_upload_mb));
        $("folder-limit").textContent = String(state.maxFolderFiles);
        loadOptions(info.defaults);
        if (info.history_enabled) { state.historyEnabled = true; loadHistory(); }
        updateButtons();
      })
      .catch(() => {});

    const fileInput = $("file-input");
    $("choose-btn").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      addFiles(fileInput.files);
      fileInput.value = "";
    });
    const folderInput = $("folder-input");
    $("folder-btn").addEventListener("click", () => folderInput.click());
    folderInput.addEventListener("change", () => {
      addFolderFiles(folderInput.files);
      folderInput.value = "";
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
      if (!item || item.kind === "md" || item.kind === "history") return;
      item.options = getOptions();
      enqueue(item);
      updateItem(item);
    });

    $("cancel-btn").addEventListener("click", () => {
      const item = selectedItem();
      if (item && isBusy(item)) requestCancel(item);
    });

    $("copy-btn").addEventListener("click", async () => {
      const item = selectedItem();
      if (!item || !item.view) return;
      const ok = await copyText(item.view.markdown);
      toast(ok ? "Markdown copied to the clipboard" : "Could not copy. Select the text in the Markdown tab instead.");
    });

    $("download-btn").addEventListener("click", async (e) => {
      const item = selectedItem();
      if (!item || !item.savePending) return;
      e.preventDefault();
      if (await flushSave(item)) followLink($("download-btn").href);
    });

    $("zip-btn").addEventListener("click", async (e) => {
      const zip = $("zip-btn");
      if (zip.getAttribute("aria-disabled") === "true") { e.preventDefault(); return; }
      if (state.items.some((i) => i.savePending)) {
        e.preventDefault();
        await flushAll();
        followLink(zip.href);
      }
    });

    $("clear-btn").addEventListener("click", () => {
      state.items = state.items.filter((i) => isBusy(i));
      state.selectedKey = null;
      renderList();
      renderViewer();
      announce("List cleared. Saved results are still in the history.");
    });
    $("clear-history-btn").addEventListener("click", clearHistory);

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

    // A PDF viewer started while the window was in the background may need
    // one more layout to paint (see renderOriginal).
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      for (const frame of document.querySelectorAll("#original-view iframe")) frame.classList.toggle("ready");
    });

    // Don't lose edits that are still waiting to be saved.
    window.addEventListener("beforeunload", (e) => {
      if (state.items.some((i) => i.savePending || i.saving)) {
        flushAll();
        e.preventDefault();
        e.returnValue = "";
      }
    });

    renderList();
    renderViewer();
    syncSplit();
  }

  init();
})();
