"""Content blocker for headless Chrome screenshots.

Uses two complementary strategies to block cookie banners, overlays,
notification prompts, chat widgets, and other annoyances:

1. **JS-level request blocking** — overrides fetch/XHR in the page to kill
   requests to known annoyance domains. Injected via
   Page.addScriptToEvaluateOnNewDocument so it runs before ANY page script.

2. **CSS hiding + JS dismissal** — hides known banner selectors via CSS
   injection, then clicks "accept"/"dismiss" buttons on any surviving dialogs.

This is the headless-CSS equivalent of uBlock Origin's EasyList annoyance
filters. No browser extension needed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ── Domain blocklist ────────────────────────────────────────────────────────
# These domains are blocked at the JS network layer (fetch/XHR override)
# AND at the CSS layer (element hiding). Covers:
#   Cookie consent | Notifications | Chat widgets | Social widgets
#   Analytics overlays | Anti-adblock | Paywalls

BLOCKED_DOMAINS: list[str] = [
    # Cookie consent platforms
    "cookiebot.com",
    "cookiebot.dk",
    "onetrust.com",
    "cookielaw.org",
    "osano.com",
    "termly.io",
    "iubenda.com",
    "cookie-script.com",
    "usercentrics.eu",
    "consentframework.com",
    "consentmanager.net",
    "trustarc.com",
    "didomi.io",
    "quantcast.com",
    "evidon.com",
    "sourcepoint.com",
    # Notification prompt services
    "onesignal.com",
    "pushwoosh.com",
    "aimtell.com",
    "webpushr.com",
    "pushengage.com",
    "pushowl.com",
    "frizbit.com",
    "cleverpush.com",
    "notix.io",
    "pushpad.xyz",
    "subscribers.com",
    "lenzmx.com",
    # Chat widgets
    "intercom.io",
    "intercomcdn.com",
    "zendesk.com",
    "zdassets.com",
    "drift.com",
    "driftt.com",
    "crisp.chat",
    "livechatinc.com",
    "tidio.co",
    "tawk.to",
    "helpscoutdocs.com",
    "helpscout.net",
    "freshdesk.com",
    "freshworks.com",
    "hubspot.com",
    "hubspot.net",
    # Social widgets
    "addthis.com",
    "sharethis.com",
    "sumo.com",
    "sumome.com",
    "sumo.me",
    # Analytics that inject banners/overlays
    "hotjar.com",
    "hotjar.io",
    "mouseflow.com",
    "fullstory.com",
    "luckyorange.com",
    "crazyegg.com",
    "matomo.org",
    "piwik.org",
    # Paywall / anti-adblock
    "tinypass.com",
    "piano.io",
    "mediego.com",
    # Ad/tracking
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google.com",
    "pagead2.googlesyndication.com",
    "googletagmanager.com",
    "facebook.net",
    "connect.facebook.net",
    "pixel.facebook.com",
]


def _build_blocked_domains_js() -> str:
    """Build the JS array literal for blocked domains."""
    items = ", ".join(f'"{d}"' for d in BLOCKED_DOMAINS)
    return f"[{items}]"


# ── JS-level request blocker (injected before page loads) ──────────────────
# This runs via Page.addScriptToEvaluateOnNewDocument — it executes in a
# fresh context BEFORE any page script. It overrides fetch and XHR to
# silently abort requests to blocked domains.

_REQUEST_BLOCKER_JS = f"""
(function() {{
    var BLOCKED = {_build_blocked_domains_js()};

    function isBlocked(url) {{
        try {{
            var host = new URL(url, location.href).hostname.toLowerCase();
            for (var i = 0; i < BLOCKED.length; i++) {{
                if (host === BLOCKED[i] || host.endsWith('.' + BLOCKED[i])) {{
                    return true;
                }}
            }}
        }} catch(e) {{}}
        return false;
    }}

    // Override fetch()
    var origFetch = window.fetch;
    window.fetch = function(url, opts) {{
        if (isBlocked(String(url))) {{
            return Promise.reject(new Error('Blocked by content blocker'));
        }}
        return origFetch.apply(this, arguments);
    }};

    // Override XMLHttpRequest.open()
    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {{
        if (isBlocked(String(url))) {{
            this._blocked = true;
            return;
        }}
        return origOpen.apply(this, arguments);
    }};

    // Block send() and setRequestHeader() for blocked XHRs
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function() {{
        if (this._blocked) return;
        return origSend.apply(this, arguments);
    }};

    var origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function() {{
        if (this._blocked) return;
        return origSetHeader.apply(this, arguments);
    }};

    // Also block dynamic script injections that load blocked domains
    var origCreateElement = document.createElement.bind(document);
    document.createElement = function(tag) {{
        var el = origCreateElement(tag);
        if (tag.toLowerCase() === 'script') {{
            var origSrcSet = Object.getOwnPropertyDescriptor(
                HTMLScriptElement.prototype, 'src'
            );
            if (origSrcSet) {{
                Object.defineProperty(el, 'src', {{
                    get: function() {{ return origSrcSet.get.call(this); }},
                    set: function(val) {{
                        if (isBlocked(String(val))) {{
                            // Silently eat it
                            return;
                        }}
                        return origSrcSet.set.call(this, val);
                    }}
                }});
            }}
        }}
        return el;
    }};
}})();
"""


# ── CSS to hide annoyance elements ─────────────────────────────────────────
# Injected AFTER page load. Covers known selectors for cookie consent
# banners, notification prompts, chat widgets, overlays, etc.

INJECT_CSS = """
/* ── Cookie consent banners ── */
[id*="cookie" i] { display: none !important; }
[id*="consent" i] { display: none !important; }
[class*="cookie" i] { display: none !important; }
[class*="consent" i] { display: none !important; }
[id*="onetrust" i], [class*="onetrust" i] { display: none !important; }
[id*="cookiebot" i], [class*="cookiebot" i] { display: none !important; }
[id*="osano" i], [class*="osano" i] { display: none !important; }
[id*="iubenda" i], [class*="iubenda" i] { display: none !important; }
[id*="termly" i], [class*="termly" i] { display: none !important; }
[id*="usercentrics" i], [class*="usercentrics" i] { display: none !important; }
[id*="didomi" i], [class*="didomi" i] { display: none !important; }
[id*="trustarc" i], [class*="trustarc" i] { display: none !important; }
[id*="cookielaw" i], [class*="cookielaw" i] { display: none !important; }
[id*="evidon" i], [class*="evidon" i] { display: none !important; }
[id*="sourcepoint" i], [class*="sourcepoint" i] { display: none !important; }
[id*="quantcast" i], [class*="quantcast" i] { display: none !important; }
[id*="consentmanager" i], [class*="consentmanager" i] { display: none !important; }
[id*="gdpr" i], [class*="gdpr" i] { display: none !important; }
[id*="ccpa" i], [class*="ccpa" i] { display: none !important; }
[id*="privacy-consent" i], [class*="privacy-consent" i] { display: none !important; }
[id*="privacy-banner" i] { display: none !important; }
div[class*="fc-consent" i], div[class*="fc-dialog" i] { display: none !important; }

/* ── Notification permission prompts (in-page) ── */
[id*="onesignal" i], [class*="onesignal" i] { display: none !important; }
[id*="push" i][class*="prompt" i] { display: none !important; }
[id*="push" i][class*="bell" i] { display: none !important; }

/* ── Overlay / dark backdrop notices ── */
[class*="overlay" i][class*="consent" i] { display: none !important; }
[class*="overlay" i][class*="cookie" i] { display: none !important; }
[class*="overlay" i][class*="notice" i] { display: none !important; }
[class*="backdrop" i][class*="consent" i] { display: none !important; }
[class*="backdrop" i][class*="cookie" i] { display: none !important; }
[class*="modal" i][class*="consent" i] { display: none !important; }
[class*="modal" i][class*="cookie" i] { display: none !important; }
[class*="modal" i][class*="privacy" i] { display: none !important; }
[class*="lightbox" i][class*="consent" i] { display: none !important; }

/* ── Social share widgets ── */
[class*="share" i][class*="widget" i] { display: none !important; }
[class*="social" i][class*="widget" i] { display: none !important; }
[class*="share" i][class*="bar" i] { display: none !important; }
.addthis_toolbox, .sharethis-container,
.sumome-popup, .sumome-float { display: none !important; }

/* ── Chat widgets ── */
[id*="intercom" i], [class*="intercom" i] { display: none !important; }
[id*="drift" i], [class*="drift" i] { display: none !important; }
[id*="crisp" i], [class*="crisp" i] { display: none !important; }
[id*="tidio" i], [class*="tidio" i] { display: none !important; }
[id*="zendesk" i][class*="widget" i], [id*="zendesk" i][class*="chat" i] { display: none !important; }
iframe[src*="intercom" i], iframe[src*="drift" i], iframe[src*="crisp" i],
iframe[src*="tidio" i], iframe[src*="zendesk" i], iframe[src*="tawk" i] { display: none !important; }

/* ── Anti-adblock walls ── */
[class*="adblock" i], [id*="adblock" i],
[class*="ad-block" i], [id*="ad-block" i],
[class*="adblocker" i], [id*="adblocker" i] { display: none !important; }

/* ── Paywall modals ── */
[class*="paywall" i], [id*="paywall" i] { display: none !important; }

/* ── Broad fixed-position banners covering the viewport ── */
body > div[style*="position: fixed" i][style*="bottom" i] { display: none !important; }
body > div[style*="position:fixed" i][style*="bottom" i] { display: none !important; }
"""


# ── JS to dismiss remaining banners ─────────────────────────────────────────
# Clicks "accept"/"dismiss" on surviving consent dialogs, then removes
# fixed-position overlays that cover the viewport.

DISMISS_JS = """
(function() {
    // 1. Click common accept/dismiss buttons
    var selectors = [
        '#onetrust-accept-btn-handler',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
        '#CybotCookiebotDialogBodyButtonDecline',
        '.osano-cm-accept-all', '.osano-cm-accept',
        '[data-cky-tag="accept-button"]',
        '.t-consentPrompt-acceptAll',
        '[data-testid="uc-accept-all-button"]',
        '.iubenda-cs-accept-btn',
        '#didomi-notice-agree-button',
        '.qc-cmp2-summary-buttons button[mode="primary"]',
        'button[id*="accept" i]',
        'button[class*="accept" i]',
        'button[class*="agree" i]',
        '[class*="consent" i] button[class*="close" i]',
        '[class*="cookie" i] button[class*="close" i]',
        '[id*="cookie" i] button[class*="close" i]',
        '[id*="consent" i] button[class*="close" i]',
    ];
    for (var i = 0; i < selectors.length; i++) {
        try {
            var els = document.querySelectorAll(selectors[i]);
            for (var j = 0; j < els.length; j++) {
                var el = els[j];
                var rect = el.getBoundingClientRect();
                var cs = window.getComputedStyle(el);
                if (rect.width > 0 && rect.height > 0 &&
                    cs.display !== 'none' && cs.visibility !== 'hidden' &&
                    cs.opacity !== '0') {
                    el.click();
                    break;
                }
            }
        } catch(e) {}
    }

    // 2. Remove fixed-position overlays covering the viewport
    var fixed = document.querySelectorAll(
        'div[style*="position: fixed" i], div[style*="position:fixed" i]'
    );
    for (var k = 0; k < fixed.length; k++) {
        var el = fixed[k];
        var cs = window.getComputedStyle(el);
        var z = parseInt(cs.zIndex, 10);
        var r = el.getBoundingClientRect();
        if (z > 999 && r.width >= window.innerWidth * 0.8 &&
            r.height >= window.innerHeight * 0.3) {
            el.remove();
        }
    }
})();
"""


# ── Public API ──────────────────────────────────────────────────────────────


def inject_request_blocker(driver: Any) -> None:
    """Inject JS that blocks requests to annoyance domains before page load.

    Must be called BEFORE driver.get(url).
    Uses Page.addScriptToEvaluateOnNewDocument so it runs in a fresh context
    before any page script executes.
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": _REQUEST_BLOCKER_JS,
            },
        )
        logger.debug("Request blocker injected (%d domains)", len(BLOCKED_DOMAINS))
    except Exception as exc:
        logger.warning("Failed to inject request blocker: %s", exc)


def dismiss_annoyances(driver: Any) -> None:
    """Inject CSS and JS to hide/dismiss cookie banners and overlays.

    Call AFTER driver.get(url) and BEFORE taking the screenshot.
    """
    # Inject CSS
    try:
        css_js = (
            "(function(){"
            "var s=document.createElement('style');"
            "s.textContent=" + repr(INJECT_CSS) + ";"
            "document.head.appendChild(s);"
            "})()"
        )
        driver.execute_script(css_js)
    except Exception as exc:
        logger.debug("Failed to inject CSS: %s", exc)

    # Inject dismiss JS
    try:
        driver.execute_script(DISMISS_JS)
    except Exception as exc:
        logger.debug("Failed to inject dismiss JS: %s", exc)

    # Second pass — some banners animate in after a delay
    time.sleep(0.3)
    try:
        driver.execute_script(DISMISS_JS)
    except Exception:
        pass
