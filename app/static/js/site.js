(function () {
  const page = document.body.dataset.page || '';

  const qs = (sel, scope = document) => scope.querySelector(sel);
  const qsa = (sel, scope = document) => Array.from(scope.querySelectorAll(sel));

  const apiFetch = async (url, options = {}) => {
    const resp = await fetch(url, options);
    if (!resp.ok) {
      if (resp.status === 401) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/login?next=${next}`;
        throw new Error('需要登录');
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

  const formatDate = (date) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  };

  const formatBytes = (bytes) => {
    const value = Number(bytes || 0);
    if (!Number.isFinite(value) || value <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const exp = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
    const num = value / 1024 ** exp;
    return `${num.toFixed(num >= 10 || exp === 0 ? 0 : 1)} ${units[exp]}`;
  };

  const MEDIA_EXTS = {
    image: new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg']),
    video: new Set(['.mp4', '.mov', '.webm', '.mkv', '.avi']),
    audio: new Set(['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg']),
    pdf: new Set(['.pdf']),
    text: new Set(['.md', '.markdown', '.txt', '.json', '.yaml', '.yml', '.toml', '.ini', '.csv', '.tsv', '.log']),
  };

  const normalizeUrl = (value) => {
    if (typeof value === 'string') return value;
    if (!value) return '';
    if (typeof value === 'object') {
      if (typeof value.href === 'string') return value.href;
      if (typeof value.url === 'string') return value.url;
    }
    return '';
  };

  const getExtension = (url) => {
    const safeUrl = normalizeUrl(url);
    if (!safeUrl) return '';
    const clean = safeUrl.split('?')[0].split('#')[0];
    const dot = clean.lastIndexOf('.');
    if (dot === -1) return '';
    return clean.slice(dot).toLowerCase();
  };

  const detectMediaType = (url) => {
    const safeUrl = normalizeUrl(url);
    if (!safeUrl) return '';
    const lower = safeUrl.toLowerCase();
    if (lower.startsWith('data:')) {
      if (lower.startsWith('data:image/')) return 'image';
      if (lower.startsWith('data:video/')) return 'video';
      if (lower.startsWith('data:audio/')) return 'audio';
      if (lower.startsWith('data:application/pdf')) return 'pdf';
      return '';
    }
    const ext = getExtension(url);
    if (MEDIA_EXTS.image.has(ext)) return 'image';
    if (MEDIA_EXTS.video.has(ext)) return 'video';
    if (MEDIA_EXTS.audio.has(ext)) return 'audio';
    if (MEDIA_EXTS.pdf.has(ext)) return 'pdf';
    if (MEDIA_EXTS.text.has(ext)) return 'text';
    return '';
  };

  const createPdfCover = (src, opts = {}) => {
    const safeSrc = normalizeUrl(src);
    if (!safeSrc) return null;
    const previewUrl = normalizeUrl(opts.preview);
    const title = opts.title || opts.alt || 'PDF';
    const asLink = opts.asLink !== false;
    const wrapper = asLink ? document.createElement('a') : document.createElement('div');
    if (asLink) {
      wrapper.href = safeSrc;
      wrapper.target = '_blank';
      wrapper.rel = 'noopener';
    }
    wrapper.className = 'pdf-cover';
    wrapper.setAttribute('aria-label', title);

    if (previewUrl) {
      const img = document.createElement('img');
      img.src = previewUrl;
      img.alt = title;
      img.loading = 'lazy';
      img.decoding = 'async';
      img.addEventListener(
        'error',
        () => {
          img.remove();
        },
        { once: true }
      );
      wrapper.appendChild(img);
    }

    const badge = document.createElement('span');
    badge.className = 'pdf-badge';
    badge.textContent = 'PDF';
    wrapper.appendChild(badge);

    const text = document.createElement('span');
    text.className = 'pdf-title';
    text.textContent = title;
    wrapper.appendChild(text);

    return wrapper;
  };

  const createMediaNode = (type, src, opts = {}) => {
    const safeSrc = normalizeUrl(src);
    if (!safeSrc) return null;
    const resolvedType = type && type !== 'file' ? type : detectMediaType(safeSrc);
    const title = opts.title || '';
    const alt = opts.alt || '';
    const className = opts.className || '';
    let node = null;

    if (resolvedType === 'image') {
      node = document.createElement('img');
      node.src = safeSrc;
      node.alt = alt;
      node.loading = 'lazy';
      node.decoding = 'async';
      if (title) node.title = title;
    } else if (resolvedType === 'video') {
      node = document.createElement('video');
      node.src = safeSrc;
      node.controls = true;
      node.preload = 'metadata';
      node.playsInline = true;
      if (title) node.title = title;
      if (alt) node.setAttribute('aria-label', alt);
    } else if (resolvedType === 'audio') {
      node = document.createElement('audio');
      node.src = safeSrc;
      node.controls = true;
      node.preload = 'metadata';
      if (title) node.title = title;
      if (alt) node.setAttribute('aria-label', alt);
    } else if (resolvedType === 'pdf') {
      node = createPdfCover(safeSrc, {
        title,
        alt,
        preview: opts.preview,
        asLink: true,
      });
    }

    if (!node) return null;
    if (className) {
      node.className = `${node.className || ''} ${className}`.trim();
    }
    return node;
  };

  if (window.marked) {
    window.marked.setOptions({ breaks: true, mangle: false, headerIds: false });
    const renderer = new window.marked.Renderer();
    renderer.image = (href, title, text) => {
      const safeUrl = normalizeUrl(href);
      if (!safeUrl) return '';
      const mediaType = detectMediaType(safeUrl) || 'image';
      const node = createMediaNode(mediaType, safeUrl, {
        title: title || '',
        alt: text || '',
        className: 'markdown-media',
      });
      return node ? node.outerHTML : '';
    };
    window.marked.use({ renderer });
  }

  const renderMath = (element) => {
    if (!element || !window.renderMathInElement) return;
    window.renderMathInElement(element, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\\\(', right: '\\\\)', display: false },
        { left: '\\\\[', right: '\\\\]', display: true },
      ],
      throwOnError: false,
      strict: 'ignore',
    });
  };

  const renderMarkdown = (text, container) => {
    if (!container) return;
    const html = window.marked ? window.marked.parse(text || '') : text || '';
    container.innerHTML = html;
    renderMath(container);
  };

  const normalizeProjectPath = (value) => {
    if (!value) return '';
    let raw = String(value).replace(/\\\\/g, '/').trim();
    if (raw.startsWith('/')) raw = raw.slice(1);
    raw = raw.split('?')[0].split('#')[0];
    const pipe = raw.indexOf('|');
    if (pipe !== -1) raw = raw.slice(0, pipe).trim();
    if (!raw) return '';
    const parts = [];
    raw.split('/').forEach((seg) => {
      if (!seg || seg === '.') return;
      if (seg === '..') {
        parts.pop();
        return;
      }
      parts.push(seg);
    });
    return parts.join('/');
  };

  const resolveProjectRel = (baseDir, target) => {
    const normalized = normalizeProjectPath(target);
    if (!normalized) return '';
    if (String(target || '').trim().startsWith('/')) return normalized;
    const base = normalizeProjectPath(baseDir || '');
    if (!base) return normalized;
    return normalizeProjectPath(`${base}/${normalized}`);
  };

  const rewriteProjectMarkdown = (text, filesByPath, baseDir) => {
    if (!text) return '';
    const map = filesByPath || new Map();

    const rewriteTarget = (rawTarget) => {
      let target = String(rawTarget || '').trim();
      if (!target) return null;

      let wrapped = false;
      if (target.startsWith('<') && target.includes('>')) {
        wrapped = true;
        target = target.slice(1, target.indexOf('>'));
      } else {
        target = target.split(/\s+/)[0];
      }

      const lower = target.toLowerCase();
      if (
        lower.startsWith('http://') ||
        lower.startsWith('https://') ||
        lower.startsWith('data:') ||
        lower.startsWith('mailto:')
      ) {
        return null;
      }

      const resolved = resolveProjectRel(baseDir, target);
      if (!resolved) return null;
      const url = map.get(resolved);
      if (!url) return null;
      return wrapped ? `<${url}>` : url;
    };

    const MD_LINK_RE = /(!?\[[^\]]*\]\()([^)]+)(\))/g;
    let updated = String(text);
    updated = updated.replace(MD_LINK_RE, (match, pre, target, post) => {
      const rewritten = rewriteTarget(target);
      if (!rewritten) return match;
      const trimmed = String(target || '').trim();
      const first = trimmed.startsWith('<')
        ? trimmed.slice(0, trimmed.indexOf('>') + 1)
        : trimmed.split(/\s+/)[0];
      const tail = trimmed.slice(first.length);
      return `${pre}${rewritten}${tail}${post}`;
    });

    updated = updated.replace(/!\[\[([^\]]+)\]\]/g, (match, inner) => {
      const rewritten = rewriteTarget(inner);
      if (!rewritten) return match;
      const resolved = resolveProjectRel(baseDir, inner);
      const url = map.get(resolved) || '';
      if (!url) return match;
      return `![](${url})`;
    });
    updated = updated.replace(/\[\[([^\]]+)\]\]/g, (match, inner) => {
      const rewritten = rewriteTarget(inner);
      if (!rewritten) return match;
      const resolved = resolveProjectRel(baseDir, inner);
      const url = map.get(resolved) || '';
      if (!url) return match;
      return `[${inner}](${url})`;
    });
    return updated;
  };

  const initNav = () => {
    const toggle = qs('[data-nav-toggle]');
    const panel = qs('#nav-panel');
    if (!toggle || !panel) return;
    toggle.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      panel.classList.toggle('is-open', !expanded);
    });
  };

  const renderProjectWindow = async (projectId, prefix, scope = document, opts = {}) => {
    const titleEl = qs(`#${prefix}-title`, scope);
    const metaEl = qs(`#${prefix}-meta`, scope);
    const actionsEl = qs(`#${prefix}-actions`, scope);
    const filesEl = qs(`#${prefix}-files`, scope);
    const filesMetaEl = qs(`#${prefix}-files-meta`, scope);
    const uploaderEl = qs(`#${prefix}-uploader`, scope);
    const uploadInput = qs(`#${prefix}-upload-input`, scope);
    const uploadFolder = qs(`#${prefix}-upload-folder`, scope);
    const uploadPrefixInput = qs(`#${prefix}-upload-prefix`, scope);
    const uploadBtn = qs(`#${prefix}-upload-btn`, scope);
    const uploadStatus = qs(`#${prefix}-upload-status`, scope);
    const previewTitle = qs(`#${prefix}-preview-title`, scope);
    const previewMeta = qs(`#${prefix}-preview-meta`, scope);
    const previewEl = qs(`#${prefix}-preview`, scope);

    const setUploadStatus = (msg) => {
      if (uploadStatus) uploadStatus.textContent = msg || '';
    };

    if (filesEl) filesEl.innerHTML = '<div class="card placeholder">Loading...</div>';
    if (previewEl) previewEl.innerHTML = '<p class="muted">Loading...</p>';

    const data = await apiFetch(`/api/projects/${projectId}`);
    const project = data.project || {};
    const files = Array.isArray(data.files) ? data.files : [];
    const canEdit = Boolean(project.can_edit);

    if (titleEl) titleEl.textContent = project.title || `Project #${projectId}`;
    if (metaEl) {
      const owner = (project.owner && project.owner.username) || '';
      const vis = project.visibility || 'private';
      metaEl.textContent = [owner ? `@${owner}` : '', vis].filter(Boolean).join('  ·  ');
    }

    const fileUrlByPath = new Map();
    files.forEach((f) => {
      if (f && f.path && f.url) {
        fileUrlByPath.set(normalizeProjectPath(f.path), f.url);
      }
    });

    const showPreview = async (file) => {
      if (!file || !previewEl) return;
      const path = file.path || '';
      if (previewTitle) previewTitle.textContent = path || '预览';
      if (previewMeta) {
        previewMeta.textContent = `${file.media_type || 'file'}  ·  ${formatBytes(file.size_bytes || 0)}`;
      }
      previewEl.innerHTML = '';

      if (file.media_type === 'text') {
        previewEl.innerHTML = '<div class="card placeholder">Loading text...</div>';
        try {
          const data = await apiFetch(
            `/api/projects/${projectId}/file/text?path=${encodeURIComponent(file.path || '')}`
          );
          const text = data.text || '';
          previewEl.innerHTML = '';
          const baseDir = normalizeProjectPath(path).split('/').slice(0, -1).join('/');
          const rewritten = rewriteProjectMarkdown(text, fileUrlByPath, baseDir);
          if (String(path).toLowerCase().endsWith('.md') || String(path).toLowerCase().endsWith('.markdown')) {
            const wrap = document.createElement('div');
            wrap.className = 'markdown-body';
            previewEl.appendChild(wrap);
            renderMarkdown(rewritten, wrap);
          } else {
            const pre = document.createElement('pre');
            pre.className = 'code-block';
            pre.textContent = rewritten;
            previewEl.appendChild(pre);
          }
        } catch (err) {
          previewEl.innerHTML = `<div class="card placeholder">${err.message}</div>`;
        }
        return;
      }

      const node = createMediaNode(file.media_type || 'file', file.url || '', {
        alt: path,
        title: path,
        preview: file.preview_url || '',
        className: 'markdown-media',
      });
      if (node) {
        previewEl.appendChild(node);
      } else {
        const link = document.createElement('a');
        link.href = file.url || '#';
        link.textContent = '下载';
        link.className = 'text-link';
        link.target = '_blank';
        link.rel = 'noopener';
        previewEl.appendChild(link);
      }

      const dl = document.createElement('a');
      dl.href = file.url || '#';
      dl.textContent = '新标签页打开';
      dl.className = 'text-link';
      dl.target = '_blank';
      dl.rel = 'noopener';
      previewEl.appendChild(dl);
    };

    if (actionsEl) {
      actionsEl.innerHTML = '';

      const addBtn = (label, onClick, cls = 'pill-btn ghost') => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = cls;
        b.textContent = label;
        b.addEventListener('click', onClick);
        actionsEl.appendChild(b);
      };

      addBtn('Clone', async () => {
        const title = window.prompt('克隆后的项目名(可选):', `${project.title || 'Project'} (clone)`);
        const payload = title ? { title } : {};
        const resp = await apiFetch(`/api/projects/${projectId}/clone`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (resp && resp.project && resp.project.id) {
          window.location.href = `/${project.module || 'blog'}?project=${resp.project.id}`;
        }
      });

      if (canEdit) {
        addBtn(project.visibility === 'public' ? '设为私有' : '设为公开', async () => {
          const next = project.visibility === 'public' ? 'private' : 'public';
          await apiFetch(`/api/projects/${projectId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ visibility: next }),
          });
          await renderProjectWindow(projectId, prefix, scope, opts);
        });
        addBtn(
          '删除项目',
          async () => {
            if (!window.confirm('删除该项目及其所有文件？')) return;
            await apiFetch(`/api/projects/${projectId}`, { method: 'DELETE' });
            window.location.href = window.location.pathname;
          },
          'pill-btn ghost danger'
        );
      } else {
        addBtn('Propose push', async () => {
          const message = window.prompt('留言(可选):', '') || '';
          const created = await apiFetch(`/api/projects/${projectId}/push-requests`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
          });
          const prId = created && created.push_request && created.push_request.id;
          if (!prId) {
            window.alert('创建 push request 失败');
            return;
          }
          const input = document.createElement('input');
          input.type = 'file';
          input.multiple = true;
          const isFolder = window.confirm('上传文件夹？(取消=上传文件)');
          if (isFolder) {
            input.setAttribute('webkitdirectory', '');
            input.setAttribute('directory', '');
          }
          input.addEventListener(
            'change',
            async () => {
              const selected = Array.from(input.files || []);
              if (!selected.length) return;
              const prefixPath = window.prompt('路径前缀(可选):', '') || '';
              for (const file of selected) {
                const fd = new FormData();
                const rel = (file.webkitRelativePath || file.name || '').replace(/\\\\/g, '/');
                const path = `${prefixPath || ''}${prefixPath && !prefixPath.endsWith('/') ? '/' : ''}${rel}`;
                fd.append('path', path);
                fd.append('file', file);
                await apiFetch(`/api/push-requests/${prId}/files/upload`, { method: 'POST', body: fd });
              }
              window.alert('已发送 push 请求，项目拥有者可在 Control Room 中同意。');
            },
            { once: true }
          );
          input.click();
        });
      }
    }

    if (filesMetaEl) filesMetaEl.textContent = `${files.length} files`;

    if (filesEl) {
      filesEl.innerHTML = '';
      if (!files.length) {
        filesEl.innerHTML = '<div class="card placeholder">No files yet.</div>';
      } else {
        files.forEach((file) => {
          const row = document.createElement('div');
          row.className = 'stack-item';

          const left = document.createElement('div');
          const strong = document.createElement('strong');
          strong.textContent = file.path || 'file';
          const meta = document.createElement('div');
          meta.className = 'album-meta';
          meta.textContent = `${file.media_type || 'file'} · ${formatBytes(file.size_bytes || 0)}`;
          left.appendChild(strong);
          left.appendChild(meta);
          row.appendChild(left);

          const openBtn = document.createElement('button');
          openBtn.type = 'button';
          openBtn.className = 'row-btn';
          openBtn.textContent = '打开';
          openBtn.addEventListener('click', () => showPreview(file));
          row.appendChild(openBtn);

          if (canEdit) {
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'row-btn danger';
            delBtn.textContent = '删除';
            delBtn.addEventListener('click', async () => {
              if (!window.confirm(`删除 ${file.path}？`)) return;
              await apiFetch(`/api/projects/${projectId}/files`, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: file.path }),
              });
              await renderProjectWindow(projectId, prefix, scope, opts);
            });
            row.appendChild(delBtn);
          }

          filesEl.appendChild(row);
        });
      }
    }

    if (uploaderEl) {
      uploaderEl.hidden = !canEdit;
    }
    if (canEdit && uploadBtn && (uploadInput || uploadFolder)) {
      uploadBtn.onclick = async () => {
        const selected = [
          ...Array.from((uploadInput && uploadInput.files) || []),
          ...Array.from((uploadFolder && uploadFolder.files) || []),
        ];
        if (!selected.length) {
          setUploadStatus('请选择文件');
          return;
        }
        const prefixValue = (uploadPrefixInput && uploadPrefixInput.value) || '';
        setUploadStatus('上传中...');
        try {
          for (const file of selected) {
            const fd = new FormData();
            const rel = (file.webkitRelativePath || file.name || '').replace(/\\\\/g, '/');
            const base = String(prefixValue || '').trim();
            const path = `${base || ''}${base && !base.endsWith('/') ? '/' : ''}${rel}`;
            fd.append('path', path);
            fd.append('file', file);
            await apiFetch(`/api/projects/${projectId}/files/upload`, { method: 'POST', body: fd });
          }
          setUploadStatus('已上传');
          if (uploadInput) uploadInput.value = '';
          if (uploadFolder) uploadFolder.value = '';
          await renderProjectWindow(projectId, prefix, scope, opts);
        } catch (err) {
          setUploadStatus(err.message);
        }
      };
    }

    const preselect = opts && opts.preselectPath ? normalizeProjectPath(opts.preselectPath) : '';
    if (preselect) {
      const target = files.find((f) => normalizeProjectPath(f.path) === preselect);
      if (target) showPreview(target);
    } else if (files.length) {
      showPreview(files[0]);
    } else if (previewEl) {
      previewEl.innerHTML = '<p class="muted">No files to preview.</p>';
    }

    return { project, files };
  };

  const initProjectsPage = (moduleName) => {
    const listEl = qs(`#${moduleName}-list`);
    const searchEl = qs(`#${moduleName}-search`);
    const refreshBtn = qs('[data-refresh-list]');
    const newBtn = qs('[data-project-new]');

    let items = [];
    let selectedId = null;

    const renderList = (filter = '') => {
      if (!listEl) return;
      listEl.innerHTML = '';
      const term = (filter || '').toLowerCase();
      const filtered = items.filter((item) => {
        const title = String(item.title || '').toLowerCase();
        const owner = String((item.owner && item.owner.username) || '').toLowerCase();
        return title.includes(term) || owner.includes(term);
      });
      if (!filtered.length) {
        listEl.innerHTML = '<div class="card placeholder">No items found.</div>';
        return;
      }
      filtered.forEach((item) => {
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'list-item';
        el.dataset.projectId = String(item.id);
        el.classList.toggle('is-active', selectedId === item.id);
        const owner = (item.owner && item.owner.username) || '';
        const vis = item.visibility || 'private';
        const meta = `${owner ? `@${owner}` : ''}${owner ? '  ·  ' : ''}${vis}${
          item.file_count != null ? `  ·  ${item.file_count} files` : ''
        }`;
        el.innerHTML = `<strong>${item.title || `Project #${item.id}`}</strong><span class="muted">${meta}</span>`;
        el.addEventListener('click', () => loadItem(item.id));
        listEl.appendChild(el);
      });
    };

    const loadList = async () => {
      if (!listEl) return;
      listEl.innerHTML = '<div class="card placeholder">Loading...</div>';
      const data = await apiFetch(`/api/projects?module=${encodeURIComponent(moduleName)}`);
      items = data.items || [];
      renderList(searchEl ? searchEl.value : '');
      const params = new URLSearchParams(window.location.search);
      const preselect = params.get('project');
      if (preselect) {
        loadItem(parseInt(preselect, 10));
      } else if (items.length) {
        loadItem(items[0].id);
      }
    };

    const loadItem = async (id) => {
      if (!id) return;
      selectedId = id;
      qsa('.list-item', listEl).forEach((el) => {
        el.classList.toggle('is-active', Number(el.dataset.projectId || 0) === Number(id));
      });
      try {
        await renderProjectWindow(id, moduleName);
        const params = new URLSearchParams(window.location.search);
        params.set('project', String(id));
        history.replaceState(null, '', `${window.location.pathname}?${params}`);
      } catch (err) {
        const filesEl = qs(`#${moduleName}-files`);
        const previewEl = qs(`#${moduleName}-preview`);
        if (filesEl) filesEl.innerHTML = `<div class="card placeholder">${err.message}</div>`;
        if (previewEl) previewEl.innerHTML = `<div class="card placeholder">${err.message}</div>`;
      }
    };

    if (searchEl) {
      searchEl.addEventListener('input', (event) => renderList(event.target.value));
    }
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => loadList().catch((err) => (listEl.innerHTML = err.message)));
    }
    if (newBtn) {
      newBtn.addEventListener('click', async () => {
        const title = window.prompt('项目名:');
        if (!title) return;
        const isPublic = window.confirm('设为公开？');
        const resp = await apiFetch('/api/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ module: moduleName, title, visibility: isPublic ? 'public' : 'private' }),
        });
        const proj = resp && resp.project;
        await loadList();
        if (proj && proj.id) loadItem(proj.id);
      });
    }

    loadList().catch((err) => {
      if (listEl) listEl.innerHTML = `<div class="card placeholder">${err.message}</div>`;
    });
  };

  const initEchoes = () => {
    const moduleSelect = qs('#album-module');
    const typeSelect = qs('#album-type');
    const loadBtn = qs('[data-album-load]');
    const grid = qs('#album-grid');
    const status = qs('[data-album-status]');
    const moreBtn = qs('[data-album-more]');
    const sentinel = qs('[data-album-sentinel]');
    const modal = qs('#resource-modal');
    const modalContent = qs('#resource-modal-content');
    const projectTemplate = qs('#modal-project-template');
    if (!grid) return;

    const supportsObserver = 'IntersectionObserver' in window;
    let streamObserver = null;
    let offset = 0;
    const limit = 120;
    let loading = false;
    let hasMore = true;

    const setStatus = (text) => {
      if (status) status.textContent = text || '';
    };

    const updateFooter = () => {
      if (loading) setStatus('Loading...');
      else if (!hasMore) setStatus('No more files.');
      else setStatus('Scroll to load more.');
      if (moreBtn) moreBtn.hidden = supportsObserver || loading || !hasMore;
    };

    const resetGrid = () => {
      grid.innerHTML = '<div class="album-empty">Loading...</div>';
    };

    const resizeGridItem = (item) => {
      if (!item) return;
      const styles = window.getComputedStyle(grid);
      const rowHeight = parseFloat(styles.getPropertyValue('grid-auto-rows')) || 1;
      const rowGap = parseFloat(styles.getPropertyValue('gap')) || 0;
      const card = qs('.album-card', item);
      if (!card) return;
      const height = card.getBoundingClientRect().height;
      const span = Math.ceil((height + rowGap) / (rowHeight + rowGap));
      item.style.gridRowEnd = `span ${span}`;
    };

    const resizeAllGridItems = () => qsa('.album-item', grid).forEach((item) => resizeGridItem(item));

    const showModalMessage = (message) => {
      if (!modalContent) return;
      modalContent.innerHTML = `<div class="card placeholder">${message}</div>`;
    };

    const closeModal = () => {
      if (!modal) return;
      modal.hidden = true;
      document.body.classList.remove('modal-open');
      if (modalContent) modalContent.innerHTML = '';
    };

    const openModal = async (item) => {
      if (!modal || !modalContent) return;
      const template = projectTemplate;
      const prefix = 'modal-project';
      if (!template) {
        showModalMessage('No preview available.');
        modal.hidden = false;
        document.body.classList.add('modal-open');
        return;
      }
      modalContent.innerHTML = '';
      modalContent.appendChild(template.content.cloneNode(true));
      modal.hidden = false;
      document.body.classList.add('modal-open');
      try {
        const projectId = item && item.project && item.project.id;
        if (!projectId) {
          showModalMessage('Missing project id.');
          return;
        }
        await renderProjectWindow(projectId, prefix, modalContent, { preselectPath: item.path || '' });
      } catch (err) {
        showModalMessage(err.message);
      }
    };

    const buildCard = (item) => {
      const wrapper = document.createElement('button');
      wrapper.type = 'button';
      wrapper.className = 'album-item';

      const card = document.createElement('article');
      card.className = 'album-card';
      const preview = document.createElement('div');
      preview.className = 'album-preview';

      const displayName = item.path || '';
      const imagePreviewUrl = item.preview_url || '';
      const imageUrl = item.url || '';
      const videoPreviewUrl = item.preview_url || '';
      const resolvedType = item.media_type || detectMediaType(item.url) || 'file';

      if (resolvedType === 'image') {
        const img = document.createElement('img');
        img.alt = displayName || 'image';
        img.decoding = 'async';
        img.src = imagePreviewUrl || imageUrl;
        img.addEventListener('load', () => resizeGridItem(wrapper), { once: true });
        preview.appendChild(img);
      } else if (resolvedType === 'video') {
        if (videoPreviewUrl) {
          const img = document.createElement('img');
          img.alt = displayName || 'video';
          img.decoding = 'async';
          img.src = videoPreviewUrl;
          img.addEventListener('load', () => resizeGridItem(wrapper), { once: true });
          preview.appendChild(img);
        } else {
          const fallback = document.createElement('div');
          fallback.className = 'album-fallback';
          fallback.textContent = 'Video';
          preview.appendChild(fallback);
        }
      } else if (resolvedType === 'audio') {
        const fallback = document.createElement('div');
        fallback.className = 'album-fallback album-fallback--audio';
        fallback.textContent = displayName || 'Audio';
        preview.appendChild(fallback);
      } else if (resolvedType === 'pdf') {
        const cover = createPdfCover(imageUrl, {
          title: displayName || 'PDF',
          preview: imagePreviewUrl,
          asLink: false,
        });
        if (cover) preview.appendChild(cover);
      } else {
        const fallback = document.createElement('div');
        fallback.className = 'album-fallback album-fallback--file';
        const label = document.createElement('span');
        label.textContent = displayName || 'File';
        fallback.appendChild(label);
        preview.appendChild(fallback);
      }

      const badges = document.createElement('div');
      badges.className = 'album-badges';
      const typeBadge = document.createElement('span');
      typeBadge.className = 'album-badge';
      typeBadge.textContent = resolvedType;
      badges.appendChild(typeBadge);
      const moduleBadge = document.createElement('span');
      moduleBadge.className = 'album-badge';
      moduleBadge.textContent = item.project?.module || 'project';
      badges.appendChild(moduleBadge);
      preview.appendChild(badges);

      card.appendChild(preview);

      const body = document.createElement('div');
      body.className = 'album-body';
      const title = document.createElement('strong');
      title.textContent = displayName || 'file';
      const meta = document.createElement('div');
      meta.className = 'album-meta';
      meta.textContent = [item.project?.title || '', item.project?.owner_username ? `@${item.project.owner_username}` : '']
        .filter(Boolean)
        .join('  ·  ');
      body.appendChild(title);
      body.appendChild(meta);
      card.appendChild(body);

      wrapper.appendChild(card);
      wrapper.addEventListener('click', () => openModal(item));
      requestAnimationFrame(() => resizeGridItem(wrapper));
      return wrapper;
    };

    const appendItems = (items) => {
      if (!items.length) return;
      const empty = qs('.album-empty', grid);
      if (empty) empty.remove();
      items.map((item) => buildCard(item)).forEach((node) => grid.appendChild(node));
      requestAnimationFrame(() => resizeAllGridItems());
    };

    const showEmpty = (message) => {
      grid.innerHTML = `<div class="album-empty">${message}</div>`;
    };

    const loadEchoes = async (reset = false) => {
      if (loading || (!hasMore && !reset)) return;
      if (reset) {
        offset = 0;
        hasMore = true;
        resetGrid();
      }
      loading = true;
      updateFooter();

      const params = new URLSearchParams();
      if (moduleSelect && moduleSelect.value) params.set('module', moduleSelect.value);
      if (typeSelect && typeSelect.value) params.set('media_type', typeSelect.value);
      params.set('limit', String(limit));
      params.set('offset', String(offset));

      try {
        const data = await apiFetch(`/api/projects/public/files?${params.toString()}`);
        const items = data.items || [];
        if (!items.length) {
          if (offset === 0) showEmpty('No files found.');
          hasMore = false;
          return;
        }
        appendItems(items);
        offset += limit;
        hasMore = items.length >= limit;
      } catch (err) {
        showEmpty(err.message);
        hasMore = false;
      } finally {
        loading = false;
        updateFooter();
      }
    };

    if (loadBtn) loadBtn.addEventListener('click', () => loadEchoes(true));
    if (moduleSelect) moduleSelect.addEventListener('change', () => loadEchoes(true));
    if (typeSelect) typeSelect.addEventListener('change', () => loadEchoes(true));
    if (moreBtn) moreBtn.addEventListener('click', () => loadEchoes());

    if (supportsObserver && sentinel) {
      streamObserver = new IntersectionObserver(
        (entries) => entries.forEach((entry) => entry.isIntersecting && loadEchoes()),
        { rootMargin: '300px 0px' }
      );
      streamObserver.observe(sentinel);
    } else if (moreBtn) {
      moreBtn.hidden = false;
    }

    if (modal) {
      modal.addEventListener('click', (event) => {
        if (event.target && event.target.matches('[data-modal-close]')) closeModal();
      });
    }

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeModal();
    });

    window.addEventListener('resize', () => resizeAllGridItems());
    loadEchoes(true);
  };

  const initDailyreel = () => {
    const refreshBtn = qs('[data-dailyreel-refresh]');
    const dateEl = qs('#dailyreel-date');
    const tableBody = qs('#dailyreel-scoreboard tbody');
    const feed = qs('#dailyreel-feed');

    const calendarGrid = qs('#dailyreel-calendar');
    const monthTitle = qs('#dailyreel-month');
    const prevBtn = qs('[data-cal-prev]');
    const nextBtn = qs('[data-cal-next]');
    const todayBtn = qs('[data-cal-today]');

    const tzOffset = () => new Date().getTimezoneOffset();
    const isValidDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || ''));
    const formatMonth = (date) => {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, '0');
      return `${year}-${month}`;
    };
    const parseDate = (value) => {
      if (!isValidDate(value)) return null;
      const [y, m, d] = String(value).split('-').map((n) => parseInt(n, 10));
      if (!y || !m || !d) return null;
      const dt = new Date(y, m - 1, d);
      if (Number.isNaN(dt.getTime())) return null;
      return dt;
    };
    const clampDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate());

    let selectedDate = formatDate(new Date());
    let monthCursor = clampDay(new Date());
    monthCursor.setDate(1);
    let monthCounts = new Map();

    const syncUrl = () => {
      const params = new URLSearchParams(window.location.search);
      params.set('date', selectedDate);
      history.replaceState(null, '', `${window.location.pathname}?${params}`);
    };

    const loadDay = async (date) => {
      if (!isValidDate(date)) return;
      selectedDate = date;
      syncUrl();
      if (dateEl) dateEl.textContent = date;
      if (tableBody) tableBody.innerHTML = '<tr><td class="muted" colspan="5">Loading...</td></tr>';
      if (feed) feed.innerHTML = '<div class="card placeholder">Loading...</div>';

      try {
        const data = await apiFetch(`/api/dailyreel/today?date=${date}&tz_offset=${tzOffset()}`);
        const scoreboard = Array.isArray(data.scoreboard) ? data.scoreboard : [];
        const events = Array.isArray(data.events) ? data.events : [];

        if (dateEl) dateEl.textContent = data.date || date;

        if (tableBody) {
          tableBody.innerHTML = '';
          if (!scoreboard.length) {
            tableBody.innerHTML = '<tr><td class="muted" colspan="5">今天还没有记录。</td></tr>';
          } else {
            scoreboard.forEach((row) => {
              const tr = document.createElement('tr');
              const total = (row.git_blog || 0) + (row.git_note || 0) + (row.clone || 0);
              tr.innerHTML = `
                <td>${row.username || ''}</td>
                <td>${row.git_blog || 0}</td>
                <td>${row.git_note || 0}</td>
                <td>${row.clone || 0}</td>
                <td><strong>${total}</strong></td>
              `;
              tableBody.appendChild(tr);
            });
          }
        }

        if (feed) {
          feed.innerHTML = '';
          if (!events.length) {
            feed.innerHTML = '<div class="card placeholder">今天还没有 git / clone 记录。</div>';
          } else {
            events.forEach((ev) => {
              const row = document.createElement('div');
              row.className = 'stack-item';
              const left = document.createElement('div');
              const strong = document.createElement('strong');
              const type = ev.type || '';
              const module = ev.module || ev.project?.module || '';
              const actor = ev.actor?.username || '';
              const projTitle = ev.project?.title || `Project #${ev.project?.id || ''}`;
              strong.textContent = `${actor ? `@${actor} ` : ''}${type} ${module}`;
              const meta = document.createElement('div');
              meta.className = 'album-meta';
              const time = ev.created_at
                ? new Date(ev.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                : '';
              meta.textContent = [time, projTitle].filter(Boolean).join('  ·  ');
              left.appendChild(strong);
              left.appendChild(meta);
              row.appendChild(left);

              if (ev.project && ev.project.id) {
                const a = document.createElement('a');
                a.className = 'text-link';
                a.href = `/${ev.project.module || module || 'blog'}?project=${ev.project.id}`;
                a.textContent = '打开';
                row.appendChild(a);
              }
              feed.appendChild(row);
            });
          }
        }
      } catch (err) {
        if (tableBody) tableBody.innerHTML = `<tr><td class="muted" colspan="5">${err.message}</td></tr>`;
        if (feed) feed.innerHTML = `<div class="card placeholder">${err.message}</div>`;
      }
    };

    const loadMonth = async () => {
      if (!calendarGrid) return;
      const month = formatMonth(monthCursor);
      if (monthTitle) monthTitle.textContent = month;
      calendarGrid.innerHTML = '<div class="muted">Loading...</div>';
      monthCounts = new Map();
      try {
        const data = await apiFetch(`/api/dailyreel/month?month=${month}&tz_offset=${tzOffset()}`);
        const days = Array.isArray(data.days) ? data.days : [];
        days.forEach((row) => {
          if (row && row.date) monthCounts.set(row.date, row);
        });
        if (monthTitle) monthTitle.textContent = data.month || month;
      } catch (err) {
        calendarGrid.innerHTML = `<div class="muted">${err.message}</div>`;
        return;
      }
      renderCalendar();
    };

    const renderCalendar = () => {
      if (!calendarGrid) return;
      calendarGrid.innerHTML = '';

      const today = formatDate(new Date());
      const year = monthCursor.getFullYear();
      const monthIdx = monthCursor.getMonth();
      const first = new Date(year, monthIdx, 1);
      const last = new Date(year, monthIdx + 1, 0);
      const daysInMonth = last.getDate();

      // Monday-start calendar.
      const startDow = (first.getDay() + 6) % 7; // 0..6 (Mon..Sun)
      const totalCells = Math.ceil((startDow + daysInMonth) / 7) * 7;

      const addCell = (label, dateStr, muted) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'calendar-day';
        btn.textContent = String(label);
        if (muted) btn.classList.add('is-muted');
        if (dateStr === selectedDate) btn.classList.add('is-active');
        if (dateStr === today) btn.classList.add('is-today');

        const info = dateStr ? monthCounts.get(dateStr) : null;
        const gitTotal = info ? Number(info.git_blog || 0) + Number(info.git_note || 0) : 0;
        const cloneTotal = info ? Number(info.clone || 0) : 0;
        const total = gitTotal + cloneTotal;
        if (total > 0) btn.classList.add('has-entry');

        if (total > 0) {
          const count = document.createElement('span');
          count.className = 'calendar-day__count';
          count.textContent = gitTotal > 0 ? String(gitTotal) : `c${cloneTotal}`;
          if (info) {
            count.title = `git: ${gitTotal} (blog ${Number(info.git_blog || 0)}, note ${Number(info.git_note || 0)}) · clone: ${cloneTotal}`;
          }
          btn.appendChild(count);
        }

        if (!muted && dateStr) {
          btn.addEventListener('click', () => {
            loadDay(dateStr);
            renderCalendar();
          });
        } else {
          btn.disabled = true;
        }
        calendarGrid.appendChild(btn);
      };

      const padPrev = startDow;
      const prevLast = new Date(year, monthIdx, 0).getDate();
      for (let i = padPrev; i > 0; i -= 1) {
        addCell(prevLast - i + 1, '', true);
      }

      for (let d = 1; d <= daysInMonth; d += 1) {
        const dt = new Date(year, monthIdx, d);
        const dateStr = formatDate(dt);
        addCell(d, dateStr, false);
      }

      const remaining = totalCells - (padPrev + daysInMonth);
      for (let i = 1; i <= remaining; i += 1) {
        addCell(i, '', true);
      }
    };

    const applyInitialDate = () => {
      const params = new URLSearchParams(window.location.search);
      const urlDate = params.get('date');
      if (isValidDate(urlDate)) {
        selectedDate = urlDate;
        const dt = parseDate(urlDate);
        if (dt) {
          monthCursor = clampDay(dt);
          monthCursor.setDate(1);
        }
      }
    };

    if (refreshBtn) refreshBtn.addEventListener('click', () => loadDay(selectedDate));
    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth() - 1, 1);
        loadMonth();
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth() + 1, 1);
        loadMonth();
      });
    }
    if (todayBtn) {
      todayBtn.addEventListener('click', () => {
        const dt = new Date();
        selectedDate = formatDate(dt);
        monthCursor = clampDay(dt);
        monthCursor.setDate(1);
        loadMonth();
        loadDay(selectedDate);
      });
    }

    applyInitialDate();
    loadMonth();
    loadDay(selectedDate);
  };

  const initHome = () => {
    const tbody = qs('#quick-links-table tbody');
    const addBtn = qs('#quick-add');
    const titleInput = qs('#quick-title');
    const urlInput = qs('#quick-url');
    const sortInput = qs('#quick-sort');
    const todayBtn = qs('[data-today-load]');
    const todayCard = qs('#today-card');

    const whiteboardEl = qs('#whiteboard');
    const whiteboardWorldEl = qs('#whiteboard-world');
    const whiteboardDateEl = qs('#whiteboard-date');
    const whiteboardAddBtn = qs('[data-whiteboard-add]');
    const whiteboardMediaBtn = qs('[data-whiteboard-media]');
    const whiteboardImportBtn = qs('[data-whiteboard-import]');
    const whiteboardExportBtn = qs('[data-whiteboard-export]');
    const whiteboardResetBtn = qs('[data-whiteboard-reset]');
    const whiteboardImportFile = qs('#whiteboard-import-file');
    const whiteboardMediaFile = qs('#whiteboard-media-file');

    const loadQuickLinks = async () => {
      if (!tbody) return;
      tbody.innerHTML = '<tr><td class="muted" colspan="4">Loading...</td></tr>';
      try {
        const data = await apiFetch('/api/links/quick');
        const items = Array.isArray(data.items) ? data.items : [];
        tbody.innerHTML = '';
        if (!items.length) {
          tbody.innerHTML = '<tr><td class="muted" colspan="4">暂无链接。</td></tr>';
          return;
        }
        items.forEach((item) => {
          const tr = document.createElement('tr');

          const tdTitle = document.createElement('td');
          const a = document.createElement('a');
          a.href = item.url || '#';
          a.target = '_blank';
          a.rel = 'noopener';
          a.className = 'text-link';
          a.textContent = item.title || 'Link';
          tdTitle.appendChild(a);

          const tdUrl = document.createElement('td');
          tdUrl.textContent = item.url || '';

          const tdSort = document.createElement('td');
          tdSort.textContent = String(item.sort_order ?? 0);

          const tdActions = document.createElement('td');
          tdActions.className = 'actions';
          const editBtn = document.createElement('button');
          editBtn.type = 'button';
          editBtn.className = 'row-btn';
          editBtn.textContent = '编辑';
          editBtn.addEventListener('click', async () => {
            const nextTitle = window.prompt('标题:', item.title || '');
            if (!nextTitle) return;
            const nextUrl = window.prompt('URL:', item.url || '');
            if (!nextUrl) return;
            const nextSortRaw = window.prompt('排序:', String(item.sort_order ?? 0));
            const nextSort = parseInt(nextSortRaw || '0', 10) || 0;
            await apiFetch(`/api/links/quick/${item.id}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ title: nextTitle, url: nextUrl, sort_order: nextSort }),
            });
            loadQuickLinks();
          });
          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'row-btn danger';
          delBtn.textContent = '删除';
          delBtn.addEventListener('click', async () => {
            if (!window.confirm(`删除 "${item.title}"？`)) return;
            await apiFetch(`/api/links/quick/${item.id}`, { method: 'DELETE' });
            loadQuickLinks();
          });
          tdActions.appendChild(editBtn);
          tdActions.appendChild(delBtn);

          tr.appendChild(tdTitle);
          tr.appendChild(tdUrl);
          tr.appendChild(tdSort);
          tr.appendChild(tdActions);
          tbody.appendChild(tr);
        });
      } catch (err) {
        tbody.innerHTML = `<tr><td class="muted" colspan="4">${err.message}</td></tr>`;
      }
    };

    const bindQuickAdd = () => {
      if (!addBtn) return;
      addBtn.addEventListener('click', async () => {
        const title = (titleInput && titleInput.value) || '';
        const url = (urlInput && urlInput.value) || '';
        const sort = parseInt((sortInput && sortInput.value) || '0', 10) || 0;
        if (!title.trim() || !url.trim()) {
          window.alert('请填写标题和 URL');
          return;
        }
        await apiFetch('/api/links/quick', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: title.trim(), url: url.trim(), sort_order: sort }),
        });
        if (titleInput) titleInput.value = '';
        if (urlInput) urlInput.value = '';
        if (sortInput) sortInput.value = '0';
        loadQuickLinks();
      });
    };

    const loadToday = async () => {
      if (!todayCard) return;
      const date = formatDate(new Date());
      todayCard.innerHTML = '<p class="muted">Loading...</p>';
      try {
        const tzOffset = new Date().getTimezoneOffset();
        const data = await apiFetch(`/api/dailyreel/today?date=${date}&tz_offset=${tzOffset}`);
        const scoreboard = Array.isArray(data.scoreboard) ? data.scoreboard : [];
        const totalGit = scoreboard.reduce((sum, r) => sum + (r.git_blog || 0) + (r.git_note || 0), 0);
        const totalClone = scoreboard.reduce((sum, r) => sum + (r.clone || 0), 0);
        const top = scoreboard[0];
        todayCard.innerHTML = `
          <h3>${date}</h3>
          <p class="muted">${totalGit} git · ${totalClone} clone</p>
          <p class="muted">${top ? `第一: ${top.username} (${(top.git_blog || 0) + (top.git_note || 0) + (top.clone || 0)})` : '今天还没有记录。'}</p>
        `;
      } catch (err) {
        todayCard.innerHTML = `<p class="muted">${err.message}</p>`;
      }
    };

    const initWhiteboard = () => {
      if (!whiteboardEl) return;
      const date = formatDate(new Date());
      if (whiteboardDateEl) whiteboardDateEl.textContent = date;

      const cardsById = new Map();
      const stateById = new Map();
      let lastEventId = 0;
      let polling = false;

      const clamp = (n, min, max) => Math.max(min, Math.min(max, n));

      const upsertCard = (card) => {
        if (!card || !card.id) return;
        const id = Number(card.id);
        const existing = stateById.get(id) || {};
        const next = { ...existing, ...card };
        stateById.set(id, next);

        let el = cardsById.get(id);
        if (!el) {
          el = document.createElement('div');
          el.className = 'whiteboard-card';
          el.dataset.cardId = String(id);

          const head = document.createElement('div');
          head.className = 'whiteboard-card__head';
          const title = document.createElement('strong');
          title.textContent = `#${id}`;
          const del = document.createElement('button');
          del.type = 'button';
          del.textContent = '删除';
          del.addEventListener('click', async () => {
            if (!window.confirm('删除这张卡片？')) return;
            await apiFetch(`/api/whiteboard/cards/${id}`, { method: 'DELETE' });
          });
          head.appendChild(title);
          head.appendChild(del);

          const body = document.createElement('div');
          body.className = 'whiteboard-card__body';
          const ta = document.createElement('textarea');
          ta.value = next.text || '';
          ta.addEventListener('input', () => {
            clearTimeout(el._saveTimer);
            el._saveTimer = setTimeout(async () => {
              try {
                await apiFetch(`/api/whiteboard/cards/${id}`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ text: ta.value }),
                });
              } catch (err) {
                // ignore
              }
            }, 300);
          });
          body.appendChild(ta);

          el.appendChild(head);
          el.appendChild(body);
          whiteboardEl.appendChild(el);
          cardsById.set(id, el);

          let dragging = false;
          let startX = 0;
          let startY = 0;
          let originX = 0;
          let originY = 0;

          const onMove = (ev) => {
            if (!dragging) return;
            const dx = ev.clientX - startX;
            const dy = ev.clientY - startY;
            const x = clamp(originX + dx, 0, whiteboardEl.clientWidth - 80);
            const y = clamp(originY + dy, 0, whiteboardEl.clientHeight - 60);
            el.style.transform = `translate(${x}px, ${y}px)`;
            el._pendingPos = { x, y };
          };

          const onUp = async () => {
            if (!dragging) return;
            dragging = false;
            document.removeEventListener('pointermove', onMove);
            document.removeEventListener('pointerup', onUp);
            if (el._pendingPos) {
              const pos = el._pendingPos;
              el._pendingPos = null;
              try {
                await apiFetch(`/api/whiteboard/cards/${id}`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ x: pos.x, y: pos.y }),
                });
              } catch (err) {
                // ignore
              }
            }
          };

          head.addEventListener('pointerdown', (ev) => {
            dragging = true;
            startX = ev.clientX;
            startY = ev.clientY;
            originX = Number(stateById.get(id)?.x || 0);
            originY = Number(stateById.get(id)?.y || 0);
            document.addEventListener('pointermove', onMove);
            document.addEventListener('pointerup', onUp);
          });
        }

        const x = Number(next.x || 0);
        const y = Number(next.y || 0);
        el.style.transform = `translate(${x}px, ${y}px)`;
        const ta = qs('textarea', el);
        if (ta && document.activeElement !== ta) {
          ta.value = next.text || '';
        }
      };

      const removeCard = (id) => {
        const el = cardsById.get(id);
        if (el) el.remove();
        cardsById.delete(id);
        stateById.delete(id);
      };

      const loadBoard = async () => {
        const data = await apiFetch(`/api/whiteboard/board?date=${date}`);
        lastEventId = Number(data.last_event_id || 0);
        const cards = Array.isArray(data.cards) ? data.cards : [];
        cards.forEach((card) => upsertCard(card));
      };

      const poll = async () => {
        if (polling) return;
        polling = true;
        try {
          const data = await apiFetch(`/api/whiteboard/events?date=${date}&after_id=${lastEventId}`);
          const events = Array.isArray(data.events) ? data.events : [];
          events.forEach((ev) => {
            lastEventId = Math.max(lastEventId, Number(ev.id || 0));
            const id = Number(ev.card_id || 0);
            if (!id) return;
            if (ev.type === 'create' && ev.payload && ev.payload.card) {
              upsertCard(ev.payload.card);
            } else if (ev.type === 'update') {
              const changes = (ev.payload && ev.payload.changes) || {};
              upsertCard({ id, ...changes });
            } else if (ev.type === 'delete') {
              removeCard(id);
            }
          });
        } catch (err) {
          // ignore
        } finally {
          polling = false;
        }
      };

      if (whiteboardAddBtn) {
        whiteboardAddBtn.addEventListener('click', async () => {
          const rect = whiteboardEl.getBoundingClientRect();
          const x = Math.round(Math.random() * Math.max(0, rect.width - 280));
          const y = Math.round(Math.random() * Math.max(0, rect.height - 200));
          await apiFetch('/api/whiteboard/cards', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date, x, y, text: '' }),
          });
        });
      }

      loadBoard().then(() => setInterval(poll, 1000));
    };

    if (todayBtn) todayBtn.addEventListener('click', loadToday);
    bindQuickAdd();
    loadQuickLinks();
    loadToday();
    initWhiteboard();
  };

  initNav();

  if (page === 'home') initHome();
  if (page === 'blog') initProjectsPage('blog');
  if (page === 'note') initProjectsPage('note');
  if (page === 'echoes') initEchoes();
  if (page === 'dailyreel') initDailyreel();
})();
