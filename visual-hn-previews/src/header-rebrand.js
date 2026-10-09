// header-rebrand.js — keep the proxied hcker.news header branded across SPA hydration.

(function () {
  // Signal that JS is active for progressive-enhancement CSS
  document.documentElement.classList.add('js');

  const BRANDED = 'hcker.news+';
  const TAGLINE_HTML = 'a <a href="https://hcker.news/" target="_blank" rel="noopener">hcker.news</a> reader with previews';
  const FULL_TITLE = 'hcker.news+ – A Better Hcker.news Reader with Previews';
  // Brand titles only: bare 'hcker.news', branded 'hcker.news+', or the SPA's
  // runtime forms with an emoji prefix. Anchored, so story titles like
  // 'Comments – Comments – hcker.news' are never rewritten.
  const TITLE_RE = /^hcker\.news\+?$/i;

  function normalizeTitle(val) {
    if (typeof val !== 'string') return null;
    const t = val.trim().replace(/^🐴\s*/, '');
    if (t === FULL_TITLE || TITLE_RE.test(t)) return FULL_TITLE;
    return null;
  }

  let applying = false;
  let scheduled = false;

  function setText(element, text) {
    if (element && element.textContent.trim() !== text) {
      element.textContent = text;
    }
  }

  function setTaglineHtml(element, html) {
    if (element && element.innerHTML.trim() !== html) {
      element.innerHTML = html;
    }
  }

  function rebrandHeader() {
    applying = true;
    try {
      setText(document.querySelector('#header h1 a'), BRANDED);
      setTaglineHtml(document.querySelector('#header .tagline'), TAGLINE_HTML);

      const branded = normalizeTitle(document.title);
      if (branded) {
        document.title = branded;
      }
    } finally {
      applying = false;
    }
  }

  function scheduleRebrand() {
    if (applying || scheduled) return;
    scheduled = true;
    queueMicrotask(function () {
      scheduled = false;
      rebrandHeader();
    });
  }

  function observeHeader() {
    var root = document.body || document.documentElement;
    if (!root) return;

    var obs = new MutationObserver(function () {
      scheduleRebrand();
    });
    obs.observe(root, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  function interceptTitleSetter() {
    var desc = Object.getOwnPropertyDescriptor(Document.prototype, 'title');
    if (desc && desc.set) {
      Object.defineProperty(document, 'title', {
        get: desc.get,
        set: function (val) {
          desc.set.call(this, normalizeTitle(val) || val);
        },
        configurable: true,
      });
    }
  }

  function start() {
    rebrandHeader();
    observeHeader();
    interceptTitleSetter();
  }

  if (document.body) {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  }
})();
