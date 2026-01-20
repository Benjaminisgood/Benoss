(function () {
  const statusEl = document.getElementById('admin-status');
  const qs = (sel, scope = document) => scope.querySelector(sel);

  const setStatus = (msg) => {
    if (statusEl) statusEl.textContent = msg;
  };

  const apiFetch = async (url, options = {}) => {
    const resp = await fetch(url, options);
    if (!resp.ok) {
      if (resp.status === 401 || resp.status === 403) {
        window.location.href = '/login';
        throw new Error('Login required');
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

  const loadQuickLinks = async () => {
    const container = qs('#quick-links-admin');
    if (!container) return;
    const data = await apiFetch('/api/admin/links/quick');
    container.innerHTML = '';
    data.items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'stack-item';
      row.innerHTML = `<div><strong>${item.title}</strong><div class="album-meta">${item.url}</div></div>`;
      const del = document.createElement('button');
      del.textContent = 'Remove';
      del.addEventListener('click', async () => {
        try {
          await apiFetch(`/api/admin/links/quick/${item.id}`, { method: 'DELETE' });
          loadQuickLinks();
        } catch (err) {
          setStatus(err.message);
        }
      });
      row.appendChild(del);
      container.appendChild(row);
    });
  };

  const bindQuickAdd = () => {
    const button = qs('#quick-add');
    if (!button) return;
    button.addEventListener('click', async () => {
      const title = qs('#quick-title')?.value || '';
      const url = qs('#quick-url')?.value || '';
      const sort = parseInt(qs('#quick-sort')?.value || '0', 10);
      if (!title || !url) {
        setStatus('Missing fields');
        return;
      }
      try {
        await apiFetch('/api/admin/links/quick', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, url, sort_order: sort, is_active: true }),
        });
        qs('#quick-title').value = '';
        qs('#quick-url').value = '';
        qs('#quick-sort').value = '0';
        loadQuickLinks();
      } catch (err) {
        setStatus(err.message);
      }
    });
  };

  const loadFriendLinks = async () => {
    const container = qs('#friend-links-admin');
    if (!container) return;
    const data = await apiFetch('/api/admin/links/friend');
    container.innerHTML = '';
    data.items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'stack-item';
      row.innerHTML = `<div><strong>${item.title}</strong><div class="album-meta">${item.url}</div></div>`;
      const del = document.createElement('button');
      del.textContent = 'Remove';
      del.addEventListener('click', async () => {
        try {
          await apiFetch(`/api/admin/links/friend/${item.id}`, { method: 'DELETE' });
          loadFriendLinks();
        } catch (err) {
          setStatus(err.message);
        }
      });
      row.appendChild(del);
      container.appendChild(row);
    });
  };

  const bindFriendAdd = () => {
    const button = qs('#friend-add');
    if (!button) return;
    button.addEventListener('click', async () => {
      const title = qs('#friend-title')?.value || '';
      const url = qs('#friend-url')?.value || '';
      const avatar = qs('#friend-avatar')?.value || '';
      const desc = qs('#friend-desc')?.value || '';
      const sort = parseInt(qs('#friend-sort')?.value || '0', 10);
      if (!title || !url) {
        setStatus('Missing fields');
        return;
      }
      try {
        await apiFetch('/api/admin/links/friend', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title,
            url,
            avatar_url: avatar || null,
            description: desc || null,
            sort_order: sort,
            is_active: true,
          }),
        });
        qs('#friend-title').value = '';
        qs('#friend-url').value = '';
        qs('#friend-avatar').value = '';
        qs('#friend-desc').value = '';
        qs('#friend-sort').value = '0';
        loadFriendLinks();
      } catch (err) {
        setStatus(err.message);
      }
    });
  };

  const loadUsers = async () => {
    const container = qs('#user-list');
    if (!container) return;
    const data = await apiFetch('/api/admin/users');
    container.innerHTML = '';
    data.items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'stack-item';
      row.innerHTML = `<div><strong>${item.username}</strong><div class="album-meta">${item.role}</div></div>`;
      container.appendChild(row);
    });
  };

  const bindUserAdd = () => {
    const button = qs('#user-add');
    if (!button) return;
    button.addEventListener('click', async () => {
      const username = qs('#user-username')?.value || '';
      const password = qs('#user-password')?.value || '';
      const role = qs('#user-role')?.value || 'user';
      if (!username || !password) {
        setStatus('Missing fields');
        return;
      }
      try {
        await apiFetch('/api/admin/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password, role }),
        });
        qs('#user-username').value = '';
        qs('#user-password').value = '';
        qs('#user-role').value = 'user';
        loadUsers();
      } catch (err) {
        setStatus(err.message);
      }
    });
  };

  const loadAccounts = async () => {
    const descriptionEl = qs('#account-description');
    const listEl = qs('#account-list');
    if (!descriptionEl && !listEl) return;
    const data = await apiFetch('/api/account');
    if (descriptionEl) {
      descriptionEl.value = data.current_user?.description || '';
    }
    if (listEl) {
      listEl.innerHTML = '';
      if (!data.accounts || data.accounts.length === 0) {
        listEl.innerHTML = '<div class="card placeholder">No other accounts yet.</div>';
      } else {
        data.accounts.forEach((account) => {
          const row = document.createElement('div');
          row.className = 'stack-item';
          const description = account.description || 'No description yet.';
          const wrapper = document.createElement('div');
          const title = document.createElement('strong');
          title.textContent = account.username;
          const meta = document.createElement('div');
          meta.className = 'album-meta';
          meta.textContent = description;
          wrapper.appendChild(title);
          wrapper.appendChild(meta);
          row.appendChild(wrapper);
          listEl.appendChild(row);
        });
      }
    }
  };

  const bindAccountSave = () => {
    const button = qs('#account-save');
    const descriptionEl = qs('#account-description');
    if (!button || !descriptionEl) return;
    button.addEventListener('click', async () => {
      setStatus('Saving description...');
      try {
        await apiFetch('/api/account/description', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ description: descriptionEl.value || '' }),
        });
        setStatus('Description saved');
        loadAccounts();
      } catch (err) {
        setStatus(err.message);
      }
    });
  };

  const loadAll = async () => {
    await loadAccounts();
    await loadQuickLinks();
    await loadFriendLinks();
    await loadUsers();
  };

  const init = async () => {
    try {
      await loadAll();
    } catch (err) {
      setStatus(err.message);
    }

    bindQuickAdd();
    bindFriendAdd();
    bindUserAdd();
    bindAccountSave();
  };

  init();
})();
