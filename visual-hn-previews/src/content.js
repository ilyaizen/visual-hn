// content.js — orchestrates: scan stories -> batch fetch -> inject ONE thumbnail
// per story, with hover preview card, click lightbox, hcker.news settings-panel
// controls, and a keyboard shortcut. Runs at document_idle on hcker.news.

(function () {
  const HANDLED_ATTR = 'data-vhn-thumb'; // marks an injected story container
  // imageSize: 'xs' (small fixed column) | 'md' (medium fixed column) | 'large' (block above title)
  const DEFAULT_SETTINGS = { enabled: true, apiBase: '', imageSize: 'md', aspectRatio: 'landscape', showFavicons: true, showDescriptions: true, showHoverPreview: false, showRankBadges: true };
  const WEB_DEFAULTS = window.VHN_WEB_DEFAULTS || {};
  const hasChromeStorage =
    typeof chrome !== 'undefined' && chrome.storage && chrome.storage.sync;

  let settings = { ...DEFAULT_SETTINGS, ...WEB_DEFAULTS };
  let scanScheduled = false;
  let observer = null;

  // Live coverage counters for the status line.
  const stats = { matched: 0, loaded: 0, apiOk: null };

  // ---------------------------------------------------------------- settings
  async function loadSettings() {
    try {
      if (hasChromeStorage) {
        const stored = await chrome.storage.sync.get({ ...DEFAULT_SETTINGS, ...WEB_DEFAULTS });
        settings = { ...DEFAULT_SETTINGS, ...WEB_DEFAULTS, ...stored };
        if (WEB_DEFAULTS.apiBase) settings.apiBase = WEB_DEFAULTS.apiBase;
        return;
      }
      const raw = window.localStorage && window.localStorage.getItem('vhn-preview-settings');
      const stored = raw ? JSON.parse(raw) : {};
      settings = { ...DEFAULT_SETTINGS, ...WEB_DEFAULTS, ...stored };
      if (WEB_DEFAULTS.apiBase) settings.apiBase = WEB_DEFAULTS.apiBase;
    } catch (e) {
      settings = { ...DEFAULT_SETTINGS, ...WEB_DEFAULTS };
    }
  }

  async function saveSetting(key, value) {
    try {
      if (hasChromeStorage) {
        await chrome.storage.sync.set({ [key]: value });
        return;
      }
      const raw = window.localStorage && window.localStorage.getItem('vhn-preview-settings');
      const stored = raw ? JSON.parse(raw) : {};
      stored[key] = value;
      window.localStorage && window.localStorage.setItem('vhn-preview-settings', JSON.stringify(stored));
    } catch (e) {
      /* storage may be unavailable; in-memory state still applies */
    }
  }

  function applyEnabledState() {
    document.documentElement.classList.toggle('vhn-disabled', !settings.enabled);
    if (settings.enabled) scheduleScan();
  }

  // ---------------------------------------------------------------- thumbnail
  // opts: { large: bool, storyHref: string|null, title: Element|null }
  //  large -> <a> block above title; click opens the story link; hovering it
  //           highlights the title (shared hover group); NO zoom modal/preview.
  //  xs    -> small fixed column thumb; hover preview card; click opens the
  //           zoom lightbox.
  function buildThumb(entry, opts) {
    const large = opts.large;
    const wrap = document.createElement(large ? 'a' : 'span');
    wrap.className = 'vhn-thumb-wrap ' + (large ? 'vhn-large' : 'vhn-' + settings.imageSize) + ' vhn-ar-' + settings.aspectRatio;
    if (large && opts.storyHref) wrap.href = opts.storyHref;

    // Clipped frame so the 10% zoom (scale 1.1) is cropped to the 16:9 box
    // without overflowing — and without clipping the absolute hover preview,
    // which lives on the wrap, not the frame.
    const frame = document.createElement('span');
    frame.className = 'vhn-thumb-frame';

    const thumb = document.createElement('img');
    thumb.className = 'vhn-thumb';
    thumb.src = entry.image_url;
    thumb.alt = entry.title || '';
    thumb.loading = 'lazy';
    if (!large) thumb.title = 'Click to enlarge';

    if (large) {
      // Image is part of the title's hover group: hovering it activates the
      // title, and clicking navigates to the story (native <a> href).
      // Also: hovering the title should zoom the image and underline the title.
      if (opts.title) {
        wrap.addEventListener('mouseenter', () => {
          opts.title.classList.add('vhn-title-hot');
          wrap.classList.add('vhn-title-hovered');
        });
        wrap.addEventListener('mouseleave', () => {
          opts.title.classList.remove('vhn-title-hot');
          wrap.classList.remove('vhn-title-hovered');
        });
        opts.title.addEventListener('mouseenter', () => {
          opts.title.classList.add('vhn-title-hot');
          wrap.classList.add('vhn-title-hovered');
        });
        opts.title.addEventListener('mouseleave', () => {
          opts.title.classList.remove('vhn-title-hot');
          wrap.classList.remove('vhn-title-hovered');
        });
      }
    } else {
      // Larger floating preview card on hover (xs only, CSS-driven).
      const preview = document.createElement('span');
      preview.className = 'vhn-preview';
      const pimg = document.createElement('img');
      pimg.className = 'vhn-preview-img';
      pimg.src = entry.image_url;
      pimg.alt = '';
      pimg.loading = 'lazy';
      preview.appendChild(pimg);

      // favicon + domain caption (empty span collapses via CSS when no domain).
      const meta = document.createElement('span');
      meta.className = 'vhn-preview-meta';
      if (entry.domain) {
        if (entry.favicon) {
          const fav = document.createElement('img');
          fav.className = 'vhn-favicon';
          fav.src = entry.favicon;
          fav.alt = '';
          fav.loading = 'lazy';
          meta.appendChild(fav);
        }
        const dom = document.createElement('span');
        dom.className = 'vhn-preview-domain';
        dom.textContent = entry.domain;
        meta.appendChild(dom);
      }
      preview.appendChild(meta);
      wrap._preview = preview; // appended after the frame below

      thumb.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openModal(entry);
      });
    }

    // A broken remote og:image (dead URL, non-image content) → swap in a
    // spacer so the row keeps its image column width instead of collapsing.
    thumb.addEventListener('error', () => {
      thumb.remove();
      var spacer = document.createElement('span');
      spacer.className = 'vhn-thumb-spacer';
      frame.appendChild(spacer);
      if (wrap._preview) wrap._preview.remove();
    });

    frame.appendChild(thumb);
    wrap.appendChild(frame);
    if (wrap._preview) wrap.appendChild(wrap._preview);
    return wrap;
  }

  // The favicon before the title, wrapped in an inverse-theme circle badge.
  // The badge background uses var(--text-color) — hcker.news's own theme
  // variable which is always the inverse luminance of the page background
  // (#000 on light themes, #fff/#b8c1d1 on dark themes). This makes the badge
  // respond instantly to theme/mode changes WITHOUT requiring a re-injection
  // or page refresh, because CSS variables cascade live from body.
  function buildFavicon(entry) {
    const badge = document.createElement('span');
    badge.className = 'vhn-fav-badge';
    const fav = document.createElement('img');
    fav.className = 'vhn-title-favicon';
    fav.src = entry.favicon;
    fav.alt = '';
    fav.loading = 'lazy';
    fav.referrerPolicy = 'no-referrer';
    badge.appendChild(fav);
    return badge;
  }

  // The og/meta description on its own line under the title + url.
  function buildDescription(entry) {
    const desc = document.createElement('div');
    desc.className = 'vhn-desc';
    desc.textContent = entry.description;
    return desc;
  }

  // Rank overlay for top-30 HN stories. Top-3 get trophy emoji (🥇🥈🥉);
  // ranks 4-30 get a bare number with a trend arrow only when the story is
  // actively moving (no marker for "same"). The server tracks each story's
  // front-page position (1-30) and movement since the last 15-min scrape;
  // we only badge stories with a real tracked rank.
  const RANK_MEDALS = ['\u{1F947}', '\u{1F948}', '\u{1F949}'];
  function buildRankBadge(entry) {
    const rank = entry && typeof entry.position === 'number' ? entry.position : 0;
    if (rank < 1 || rank > 30) return null;

    const badge = document.createElement('span');
    badge.className = 'vhn-rank-badge';

    if (rank <= 3) {
      badge.classList.add('vhn-rank-trophy');
      badge.setAttribute('data-rank', String(rank));
      badge.textContent = RANK_MEDALS[rank - 1];
      return badge;
    }

    const num = document.createElement('span');
    num.className = 'vhn-rank-num';
    num.textContent = String(rank);
    badge.appendChild(num);

    const trend = (entry && entry.trend) || 'same';
    if (trend === 'up' || trend === 'down') {
      const arrow = document.createElement('span');
      arrow.className = 'vhn-rank-trend vhn-trend-' + trend;
      arrow.textContent = trend === 'up' ? '\u25B2' : '\u25BC';
      badge.appendChild(arrow);
    }

    return badge;
  }

  // Placeholder that holds the image column's space when no image exists.
  function buildSpacer() {
    const wrap = document.createElement('span');
    const sizeClass = settings.imageSize === 'large' ? 'vhn-large' : 'vhn-' + settings.imageSize;
    wrap.className = 'vhn-thumb-wrap vhn-spacer ' + sizeClass + ' vhn-ar-' + settings.aspectRatio;
    const frame = document.createElement('span');
    frame.className = 'vhn-thumb-frame';
    const spacer = document.createElement('span');
    spacer.className = 'vhn-thumb-spacer';
    frame.appendChild(spacer);
    wrap.appendChild(frame);
    return wrap;
  }

  function injectInto(row, anchor, entry, position) {
    // Clean any stale VHN elements (idempotent — prevents duplicate images
    // when the SPA re-renders rows during comments sidebar open/close, where
    // old row elements briefly coexist with new ones in the DOM).
    _cleanRow(row);

    const title = window.VHN.titleAnchor(row, anchor);
    const host = window.VHN.titleHost(row, title);
    const large = settings.imageSize === 'large';
    const storyHref = title ? title.getAttribute('href') : null;

    const node = buildThumb(entry, { large, storyHref, title });

    // Rank chip for top-30 stories (number + trend arrow).
    // Use page-order rank (position param), not entry.position (stale server
    // data from 15-min-old scrape). Otherwise trophies can be jumbled — e.g.
    // two silvers and no gold — when page order has drifted from scrape order.
    if (settings.showRankBadges) {
      const badge = buildRankBadge({ ...entry, position: position });
      if (badge) node.appendChild(badge);
    }

    // textHost = where favicon/description land. In xs mode the site's own
    // children move into a right-hand text column so the thumb becomes a fixed
    // left column (flex row); in large mode they stay on the host.
    let textHost = host;
    if (large) {
      // Large mode: insert thumb AFTER the description (which is appended to textHost)
      // We'll insert it after textHost's content, effectively at the end of host
      // Favicon immediately before the title text (inline, in the title's line).
      if (entry.favicon && settings.showFavicons && title && title.parentElement) {
        title.parentElement.insertBefore(buildFavicon(entry), title);
      }

      // Description as a new line after the title/url.
      if (entry.description && settings.showDescriptions) {
        textHost.appendChild(buildDescription(entry));
      }

      // Image goes after description (at end of host)
      host.appendChild(node);
    } else {
      host.classList.add('vhn-xs-host');
      const text = document.createElement('div');
      text.className = 'vhn-xs-text';
      while (host.firstChild) text.appendChild(host.firstChild);
      // Small mode: image must be FIRST child for flex layout (left column)
      host.appendChild(node);
      host.appendChild(text);
      textHost = text;

      // Favicon immediately before the title text (inline, in the title's line).
      if (entry.favicon && settings.showFavicons && title && title.parentElement) {
        title.parentElement.insertBefore(buildFavicon(entry), title);
      }

      // Description as a new line after the title/url.
      if (entry.description && settings.showDescriptions) {
        textHost.appendChild(buildDescription(entry));
      }
    }
  }

  // Remove stale VHN elements from a row without disturbing the site's own
  // DOM. Used to make injection idempotent — safe to call on a fresh or
  // already-injected row.
  function _cleanRow(row) {
    row.querySelectorAll(
      '.vhn-thumb-wrap, .vhn-fav-badge, .vhn-desc, .vhn-rank-badge'
    ).forEach((n) => n.remove());
    const host = row.querySelector('.vhn-xs-host');
    if (host) {
      const text = host.querySelector('.vhn-xs-text');
      if (text) {
        while (text.firstChild) host.appendChild(text.firstChild);
        text.remove();
      }
      host.classList.remove('vhn-xs-host');
    }
  }

  function injectSpacer(row, rank) {
    // Idempotent: clean stale elements before injecting (same rationale as
    // injectInto — prevents duplicates on SPA re-render).
    _cleanRow(row);

    const title = window.VHN.titleAnchor(row);
    const host = window.VHN.titleHost(row, title);
    const large = settings.imageSize === 'large';

    row.setAttribute(HANDLED_ATTR, '1');

    let node;
    if (large) {
      node = buildSpacer();
      host.appendChild(node);
    } else {
      host.classList.add('vhn-xs-host');
      const text = document.createElement('div');
      text.className = 'vhn-xs-text';
      while (host.firstChild) text.appendChild(host.firstChild);
      node = buildSpacer();
      host.appendChild(node);
      host.appendChild(text);
    }

    // Badge the gray fallback with its front-page rank, mirroring real
    // thumbnails so position info is visible even without an image.
    if (settings.showRankBadges && rank) {
      const badge = buildRankBadge({ position: rank, trend: 'same' });
      if (badge) node.appendChild(badge);
    }
  }

  // Reverse injectInto for a row: remove our nodes and unwrap the xs text
  // column, restoring the site's original DOM so a re-scan injects cleanly in
  // whatever mode is now active.
  function removeInjections(row) {
    row.querySelectorAll('.vhn-thumb-wrap, .vhn-fav-badge, .vhn-desc, .vhn-rank-badge').forEach((n) => n.remove());
    row.querySelectorAll('.vhn-title-hot').forEach((n) => n.classList.remove('vhn-title-hot'));
    const host = row.querySelector('.vhn-xs-host');
    if (host) {
      const text = host.querySelector('.vhn-xs-text');
      if (text) {
        while (text.firstChild) host.appendChild(text.firstChild);
        text.remove();
      }
      host.classList.remove('vhn-xs-host');
    }
    row.removeAttribute(HANDLED_ATTR);
  }

  // Tear down every injected story and re-scan in the current settings.
  function reapplyInjections() {
    document.querySelectorAll('[' + HANDLED_ATTR + ']').forEach(removeInjections);
    stats.matched = 0;
    stats.loaded = 0;
    scheduleScan();
  }

  async function setSize(value) {
    if (settings.imageSize === value) return;
    settings.imageSize = value;
    await saveSetting('imageSize', value);
    reapplyInjections();
    renderVhnSettings();
  }

  async function setShowFavicons(value) {
    if (settings.showFavicons === value) return;
    settings.showFavicons = value;
    await saveSetting('showFavicons', value);
    reapplyInjections();
    renderVhnSettings();
  }

  async function setShowDescriptions(value) {
    if (settings.showDescriptions === value) return;
    settings.showDescriptions = value;
    await saveSetting('showDescriptions', value);
    reapplyInjections();
    renderVhnSettings();
  }

  async function setShowHoverPreview(value) {
    if (settings.showHoverPreview === value) return;
    settings.showHoverPreview = value;
    await saveSetting('showHoverPreview', value);
    applyHoverPreviewState();
    renderVhnSettings();
  }

  async function setShowRankBadges(value) {
    if (settings.showRankBadges === value) return;
    settings.showRankBadges = value;
    await saveSetting('showRankBadges', value);
    reapplyInjections();
    renderVhnSettings();
  }

  function applyHoverPreviewState() {
    document.documentElement.classList.toggle('vhn-hover-disabled', !settings.showHoverPreview);
  }

  async function setAspectRatio(value) {
    if (settings.aspectRatio === value) return;
    settings.aspectRatio = value;
    await saveSetting('aspectRatio', value);
    reapplyInjections();
    renderVhnSettings();
  }

  // ---------------------------------------------------------------- lightbox
  // Pan & zoom viewer ported from svelte-image-viewer's interaction model:
  // drag to pan, scroll to zoom, pinch-to-zoom, double-click to reset.
  // Smooth animation via requestAnimationFrame + lerp.
  let modalEl = null;
  let modalImg = null;
  let modalStage = null;
  let animFrame = null;

  // Animated state (lerped towards targets each frame).
  let ax = 0, ay = 0, as = 1;
  // Target state.
  let tx = 0, ty = 0, ts = 1;
  const LERP = 0.18;

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function updateTransform() {
    if (modalImg) {
      modalImg.style.transform = 'translate(' + ax + 'px,' + ay + 'px) scale(' + as + ')';
    }
  }

  function animLoop() {
    let dirty = false;
    if (Math.abs(ax - tx) > 0.05) { ax += (tx - ax) * LERP; dirty = true; } else { ax = tx; }
    if (Math.abs(ay - ty) > 0.05) { ay += (ty - ay) * LERP; dirty = true; } else { ay = ty; }
    if (Math.abs(as - ts) > 0.001) { as += (ts - as) * LERP; dirty = true; } else { as = ts; }
    updateTransform();
    if (dirty) animFrame = requestAnimationFrame(animLoop);
    else animFrame = null;
  }

  function startAnim() { if (!animFrame) animFrame = requestAnimationFrame(animLoop); }

  // Scale-to-fit: compute scale so the image fits inside the stage.
  function scaleToFit() {
    if (!modalImg || !modalStage || !modalImg.naturalWidth) return;
    const sw = modalStage.clientWidth;
    const sh = modalStage.clientHeight;
    const iw = modalImg.naturalWidth;
    const ih = modalImg.naturalHeight;
    ts = clamp(Math.min(sw / iw, sh / ih) * 0.92, 0.05, 8);
    tx = 0; ty = 0;
    startAnim();
  }

  // --- Pan & zoom interaction (ported from svelte-image-viewer) ---
  const pointers = new Map();
  let panInitX = 0, panInitY = 0, panBaseX = 0, panBaseY = 0;
  let pinchDist0 = 0, pinchScale0 = 1, pinchCX = 0, pinchCY = 0;
  let didDrag = false; // true once pointer moves >3px — suppresses click-to-close
  let ptrDownX = 0, ptrDownY = 0; // initial pointer position for click detection

  function ptrDist(a, b) { return Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY); }
  function ptrMid(a, b) { return [(a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2]; }

  // Check if a screen coordinate is over the rendered image (accounting for transform).
  function hitTestImage(sx, sy) {
    if (!modalImg || !modalStage || !modalImg.naturalWidth) return false;
    var r = modalStage.getBoundingClientRect();
    var iw = modalImg.naturalWidth * as;
    var ih = modalImg.naturalHeight * as;
    var cx = r.left + r.width * 0.5 + ax;
    var cy = r.top + r.height * 0.5 + ay;
    return sx >= cx - iw / 2 && sx <= cx + iw / 2 && sy >= cy - ih / 2 && sy <= cy + ih / 2;
  }

  function onPtrDown(e) {
    if (!modalEl || !modalEl.classList.contains('vhn-open')) return;
    if (e.target.closest('.vhn-modal-bar, .vhn-modal-close')) return;
    e.preventDefault();
    pointers.set(e.pointerId, e);
    modalStage.setPointerCapture(e.pointerId);
    didDrag = false;
    ptrDownX = e.clientX; ptrDownY = e.clientY;

    if (pointers.size === 1) {
      panBaseX = tx; panBaseY = ty;
      panInitX = e.clientX; panInitY = e.clientY;
      modalEl.classList.add('vhn-dragging');
    } else if (pointers.size === 2) {
      const [p1, p2] = Array.from(pointers.values());
      pinchDist0 = ptrDist(p1, p2);
      pinchScale0 = ts;
      panBaseX = tx; panBaseY = ty;
      [panInitX, panInitY] = ptrMid(p1, p2);
      const r = modalStage.getBoundingClientRect();
      pinchCX = (panInitX - r.left - r.width * 0.5 - panBaseX) / pinchScale0;
      pinchCY = (panInitY - r.top - r.height * 0.5 - panBaseY) / pinchScale0;
    }
  }

  function onPtrMove(e) {
    if (!pointers.has(e.pointerId)) return;
    e.preventDefault();
    pointers.set(e.pointerId, e);
    if (!didDrag && pointers.size === 1) {
      const dx = e.clientX - panInitX, dy = e.clientY - panInitY;
      if (dx * dx + dy * dy > 9) didDrag = true; // >3px threshold
    }

    if (pointers.size === 1) {
      const dx = e.clientX - panInitX;
      const dy = e.clientY - panInitY;
      tx = panBaseX + dx; ty = panBaseY + dy;
      startAnim();
    } else if (pointers.size === 2) {
      didDrag = true; // pinch gesture — never close
      const [p1, p2] = Array.from(pointers.values());
      const d = ptrDist(p1, p2);
      const ratio = d / pinchDist0;
      ts = clamp(pinchScale0 * ratio, 0.05, 8);
      const [mx, my] = ptrMid(p1, p2);
      const r = modalStage.getBoundingClientRect();
      tx = mx - r.left - r.width * 0.5 - ts * pinchCX;
      ty = my - r.top - r.height * 0.5 - ts * pinchCY;
      startAnim();
    }
  }

  function onPtrUp(e) {
    if (!pointers.has(e.pointerId)) return;
    e.preventDefault();
    pointers.delete(e.pointerId);
    try { modalStage.releasePointerCapture(e.pointerId); } catch (_) {}
    if (pointers.size === 0) {
      modalEl.classList.remove('vhn-dragging');
      if (!didDrag && !hitTestImage(ptrDownX, ptrDownY)) closeModal(); // click on empty space = close
    } else if (pointers.size === 1) {
      const p = Array.from(pointers.values())[0];
      panBaseX = tx; panBaseY = ty;
      panInitX = p.clientX; panInitY = p.clientY;
    }
  }

  function onWheel(e) {
    if (!modalEl || !modalEl.classList.contains('vhn-open')) return;
    if (e.target.closest('.vhn-modal-bar')) return;
    e.preventDefault();
    const delta = -e.deltaY;
    const factor = 1 + delta / 500;
    const ns = clamp(ts * factor, 0.05, 8);
    const adj = ns / ts;
    const r2 = modalStage.getBoundingClientRect();
    const cx = e.clientX - r2.left - r2.width * 0.5;
    const cy = e.clientY - r2.top - r2.height * 0.5;
    tx = cx - adj * (cx - tx);
    ty = cy - adj * (cy - ty);
    ts = ns;
    startAnim();
  }

  function onDblClick(e) {
    if (!modalEl || !modalEl.classList.contains('vhn-open')) return;
    if (e.target.closest('.vhn-modal-bar, .vhn-modal-close')) return;
    e.preventDefault();
    // If already zoomed in (>1.2x), reset to fit. Otherwise zoom to 2x at click point.
    if (as > 1.2) {
      scaleToFit();
    } else {
      const r = modalStage.getBoundingClientRect();
      const cx = e.clientX - r.left - r.width * 0.5;
      const cy = e.clientY - r.top - r.height * 0.5;
      ts = clamp(ts * 2.5, 0.05, 8);
      const adj2 = ts / as;
      tx = cx - adj2 * (cx - tx);
      ty = cy - adj2 * (cy - ty);
      startAnim();
    }
  }

  function ensureModal() {
    if (modalEl) return modalEl;
    modalEl = document.createElement('div');
    modalEl.className = 'vhn-modal';
    modalEl.innerHTML =
      '<button type="button" class="vhn-modal-close" aria-label="Close"><svg viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button>' +
      '<div class="vhn-modal-stage"><img class="vhn-modal-img" alt="" /></div>' +
      '<div class="vhn-modal-bar">' +
      '<span class="vhn-modal-info">' +
      '<img class="vhn-favicon vhn-modal-favicon" alt="" />' +
      '<span class="vhn-modal-text">' +
      '<a class="vhn-modal-title" href="#" target="_blank" rel="noopener noreferrer"></a>' +
      '<span class="vhn-modal-desc"></span>' +
      '</span>' +
      '</span>' +
      '<span class="vhn-modal-zoom">' +
      '<button type="button" class="vhn-zoom-btn" data-zoom="in" aria-label="Zoom in" title="Zoom in"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/><line x1="20" y1="20" x2="16.5" y2="16.5"/></svg></button>' +
      '<button type="button" class="vhn-zoom-btn" data-zoom="out" aria-label="Zoom out" title="Zoom out"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="8" y1="11" x2="14" y2="11"/><line x1="20" y1="20" x2="16.5" y2="16.5"/></svg></button>' +
      '<button type="button" class="vhn-zoom-btn" data-zoom="actual" aria-label="Actual size (1:1)" title="Actual size (1:1)"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="9"/></svg></button>' +
      '<button type="button" class="vhn-zoom-btn" data-zoom="fit" aria-label="Fit to screen" title="Fit to screen"><svg viewBox="0 0 24 24"><path d="M4 9V4h5"/><path d="M20 15v5h-5"/><path d="M4 15v5h5"/><path d="M20 9V4h-5"/></svg></button>' +
      '</span>' +
      '</div>';

    modalStage = modalEl.querySelector('.vhn-modal-stage');
    modalImg = modalEl.querySelector('.vhn-modal-img');

    // Close button
    modalEl.querySelector('.vhn-modal-close').addEventListener('click', closeModal);

    // Zoom buttons
    modalEl.querySelectorAll('.vhn-zoom-btn').forEach(function(btn) {
      btn.addEventListener('click', function(ev) {
        ev.stopPropagation();
        var action = btn.getAttribute('data-zoom');
        if (action === 'fit') { scaleToFit(); }
        else if (action === 'actual') { ts = 1; tx = 0; ty = 0; startAnim(); }
        else if (action === 'in') { ts = clamp(ts * 1.5, 0.05, 8); startAnim(); }
        else if (action === 'out') { ts = clamp(ts / 1.5, 0.05, 8); startAnim(); }
      });
    });

    // Pan & zoom events on the stage
    modalStage.addEventListener('pointerdown', onPtrDown);
    modalStage.addEventListener('pointermove', onPtrMove);
    modalStage.addEventListener('pointerup', onPtrUp);
    modalStage.addEventListener('pointercancel', onPtrUp);
    modalStage.addEventListener('pointerleave', onPtrUp);
    modalStage.addEventListener('wheel', onWheel, { passive: false });
    modalStage.addEventListener('dblclick', onDblClick);

    document.body.appendChild(modalEl);
    return modalEl;
  }

  function openModal(entry) {
    var el = ensureModal();
    el.querySelector('.vhn-modal-img').src = entry.image_url;
    var titleEl = el.querySelector('.vhn-modal-title');
    titleEl.textContent = entry.title || '';
    titleEl.href = entry.url || '#';
    el.querySelector('.vhn-modal-desc').textContent = entry.description || '';

    var fav = el.querySelector('.vhn-modal-favicon');
    if (entry.favicon) {
      fav.src = entry.favicon;
      fav.alt = entry.domain || '';
      fav.style.display = '';
    } else {
      fav.removeAttribute('src');
      fav.style.display = 'none';
    }

    // Reset viewer state (no animation — snap)
    ax = 0; ay = 0; as = 1;
    tx = 0; ty = 0; ts = 1;
    updateTransform();

    el.classList.add('vhn-open');

    // Auto-fit only when the image is larger than the stage (with padding).
    // If it fits at 100% natural size, leave it there — pixel-sharp.
    function autoFit() {
      if (!modalImg.naturalWidth || !modalStage) return;
      var sw = modalStage.clientWidth;
      var sh = modalStage.clientHeight;
      if (modalImg.naturalWidth > sw || modalImg.naturalHeight > sh) {
        scaleToFit();
      }
    }
    if (modalImg.complete && modalImg.naturalWidth) {
      autoFit();
    } else {
      modalImg.addEventListener('load', autoFit, { once: true });
    }
  }

  function closeModal() {
    if (modalEl) modalEl.classList.remove('vhn-open');
    pointers.clear();
    if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
  }

  // ------------------------------------------------ hcker.news settings panel
  // Injected as another category INSIDE hcker.news's existing settings tab
  // (not a separate top-level tab) — appended to the first tab panel found.
  let vhnSectionEl = null;
  let settingsRenderScheduled = false;

  function findSettingsPanel() {
    return document.querySelector('#settings-panel, .settings-panel');
  }

  function buildVhnSection() {
    const section = document.createElement('section');
    section.id = 'vhn-previews-settings-section';
    section.className = 'settings-section vhn-settings-section';
    section.innerHTML =
      '<h2 class="settings-section-title">Previews</h2>' +
      '<div class="settings-section-content">' +
      '<div class="settings-row">' +
      '<span class="settings-label">Image Size</span>' +
      '<div class="settings-options">' +
      '<div class="vhn-custom-dropdown" id="vhn-size-dropdown">' +
      '<button type="button" class="vhn-dropdown-trigger" id="vhn-size-trigger" aria-haspopup="listbox" aria-expanded="false">' +
      '<span class="vhn-dropdown-selected-text">Medium</span>' +
      '<svg class="vhn-dropdown-arrow" width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</button>' +
      '<div class="vhn-dropdown-menu" id="vhn-size-menu" role="listbox" aria-hidden="true">' +
      '<button type="button" class="vhn-dropdown-option" role="option" aria-selected="false" data-vhn-size="xs">Small</button>' +
      '<button type="button" class="vhn-dropdown-option" role="option" aria-selected="true" data-vhn-size="md">Medium</button>' +
      '<button type="button" class="vhn-dropdown-option" role="option" aria-selected="false" data-vhn-size="large">Large</button>' +
      '</div>' +
      '</div>' +
      '</div>' +
      '</div>' +
      '<div class="settings-row">' +
      '<span class="settings-label">Aspect Ratio</span>' +
      '<div class="settings-options">' +
      '<div class="vhn-custom-dropdown" id="vhn-ar-dropdown">' +
      '<button type="button" class="vhn-dropdown-trigger" id="vhn-ar-trigger" aria-haspopup="listbox" aria-expanded="false">' +
      '<span class="vhn-dropdown-selected-text">Landscape</span>' +
      '<svg class="vhn-dropdown-arrow" width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</button>' +
      '<div class="vhn-dropdown-menu" id="vhn-ar-menu" role="listbox" aria-hidden="true">' +
      '<button type="button" class="vhn-dropdown-option" role="option" aria-selected="false" data-vhn-ar="square">Square</button>' +
      '<button type="button" class="vhn-dropdown-option" role="option" aria-selected="false" data-vhn-ar="portrait">Portrait</button>' +
      '<button type="button" class="vhn-dropdown-option" role="option" aria-selected="true" data-vhn-ar="landscape">Landscape</button>' +
      '</div>' +
      '</div>' +
      '</div>' +
      '</div>' +
      '<div class="settings-row">' +
      '<label class="settings-label" for="vhn-show-favicons">Title favicons</label>' +
      '<div class="settings-options"><label class="toggle-switch"><input type="checkbox" id="vhn-show-favicons"><span class="toggle-slider"></span></label></div>' +
      '</div>' +
      '<div class="settings-row">' +
      '<label class="settings-label" for="vhn-show-descriptions">Descriptions</label>' +
      '<div class="settings-options"><label class="toggle-switch"><input type="checkbox" id="vhn-show-descriptions"><span class="toggle-slider"></span></label></div>' +
      '</div>' +
      '<div class="settings-row">' +
      '<label class="settings-label" for="vhn-show-hover-preview">Hover preview</label>' +
      '<div class="settings-options"><label class="toggle-switch"><input type="checkbox" id="vhn-show-hover-preview"><span class="toggle-slider"></span></label></div>' +
      '</div>' +
      '<div class="settings-row">' +
      '<label class="settings-label" for="vhn-show-rank-badges">Rank badges</label>' +
      '<div class="settings-options"><label class="toggle-switch"><input type="checkbox" id="vhn-show-rank-badges"><span class="toggle-slider"></span></label></div>' +
      '</div>' +
      '</div>';

    // Image Size dropdown
    const sizeTrigger = section.querySelector('#vhn-size-trigger');
    const sizeMenu = section.querySelector('#vhn-size-menu');
    sizeTrigger.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const expanded = sizeTrigger.getAttribute('aria-expanded') === 'true';
      closeAllDropdowns(section);
      if (!expanded) {
        sizeTrigger.setAttribute('aria-expanded', 'true');
        sizeMenu.setAttribute('aria-hidden', 'false');
      }
    });
    sizeMenu.querySelectorAll('.vhn-dropdown-option').forEach((opt) => {
      opt.addEventListener('click', (ev) => {
        ev.stopPropagation();
        setSize(opt.getAttribute('data-vhn-size'));
        closeAllDropdowns(section);
      });
    });

    // Aspect Ratio dropdown
    const arTrigger = section.querySelector('#vhn-ar-trigger');
    const arMenu = section.querySelector('#vhn-ar-menu');
    arTrigger.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const expanded = arTrigger.getAttribute('aria-expanded') === 'true';
      closeAllDropdowns(section);
      if (!expanded) {
        arTrigger.setAttribute('aria-expanded', 'true');
        arMenu.setAttribute('aria-hidden', 'false');
      }
    });
    arMenu.querySelectorAll('.vhn-dropdown-option').forEach((opt) => {
      opt.addEventListener('click', (ev) => {
        ev.stopPropagation();
        setAspectRatio(opt.getAttribute('data-vhn-ar'));
        closeAllDropdowns(section);
      });
    });

    // Close dropdowns on outside click
    document.addEventListener('click', () => closeAllDropdowns(section));

    section
      .querySelector('#vhn-show-favicons')
      .addEventListener('change', (ev) => setShowFavicons(ev.target.checked));
    section
      .querySelector('#vhn-show-descriptions')
      .addEventListener('change', (ev) => setShowDescriptions(ev.target.checked));
    section
      .querySelector('#vhn-show-hover-preview')
      .addEventListener('change', (ev) => setShowHoverPreview(ev.target.checked));
    section
      .querySelector('#vhn-show-rank-badges')
      .addEventListener('change', (ev) => setShowRankBadges(ev.target.checked));

    return section;
  }

  function closeAllDropdowns(section) {
    if (!section) section = document.querySelector('#vhn-previews-settings-section');
    if (!section) return;
    section.querySelectorAll('.vhn-dropdown-trigger').forEach((t) => t.setAttribute('aria-expanded', 'false'));
    section.querySelectorAll('.vhn-dropdown-menu').forEach((m) => m.setAttribute('aria-hidden', 'true'));
  }

  function updateDropdownText(triggerSel, menuSel, value) {
    const trigger = document.querySelector(triggerSel);
    const menu = document.querySelector(menuSel);
    if (!trigger || !menu) return;
    trigger.querySelector('.vhn-dropdown-selected-text').textContent =
      menu.querySelector('[data-vhn-size="' + value + '"], [data-vhn-ar="' + value + '"]')?.textContent || value;
    menu.querySelectorAll('.vhn-dropdown-option').forEach((opt) => {
      const optVal = opt.getAttribute('data-vhn-size') || opt.getAttribute('data-vhn-ar');
      const active = optVal === value;
      opt.classList.toggle('active', active);
      opt.setAttribute('aria-selected', String(active));
    });
  }

  function ensureVhnSettingsPanel() {
    const settingsPanel = findSettingsPanel();
    if (!settingsPanel) return false;

    vhnSectionEl = settingsPanel.querySelector('#vhn-previews-settings-section');
    if (!vhnSectionEl) {
      const targetPanel = settingsPanel.querySelector('.settings-tab-panel') || settingsPanel;
      const wrapper = targetPanel.querySelector('.settings-sections-wrapper') || targetPanel;
      vhnSectionEl = buildVhnSection();
      wrapper.prepend(vhnSectionEl);
    }

    return true;
  }

  function renderVhnSettings() {
    if (!ensureVhnSettingsPanel()) return;
    const vhnPanelEl = vhnSectionEl;

    updateDropdownText('#vhn-size-trigger', '#vhn-size-menu', settings.imageSize);
    updateDropdownText('#vhn-ar-trigger', '#vhn-ar-menu', settings.aspectRatio);

    const showFavicons = vhnPanelEl.querySelector('#vhn-show-favicons');
    if (showFavicons && showFavicons.checked !== settings.showFavicons) showFavicons.checked = settings.showFavicons;

    const showDescriptions = vhnPanelEl.querySelector('#vhn-show-descriptions');
    if (showDescriptions && showDescriptions.checked !== settings.showDescriptions) showDescriptions.checked = settings.showDescriptions;

    const showHoverPreview = vhnPanelEl.querySelector('#vhn-show-hover-preview');
    if (showHoverPreview && showHoverPreview.checked !== settings.showHoverPreview) showHoverPreview.checked = settings.showHoverPreview;

    const showRankBadges = vhnPanelEl.querySelector('#vhn-show-rank-badges');
    if (showRankBadges && showRankBadges.checked !== settings.showRankBadges) showRankBadges.checked = settings.showRankBadges;
  }

  function scheduleSettingsRender() {
    if (settingsRenderScheduled) return;
    settingsRenderScheduled = true;
    setTimeout(() => {
      settingsRenderScheduled = false;
      renderVhnSettings();
    }, 250);
  }

  // ---------------------------------------------------------------- scanning
  function isFeedPage() {
    // Only scan on the homepage / feed — not on comment/search/item SPA views.
    const q = window.location.search;
    return !q.includes("comments=") && !q.includes("item=") && !q.includes("search=");
  }

  async function scan() {
    scanScheduled = false;
    if (!settings.enabled || !isFeedPage()) return;

    const rows = window.VHN.findRows(document).filter((r) => !r.row.hasAttribute(HANDLED_ATTR));
    if (rows.length === 0) return;

    // Mark immediately so a concurrent mutation scan does not double-inject.
    rows.forEach((r) => r.row.setAttribute(HANDLED_ATTR, '1'));
    stats.matched += rows.length;

    const ids = [...new Set(rows.map((r) => r.id))];
    const images = await window.VHN.fetchImages(ids, settings);
    stats.apiOk = window.VHN.apiOk;

    let injected = 0;
    rows.forEach((r, index) => {
      const entry = images.get(r.id);
      if (entry) {
        injectInto(r.row, r.anchor, entry, index + 1);
        stats.loaded += 1;
        injected += 1;
      } else {
        injectSpacer(r.row, index + 1);
      }
    });

    // Hard network failure with nothing resolved: let these rows retry later.
    if (stats.apiOk === false && injected === 0) {
      rows.forEach((r) => r.row.removeAttribute(HANDLED_ATTR));
      stats.matched -= rows.length;
    }
    renderVhnSettings();
  }

  function scheduleScan() {
    if (scanScheduled) return;
    scanScheduled = true;
    setTimeout(scan, 150); // debounce bursts of mutations
  }

  function onMutation() {
    if (!isFeedPage()) return;
    scheduleScan();
    if (!vhnSectionEl || !document.documentElement.contains(vhnSectionEl)) {
      vhnSectionEl = null;
      scheduleSettingsRender();
    }
  }

  // ---------------------------------------------------------------- keyboard
  function onKeydown(ev) {
    if (ev.key === 'Escape' && modalEl && modalEl.classList.contains('vhn-open')) {
      closeModal();
      return;
    }
    // Ignore shortcut while typing in hcker.news search/inputs.
    const t = ev.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (!typing && (ev.key === 'i' || ev.key === 'I') && !ev.metaKey && !ev.ctrlKey) {
      settings.enabled = !settings.enabled;
      saveSetting('enabled', settings.enabled);
      applyEnabledState();
    }
  }

  // ---------------------------------------------------------------- init
  async function init() {
    await loadSettings();
    applyEnabledState();
    applyHoverPreviewState();
    scheduleSettingsRender();
    if (settings.enabled) scan();

    observer = new MutationObserver(onMutation);
    observer.observe(document.body, { childList: true, subtree: true });

    document.addEventListener('keydown', onKeydown, true);

    // Watch for SPA navigation (pushState/replaceState) — hcker.news stays on
    // / but switches views via query params (?comments=X, ?search=...).
    var lastUrl = location.href;
    setInterval(function() {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        if (settings.enabled) {
          if (isFeedPage()) {
            // Returned to feed — re-inject
            document.querySelectorAll('[' + HANDLED_ATTR + ']').forEach(removeInjections);
            scheduleScan();
          } else {
            // Navigated to comments/search — clean up injections
            document.querySelectorAll('[' + HANDLED_ATTR + ']').forEach(removeInjections);
          }
        }
      }
    }, 500);

    // React to live option changes without a page reload.
    if (hasChromeStorage && chrome.storage.onChanged) {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'sync') return;
        loadSettings().then(() => {
          applyEnabledState();
          if (changes.imageSize || changes.aspectRatio || changes.showFavicons || changes.showDescriptions) reapplyInjections();
          if (changes.showHoverPreview) applyHoverPreviewState();
        });
      });
    }
  }

  init();
})();
