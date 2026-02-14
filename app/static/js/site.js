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
      let payload = null;
      try {
        const data = await resp.json();
        payload = data;
        if (data && data.error) message = data.error;
      } catch (err) {
        // ignore
      }
      const error = new Error(message);
      error.status = resp.status;
      error.payload = payload;
      throw error;
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

  const safeRelPath = (value, fallback = 'file') => {
    const fb = String(fallback || 'file')
      .replace(/\\/g, '/')
      .split('/')
      .slice(-1)[0]
      .trim();
    let raw = String(value || '').replace(/\\/g, '/').trim();
    if (!raw) raw = fb || 'file';
    raw = raw.replace(/^\/+/, '');
    const parts = [];
    for (const seg of raw.split('/')) {
      if (!seg || seg === '.') continue;
      if (seg === '..') {
        if (!parts.length) return null;
        parts.pop();
        continue;
      }
      parts.push(seg);
    }
    let out = parts.join('/');
    if (!out || out === '.' || out === '/') out = fb || 'file';
    if (!out || out.length > 500) return null;
    const check = out.split('/');
    if (check.some((p) => !p || p === '.' || p === '..')) return null;
    return out;
  };

  const isIgnoredRelPath = (relPath) => {
    const raw = String(relPath || '').replace(/\\/g, '/');
    if (!raw) return false;
    const parts = raw.split('/').filter(Boolean);
    const denyDirs = new Set(['.benoss', '.git', '.venv', 'node_modules', '__pycache__']);
    if (parts.some((p) => denyDirs.has(p))) return true;
    const base = parts[parts.length - 1] || '';
    if (base === '.DS_Store') return true;
    if (base.endsWith('.pyc')) return true;
    return false;
  };

  const hexFromArrayBuffer = (buf) =>
    Array.from(new Uint8Array(buf || new ArrayBuffer(0)))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');

  const MAX_SUBTLE_SHA256_BYTES = 32 * 1024 * 1024;

  const sha256Hex = async (blob) => {
    try {
      if (window.DigestStream && blob && typeof blob.stream === 'function') {
        const ds = new DigestStream('SHA-256');
        await blob.stream().pipeTo(ds.writable);
        const digest = await ds.digest;
        return hexFromArrayBuffer(digest);
      }
      if (window.crypto && crypto.subtle && typeof crypto.subtle.digest === 'function') {
        const size = Number((blob && blob.size) || 0);
        if (Number.isFinite(size) && size > MAX_SUBTLE_SHA256_BYTES) return '';
        const ab = await blob.arrayBuffer();
        const digest = await crypto.subtle.digest('SHA-256', ab);
        return hexFromArrayBuffer(digest);
      }
    } catch (err) {
      // Fall back to no-hash mode (we'll just upload).
    }
    return '';
  };

  const ossPut = (url, headers, file, opts = {}) =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', url, true);
      Object.entries(headers || {}).forEach(([k, v]) => {
        if (k && v != null) xhr.setRequestHeader(k, v);
      });
      xhr.upload.onprogress = (event) => {
        if (!opts || typeof opts.onProgress !== 'function') return;
        const total = Number(event.total || file.size || 0);
        const loaded = Number(event.loaded || 0);
        const progress = total > 0 ? loaded / total : 0;
        opts.onProgress(Math.max(0, Math.min(1, progress)));
      };
      xhr.onerror = () => reject(new Error('OSS upload failed (network/CORS)'));
      xhr.onabort = () => reject(new Error('OSS upload aborted'));
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          if (opts && typeof opts.onProgress === 'function') opts.onProgress(1);
          resolve();
          return;
        }
        reject(new Error(`OSS upload failed: ${xhr.status}`));
      };
      xhr.send(file);
    });

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms || 0))));

  const retryAsync = async (fn, retries = 2, baseDelay = 600) => {
    let lastErr = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        // eslint-disable-next-line no-await-in-loop
        return await fn(attempt);
      } catch (err) {
        lastErr = err;
        if (attempt >= retries) break;
        // eslint-disable-next-line no-await-in-loop
        await wait(baseDelay * 2 ** attempt);
      }
    }
    throw lastErr || new Error('retry failed');
  };

  const newIdempotencyKey = (prefix = 'wb') => {
    const rnd =
      window.crypto && typeof window.crypto.randomUUID === 'function'
        ? window.crypto.randomUUID().replace(/-/g, '')
        : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 11)}`;
    return `${prefix}:${rnd}`;
  };

  const directUploadProjectFile = async (projectId, path, file, shaHint = '') => {
    const contentType = (file && file.type) || 'application/octet-stream';
    const init = await apiFetch(`/api/projects/${projectId}/files/upload-direct/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, filename: file.name || 'file', content_type: contentType }),
    });
    const maxBytes = Number(init.max_bytes || 0);
    if (maxBytes > 0 && Number(file.size || 0) > maxBytes) throw new Error('文件太大');
    await ossPut(init.upload_url, init.upload_headers, file);
    const sha256 = shaHint || (await sha256Hex(file));
    return apiFetch(`/api/projects/${projectId}/files/upload-direct/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: init.path || path,
        filename: file.name || 'file',
        content_type: contentType,
        oss_key: init.oss_key,
        sha256,
      }),
    });
  };

  const directUploadPushRequestFile = async (pushRequestId, path, file, shaHint = '') => {
    const contentType = (file && file.type) || 'application/octet-stream';
    const init = await apiFetch(`/api/push-requests/${pushRequestId}/files/upload-direct/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, filename: file.name || 'file', content_type: contentType }),
    });
    const maxBytes = Number(init.max_bytes || 0);
    if (maxBytes > 0 && Number(file.size || 0) > maxBytes) throw new Error('文件太大');
    await ossPut(init.upload_url, init.upload_headers, file);
    const sha256 = shaHint || (await sha256Hex(file));
    return apiFetch(`/api/push-requests/${pushRequestId}/files/upload-direct/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: init.path || path,
        filename: file.name || 'file',
        content_type: contentType,
        oss_key: init.oss_key,
        sha256,
      }),
    });
  };

  const directUploadWhiteboardMedia = async (options) => {
    const opts = options || {};
    const date = opts.date;
    const x = Number(opts.x || 0);
    const y = Number(opts.y || 0);
    const text = String(opts.text || '');
    const file = opts.file;
    const shaHint = opts.shaHint || '';
    const idempotencyKey = String(opts.idempotencyKey || '');
    const entry = opts.entry || {};
    const contentType = (file && file.type) || 'application/octet-stream';
    const init = await apiFetch('/api/whiteboard/cards/upload-direct/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date, filename: file.name || 'file', content_type: contentType }),
    });
    const maxBytes = Number(init.max_bytes || 0);
    if (maxBytes > 0 && Number(file.size || 0) > maxBytes) throw new Error('文件太大');
    await ossPut(init.upload_url, init.upload_headers, file, { onProgress: opts.onProgress });
    const sha256 = shaHint || (await sha256Hex(file));
    return apiFetch('/api/whiteboard/cards/upload-direct/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        date: init.date || date,
        x,
        y,
        text,
        idempotency_key: idempotencyKey,
        entry_date: entry.date || '',
        entry_tags: entry.tags || [],
        entry_mood: entry.mood || '',
        entry_type: entry.type || '',
        filename: file.name || 'file',
        content_type: contentType,
        oss_key: init.oss_key,
        sha256,
      }),
    });
  };

  const MEDIA_EXTS = {
    image: new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg']),
    video: new Set(['.mp4', '.mov', '.webm', '.mkv', '.avi']),
    audio: new Set(['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg']),
    pdf: new Set(['.pdf']),
    text: new Set([
      '.md',
      '.markdown',
      '.txt',
      '.json',
      '.yaml',
      '.yml',
      '.toml',
      '.ini',
      '.csv',
      '.tsv',
      '.log',
      '.py',
      '.js',
      '.ts',
      '.tsx',
      '.jsx',
      '.html',
      '.css',
      '.xml',
      '.sh',
      '.zsh',
      '.bash',
      '.sql',
    ]),
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

  const highlightCodeBlocks = (root) => {
    if (!root || !window.hljs) return;
    qsa('pre code', root).forEach((block) => {
      try {
        window.hljs.highlightElement(block);
      } catch (err) {
        // ignore
      }
    });
  };

  const renderMarkdown = (text, container) => {
    if (!container) return;
    const source = String(text || '');
    if (!window.marked) {
      container.textContent = source;
      return;
    }
    if (!window.DOMPurify) {
      // Fail closed (no HTML rendering) if sanitizer isn't available.
      container.textContent = source;
      return;
    }
    const raw = window.marked.parse(source);
    const safe = window.DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
    container.innerHTML = safe;
    renderMath(container);
    highlightCodeBlocks(container);
  };

  const guessCodeLanguage = (path) => {
    const p = String(path || '').toLowerCase();
    const dot = p.lastIndexOf('.');
    if (dot === -1) return '';
    const ext = p.slice(dot);
    const map = {
      '.py': 'python',
      '.js': 'javascript',
      '.jsx': 'javascript',
      '.ts': 'typescript',
      '.tsx': 'typescript',
      '.json': 'json',
      '.yaml': 'yaml',
      '.yml': 'yaml',
      '.toml': 'toml',
      '.ini': 'ini',
      '.sql': 'sql',
      '.sh': 'bash',
      '.bash': 'bash',
      '.zsh': 'bash',
      '.html': 'xml',
      '.xml': 'xml',
      '.css': 'css',
    };
    return map[ext] || '';
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
    const remoteMetaByPath = new Map();
    files.forEach((f) => {
      const rel = normalizeProjectPath(f && f.path);
      if (!rel) return;
      if (f && f.url) fileUrlByPath.set(rel, f.url);
      remoteMetaByPath.set(rel, { sha256: String((f && f.sha256) || ''), sizeBytes: Number((f && f.size_bytes) || 0) });
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
          const lowerPath = String(path).toLowerCase();
          const isMarkdown = lowerPath.endsWith('.md') || lowerPath.endsWith('.markdown');

          const baseDir = normalizeProjectPath(path).split('/').slice(0, -1).join('/');
          const renderReadOnlyText = (container, sourceText) => {
            if (!container) return;
            container.innerHTML = '';
            if (isMarkdown) {
              const rewritten = rewriteProjectMarkdown(sourceText, fileUrlByPath, baseDir);
              const wrap = document.createElement('div');
              wrap.className = 'markdown-body';
              container.appendChild(wrap);
              renderMarkdown(rewritten, wrap);
              return;
            }
            const pre = document.createElement('pre');
            pre.className = 'code-block';
            const code = document.createElement('code');
            const lang = guessCodeLanguage(path);
            if (lang) code.className = `language-${lang}`;
            code.textContent = sourceText;
            pre.appendChild(code);
            container.appendChild(pre);
            if (lang) highlightCodeBlocks(pre);
          };

          if (!canEdit) {
            renderReadOnlyText(previewEl, text);
            return;
          }

          const toolbar = document.createElement('div');
          toolbar.className = 'button-row text-editor-toolbar';

          const editBtn = document.createElement('button');
          editBtn.type = 'button';
          editBtn.className = 'pill-btn ghost';
          editBtn.textContent = '编辑';

          toolbar.appendChild(editBtn);
          previewEl.appendChild(toolbar);

          const body = document.createElement('div');
          body.className = 'text-editor-body';
          previewEl.appendChild(body);
          renderReadOnlyText(body, text);

          const enterEditMode = (initial) => {
            previewEl.innerHTML = '';

            const editToolbar = document.createElement('div');
            editToolbar.className = 'button-row text-editor-toolbar';

            const saveBtn = document.createElement('button');
            saveBtn.type = 'button';
            saveBtn.className = 'pill-btn';
            saveBtn.textContent = '保存';

            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'pill-btn ghost';
            cancelBtn.textContent = '取消';

            editToolbar.appendChild(saveBtn);
            editToolbar.appendChild(cancelBtn);
            previewEl.appendChild(editToolbar);

            const status = document.createElement('div');
            status.className = 'muted text-editor-status';
            status.textContent = '编辑中...';
            previewEl.appendChild(status);

            const textarea = document.createElement('textarea');
            textarea.className = 'textarea text-editor-input';
            textarea.spellcheck = false;
            textarea.value = String(initial || '');

            const setStatus = (msg) => {
              status.textContent = msg || '';
            };

            const renderMarkdownPreview = (previewContainer, sourceText) => {
              if (!previewContainer) return;
              const rewritten = rewriteProjectMarkdown(sourceText, fileUrlByPath, baseDir);
              renderMarkdown(rewritten, previewContainer);
            };

            let previewWrap = null;
            let previewInner = null;
            let previewTimer = null;

            const mount = document.createElement('div');
            if (isMarkdown) {
              mount.className = 'text-editor-split';
              const left = document.createElement('div');
              left.appendChild(textarea);

              previewWrap = document.createElement('div');
              previewWrap.className = 'text-editor-preview';
              previewInner = document.createElement('div');
              previewInner.className = 'markdown-body';
              previewWrap.appendChild(previewInner);

              mount.appendChild(left);
              mount.appendChild(previewWrap);
              previewEl.appendChild(mount);

              const schedulePreview = () => {
                if (!previewInner) return;
                if (previewTimer) window.clearTimeout(previewTimer);
                previewTimer = window.setTimeout(() => {
                  previewTimer = null;
                  renderMarkdownPreview(previewInner, textarea.value);
                }, 180);
              };
              textarea.addEventListener('input', schedulePreview);
              renderMarkdownPreview(previewInner, textarea.value);
            } else {
              mount.appendChild(textarea);
              previewEl.appendChild(mount);
            }

            const save = async () => {
              setStatus('保存中...');
              saveBtn.disabled = true;
              cancelBtn.disabled = true;
              try {
                await apiFetch(`/api/projects/${projectId}/file/text`, {
                  method: 'PUT',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ path: file.path || '', text: textarea.value }),
                });
                setStatus('已保存，刷新中...');
                await renderProjectWindow(projectId, prefix, scope, {
                  ...(opts || {}),
                  preselectPath: file.path || '',
                });
              } catch (err) {
                setStatus(err.message || '保存失败');
                saveBtn.disabled = false;
                cancelBtn.disabled = false;
              }
            };

            const cancel = () => {
              if (textarea.value !== String(initial || '') && !window.confirm('放弃未保存的修改？')) return;
              previewEl.innerHTML = '';
              previewEl.appendChild(toolbar);
              previewEl.appendChild(body);
              renderReadOnlyText(body, initial);
            };

            saveBtn.addEventListener('click', save);
            cancelBtn.addEventListener('click', cancel);

            textarea.addEventListener('keydown', (event) => {
              const key = String(event.key || '').toLowerCase();
              if ((event.ctrlKey || event.metaKey) && key === 's') {
                event.preventDefault();
                save();
              }
            });

            textarea.focus();
          };

          editBtn.addEventListener('click', () => enterEditMode(text));
        } catch (err) {
          previewEl.innerHTML = `<div class="card placeholder">${err.message}</div>`;
        }
        return;
      }

      const resolvedType = (file.media_type || detectMediaType(file.url || '') || 'file').toLowerCase();
      if (resolvedType === 'pdf') {
        const wrapper = document.createElement('div');
        wrapper.className = 'pdf-embed';
        const iframe = document.createElement('iframe');
        iframe.src = file.url || '';
        iframe.loading = 'lazy';
        iframe.title = path || 'PDF';
        iframe.referrerPolicy = 'no-referrer';
        wrapper.appendChild(iframe);
        previewEl.appendChild(wrapper);
      } else {
        const node = createMediaNode(resolvedType, file.url || '', {
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
              const base = String(prefixPath || '').trim();
              const byPath = new Map();
              let ignored = 0;
              for (const file of selected) {
                const rel = (file.webkitRelativePath || file.name || '').replace(/\\\\/g, '/');
                const fallback = String(file.name || 'file').replace(/\\\\/g, '/').split('/').slice(-1)[0] || 'file';
                const rawPath = `${base || ''}${base && !base.endsWith('/') ? '/' : ''}${rel}`;
                const safePath = safeRelPath(rawPath, fallback);
                if (!safePath) {
                  window.alert(`路径非法: ${rawPath}`);
                  return;
                }
                if (isIgnoredRelPath(safePath)) {
                  ignored += 1;
                  continue;
                }
                byPath.set(safePath, file);
              }

              const entries = Array.from(byPath.entries());
              if (!entries.length) {
                window.alert(`没有可上传的文件（忽略 ${ignored}）`);
                return;
              }

              const toUpload = [];
              let skipped = 0;
              for (const [path, file] of entries) {
                const remote = remoteMetaByPath.get(path);
                let shaHint = '';
                if (remote && remote.sha256) {
                  const remoteSize = Number(remote.sizeBytes || 0);
                  if (Number(file.size || 0) === remoteSize) {
                    shaHint = await sha256Hex(file);
                    if (shaHint && shaHint === remote.sha256) {
                      skipped += 1;
                      continue;
                    }
                  }
                }
                toUpload.push({ path, file, shaHint });
              }

              if (!toUpload.length) {
                window.alert(`没有检测到变更（跳过 ${skipped}，忽略 ${ignored}）`);
                return;
              }

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

              let uploaded = 0;
              let serverSkipped = 0;
              for (const item of toUpload) {
                const resp = await directUploadPushRequestFile(prId, item.path, item.file, item.shaHint || '');
                if (resp && resp.skipped) serverSkipped += 1;
                else uploaded += 1;
              }

              window.alert(
                `已发送 push 请求 #${prId}：上传 ${uploaded}，跳过 ${skipped + serverSkipped}，忽略 ${ignored}。项目拥有者可在 Control Room 中同意。`
              );
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

          const actions = document.createElement('div');
          actions.className = 'stack-actions';

          const openBtn = document.createElement('button');
          openBtn.type = 'button';
          openBtn.className = 'row-btn';
          openBtn.textContent = '打开';
          openBtn.addEventListener('click', () => showPreview(file));
          actions.appendChild(openBtn);

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
            actions.appendChild(delBtn);
          }

          row.appendChild(actions);
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
        const base = String(prefixValue || '').trim();
        const byPath = new Map();
        let ignored = 0;
        for (const file of selected) {
          const rel = (file.webkitRelativePath || file.name || '').replace(/\\\\/g, '/');
          const fallback = String(file.name || 'file').replace(/\\\\/g, '/').split('/').slice(-1)[0] || 'file';
          const rawPath = `${base || ''}${base && !base.endsWith('/') ? '/' : ''}${rel}`;
          const safePath = safeRelPath(rawPath, fallback);
          if (!safePath) {
            setUploadStatus(`路径非法: ${rawPath}`);
            return;
          }
          if (isIgnoredRelPath(safePath)) {
            ignored += 1;
            continue;
          }
          byPath.set(safePath, file);
        }

        const entries = Array.from(byPath.entries());
        if (!entries.length) {
          setUploadStatus(`没有可上传的文件（忽略 ${ignored}）`);
          return;
        }

        setUploadStatus('准备上传...');
        try {
          let uploaded = 0;
          let skipped = 0;
          for (let i = 0; i < entries.length; i += 1) {
            const [path, file] = entries[i];
            const remote = remoteMetaByPath.get(path);
            let shaHint = '';
            if (remote && remote.sha256) {
              const remoteSize = Number(remote.sizeBytes || 0);
              if (Number(file.size || 0) === remoteSize) {
                setUploadStatus(`校验 ${i + 1}/${entries.length}: ${path}`);
                shaHint = await sha256Hex(file);
                if (shaHint && shaHint === remote.sha256) {
                  skipped += 1;
                  continue;
                }
              }
            }

            setUploadStatus(`上传 ${i + 1}/${entries.length}: ${path}`);
            const resp = await directUploadProjectFile(projectId, path, file, shaHint || '');
            if (resp && resp.skipped) skipped += 1;
            else uploaded += 1;
          }
          setUploadStatus(`完成：上传 ${uploaded}，跳过 ${skipped}，忽略 ${ignored}`);
          if (uploadInput) uploadInput.value = '';
          if (uploadFolder) uploadFolder.value = '';
          await renderProjectWindow(projectId, prefix, scope, opts);
        } catch (err) {
          setUploadStatus(err.message);
        }
      };
    }

    const preselect = opts && opts.preselectPath ? normalizeProjectPath(opts.preselectPath) : '';
    let previewShown = false;
    if (preselect) {
      const target = files.find((f) => normalizeProjectPath(f.path) === preselect);
      if (target) {
        previewShown = true;
        showPreview(target);
      }
    }
    if (!previewShown && previewEl) {
      if (previewTitle) previewTitle.textContent = '预览';
      if (previewMeta) previewMeta.textContent = '';
      previewEl.innerHTML = files.length ? '<p class="muted">选择文件进行预览。</p>' : '<p class="muted">No files to preview.</p>';
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
	      if (modalContent) {
	        try {
	          if (typeof modalContent._cleanup === 'function') modalContent._cleanup();
	        } catch (err) {
	          // ignore
	        }
	        modalContent._cleanup = null;
	        modalContent.innerHTML = '';
	      }
	    };

    const openProjectModal = async (item) => {
      if (!modal || !modalContent) return;
      const template = projectTemplate;
      const prefix = 'modal-project';
      if (!template) {
        showModalMessage('No preview available.');
        return;
      }
      modalContent.innerHTML = '';
      modalContent.appendChild(template.content.cloneNode(true));
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

	    const openWhiteboardModal = async (item) => {
	      if (!modalContent) return;
	      if (typeof modalContent._cleanup === 'function') {
	        try {
	          modalContent._cleanup();
	        } catch (err) {
	          // ignore
	        }
	      }
	      modalContent._cleanup = null;
	      modalContent.innerHTML = '<div class="card placeholder">Loading...</div>';
	      try {
	        const wb = (item && item.whiteboard) || {};
	        const focusCardId = Number(wb.card_id || 0);
	        const isValidDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || '').trim());
	        let dateLabel = String(wb.date || '').trim();
	        if (!isValidDate(dateLabel) && focusCardId) {
	          const cardData = await apiFetch(`/api/whiteboard/cards/${focusCardId}`);
	          dateLabel = String((cardData && cardData.card && cardData.card.date) || '').trim();
	        }
	        if (!isValidDate(dateLabel)) {
	          showModalMessage('Missing whiteboard date.');
	          return;
	        }

	        const aborter = new AbortController();
	        const { signal } = aborter;
	        modalContent._cleanup = () => aborter.abort();

	        const cardsById = new Map();
	        const stateById = new Map();
	        let links = [];

	        const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
	        const camera = { x: 0, y: 0, scale: 1 };

	        const shell = document.createElement('div');
	        shell.className = 'whiteboard-shell whiteboard-shell--modal';

	        const head = document.createElement('div');
	        head.className = 'whiteboard-head';

	        const dateEl = document.createElement('div');
	        dateEl.className = 'muted';
	        dateEl.textContent = dateLabel;

	        const actions = document.createElement('div');
	        actions.className = 'button-row';

	        const resetBtn = document.createElement('button');
	        resetBtn.type = 'button';
	        resetBtn.className = 'pill-btn ghost';
	        resetBtn.textContent = '重置视图';

	        const refreshBtn = document.createElement('button');
	        refreshBtn.type = 'button';
	        refreshBtn.className = 'pill-btn ghost';
	        refreshBtn.textContent = '刷新';

	        const openBoard = document.createElement('a');
	        openBoard.className = 'pill-btn';
	        openBoard.href = `/whiteboard?date=${encodeURIComponent(dateLabel)}${focusCardId ? `#whiteboard-card-${focusCardId}` : ''}`;
	        openBoard.target = '_blank';
	        openBoard.rel = 'noopener';
	        openBoard.textContent = '打开白板';

	        const snapshotBtn = document.createElement('button');
	        snapshotBtn.type = 'button';
	        snapshotBtn.className = 'pill-btn ghost';
	        snapshotBtn.textContent = 'JSON快照';

	        actions.appendChild(resetBtn);
	        actions.appendChild(refreshBtn);
	        actions.appendChild(openBoard);
	        actions.appendChild(snapshotBtn);
	        head.appendChild(dateEl);
	        head.appendChild(actions);
	        shell.appendChild(head);

	        const hint = document.createElement('div');
	        hint.className = 'whiteboard-hint muted';
	        hint.textContent = '拖动空白处平移 · 滚轮平移 · Ctrl/触控板捏合缩放 · 仅查看（不可编辑）';
	        shell.appendChild(hint);

	        const whiteboardEl = document.createElement('div');
	        whiteboardEl.className = 'whiteboard whiteboard--modal';
	        const whiteboardWorldEl = document.createElement('div');
	        whiteboardWorldEl.className = 'whiteboard-world';
	        whiteboardEl.appendChild(whiteboardWorldEl);
	        shell.appendChild(whiteboardEl);

	        modalContent.innerHTML = '';
	        modalContent.appendChild(shell);

	        let linksCanvas = null;
	        let linksCtx = null;
	        let drawPending = false;

	        const getAccent = () => {
	          const value = window.getComputedStyle(document.documentElement).getPropertyValue('--accent');
	          return (value && value.trim()) || '#0f6b5d';
	        };

	        const ensureLinksCanvas = () => {
	          if (linksCanvas) return;
	          linksCanvas = document.createElement('canvas');
	          linksCanvas.className = 'whiteboard-links';
	          whiteboardEl.insertBefore(linksCanvas, whiteboardWorldEl);
	          linksCtx = linksCanvas.getContext('2d');
	        };

	        const resizeLinksCanvas = () => {
	          ensureLinksCanvas();
	          if (!linksCanvas || !linksCtx) return;
	          const dpr = window.devicePixelRatio || 1;
	          const w = Math.max(1, whiteboardEl.clientWidth || 1);
	          const h = Math.max(1, whiteboardEl.clientHeight || 1);
	          const pw = Math.floor(w * dpr);
	          const ph = Math.floor(h * dpr);
	          if (linksCanvas.width !== pw || linksCanvas.height !== ph) {
	            linksCanvas.width = pw;
	            linksCanvas.height = ph;
	            linksCanvas.style.width = `${w}px`;
	            linksCanvas.style.height = `${h}px`;
	          }
	          linksCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
	        };

	        const pointOnRectEdge = (cx, cy, hw, hh, dx, dy) => {
	          const absDx = Math.abs(dx);
	          const absDy = Math.abs(dy);
	          if (hw <= 0 || hh <= 0 || (absDx === 0 && absDy === 0)) return { x: cx, y: cy };
	          const scale = 1 / Math.max(absDx / hw, absDy / hh);
	          return { x: cx + dx * scale, y: cy + dy * scale };
	        };

	        const drawArrow = (ctx, fromRect, toRect, accent) => {
	          const fromCx = fromRect.x + fromRect.w / 2;
	          const fromCy = fromRect.y + fromRect.h / 2;
	          const toCx = toRect.x + toRect.w / 2;
	          const toCy = toRect.y + toRect.h / 2;
	          const dx = toCx - fromCx;
	          const dy = toCy - fromCy;
	          const dist = Math.hypot(dx, dy);
	          if (!Number.isFinite(dist) || dist < 5) return;

	          const start = pointOnRectEdge(fromCx, fromCy, fromRect.w / 2, fromRect.h / 2, dx, dy);
	          const end = pointOnRectEdge(toCx, toCy, toRect.w / 2, toRect.h / 2, -dx, -dy);

	          ctx.save();
	          ctx.globalAlpha = 0.6;
	          ctx.strokeStyle = accent;
	          ctx.fillStyle = accent;
	          ctx.lineWidth = 2;
	          ctx.lineCap = 'round';

	          ctx.beginPath();
	          ctx.moveTo(start.x, start.y);
	          ctx.lineTo(end.x, end.y);
	          ctx.stroke();

	          const ux = (end.x - start.x) / dist;
	          const uy = (end.y - start.y) / dist;
	          const arrowLen = 11;
	          const arrowW = 7;
	          const bx = end.x - ux * arrowLen;
	          const by = end.y - uy * arrowLen;
	          const px = -uy;
	          const py = ux;

	          ctx.beginPath();
	          ctx.moveTo(end.x, end.y);
	          ctx.lineTo(bx + px * arrowW, by + py * arrowW);
	          ctx.lineTo(bx - px * arrowW, by - py * arrowW);
	          ctx.closePath();
	          ctx.fill();
	          ctx.restore();
	        };

	        const drawLinks = () => {
	          ensureLinksCanvas();
	          if (!linksCanvas || !linksCtx) return;
	          resizeLinksCanvas();
	          const boardRect = whiteboardEl.getBoundingClientRect();
	          linksCtx.clearRect(0, 0, whiteboardEl.clientWidth || 1, whiteboardEl.clientHeight || 1);
	          const accent = getAccent();
	          (Array.isArray(links) ? links : []).forEach((link) => {
	            const fromId = Number(link.from_id || 0);
	            const toId = Number(link.to_id || 0);
	            if (!fromId || !toId) return;
	            const fromEl = cardsById.get(fromId);
	            const toEl = cardsById.get(toId);
	            if (!fromEl || !toEl) return;
	            const fr = fromEl.getBoundingClientRect();
	            const tr = toEl.getBoundingClientRect();
	            const fromRect = { x: fr.left - boardRect.left, y: fr.top - boardRect.top, w: fr.width, h: fr.height };
	            const toRect = { x: tr.left - boardRect.left, y: tr.top - boardRect.top, w: tr.width, h: tr.height };
	            drawArrow(linksCtx, fromRect, toRect, accent);
	          });
	        };

	        const scheduleDraw = () => {
	          if (drawPending) return;
	          drawPending = true;
	          window.requestAnimationFrame(() => {
	            drawPending = false;
	            if (signal.aborted) return;
	            drawLinks();
	          });
	        };

	        const applyCamera = () => {
	          whiteboardWorldEl.style.transform = `translate(${camera.x}px, ${camera.y}px) scale(${camera.scale})`;
	          scheduleDraw();
	        };

	        const centerOnWorld = (wx, wy, scale = camera.scale) => {
	          const rect = whiteboardEl.getBoundingClientRect();
	          camera.scale = scale;
	          camera.x = rect.width / 2 - wx * camera.scale;
	          camera.y = rect.height / 2 - wy * camera.scale;
	          applyCamera();
	        };

	        let highlightTimer = 0;
	        modalContent._cleanup = () => {
	          aborter.abort();
	          clearTimeout(highlightTimer);
	        };
	        const focusCard = (id) => {
	          const st = stateById.get(Number(id));
	          if (!st) return false;
	          const x = Number(st.x || 0);
	          const y = Number(st.y || 0);
	          centerOnWorld(x + 130, y + 110, camera.scale);
	          const el = cardsById.get(Number(id));
	          if (el) {
	            el.classList.add('is-highlight');
	            clearTimeout(highlightTimer);
	            highlightTimer = setTimeout(() => el.classList.remove('is-highlight'), 1200);
	          }
	          return true;
	        };

	        const resetView = () => {
	          if (!stateById.size) {
	            camera.x = 0;
	            camera.y = 0;
	            camera.scale = 1;
	            applyCamera();
	            return;
	          }
	          let minX = Infinity;
	          let minY = Infinity;
	          let maxX = -Infinity;
	          let maxY = -Infinity;
	          stateById.forEach((c) => {
	            const x = Number(c.x || 0);
	            const y = Number(c.y || 0);
	            minX = Math.min(minX, x);
	            minY = Math.min(minY, y);
	            maxX = Math.max(maxX, x + 260);
	            maxY = Math.max(maxY, y + 220);
	          });
	          const cx = (minX + maxX) / 2;
	          const cy = (minY + maxY) / 2;
	          centerOnWorld(cx, cy, 1);
	        };

	        const renderAttachments = (container, atts) => {
	          if (!container) return;
	          container.innerHTML = '';
	          const list = Array.isArray(atts) ? atts : [];
	          if (!list.length) return;
	          list.forEach((att) => {
	            const media = createMediaNode(att.media_type, att.url || '', {
	              title: att.filename || '',
	              alt: att.filename || '',
	              preview: att.preview_url || '',
	            });
	            if (media) {
	              const scheduleOnce = () => scheduleDraw();
	              const tag = String(media.tagName || '').toLowerCase();
	              if (tag === 'img') media.addEventListener('load', scheduleOnce, { once: true });
	              if (tag === 'video' || tag === 'audio') media.addEventListener('loadedmetadata', scheduleOnce, { once: true });
	              if (media.querySelector) {
	                const innerImg = media.querySelector('img');
	                if (innerImg) innerImg.addEventListener('load', scheduleOnce, { once: true });
	                const innerMedia = media.querySelector('video,audio');
	                if (innerMedia) innerMedia.addEventListener('loadedmetadata', scheduleOnce, { once: true });
	              }
	              if (att.url && att.media_type === 'image') {
	                const a = document.createElement('a');
	                a.href = att.url;
	                a.target = '_blank';
	                a.rel = 'noopener';
	                a.appendChild(media);
	                container.appendChild(a);
	              } else {
	                container.appendChild(media);
	              }
	            } else if (att.url) {
	              const a = document.createElement('a');
	              a.href = att.url;
	              a.target = '_blank';
	              a.rel = 'noopener';
	              a.className = 'text-link';
	              a.textContent = att.filename || 'file';
	              container.appendChild(a);
	            }

	            const meta = document.createElement('div');
	            meta.className = 'whiteboard-attachment__meta';
	            meta.textContent = `${att.filename || 'file'} · ${att.media_type || 'file'} · ${formatBytes(att.size_bytes || 0)}`;
	            container.appendChild(meta);
	          });
	        };

	        const renderCard = (card) => {
	          if (!card || !card.id) return;
	          const id = Number(card.id);
	          stateById.set(id, card);
	          let el = cardsById.get(id);
	          if (!el) {
	            el = document.createElement('div');
	            el.className = 'whiteboard-card';
	            el.dataset.cardId = String(id);
	            el.id = `whiteboard-card-${id}`;

	            const head = document.createElement('div');
	            head.className = 'whiteboard-card__head';
	            const title = document.createElement('strong');
	            title.textContent = `#${id}`;
	            head.appendChild(title);
	            el.appendChild(head);

	            const body = document.createElement('div');
	            body.className = 'whiteboard-card__body';
	            const attachments = document.createElement('div');
	            attachments.className = 'whiteboard-attachments';
	            body.appendChild(attachments);
	            el._attachmentsEl = attachments;

	            const textBox = document.createElement('div');
	            textBox.className = 'whiteboard-card__text';
	            body.appendChild(textBox);
	            el._textEl = textBox;

	            el.appendChild(body);
	            whiteboardWorldEl.appendChild(el);
	            cardsById.set(id, el);
	          }

	          const x = Number(card.x || 0);
	          const y = Number(card.y || 0);
	          el.style.transform = `translate(${x}px, ${y}px)`;
	          if (el._attachmentsEl) renderAttachments(el._attachmentsEl, card.attachments || []);
	          const text = String(card.text || '');
	          if (el._textEl) {
	            const trimmed = text.trim();
	            if (!trimmed) {
	              el._textEl.textContent = '';
	              el._textEl.hidden = true;
	            } else {
	              el._textEl.hidden = false;
	              el._textEl.textContent = text;
	            }
	          }
	        };

	        const renderBoard = (data) => {
	          cardsById.clear();
	          stateById.clear();
	          whiteboardWorldEl.innerHTML = '';
	          const cards = Array.isArray(data.cards) ? data.cards : [];
	          links = Array.isArray(data.links) ? data.links : [];
	          cards.forEach((c) => renderCard(c));
	          applyCamera();
	          // draw once after layout settles
	          window.requestAnimationFrame(() => scheduleDraw());
	        };

	        const loadBoard = async (opts = {}) => {
	          const { focus = true } = opts;
	          const data = await apiFetch(`/api/whiteboard/board?date=${encodeURIComponent(dateLabel)}`);
	          if (signal.aborted) return;
	          renderBoard(data || {});
	          if (focus && focusCardId) {
	            if (!focusCard(focusCardId)) resetView();
	          } else {
	            resetView();
	          }
	        };

	        resetBtn.addEventListener('click', () => resetView(), { signal });
	        refreshBtn.addEventListener('click', () => loadBoard({ focus: false }), { signal });
	        snapshotBtn.addEventListener(
	          'click',
	          async () => {
	            try {
	              const data = await apiFetch(`/api/whiteboard/snapshot?date=${encodeURIComponent(dateLabel)}`);
	              if (signal.aborted) return;
	              const url = data && data.url;
	              if (url) window.open(url, '_blank', 'noopener');
	            } catch (err) {
	              window.alert(err.message);
	            }
	          },
	          { signal }
	        );

	        let panning = false;
	        let panStartX = 0;
	        let panStartY = 0;
	        let panOriginX = 0;
	        let panOriginY = 0;

	        const endPan = () => {
	          if (!panning) return;
	          panning = false;
	          whiteboardEl.classList.remove('is-panning');
	        };

	        whiteboardEl.addEventListener(
	          'pointerdown',
	          (ev) => {
	            if (!ev.isPrimary) return;
	            if (ev.button != null && ev.button !== 0) return;
	            if (ev.target && ev.target.closest && ev.target.closest('.whiteboard-card')) return;
	            panning = true;
	            panStartX = ev.clientX;
	            panStartY = ev.clientY;
	            panOriginX = camera.x;
	            panOriginY = camera.y;
	            whiteboardEl.classList.add('is-panning');
	            try {
	              whiteboardEl.setPointerCapture(ev.pointerId);
	            } catch (err) {
	              // ignore
	            }
	          },
	          { signal }
	        );

	        whiteboardEl.addEventListener(
	          'pointermove',
	          (ev) => {
	            if (!panning) return;
	            const dx = ev.clientX - panStartX;
	            const dy = ev.clientY - panStartY;
	            camera.x = panOriginX + dx;
	            camera.y = panOriginY + dy;
	            applyCamera();
	          },
	          { signal }
	        );

	        whiteboardEl.addEventListener('pointerup', endPan, { signal });
	        whiteboardEl.addEventListener('pointercancel', endPan, { signal });

	        whiteboardEl.addEventListener(
	          'wheel',
	          (ev) => {
	            const overCard = ev.target && ev.target.closest && ev.target.closest('.whiteboard-card');
	            const rect = whiteboardEl.getBoundingClientRect();
	            const sx = ev.clientX - rect.left;
	            const sy = ev.clientY - rect.top;
	            if (ev.ctrlKey || ev.metaKey) {
	              ev.preventDefault();
	              const zoomIntensity = 0.0018;
	              const nextScale = clamp(camera.scale * (1 - ev.deltaY * zoomIntensity), 0.2, 3);
	              const wx = (sx - camera.x) / camera.scale;
	              const wy = (sy - camera.y) / camera.scale;
	              camera.scale = nextScale;
	              camera.x = sx - wx * camera.scale;
	              camera.y = sy - wy * camera.scale;
	              applyCamera();
	            } else {
	              if (overCard) return;
	              ev.preventDefault();
	              camera.x -= ev.deltaX;
	              camera.y -= ev.deltaY;
	              applyCamera();
	            }
	          },
	          { passive: false, signal }
	        );

	        window.addEventListener('resize', () => scheduleDraw(), { signal });

	        applyCamera();
	        await loadBoard({ focus: true });
	      } catch (err) {
	        showModalMessage(err.message);
	      }
	    };

    const openModal = async (item) => {
      if (!modal || !modalContent) return;
      modal.hidden = false;
      document.body.classList.add('modal-open');
      const source = (item && item.source) || ((item && item.whiteboard) ? 'whiteboard' : 'project');
      if (source === 'whiteboard') return openWhiteboardModal(item);
      return openProjectModal(item);
    };

    const buildCard = (item) => {
      const wrapper = document.createElement('button');
      wrapper.type = 'button';
      wrapper.className = 'album-item';

      const card = document.createElement('article');
      card.className = 'album-card';
      const preview = document.createElement('div');
      preview.className = 'album-preview';

      const source = item.source || (item.whiteboard ? 'whiteboard' : 'project');
      const displayName = item.path || item.filename || '';
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
      moduleBadge.textContent = source === 'whiteboard' ? 'whiteboard' : item.project?.module || 'project';
      badges.appendChild(moduleBadge);
      preview.appendChild(badges);

      card.appendChild(preview);

      const body = document.createElement('div');
      body.className = 'album-body';
      const title = document.createElement('strong');
      title.textContent = displayName || 'file';
      const meta = document.createElement('div');
      meta.className = 'album-meta';
      if (source === 'whiteboard') {
        const wb = item.whiteboard || {};
        meta.textContent = ['Whiteboard', wb.date || '', wb.card_id ? `#${wb.card_id}` : ''].filter(Boolean).join('  ·  ');
      } else {
        meta.textContent = [item.project?.title || '', item.project?.owner_username ? `@${item.project.owner_username}` : '']
          .filter(Boolean)
          .join('  ·  ');
      }
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

  const initDailyreal = () => {
    const refreshBtn = qs('[data-dailyreal-refresh]');
    const summaryBtn = qs('[data-dailyreal-summary]');
    const summaryGenerateBtn = qs('[data-dailyreal-summary-generate]');
    const summaryCopyBtn = qs('[data-dailyreal-summary-copy]');
    const summaryEl = qs('#dailyreal-summary');
    const dateEl = qs('#dailyreal-date');
    const tableBody = qs('#dailyreal-scoreboard tbody');
    const feed = qs('#dailyreal-feed');

    const calendarGrid = qs('#dailyreal-calendar');
    const monthTitle = qs('#dailyreal-month');
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

    const loadSummary = async (date) => {
      if (!summaryEl || !isValidDate(date)) return;
      summaryEl.textContent = '生成中...';
      try {
        const data = await apiFetch(`/api/dailyreal/summary?date=${date}&tz_offset=${tzOffset()}`);
        summaryEl.textContent = String((data && data.summary) || '').trim() || '今天还没有可汇总的数据。';
      } catch (err) {
        summaryEl.textContent = err.message || '生成失败';
      }
    };

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
	      if (tableBody) tableBody.innerHTML = '<tr><td class="muted" colspan="7">Loading...</td></tr>';
	      if (feed) feed.innerHTML = '<div class="card placeholder">Loading...</div>';

      try {
        const data = await apiFetch(`/api/dailyreal/today?date=${date}&tz_offset=${tzOffset()}`);
        const scoreboard = Array.isArray(data.scoreboard) ? data.scoreboard : [];
        const events = Array.isArray(data.events) ? data.events : [];

        if (dateEl) dateEl.textContent = data.date || date;

	        if (tableBody) {
	          tableBody.innerHTML = '';
	          if (!scoreboard.length) {
	            tableBody.innerHTML = '<tr><td class="muted" colspan="7">今天还没有记录。</td></tr>';
	          } else {
	            scoreboard.forEach((row) => {
	              const tr = document.createElement('tr');
	              const total =
	                (row.git_blog || 0) + (row.git_note || 0) + (row.push || 0) + (row.clone || 0) + (row.whiteboard || 0);
	              tr.innerHTML = `
	                <td>${row.username || ''}</td>
	                <td>${row.git_blog || 0}</td>
	                <td>${row.git_note || 0}</td>
	                <td>${row.push || 0}</td>
	                <td>${row.clone || 0}</td>
	                <td>${row.whiteboard || 0}</td>
	                <td><strong>${total}</strong></td>
	              `;
	              tableBody.appendChild(tr);
	            });
	          }
	        }

	        if (feed) {
	          feed.innerHTML = '';
	          if (!events.length) {
	            feed.innerHTML = '<div class="card placeholder">今天还没有记录。</div>';
	          } else {
	            events.forEach((ev) => {
	              const row = document.createElement('div');
	              row.className = 'stack-item';
	              const left = document.createElement('div');
	              const strong = document.createElement('strong');
	              const type = ev.type || '';
	              const module = ev.module || ev.project?.module || '';
	              const actor = ev.actor?.username || '';
	              const wbDate = ev.whiteboard?.date || '';
	              const wbCardId = ev.whiteboard?.card_id || '';
	              const projTitle = ev.project?.title || (wbDate ? `Whiteboard · ${wbDate}${wbCardId ? ` · #${wbCardId}` : ''}` : '');
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

              const actions = document.createElement('div');
              actions.className = 'stack-actions';

              if (ev.whiteboard && wbDate) {
                const a = document.createElement('a');
                a.className = 'text-link';
                a.href = `/whiteboard?date=${encodeURIComponent(wbDate)}${wbCardId ? `#whiteboard-card-${wbCardId}` : ''}`;
                a.textContent = '打开';
                actions.appendChild(a);
              } else if (ev.project && ev.project.id) {
                const a = document.createElement('a');
                a.className = 'text-link';
                a.href = `/${ev.project.module || module || 'blog'}?project=${ev.project.id}`;
                a.textContent = '打开';
                actions.appendChild(a);
              }

              if (actions.childNodes.length) row.appendChild(actions);
              feed.appendChild(row);
            });
          }
        }
	      } catch (err) {
	        if (tableBody) tableBody.innerHTML = `<tr><td class="muted" colspan="7">${err.message}</td></tr>`;
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
        const data = await apiFetch(`/api/dailyreal/month?month=${month}&tz_offset=${tzOffset()}`);
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
	        const pushTotal = info ? Number(info.push || 0) : 0;
	        const cloneTotal = info ? Number(info.clone || 0) : 0;
	        const wbTotal = info ? Number(info.whiteboard || 0) : 0;
	        const total = gitTotal + pushTotal + cloneTotal + wbTotal;
	        if (total > 0) btn.classList.add('has-entry');

	        if (total > 0) {
	          const count = document.createElement('span');
	          count.className = 'calendar-day__count';
	          count.textContent = String(total);
	          if (info) {
	            count.title = `git: ${gitTotal} (blog ${Number(info.git_blog || 0)}, note ${Number(
	              info.git_note || 0
	            )}) · push: ${pushTotal} · clone: ${cloneTotal} · whiteboard: ${wbTotal}`;
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
    if (summaryBtn) summaryBtn.addEventListener('click', () => loadSummary(selectedDate));
    if (summaryGenerateBtn) summaryGenerateBtn.addEventListener('click', () => loadSummary(selectedDate));
    if (summaryCopyBtn) {
      summaryCopyBtn.addEventListener('click', async () => {
        if (!summaryEl) return;
        const text = String(summaryEl.textContent || '').trim();
        if (!text) return;
        try {
          await navigator.clipboard.writeText(text);
          summaryCopyBtn.textContent = '已复制';
          setTimeout(() => {
            summaryCopyBtn.textContent = '复制';
          }, 1400);
        } catch (err) {
          window.alert('复制失败，请手动复制。');
        }
      });
    }
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
    loadSummary(selectedDate);
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
    const whiteboardSummaryBtn = qs('[data-whiteboard-summary]');
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
	        const data = await apiFetch(`/api/dailyreal/today?date=${date}&tz_offset=${tzOffset}`);
	        const scoreboard = Array.isArray(data.scoreboard) ? data.scoreboard : [];
	        const totalGit = scoreboard.reduce((sum, r) => sum + (r.git_blog || 0) + (r.git_note || 0), 0);
	        const totalPush = scoreboard.reduce((sum, r) => sum + (r.push || 0), 0);
	        const totalClone = scoreboard.reduce((sum, r) => sum + (r.clone || 0), 0);
	        const totalWb = scoreboard.reduce((sum, r) => sum + (r.whiteboard || 0), 0);
	        const top = scoreboard[0];
	        todayCard.innerHTML = `
	          <h3>${date}</h3>
	          <p class="muted">${totalGit} git · ${totalPush} push · ${totalClone} clone · ${totalWb} whiteboard</p>
	          <p class="muted">${top ? `第一: ${top.username} (${(top.git_blog || 0) + (top.git_note || 0) + (top.push || 0) + (top.clone || 0) + (top.whiteboard || 0)})` : '今天还没有记录。'}</p>
	        `;
	      } catch (err) {
	        todayCard.innerHTML = `<p class="muted">${err.message}</p>`;
	      }
	    };

    const initWhiteboard = () => {
      if (!whiteboardEl || !whiteboardWorldEl) return;

      const whiteboardShell = whiteboardEl.closest('.whiteboard-shell');
      const whiteboardSyncStateEl = qs('#whiteboard-sync-state');
      const quickTextEl = qs('#whiteboard-quick-text');
      const quickTagsEl = qs('#whiteboard-quick-tags');
      const quickMoodEl = qs('#whiteboard-quick-mood');
      const quickTypeEl = qs('#whiteboard-quick-type');
      const quickSendBtn = qs('[data-whiteboard-quick-send]');
      const queueStatusEl = qs('#whiteboard-queue-status');
      const filterTagEl = qs('#whiteboard-filter-tag');
      const filterMoodEl = qs('#whiteboard-filter-mood');
      const filterTypeEl = qs('#whiteboard-filter-type');
      const filterClearBtn = qs('[data-whiteboard-filter-clear]');
      const mobileQuickBtn = qs('[data-whiteboard-mobile-quick]');

      const isValidDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || ''));
      const params = new URLSearchParams(window.location.search);
      let date = formatDate(new Date());
      const urlDate = params.get('date');
      if (isValidDate(urlDate)) date = urlDate;
      if (whiteboardDateEl) whiteboardDateEl.textContent = date;

      const cardsById = new Map();
      const stateById = new Map();
      const linksById = new Map();
      let linkingFromId = 0;
      let syncCursor = '';
      let polling = false;
      let queueBusy = false;
      let uploadProgress = 0;
      let uploadMessage = '';
      const filterState = { tag: '', mood: '', type: '' };

      const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
      const textQueueKey = `benoss:whiteboard:text-queue:${date}`;
      const readQueue = () => {
        try {
          const raw = window.localStorage.getItem(textQueueKey);
          const data = raw ? JSON.parse(raw) : [];
          return Array.isArray(data) ? data : [];
        } catch (err) {
          return [];
        }
      };
      const saveQueue = (items) => {
        try {
          window.localStorage.setItem(textQueueKey, JSON.stringify(Array.isArray(items) ? items : []));
        } catch (err) {
          // ignore
        }
      };
      let textQueue = readQueue();

      const camera = { x: 0, y: 0, scale: 1 };
      let linksCanvas = null;
      let linksCtx = null;
      let drawPending = false;

      const setSyncState = (text) => {
        if (!whiteboardSyncStateEl) return;
        whiteboardSyncStateEl.textContent = text;
      };

      const updateQueueStatus = () => {
        if (!queueStatusEl) return;
        const parts = [];
        if (!navigator.onLine) parts.push('离线模式');
        if (textQueue.length) parts.push(`待同步 ${textQueue.length} 条`);
        if (queueBusy) parts.push('同步中...');
        if (uploadMessage) parts.push(uploadMessage);
        queueStatusEl.textContent = parts.join(' · ') || '在线';
      };

      const parseTags = (value) =>
        String(value || '')
          .split(/[,\n，;；]/g)
          .map((v) => v.trim().replace(/^#/, ''))
          .filter(Boolean)
          .slice(0, 12);

      const quickEntry = (defaultType = 'note') => ({
        date,
        tags: parseTags(quickTagsEl ? quickTagsEl.value : ''),
        mood: quickMoodEl ? String(quickMoodEl.value || '').trim() : '',
        type: (quickTypeEl ? String(quickTypeEl.value || '').trim() : '') || defaultType,
      });

      const formatClock = (value) => {
        if (!value) return '';
        const dt = new Date(value);
        if (Number.isNaN(dt.getTime())) return '';
        return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      };

      const withTimePrefix = (text) => {
        const raw = String(text || '').trim();
        if (!raw) return '';
        if (/^\[\d{2}:\d{2}\]\s*/.test(raw)) return raw;
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, '0');
        const mm = String(now.getMinutes()).padStart(2, '0');
        return `[${hh}:${mm}] ${raw}`;
      };

      const getAccent = () => {
        const value = window.getComputedStyle(document.documentElement).getPropertyValue('--accent');
        return (value && value.trim()) || '#0f6b5d';
      };

      const ensureLinksCanvas = () => {
        if (linksCanvas) return;
        linksCanvas = document.createElement('canvas');
        linksCanvas.className = 'whiteboard-links';
        whiteboardEl.insertBefore(linksCanvas, whiteboardWorldEl);
        linksCtx = linksCanvas.getContext('2d');
      };

      const resizeLinksCanvas = () => {
        ensureLinksCanvas();
        if (!linksCanvas || !linksCtx) return;
        const dpr = window.devicePixelRatio || 1;
        const w = Math.max(1, whiteboardEl.clientWidth || 1);
        const h = Math.max(1, whiteboardEl.clientHeight || 1);
        const pw = Math.floor(w * dpr);
        const ph = Math.floor(h * dpr);
        if (linksCanvas.width !== pw || linksCanvas.height !== ph) {
          linksCanvas.width = pw;
          linksCanvas.height = ph;
          linksCanvas.style.width = `${w}px`;
          linksCanvas.style.height = `${h}px`;
        }
        linksCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
      };

      const pointOnRectEdge = (cx, cy, hw, hh, dx, dy) => {
        const absDx = Math.abs(dx);
        const absDy = Math.abs(dy);
        if (hw <= 0 || hh <= 0 || (absDx === 0 && absDy === 0)) return { x: cx, y: cy };
        const scale = 1 / Math.max(absDx / hw, absDy / hh);
        return { x: cx + dx * scale, y: cy + dy * scale };
      };

      const drawArrow = (ctx, fromRect, toRect, accent) => {
        const fromCx = fromRect.x + fromRect.w / 2;
        const fromCy = fromRect.y + fromRect.h / 2;
        const toCx = toRect.x + toRect.w / 2;
        const toCy = toRect.y + toRect.h / 2;
        const dx = toCx - fromCx;
        const dy = toCy - fromCy;
        const dist = Math.hypot(dx, dy);
        if (!Number.isFinite(dist) || dist < 5) return;

        const start = pointOnRectEdge(fromCx, fromCy, fromRect.w / 2, fromRect.h / 2, dx, dy);
        const end = pointOnRectEdge(toCx, toCy, toRect.w / 2, toRect.h / 2, -dx, -dy);

        ctx.save();
        ctx.globalAlpha = 0.6;
        ctx.strokeStyle = accent;
        ctx.fillStyle = accent;
        ctx.lineWidth = 2;
        ctx.lineCap = 'round';

        ctx.beginPath();
        ctx.moveTo(start.x, start.y);
        ctx.lineTo(end.x, end.y);
        ctx.stroke();

        const ux = (end.x - start.x) / dist;
        const uy = (end.y - start.y) / dist;
        const arrowLen = 11;
        const arrowW = 7;
        const bx = end.x - ux * arrowLen;
        const by = end.y - uy * arrowLen;
        const px = -uy;
        const py = ux;

        ctx.beginPath();
        ctx.moveTo(end.x, end.y);
        ctx.lineTo(bx + px * arrowW, by + py * arrowW);
        ctx.lineTo(bx - px * arrowW, by - py * arrowW);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      };

      const drawLinks = () => {
        ensureLinksCanvas();
        if (!linksCanvas || !linksCtx) return;
        resizeLinksCanvas();
        const boardRect = whiteboardEl.getBoundingClientRect();
        linksCtx.clearRect(0, 0, whiteboardEl.clientWidth || 1, whiteboardEl.clientHeight || 1);
        const accent = getAccent();

        Array.from(linksById.values()).forEach((link) => {
          const fromId = Number(link.from_id || 0);
          const toId = Number(link.to_id || 0);
          if (!fromId || !toId) return;
          const fromEl = cardsById.get(fromId);
          const toEl = cardsById.get(toId);
          if (!fromEl || !toEl || fromEl.hidden || toEl.hidden) return;
          const fr = fromEl.getBoundingClientRect();
          const tr = toEl.getBoundingClientRect();
          if (!fr.width || !fr.height || !tr.width || !tr.height) return;

          const fromRect = { x: fr.left - boardRect.left, y: fr.top - boardRect.top, w: fr.width, h: fr.height };
          const toRect = { x: tr.left - boardRect.left, y: tr.top - boardRect.top, w: tr.width, h: tr.height };
          drawArrow(linksCtx, fromRect, toRect, accent);
        });
      };

      const scheduleDraw = () => {
        if (drawPending) return;
        drawPending = true;
        window.requestAnimationFrame(() => {
          drawPending = false;
          drawLinks();
        });
      };

      const autosizeTextarea = (ta) => {
        if (!ta || ta.hidden) return;
        ta.style.height = 'auto';
        const max = 420;
        const next = Math.min(max, ta.scrollHeight || 0);
        ta.style.height = `${Math.max(44, next)}px`;
        ta.style.overflowY = ta.scrollHeight > max ? 'auto' : 'hidden';
        scheduleDraw();
      };

      const scheduleAutosize = (ta) => {
        window.requestAnimationFrame(() => autosizeTextarea(ta));
      };

      const applyCamera = () => {
        whiteboardWorldEl.style.transform = `translate(${camera.x}px, ${camera.y}px) scale(${camera.scale})`;
        scheduleDraw();
      };

      const viewCenterWorld = () => {
        const rect = whiteboardEl.getBoundingClientRect();
        const sx = rect.width / 2;
        const sy = rect.height / 2;
        return { x: (sx - camera.x) / camera.scale, y: (sy - camera.y) / camera.scale };
      };

      const centerOnWorld = (wx, wy, scale = camera.scale) => {
        const rect = whiteboardEl.getBoundingClientRect();
        camera.scale = scale;
        camera.x = rect.width / 2 - wx * camera.scale;
        camera.y = rect.height / 2 - wy * camera.scale;
        applyCamera();
      };

      const focusFromHash = () => {
        const hash = String(window.location.hash || '').trim();
        const m = hash.match(/^#whiteboard-card-(\d+)$/);
        if (!m) return false;
        const id = Number(m[1]);
        const st = stateById.get(id);
        if (!st) return false;
        const x = Number(st.x || 0);
        const y = Number(st.y || 0);
        centerOnWorld(x + 130, y + 110, camera.scale);
        const el = cardsById.get(id);
        if (el) {
          el.classList.add('is-highlight');
          setTimeout(() => el.classList.remove('is-highlight'), 1200);
        }
        return true;
      };

      const resetView = () => {
        if (!stateById.size) {
          camera.x = 0;
          camera.y = 0;
          camera.scale = 1;
          applyCamera();
          return;
        }
        let minX = Infinity;
        let minY = Infinity;
        let maxX = -Infinity;
        let maxY = -Infinity;
        stateById.forEach((c) => {
          const x = Number(c.x || 0);
          const y = Number(c.y || 0);
          minX = Math.min(minX, x);
          minY = Math.min(minY, y);
          maxX = Math.max(maxX, x + 300);
          maxY = Math.max(maxY, y + 240);
        });
        const cx = (minX + maxX) / 2;
        const cy = (minY + maxY) / 2;
        centerOnWorld(cx, cy, 1);
      };

      const pointers = new Map();
      let gesture = 'none';
      let panPointerId = 0;
      let panStartX = 0;
      let panStartY = 0;
      let panOriginX = 0;
      let panOriginY = 0;

      let pinchStartDist = 0;
      let pinchStartScale = 1;
      let pinchWorldMidX = 0;
      let pinchWorldMidY = 0;

      const setGesture = (next) => {
        gesture = next;
        whiteboardEl.classList.toggle('is-panning', next !== 'none');
      };

      const startPanFrom = (pointerId, cx, cy) => {
        panPointerId = pointerId;
        panStartX = cx;
        panStartY = cy;
        panOriginX = camera.x;
        panOriginY = camera.y;
        setGesture('pan');
      };

      const startPinch = () => {
        if (pointers.size < 2) return;
        const rect = whiteboardEl.getBoundingClientRect();
        const pts = Array.from(pointers.values());
        const a = pts[0];
        const b = pts[1];
        pinchStartDist = Math.max(1, Math.hypot(a.cx - b.cx, a.cy - b.cy));
        pinchStartScale = camera.scale;
        const midClientX = (a.cx + b.cx) / 2;
        const midClientY = (a.cy + b.cy) / 2;
        const sx = midClientX - rect.left;
        const sy = midClientY - rect.top;
        pinchWorldMidX = (sx - camera.x) / camera.scale;
        pinchWorldMidY = (sy - camera.y) / camera.scale;
        setGesture('pinch');
      };

      const updatePinch = () => {
        if (gesture !== 'pinch' || pointers.size < 2) return;
        const rect = whiteboardEl.getBoundingClientRect();
        const pts = Array.from(pointers.values());
        const a = pts[0];
        const b = pts[1];
        const dist = Math.max(1, Math.hypot(a.cx - b.cx, a.cy - b.cy));
        const nextScale = clamp(pinchStartScale * (dist / pinchStartDist), 0.2, 3);
        const midClientX = (a.cx + b.cx) / 2;
        const midClientY = (a.cy + b.cy) / 2;
        const sx = midClientX - rect.left;
        const sy = midClientY - rect.top;
        camera.scale = nextScale;
        camera.x = sx - pinchWorldMidX * camera.scale;
        camera.y = sy - pinchWorldMidY * camera.scale;
        applyCamera();
      };

      const onPointerEnd = (ev) => {
        if (!pointers.has(ev.pointerId)) return;
        pointers.delete(ev.pointerId);

        if (gesture === 'pinch') {
          if (pointers.size === 1) {
            const [nextId, nextPt] = pointers.entries().next().value;
            startPanFrom(nextId, nextPt.cx, nextPt.cy);
          } else if (!pointers.size) {
            setGesture('none');
          }
          return;
        }

        if (gesture === 'pan' && ev.pointerId === panPointerId) {
          if (pointers.size === 1) {
            const [nextId, nextPt] = pointers.entries().next().value;
            startPanFrom(nextId, nextPt.cx, nextPt.cy);
          } else {
            setGesture('none');
          }
          return;
        }

        if (!pointers.size) setGesture('none');
      };

      whiteboardEl.addEventListener('pointerdown', (ev) => {
        if (ev.button != null && ev.button !== 0) return;
        if (ev.target && ev.target.closest && ev.target.closest('.whiteboard-card')) return;
        if (pointers.size >= 2 && !pointers.has(ev.pointerId)) return;

        pointers.set(ev.pointerId, { cx: ev.clientX, cy: ev.clientY });
        try {
          whiteboardEl.setPointerCapture(ev.pointerId);
        } catch (err) {
          // ignore
        }

        if (pointers.size === 1) startPanFrom(ev.pointerId, ev.clientX, ev.clientY);
        else if (pointers.size === 2) startPinch();
      });

      whiteboardEl.addEventListener('pointermove', (ev) => {
        if (!pointers.has(ev.pointerId)) return;
        pointers.set(ev.pointerId, { cx: ev.clientX, cy: ev.clientY });

        if (gesture === 'pinch') {
          updatePinch();
          return;
        }

        if (gesture !== 'pan' || ev.pointerId !== panPointerId) return;
        const dx = ev.clientX - panStartX;
        const dy = ev.clientY - panStartY;
        camera.x = panOriginX + dx;
        camera.y = panOriginY + dy;
        applyCamera();
      });

      whiteboardEl.addEventListener('pointerup', onPointerEnd);
      whiteboardEl.addEventListener('pointercancel', onPointerEnd);

      whiteboardEl.addEventListener(
        'wheel',
        (ev) => {
          const overCard = ev.target && ev.target.closest && ev.target.closest('.whiteboard-card');
          const rect = whiteboardEl.getBoundingClientRect();
          const sx = ev.clientX - rect.left;
          const sy = ev.clientY - rect.top;
          if (ev.ctrlKey || ev.metaKey) {
            ev.preventDefault();
            const zoomIntensity = 0.0018;
            const nextScale = clamp(camera.scale * (1 - ev.deltaY * zoomIntensity), 0.2, 3);
            const wx = (sx - camera.x) / camera.scale;
            const wy = (sy - camera.y) / camera.scale;
            camera.scale = nextScale;
            camera.x = sx - wx * camera.scale;
            camera.y = sy - wy * camera.scale;
            applyCamera();
          } else {
            if (overCard) return;
            ev.preventDefault();
            camera.x -= ev.deltaX;
            camera.y -= ev.deltaY;
            applyCamera();
          }
        },
        { passive: false }
      );

      const cardMatchFilter = (card) => {
        if (!card) return false;
        const entry = card.entry || {};
        const tags = Array.isArray(card.entry_tags) ? card.entry_tags : Array.isArray(entry.tags) ? entry.tags : [];
        const mood = String(card.entry_mood || entry.mood || '').trim().toLowerCase();
        const type = String(card.entry_type || entry.type || '').trim().toLowerCase();
        if (filterState.tag) {
          const tagNeedle = filterState.tag.toLowerCase();
          const ok = tags.some((tag) => String(tag || '').toLowerCase().includes(tagNeedle));
          if (!ok) return false;
        }
        if (filterState.mood && mood !== filterState.mood) return false;
        if (filterState.type && type !== filterState.type) return false;
        return true;
      };

      const applyFilters = () => {
        cardsById.forEach((el, id) => {
          const st = stateById.get(Number(id));
          el.hidden = !cardMatchFilter(st);
        });
        scheduleDraw();
      };

      const renderAttachments = (container, atts) => {
        if (!container) return;
        container.innerHTML = '';
        const list = Array.isArray(atts) ? atts : [];
        if (!list.length) return;
        list.forEach((att) => {
          const media = createMediaNode(att.media_type, att.url || '', {
            title: att.filename || '',
            alt: att.filename || '',
            preview: att.preview_url || '',
          });
          if (media) {
            const scheduleOnce = () => scheduleDraw();
            const tag = String(media.tagName || '').toLowerCase();
            if (tag === 'img') media.addEventListener('load', scheduleOnce, { once: true });
            if (tag === 'video' || tag === 'audio') media.addEventListener('loadedmetadata', scheduleOnce, { once: true });
            if (media.querySelector) {
              const innerImg = media.querySelector('img');
              if (innerImg) innerImg.addEventListener('load', scheduleOnce, { once: true });
              const innerMedia = media.querySelector('video,audio');
              if (innerMedia) innerMedia.addEventListener('loadedmetadata', scheduleOnce, { once: true });
            }
            if (att.url && att.media_type === 'image') {
              const a = document.createElement('a');
              a.href = att.url;
              a.target = '_blank';
              a.rel = 'noopener';
              a.appendChild(media);
              container.appendChild(a);
            } else {
              container.appendChild(media);
            }
          } else if (att.url) {
            const a = document.createElement('a');
            a.href = att.url;
            a.target = '_blank';
            a.rel = 'noopener';
            a.className = 'text-link';
            a.textContent = att.filename || 'file';
            container.appendChild(a);
          }

          const meta = document.createElement('div');
          meta.className = 'whiteboard-attachment__meta';
          meta.textContent = `${att.filename || 'file'} · ${att.media_type || 'file'} · ${formatBytes(att.size_bytes || 0)}`;
          container.appendChild(meta);
        });
      };

      const renderMeta = (container, card) => {
        if (!container) return;
        container.innerHTML = '';
        const entry = card.entry || {};
        const tags = Array.isArray(card.entry_tags) ? card.entry_tags : Array.isArray(entry.tags) ? entry.tags : [];
        const mood = String(card.entry_mood || entry.mood || '').trim();
        const type = String(card.entry_type || entry.type || '').trim();
        const t = formatClock(card.created_at || card.updated_at);
        const chips = [];
        if (t) chips.push(`⏱ ${t}`);
        if (type) chips.push(`type:${type}`);
        if (mood) chips.push(`mood:${mood}`);
        tags.slice(0, 4).forEach((tag) => chips.push(`#${tag}`));
        chips.forEach((text) => {
          const chip = document.createElement('span');
          chip.className = 'whiteboard-card__chip';
          chip.textContent = text;
          container.appendChild(chip);
        });
      };

      const updateLinkingUI = () => {
        cardsById.forEach((el, id) => {
          el.classList.toggle('is-linking', linkingFromId && Number(id) === Number(linkingFromId));
        });
      };

      const upsertLink = (link) => {
        if (!link || !link.id) return;
        linksById.set(Number(link.id), link);
        scheduleDraw();
      };

      const removeLink = (id) => {
        linksById.delete(Number(id));
        scheduleDraw();
      };

      const replaceLinks = (links) => {
        linksById.clear();
        (Array.isArray(links) ? links : []).forEach((link) => {
          if (link && link.id) linksById.set(Number(link.id), link);
        });
        scheduleDraw();
      };

      const createLink = async (fromId, toId) => {
        const resp = await apiFetch('/api/whiteboard/links', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date, from_id: fromId, to_id: toId }),
        });
        if (resp && resp.link) upsertLink(resp.link);
      };

      const deleteLink = async (linkId) => {
        await apiFetch(`/api/whiteboard/links/${linkId}`, { method: 'DELETE' });
        removeLink(linkId);
      };

      const clearLinksForCard = async (cardId) => {
        const ids = Array.from(linksById.values())
          .filter((l) => Number(l.from_id || 0) === Number(cardId) || Number(l.to_id || 0) === Number(cardId))
          .map((l) => Number(l.id || 0))
          .filter(Boolean);
        if (!ids.length) return;
        for (const id of ids) {
          try {
            // eslint-disable-next-line no-await-in-loop
            await deleteLink(id);
          } catch (err) {
            // ignore
          }
        }
      };

      const handlePatchConflict = (err) => {
        if (err && err.status === 409 && err.payload && err.payload.card) {
          upsertCard(err.payload.card);
          setSyncState('发现并合并了冲突更新');
          return true;
        }
        return false;
      };

      const upsertCard = (card) => {
        if (!card || !card.id) return;
        const id = Number(card.id);
        const existing = stateById.get(id) || {};
        const next = { ...existing, ...card };
        if (card.attachments) next.attachments = card.attachments;
        if (card.entry) next.entry = card.entry;
        stateById.set(id, next);

        let el = cardsById.get(id);
        if (!el) {
          el = document.createElement('div');
          el.className = 'whiteboard-card';
          el.dataset.cardId = String(id);
          el.id = `whiteboard-card-${id}`;

          const head = document.createElement('div');
          head.className = 'whiteboard-card__head';
          const title = document.createElement('strong');
          title.textContent = `#${id}`;
          const actions = document.createElement('div');
          actions.className = 'whiteboard-card__actions';

          const linkBtn = document.createElement('button');
          linkBtn.type = 'button';
          linkBtn.className = 'whiteboard-link-btn';
          linkBtn.textContent = '->';
          linkBtn.title = '连接箭头：先点起点，再点终点';
          linkBtn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            if (!linkingFromId) {
              linkingFromId = id;
              updateLinkingUI();
              return;
            }
            if (Number(linkingFromId) === Number(id)) {
              linkingFromId = 0;
              updateLinkingUI();
              return;
            }
            const fromId = linkingFromId;
            linkingFromId = 0;
            updateLinkingUI();
            try {
              await createLink(fromId, id);
            } catch (err) {
              window.alert(err.message);
            }
          });

          const clearLinksBtn = document.createElement('button');
          clearLinksBtn.type = 'button';
          clearLinksBtn.className = 'whiteboard-clear-links-btn';
          clearLinksBtn.textContent = 'x';
          clearLinksBtn.title = '清除此卡片相关的所有箭头';
          clearLinksBtn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            if (!window.confirm('清除与此卡片相关的所有箭头？')) return;
            await clearLinksForCard(id);
          });

          const del = document.createElement('button');
          del.type = 'button';
          del.className = 'whiteboard-del-btn';
          del.textContent = '删除';
          del.addEventListener('click', async () => {
            if (!window.confirm('删除这张卡片？')) return;
            await apiFetch(`/api/whiteboard/cards/${id}`, { method: 'DELETE' });
          });
          actions.appendChild(linkBtn);
          actions.appendChild(clearLinksBtn);
          actions.appendChild(del);
          head.appendChild(title);
          head.appendChild(actions);

          const body = document.createElement('div');
          body.className = 'whiteboard-card__body';

          const meta = document.createElement('div');
          meta.className = 'whiteboard-card__meta';

          const attachments = document.createElement('div');
          attachments.className = 'whiteboard-attachments';

          const noteBtn = document.createElement('button');
          noteBtn.type = 'button';
          noteBtn.className = 'whiteboard-note-btn';
          noteBtn.textContent = '添加文字';
          noteBtn.hidden = true;

          const ta = document.createElement('textarea');
          ta.value = next.text || '';
          ta.placeholder = '写点什么...';
          ta.addEventListener('input', () => {
            scheduleAutosize(ta);
            if (noteBtn && !noteBtn.hidden) {
              const hasText = String(ta.value || '').trim().length > 0;
              noteBtn.textContent = ta.hidden ? (hasText ? '显示文字' : '添加文字') : hasText ? '隐藏文字' : '收起';
            }
            clearTimeout(el._saveTimer);
            el._saveTimer = setTimeout(async () => {
              try {
                const state = stateById.get(id) || {};
                const resp = await apiFetch(`/api/whiteboard/cards/${id}`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ text: ta.value, expected_version: Number(state.version || 1) }),
                });
                if (resp && resp.card) upsertCard(resp.card);
              } catch (err) {
                if (handlePatchConflict(err)) return;
              }
            }, 350);
          });
          ta.addEventListener('blur', () => {
            const st = stateById.get(id) || {};
            const hasAttachments = Array.isArray(st.attachments) && st.attachments.length > 0;
            const hasText = String(ta.value || '').trim().length > 0;
            if (hasAttachments && !hasText) {
              el.dataset.textCollapsed = '1';
              ta.hidden = true;
              if (noteBtn) {
                noteBtn.hidden = false;
                noteBtn.textContent = '添加文字';
              }
              scheduleDraw();
            }
          });

          body.appendChild(meta);
          body.appendChild(attachments);
          body.appendChild(noteBtn);
          body.appendChild(ta);
          el.appendChild(head);
          el.appendChild(body);
          whiteboardWorldEl.appendChild(el);
          cardsById.set(id, el);
          el._headEl = head;
          el._titleEl = title;
          el._metaEl = meta;
          el._attachmentsEl = attachments;
          el._textareaEl = ta;
          el._noteBtnEl = noteBtn;

          noteBtn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            const st = stateById.get(id) || {};
            const hasAttachments = Array.isArray(st.attachments) && st.attachments.length > 0;
            if (!hasAttachments) return;
            if (ta.hidden) {
              el.dataset.textCollapsed = '0';
              ta.hidden = false;
              noteBtn.hidden = false;
              noteBtn.textContent = String(ta.value || '').trim().length > 0 ? '隐藏文字' : '收起';
              ta.focus();
              scheduleAutosize(ta);
              return;
            }
            el.dataset.textCollapsed = '1';
            ta.hidden = true;
            noteBtn.hidden = false;
            noteBtn.textContent = String(ta.value || '').trim().length > 0 ? '显示文字' : '添加文字';
            scheduleDraw();
          });

          let dragging = false;
          let startX = 0;
          let startY = 0;
          let originX = 0;
          let originY = 0;

          const onMove = (ev) => {
            if (!dragging) return;
            const dx = (ev.clientX - startX) / camera.scale;
            const dy = (ev.clientY - startY) / camera.scale;
            const x = originX + dx;
            const y = originY + dy;
            el.style.transform = `translate(${x}px, ${y}px)`;
            el._pendingPos = { x, y };
            scheduleDraw();
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
                const state = stateById.get(id) || {};
                const resp = await apiFetch(`/api/whiteboard/cards/${id}`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ x: pos.x, y: pos.y, expected_version: Number(state.version || 1) }),
                });
                if (resp && resp.card) upsertCard(resp.card);
              } catch (err) {
                if (handlePatchConflict(err)) return;
              }
            }
          };

          head.addEventListener('pointerdown', (ev) => {
            ev.stopPropagation();
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
        const hasAttachments = Array.isArray(next.attachments) && next.attachments.length > 0;
        const hasText = String(next.text || '').trim().length > 0;
        const isMediaOnly = hasAttachments && !hasText;

        el.style.transform = `translate(${x}px, ${y}px)`;
        el.classList.toggle('whiteboard-card--media', isMediaOnly);
        if (el._attachmentsEl) renderAttachments(el._attachmentsEl, next.attachments || []);
        if (el._metaEl) renderMeta(el._metaEl, next);
        if (el._titleEl) {
          const clock = formatClock(next.created_at || next.updated_at);
          el._titleEl.textContent = clock ? `#${id} · ${clock}` : `#${id}`;
        }

        const ta = el._textareaEl || qs('textarea', el);
        if (ta && document.activeElement !== ta) {
          ta.value = next.text || '';
        }
        const noteBtn = el._noteBtnEl;
        if (noteBtn) noteBtn.hidden = !hasAttachments || isMediaOnly;
        if (ta) {
          const collapsedFlag = String(el.dataset.textCollapsed || '');
          const desiredCollapsed = isMediaOnly
            ? true
            : hasAttachments
              ? hasText
                ? collapsedFlag === '1'
                : collapsedFlag !== '0'
              : false;
          if (document.activeElement !== ta) {
            ta.hidden = desiredCollapsed;
          }
          if (noteBtn && !noteBtn.hidden) {
            noteBtn.textContent = ta.hidden ? (hasText ? '显示文字' : '添加文字') : hasText ? '隐藏文字' : '收起';
          }
          if (!ta.hidden) scheduleAutosize(ta);
        }
        updateLinkingUI();
        applyFilters();
        scheduleDraw();
      };

      const removeCard = (id) => {
        const el = cardsById.get(id);
        if (el) el.remove();
        cardsById.delete(id);
        stateById.delete(id);
        Array.from(linksById.entries()).forEach(([linkId, link]) => {
          const fromId = Number(link.from_id || 0);
          const toId = Number(link.to_id || 0);
          if (fromId === Number(id) || toId === Number(id)) linksById.delete(linkId);
        });
        if (Number(linkingFromId) === Number(id)) {
          linkingFromId = 0;
          updateLinkingUI();
        }
        scheduleDraw();
      };

      const replaceBoard = (cards) => {
        const nextIds = new Set((cards || []).map((c) => Number(c && c.id)));
        Array.from(stateById.keys()).forEach((id) => {
          if (!nextIds.has(id)) removeCard(id);
        });
        (cards || []).forEach((card) => upsertCard(card));
      };

      const loadBoard = async () => {
        const data = await apiFetch(`/api/whiteboard/board?date=${encodeURIComponent(date)}`);
        syncCursor = String(data.sync_cursor || '').trim();
        const cards = Array.isArray(data.cards) ? data.cards : [];
        const links = Array.isArray(data.links) ? data.links : [];
        replaceBoard(cards);
        replaceLinks(links);
        setSyncState(navigator.onLine ? '已同步' : '离线');
        updateQueueStatus();
      };

      const poll = async () => {
        if (polling || !navigator.onLine) return;
        polling = true;
        try {
          const data = await apiFetch(
            `/api/whiteboard/changes?date=${encodeURIComponent(date)}&cursor=${encodeURIComponent(syncCursor || '')}`
          );
          syncCursor = String(data.next_cursor || syncCursor || '').trim();
          if (data.reset) {
            replaceBoard(Array.isArray(data.cards) ? data.cards : []);
            replaceLinks(Array.isArray(data.links) ? data.links : []);
            return;
          }
          (Array.isArray(data.cards) ? data.cards : []).forEach((card) => upsertCard(card));
          (Array.isArray(data.deleted_card_ids) ? data.deleted_card_ids : []).forEach((id) => removeCard(Number(id)));
          if (data.links_changed) {
            replaceLinks(Array.isArray(data.links) ? data.links : []);
          }
          setSyncState('已同步');
        } catch (err) {
          if (!navigator.onLine) setSyncState('离线');
        } finally {
          polling = false;
        }
      };

      const processTextQueue = async () => {
        if (queueBusy || !navigator.onLine || !textQueue.length) {
          updateQueueStatus();
          return;
        }
        queueBusy = true;
        setSyncState('同步中...');
        updateQueueStatus();
        while (textQueue.length && navigator.onLine) {
          const item = textQueue[0];
          try {
            // eslint-disable-next-line no-await-in-loop
            const resp = await apiFetch('/api/whiteboard/cards', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(item.payload || {}),
            });
            if (resp && resp.card) upsertCard(resp.card);
            textQueue.shift();
            saveQueue(textQueue);
          } catch (err) {
            if (err && err.status >= 400 && err.status < 500 && err.status !== 409) {
              textQueue.shift();
              saveQueue(textQueue);
              continue;
            }
            break;
          }
        }
        queueBusy = false;
        setSyncState(navigator.onLine ? '已同步' : '离线');
        updateQueueStatus();
      };

      const buildTextPayload = (rawText, opts = {}) => {
        const baseText = opts.skipTimestamp ? String(rawText || '').trim() : withTimePrefix(rawText);
        if (!baseText) return null;
        const normalizedText = baseText.length > 5000 ? baseText.slice(0, 5000) : baseText;
        const center = viewCenterWorld();
        const x = center.x - 42;
        const y = center.y - 42;
        const entry = { ...quickEntry('note'), ...(opts.entry || {}) };
        return {
          date,
          x,
          y,
          text: normalizedText,
          idempotency_key: opts.idempotencyKey || newIdempotencyKey('wbtxt'),
          entry_date: entry.date || date,
          entry_tags: Array.isArray(entry.tags) ? entry.tags : [],
          entry_mood: entry.mood || '',
          entry_type: entry.type || 'note',
        };
      };

      const enqueueTextPayload = async (payload, clearQuick = true) => {
        if (!payload) return;
        textQueue.push({ id: payload.idempotency_key, payload, created_at: Date.now() });
        saveQueue(textQueue);
        updateQueueStatus();
        if (clearQuick && quickTextEl) quickTextEl.value = '';
        if (whiteboardShell) whiteboardShell.classList.remove('is-quick-open');
        await processTextQueue();
      };

      const queueTextRecord = async (rawText) => {
        const payload = buildTextPayload(rawText);
        await enqueueTextPayload(payload, true);
      };

      const uploadMediaFile = async (file, extraText = '') => {
        if (!file) return;
        const center = viewCenterWorld();
        const x = center.x - 42;
        const y = center.y - 42;
        const entry = quickEntry('media');
        const payloadText = String(extraText || '').trim();
        const mediaIdempotencyKey = newIdempotencyKey('wbmedia');
        uploadProgress = 0;
        uploadMessage = '媒体上传 0%';
        updateQueueStatus();
        try {
          const response = await retryAsync(
            (attempt) =>
              directUploadWhiteboardMedia({
                date,
                x,
                y,
                text: payloadText,
                file,
                idempotencyKey: mediaIdempotencyKey,
                entry: {
                  date: entry.date,
                  tags: entry.tags,
                  mood: entry.mood,
                  type: payloadText ? entry.type || 'note' : entry.type || 'media',
                },
                onProgress: (p) => {
                  uploadProgress = p;
                  const pct = Math.round(Math.max(0, Math.min(1, p || 0)) * 100);
                  uploadMessage = `媒体上传 ${pct}%${attempt > 0 ? ` (重试 ${attempt})` : ''}`;
                  updateQueueStatus();
                },
              }),
            2,
            800
          );
          if (response && response.card) upsertCard(response.card);
          if (quickTextEl && payloadText) quickTextEl.value = '';
        } catch (err) {
          window.alert(err.message || '媒体上传失败');
        } finally {
          uploadProgress = 0;
          uploadMessage = '';
          updateQueueStatus();
        }
      };

      if (whiteboardAddBtn) {
        whiteboardAddBtn.addEventListener('click', async () => {
          const center = viewCenterWorld();
          const x = center.x - 40;
          const y = center.y - 40;
          const resp = await apiFetch('/api/whiteboard/cards', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              date,
              x,
              y,
              text: withTimePrefix(''),
              entry_date: date,
              entry_tags: [],
              entry_mood: '',
              entry_type: 'note',
              idempotency_key: newIdempotencyKey('wbblank'),
            }),
          });
          if (resp && resp.card) upsertCard(resp.card);
        });
      }

      if (whiteboardSummaryBtn) {
        whiteboardSummaryBtn.addEventListener('click', async () => {
          try {
            const data = await apiFetch(`/api/dailyreal/summary?date=${date}&tz_offset=${new Date().getTimezoneOffset()}`);
            const summary = String((data && data.summary) || '').trim();
            if (!summary) {
              window.alert('今天暂无可汇总内容');
              return;
            }
            const payload = buildTextPayload(summary, {
              skipTimestamp: true,
              idempotencyKey: newIdempotencyKey('wbsummary'),
              entry: {
                date,
                tags: ['dailyreal', 'summary'],
                mood: '',
                type: 'summary',
              },
            });
            await enqueueTextPayload(payload, false);
          } catch (err) {
            window.alert(err.message || '生成总结失败');
          }
        });
      }

      if (whiteboardResetBtn) whiteboardResetBtn.addEventListener('click', () => resetView());

      if (whiteboardExportBtn) {
        whiteboardExportBtn.addEventListener('click', async () => {
          try {
            const data = await apiFetch(`/api/whiteboard/export?date=${encodeURIComponent(date)}`);
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `whiteboard-${date}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
          } catch (err) {
            window.alert(err.message);
          }
        });
      }

      if (whiteboardImportBtn && whiteboardImportFile) {
        whiteboardImportBtn.addEventListener('click', () => {
          whiteboardImportFile.value = '';
          whiteboardImportFile.click();
        });
        whiteboardImportFile.addEventListener('change', async () => {
          const file = (whiteboardImportFile.files && whiteboardImportFile.files[0]) || null;
          if (!file) return;
          let parsed = null;
          try {
            const text = await file.text();
            parsed = JSON.parse(text);
          } catch (err) {
            window.alert('导入失败：不是合法 JSON');
            return;
          }
          const mode = window.confirm('导入模式：确定=覆盖(replace)，取消=合并(merge)') ? 'replace' : 'merge';
          try {
            await apiFetch('/api/whiteboard/import', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ date, mode, board: parsed }),
            });
            await loadBoard();
          } catch (err) {
            window.alert(err.message);
          }
        });
      }

      if (whiteboardMediaBtn && whiteboardMediaFile) {
        whiteboardMediaBtn.addEventListener('click', () => {
          whiteboardMediaFile.value = '';
          whiteboardMediaFile.click();
        });
        whiteboardMediaFile.addEventListener('change', async () => {
          const file = (whiteboardMediaFile.files && whiteboardMediaFile.files[0]) || null;
          if (!file) return;
          await uploadMediaFile(file, quickTextEl ? quickTextEl.value : '');
          whiteboardMediaFile.value = '';
        });
      }

      if (quickSendBtn && quickTextEl) {
        quickSendBtn.addEventListener('click', async () => {
          await queueTextRecord(quickTextEl.value);
        });
        quickTextEl.addEventListener('keydown', async (ev) => {
          if (ev.key !== 'Enter' || ev.shiftKey) return;
          ev.preventDefault();
          await queueTextRecord(quickTextEl.value);
        });
      }

      if (mobileQuickBtn && whiteboardShell && quickTextEl) {
        mobileQuickBtn.addEventListener('click', () => {
          whiteboardShell.classList.toggle('is-quick-open');
          if (whiteboardShell.classList.contains('is-quick-open')) quickTextEl.focus();
        });
      }

      const pasteHandler = async (ev) => {
        const cd = ev.clipboardData;
        if (!cd || !cd.items) return;
        const files = [];
        for (const item of cd.items) {
          if (!item || !item.type || !item.type.startsWith('image/')) continue;
          const file = item.getAsFile();
          if (file) files.push(file);
        }
        if (!files.length) return;
        ev.preventDefault();
        for (const file of files) {
          // eslint-disable-next-line no-await-in-loop
          await uploadMediaFile(file, quickTextEl ? quickTextEl.value : '');
        }
      };
      whiteboardEl.addEventListener('paste', pasteHandler);
      if (quickTextEl) quickTextEl.addEventListener('paste', pasteHandler);

      if (filterTagEl) {
        filterTagEl.addEventListener('input', () => {
          filterState.tag = String(filterTagEl.value || '').trim();
          applyFilters();
        });
      }
      if (filterMoodEl) {
        filterMoodEl.addEventListener('change', () => {
          filterState.mood = String(filterMoodEl.value || '').trim().toLowerCase();
          applyFilters();
        });
      }
      if (filterTypeEl) {
        filterTypeEl.addEventListener('change', () => {
          filterState.type = String(filterTypeEl.value || '').trim().toLowerCase();
          applyFilters();
        });
      }
      if (filterClearBtn) {
        filterClearBtn.addEventListener('click', () => {
          filterState.tag = '';
          filterState.mood = '';
          filterState.type = '';
          if (filterTagEl) filterTagEl.value = '';
          if (filterMoodEl) filterMoodEl.value = '';
          if (filterTypeEl) filterTypeEl.value = '';
          applyFilters();
        });
      }

      document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' && linkingFromId) {
          linkingFromId = 0;
          updateLinkingUI();
        }
      });

      window.addEventListener('resize', () => scheduleDraw());
      window.addEventListener('hashchange', () => focusFromHash());
      window.addEventListener('online', () => {
        setSyncState('在线');
        processTextQueue();
      });
      window.addEventListener('offline', () => setSyncState('离线'));

      updateQueueStatus();
      setSyncState(navigator.onLine ? '在线' : '离线');
      applyCamera();
      loadBoard().then(async () => {
        if (!focusFromHash()) resetView();
        await processTextQueue();
        setInterval(poll, 1200);
      });
    };

    if (todayBtn) todayBtn.addEventListener('click', loadToday);
    bindQuickAdd();
    loadQuickLinks();
    loadToday();
    initWhiteboard();
  };

	  initNav();

	  if (page === 'home' || page === 'whiteboard') initHome();
	  if (page === 'blog') initProjectsPage('blog');
	  if (page === 'note') initProjectsPage('note');
	  if (page === 'echoes') initEchoes();
	  if (page === 'dailyreal') initDailyreal();
	})();
