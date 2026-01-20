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
        throw new Error('Login required');
      }
      let message = `Request failed: ${resp.status}`;
      try {
        const data = await resp.json();
        if (data && data.error) {
          message = data.error;
        }
      } catch (err) {
        // ignore
      }
      throw new Error(message);
    }
    return resp.json();
  };

  const MEDIA_EXTS = {
    image: new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg']),
    video: new Set(['.mp4', '.mov', '.webm', '.mkv', '.avi']),
    audio: new Set(['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg']),
    pdf: new Set(['.pdf']),
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
    return '';
  };

  const resolveMediaType = (item, fallback = 'file') => {
    if (!item) return fallback;
    const urlType = detectMediaType(
      normalizeUrl(item.url || item.src || item.oss_key || item.ref || '')
    );
    if (urlType) return urlType;
    return item.media_type || item.type || fallback;
  };

  const createPdfCover = (src, opts = {}) => {
    const safeSrc = normalizeUrl(src);
    if (!safeSrc) return null;
    const previewUrl = normalizeUrl(opts.preview);
    const title = opts.title || opts.alt || 'PDF document';
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

    if (title) {
      const text = document.createElement('span');
      text.className = 'pdf-title';
      text.textContent = title;
      wrapper.appendChild(text);
    }

    if (opts.showOpen !== false) {
      const open = document.createElement('span');
      open.className = 'pdf-open';
      open.textContent = 'Open';
      wrapper.appendChild(open);
    }

    if (opts.className) {
      wrapper.className = `${wrapper.className} ${opts.className}`.trim();
    }

    return wrapper;
  };

  const createMediaNode = (type, src, opts = {}) => {
    const safeSrc = normalizeUrl(src);
    if (!safeSrc) return null;
    const resolvedType = type && type !== 'file' ? type : detectMediaType(safeSrc);
    if (!resolvedType) return null;
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
        showOpen: true,
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
      let url = href;
      let resolvedTitle = title || '';
      let resolvedText = text || '';
      if (href && typeof href === 'object') {
        url = href.href || href.url || '';
        resolvedTitle = href.title || resolvedTitle;
        resolvedText = href.text || href.alt || resolvedText;
      }
      const safeUrl = normalizeUrl(url);
      if (!safeUrl) return '';
      const mediaType = detectMediaType(safeUrl) || 'image';
      const node = createMediaNode(mediaType, safeUrl, {
        title: resolvedTitle || '',
        alt: resolvedText || '',
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

  const formatDate = (date) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  };

  const escapeRegExp = (str) => str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  const parseWikiRef = (value) => {
    if (!value) return { path: '', label: '' };
    let raw = value.trim();
    let label = '';
    const pipeIndex = raw.indexOf('|');
    if (pipeIndex !== -1) {
      label = raw.slice(pipeIndex + 1).trim();
      raw = raw.slice(0, pipeIndex).trim();
    }
    const hashIndex = raw.indexOf('#');
    if (hashIndex !== -1) {
      raw = raw.slice(0, hashIndex).trim();
    }
    const queryIndex = raw.indexOf('?');
    if (queryIndex !== -1) {
      raw = raw.slice(0, queryIndex).trim();
    }
    return { path: raw, label };
  };

  const normalizeRelativePath = (path) => {
    if (!path) return '';
    const parts = path.split('/').filter((part) => part && part !== '.');
    const out = [];
    for (const part of parts) {
      if (part === '..') return '';
      out.push(part);
    }
    return out.join('/');
  };

  const resolveMarkdownKey = (path, baseKey) => {
    if (!path) return '';
    let normalized = path.replace(/\\/g, '/').trim();
    if (!normalized) return '';
    if (normalized.startsWith('/')) {
      normalized = normalized.replace(/^\/+/, '');
    } else if (normalized.startsWith('./')) {
      const baseDir = baseKey ? baseKey.split('/').slice(0, -1).join('/') : '';
      normalized = baseDir ? `${baseDir}/${normalized.slice(2)}` : normalized.slice(2);
    }
    normalized = normalizeRelativePath(normalized);
    return normalized;
  };

  const applyMarkdownDocLinks = (content, options = {}) => {
    const moduleName = options.moduleName || '';
    if (!moduleName) return content;
    const baseKey = options.baseKey || '';
    return content.replace(/\[\[([^\]]+)\]\]/g, (match, inner, offset, source) => {
      if (offset > 0 && source[offset - 1] === '!') return match;
      const parsed = parseWikiRef(inner);
      const path = parsed.path || '';
      if (!path) return match;
      const lower = path.toLowerCase();
      if (lower.startsWith('http://') || lower.startsWith('https://') || lower.startsWith('data:')) {
        return match;
      }
      if (!lower.endsWith('.md')) return match;
      const key = resolveMarkdownKey(path, baseKey);
      if (!key) return match;
      const label = parsed.label || path;
      const href = `/${moduleName}?key=${encodeURIComponent(key)}`;
      return `[${label}](${href})`;
    });
  };

  const applyAttachmentRefs = (content, attachments, options = {}) => {
    if (!attachments || !attachments.length) {
      return applyMarkdownDocLinks(content, options);
    }
    let result = content;
    attachments.forEach((att) => {
      if (!att.ref || !att.url) {
        return;
      }
      const escaped = escapeRegExp(att.ref);
      const wikiSuffix = `(?=[#?|\\]])[^\\]]*`;
      result = result.replace(
        new RegExp(`!\\[\\[${escaped}${wikiSuffix}\\]\\]`, 'g'),
        `![](${att.url})`
      );
      result = result.replace(
        new RegExp(`\\[\\[${escaped}${wikiSuffix}\\]\\]`, 'g'),
        `[${att.ref}](${att.url})`
      );
      result = result.replace(
        new RegExp(`(!\\[${escaped}\\]\\()([^\\s)]+)([^)]*\\))`, 'g'),
        `$1${att.url}$3`
      );
      result = result.replace(
        new RegExp(`(\\[${escaped}\\]\\()([^\\s)]+)([^)]*\\))`, 'g'),
        `$1${att.url}$3`
      );
      result = result.replace(new RegExp(`\\(${escaped}\\)`, 'g'), `(${att.url})`);
    });
    return applyMarkdownDocLinks(result, options);
  };

  const renderAttachmentStrip = (container, attachments, options = {}) => {
    if (!container) return;
    const allowDelete = Boolean(options.allowDelete && typeof options.onDelete === 'function');
    container.innerHTML = '';
    if (!Array.isArray(attachments) || attachments.length === 0) {
      return;
    }
    attachments.forEach((att) => {
      const card = document.createElement('div');
      card.className = 'attachment-card';
      const label = document.createElement('div');
      label.className = 'album-meta';
      const resolvedType = resolveMediaType(att, 'file');
      label.textContent = resolvedType || 'file';

      const mediaNode = createMediaNode(resolvedType, att.url, {
        alt: getMediaLabel(att) || resolvedType,
        title: getMediaLabel(att) || '',
        preview: att.preview_url || '',
      });

      if (mediaNode) {
        card.appendChild(mediaNode);
      } else {
        const link = document.createElement('a');
        link.href = att.url;
        link.textContent = getMediaLabel(att) || 'file';
        link.className = 'text-link';
        link.target = '_blank';
        link.rel = 'noopener';
        card.appendChild(link);
      }
      card.appendChild(label);
      if (allowDelete) {
        const actions = document.createElement('div');
        actions.className = 'attachment-actions';
        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'attachment-delete';
        delBtn.textContent = 'Delete';
        if (!att || !att.uuid) {
          delBtn.disabled = true;
          delBtn.title = 'Missing attachment id';
        }
        delBtn.addEventListener('click', async () => {
          if (!att || !att.uuid) return;
          const labelText = getMediaLabel(att) || 'this file';
          if (!window.confirm(`Delete ${labelText}?`)) return;
          delBtn.disabled = true;
          delBtn.classList.add('is-busy');
          delBtn.textContent = 'Deleting...';
          try {
            await options.onDelete(att);
          } catch (err) {
            window.alert(err.message || 'Failed to delete attachment');
          } finally {
            delBtn.disabled = false;
            delBtn.classList.remove('is-busy');
            delBtn.textContent = 'Delete';
          }
        });
        actions.appendChild(delBtn);
        card.appendChild(actions);
      }
      container.appendChild(card);
    });
  };

  const renderMarkdownWindow = async (moduleName, key, prefix, scope = document) => {
    const titleEl = qs(`#${prefix}-title`, scope);
    const metaEl = qs(`#${prefix}-meta`, scope);
    const contentEl = qs(`#${prefix}-content`, scope);
    const attachEl = qs(`#${prefix}-attachments`, scope);
    if (contentEl) {
      contentEl.innerHTML = '<p class="muted">Loading...</p>';
    }
    const data = await apiFetch(`/api/${moduleName}/item?key=${encodeURIComponent(key)}`);
    const content = applyAttachmentRefs(data.content || '', data.attachments || [], {
      moduleName,
      baseKey: data.key || key,
    });
    renderMarkdown(content, contentEl);
    if (titleEl) titleEl.textContent = data.title || key;
    if (metaEl) metaEl.textContent = data.key || key;
    renderAttachmentStrip(attachEl, data.attachments);
    return data;
  };

  const normalizeMediaType = (item) => {
    return resolveMediaType(item, '');
  };

  const getMediaUrl = (item) => {
    if (!item) return '';
    return normalizeUrl(item.url || item.src || '');
  };

  const getMediaLabel = (item) => {
    if (!item) return '';
    if (item.caption) return item.caption;
    if (item.ref) return item.ref;
    if (item.oss_key) return item.oss_key.split('/').pop();
    return item.uuid || '';
  };

  const buildReelData = (entry) => {
    const safeEntry = entry || {};
    const attachments = Array.isArray(safeEntry.attachments) ? safeEntry.attachments : [];
    const reel = safeEntry.reel || {};
    const text = (reel.text != null ? reel.text : safeEntry.text) || '';
    let media = Array.isArray(reel.media) ? reel.media : [];
    let audio = reel.audio || null;

    if (!media.length) {
      media = attachments.filter((att) => {
        const mediaType = normalizeMediaType(att);
        return (mediaType === 'image' || mediaType === 'video') && getMediaUrl(att);
      });
    } else {
      media = media.filter((item) => {
        const mediaType = normalizeMediaType(item);
        return (mediaType === 'image' || mediaType === 'video') && getMediaUrl(item);
      });
    }

    if (!audio) {
      audio =
        attachments.find((att) => normalizeMediaType(att) === 'audio' && getMediaUrl(att)) ||
        null;
    } else if (!getMediaUrl(audio)) {
      audio = null;
    }

    const documents = attachments.filter((att) => {
      const mediaType = normalizeMediaType(att);
      return mediaType === 'pdf' && getMediaUrl(att);
    });

    const audioTracks = attachments.filter((att) => {
      const mediaType = normalizeMediaType(att);
      return mediaType === 'audio' && getMediaUrl(att);
    });

    const otherFiles = attachments.filter((att) => {
      const mediaType = normalizeMediaType(att);
      return (
        mediaType &&
        !['image', 'video', 'audio', 'pdf'].includes(mediaType) &&
        getMediaUrl(att)
      );
    });

    return { text, media, audio, documents, audioTracks, otherFiles };
  };

  const renderReelWindow = (entry, container) => {
    if (!container) return null;
    const reelData = buildReelData(entry);
    const reelMode = container.dataset.reelMode || 'viewer';
    const hasExtras =
      (reelData.documents && reelData.documents.length) ||
      (reelData.audioTracks && reelData.audioTracks.length) ||
      (reelData.otherFiles && reelData.otherFiles.length);
    container.innerHTML = '';

    if (!reelData.media.length && !reelData.text && !reelData.audio && !hasExtras) {
      container.innerHTML = '<div class="card placeholder">No reel moments yet.</div>';
      return reelData;
    }

    const shell = document.createElement('div');
    shell.className = 'reel-window';

    const stage = document.createElement('div');
    stage.className = 'reel-stage';

    const media = document.createElement('div');
    media.className = 'reel-media';

    const track = document.createElement('div');
    track.className = 'reel-track';
    media.appendChild(track);

    const slides = reelData.media.length ? reelData.media : [{ media_type: 'empty' }];
    const isMusicOn = Boolean(reelData.audio && getMediaUrl(reelData.audio));

    const emptyMessage = hasExtras
      ? 'No photos or videos yet. Attachments below.'
      : 'No photos or videos yet.';

    slides.forEach((item) => {
      const slide = document.createElement('div');
      slide.className = 'reel-slide';
      const mediaType = normalizeMediaType(item);
      const url = getMediaUrl(item);

      if (!url || mediaType === 'empty') {
        const placeholder = document.createElement('div');
        placeholder.className = 'reel-placeholder';
        placeholder.textContent = emptyMessage;
        slide.appendChild(placeholder);
      } else if (mediaType === 'image') {
        const img = document.createElement('img');
        img.src = url;
        img.alt = getMediaLabel(item) || 'image';
        img.loading = 'lazy';
        img.decoding = 'async';
        slide.appendChild(img);
      } else if (mediaType === 'video') {
        const video = document.createElement('video');
        video.src = url;
        video.controls = true;
        video.playsInline = true;
        video.muted = isMusicOn;
        video.preload = 'metadata';
        slide.appendChild(video);
      } else {
        const fallback = document.createElement('div');
        fallback.className = 'reel-placeholder';
        fallback.textContent = getMediaLabel(item) || 'Unsupported media';
        slide.appendChild(fallback);
      }

      const captionText = item && item.caption != null ? String(item.caption).trim() : '';
      if (captionText) {
        const caption = document.createElement('div');
        caption.className = 'reel-slide-caption';
        caption.textContent = captionText;
        slide.appendChild(caption);
      }

      track.appendChild(slide);
    });

    const copy = document.createElement('div');
    copy.className = 'reel-copy';

    const meta = document.createElement('div');
    meta.className = 'reel-meta';
    const formatCount = (count, label) => `${count} ${count === 1 ? label : `${label}s`}`;
    const mediaCount = reelData.media.length;
    const docCount = reelData.documents.length;
    const audioCount = reelData.audioTracks.length;
    const fileCount = reelData.otherFiles.length;
    const metaParts = [];
    if (mediaCount) {
      metaParts.push(formatCount(mediaCount, 'moment'));
    } else {
      metaParts.push('No media yet');
    }
    if (docCount) metaParts.push(formatCount(docCount, 'pdf'));
    if (audioCount) metaParts.push(formatCount(audioCount, 'track'));
    if (fileCount) metaParts.push(formatCount(fileCount, 'file'));
    meta.textContent = metaParts.join(' - ');
    copy.appendChild(meta);

    const textWrap = document.createElement('div');
    textWrap.className = 'reel-text markdown-body';
    if (reelData.text) {
      renderMarkdown(reelData.text, textWrap);
    } else {
      textWrap.innerHTML = '<p class="muted">No text yet.</p>';
    }
    copy.appendChild(textWrap);

    let audioEl = null;
    if (reelData.audio && getMediaUrl(reelData.audio)) {
      const audioWrap = document.createElement('div');
      audioWrap.className = 'reel-audio';
      const audioInfo = document.createElement('div');
      audioInfo.className = 'reel-audio-info';
      const audioLabel = document.createElement('span');
      audioLabel.className = 'reel-audio-label';
      audioLabel.textContent = 'Background music';
      const audioTitle = document.createElement('span');
      audioTitle.className = 'reel-audio-title';
      audioTitle.textContent = getMediaLabel(reelData.audio) || 'Audio track';
      audioInfo.appendChild(audioLabel);
      audioInfo.appendChild(audioTitle);

      const audioBtn = document.createElement('button');
      audioBtn.type = 'button';
      audioBtn.className = 'reel-audio-btn is-paused';
      audioBtn.textContent = 'Play';

      audioEl = document.createElement('audio');
      audioEl.src = getMediaUrl(reelData.audio);
      audioEl.loop = true;
      audioEl.preload = 'metadata';
      audioEl.volume = 0.6;

      const updateAudioState = () => {
        const playing = !audioEl.paused;
        audioBtn.textContent = playing ? 'Pause' : 'Play';
        audioBtn.classList.toggle('is-playing', playing);
        audioBtn.classList.toggle('is-paused', !playing);
      };

      audioBtn.addEventListener('click', async () => {
        if (audioEl.paused) {
          try {
            await audioEl.play();
          } catch (err) {
            // ignore autoplay block
          }
        } else {
          audioEl.pause();
        }
        updateAudioState();
      });

      audioEl.addEventListener('play', updateAudioState);
      audioEl.addEventListener('pause', updateAudioState);

      audioWrap.appendChild(audioInfo);
      audioWrap.appendChild(audioBtn);
      audioWrap.appendChild(audioEl);
      copy.appendChild(audioWrap);
    }

    const shouldShowAudioList =
      reelData.audioTracks.length > 1 || (!reelData.media.length && reelData.audioTracks.length);
    const shouldShowAttachments =
      reelMode !== 'studio' &&
      (reelData.documents.length || reelData.otherFiles.length || shouldShowAudioList);

    if (shouldShowAttachments) {
      const attachmentsWrap = document.createElement('div');
      attachmentsWrap.className = 'reel-attachments';
      const attachmentsTitle = document.createElement('div');
      attachmentsTitle.className = 'reel-attachments-title';
      attachmentsTitle.textContent = 'Attachments';
      attachmentsWrap.appendChild(attachmentsTitle);

      const attachmentsGrid = document.createElement('div');
      attachmentsGrid.className = 'reel-grid';
      attachmentsWrap.appendChild(attachmentsGrid);

      const appendAttachmentItem = (item, label) => {
        const card = document.createElement('div');
        card.className = 'reel-item';
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = label;
        card.appendChild(tag);

        const mediaType = normalizeMediaType(item) || 'file';
        const mediaLabel = getMediaLabel(item) || label;
        const mediaNode = createMediaNode(mediaType, getMediaUrl(item), {
          alt: mediaLabel,
          title: mediaLabel,
          preview: item.preview_url || '',
          className: 'reel-item-media',
        });

        if (mediaNode) {
          card.appendChild(mediaNode);
        } else {
          const link = document.createElement('a');
          link.href = getMediaUrl(item);
          link.textContent = mediaLabel || 'Open file';
          link.className = 'text-link';
          link.target = '_blank';
          link.rel = 'noopener';
          card.appendChild(link);
        }

        const title = document.createElement('div');
        title.className = 'reel-item-title';
        title.textContent = mediaLabel;
        card.appendChild(title);
        attachmentsGrid.appendChild(card);
      };

      reelData.documents.forEach((item) => appendAttachmentItem(item, 'PDF'));
      if (shouldShowAudioList) {
        reelData.audioTracks.forEach((item) => appendAttachmentItem(item, 'Audio'));
      }
      reelData.otherFiles.forEach((item) => appendAttachmentItem(item, 'File'));

      copy.appendChild(attachmentsWrap);
    }

    stage.appendChild(media);
    stage.appendChild(copy);
    shell.appendChild(stage);

    const slideCount = slides.length;
    if (slideCount > 1) {
      const counter = document.createElement('div');
      counter.className = 'reel-counter';
      counter.textContent = `1 / ${slideCount}`;
      media.appendChild(counter);

      const prevBtn = document.createElement('button');
      prevBtn.type = 'button';
      prevBtn.className = 'reel-nav reel-nav--prev';
      prevBtn.textContent = 'Prev';
      const nextBtn = document.createElement('button');
      nextBtn.type = 'button';
      nextBtn.className = 'reel-nav reel-nav--next';
      nextBtn.textContent = 'Next';
      media.appendChild(prevBtn);
      media.appendChild(nextBtn);

      const dots = document.createElement('div');
      dots.className = 'reel-dots';
      const dotItems = [];
      for (let i = 0; i < slideCount; i += 1) {
        const dot = document.createElement('button');
        dot.type = 'button';
        dot.className = 'reel-dot';
        dot.setAttribute('aria-label', `Go to slide ${i + 1}`);
        if (i === 0) {
          dot.classList.add('is-active');
        }
        dots.appendChild(dot);
        dotItems.push(dot);
        dot.addEventListener('click', () => {
          track.scrollTo({ left: i * track.clientWidth, behavior: 'smooth' });
        });
      }
      media.appendChild(dots);

      const pauseInactiveVideos = (activeIndex) => {
        qsa('video', track).forEach((video, index) => {
          if (index !== activeIndex) {
            video.pause();
          }
        });
      };

      let activeIndex = 0;
      let scrollFrame = null;
      const updateActive = (nextIndex) => {
        const clamped = Math.max(0, Math.min(slideCount - 1, nextIndex));
        activeIndex = clamped;
        dotItems.forEach((dot, index) => dot.classList.toggle('is-active', index === clamped));
        counter.textContent = `${clamped + 1} / ${slideCount}`;
        pauseInactiveVideos(clamped);
      };

      prevBtn.addEventListener('click', () => {
        track.scrollTo({ left: (activeIndex - 1) * track.clientWidth, behavior: 'smooth' });
      });

      nextBtn.addEventListener('click', () => {
        track.scrollTo({ left: (activeIndex + 1) * track.clientWidth, behavior: 'smooth' });
      });

      track.addEventListener('scroll', () => {
        if (scrollFrame) return;
        scrollFrame = window.requestAnimationFrame(() => {
          const width = track.clientWidth || 1;
          const nextIndex = Math.round(track.scrollLeft / width);
          updateActive(nextIndex);
          scrollFrame = null;
        });
      });
    }

    if (audioEl) {
      media.addEventListener(
        'pointerdown',
        () => {
          if (audioEl.paused) {
            audioEl.play().catch(() => {});
          }
        },
        { once: true }
      );
    }

    container.appendChild(shell);
    return reelData;
  };

  const initNav = () => {
    const toggle = qs('[data-nav-toggle]');
    const panel = qs('#nav-panel');
    if (!toggle || !panel) return;
    toggle.addEventListener('click', () => {
      const isOpen = panel.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });
  };

  const initHome = () => {
    const quickWrap = qs('#quick-links');
    const friendWrap = qs('#friend-links');
    const todayBtn = qs('[data-today-load]');
    const todayCard = qs('#today-card');

    const loadLinks = async () => {
      try {
        const data = await apiFetch('/api/site/links');
        if (quickWrap) {
          quickWrap.innerHTML = '';
          if (!data.quick.length) {
            quickWrap.innerHTML = '<div class="card placeholder">No quick links yet.</div>';
          } else {
            data.quick.forEach((link) => {
              const card = document.createElement('a');
              card.className = 'card';
              card.href = link.url;
              card.target = '_blank';
              card.rel = 'noopener';
              card.innerHTML = `<h3>${link.title}</h3><p class="muted">${link.url}</p>`;
              quickWrap.appendChild(card);
            });
          }
        }
        if (friendWrap) {
          friendWrap.innerHTML = '';
          if (!data.friends.length) {
            friendWrap.innerHTML = '<div class="card placeholder">No friend links yet.</div>';
          } else {
            data.friends.forEach((link) => {
              const card = document.createElement('div');
              card.className = 'card';
              const avatar = link.avatar_url ? `<img src="${link.avatar_url}" alt="${link.title}" />` : '';
              card.innerHTML = `${avatar}<h3>${link.title}</h3><p class="muted">${link.description || link.url}</p><a class="text-link" href="${link.url}" target="_blank" rel="noopener">Visit</a>`;
              friendWrap.appendChild(card);
            });
          }
        }
      } catch (err) {
        // ignore for now
      }
    };

    const loadToday = async () => {
      if (!todayCard) return;
      const date = formatDate(new Date());
      try {
        const data = await apiFetch(`/api/everyday/day?date=${date}`);
        const entry = data.entry || {};
        const text = entry.text || 'No text yet.';
        const count = (entry.attachments || []).length;
        todayCard.innerHTML = `
          <h3>${date}</h3>
          <p class="muted">${text.slice(0, 120)}</p>
          <p class="muted">${count} attachments</p>
        `;
      } catch (err) {
        todayCard.innerHTML = '<p class="muted">Unable to load today.</p>';
      }
    };

    if (todayBtn) {
      todayBtn.addEventListener('click', loadToday);
    }

    loadLinks();
    loadToday();
  };

  const initMarkdownPage = (moduleName) => {
    const listEl = qs(`#${moduleName}-list`);
    const searchEl = qs(`#${moduleName}-search`);
    const titleEl = qs(`#${moduleName}-title`);
    const metaEl = qs(`#${moduleName}-meta`);
    const contentEl = qs(`#${moduleName}-content`);
    const attachEl = qs(`#${moduleName}-attachments`);
    const refreshBtn = qs('[data-refresh-list]');

    let items = [];

    const renderList = (filter = '') => {
      if (!listEl) return;
      listEl.innerHTML = '';
      const filtered = items.filter((item) => item.title.toLowerCase().includes(filter.toLowerCase()));
      if (!filtered.length) {
        listEl.innerHTML = '<div class="card placeholder">No items found.</div>';
        return;
      }
      filtered.forEach((item) => {
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'list-item';
        el.dataset.key = item.key;
        el.innerHTML = `<strong>${item.title}</strong><span class="muted">${item.key}</span>`;
        el.addEventListener('click', () => {
          loadItem(item.key);
        });
        listEl.appendChild(el);
      });
    };

    const loadList = async () => {
      if (!listEl) return;
      listEl.innerHTML = '<div class="card placeholder">Loading...</div>';
      try {
        const data = await apiFetch(`/api/${moduleName}`);
        items = data.items || [];
        renderList(searchEl ? searchEl.value : '');
        const params = new URLSearchParams(window.location.search);
        const preselect = params.get('key');
        if (preselect) {
          loadItem(preselect);
        } else if (items.length) {
          loadItem(items[0].key);
        }
      } catch (err) {
        listEl.innerHTML = `<div class="card placeholder">${err.message}</div>`;
      }
    };

    const loadItem = async (key) => {
      if (!contentEl) return;
      qsa('.list-item', listEl).forEach((el) => {
        el.classList.toggle('is-active', el.dataset.key === key);
      });
      try {
        await renderMarkdownWindow(moduleName, key, moduleName);
        const params = new URLSearchParams(window.location.search);
        params.set('key', key);
        history.replaceState(null, '', `${window.location.pathname}?${params}`);
      } catch (err) {
        contentEl.innerHTML = `<p class="muted">${err.message}</p>`;
      }
    };

    if (searchEl) {
      searchEl.addEventListener('input', (event) => {
        renderList(event.target.value);
      });
    }

    if (refreshBtn) {
      refreshBtn.addEventListener('click', loadList);
    }

    loadList();
  };

  const initEverydayManage = () => {
    const dateInput = qs('#everyday-date');
    const textInput = qs('#everyday-text');
    const captionInput = qs('#everyday-caption');
    const fileInput = qs('#everyday-file');
    const statusEl = qs('#everyday-status');
    const dayTitle = qs('#everyday-title');
    const dayMeta = qs('#everyday-meta');
    const attachEl = qs('#everyday-attachments');
    const reelWindow = qs('#everyday-reel-window');
    const monthList = qs('#everyday-month-list');
    const monthLabel = qs('#everyday-month-label');
    const calendarLabel = qs('#calendar-label');
    const calendarGrid = qs('#calendar-grid');
    const prevBtn = qs('[data-calendar-prev]');
    const nextBtn = qs('[data-calendar-next]');
    const todayBtn = qs('[data-calendar-today]');
    const saveTextBtn = qs('[data-save-text]');
    const uploadBtn = qs('[data-upload-file]');
    const refreshBtn = qs('[data-refresh-day]');

    const state = {
      current: new Date(),
      days: {},
      selected: null,
    };

    state.current.setDate(1);

    const monthKey = () => {
      const year = state.current.getFullYear();
      const month = String(state.current.getMonth() + 1).padStart(2, '0');
      return `${year}-${month}`;
    };

    const setStatus = (msg) => {
      if (statusEl) statusEl.textContent = msg;
    };

    const updateSelected = (dateStr) => {
      if (dateInput) dateInput.value = dateStr;
      state.selected = dateStr;
    };

    const deleteAttachment = async (attachment) => {
      if (!attachment || !attachment.uuid) {
        throw new Error('Missing attachment id');
      }
      const dateStr = dateInput ? dateInput.value : '';
      if (!dateStr) {
        throw new Error('Missing date');
      }
      setStatus('Deleting attachment...');
      try {
        const data = await apiFetch('/api/everyday/attachment', {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date: dateStr, uuid: attachment.uuid }),
        });
        renderDay(dateStr, data.entry || {});
        loadMonth();
        setStatus('Attachment deleted');
        return data;
      } catch (err) {
        setStatus(err.message);
        throw err;
      }
    };

    const renderDay = (dateStr, entry) => {
      updateSelected(dateStr);
      if (dayTitle) dayTitle.textContent = dateStr;
      const reelData = renderReelWindow(entry, reelWindow);
      if (dayMeta) {
        dayMeta.textContent = '';
      }
      if (textInput) textInput.value = entry.text || '';
      renderAttachmentStrip(attachEl, entry.attachments || [], {
        allowDelete: true,
        onDelete: deleteAttachment,
      });
    };

    const renderMonthList = (days) => {
      if (!monthList) return;
      const keys = Object.keys(days).sort();
      monthList.innerHTML = '';
      if (!keys.length) {
        monthList.innerHTML = '<div class="card placeholder">No entries yet.</div>';
        return;
      }
      keys.forEach((key) => {
        const entry = days[key] || {};
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'list-item';
        const snippet = (entry.text || '').slice(0, 60) || 'No text';
        const count = (entry.attachments || []).length;
        card.innerHTML = `<strong>${key}</strong><span class="muted">${snippet}</span><span class="muted">${count} attachments</span>`;
        card.addEventListener('click', () => loadDay(key));
        monthList.appendChild(card);
      });
    };

    const loadDay = async (dateStr) => {
      if (!dateStr) return;
      updateSelected(dateStr);
      setStatus('Loading day...');
      try {
        const data = await apiFetch(`/api/everyday/day?date=${dateStr}`);
        renderDay(dateStr, data.entry || {});
        setStatus('Loaded');
        const params = new URLSearchParams(window.location.search);
        params.set('date', dateStr);
        history.replaceState(null, '', `${window.location.pathname}?${params}`);
      } catch (err) {
        setStatus(err.message);
      }
      renderCalendar();
    };

    const renderCalendar = () => {
      if (!calendarGrid) return;
      calendarGrid.innerHTML = '';
      const year = state.current.getFullYear();
      const month = state.current.getMonth();
      const first = new Date(year, month, 1);
      const startDay = first.getDay();
      const daysInMonth = new Date(year, month + 1, 0).getDate();

      for (let i = 0; i < startDay; i += 1) {
        const cell = document.createElement('div');
        cell.className = 'calendar-day is-muted';
        cell.textContent = '';
        calendarGrid.appendChild(cell);
      }

      for (let day = 1; day <= daysInMonth; day += 1) {
        const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'calendar-day';
        cell.textContent = String(day);
        cell.dataset.date = dateStr;
        if (state.days[dateStr]) {
          cell.classList.add('has-entry');
        }
        if (state.selected === dateStr) {
          cell.classList.add('is-active');
        }
        const today = formatDate(new Date());
        if (today === dateStr) {
          cell.classList.add('is-today');
        }
        cell.addEventListener('click', () => loadDay(dateStr));
        calendarGrid.appendChild(cell);
      }
    };

    const loadMonth = async () => {
      const labelText = monthKey();
      if (calendarLabel) calendarLabel.textContent = labelText;
      if (monthLabel) monthLabel.textContent = labelText;
      if (monthList) monthList.innerHTML = '<div class="card placeholder">Loading entries...</div>';
      try {
        const data = await apiFetch(`/api/everyday/month?month=${labelText}`);
        state.days = data.days || {};
      } catch (err) {
        state.days = {};
        if (monthList) {
          monthList.innerHTML = `<div class="card placeholder">${err.message}</div>`;
        }
        renderCalendar();
        return;
      }
      renderCalendar();
      renderMonthList(state.days);
    };

    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        state.current.setMonth(state.current.getMonth() - 1);
        loadMonth();
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        state.current.setMonth(state.current.getMonth() + 1);
        loadMonth();
      });
    }

    if (todayBtn) {
      todayBtn.addEventListener('click', () => {
        const today = new Date();
        state.current = new Date(today.getFullYear(), today.getMonth(), 1);
        loadMonth();
        loadDay(formatDate(today));
      });
    }

    const params = new URLSearchParams(window.location.search);
    const initialDate = params.get('date');
    if (initialDate) {
      const init = new Date(initialDate);
      if (!Number.isNaN(init.getTime())) {
        state.current = new Date(init.getFullYear(), init.getMonth(), 1);
        loadMonth();
        loadDay(formatDate(init));
      } else {
        loadMonth();
        loadDay(formatDate(new Date()));
      }
    } else {
      loadMonth();
      loadDay(formatDate(new Date()));
    }

    if (saveTextBtn) {
      saveTextBtn.addEventListener('click', async () => {
        const dateStr = dateInput ? dateInput.value : '';
        if (!dateStr) return;
        setStatus('Saving text...');
        try {
          const data = await apiFetch('/api/everyday/text', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date: dateStr, text: textInput ? textInput.value : '' }),
          });
          renderDay(dateStr, data.entry || {});
          setStatus('Saved');
          loadMonth();
        } catch (err) {
          setStatus(err.message);
        }
      });
    }

    const uploadViaServer = async (dateStr, file, caption) => {
      const form = new FormData();
      form.append('date', dateStr);
      form.append('caption', caption || '');
      form.append('file', file);
      const data = await apiFetch('/api/everyday/upload', { method: 'POST', body: form });
      return data;
    };

    if (uploadBtn) {
      uploadBtn.addEventListener('click', async () => {
        const dateStr = dateInput ? dateInput.value : '';
        if (!dateStr) return;
        if (!fileInput || !fileInput.files.length) {
          setStatus('Select a file');
          return;
        }
        const file = fileInput.files[0];
        let data = null;
        const caption = captionInput ? captionInput.value : '';
        let presign = null;
        try {
          setStatus('Preparing direct upload...');
          presign = await apiFetch('/api/everyday/upload/presign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              date: dateStr,
              filename: file.name,
              content_type: file.type || 'application/octet-stream',
            }),
          });
          setStatus('Uploading direct...');
          const uploadResp = await fetch(presign.upload_url, {
            method: 'PUT',
            headers: presign.headers || {},
            body: file,
          });
          if (!uploadResp.ok) {
            throw new Error('Direct upload failed');
          }
        } catch (err) {
          setStatus('Direct upload failed, fallback to server...');
          try {
            data = await uploadViaServer(dateStr, file, caption);
          } catch (fallbackErr) {
            setStatus(fallbackErr.message);
            return;
          }
        }
        if (!data && presign) {
          setStatus('Finalizing...');
          try {
            data = await apiFetch('/api/everyday/upload/commit', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                date: dateStr,
                uuid: presign.uuid,
                oss_key: presign.oss_key,
                caption,
                media_type: presign.media_type,
              }),
            });
          } catch (err) {
            setStatus(err.message);
            return;
          }
        }
        renderDay(dateStr, data.entry || {});
        setStatus('Uploaded');
        if (captionInput) captionInput.value = '';
        if (fileInput) fileInput.value = '';
        loadMonth();
      });
    }

    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        if (dateInput) loadDay(dateInput.value);
      });
    }
  };

  const initEverydayView = () => {
    const label = qs('#calendar-label');
    const grid = qs('#calendar-grid');
    const prevBtn = qs('[data-calendar-prev]');
    const nextBtn = qs('[data-calendar-next]');
    const todayBtn = qs('[data-calendar-today]');
    const viewerDate = qs('#viewer-date');
    const viewerMeta = qs('#viewer-meta');
    const reelWindow = qs('#viewer-reel-window');

    const state = {
      current: new Date(),
      days: {},
      selected: null,
    };

    state.current.setDate(1);

    const monthKey = () => {
      const year = state.current.getFullYear();
      const month = String(state.current.getMonth() + 1).padStart(2, '0');
      return `${year}-${month}`;
    };

    const setSelected = (dateStr) => {
      state.selected = dateStr;
    };

    const renderViewer = (dateStr, entry) => {
      if (viewerDate) viewerDate.textContent = dateStr || 'Select a day';
      const reelData = renderReelWindow(entry, reelWindow);
      if (viewerMeta) {
        viewerMeta.textContent = '';
      }
    };

    const loadDay = async (dateStr) => {
      if (!dateStr) return;
      setSelected(dateStr);
      if (reelWindow) reelWindow.innerHTML = '<div class="card placeholder">Loading reel...</div>';
      try {
        const data = await apiFetch(`/api/everyday/day?date=${dateStr}`);
        renderViewer(dateStr, data.entry || {});
        const params = new URLSearchParams(window.location.search);
        params.set('date', dateStr);
        history.replaceState(null, '', `${window.location.pathname}?${params}`);
      } catch (err) {
        if (reelWindow) reelWindow.innerHTML = `<div class="card placeholder">${err.message}</div>`;
        if (viewerMeta) viewerMeta.textContent = '';
      }
      renderCalendar();
    };

    const renderCalendar = () => {
      if (!grid) return;
      grid.innerHTML = '';
      const year = state.current.getFullYear();
      const month = state.current.getMonth();
      const first = new Date(year, month, 1);
      const startDay = first.getDay();
      const daysInMonth = new Date(year, month + 1, 0).getDate();

      for (let i = 0; i < startDay; i += 1) {
        const cell = document.createElement('div');
        cell.className = 'calendar-day is-muted';
        cell.textContent = '';
        grid.appendChild(cell);
      }

      for (let day = 1; day <= daysInMonth; day += 1) {
        const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'calendar-day';
        cell.textContent = String(day);
        cell.dataset.date = dateStr;
        if (state.days[dateStr]) {
          cell.classList.add('has-entry');
        }
        if (state.selected === dateStr) {
          cell.classList.add('is-active');
        }
        const today = formatDate(new Date());
        if (today === dateStr) {
          cell.classList.add('is-today');
        }
        cell.addEventListener('click', () => loadDay(dateStr));
        grid.appendChild(cell);
      }
    };

    const loadMonth = async () => {
      const labelText = monthKey();
      if (label) label.textContent = labelText;
      state.days = {};
      try {
        const data = await apiFetch(`/api/everyday/month?month=${labelText}`);
        state.days = data.days || {};
      } catch (err) {
        state.days = {};
      }
      renderCalendar();
    };

    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        state.current.setMonth(state.current.getMonth() - 1);
        loadMonth();
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        state.current.setMonth(state.current.getMonth() + 1);
        loadMonth();
      });
    }

    if (todayBtn) {
      todayBtn.addEventListener('click', () => {
        const today = new Date();
        state.current = new Date(today.getFullYear(), today.getMonth(), 1);
        loadMonth();
        loadDay(formatDate(today));
      });
    }

    const params = new URLSearchParams(window.location.search);
    const initialDate = params.get('date');
    if (initialDate) {
      const init = new Date(initialDate);
      if (!Number.isNaN(init.getTime())) {
        state.current = new Date(init.getFullYear(), init.getMonth(), 1);
        loadMonth();
        loadDay(formatDate(init));
        return;
      }
    }
    loadMonth();
    loadDay(formatDate(new Date()));
  };

  const initAlbum = () => {
    const moduleSelect = qs('#album-module');
    const typeSelect = qs('#album-type');
    const loadBtn = qs('[data-album-load]');
    const grid = qs('#album-grid');
    const status = qs('[data-album-status]');
    const moreBtn = qs('[data-album-more]');
    const sentinel = qs('[data-album-sentinel]');
    const modal = qs('#resource-modal');
    const modalContent = qs('#resource-modal-content');
    const blogTemplate = qs('#modal-blog-template');
    const noteTemplate = qs('#modal-note-template');
    const dailyreelTemplate = qs('#modal-dailyreel-template');
    if (!grid) return;

    const supportsObserver = 'IntersectionObserver' in window;
    let streamObserver = null;
    let offset = 0;
    const limit = 120;
    let loading = false;
    let hasMore = true;

    const setStatus = (text) => {
      if (status) {
        status.textContent = text || '';
      }
    };

    const updateFooter = () => {
      if (loading) {
        setStatus('Loading...');
      } else if (!hasMore) {
        setStatus('No more attachments.');
      } else {
        setStatus('Scroll to load more.');
      }

      if (moreBtn) {
        moreBtn.hidden = supportsObserver || loading || !hasMore;
      }
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

    const resizeAllGridItems = () => {
      qsa('.album-item', grid).forEach((item) => resizeGridItem(item));
    };

    const showModalMessage = (message) => {
      if (!modalContent) return;
      modalContent.innerHTML = `<div class="card placeholder">${message}</div>`;
    };

    const closeModal = () => {
      if (!modal) return;
      modal.hidden = true;
      document.body.classList.remove('modal-open');
      if (modalContent) {
        modalContent.innerHTML = '';
      }
    };

    const resolveSourceId = async (item) => {
      if (item.source_id) {
        return item.source_id;
      }
      if (!item.uuid || (item.source_module !== 'blog' && item.source_module !== 'note')) {
        return null;
      }
      const data = await apiFetch(
        `/api/album/source?module=${item.source_module}&uuid=${encodeURIComponent(item.uuid)}`
      );
      if (data.source_id) {
        item.source_id = data.source_id;
      }
      return data.source_id || null;
    };

    const openModal = async (item) => {
      if (!modal || !modalContent) return;
      let template = null;
      let prefix = '';
      if (item.source_module === 'blog') {
        template = blogTemplate;
        prefix = 'modal-blog';
      } else if (item.source_module === 'note') {
        template = noteTemplate;
        prefix = 'modal-note';
      } else if (item.source_module === 'everyday') {
        template = dailyreelTemplate;
      }
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
        if (item.source_module === 'blog' || item.source_module === 'note') {
          const sourceId = await resolveSourceId(item);
          if (!sourceId) {
            showModalMessage('Source entry not found.');
            return;
          }
          await renderMarkdownWindow(item.source_module, sourceId, prefix, modalContent);
          return;
        }
        if (item.source_module === 'everyday') {
          const dateStr = item.source_id;
          if (!dateStr) {
            showModalMessage('Missing daily reel date.');
            return;
          }
          const titleEl = qs('#modal-dailyreel-title', modalContent);
          if (titleEl) {
            titleEl.textContent = dateStr;
          }
          const reelEl = qs('#modal-dailyreel-window', modalContent);
          if (reelEl) {
            reelEl.innerHTML = '<div class="card placeholder">Loading reel...</div>';
          }
          const data = await apiFetch(`/api/everyday/day?date=${dateStr}`);
          if (reelEl) {
            renderReelWindow(data.entry || {}, reelEl);
          }
        }
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
      const displayName = item.oss_key ? item.oss_key.split('/').pop() : item.uuid;
      const imagePreviewUrl = item.preview_url || '';
      const imageUrl = item.url || '';
      const videoPreviewUrl = item.preview_url || '';
      const resolvedType = resolveMediaType(item, item.media_type || 'file');
      const typeLabel = resolvedType || item.media_type || 'file';
      const moduleLabel = item.source_module || 'attachment';
      const labelParts = [`Open ${typeLabel} from ${moduleLabel}`];
      if (displayName) {
        labelParts.push(displayName);
      }
      wrapper.setAttribute('aria-label', labelParts.join(': '));

      if (resolvedType === 'image') {
        const img = document.createElement('img');
        img.alt = displayName || item.uuid;
        img.decoding = 'async';
        img.src = imagePreviewUrl || imageUrl;
        if (imagePreviewUrl && imageUrl && imagePreviewUrl !== imageUrl) {
          img.addEventListener(
            'error',
            () => {
              img.src = imageUrl;
            },
            { once: true }
          );
        }
        img.addEventListener('load', () => resizeGridItem(wrapper), { once: true });
        preview.appendChild(img);
      } else if (resolvedType === 'video') {
        if (videoPreviewUrl) {
          const img = document.createElement('img');
          img.alt = displayName || item.uuid;
          img.decoding = 'async';
          img.src = videoPreviewUrl;
          img.addEventListener('load', () => resizeGridItem(wrapper), { once: true });
          preview.appendChild(img);
        } else {
          const fallback = document.createElement('div');
          fallback.className = 'album-fallback';
          fallback.textContent = 'Video preview unavailable';
          preview.appendChild(fallback);
        }
      } else if (resolvedType === 'audio') {
        const fallback = document.createElement('div');
        fallback.className = 'album-fallback album-fallback--audio';
        fallback.textContent = displayName || 'Audio file';
        preview.appendChild(fallback);
      } else if (resolvedType === 'pdf') {
        const cover = createPdfCover(imageUrl, {
          title: displayName || item.uuid || 'PDF document',
          preview: imagePreviewUrl,
          asLink: false,
          showOpen: false,
        });
        if (cover) {
          preview.appendChild(cover);
        } else {
          const fallback = document.createElement('div');
          fallback.className = 'album-fallback album-fallback--pdf';
          fallback.textContent = displayName || 'PDF document';
          preview.appendChild(fallback);
        }
      } else {
        const fallback = document.createElement('div');
        fallback.className = 'album-fallback album-fallback--file';
        const label = document.createElement('span');
        label.textContent = displayName || item.uuid;
        fallback.appendChild(label);
        preview.appendChild(fallback);
      }

      const badges = document.createElement('div');
      badges.className = 'album-badges';
      const typeBadge = document.createElement('span');
      typeBadge.className = 'album-badge';
      typeBadge.textContent = typeLabel;
      badges.appendChild(typeBadge);
      if (moduleLabel) {
        const moduleBadge = document.createElement('span');
        moduleBadge.className = 'album-badge';
        moduleBadge.textContent = moduleLabel;
        badges.appendChild(moduleBadge);
      }
      preview.appendChild(badges);
      card.appendChild(preview);
      wrapper.appendChild(card);
      wrapper.addEventListener('click', () => openModal(item));
      requestAnimationFrame(() => resizeGridItem(wrapper));
      return wrapper;
    };

    const appendItems = (items) => {
      if (!items.length) {
        return;
      }
      const empty = qs('.album-empty', grid);
      if (empty) {
        empty.remove();
      }
      const nodes = items.map((item) => buildCard(item));
      nodes.forEach((node) => grid.appendChild(node));
      requestAnimationFrame(() => resizeAllGridItems());
    };

    const showEmpty = (message) => {
      grid.innerHTML = `<div class="album-empty">${message}</div>`;
    };

    const loadAlbum = async (reset = false) => {
      if (loading || (!hasMore && !reset)) {
        return;
      }
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
      params.set('limit', limit);
      params.set('offset', offset);

      try {
        const data = await apiFetch(`/api/album?${params.toString()}`);
        const items = data.items || [];
        if (!items.length) {
          if (offset === 0) {
            showEmpty('No attachments found.');
          }
          hasMore = false;
          return;
        }
        appendItems(items);
        offset += limit;
        hasMore = items.length > 0;
      } catch (err) {
        showEmpty(err.message);
        hasMore = false;
      } finally {
        loading = false;
        updateFooter();
      }
    };

    if (loadBtn) {
      loadBtn.addEventListener('click', () => loadAlbum(true));
    }

    if (moduleSelect) {
      moduleSelect.addEventListener('change', () => loadAlbum(true));
    }

    if (typeSelect) {
      typeSelect.addEventListener('change', () => loadAlbum(true));
    }

    if (moreBtn) {
      moreBtn.addEventListener('click', () => loadAlbum());
    }

    if (supportsObserver && sentinel) {
      streamObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              loadAlbum();
            }
          });
        },
        { rootMargin: '300px 0px' }
      );
      streamObserver.observe(sentinel);
    } else if (moreBtn) {
      moreBtn.hidden = false;
    }

    if (modal) {
      modal.addEventListener('click', (event) => {
        if (event.target && event.target.matches('[data-modal-close]')) {
          closeModal();
        }
      });
    }

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeModal();
      }
    });

    window.addEventListener('resize', () => resizeAllGridItems());

    loadAlbum(true);
  };

  initNav();

  if (page === 'home') initHome();
  if (page === 'blog') initMarkdownPage('blog');
  if (page === 'note') initMarkdownPage('note');
  if (page === 'dailyreel-manage') initEverydayManage();
  if (page === 'dailyreel-view') initEverydayView();
  if (page === 'everyday') initAlbum();
})();
