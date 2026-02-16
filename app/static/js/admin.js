(function () {
  const page = document.body.dataset.page || "";
  if (page !== "admin") {
    return;
  }

  const form = document.getElementById("admin-settings-form");
  const statusEl = document.getElementById("admin-settings-status");
  const summaryEl = document.getElementById("admin-settings-summary");
  const refreshBtn = document.getElementById("admin-refresh-btn");
  const saveBtn = document.getElementById("admin-save-btn");
  const filterInput = document.getElementById("admin-filter-input");
  const onlyOverridesInput = document.getElementById("admin-only-overrides");

  let pendingReset = new Set();
  let latestGroups = [];
  const fieldByKey = new Map();
  const draftValues = new Map();

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function sourceLabel(source) {
    if (source === "override") {
      return "前端覆盖";
    }
    if (source === "config") {
      return "环境变量";
    }
    return "系统默认";
  }

  function sourceClass(source) {
    if (source === "override") {
      return "source-override";
    }
    if (source === "config") {
      return "source-config";
    }
    return "source-default";
  }

  function normalizeText(value) {
    return String(value || "").trim().toLowerCase();
  }

  function hasActiveFilter() {
    const query = normalizeText(filterInput && filterInput.value);
    const onlyOverrides = Boolean(onlyOverridesInput && onlyOverridesInput.checked);
    return Boolean(query || onlyOverrides);
  }

  function valuePreview(item, value) {
    if (item.secret) {
      return "******";
    }
    if (item.type === "bool") {
      return value ? "true" : "false";
    }
    const text = String(value ?? "").trim();
    if (!text) {
      return "(空)";
    }
    return text.length > 72 ? `${text.slice(0, 72)}...` : text;
  }

  function currentItemValue(item) {
    const key = item.key;
    if (key && draftValues.has(key)) {
      return draftValues.get(key);
    }
    return item.value;
  }

  function fieldWarningHtml(item) {
    const key = String(item?.key || "");
    if (key !== "ARCHIVE_RETENTION_DAYS") {
      return "";
    }
    const raw = String(currentItemValue(item) ?? "").trim();
    if (raw !== "0") {
      return "";
    }
    return `
      <p class="admin-field-warning">
        当前为“永久保留”。归档与文件本体会持续增长，可能导致磁盘快速膨胀，请确认有容量监控和清理策略。
      </p>
    `;
  }

  function itemMatches(item, groupName, query, onlyOverrides) {
    if (onlyOverrides && item.source !== "override" && !pendingReset.has(item.key)) {
      return false;
    }
    if (!query) {
      return true;
    }
    const stack = [
      item.key,
      item.label,
      item.description,
      groupName,
      sourceLabel(item.source),
    ];
    return normalizeText(stack.join(" ")).includes(query);
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
      throw new Error(data.error || data.raw || `Request failed (${response.status})`);
    }
    return data;
  }

  function setStatus(text, isError = false) {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = text || "";
    statusEl.style.color = isError ? "#8d2200" : "";
  }

  function setSummary(text) {
    if (!summaryEl) {
      return;
    }
    summaryEl.textContent = text || "";
  }

  function inputHtml(item) {
    const type = item.type || "string";
    const key = item.key;
    const value = currentItemValue(item);
    const isSecret = Boolean(item.secret);

    if (type === "bool") {
      return `
        <label class="inline-check">
          <input type="checkbox" data-setting-key="${escapeHtml(key)}" data-setting-type="bool" ${value ? "checked" : ""}>
          <span>启用</span>
        </label>
      `;
    }

    if (type === "choice") {
      const options = Array.isArray(item.options) ? item.options : [];
      const optionsHtml = options
        .map((option) => {
          const optionValue = String(option.value || "");
          const selected = optionValue === String(value || "") ? "selected" : "";
          return `<option value="${escapeHtml(optionValue)}" ${selected}>${escapeHtml(option.label || optionValue || "(空)")}</option>`;
        })
        .join("");
      return `<select data-setting-key="${escapeHtml(key)}" data-setting-type="choice">${optionsHtml}</select>`;
    }

    if (type === "text") {
      return `<textarea rows="5" data-setting-key="${escapeHtml(key)}" data-setting-type="text">${escapeHtml(value ?? "")}</textarea>`;
    }

    if (type === "int") {
      const min = item.min !== undefined ? `min="${Number(item.min)}"` : "";
      const max = item.max !== undefined ? `max="${Number(item.max)}"` : "";
      return `<input type="number" ${min} ${max} data-setting-key="${escapeHtml(key)}" data-setting-type="int" value="${escapeHtml(value ?? "")}">`;
    }

    const inputType = isSecret ? "password" : "text";
    return `<input type="${inputType}" data-setting-key="${escapeHtml(key)}" data-setting-type="string" value="${escapeHtml(value ?? "")}" autocomplete="off">`;
  }

  function allItems() {
    return latestGroups.flatMap((group) => (Array.isArray(group.items) ? group.items : []));
  }

  function filteredGroups() {
    const query = normalizeText(filterInput && filterInput.value);
    const onlyOverrides = Boolean(onlyOverridesInput && onlyOverridesInput.checked);
    return latestGroups
      .map((group) => {
        const rawItems = Array.isArray(group.items) ? group.items : [];
        const items = rawItems.filter((item) => itemMatches(item, group.name || "", query, onlyOverrides));
        return {
          name: group.name || "未分组",
          allCount: rawItems.length,
          items,
        };
      })
      .filter((group) => group.items.length > 0);
  }

  function refreshSummary(filtered = null) {
    const groups = filtered || filteredGroups();
    const totalCount = allItems().length;
    const visibleCount = groups.reduce((sum, group) => sum + group.items.length, 0);
    const overrideCount = allItems().filter((item) => item.source === "override").length;
    const pendingCount = pendingReset.size;
    setSummary(`共 ${totalCount} 项，显示 ${visibleCount} 项，前端覆盖 ${overrideCount} 项，待回退 ${pendingCount} 项。`);
  }

  function render(groups) {
    latestGroups = Array.isArray(groups) ? groups : [];
    fieldByKey.clear();
    latestGroups.forEach((group) => {
      (group.items || []).forEach((item) => {
        if (item && item.key) {
          fieldByKey.set(item.key, item);
        }
      });
    });

    if (!form) {
      return;
    }

    if (!latestGroups.length) {
      form.innerHTML = `<p class="muted">暂无可配置项</p>`;
      setSummary("");
      return;
    }

    const filtered = filteredGroups();
    refreshSummary(filtered);

    if (!filtered.length) {
      form.innerHTML = `<p class="muted">没有匹配项，请调整筛选条件。</p>`;
      return;
    }

    form.innerHTML = filtered
      .map((group, index) => {
        const isOpen = hasActiveFilter() || index === 0;
        const itemsHtml = group.items
          .map(
            (item) => `
              <article class="admin-field ${pendingReset.has(item.key || "") ? "is-pending-reset" : ""}" data-field-key="${escapeHtml(item.key || "")}" data-field-source="${escapeHtml(item.source || "default")}">
                <div class="admin-field-head">
                  <strong>${escapeHtml(item.label || item.key || "")}</strong>
                  <span class="admin-source ${sourceClass(item.source)}">${escapeHtml(sourceLabel(item.source))}</span>
                </div>
                <p class="admin-field-key"><code>${escapeHtml(item.key || "")}</code></p>
                <span class="muted">${escapeHtml(item.description || "")}</span>
                ${inputHtml(item)}
                <p class="admin-field-default muted">默认值：${escapeHtml(valuePreview(item, item.default))}</p>
                ${fieldWarningHtml(item)}
                <button class="secondary admin-reset" type="button" data-reset-key="${escapeHtml(item.key || "")}">${pendingReset.has(item.key || "") ? "已标记回退" : "回退默认"}</button>
              </article>
            `,
          )
          .join("");
        return `
          <details class="admin-group" ${isOpen ? "open" : ""}>
            <summary>
              <span>${escapeHtml(group.name || "未分组")}</span>
              <span class="admin-group-meta">${group.items.length} / ${group.allCount}</span>
            </summary>
            <div class="admin-group-grid">${itemsHtml}</div>
          </details>
        `;
      })
      .join("");
  }

  function collectValues() {
    const values = {};
    allItems().forEach((item) => {
      const key = item.key || "";
      const type = item.type || "string";
      if (!key || pendingReset.has(key)) {
        return;
      }
      const value = currentItemValue(item);
      if (type === "bool") {
        values[key] = Boolean(value);
      } else {
        values[key] = value ?? "";
      }
    });
    return values;
  }

  function setFieldToDefault(key) {
    if (!form || !key) {
      return;
    }
    const field = fieldByKey.get(key);
    if (!field) {
      return;
    }

    draftValues.delete(key);
    pendingReset.add(key);
    render(latestGroups);
    setStatus(`已标记 ${key} 回退默认，点击“保存全部”生效。`);
  }

  async function loadSettings() {
    try {
      setStatus("正在加载管理员配置...");
      const data = await api("/api/admin/settings");
      pendingReset = new Set();
      draftValues.clear();
      render(data.groups || []);
      setStatus("配置已加载。");
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function saveSettings() {
    try {
      setStatus("正在保存...");
      const values = collectValues();
      const payload = {
        values,
        reset_keys: Array.from(pendingReset),
      };
      const data = await api("/api/admin/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      pendingReset = new Set();
      draftValues.clear();
      render(data.groups || []);
      setStatus("保存成功，配置已即时生效。");
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  document.addEventListener("click", (event) => {
    const resetBtn = event.target.closest("button[data-reset-key]");
    if (resetBtn) {
      setFieldToDefault(resetBtn.dataset.resetKey || "");
      return;
    }
  });

  document.addEventListener("input", (event) => {
    const input = event.target.closest("[data-setting-key]");
    if (!input) {
      return;
    }
    const key = input.dataset.settingKey || "";
    const fieldEl = input.closest(".admin-field");
    if (key && pendingReset.has(key)) {
      pendingReset.delete(key);
      if (fieldEl) {
        fieldEl.classList.remove("is-pending-reset");
        const resetBtn = fieldEl.querySelector("button[data-reset-key]");
        if (resetBtn) {
          resetBtn.textContent = "回退默认";
        }
      }
      refreshSummary();
    }
    if (key) {
      if (input.dataset.settingType === "bool") {
        draftValues.set(key, Boolean(input.checked));
      } else {
        draftValues.set(key, input.value);
      }
      setStatus("存在未保存改动，点击“保存全部”生效。");
    }
  });

  if (filterInput) {
    filterInput.addEventListener("input", () => {
      render(latestGroups);
    });
  }
  if (onlyOverridesInput) {
    onlyOverridesInput.addEventListener("change", () => {
      render(latestGroups);
    });
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", loadSettings);
  }
  if (saveBtn) {
    saveBtn.addEventListener("click", saveSettings);
  }

  loadSettings();
})();
