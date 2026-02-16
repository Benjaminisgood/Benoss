(function () {
  const page = document.body.dataset.page || "";
  if (page !== "admin") {
    return;
  }

  const form = document.getElementById("admin-settings-form");
  const statusEl = document.getElementById("admin-settings-status");
  const refreshBtn = document.getElementById("admin-refresh-btn");
  const saveBtn = document.getElementById("admin-save-btn");

  let pendingReset = new Set();
  let latestGroups = [];
  const fieldByKey = new Map();

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

  function inputHtml(item) {
    const type = item.type || "string";
    const key = item.key;
    const value = item.value;
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
      return;
    }

    form.innerHTML = latestGroups
      .map((group) => {
        const items = Array.isArray(group.items) ? group.items : [];
        const itemsHtml = items
          .map(
            (item) => `
              <article class="admin-field" data-field-key="${escapeHtml(item.key || "")}">
                <strong>${escapeHtml(item.label || item.key || "")}</strong>
                <span class="admin-source">${escapeHtml(sourceLabel(item.source))}</span>
                <span class="muted">${escapeHtml(item.description || "")}</span>
                ${inputHtml(item)}
                <button class="secondary admin-reset" type="button" data-reset-key="${escapeHtml(item.key || "")}">回退默认</button>
              </article>
            `,
          )
          .join("");
        return `
          <section class="admin-group">
            <h4>${escapeHtml(group.name || "未分组")}</h4>
            <div class="admin-group-grid">${itemsHtml}</div>
          </section>
        `;
      })
      .join("");
  }

  function fieldElements() {
    if (!form) {
      return [];
    }
    return Array.from(form.querySelectorAll("[data-setting-key]"));
  }

  function collectValues() {
    const values = {};
    fieldElements().forEach((element) => {
      const key = element.dataset.settingKey || "";
      const type = element.dataset.settingType || "string";
      if (!key || pendingReset.has(key)) {
        return;
      }

      if (type === "bool") {
        values[key] = Boolean(element.checked);
      } else {
        values[key] = element.value;
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

    const escaped = window.CSS && typeof window.CSS.escape === "function"
      ? window.CSS.escape(key)
      : key.replaceAll('"', '\\"');
    const input = form.querySelector(`[data-setting-key="${escaped}"]`);
    if (!input) {
      return;
    }

    const fallback = field.default;
    const type = field.type || "string";
    if (type === "bool") {
      input.checked = Boolean(fallback);
    } else {
      input.value = fallback ?? "";
    }
    pendingReset.add(key);
    setStatus(`已标记 ${key} 回退默认，点击“保存全部”生效。`);
  }

  async function loadSettings() {
    try {
      setStatus("正在加载管理员配置...");
      const data = await api("/api/admin/settings");
      render(data.groups || []);
      pendingReset = new Set();
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
    if (key && pendingReset.has(key)) {
      pendingReset.delete(key);
    }
  });

  if (refreshBtn) {
    refreshBtn.addEventListener("click", loadSettings);
  }
  if (saveBtn) {
    saveBtn.addEventListener("click", saveSettings);
  }

  loadSettings();
})();
