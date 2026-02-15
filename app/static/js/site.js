(function () {
  const page = document.body.dataset.page || "";
  const dialog = document.getElementById("record-dialog");
  const dialogBody = document.getElementById("record-dialog-body");
  const dialogTitle = document.getElementById("record-dialog-title");

  let usersCache = null;

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function qs(selector, root = document) {
    return root.querySelector(selector);
  }

  function formatTime(value) {
    if (!value) {
      return "";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  }

  function buildQuery(params) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === "") {
        return;
      }
      query.set(key, String(value));
    });
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

    let html = "";
    if (allowAll) {
      html += `<option value="">${escapeHtml(allText)}</option>`;
    }
    html += items
      .map((item) => `<option value="${item.id}">${escapeHtml(item.username || item.name || item.id)}</option>`)
      .join("");

    selectEl.innerHTML = html;
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
      const data = await api(`/api/records/${recordId}?include_comments=1`);
      const record = data.record;

      dialogTitle.textContent = `记录 #${record.record_no}`;
      const primaryAction = record.can_edit
        ? `<button class="bubble" type="button" data-dialog-action="edit-record" data-record-id="${record.id}">编辑这条记录</button>`
        : (record.can_comment
            ? `<button class="bubble" type="button" data-dialog-action="comment-record" data-record-id="${record.id}">评论这条记录</button>`
            : "");
      dialogBody.innerHTML = `
        <div class="dialog-content">
          <p class="muted">用户: ${escapeHtml(record.user?.username || "")} | ${escapeHtml(formatTime(record.created_at))} | ${escapeHtml(record.visibility)}</p>
          <p>${escapeHtml(record.preview || "")}</p>
          <div class="tag-line">${tagHtml(record.tags)}</div>
          <div class="action-line">${primaryAction}</div>
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

      if (dialog && !dialog.open) {
        dialog.showModal();
      }
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function editRecord(recordId) {
    try {
      const data = await api(`/api/records/${recordId}`);
      const record = data.record;
      const payload = {};

      const tagsCurrent = Array.isArray(record.tags) ? record.tags.join(",") : "";
      const tagsNext = window.prompt("标签（逗号分隔）", tagsCurrent);
      if (tagsNext === null) {
        return;
      }
      payload.tags = tagsNext;

      const visibilityNext = window.prompt("可见性（public/private）", record.visibility || "private");
      if (visibilityNext === null) {
        return;
      }
      payload.visibility = visibilityNext;

      if (record.content?.kind === "text") {
        const textNext = window.prompt("编辑文本内容", record.content.text || "");
        if (textNext === null) {
          return;
        }
        payload.text = textNext;
      }

      await api(`/api/records/${recordId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      window.alert("记录已更新");
      await refreshCurrentPageData();
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
      const button = event.target.closest("button[data-action], button[data-dialog-action]");
      if (!button) {
        return;
      }
      const action = button.dataset.action || button.dataset.dialogAction;
      const recordId = Number(button.dataset.recordId || 0);
      if (!recordId) {
        return;
      }

      if (action === "view-record") {
        openRecordDialog(recordId);
      } else if (action === "edit-record") {
        editRecord(recordId);
      } else if (action === "comment-record") {
        commentRecord(recordId);
      }
    });
  }

  async function loadHome() {
    const dateEl = qs("#today-date");
    const aiStatusEl = qs("#ai-status");
    const metricPublicEl = qs("#metric-public-count");
    const metricUserEl = qs("#metric-user-count");
    const metricTagsEl = qs("#metric-top-tags");

    const data = await api("/api/home/today");
    const records = data.public_records || [];

    if (dateEl) {
      dateEl.textContent = `日期: ${data.date || ""}`;
    }
    if (aiStatusEl) {
      aiStatusEl.textContent = data.ai?.message || "未启用";
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
  }

  async function initHome() {
    const form = qs("#quick-publish-form");
    const msgEl = qs("#quick-publish-msg");

    if (form) {
      const formatEl = qs("#quick-publish-format", form);
      const textWrap = qs("#quick-publish-text-wrap", form);
      const fileWrap = qs("#quick-publish-file-wrap", form);
      const textInput = qs('textarea[name="text"]', form);
      const fileInput = qs("#quick-publish-file-input", form);
      const fileLabel = qs("#quick-publish-file-label", form);
      const fileHint = qs("#quick-publish-file-hint", form);

      const formatConfig = {
        text: {
          useFile: false,
          fileLabel: "",
          fileHint: "",
          accept: "",
        },
        image: {
          useFile: true,
          fileLabel: "上传图片",
          fileHint: "支持 png/jpg/webp/gif 等图片类型",
          accept: "image/*",
        },
        audio: {
          useFile: true,
          fileLabel: "上传音频",
          fileHint: "支持 mp3/wav/m4a/aac/ogg/flac 等音频类型",
          accept: "audio/*",
        },
        video: {
          useFile: true,
          fileLabel: "上传视频",
          fileHint: "支持 mp4/mov/webm/mkv 等视频类型",
          accept: "video/*",
        },
        document: {
          useFile: true,
          fileLabel: "上传文档",
          fileHint: "支持 pdf/doc/docx/ppt/xls/txt/md/csv/json 等文档",
          accept: ".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.csv,.json",
        },
        file: {
          useFile: true,
          fileLabel: "上传文件",
          fileHint: "可上传任意文件类型",
          accept: "",
        },
      };

      const applyQuickPublishFormat = () => {
        const formatValue = String(formatEl?.value || "text").trim().toLowerCase();
        const config = formatConfig[formatValue] || formatConfig.text;
        const useFile = Boolean(config.useFile);

        if (textWrap) {
          textWrap.hidden = useFile;
        }
        if (fileWrap) {
          fileWrap.hidden = !useFile;
        }
        if (textInput) {
          textInput.disabled = useFile;
        }
        if (fileInput) {
          fileInput.disabled = !useFile;
          fileInput.accept = config.accept || "";
        }
        if (fileLabel) {
          fileLabel.textContent = config.fileLabel || "上传文件";
        }
        if (fileHint) {
          fileHint.textContent = config.fileHint || "";
        }
      };

      if (formatEl) {
        formatEl.addEventListener("change", applyQuickPublishFormat);
      }
      applyQuickPublishFormat();

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const formatValue = String(formData.get("format") || "text").trim().toLowerCase();
        const config = formatConfig[formatValue] || formatConfig.text;
        const textValue = String(formData.get("text") || "").trim();
        const fileValue = formData.get("file");
        const hasFile = fileValue instanceof File && Boolean(fileValue.name);

        if (config.useFile && !hasFile) {
          window.alert("当前发布格式需要选择文件");
          return;
        }
        if (!config.useFile && !textValue) {
          window.alert("文本格式需要填写内容");
          return;
        }

        try {
          await api("/api/push", {
            method: "POST",
            body: formData,
          });
          if (msgEl) {
            msgEl.textContent = "发布成功";
          }
          form.reset();
          applyQuickPublishFormat();
          await loadHome();
        } catch (error) {
          if (msgEl) {
            msgEl.textContent = error.message;
          }
        }
      });
    }

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
    let maxCount = 0;
    users.forEach((user) => {
      dates.forEach((day) => {
        const count = matrix[String(user.id)]?.[day] || 0;
        if (count > maxCount) {
          maxCount = count;
        }
      });
    });

    let html = "<table class=\"board-table\"><thead><tr><th>用户 \\ 日期</th>";
    html += dates
      .map((day) => `<th class="clickable" data-board-action="day" data-day="${escapeHtml(day)}" data-tag="${escapeHtml(tagValue || "")}">${escapeHtml(day)}</th>`)
      .join("");
    html += "</tr></thead><tbody>";

    users.forEach((user) => {
      html += `<tr><th class="clickable" data-board-action="user" data-user-id="${user.id}" data-tag="${escapeHtml(tagValue || "")}">${escapeHtml(user.username)}</th>`;
      dates.forEach((day) => {
        const count = matrix[String(user.id)]?.[day] || 0;
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
        html += `<td class="clickable heat-${level}" data-board-action="cell" data-user-id="${user.id}" data-day="${escapeHtml(day)}" data-tag="${escapeHtml(tagValue || "")}">${count}</td>`;
      });
      html += "</tr>";
    });

    html += "</tbody></table>";
    wrap.innerHTML = html;
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

    const data = await api(path);
    renderBoardRecordList(listEl, data.items || [], { emptyText: "无匹配记录" });
  }

  async function loadBoard() {
    const form = qs("#board-filter-form");
    const formData = new FormData(form);
    const tag = String(formData.get("tag") || "").trim();
    const query = buildQuery({
      days: formData.get("days") || 7,
      tag,
    });
    const data = await api(`/api/board?${query}`);
    renderBoardTable(data, tag);
  }

  async function initBoard() {
    const form = qs("#board-filter-form");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        loadBoard().catch((error) => window.alert(error.message));
      });
    }

    const wrap = qs("#board-table-wrap");
    if (wrap) {
      wrap.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-board-action]");
        if (!trigger) {
          return;
        }

        const action = trigger.dataset.boardAction;
        const tag = trigger.dataset.tag || "";
        const tagQuery = tag ? `&tag=${encodeURIComponent(tag)}` : "";

        if (action === "user") {
          const userId = trigger.dataset.userId;
          loadBoardSide(`用户 #${userId} 的可见记录`, `/api/board/user/${userId}/records?${buildQuery({ tag })}`).catch((error) => window.alert(error.message));
          return;
        }

        if (action === "day") {
          const day = trigger.dataset.day;
          loadBoardSide(`${day} 的公开记录`, `/api/board/date/${day}?${buildQuery({ tag })}`).catch((error) => window.alert(error.message));
          return;
        }

        if (action === "cell") {
          const userId = trigger.dataset.userId;
          const day = trigger.dataset.day;
          loadBoardSide(`用户 #${userId} 在 ${day} 的记录`, `/api/board/cell?user_id=${encodeURIComponent(userId)}&day=${encodeURIComponent(day)}${tagQuery}`).catch((error) => window.alert(error.message));
        }
      });
    }

    await loadBoard();
  }

  function echoCardHtml(record) {
    const content = record.content || {};
    const mediaType = content.media_type || "";
    const src = content.blob_url || content.signed_url || "";

    let mediaHtml = "";
    if (content.kind === "text") {
      mediaHtml = `<pre>${escapeHtml(content.text || "")}</pre>`;
    } else if (src && mediaType === "image") {
      mediaHtml = `<img src="${escapeHtml(src)}" alt="${escapeHtml(content.filename || "image")}">`;
    } else if (src && mediaType === "video") {
      mediaHtml = `<video controls src="${escapeHtml(src)}"></video>`;
    } else if (src && mediaType === "audio") {
      mediaHtml = `<audio controls src="${escapeHtml(src)}"></audio>`;
    } else if (src) {
      mediaHtml = `<p><a href="${escapeHtml(src)}" target="_blank" rel="noreferrer">${escapeHtml(content.filename || "查看文件")}</a></p>`;
    }
    if (!mediaHtml) {
      mediaHtml = `<p class="muted">内容不可用</p>`;
    }

    return `
      <article class="echo-card">
        ${mediaHtml}
        <div class="action-line">
          <button class="bubble" type="button" data-action="view-record" data-record-id="${record.id}">查看完整记录</button>
        </div>
      </article>
    `;
  }

  async function loadEchoes() {
    const query = buildQuery({
      page: 1,
      per: 80,
    });

    const data = await api(`/api/echoes?${query}`);
    const items = data.items || [];
    const grid = qs("#echoes-grid");
    if (!grid) {
      return;
    }
    if (!items.length) {
      grid.innerHTML = `<p class="muted">暂无公开内容</p>`;
      return;
    }
    grid.innerHTML = items.map((item) => echoCardHtml(item)).join("");
  }

  async function initEchoes() {
    await loadEchoes();
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

  function hasNoticeRealFilter(filters) {
    return Boolean((filters?.day || "").trim() || (filters?.user_id || "").trim() || (filters?.tag || "").trim());
  }

  function setNoticeResultsVisible(visible) {
    const metaEl = qs("#notice-render-meta");
    const actionsEl = qs("#notice-actions");
    const panelEl = qs("#notice-render-panel");
    if (metaEl) {
      metaEl.hidden = !visible;
    }
    if (actionsEl) {
      actionsEl.hidden = !visible;
    }
    if (panelEl) {
      panelEl.hidden = !visible;
    }
  }

  function setNoticeAiOutputVisible(visible) {
    const outputEl = qs("#notice-ai-output");
    if (outputEl) {
      outputEl.hidden = !visible;
    }
  }

  function clearNoticeRender() {
    const metaEl = qs("#notice-render-meta");
    const htmlEl = qs("#notice-render-html");
    const outputEl = qs("#notice-ai-output");
    if (metaEl) {
      metaEl.textContent = "";
    }
    if (htmlEl) {
      htmlEl.innerHTML = "";
    }
    if (outputEl) {
      outputEl.textContent = "";
    }
    setNoticeResultsVisible(false);
    setNoticeAiOutputVisible(false);
  }

  async function loadNoticeRender(filters = readNoticeFilters()) {
    if (!hasNoticeRealFilter(filters)) {
      clearNoticeRender();
      return;
    }

    const query = buildQuery(filters);
    const data = await api(`/api/notice/render?${query}`);

    const metaEl = qs("#notice-render-meta");
    const htmlEl = qs("#notice-render-html");
    const outputEl = qs("#notice-ai-output");
    if (metaEl) {
      metaEl.textContent = `匹配记录: ${data.count || 0}`;
    }
    if (htmlEl) {
      htmlEl.innerHTML = data.rendered_html || "";
    }
    if (outputEl) {
      outputEl.textContent = "";
    }
    setNoticeResultsVisible(true);
    setNoticeAiOutputVisible(false);
  }

  async function runNoticeAi(action) {
    const filters = readNoticeFilters();
    const outputEl = qs("#notice-ai-output");
    const htmlEl = qs("#notice-render-html");
    if (!hasNoticeRealFilter(filters)) {
      clearNoticeRender();
      window.alert("请先至少填写一个筛选条件（日期/用户/标签）。");
      return;
    }
    setNoticeResultsVisible(true);
    setNoticeAiOutputVisible(true);
    if (outputEl) {
      outputEl.textContent = "AI 处理中...";
    }
    try {
      const endpoint = action === "optimize" ? "/api/notice/ai" : "/api/notice/assets";
      const data = await api(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          filters,
        }),
      });

      if (action === "optimize") {
        if (htmlEl) {
          htmlEl.innerHTML = data.output || "";
        }
        if (outputEl) {
          const title = `provider=${data.provider || "-"} model=${data.model || "-"} records=${data.record_count || 0}`;
          outputEl.textContent = `${title}\n\n${data.output || ""}`;
        }
        return;
      }

      const asset = data.asset || {};
      const title = `${action === "podcast" ? "播客音频" : "海报图片"} 已生成\nprovider=${asset.provider || "-"} model=${asset.model || "-"} size=${asset.size_bytes || 0} bytes`;
      if (outputEl) {
        outputEl.textContent = title;
      }
      if (htmlEl && asset.blob_url) {
        if (action === "podcast") {
          htmlEl.innerHTML = `
            <article class="notice-render">
              <h3>播客音频</h3>
              <audio controls src="${escapeHtml(asset.blob_url)}"></audio>
              <p><a href="${escapeHtml(asset.blob_url)}" target="_blank" rel="noreferrer">打开音频文件</a></p>
            </article>
          `;
        } else {
          htmlEl.innerHTML = `
            <article class="notice-render">
              <h3>海报图片</h3>
              <img src="${escapeHtml(asset.blob_url)}" alt="poster">
              <p><a href="${escapeHtml(asset.blob_url)}" target="_blank" rel="noreferrer">打开图片文件</a></p>
            </article>
          `;
        }
      }
    } catch (error) {
      if (outputEl) {
        outputEl.textContent = error.message;
      } else {
        window.alert(error.message);
      }
    }
  }

  async function initNotice() {
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
        const filters = readNoticeFilters();
        if (!hasNoticeRealFilter(filters)) {
          clearNoticeRender();
          return;
        }
        loadNoticeRender(filters).catch((error) => window.alert(error.message));
      });
    }

    document.querySelectorAll("[data-ai-action]").forEach((button) => {
      button.addEventListener("click", () => {
        runNoticeAi(button.dataset.aiAction || "optimize");
      });
    });
    clearNoticeRender();
  }

  function initDialogControls() {
    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-close-dialog]") && dialog?.open) {
        dialog.close();
      }
    });
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
      await loadEchoes();
      return;
    }
    if (page === "notice") {
      await loadNoticeRender();
    }
  }

  async function boot() {
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
