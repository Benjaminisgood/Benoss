(function () {
  const statusEl = document.getElementById('admin-status');
  const qs = (sel, scope = document) => scope.querySelector(sel);

  const setStatus = (msg) => {
    if (statusEl) statusEl.textContent = msg || '';
  };

  const apiFetch = async (url, options = {}) => {
    const resp = await fetch(url, options);
    if (!resp.ok) {
      if (resp.status === 401 || resp.status === 403) {
        window.location.href = '/login';
        throw new Error('登录已过期');
      }
      let message = `Request failed: ${resp.status}`;
      try {
        const data = await resp.json();
        if (data && data.error) message = data.error;
      } catch (err) {
        // ignore
      }
      throw new Error(message);
    }
    return resp.json();
  };

  const countChars = (text) => Array.from(String(text || '')).length;

  const autosizeTextarea = (ta, { min = 120, max = 360 } = {}) => {
    if (!ta || ta.hidden) return;
    // Let the textarea shrink when text is deleted.
    ta.style.height = 'auto';
    const next = Math.min(max, ta.scrollHeight || 0);
    ta.style.height = `${Math.max(min, next)}px`;
    ta.style.overflowY = ta.scrollHeight > max ? 'auto' : 'hidden';
  };

  const bindAccountDescriptionEditor = () => {
    const descriptionEl = qs('#account-description');
    const countEl = qs('#account-description-count');
    if (!descriptionEl) return;

    const sync = () => {
      if (countEl) countEl.textContent = `${countChars(descriptionEl.value)} 字`;
      window.requestAnimationFrame(() => autosizeTextarea(descriptionEl));
    };

    descriptionEl.addEventListener('input', sync);
    descriptionEl.addEventListener('keydown', (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
        ev.preventDefault();
        saveAccountDescription();
      }
    });

    window.addEventListener('resize', () => window.requestAnimationFrame(() => autosizeTextarea(descriptionEl)));
    sync();
  };

  let savingDescription = false;
  const saveAccountDescription = async () => {
    const descriptionEl = qs('#account-description');
    const button = qs('#account-save');
    if (!descriptionEl) return;
    if (savingDescription) return;
    savingDescription = true;
    if (button) button.disabled = true;
    setStatus('保存中...');
    try {
      await apiFetch('/api/account/description', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: descriptionEl.value || '' }),
      });
      setStatus('已保存');
    } catch (err) {
      setStatus(err.message);
    } finally {
      savingDescription = false;
      if (button) button.disabled = false;
    }
  };

  const loadAccounts = async () => {
    const descriptionEl = qs('#account-description');
    const listEl = qs('#account-list');
    if (!descriptionEl && !listEl) return null;
    const data = await apiFetch('/api/account');
    if (descriptionEl) {
      descriptionEl.value = data.current_user?.description || '';
    }
    if (listEl) {
      listEl.innerHTML = '';
      const items = data.accounts || [];
      if (!items.length) {
        listEl.innerHTML = '<div class="card placeholder">暂无其他用户。</div>';
      } else {
        items.forEach((account) => {
          const row = document.createElement('div');
          row.className = 'stack-item';
          const wrapper = document.createElement('div');
          const title = document.createElement('strong');
          title.textContent = account.username || '';
          const meta = document.createElement('div');
          meta.className = 'album-meta';
          meta.textContent = account.description || '';
          wrapper.appendChild(title);
          wrapper.appendChild(meta);
          row.appendChild(wrapper);
          listEl.appendChild(row);
        });
      }
    }
    return data.current_user || null;
  };

  const bindAccountSave = () => {
    const button = qs('#account-save');
    const descriptionEl = qs('#account-description');
    if (!button || !descriptionEl) return;
    button.addEventListener('click', saveAccountDescription);
  };

  const loadCollab = async () => {
    const inboxEl = qs('#collab-inbox');
    const outboxEl = qs('#collab-outbox');
    if (!inboxEl && !outboxEl) return;

    const renderEmpty = (el, msg) => {
      if (!el) return;
      el.innerHTML = `<div class="card placeholder">${msg}</div>`;
    };

    if (inboxEl) {
      renderEmpty(inboxEl, '加载中...');
      try {
        const data = await apiFetch('/api/collab/inbox');
        const items = data.items || [];
        inboxEl.innerHTML = '';
        if (!items.length) {
          renderEmpty(inboxEl, '暂无待处理请求。');
        } else {
          items.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'stack-item';

            const left = document.createElement('div');
            const title = document.createElement('strong');
            title.textContent = `${item.project?.title || 'Project'} (${item.project?.module || ''})`;
            const meta = document.createElement('div');
            meta.className = 'album-meta';
            meta.textContent = `来自 @${item.proposer?.username || 'user'} · ${item.file_count || 0} files`;
            left.appendChild(title);
            left.appendChild(meta);
            if (item.message) {
              const msg = document.createElement('div');
              msg.className = 'album-meta';
              msg.textContent = item.message;
              left.appendChild(msg);
            }

            const actions = document.createElement('div');
            const approve = document.createElement('button');
            approve.textContent = '同意';
            approve.addEventListener('click', async () => {
              try {
                setStatus('同意中...');
                await apiFetch(`/api/push-requests/${item.id}/approve`, { method: 'POST' });
                setStatus('已同意');
                loadCollab();
              } catch (err) {
                setStatus(err.message);
              }
            });
            const reject = document.createElement('button');
            reject.textContent = '拒绝';
            reject.className = 'danger';
            reject.addEventListener('click', async () => {
              if (!window.confirm('确定拒绝该请求？')) return;
              try {
                setStatus('拒绝中...');
                await apiFetch(`/api/push-requests/${item.id}/reject`, { method: 'POST' });
                setStatus('已拒绝');
                loadCollab();
              } catch (err) {
                setStatus(err.message);
              }
            });
            actions.appendChild(approve);
            actions.appendChild(reject);

            row.appendChild(left);
            row.appendChild(actions);
            inboxEl.appendChild(row);
          });
        }
      } catch (err) {
        renderEmpty(inboxEl, err.message);
      }
    }

    if (outboxEl) {
      renderEmpty(outboxEl, '加载中...');
      try {
        const data = await apiFetch('/api/collab/outbox');
        const items = data.items || [];
        outboxEl.innerHTML = '';
        if (!items.length) {
          renderEmpty(outboxEl, '暂无发出的请求。');
        } else {
          items.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'stack-item';

            const left = document.createElement('div');
            const title = document.createElement('strong');
            title.textContent = `${item.project?.title || 'Project'} (${item.project?.module || ''})`;
            const meta = document.createElement('div');
            meta.className = 'album-meta';
            meta.textContent = `发往 @${item.project?.owner_username || 'owner'} · ${item.status}`;
            left.appendChild(title);
            left.appendChild(meta);
            if (item.message) {
              const msg = document.createElement('div');
              msg.className = 'album-meta';
              msg.textContent = item.message;
              left.appendChild(msg);
            }

            row.appendChild(left);

            if (item.status === 'pending') {
              const cancel = document.createElement('button');
              cancel.textContent = '撤销';
              cancel.addEventListener('click', async () => {
                if (!window.confirm('确定撤销该请求？')) return;
                try {
                  setStatus('撤销中...');
                  await apiFetch(`/api/push-requests/${item.id}/cancel`, { method: 'POST' });
                  setStatus('已撤销');
                  loadCollab();
                } catch (err) {
                  setStatus(err.message);
                }
              });
              row.appendChild(cancel);
            }

            outboxEl.appendChild(row);
          });
        }
      } catch (err) {
        renderEmpty(outboxEl, err.message);
      }
    }
  };

  const loadUsers = async () => {
    const container = qs('#user-list');
    if (!container) return;
    container.innerHTML = '<div class="card placeholder">加载中...</div>';
    try {
      const data = await apiFetch('/api/admin/users');
      const items = data.items || [];
      container.innerHTML = '';
      if (!items.length) {
        container.innerHTML = '<div class="card placeholder">暂无用户。</div>';
        return;
      }
      items.forEach((user) => {
        const row = document.createElement('div');
        row.className = 'stack-item';

        const left = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = user.username || '';
        const meta = document.createElement('div');
        meta.className = 'album-meta';
        meta.textContent = `${user.role || 'user'} · ${user.is_active ? 'active' : 'inactive'}`;
        left.appendChild(title);
        left.appendChild(meta);

        const actions = document.createElement('div');

        const toggleActive = document.createElement('button');
        toggleActive.textContent = user.is_active ? '停用' : '启用';
        toggleActive.className = user.is_active ? 'danger' : '';
        toggleActive.addEventListener('click', async () => {
          const next = !user.is_active;
          if (!next && !window.confirm(`确定停用 ${user.username}？`)) return;
          try {
            setStatus('更新状态...');
            await apiFetch(`/api/admin/users/${user.id}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ is_active: next }),
            });
            setStatus('已更新');
            loadUsers();
          } catch (err) {
            setStatus(err.message);
          }
        });

        const resetPwd = document.createElement('button');
        resetPwd.textContent = '重置密码';
        resetPwd.addEventListener('click', async () => {
          const pwd = window.prompt(`为 ${user.username} 设置新密码：`);
          if (!pwd) return;
          try {
            setStatus('重置密码...');
            await apiFetch(`/api/admin/users/${user.id}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ password: pwd }),
            });
            setStatus('已重置');
          } catch (err) {
            setStatus(err.message);
          }
        });

        actions.appendChild(toggleActive);
        actions.appendChild(resetPwd);

        row.appendChild(left);
        row.appendChild(actions);
        container.appendChild(row);
      });
    } catch (err) {
      container.innerHTML = `<div class="card placeholder">${err.message}</div>`;
    }
  };

  const bindUserAdd = () => {
    const button = qs('#user-add');
    if (!button) return;
    button.addEventListener('click', async () => {
      const username = qs('#user-username')?.value || '';
      const password = qs('#user-password')?.value || '';
      if (!username || !password) {
        setStatus('缺少用户名或密码');
        return;
      }
      try {
        setStatus('创建中...');
        await apiFetch('/api/admin/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        });
        qs('#user-username').value = '';
        qs('#user-password').value = '';
        setStatus('已创建');
        loadUsers();
      } catch (err) {
        setStatus(err.message);
      }
    });
  };

  const init = async () => {
    try {
      await loadAccounts();
      await loadCollab();
      await loadUsers();
    } catch (err) {
      setStatus(err.message);
    }
    bindAccountSave();
    bindAccountDescriptionEditor();
    bindUserAdd();
  };

  init();
})();
