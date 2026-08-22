// header-rebrand.js — keep the proxied hcker.news header branded across SPA hydration.

(function () {
  // Signal that JS is active for progressive-enhancement CSS
  document.documentElement.classList.add('js');

  const BRANDED = 'visual-hn';
  const TAGLINE_HTML = 'a <a href="https://hcker.news/" target="_blank" rel="noopener">hcker.news</a> reader with pictures';
  const TITLE_RE = /^hcker\.news$/i;

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

      if (document.title && TITLE_RE.test(document.title.trim())) {
        document.title = BRANDED;
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
          if (typeof val === 'string' && TITLE_RE.test(val.trim())) {
            desc.set.call(this, BRANDED);
          } else {
            desc.set.call(this, val);
          }
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
