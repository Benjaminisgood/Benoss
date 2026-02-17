(function () {
  document.body.classList.add("has-js");
  const page = document.body.dataset.page || "";
  const siteHeader = document.getElementById("site-header");
  const siteHeaderRight = document.getElementById("site-header-right");
  const siteNavToggle = document.getElementById("site-nav-toggle");
  const dialog = document.getElementById("record-dialog");
  const dialogBody = document.getElementById("record-dialog-body");
  const dialogTitle = document.getElementById("record-dialog-title");

  let usersCache = null;
  const timeTextCache = new Map();
  const timeCacheMax = 512;
  const htmlEscapePattern = /[&<>"']/g;
  const htmlEscapeMap = Object.freeze({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  });
  const recordActionHandlers = {
    "view-record": openRecordDialog,
    "edit-record": editRecord,
    "comment-record": commentRecord,
  };
  const echoFileTypeLabels = Object.freeze({
    text: "文本",
    web: "网页",
    image: "图片",
    video: "视频",
    audio: "音频",
    log: "日志",
    database: "数据库",
    archive: "压缩包",
    document: "文档",
    file: "文件",
  });
  const webContentTypes = new Set(["text/html", "application/xhtml+xml"]);
  const webFileExtensions = [".html", ".htm", ".xhtml"];
  const logFileExtensions = [".log", ".out", ".err"];
  const databaseContentTypes = new Set(["application/x-sqlite3", "application/vnd.sqlite3"]);
  const databaseFileExtensions = [".db", ".sqlite", ".sqlite3", ".db3"];
  const archiveContentTypes = new Set([
    "application/zip",
    "application/x-zip-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/x-7z-compressed",
    "application/vnd.rar",
    "application/x-rar-compressed",
    "application/x-bzip2",
    "application/x-xz",
  ]);
  const archiveFileExtensions = [".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tbz2", ".xz", ".txz", ".7z", ".rar"];
  const documentContentTypes = new Set([
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
  ]);
  const documentFileExtensions = [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".ods", ".odp"];
  const echoesState = {
    fileType: "",
    cursor: null,
    hasMore: true,
    loading: false,
    visibleCount: 0,
    observer: null,
    pageSize: 24,
    density: "cozy",
  };
  const homePanelStorageKey = "benoss.home.active_panel";
  const echoesDensityStorageKey = "benoss.echoes.density";
  const noticeReaderStorageKey = "benoss.notice.reader";
  const validVisibilityValues = new Set(["public", "private"]);
  const validEchoesDensity = new Set(["cozy", "compact"]);
  const defaultNoticeReaderPrefs = Object.freeze({
    font: "md",
    width: "normal",
    media: "expand",
  });
  const validNoticeFontValues = new Set(["md", "lg"]);
  const validNoticeWidthValues = new Set(["normal", "wide"]);
  const validNoticeMediaValues = new Set(["expand", "collapse"]);
  let noticeReaderPrefs = {
    ...defaultNoticeReaderPrefs,
  };
  const dialogState = {
    record: null,
    mode: "view",
    saving: false,
  };

  function normalizeVisibility(value, fallback = "private") {
    const normalized = String(value || "").trim().toLowerCase();
    if (validVisibilityValues.has(normalized)) {
      return normalized;
    }
    const fallbackValue = String(fallback || "private").trim().toLowerCase();
    return validVisibilityValues.has(fallbackValue) ? fallbackValue : "private";
  }

  function formatFileSize(sizeBytes) {
    const value = Number(sizeBytes || 0);
    if (value < 1024) {
      return `${value} B`;
    }
    if (value < 1024 * 1024) {
      return `${(value / 1024).toFixed(1)} KB`;
    }
    if (value < 1024 * 1024 * 1024) {
      return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    }
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(htmlEscapePattern, (char) => htmlEscapeMap[char]);
  }

  function qs(selector, root = document) {
    return root.querySelector(selector);
  }

  function markFeedbackEl(el, tone = "neutral") {
    if (!(el instanceof HTMLElement)) {
      return;
    }
    el.classList.add("feedback-inline");
    el.dataset.tone = String(tone || "neutral");
  }

  function setFeedback(el, text, tone = "neutral") {
    if (!(el instanceof HTMLElement)) {
      return;
    }
    markFeedbackEl(el, tone);
    el.textContent = String(text || "");
  }

  function setButtonBusy(button, busy, options = {}) {
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }
    const isBusy = Boolean(busy);
    const busyText = String(options.busyText || "处理中...");
    if (isBusy) {
      if (!button.dataset.busyLabel) {
        button.dataset.busyLabel = button.textContent || "";
      }
      button.dataset.busyWasDisabled = button.disabled ? "1" : "0";
      button.classList.add("is-busy");
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (options.keepLabel !== true) {
        button.textContent = busyText;
      }
      return;
    }
    button.classList.remove("is-busy");
    button.removeAttribute("aria-busy");
    if (options.keepLabel !== true && button.dataset.busyLabel) {
      button.textContent = button.dataset.busyLabel;
    }
    if (!options.keepDisabled) {
      button.disabled = button.dataset.busyWasDisabled === "1";
    }
    delete button.dataset.busyLabel;
    delete button.dataset.busyWasDisabled;
  }

  function initStatusDecorators() {
    const ids = [
      "quick-publish-msg",
      "vector-status",
      "archive-status",
      "digest-status",
      "echoes-status",
      "notice-render-meta",
      "admin-settings-status",
    ];
    ids.forEach((id) => {
      const el = qs(`#${id}`);
      if (el) {
        markFeedbackEl(el, "neutral");
      }
    });
  }

  function syncStickyHeaderOffset() {
    const root = document.documentElement;
    if (!root) {
      return;
    }
    if (!siteHeader) {
      root.style.setProperty("--site-header-offset", "88px");
      return;
    }
    const rect = siteHeader.getBoundingClientRect();
    const next = Math.max(64, Math.ceil(rect.height + 4));
    root.style.setProperty("--site-header-offset", `${next}px`);
  }

  function setHeaderMenuOpen(value) {
    if (!siteHeader || !siteNavToggle || !siteHeaderRight) {
      return;
    }
    const shouldOpen = Boolean(value);
    siteHeader.classList.toggle("is-nav-open", shouldOpen);
    siteNavToggle.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
    document.body.classList.toggle("nav-open-lock", shouldOpen);
    window.requestAnimationFrame(syncStickyHeaderOffset);
  }

  function initHeaderNavToggle() {
    if (!siteHeader || !siteNavToggle || !siteHeaderRight) {
      return;
    }

    const closeMenu = () => setHeaderMenuOpen(false);
    const mediaQuery = window.matchMedia("(min-width: 981px)");

    siteNavToggle.addEventListener("click", () => {
      setHeaderMenuOpen(!siteHeader.classList.contains("is-nav-open"));
    });

    siteHeaderRight.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest("a")) {
        closeMenu();
      }
    });

    document.addEventListener("click", (event) => {
      if (!siteHeader.classList.contains("is-nav-open")) {
        return;
      }
      if (!(event.target instanceof Node)) {
        return;
      }
      if (siteHeader.contains(event.target)) {
        return;
      }
      closeMenu();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeMenu();
      }
    });

    const syncForDesktop = () => {
      if (mediaQuery.matches) {
        closeMenu();
      }
      syncStickyHeaderOffset();
    };
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", syncForDesktop);
    } else if (typeof mediaQuery.addListener === "function") {
      mediaQuery.addListener(syncForDesktop);
    }

    window.addEventListener("resize", syncStickyHeaderOffset);
    window.addEventListener("orientationchange", syncStickyHeaderOffset);
    window.addEventListener("load", syncStickyHeaderOffset);
    window.requestAnimationFrame(syncStickyHeaderOffset);
  }

  function initRevealAnimations() {
    const revealBlocks = Array.from(document.querySelectorAll("[data-reveal]"));
    if (!revealBlocks.length) {
      return;
    }
    const prefersReduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    revealBlocks.forEach((block) => {
      const delayValue = Number(block.getAttribute("data-reveal") || "0");
      const delay = Number.isFinite(delayValue) ? Math.max(0, delayValue) : 0;
      block.style.setProperty("--reveal-delay", `${delay}ms`);
      block.classList.add("is-reveal-ready");
    });

    if (prefersReduceMotion || !("IntersectionObserver" in window)) {
      revealBlocks.forEach((block) => block.classList.add("is-revealed"));
      return;
    }

    const observer = new IntersectionObserver(
      (entries, obs) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) {
            return;
          }
          entry.target.classList.add("is-revealed");
          obs.unobserve(entry.target);
        });
      },
      {
        threshold: 0.08,
        rootMargin: "0px 0px -4% 0px",
      },
    );

    revealBlocks.forEach((block) => observer.observe(block));
  }

  function normalizeEchoesDensity(value) {
    const normalized = String(value || "").trim().toLowerCase();
    return validEchoesDensity.has(normalized) ? normalized : "cozy";
  }

  function readStoredEchoesDensity() {
    try {
      return normalizeEchoesDensity(window.localStorage.getItem(echoesDensityStorageKey));
    } catch (_error) {
      return "cozy";
    }
  }

  function saveEchoesDensity(value) {
    try {
      window.localStorage.setItem(echoesDensityStorageKey, normalizeEchoesDensity(value));
    } catch (_error) {
      // Ignore storage failures.
    }
  }

  function applyEchoesDensity(value, opts = {}) {
    const mode = normalizeEchoesDensity(value);
    echoesState.density = mode;

    const grid = qs("#echoes-grid");
    if (grid) {
      grid.classList.toggle("is-compact", mode === "compact");
    }

    const buttons = document.querySelectorAll("[data-echoes-density]");
    buttons.forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      const isActive = normalizeEchoesDensity(button.dataset.echoesDensity) === mode;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", isActive ? "true" : "false");
    });

    if (opts.persist !== false) {
      saveEchoesDensity(mode);
    }
  }

  function normalizeNoticeReaderPrefs(value) {
    const raw = value && typeof value === "object" ? value : {};
    const font = validNoticeFontValues.has(String(raw.font || "").trim().toLowerCase())
      ? String(raw.font).trim().toLowerCase()
      : defaultNoticeReaderPrefs.font;
    const width = validNoticeWidthValues.has(String(raw.width || "").trim().toLowerCase())
      ? String(raw.width).trim().toLowerCase()
      : defaultNoticeReaderPrefs.width;
    const media = validNoticeMediaValues.has(String(raw.media || "").trim().toLowerCase())
      ? String(raw.media).trim().toLowerCase()
      : defaultNoticeReaderPrefs.media;
    return { font, width, media };
  }

  function readStoredNoticeReaderPrefs() {
    try {
      const raw = window.localStorage.getItem(noticeReaderStorageKey);
      if (!raw) {
        return { ...defaultNoticeReaderPrefs };
      }
      const parsed = JSON.parse(raw);
      return normalizeNoticeReaderPrefs(parsed);
    } catch (_error) {
      return { ...defaultNoticeReaderPrefs };
    }
  }

  function saveNoticeReaderPrefs(value) {
    try {
      const normalized = normalizeNoticeReaderPrefs(value);
      window.localStorage.setItem(noticeReaderStorageKey, JSON.stringify(normalized));
    } catch (_error) {
      // Ignore storage failures.
    }
  }

  function applyNoticeReaderPrefs(value, opts = {}) {
    noticeReaderPrefs = normalizeNoticeReaderPrefs(value);

    const panel = qs("#notice-render-panel");
    const html = qs("#notice-render-html");
    if (panel && html) {
      panel.classList.toggle("notice-font-lg", noticeReaderPrefs.font === "lg");
      panel.classList.toggle("notice-width-wide", noticeReaderPrefs.width === "wide");
      panel.classList.toggle("notice-media-collapsed", noticeReaderPrefs.media === "collapse");
    }

    const fontButtons = document.querySelectorAll("[data-notice-font]");
    fontButtons.forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      const active = String(button.dataset.noticeFont || "") === noticeReaderPrefs.font;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });

    const widthButtons = document.querySelectorAll("[data-notice-width]");
    widthButtons.forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      const active = String(button.dataset.noticeWidth || "") === noticeReaderPrefs.width;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });

    const mediaButtons = document.querySelectorAll("[data-notice-media]");
    mediaButtons.forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      const active = String(button.dataset.noticeMedia || "") === noticeReaderPrefs.media;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });

    if (opts.persist !== false) {
      saveNoticeReaderPrefs(noticeReaderPrefs);
    }
  }

  function formatTime(value) {
    if (!value) {
      return "";
    }
    const raw = String(value);
    const cached = timeTextCache.get(raw);
    if (cached !== undefined) {
      return cached;
    }

    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) {
      return raw;
    }
    const formatted = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    if (timeTextCache.size >= timeCacheMax) {
      timeTextCache.clear();
    }
    timeTextCache.set(raw, formatted);
    return formatted;
  }

  function buildQuery(params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value === undefined || value === null || value === "") {
        continue;
      }
      query.set(key, String(value));
    }
    return query.toString();
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
    });

    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");
    const data = isJson ? await response.json() : { raw: await response.text() };

    if (!response.ok) {
      const message = data.error || data.raw || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return data;
  }

  async function getUsers() {
    if (usersCache) {
      return usersCache;
    }
    const data = await api("/api/users");
    usersCache = data.items || [];
    return usersCache;
  }

  function populateSelect(selectEl, items, opts = {}) {
    if (!selectEl) {
      return;
    }

    const allowAll = Boolean(opts.allowAll);
    const allText = opts.allText || "全部";
    const current = selectEl.value;
    const options = [];

    if (allowAll) {
      options.push(`<option value="">${escapeHtml(allText)}</option>`);
    }
    for (const item of items) {
      options.push(`<option value="${item.id}">${escapeHtml(item.username || item.name || item.id)}</option>`);
    }

    selectEl.innerHTML = options.join("");
    if (current) {
      selectEl.value = current;
    }
  }

  function tagHtml(tags) {
    const list = Array.isArray(tags) ? tags : [];
    if (!list.length) {
      return "<span class=\"muted\">无标签</span>";
    }
    return list.map((tag) => `<span class="tag-pill">#${escapeHtml(tag)}</span>`).join("");
  }

  function contentHtml(content) {
    if (!content) {
      return "<p class=\"muted\">无内容</p>";
    }
    if (content.kind === "text") {
      return `<pre>${escapeHtml(content.text || "")}</pre>`;
    }

    const mediaType = content.media_type || "file";
    const src = content.blob_url || content.signed_url || "";
    if (!src) {
      return `<p class="muted">文件内容不可用</p>`;
    }

    if (mediaType === "image") {
      return `<img src="${escapeHtml(src)}" alt="${escapeHtml(content.filename || "image")}">`;
    }
    if (mediaType === "video") {
      return `<video controls src="${escapeHtml(src)}"></video>`;
    }
    if (mediaType === "audio") {
      return `<audio controls src="${escapeHtml(src)}"></audio>`;
    }
    return `<p><a href="${escapeHtml(src)}" target="_blank" rel="noreferrer">下载/查看文件：${escapeHtml(content.filename || "file")}</a></p>`;
  }

  function commentsHtml(comments) {
    const list = Array.isArray(comments) ? comments : [];
    if (!list.length) {
      return "<p class=\"muted\">暂无评论</p>";
    }

    return list
      .map(
        (item) => `
      <article class="notice-item">
        <div class="notice-head">
          <strong>${escapeHtml(item.user?.username || "")}</strong>
          <span class="muted">${escapeHtml(formatTime(item.created_at))}</span>
        </div>
        <p>${escapeHtml(item.body || "")}</p>
      </article>
    `,
      )
      .join("");
  }

  function dialogRecordMeta(record) {
    const visibilityText = normalizeVisibility(record.visibility, "private") === "public" ? "公开" : "私密";
    return `用户: ${escapeHtml(record.user?.username || "")} | ${escapeHtml(formatTime(record.created_at))} | ${visibilityText}`;
  }

  function currentFileMetaHtml(content) {
    if (!content || content.kind !== "file") {
      return "<p class=\"muted\">当前记录不是文件类型。</p>";
    }
    const sizeText = formatFileSize(content.size_bytes);
    return `
      <div class="record-edit-file-current">
        <p><strong>${escapeHtml(content.filename || "未命名文件")}</strong></p>
        <p class="muted">${escapeHtml(content.content_type || "application/octet-stream")} | ${escapeHtml(sizeText)}</p>
      </div>
    `;
  }

  function ensureDialogVisible() {
    if (dialog && !dialog.open) {
      dialog.showModal();
    }
  }

  async function fetchRecord(recordId, includeComments = true) {
    const flag = includeComments ? "1" : "0";
    const data = await api(`/api/records/${recordId}?include_comments=${flag}`);
    return data.record;
  }

  function renderRecordDialogView(record) {
    dialogState.record = record;
    dialogState.mode = "view";

    dialogTitle.textContent = `记录 #${record.record_no}`;
    const actions = [];
    if (record.can_edit) {
      actions.push(`<button class="bubble" type="button" data-dialog-action="edit-record" data-record-id="${record.id}">编辑这条记录</button>`);
    }
    if (record.can_comment) {
      actions.push(`<button class="bubble" type="button" data-dialog-action="comment-record" data-record-id="${record.id}">评论这条记录</button>`);
    }

    dialogBody.innerHTML = `
      <div class="dialog-content">
        <p class="muted">${dialogRecordMeta(record)}</p>
        <p>${escapeHtml(record.preview || "(无预览内容)")}</p>
        <div class="tag-line">${tagHtml(record.tags)}</div>
        <div class="action-line">${actions.join("")}</div>
        <section>
          <h4>具体内容</h4>
          ${contentHtml(record.content)}
        </section>
        <section>
          <h4>评论</h4>
          ${commentsHtml(record.comments)}
        </section>
      </div>
    `;
  }

  function renderRecordDialogEditor(record) {
    dialogState.record = record;
    dialogState.mode = "edit";

    const tagsValue = Array.isArray(record.tags) ? record.tags.join(", ") : "";
    const visibilityValue = normalizeVisibility(record.visibility, "private");
    const formatValue = String(record.format || "").trim();
    const isTextRecord = record.content?.kind === "text";
    const textValue = isTextRecord ? String(record.content?.text || "") : "";
    const fileEditorSection = isTextRecord
      ? ""
      : `
        <section class="record-edit-file-card">
          <h4>文件替换</h4>
          ${currentFileMetaHtml(record.content)}
          <div class="record-edit-preview">
            ${contentHtml(record.content)}
          </div>
          <label>
            上传新文件（可选）
            <input type="file" name="file" data-record-edit-file-input>
          </label>
          <p class="record-edit-file-hint muted" data-record-edit-file-hint>未选择新文件，将保留当前文件。</p>
        </section>
      `;

    dialogTitle.textContent = `编辑记录 #${record.record_no}`;
    dialogBody.innerHTML = `
      <div class="dialog-content">
        <form class="record-edit-form stack-form" data-record-edit-form data-record-id="${record.id}">
          <p class="muted">${dialogRecordMeta(record)}</p>
          <div class="record-edit-grid">
            <label>
              可见性
              <select name="visibility">
                <option value="private" ${visibilityValue === "private" ? "selected" : ""}>私密</option>
                <option value="public" ${visibilityValue === "public" ? "selected" : ""}>公开</option>
              </select>
            </label>
            <label>
              格式
              <input type="text" name="format" value="${escapeHtml(formatValue)}" placeholder="留空将自动推断">
            </label>
          </div>
          <label>
            标签（逗号分隔）
            <input type="text" name="tags" value="${escapeHtml(tagsValue)}" placeholder="math,python,reading">
          </label>
          ${
            isTextRecord
              ? `
              <label>
                文本内容
                <textarea name="text" rows="8" required>${escapeHtml(textValue)}</textarea>
              </label>
              <p class="muted">文本记录可直接改写内容。</p>
            `
              : "<p class=\"muted\">文件记录可修改标签/可见性/格式，或上传新文件进行替换。</p>"
          }
          ${fileEditorSection}
          <p class="record-edit-feedback muted" data-record-edit-feedback></p>
          <div class="action-line">
            <button type="submit" data-record-edit-submit>保存修改</button>
            <button class="bubble" type="button" data-dialog-action="view-record" data-record-id="${record.id}">返回详情</button>
          </div>
        </form>
      </div>
    `;
  }

  function recordCardHtml(record, opts = {}) {
    const showUser = opts.showUser !== false;

    const meta = [];
    if (showUser) {
      meta.push(`用户: ${escapeHtml(record.user?.username || "")}`);
    }
    meta.push(`时间: ${escapeHtml(formatTime(record.created_at))}`);
    meta.push(record.visibility === "public" ? "公开" : "私密");

    const canEdit = Boolean(record.can_edit);
    const canComment = Boolean(record.can_comment);

    return `
      <article class="record-item" data-record-id="${record.id}">
        <div class="record-head">
          <strong>#${record.record_no} ${escapeHtml(record.format || "record")}</strong>
          <span class="muted">${meta.join(" | ")}</span>
        </div>
        <p>${escapeHtml(record.preview || "(无预览内容)")}</p>
        <div class="tag-line">${tagHtml(record.tags)}</div>
        <div class="action-line">
          <button class="bubble" type="button" data-action="view-record" data-record-id="${record.id}">查看内容</button>
          ${canEdit ? `<button class="bubble" type="button" data-action="edit-record" data-record-id="${record.id}">编辑</button>` : ""}
          ${canComment ? `<button class="bubble" type="button" data-action="comment-record" data-record-id="${record.id}">评论</button>` : ""}
        </div>
      </article>
    `;
  }

  function renderRecordList(container, records, opts = {}) {
    if (!container) {
      return;
    }
    if (!records || !records.length) {
      container.innerHTML = `<p class="muted">${escapeHtml(opts.emptyText || "暂无记录")}</p>`;
      return;
    }
    container.innerHTML = records.map((record) => recordCardHtml(record, opts)).join("");
  }

  function boardRecordCardHtml(record) {
    const meta = [
      `用户: ${escapeHtml(record.user?.username || "")}`,
      `时间: ${escapeHtml(formatTime(record.created_at))}`,
      record.visibility === "public" ? "公开" : "私密",
    ];

    return `
      <article class="record-item" data-record-id="${record.id}">
        <div class="record-head">
          <strong>#${record.record_no} ${escapeHtml(record.format || "record")}</strong>
          <span class="muted">${meta.join(" | ")}</span>
        </div>
        <div class="tag-line">${tagHtml(record.tags)}</div>
        <div class="action-line">
          <button class="bubble" type="button" data-action="view-record" data-record-id="${record.id}">查看内容</button>
        </div>
      </article>
    `;
  }

  function renderBoardRecordList(container, records, opts = {}) {
    if (!container) {
      return;
    }
    if (!records || !records.length) {
      container.innerHTML = `<p class="muted">${escapeHtml(opts.emptyText || "暂无记录")}</p>`;
      return;
    }
    container.innerHTML = records.map((record) => boardRecordCardHtml(record)).join("");
  }

  async function openRecordDialog(recordId) {
    try {
      const record = await fetchRecord(recordId, true);
      renderRecordDialogView(record);
      ensureDialogVisible();
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function editRecord(recordId) {
    try {
      const cachedRecord = dialogState.record && Number(dialogState.record.id) === recordId ? dialogState.record : null;
      const record = cachedRecord || (await fetchRecord(recordId, true));
      renderRecordDialogEditor(record);
      ensureDialogVisible();
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function commentRecord(recordId) {
    const body = window.prompt("请输入评论内容");
    if (!body || !body.trim()) {
      return;
    }
    try {
      await api(`/api/records/${recordId}/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: body.trim() }),
      });
      window.alert("评论已发布");
      await openRecordDialog(recordId);
    } catch (error) {
      window.alert(error.message);
    }
  }

  function bindGlobalRecordActions() {
    document.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      const button = event.target.closest("button[data-action], button[data-dialog-action]");
      if (!button) {
        return;
      }
      const action = button.dataset.action || button.dataset.dialogAction;
      const recordId = Number(button.dataset.recordId || 0);
      if (!recordId) {
        return;
      }

      const handler = recordActionHandlers[action];
      if (handler) {
        handler(recordId);
      }
    });
  }

  function initHomePanelSwitcher() {
    const tabs = Array.from(document.querySelectorAll("[data-home-panel-tab]"));
    const panels = Array.from(document.querySelectorAll("[data-home-panel]"));
    if (!tabs.length || !panels.length) {
      return;
    }

    const tabMap = new Map();
    const panelMap = new Map();
    tabs.forEach((tab) => {
      const key = String(tab.dataset.homePanelTab || "").trim();
      if (key) {
        tabMap.set(key, tab);
      }
    });
    panels.forEach((panel) => {
      const key = String(panel.dataset.homePanel || "").trim();
      if (key) {
        panelMap.set(key, panel);
      }
    });

    const keys = [...panelMap.keys()].filter((key) => tabMap.has(key));
    if (!keys.length) {
      return;
    }

    const defaultKey = keys.includes("quick") ? "quick" : keys[0];
    const normalizeKey = (value) => (keys.includes(value) ? value : defaultKey);

    const activatePanel = (key, opts = {}) => {
      const nextKey = normalizeKey(String(key || "").trim());
      const animate = opts.animate !== false;
      for (const [panelKey, panelEl] of panelMap.entries()) {
        const active = panelKey === nextKey;
        panelEl.hidden = !active;
        panelEl.classList.toggle("is-entering", active && animate);
      }
      for (const [tabKey, tabEl] of tabMap.entries()) {
        const active = tabKey === nextKey;
        tabEl.classList.toggle("is-active", active);
        tabEl.setAttribute("aria-selected", active ? "true" : "false");
      }
      try {
        window.localStorage.setItem(homePanelStorageKey, nextKey);
      } catch (_error) {
        // Ignore storage failures (private mode / restricted contexts).
      }
    };

    tabs.forEach((tabEl) => {
      tabEl.addEventListener("click", () => {
        activatePanel(tabEl.dataset.homePanelTab, { animate: true });
      });
    });
    panels.forEach((panelEl) => {
      panelEl.addEventListener("animationend", () => {
        panelEl.classList.remove("is-entering");
      });
    });

    let initialKey = defaultKey;
    try {
      initialKey = normalizeKey(window.localStorage.getItem(homePanelStorageKey) || defaultKey);
    } catch (_error) {
      initialKey = defaultKey;
    }
    activatePanel(initialKey, { animate: false });
  }

  async function loadHome() {
    const dateEl = qs("#today-date");
    const aiStatusEl = qs("#ai-status");
    const archiveStatusEl = qs("#archive-status");
    const vectorStatusEl = qs("#vector-status");
    const digestStatusEl = qs("#digest-status");
    const todayAssetsEl = qs("#today-assets-list");
    const metricPublicEl = qs("#metric-public-count");
    const metricUserEl = qs("#metric-user-count");
    const metricTagsEl = qs("#metric-top-tags");

    const data = await api("/api/home/today");
    const records = data.public_records || [];
    const todayAssets = data.digest_assets || data.today_assets || [];
    const digestDay = String(data.digest_day || data.digest_build?.day || "").trim();

    if (dateEl) {
      dateEl.textContent = `日期: ${data.date || ""} (${data.timezone || "UTC"})`;
    }
    if (aiStatusEl) {
      aiStatusEl.textContent = data.ai?.message || "未启用";
    }
    if (archiveStatusEl) {
      const archive = data.archive || {};
      const retention = archive.retention || {};
      const deletedDays = Number(retention.deleted_count || 0);
      const cleanupText = deletedDays > 0 ? `，自动清理 ${deletedDays} 天过期归档` : "";
      if (archive.saved && archive.archive) {
        const changedText = archive.archive.changed ? "已更新" : "无变更";
        setFeedback(archiveStatusEl, `归档${changedText}：${archive.archive.day || "-"}，记录 ${archive.archive.record_count || 0} 条${cleanupText}`, archive.archive.changed ? "success" : "neutral");
      } else if (archive.reason) {
        setFeedback(archiveStatusEl, `归档状态：${archive.reason}${cleanupText}`, "warning");
      } else if (cleanupText) {
        setFeedback(archiveStatusEl, `归档状态：无更新${cleanupText}`, "neutral");
      } else {
        setFeedback(archiveStatusEl, "归档状态：无更新", "neutral");
      }
    }
    if (vectorStatusEl) {
      const vector = data.vector || {};
      const base = `索引文档 ${vector.doc_count || 0} 条，语料文件 ${vector.archive_count || 0} 个`;
      if (vector.error) {
        setFeedback(vectorStatusEl, `${base}（${vector.error}）`, "error");
      } else {
        setFeedback(vectorStatusEl, base, "success");
      }
    }
    if (digestStatusEl) {
      const digest = data.digest_build || {};
      const status = String(digest.status || "unknown");
      const message = String(digest.message || "").trim();
      const dayPrefix = digestDay ? `${digestDay} ` : "";
      const statusMessage = message
        ? `${dayPrefix}自动生成状态：${status} (${message})`
        : `${dayPrefix}自动生成状态：${status}`;
      const tone = status === "ready" ? "success" : status === "failed" ? "error" : status === "partial" ? "warning" : "neutral";
      setFeedback(digestStatusEl, statusMessage, tone);
    }
    if (todayAssetsEl) {
      if (!todayAssets.length) {
        const dayText = digestDay ? `${escapeHtml(digestDay)} 的日报内容尚未生成` : "已闭合日报内容尚未生成";
        todayAssetsEl.innerHTML = `<p class="muted">${dayText}</p>`;
      } else {
        todayAssetsEl.innerHTML = todayAssets.map((asset) => echoAssetCardHtml(asset)).join("");
      }
    }

    const userSet = new Set();
    const tagCounter = new Map();
    records.forEach((record) => {
      if (record.user?.id) {
        userSet.add(record.user.id);
      }
      (record.tags || []).forEach((tag) => {
        tagCounter.set(tag, (tagCounter.get(tag) || 0) + 1);
      });
    });
    const topTags = [...tagCounter.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 3)
      .map((item) => `#${item[0]}`);

    if (metricPublicEl) {
      metricPublicEl.textContent = String(records.length);
    }
    if (metricUserEl) {
      metricUserEl.textContent = String(userSet.size);
    }
    if (metricTagsEl) {
      metricTagsEl.textContent = topTags.length ? topTags.join(" ") : "-";
    }
    return data;
  }

  function vectorHitHtml(hit) {
    const tags = Array.isArray(hit.tags) && hit.tags.length ? hit.tags.map((tag) => `#${escapeHtml(tag)}`).join(" ") : "无标签";
    return `
      <article class="vector-hit-item">
        <p><strong>${escapeHtml(hit.day || "")} #${Number(hit.record_id || 0)}</strong> | 用户: ${escapeHtml(hit.username || "-")} | score=${Number(hit.score || 0).toFixed(3)}</p>
        <p class="muted">${escapeHtml(tags)}</p>
        <p>${escapeHtml(hit.snippet || "")}</p>
      </article>
    `;
  }

  function renderVectorHits(hits) {
    const wrap = qs("#vector-chat-hits");
    if (!wrap) {
      return;
    }
    if (!Array.isArray(hits) || !hits.length) {
      wrap.innerHTML = "<p class=\"muted\">暂无检索命中</p>";
      return;
    }
    wrap.innerHTML = hits.map((hit) => vectorHitHtml(hit)).join("");
  }

  async function initHome() {
    initHomePanelSwitcher();

    const form = qs("#quick-publish-form");
    const msgEl = qs("#quick-publish-msg");
    const vectorForm = qs("#vector-chat-form");
    const vectorAnswerEl = qs("#vector-chat-answer");
    const vectorStatusEl = qs("#vector-status");
    const vectorRebuildBtn = qs("#vector-rebuild-btn");

    if (form) {
      const visibilityInput = qs('select[name="visibility"]', form);
      const tagsInput = qs('input[name="tags"]', form);
      const textInput = qs('textarea[name="text"]', form);
      const fileInput = qs("#quick-publish-file-input", form);
      const folderInput = qs("#quick-publish-folder-input", form);
      const dropzone = qs("#quick-publish-dropzone", form);
      const filePickerBtn = qs("#quick-publish-file-btn", form);
      const folderPickerBtn = qs("#quick-publish-folder-btn", form);
      const fileHint = qs("#quick-publish-file-hint", form);
      const fileList = qs("#quick-publish-file-list", form);
      const submitBtn = qs('button[type="submit"]', form);
      const progressWrap = qs("#quick-publish-progress-wrap", form);
      const progressTextEl = qs("#quick-publish-progress-text", form);
      const progressFillEl = qs("#quick-publish-progress-fill", form);
      const progressBarEl = qs(".quick-publish-progress-bar", form);
      const retryFailedBtn = qs("#quick-publish-retry-failed-btn", form);

      const uploadStatusLabel = {
        pending: "待发布",
        uploading: "上传中",
        success: "已成功",
        failed: "失败",
      };
      const textTask = {
        status: "idle",
        error: "",
        attempts: 0,
      };

      let selectedFiles = [];
      let dragDepth = 0;
      let isPublishing = false;

      const trimUploadError = (error) => String(error?.message || error || "未知错误").trim().slice(0, 260);
      const newUploadId = () => `upload-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const publishFields = () => ({
        visibility: normalizeVisibility(visibilityInput?.value || "private"),
        tags: String(tagsInput?.value || "").trim(),
        text: String(textInput?.value || "").trim(),
      });
      const fileDisplayPath = (item) =>
        String(item?.relativePath || item?.file?.webkitRelativePath || item?.file?.name || "").replace(/^\/+/, "");
      const fileIdentity = (item) => `${fileDisplayPath(item)}::${item.file?.size || 0}::${item.file?.lastModified || 0}`;

      const setProgress = (completed, total, label = "") => {
        if (!progressWrap || !progressTextEl || !progressFillEl) {
          return;
        }
        const safeTotal = Math.max(0, Number(total || 0));
        const safeCompleted = Math.max(0, Number(completed || 0));
        const ratio = safeTotal > 0 ? Math.min(1, safeCompleted / safeTotal) : 0;
        const percent = Math.round(ratio * 100);
        progressWrap.hidden = false;
        progressFillEl.style.width = `${percent}%`;
        progressTextEl.textContent = label || `发布进度 ${safeCompleted}/${safeTotal}`;
        if (progressBarEl) {
          progressBarEl.setAttribute("aria-valuenow", String(percent));
        }
      };

      const hideProgress = () => {
        if (progressWrap) {
          progressWrap.hidden = true;
        }
      };

      const resetTextTask = () => {
        textTask.status = "idle";
        textTask.error = "";
        textTask.attempts = 0;
      };

      const setPublishingState = (value) => {
        isPublishing = Boolean(value);
        setButtonBusy(submitBtn, isPublishing, { busyText: "发布中..." });
        if (visibilityInput) {
          visibilityInput.disabled = isPublishing;
        }
        if (tagsInput) {
          tagsInput.disabled = isPublishing;
        }
        if (textInput) {
          textInput.disabled = isPublishing;
        }
        if (fileInput) {
          fileInput.disabled = isPublishing;
        }
        if (folderInput) {
          folderInput.disabled = isPublishing;
        }
        if (filePickerBtn) {
          filePickerBtn.disabled = isPublishing;
        }
        if (folderPickerBtn) {
          folderPickerBtn.disabled = isPublishing;
        }
        if (dropzone) {
          dropzone.classList.toggle("is-disabled", isPublishing);
        }
      };

      const renderSelectedFiles = () => {
        const failedFileCount = selectedFiles.filter((item) => item.status === "failed").length;
        const retryableText = Boolean(publishFields().text) && textTask.status === "failed";

        if (fileHint) {
          if (!selectedFiles.length) {
            fileHint.textContent = "未选择文件，可仅发布文本。";
          } else if (failedFileCount > 0) {
            fileHint.textContent = `已选择 ${selectedFiles.length} 个文件，失败 ${failedFileCount} 个，可重试。`;
          } else {
            fileHint.textContent = `已选择 ${selectedFiles.length} 个文件，可批量发布。`;
          }
        }
        if (retryFailedBtn) {
          retryFailedBtn.hidden = isPublishing || (!failedFileCount && !retryableText);
          retryFailedBtn.disabled = isPublishing;
        }
        if (dropzone) {
          dropzone.classList.toggle("has-files", selectedFiles.length > 0);
        }
        if (!isPublishing && !failedFileCount && !retryableText) {
          const hasUploading = selectedFiles.some((item) => item.status === "uploading");
          if (!hasUploading) {
            hideProgress();
          }
        }

        if (!fileList) {
          return;
        }
        if (!selectedFiles.length) {
          fileList.innerHTML = "";
          return;
        }

        fileList.innerHTML = selectedFiles
          .map(
            (item) => `
            <li class="quick-publish-file-item">
              <div class="quick-publish-file-main">
                <span class="quick-publish-file-name" title="${escapeHtml(fileDisplayPath(item))}">${escapeHtml(fileDisplayPath(item))}</span>
                <span class="quick-publish-file-meta">${escapeHtml(formatFileSize(item.file.size))}</span>
              </div>
              <div class="quick-publish-file-actions">
                <span class="quick-publish-file-status is-${escapeHtml(item.status)}">${escapeHtml(uploadStatusLabel[item.status] || "待发布")}</span>
                ${
                  !isPublishing
                    ? `<button type="button" class="quick-publish-file-remove" data-remove-upload-id="${escapeHtml(item.id)}">移除</button>`
                    : ""
                }
                ${
                  !isPublishing && item.status === "failed"
                    ? `<button type="button" class="quick-publish-file-retry" data-retry-upload-id="${escapeHtml(item.id)}">重试</button>`
                    : ""
                }
              </div>
              ${item.error ? `<p class="quick-publish-file-error">${escapeHtml(item.error)}</p>` : ""}
            </li>
          `,
          )
          .join("");
      };

      const addFileCandidates = (candidates) => {
        const rows = Array.isArray(candidates) ? candidates : [];
        if (!rows.length) {
          return 0;
        }
        const existed = new Set(selectedFiles.map((item) => fileIdentity(item)));
        let added = 0;
        for (const row of rows) {
          const file = row?.file;
          if (!(file instanceof File) || !file.name) {
            continue;
          }
          const relativePath = String(row.relativePath || file.webkitRelativePath || file.name).replace(/^\/+/, "") || file.name;
          const identity = fileIdentity({ file, relativePath });
          if (existed.has(identity)) {
            continue;
          }
          existed.add(identity);
          selectedFiles.push({
            id: newUploadId(),
            file,
            relativePath,
            status: "pending",
            error: "",
            attempts: 0,
          });
          added += 1;
        }
        if (added > 0) {
          if (msgEl) {
            setFeedback(msgEl, `已加入 ${added} 个文件`, "info");
          }
          renderSelectedFiles();
        }
        return added;
      };

      const removeFileById = (id) => {
        if (!id) {
          return;
        }
        selectedFiles = selectedFiles.filter((item) => item.id !== id);
        renderSelectedFiles();
      };

      const clearSelectedFiles = () => {
        selectedFiles = [];
        if (fileInput) {
          fileInput.value = "";
        }
        if (folderInput) {
          folderInput.value = "";
        }
        renderSelectedFiles();
      };

      const collectFilesFromInput = (fileListValue) =>
        Array.from(fileListValue || [])
          .filter((item) => item instanceof File && item.name)
          .map((file) => ({
            file,
            relativePath: String(file.webkitRelativePath || file.name || "").replace(/^\/+/, "") || file.name,
          }));

      const readFileEntry = (entry) =>
        new Promise((resolve) => {
          entry.file(
            (file) => resolve(file || null),
            () => resolve(null),
          );
        });

      const readDirectoryEntries = (reader) =>
        new Promise((resolve) => {
          reader.readEntries(
            (entries) => resolve(Array.isArray(entries) ? entries : []),
            () => resolve([]),
          );
        });

      const walkFileSystemEntry = async (entry, prefix = "") => {
        if (!entry) {
          return [];
        }
        if (entry.isFile) {
          const file = await readFileEntry(entry);
          if (!(file instanceof File) || !file.name) {
            return [];
          }
          const fallbackPath = String(entry.fullPath || "").replace(/^\/+/, "");
          const relativePath = `${prefix}${file.name}` || fallbackPath || file.name;
          return [{ file, relativePath }];
        }
        if (!entry.isDirectory || typeof entry.createReader !== "function") {
          return [];
        }
        const nextPrefix = prefix ? `${prefix}${entry.name}/` : `${entry.name}/`;
        const reader = entry.createReader();
        const files = [];
        while (true) {
          const chunk = await readDirectoryEntries(reader);
          if (!chunk.length) {
            break;
          }
          for (const child of chunk) {
            const childFiles = await walkFileSystemEntry(child, nextPrefix);
            files.push(...childFiles);
          }
        }
        return files;
      };

      const collectDroppedFiles = async (dataTransfer) => {
        const items = Array.from(dataTransfer?.items || []);
        if (items.length) {
          const expanded = [];
          for (const item of items) {
            if (typeof item.webkitGetAsEntry === "function") {
              const entry = item.webkitGetAsEntry();
              if (entry) {
                const files = await walkFileSystemEntry(entry, "");
                expanded.push(...files);
                continue;
              }
            }
            const fallbackFile = item.getAsFile?.();
            if (fallbackFile instanceof File && fallbackFile.name) {
              expanded.push({
                file: fallbackFile,
                relativePath: fallbackFile.name,
              });
            }
          }
          if (expanded.length) {
            return expanded;
          }
        }
        return collectFilesFromInput(dataTransfer?.files);
      };

      const buildUploadPayload = ({ visibility, tags, text = "", file = null }) => {
        const payload = new FormData();
        payload.set("visibility", visibility || "private");
        if (tags) {
          payload.set("tags", tags);
        }
        if (text) {
          payload.set("text", text);
        }
        if (file) {
          payload.append("file", file, file.name);
        }
        return payload;
      };

      const uploadTextTask = async ({ visibility, tags, text }) => {
        textTask.status = "uploading";
        textTask.error = "";
        textTask.attempts += 1;
        await api("/api/push", {
          method: "POST",
          body: buildUploadPayload({ visibility, tags, text }),
        });
        textTask.status = "success";
        textTask.error = "";
      };

      const uploadFileTask = async ({ visibility, tags }, item) => {
        item.status = "uploading";
        item.error = "";
        item.attempts += 1;
        renderSelectedFiles();
        await api("/api/push", {
          method: "POST",
          body: buildUploadPayload({
            visibility,
            tags,
            file: item.file,
          }),
        });
        item.status = "success";
        item.error = "";
      };

      const runPublish = async (opts = {}) => {
        if (isPublishing) {
          return;
        }
        const fields = publishFields();
        const mode = String(opts.mode || "all");
        const targetIdSet = Array.isArray(opts.fileIds) ? new Set(opts.fileIds) : null;
        const hasText = Boolean(fields.text);

        let needTextTask = false;
        if (mode === "failed") {
          needTextTask = hasText && textTask.status === "failed";
        } else if (mode === "single") {
          needTextTask = false;
        } else {
          needTextTask = hasText && textTask.status !== "success";
        }

        let fileTargets = [];
        if (mode === "failed") {
          fileTargets = selectedFiles.filter((item) => item.status === "failed");
        } else if (mode === "single" && targetIdSet) {
          fileTargets = selectedFiles.filter((item) => targetIdSet.has(item.id) && item.status !== "uploading");
        } else {
          fileTargets = selectedFiles.filter((item) => item.status !== "success");
        }

        if (!needTextTask && !fileTargets.length) {
          if (msgEl) {
            setFeedback(msgEl, "没有需要发布的项", "warning");
          }
          return;
        }

        setPublishingState(true);
        renderSelectedFiles();

        const totalTasks = fileTargets.length + (needTextTask ? 1 : 0);
        let completedTasks = 0;
        let successCount = 0;
        let failedCount = 0;
        setProgress(0, totalTasks, `发布进度 0/${totalTasks}`);
        if (msgEl) {
          setFeedback(msgEl, "发布中...", "info");
        }

        if (needTextTask) {
          try {
            setProgress(completedTasks, totalTasks, `正在发布文本 (${completedTasks + 1}/${totalTasks})`);
            await uploadTextTask(fields);
            successCount += 1;
          } catch (error) {
            textTask.status = "failed";
            textTask.error = trimUploadError(error);
            failedCount += 1;
          } finally {
            completedTasks += 1;
            setProgress(completedTasks, totalTasks, `发布进度 ${completedTasks}/${totalTasks}`);
          }
        }

        for (const item of fileTargets) {
          try {
            setProgress(completedTasks, totalTasks, `正在上传 ${fileDisplayPath(item)} (${completedTasks + 1}/${totalTasks})`);
            await uploadFileTask(fields, item);
            successCount += 1;
          } catch (error) {
            item.status = "failed";
            item.error = trimUploadError(error);
            failedCount += 1;
          } finally {
            completedTasks += 1;
            setProgress(completedTasks, totalTasks, `发布进度 ${completedTasks}/${totalTasks}`);
            renderSelectedFiles();
          }
        }

        setPublishingState(false);
        renderSelectedFiles();

        if (failedCount > 0 && successCount > 0) {
          if (msgEl) {
            setFeedback(msgEl, `部分成功：成功 ${successCount}，失败 ${failedCount}`, "warning");
          }
        } else if (failedCount > 0) {
          if (msgEl) {
            setFeedback(msgEl, `发布失败 ${failedCount} 项，可点击重试`, "error");
          }
        } else if (msgEl) {
          setFeedback(msgEl, successCount > 1 ? `发布成功，共 ${successCount} 条记录` : "发布成功", "success");
        }

        if (successCount > 0) {
          try {
            await loadHome();
          } catch (error) {
            if (msgEl) {
              setFeedback(msgEl, `${msgEl.textContent || ""}（列表刷新失败: ${trimUploadError(error)}）`, "warning");
            }
          }
        }
        if (failedCount === 0) {
          hideProgress();
          form.reset();
          clearSelectedFiles();
          resetTextTask();
        }
      };

      if (filePickerBtn && fileInput) {
        filePickerBtn.addEventListener("click", () => {
          if (isPublishing) {
            return;
          }
          fileInput.click();
        });
      }
      if (folderPickerBtn && folderInput) {
        folderPickerBtn.addEventListener("click", () => {
          if (isPublishing) {
            return;
          }
          folderInput.click();
        });
      }
      if (fileInput) {
        fileInput.addEventListener("change", (event) => {
          const added = addFileCandidates(collectFilesFromInput(event.target.files));
          if (!added && msgEl) {
            setFeedback(msgEl, "未新增文件（可能都已在列表中）", "warning");
          }
          fileInput.value = "";
        });
      }
      if (folderInput) {
        folderInput.addEventListener("change", (event) => {
          const added = addFileCandidates(collectFilesFromInput(event.target.files));
          if (!added && msgEl) {
            setFeedback(msgEl, "未新增文件（可能都已在列表中）", "warning");
          }
          folderInput.value = "";
        });
      }
      if (fileList) {
        fileList.addEventListener("click", (event) => {
          if (!(event.target instanceof Element)) {
            return;
          }
          const removeBtn = event.target.closest("[data-remove-upload-id]");
          if (removeBtn && !isPublishing) {
            removeFileById(String(removeBtn.getAttribute("data-remove-upload-id") || ""));
            return;
          }
          const retryBtn = event.target.closest("[data-retry-upload-id]");
          if (retryBtn && !isPublishing) {
            const id = String(retryBtn.getAttribute("data-retry-upload-id") || "");
            if (!id) {
              return;
            }
            const target = selectedFiles.find((item) => item.id === id);
            if (!target) {
              return;
            }
            target.status = "pending";
            target.error = "";
            runPublish({ mode: "single", fileIds: [id] }).catch((error) => {
              if (msgEl) {
                setFeedback(msgEl, trimUploadError(error), "error");
              }
            });
          }
        });
      }
      if (dropzone && fileInput) {
        const haltDragEvent = (event) => {
          event.preventDefault();
          event.stopPropagation();
        };

        dropzone.addEventListener("click", (event) => {
          if (isPublishing) {
            return;
          }
          if (event.target instanceof Element && event.target.closest("button")) {
            return;
          }
          fileInput.click();
        });
        dropzone.addEventListener("keydown", (event) => {
          if (isPublishing) {
            return;
          }
          if (event.key !== "Enter" && event.key !== " ") {
            return;
          }
          event.preventDefault();
          fileInput.click();
        });
        dropzone.addEventListener("dragenter", (event) => {
          haltDragEvent(event);
          dragDepth += 1;
          dropzone.classList.add("is-dragover");
        });
        dropzone.addEventListener("dragover", haltDragEvent);
        dropzone.addEventListener("dragleave", (event) => {
          haltDragEvent(event);
          dragDepth = Math.max(0, dragDepth - 1);
          if (!dragDepth) {
            dropzone.classList.remove("is-dragover");
          }
        });
        dropzone.addEventListener("drop", (event) => {
          haltDragEvent(event);
          dragDepth = 0;
          dropzone.classList.remove("is-dragover");
          if (isPublishing) {
            return;
          }
          collectDroppedFiles(event.dataTransfer)
            .then((rows) => {
              const added = addFileCandidates(rows);
              if (!added && msgEl) {
                setFeedback(msgEl, "未新增文件（可能都已在列表中）", "warning");
              }
            })
            .catch((error) => {
              if (msgEl) {
                setFeedback(msgEl, `读取拖拽内容失败: ${trimUploadError(error)}`, "error");
              }
            });
        });
      }
      if (textInput) {
        textInput.addEventListener("input", () => {
          if (textTask.status !== "uploading") {
            resetTextTask();
            renderSelectedFiles();
          }
        });
      }
      if (retryFailedBtn) {
        retryFailedBtn.addEventListener("click", () => {
          if (isPublishing) {
            return;
          }
          runPublish({ mode: "failed" }).catch((error) => {
            if (msgEl) {
              setFeedback(msgEl, trimUploadError(error), "error");
            }
          });
        });
      }

      hideProgress();
      renderSelectedFiles();

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const textValue = String(textInput?.value || "").trim();
        if (!textValue && !selectedFiles.length) {
          window.alert("请填写文本或选择至少一个文件");
          return;
        }
        try {
          await runPublish({ mode: "all" });
        } catch (error) {
          if (msgEl) {
            setFeedback(msgEl, trimUploadError(error), "error");
          }
        }
      });
    }

    if (vectorForm) {
      vectorForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const formData = new FormData(vectorForm);
        const query = String(formData.get("query") || "").trim();
        if (!query) {
          window.alert("请输入问题");
          return;
        }
        const topK = Number(formData.get("top_k") || 6);
        const useAiChecked = Boolean(qs('input[name="use_ai"]', vectorForm)?.checked);
        const submitBtn = qs('button[type="submit"]', vectorForm);
        setButtonBusy(submitBtn, true, { busyText: "检索中..." });
        if (vectorAnswerEl) {
          vectorAnswerEl.hidden = false;
          vectorAnswerEl.textContent = "检索中...";
        }
        try {
          const data = await api("/api/vector/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              query,
              top_k: topK,
              use_ai: useAiChecked,
            }),
          });
          if (vectorAnswerEl) {
            const aiFlag = data.ai_used ? "AI 已增强回答" : "检索直答";
            vectorAnswerEl.textContent = `${aiFlag}\n\n${data.answer || ""}`;
          }
          renderVectorHits(data.citations || []);
          if (vectorStatusEl) {
            const vector = data.vector || {};
            setFeedback(vectorStatusEl, `索引文档 ${vector.doc_count || 0} 条，语料文件 ${vector.archive_count || 0} 个`, "success");
          }
        } catch (error) {
          if (vectorAnswerEl) {
            vectorAnswerEl.hidden = false;
            vectorAnswerEl.textContent = error.message;
          }
          if (vectorStatusEl) {
            setFeedback(vectorStatusEl, `问答失败: ${error.message || "未知错误"}`, "error");
          }
        } finally {
          setButtonBusy(submitBtn, false);
        }
      });
    }

    if (vectorRebuildBtn) {
      vectorRebuildBtn.addEventListener("click", async () => {
        setButtonBusy(vectorRebuildBtn, true, { busyText: "重建中..." });
        if (vectorStatusEl) {
          setFeedback(vectorStatusEl, "正在重建向量索引...", "info");
        }
        try {
          const data = await api("/api/vector/rebuild", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
          });
          if (vectorStatusEl) {
            setFeedback(vectorStatusEl, `索引已重建：文档 ${data.doc_count || 0} 条，语料文件 ${data.archive_count || 0} 个`, "success");
          }
          if (vectorAnswerEl) {
            vectorAnswerEl.hidden = false;
            vectorAnswerEl.textContent = `向量索引重建完成\nbuilt_at=${data.built_at || ""}`;
          }
          renderVectorHits([]);
        } catch (error) {
          if (vectorStatusEl) {
            setFeedback(vectorStatusEl, `重建失败: ${error.message || "未知错误"}`, "error");
          }
        } finally {
          setButtonBusy(vectorRebuildBtn, false);
        }
      });
    }

    renderVectorHits([]);
    await loadHome();
  }

  function renderBoardTable(data, tagValue) {
    const wrap = qs("#board-table-wrap");
    if (!wrap) {
      return;
    }

    const dates = Array.isArray(data.dates) ? data.dates : [];
    const users = Array.isArray(data.users) ? data.users : [];
    const matrix = data.matrix || {};
    const escapedDates = dates.map((day) => escapeHtml(day));
    const escapedTag = escapeHtml(tagValue || "");
    let maxCount = 0;
    let totalCount = 0;
    let activeCells = 0;
    const dayTotals = Array(dates.length).fill(0);
    const peak = {
      count: 0,
      user: "-",
      day: "-",
    };

    if (!dates.length || !users.length) {
      wrap.innerHTML = `<p class="muted">暂无可展示的 Board 数据</p>`;
      return;
    }

    const rows = users.map((user) => {
      const userCounts = [];
      const rowMatrix = matrix[String(user.id)] || {};
      let userTotal = 0;
      dates.forEach((day, dayIndex) => {
        const count = Number(rowMatrix[day] || 0) || 0;
        userCounts.push(count);
        userTotal += count;
        totalCount += count;
        if (count > 0) {
          activeCells += 1;
        }
        if (count > maxCount) {
          maxCount = count;
        }
        dayTotals[dayIndex] += count;
        if (count > peak.count) {
          peak.count = count;
          peak.user = user.username || "-";
          peak.day = day;
        }
      });
      return {
        user,
        userCounts,
        userTotal,
      };
    });
    const activeUsers = rows.filter((row) => row.userTotal > 0).length;
    const totalCells = dates.length * users.length;
    const activityRate = totalCells > 0 ? Math.round((activeCells / totalCells) * 100) : 0;

    const headHtml = dates
      .map((day, index) => {
        const escapedDay = escapedDates[index];
        const dayTotal = dayTotals[index] || 0;
        return `
          <th class="clickable board-day-head-cell" data-board-action="day" data-day="${escapedDay}" data-tag="${escapedTag}" title="查看 ${escapedDay} 的公开记录">
            <span class="board-day-label">${escapedDay}</span>
            <span class="board-day-total">总 ${dayTotal}</span>
          </th>
        `;
      })
      .join("");

    const bodyHtml = rows
      .map(({ user, userCounts, userTotal }) => {
        const username = String(user.username || `用户#${user.id}`);
        const escapedUsername = escapeHtml(username);
        const cells = userCounts
          .map((count, index) => {
            const escapedDay = escapedDates[index];
            let level = 0;
            if (maxCount > 0) {
              const ratio = count / maxCount;
              if (ratio >= 0.8) {
                level = 4;
              } else if (ratio >= 0.55) {
                level = 3;
              } else if (ratio >= 0.3) {
                level = 2;
              } else if (ratio > 0) {
                level = 1;
              }
            }
            const title = `${username} 在 ${dates[index]} 有 ${count} 条记录`;
            return `
              <td
                class="clickable heat-${level} ${count === 0 ? "is-zero" : "is-active"}"
                data-board-action="cell"
                data-user-id="${user.id}"
                data-day="${escapedDay}"
                data-tag="${escapedTag}"
                data-count="${count}"
                title="${escapeHtml(title)}"
                aria-label="${escapeHtml(title)}"
              >
                <span class="board-cell-count">${count}</span>
              </td>
            `;
          })
          .join("");
        return `
          <tr>
            <th class="clickable board-sticky-col board-user-head" data-board-action="user" data-user-id="${user.id}" data-tag="${escapedTag}" title="查看 ${escapedUsername} 的可见记录">
              <span class="board-row-user">${escapedUsername}</span>
              <span class="board-row-total">总 ${userTotal}</span>
            </th>
            ${cells}
          </tr>
        `;
      })
      .join("");

    wrap.innerHTML = `
      <div class="board-insight">
        <article class="board-insight-item">
          <span>总记录数</span>
          <strong>${totalCount}</strong>
        </article>
        <article class="board-insight-item">
          <span>活跃用户</span>
          <strong>${activeUsers}/${users.length}</strong>
        </article>
        <article class="board-insight-item">
          <span>活跃格子率</span>
          <strong>${activityRate}%</strong>
        </article>
        <article class="board-insight-item">
          <span>峰值单元</span>
          <strong>${escapeHtml(peak.user)} · ${escapeHtml(peak.day)} · ${peak.count}</strong>
        </article>
      </div>
      <div class="board-legend" aria-label="热力图图例">
        <span class="board-legend-label">热力强度</span>
        <span class="board-legend-chip"><i class="board-legend-swatch level-0"></i>0</span>
        <span class="board-legend-chip"><i class="board-legend-swatch level-1"></i>低</span>
        <span class="board-legend-chip"><i class="board-legend-swatch level-2"></i>中</span>
        <span class="board-legend-chip"><i class="board-legend-swatch level-3"></i>高</span>
        <span class="board-legend-chip"><i class="board-legend-swatch level-4"></i>峰值</span>
        ${
          escapedTag
            ? `<span class="board-legend-tag">当前标签: #${escapedTag}</span>`
            : `<span class="board-legend-tag">当前标签: 全部</span>`
        }
      </div>
      <div class="board-table-scroll">
        <table class="board-table" aria-label="学习记录热力表">
          <thead>
            <tr>
              <th class="board-corner board-sticky-col">用户 \\ 日期</th>
              ${headHtml}
            </tr>
          </thead>
          <tbody>${bodyHtml}</tbody>
        </table>
      </div>
    `;
  }

  function boardTableLoadingHtml() {
    return `
      <div class="board-loading-grid" aria-live="polite">
        <div class="board-loading-item"></div>
        <div class="board-loading-item"></div>
        <div class="board-loading-item"></div>
        <div class="board-loading-item"></div>
      </div>
    `;
  }

  function boardSideLoadingHtml() {
    return `
      <div class="board-side-loading" aria-live="polite">
        <div class="board-side-loading-row"></div>
        <div class="board-side-loading-row"></div>
        <div class="board-side-loading-row"></div>
      </div>
    `;
  }

  async function loadBoardSide(title, path) {
    const titleEl = qs("#board-side-title");
    const listEl = qs("#board-side-list");
    if (!listEl) {
      return;
    }
    if (titleEl) {
      titleEl.textContent = title;
    }

    listEl.innerHTML = boardSideLoadingHtml();
    try {
      const data = await api(path);
      renderBoardRecordList(listEl, data.items || [], { emptyText: "无匹配记录" });
    } catch (error) {
      listEl.innerHTML = `<p class="feedback-inline" data-tone="error">读取失败: ${escapeHtml(error.message || "未知错误")}</p>`;
    }
  }

  async function loadBoard() {
    const form = qs("#board-filter-form");
    const submitBtn = form ? qs('button[type="submit"]', form) : null;
    const wrap = qs("#board-table-wrap");
    if (wrap) {
      wrap.innerHTML = boardTableLoadingHtml();
    }
    setButtonBusy(submitBtn, true, { busyText: "刷新中..." });
    const formData = new FormData(form);
    const tag = String(formData.get("tag") || "").trim();
    const query = buildQuery({
      days: formData.get("days") || 7,
      tag,
    });
    try {
      const data = await api(`/api/board?${query}`);
      renderBoardTable(data, tag);
    } catch (error) {
      if (wrap) {
        wrap.innerHTML = `<p class="feedback-inline" data-tone="error">Board 加载失败: ${escapeHtml(error.message || "未知错误")}</p>`;
      }
    } finally {
      setButtonBusy(submitBtn, false);
    }
  }

  async function initBoard() {
    const form = qs("#board-filter-form");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        loadBoard().catch((error) => window.console.error(error));
      });
    }

    const wrap = qs("#board-table-wrap");
    if (wrap) {
      wrap.addEventListener("click", (event) => {
        if (!(event.target instanceof Element)) {
          return;
        }
        const trigger = event.target.closest("[data-board-action]");
        if (!trigger) {
          return;
        }

        const action = trigger.dataset.boardAction;
        const tag = trigger.dataset.tag || "";
        const tagQuery = tag ? `&tag=${encodeURIComponent(tag)}` : "";

        switch (action) {
          case "user": {
            const userId = trigger.dataset.userId;
            loadBoardSide(`用户 #${userId} 的可见记录`, `/api/board/user/${userId}/records?${buildQuery({ tag })}`).catch((error) => window.console.error(error));
            break;
          }
          case "day": {
            const day = trigger.dataset.day;
            loadBoardSide(`${day} 的公开记录`, `/api/board/date/${day}?${buildQuery({ tag })}`).catch((error) => window.console.error(error));
            break;
          }
          case "cell": {
            const userId = trigger.dataset.userId;
            const day = trigger.dataset.day;
            loadBoardSide(`用户 #${userId} 在 ${day} 的记录`, `/api/board/cell?user_id=${encodeURIComponent(userId)}&day=${encodeURIComponent(day)}${tagQuery}`).catch((error) => window.console.error(error));
            break;
          }
          default:
            break;
        }
      });
    }

    await loadBoard();
  }

  function normalizeEchoFileType(value) {
    const normalized = String(value || "").trim().toLowerCase();
    if (!normalized || normalized === "all") {
      return "";
    }
    return Object.prototype.hasOwnProperty.call(echoFileTypeLabels, normalized) ? normalized : "";
  }

  function isWebType(contentType, filenameOrExt = "") {
    const normalizedType = String(contentType || "").split(";", 1)[0].trim().toLowerCase();
    const normalizedName = String(filenameOrExt || "").trim().toLowerCase();
    if (webContentTypes.has(normalizedType)) {
      return true;
    }
    return webFileExtensions.some((ext) => normalizedName.endsWith(ext) || normalizedName === ext);
  }

  function mimeMatch(normalizedType, candidates) {
    if (candidates.has(normalizedType)) {
      return true;
    }
    for (const mime of candidates) {
      if (normalizedType.startsWith(`${mime};`)) {
        return true;
      }
    }
    return false;
  }

  function detectEchoFileTypeFromMeta(contentType, filenameOrExt = "") {
    const normalizedType = String(contentType || "").split(";", 1)[0].trim().toLowerCase();
    const normalizedName = String(filenameOrExt || "").trim().toLowerCase();
    if (!normalizedType && !normalizedName) {
      return "";
    }
    if (isWebType(normalizedType, normalizedName)) {
      return "web";
    }
    if (logFileExtensions.some((ext) => normalizedName.endsWith(ext))) {
      return "log";
    }
    if (mimeMatch(normalizedType, databaseContentTypes) || databaseFileExtensions.some((ext) => normalizedName.endsWith(ext))) {
      return "database";
    }
    if (mimeMatch(normalizedType, archiveContentTypes) || archiveFileExtensions.some((ext) => normalizedName.endsWith(ext))) {
      return "archive";
    }
    if (mimeMatch(normalizedType, documentContentTypes) || documentFileExtensions.some((ext) => normalizedName.endsWith(ext))) {
      return "document";
    }
    return "";
  }

  function echoFileTypeFromRecord(record) {
    const content = record?.content || {};
    if (content.kind === "text") {
      return "text";
    }
    const fileLikeType = detectEchoFileTypeFromMeta(content.content_type, content.filename);
    if (fileLikeType) {
      return fileLikeType;
    }
    const mediaType = normalizeEchoFileType(content.media_type || "");
    if (mediaType) {
      return mediaType;
    }

    const contentType = String(content.content_type || "").toLowerCase();
    if (contentType.startsWith("image/")) {
      return "image";
    }
    if (contentType.startsWith("video/")) {
      return "video";
    }
    if (contentType.startsWith("audio/")) {
      return "audio";
    }
    if (contentType.startsWith("text/")) {
      return "text";
    }
    return "file";
  }

  function echoFileTypeFromAsset(asset) {
    const kind = String(asset.kind || "").toLowerCase();
    if (kind === "blog_html") {
      return "web";
    }
    const fileLikeType = detectEchoFileTypeFromMeta(asset.content_type, asset.ext);
    if (fileLikeType) {
      return fileLikeType;
    }

    const contentType = String(asset.content_type || "").toLowerCase();
    if (contentType.startsWith("image/")) {
      return "image";
    }
    if (contentType.startsWith("video/")) {
      return "video";
    }
    if (contentType.startsWith("audio/")) {
      return "audio";
    }
    if (contentType.startsWith("text/")) {
      return "text";
    }
    return "file";
  }

  function echoSourceBadgeHtml(sourceType) {
    if (sourceType === "asset") {
      return `<span class="echo-badge source-asset">AI 资产</span>`;
    }
    return `<span class="echo-badge source-record">公开记录</span>`;
  }

  function echoFileBadgeHtml(fileType) {
    const normalized = normalizeEchoFileType(fileType) || "file";
    const label = echoFileTypeLabels[normalized] || "文件";
    return `<span class="echo-badge file-${normalized}">${escapeHtml(label)}</span>`;
  }

  function setEchoesStatus(text, tone = "neutral") {
    const statusEl = qs("#echoes-status");
    if (statusEl) {
      setFeedback(statusEl, text, tone);
    }
  }

  function setEchoesCount() {
    const countEl = qs("#echoes-count");
    if (!countEl) {
      return;
    }
    const typeLabel = echoesState.fileType ? (echoFileTypeLabels[echoesState.fileType] || "全部") : "全部";
    countEl.textContent = `已显示 ${echoesState.visibleCount} 条 · ${typeLabel}`;
  }

  function setActiveEchoesType(fileType) {
    const normalized = normalizeEchoFileType(fileType);
    const chips = document.querySelectorAll("#echoes-type-chips button[data-file-type]");
    chips.forEach((chip) => {
      const chipType = normalizeEchoFileType(chip.dataset.fileType || "");
      const active = chipType === normalized;
      chip.classList.toggle("active", active);
      chip.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setEchoesLoadingVisible(visible) {
    const loadingEl = qs("#echoes-loading");
    if (loadingEl) {
      markFeedbackEl(loadingEl, "info");
      loadingEl.hidden = !visible;
    }
  }

  function setEchoesEmptyVisible(visible, text = "暂无公开内容") {
    const emptyEl = qs("#echoes-empty");
    if (emptyEl) {
      setFeedback(emptyEl, text, "warning");
      emptyEl.hidden = !visible;
    }
  }

  function setEchoesEndVisible(visible) {
    const endEl = qs("#echoes-end");
    if (endEl) {
      markFeedbackEl(endEl, "success");
      endEl.hidden = !visible;
    }
  }

  function normalizeEchoEntries(data) {
    if (Array.isArray(data.entries)) {
      return data.entries
        .map((entry) => {
          const entryType = String(entry.entry_type || entry.type || "").toLowerCase();
          if (entryType === "record" && entry.record) {
            return {
              entry_type: "record",
              record: entry.record,
              created_at: entry.created_at || entry.record.created_at || "",
              file_type: normalizeEchoFileType(entry.file_type || echoFileTypeFromRecord(entry.record)) || "file",
            };
          }
          if (entryType === "asset" && entry.asset) {
            return {
              entry_type: "asset",
              asset: entry.asset,
              created_at: entry.created_at || entry.asset.created_at || "",
              file_type: normalizeEchoFileType(entry.file_type || echoFileTypeFromAsset(entry.asset)) || "file",
            };
          }
          return null;
        })
        .filter(Boolean);
    }

    const parseTimestamp = (dateValue) => {
      const timestamp = Number(new Date(dateValue || 0).getTime());
      return Number.isFinite(timestamp) ? timestamp : 0;
    };

    const items = Array.isArray(data.items) ? data.items : [];
    const assets = Array.isArray(data.assets) ? data.assets : [];
    return [
      ...items.map((item) => ({
        entry_type: "record",
        created_at: item.created_at,
        file_type: echoFileTypeFromRecord(item),
        record: item,
        _timestamp: parseTimestamp(item.created_at),
      })),
      ...assets.map((item) => ({
        entry_type: "asset",
        created_at: item.created_at,
        file_type: echoFileTypeFromAsset(item),
        asset: item,
        _timestamp: parseTimestamp(item.created_at),
      })),
    ]
      .sort((a, b) => b._timestamp - a._timestamp)
      .map((item) => {
        if (item.entry_type === "record") {
          return {
            entry_type: "record",
            created_at: item.created_at,
            file_type: item.file_type,
            record: item.record,
          };
        }
        return {
          entry_type: "asset",
          created_at: item.created_at,
          file_type: item.file_type,
          asset: item.asset,
        };
      });
  }

  function echoCardHtml(record, entry = {}) {
    const content = record.content || {};
    const mediaType = String(content.media_type || "").toLowerCase();
    const src = content.blob_url || content.signed_url || "";
    const fileType = normalizeEchoFileType(entry.file_type || echoFileTypeFromRecord(record)) || "file";

    let mediaHtml = "";
    if (content.kind === "text") {
      const textPreview = String(record.preview || content.text || "").trim();
      mediaHtml = textPreview
        ? `<pre class="echo-text">${escapeHtml(textPreview)}</pre>`
        : `<p class="muted">文本内容不可用</p>`;
    } else if (src && mediaType === "image") {
      mediaHtml = `<img loading="lazy" decoding="async" src="${escapeHtml(src)}" alt="${escapeHtml(content.filename || "image")}">`;
    } else if (src && mediaType === "video") {
      mediaHtml = `<video controls preload="none" src="${escapeHtml(src)}"></video>`;
    } else if (src && mediaType === "audio") {
      mediaHtml = `<audio controls preload="none" src="${escapeHtml(src)}"></audio>`;
    } else if (src) {
      mediaHtml = `<p><a href="${escapeHtml(src)}" target="_blank" rel="noreferrer">${escapeHtml(content.filename || "查看文件")}</a></p>`;
    }

    if (!mediaHtml) {
      mediaHtml = `<p class="muted">内容不可用</p>`;
    }

    const meta = [];
    if (record.user?.username) {
      meta.push(`发布者: ${escapeHtml(record.user.username)}`);
    }
    if (Array.isArray(record.tags) && record.tags.length) {
      meta.push(record.tags.slice(0, 3).map((tag) => `#${escapeHtml(tag)}`).join(" "));
    }

    return `
      <article class="echo-card" data-echo-source="record" data-echo-type="${fileType}">
        <header class="echo-card-head">
          <div class="echo-badge-line">
            ${echoSourceBadgeHtml("record")}
            ${echoFileBadgeHtml(fileType)}
          </div>
          <span class="muted">${escapeHtml(formatTime(record.created_at))}</span>
        </header>
        <div class="echo-card-media">${mediaHtml}</div>
        <p class="muted echo-card-meta">${meta.join(" | ")}</p>
        <div class="action-line">
          <button class="bubble" type="button" data-action="view-record" data-record-id="${record.id}">查看完整记录</button>
        </div>
      </article>
    `;
  }

  function echoAssetCardHtml(asset, entry = {}) {
    const src = asset.blob_url || "";
    const contentType = String(asset.content_type || "").toLowerCase();
    const fileType = normalizeEchoFileType(entry.file_type || echoFileTypeFromAsset(asset)) || "file";

    let mediaHtml = "";
    if (fileType === "web") {
      mediaHtml = src
        ? `<p><a href="${escapeHtml(src)}" target="_blank" rel="noreferrer">打开博客网页</a></p>`
        : `<p class="muted">博客内容不可用</p>`;
    } else if (src && contentType.startsWith("image/")) {
      mediaHtml = `<img loading="lazy" decoding="async" src="${escapeHtml(src)}" alt="${escapeHtml(asset.title || "poster")}">`;
    } else if (src && contentType.startsWith("audio/")) {
      mediaHtml = `<audio controls preload="none" src="${escapeHtml(src)}"></audio>`;
    } else if (src && contentType.startsWith("video/")) {
      mediaHtml = `<video controls preload="none" src="${escapeHtml(src)}"></video>`;
    } else if (src) {
      mediaHtml = `<p><a href="${escapeHtml(src)}" target="_blank" rel="noreferrer">打开 AI 资产</a></p>`;
    } else {
      mediaHtml = `<p class="muted">资产不可用</p>`;
    }

    const meta = [];
    if (asset.user?.username) {
      meta.push(`发布者: ${escapeHtml(asset.user.username)}`);
    }
    if (asset.source_day) {
      meta.push(`归档日期: ${escapeHtml(asset.source_day)}`);
    }
    if (asset.is_daily_digest) {
      meta.push("日报归档");
    }

    return `
      <article class="echo-card" data-echo-source="asset" data-echo-type="${fileType}">
        <header class="echo-card-head">
          <div class="echo-badge-line">
            ${echoSourceBadgeHtml("asset")}
            ${echoFileBadgeHtml(fileType)}
          </div>
          <span class="muted">${escapeHtml(formatTime(asset.created_at))}</span>
        </header>
        <p><strong>${escapeHtml(asset.title || asset.kind || "AI 资产")}</strong></p>
        <div class="echo-card-media">${mediaHtml}</div>
        <p class="muted echo-card-meta">${meta.join(" | ")}</p>
      </article>
    `;
  }

  function renderEchoEntries(entries, opts = {}) {
    const grid = qs("#echoes-grid");
    if (!grid) {
      return;
    }
    if (opts.reset) {
      grid.innerHTML = "";
    }
    if (!entries.length) {
      return;
    }

    const html = entries
      .map((entry) => (entry.entry_type === "record" ? echoCardHtml(entry.record, entry) : echoAssetCardHtml(entry.asset, entry)))
      .join("");
    grid.insertAdjacentHTML("beforeend", html);
    echoesState.visibleCount += entries.length;
    setEchoesCount();
  }

  async function loadEchoes(opts = {}) {
    const reset = Boolean(opts.reset);
    const grid = qs("#echoes-grid");
    if (!grid || echoesState.loading) {
      return;
    }

    if (reset) {
      echoesState.cursor = null;
      echoesState.hasMore = true;
      echoesState.visibleCount = 0;
      renderEchoEntries([], { reset: true });
      setEchoesCount();
      setEchoesEmptyVisible(false);
      setEchoesEndVisible(false);
    }

    if (!echoesState.hasMore) {
      setEchoesEndVisible(echoesState.visibleCount > 0);
      return;
    }

    echoesState.loading = true;
    setEchoesLoadingVisible(true);
    setEchoesStatus("加载中...", "info");

    try {
      const query = buildQuery({
        limit: echoesState.pageSize,
        file_type: echoesState.fileType,
        cursor_time: echoesState.cursor?.created_at || "",
        cursor_kind: echoesState.cursor?.entry_type || "",
        cursor_id: echoesState.cursor?.id || "",
      });
      const path = query ? `/api/echoes?${query}` : "/api/echoes";
      const data = await api(path);
      const entries = normalizeEchoEntries(data);
      renderEchoEntries(entries);

      if (data.next_cursor && typeof data.next_cursor === "object") {
        const cursor = {
          created_at: String(data.next_cursor.created_at || ""),
          entry_type: String(data.next_cursor.entry_type || data.next_cursor.type || ""),
          id: Number(data.next_cursor.id || 0),
        };
        echoesState.cursor = cursor.created_at && cursor.entry_type && cursor.id > 0 ? cursor : null;
      } else {
        echoesState.cursor = null;
      }

      if (typeof data.has_more === "boolean") {
        echoesState.hasMore = data.has_more;
      } else {
        echoesState.hasMore = entries.length >= echoesState.pageSize;
      }

      if (!echoesState.visibleCount) {
        setEchoesEmptyVisible(true, echoesState.fileType ? "该类型暂无公开内容" : "暂无公开内容");
        setEchoesStatus("暂无数据", "warning");
      } else if (echoesState.hasMore) {
        setEchoesEmptyVisible(false);
        setEchoesStatus("向下滚动自动加载更多", "info");
      } else {
        setEchoesEmptyVisible(false);
        setEchoesStatus("已加载全部内容", "success");
      }
      setEchoesEndVisible(!echoesState.hasMore && echoesState.visibleCount > 0);
    } catch (error) {
      echoesState.hasMore = false;
      setEchoesStatus(`加载失败: ${error.message || "未知错误"}`, "error");
      setEchoesEmptyVisible(true, "加载失败，请稍后重试");
    } finally {
      echoesState.loading = false;
      setEchoesLoadingVisible(false);
    }
  }

  function initEchoesObserver() {
    const sentinel = qs("#echoes-sentinel");
    if (!sentinel || !("IntersectionObserver" in window)) {
      return;
    }
    if (echoesState.observer) {
      echoesState.observer.disconnect();
    }
    echoesState.observer = new IntersectionObserver(
      (entries) => {
        const hit = entries.some((entry) => entry.isIntersecting);
        if (!hit || echoesState.loading || !echoesState.hasMore) {
          return;
        }
        loadEchoes().catch((error) => {
          window.console.error(error);
        });
      },
      { rootMargin: "700px 0px" },
    );
    echoesState.observer.observe(sentinel);
  }

  function bindEchoesFilters() {
    const chipsWrap = qs("#echoes-type-chips");
    const refreshBtn = qs("#echoes-refresh-btn");
    const densityWrap = qs(".echoes-density-switch");

    if (chipsWrap) {
      chipsWrap.addEventListener("click", (event) => {
        if (!(event.target instanceof Element)) {
          return;
        }
        const chip = event.target.closest("button[data-file-type]");
        if (!chip) {
          return;
        }
        const nextType = normalizeEchoFileType(chip.dataset.fileType || "");
        if (nextType === echoesState.fileType && echoesState.visibleCount > 0) {
          return;
        }
        echoesState.fileType = nextType;
        setActiveEchoesType(echoesState.fileType);
        setEchoesCount();
        loadEchoes({ reset: true }).catch((error) => window.console.error(error));
      });
    }

    if (densityWrap) {
      densityWrap.addEventListener("click", (event) => {
        if (!(event.target instanceof Element)) {
          return;
        }
        const button = event.target.closest("button[data-echoes-density]");
        if (!button) {
          return;
        }
        const nextMode = normalizeEchoesDensity(button.dataset.echoesDensity || "");
        if (nextMode === echoesState.density) {
          return;
        }
        applyEchoesDensity(nextMode);
      });
    }

    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        loadEchoes({ reset: true }).catch((error) => window.console.error(error));
      });
    }
  }

  async function initEchoes() {
    applyEchoesDensity(readStoredEchoesDensity(), { persist: false });
    setActiveEchoesType(echoesState.fileType);
    setEchoesCount();
    bindEchoesFilters();
    initEchoesObserver();
    await loadEchoes({ reset: true });
  }

  function readNoticeFilters() {
    const form = qs("#notice-filter-form");
    const formData = new FormData(form);
    return {
      day: formData.get("day") || "",
      user_id: formData.get("user_id") || "",
      tag: formData.get("tag") || "",
      order: formData.get("order") || "asc",
    };
  }

  function setNoticeMeta(text, tone = "neutral") {
    const metaEl = qs("#notice-render-meta");
    if (metaEl) {
      setFeedback(metaEl, text, tone);
      metaEl.hidden = false;
    }
  }

  function initNoticeReaderToolbar() {
    const toolbar = qs("#notice-reader-toolbar");
    if (!toolbar) {
      return;
    }

    applyNoticeReaderPrefs(readStoredNoticeReaderPrefs(), { persist: false });

    toolbar.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) {
        return;
      }

      const fontBtn = event.target.closest("button[data-notice-font]");
      if (fontBtn) {
        const nextFont = String(fontBtn.getAttribute("data-notice-font") || "");
        applyNoticeReaderPrefs(
          {
            ...noticeReaderPrefs,
            font: nextFont,
          },
          { persist: true },
        );
        return;
      }

      const widthBtn = event.target.closest("button[data-notice-width]");
      if (widthBtn) {
        const nextWidth = String(widthBtn.getAttribute("data-notice-width") || "");
        applyNoticeReaderPrefs(
          {
            ...noticeReaderPrefs,
            width: nextWidth,
          },
          { persist: true },
        );
        return;
      }

      const mediaBtn = event.target.closest("button[data-notice-media]");
      if (mediaBtn) {
        const nextMedia = String(mediaBtn.getAttribute("data-notice-media") || "");
        applyNoticeReaderPrefs(
          {
            ...noticeReaderPrefs,
            media: nextMedia,
          },
          { persist: true },
        );
        return;
      }

      if (event.target.closest("#notice-reader-top")) {
        const panel = qs("#notice-render-panel");
        if (panel) {
          panel.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      }
    });
  }

  function setNoticeResultsVisible(visible) {
    const metaEl = qs("#notice-render-meta");
    const panelEl = qs("#notice-render-panel");
    if (metaEl) {
      metaEl.hidden = !visible;
    }
    if (panelEl) {
      panelEl.hidden = !visible;
    }
  }

  function clearNoticeRender() {
    const htmlEl = qs("#notice-render-html");
    setNoticeMeta("", "neutral");
    if (htmlEl) {
      htmlEl.innerHTML = "";
    }
    setNoticeResultsVisible(false);
  }

  async function loadNoticeRender(filters = readNoticeFilters()) {
    const query = buildQuery(filters);
    const path = query ? `/api/notice/render?${query}` : "/api/notice/render";
    const form = qs("#notice-filter-form");
    const submitBtn = form ? qs('button[type="submit"]', form) : null;
    const htmlEl = qs("#notice-render-html");
    setButtonBusy(submitBtn, true, { busyText: "渲染中..." });
    setNoticeMeta("正在渲染...", "info");
    try {
      const data = await api(path);
      if (htmlEl) {
        htmlEl.innerHTML = data.rendered_html || "";
      }
      setNoticeMeta(`匹配记录: ${data.count || 0}`, "success");
      applyNoticeReaderPrefs(noticeReaderPrefs, { persist: false });
      setNoticeResultsVisible(true);
    } catch (error) {
      const message = String(error?.message || "渲染失败");
      if (htmlEl) {
        htmlEl.innerHTML = `<p class="feedback-inline" data-tone="error">${escapeHtml(message)}</p>`;
      }
      setNoticeMeta(`渲染失败: ${message}`, "error");
      setNoticeResultsVisible(true);
    } finally {
      setButtonBusy(submitBtn, false);
    }
  }

  async function initNotice() {
    initNoticeReaderToolbar();
    const users = await getUsers();
    populateSelect(
      qs("#notice-user"),
      users.map((item) => ({ id: item.id, username: item.username })),
      { allowAll: true, allText: "全部用户" },
    );

    const form = qs("#notice-filter-form");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        loadNoticeRender(readNoticeFilters()).catch((error) => window.console.error(error));
      });
    }
    await loadNoticeRender();
  }

  function initDialogControls() {
    document.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest("[data-close-dialog]") && dialog?.open) {
        dialog.close();
      }
    });

    if (dialogBody) {
      dialogBody.addEventListener("change", (event) => {
        if (!(event.target instanceof Element)) {
          return;
        }
        const fileInput = event.target.closest("[data-record-edit-file-input]");
        if (!(fileInput instanceof HTMLInputElement)) {
          return;
        }
        const hintEl = qs("[data-record-edit-file-hint]", dialogBody);
        if (!hintEl) {
          return;
        }
        const nextFile = fileInput.files && fileInput.files.length ? fileInput.files[0] : null;
        hintEl.textContent = nextFile
          ? `将替换为: ${nextFile.name} (${formatFileSize(nextFile.size)})`
          : "未选择新文件，将保留当前文件。";
      });

      dialogBody.addEventListener("submit", async (event) => {
        const form = event.target instanceof Element ? event.target.closest("form[data-record-edit-form]") : null;
        if (!(form instanceof HTMLFormElement)) {
          return;
        }
        event.preventDefault();

        const recordId = Number(form.dataset.recordId || 0);
        if (!recordId || dialogState.saving) {
          return;
        }

        const formData = new FormData(form);
        const payload = new FormData();
        const submitBtn = qs("[data-record-edit-submit]", form);
        const feedbackEl = qs("[data-record-edit-feedback]", form);

        dialogState.saving = true;
        if (submitBtn instanceof HTMLButtonElement) {
          submitBtn.disabled = true;
        }
        if (feedbackEl) {
          feedbackEl.textContent = "保存中...";
        }

        try {
          const currentRecord =
            dialogState.record && Number(dialogState.record.id) === recordId
              ? dialogState.record
              : await fetchRecord(recordId, false);

          const visibility = normalizeVisibility(formData.get("visibility"), currentRecord?.visibility || "private");
          const tags = String(formData.get("tags") || "");
          const format = String(formData.get("format") || "").trim();

          payload.set("visibility", visibility);
          payload.set("tags", tags);
          payload.set("format", format);

          if (currentRecord?.content?.kind === "text") {
            const text = String(formData.get("text") || "").trim();
            if (!text) {
              throw new Error("文本内容不能为空");
            }
            payload.set("text", text);
          }

          const fileInput = qs("[data-record-edit-file-input]", form);
          const nextFile =
            fileInput instanceof HTMLInputElement && fileInput.files && fileInput.files.length ? fileInput.files[0] : null;
          if (nextFile) {
            payload.set("file", nextFile, nextFile.name);
          }

          await api(`/api/records/${recordId}`, {
            method: "PATCH",
            body: payload,
          });
          if (feedbackEl) {
            feedbackEl.textContent = "保存成功，正在刷新...";
          }
          await refreshCurrentPageData();
          await openRecordDialog(recordId);
          window.alert("记录已更新");
        } catch (error) {
          const message = error instanceof Error ? error.message : "更新失败";
          if (feedbackEl) {
            feedbackEl.textContent = message;
          }
          window.alert(message);
        } finally {
          dialogState.saving = false;
          if (submitBtn instanceof HTMLButtonElement) {
            submitBtn.disabled = false;
          }
        }
      });
    }

    if (dialog) {
      dialog.addEventListener("close", () => {
        dialogState.saving = false;
      });
    }
  }

  async function refreshCurrentPageData() {
    if (page === "home") {
      await loadHome();
      return;
    }
    if (page === "board") {
      await loadBoard();
      return;
    }
    if (page === "echoes") {
      await loadEchoes({ reset: true });
      return;
    }
    if (page === "notice") {
      await loadNoticeRender();
    }
  }

  async function boot() {
    initHeaderNavToggle();
    initRevealAnimations();
    initStatusDecorators();
    bindGlobalRecordActions();
    initDialogControls();

    if (page === "home") {
      await initHome();
      return;
    }
    if (page === "board") {
      await initBoard();
      return;
    }
    if (page === "echoes") {
      await initEchoes();
      return;
    }
    if (page === "notice") {
      await initNotice();
    }
  }

  boot().catch((error) => {
    window.console.error(error);
    window.alert(error.message || "页面初始化失败");
  });
})();
