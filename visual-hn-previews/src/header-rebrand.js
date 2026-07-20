// header-rebrand.js — keep the proxied hcker.news header branded across SPA hydration.

(function () {
  const BRANDED = 'visual HN';
  const TAGLINE_HTML = 'a <a href="https://hcker.news/" target="_blank" rel="noopener">hcker.news</a> reader';
  const TITLE_RE = /^hcker\.news$/i;
  const RAINBOW_FADE_MS = 2000;

  const RAINBOW_HTML =
    '<span class="vhn-rainbow">' +
    '<span class="vhn-rainbow-char" style="--i:0">v</span>' +
    '<span class="vhn-rainbow-char" style="--i:1">i</span>' +
    '<span class="vhn-rainbow-char" style="--i:2">s</span>' +
    '<span class="vhn-rainbow-char" style="--i:3">u</span>' +
    '<span class="vhn-rainbow-char" style="--i:4">a</span>' +
    '<span class="vhn-rainbow-char" style="--i:5">l</span>' +
    '</span> <span class="vhn-thinsp"></span> HN';

  let applying = false;
  let scheduled = false;

  function setRainbowText(element) {
    if (!element) return;
    if (element.querySelector('.vhn-rainbow')) return;
    element.innerHTML = RAINBOW_HTML;
    // Schedule fade-to-white after RAINBOW_FADE_MS
    setTimeout(function () {
      var rainbow = element.querySelector('.vhn-rainbow');
      if (rainbow) rainbow.classList.add('vhn-rainbow-faded');
    }, RAINBOW_FADE_MS);
  }

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
      setRainbowText(document.querySelector('#header h1 a'));
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
