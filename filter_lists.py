"""Parse vendored Adblock filter lists into Playwright-blocking components.

The Fanboy Cookie list (fanboy-cookiemonster.txt) is the comprehensive
cookie-consent / GDPR banner filter list maintained by the EasyList project
and used by uBlock Origin. It ships two kinds of rules:

1. **Network block rules** — e.g. `||cookiebot.com^$third-party` — matched
   against request URLs. Playwright's ``page.route()`` aborts these so the
   consent script never loads and the banner never renders.

2. **Cosmetic (element-hiding) rules** — e.g. `###onetrust-banner-sdk` or
   `##.cookie-banner` — CSS selectors injected as a <style> tag to hide
   inline / first-party banners that don't load from a blockable domain.

This module parses the list once at import time into two lists:

    NETWORK_PATTERNS : list[str]  → host patterns for request blocking
    COSMETIC_CSS     : str        → compiled `selector { display:none }` CSS

The vendored file is the source of truth — there is no runtime fetch. Bump
``static/filters/fanboy-cookiemonster.txt`` manually when you want fresher
rules.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

FILTER_FILE = Path(__file__).parent / "static" / "filters" / "fanboy-cookiemonster.txt"

# Heuristic limit — if a parsed CSS blob is enormous something went wrong.
_MAX_CSS_SELECTORS = 25000


def _parse_network_rule(rule: str) -> str | None:
    """Extract a matchable substring from an Adblock network rule.

    Handles ``||example.com^`` (anchor-to-host), ``/path/resource.js``,
    and generic patterns like ``-cookie-consent.js``. Options after ``$``
    (e.g. ``$third-party``, ``$script``) are stripped — we block regardless
    of resource type. Returns the raw pattern matched as a URL substring,
    or None for allowlist/regex rules we can't handle.
    """
    # Strip options: everything after the first unescaped $
    rule = rule.split("$")[0]
    rule = rule.strip()
    if not rule or rule.startswith("@@"):  # @@ = exception/allow, skip
        return None
    # ||example.com^  →  anchor to hostname boundary → strip the marker
    if rule.startswith("||"):
        rule = rule[2:]
    # |example.com  →  anchor to start of URL → strip
    if rule.startswith("|"):
        rule = rule[1:]
    # Remove trailing ^ (Adblock separator char)
    rule = rule.rstrip("^")
    # Skip regex rules and pure wildcards — too broad / can't substring-match
    if not rule or "*" in rule or rule.startswith("~") or rule.startswith("RegExp"):
        return None
    return rule.lower()


def _parse_cosmetic_rule(rule: str) -> str | None:
    """Extract a CSS selector from an Adblock cosmetic (element-hiding) rule.

    Rules look like ``example.com##.cookie-banner`` or ``##.cookie-banner``
    or ``###onetrust-banner-sdk``. Returns the selector portion only.
    Domain restrictions (``example.com##...``) are dropped — we inject a
    global stylesheet so the selector applies on all pages.
    """
    # Split on ## (domain-specific) or #?# (procedural — skip, Playwright
    # can't run those as static CSS)
    if "#?#" in rule:
        return None
    parts = rule.split("##", 1)
    if len(parts) != 2:
        return None
    selector = parts[1].strip()
    # Skip rules with resource types or script injections
    if "{" in selector or "$" in selector or selector.startswith("+"):
        return None
    if not selector:
        return None
    return selector


def _load_filter_file(path: Path) -> tuple[list[str], list[str]]:
    """Parse a filter list file into (network_hosts, cosmetic_selectors)."""
    if not path.exists():
        logger.warning(
            "Filter list not found at %s — annoyances will not be blocked", path
        )
        return [], []

    text = path.read_text(encoding="utf-8", errors="replace")
    network_hosts: list[str] = []
    cosmetic_selectors: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("!") or line.startswith("["):
            continue

        # Cosmetic rule (contains ##)
        if "##" in line:
            sel = _parse_cosmetic_rule(line)
            if sel:
                cosmetic_selectors.append(sel)
            continue

        # Network rule (no ##, not a comment)
        host = _parse_network_rule(line)
        if host:
            network_hosts.append(host)

    # Dedupe while preserving order
    seen: set[str] = set()
    network_hosts = [h for h in network_hosts if not (h in seen or seen.add(h))]
    seen.clear()
    cosmetic_selectors = [
        s for s in cosmetic_selectors if not (s in seen or seen.add(s))
    ]

    logger.info(
        "Parsed filter list %s: %d network hosts, %d cosmetic selectors",
        path.name,
        len(network_hosts),
        len(cosmetic_selectors),
    )
    return network_hosts, cosmetic_selectors


_NETWORK_HOSTS, _COSMETIC_SELECTORS = _load_filter_file(FILTER_FILE)

NETWORK_HOSTS: list[str] = _NETWORK_HOSTS
COSMETIC_SELECTORS: list[str] = _COSMETIC_SELECTORS


def get_cosmetic_css() -> str:
    """Compile cosmetic selectors into a single ``display:none`` stylesheet."""
    if len(_COSMETIC_SELECTORS) > _MAX_CSS_SELECTORS:
        logger.warning(
            "Cosmetic selector count (%d) exceeds safety limit — truncating",
            len(_COSMETIC_SELECTORS),
        )
        selectors = _COSMETIC_SELECTORS[:_MAX_CSS_SELECTORS]
    else:
        selectors = _COSMETIC_SELECTORS
    if not selectors:
        return ""
    # Wrap all selectors in a single display:none rule. This is far more
    # compact than one rule per selector and avoids megabytes of injected CSS.
    return f"{', '.join(selectors)} {{ display: none !important; }}"


def _build_blocked_url_regex(hosts: list[str]) -> re.Pattern[str] | None:
    """Compile network patterns into a single regex for fast substring matching.

    Adblock network rules match as substrings of the full request URL (with
    ``||`` anchoring to the hostname boundary). We normalize to lowercase and
    escape regex metacharacters so patterns like ``-cookie-consent.js`` and
    ``cookiebot.com`` both match correctly.
    """
    if not hosts:
        return None
    # Sort by length descending so longer/more-specific patterns win
    parts = [re.escape(h) for h in sorted(set(hosts), key=len, reverse=True)]
    return re.compile("|".join(parts), re.IGNORECASE)


_BLOCKED_URL_REGEX: re.Pattern[str] | None = _build_blocked_url_regex(_NETWORK_HOSTS)


def is_blocked_url(url: str) -> bool:
    """Return True if a request URL contains any network-block pattern."""
    if _BLOCKED_URL_REGEX is None:
        return False
    return bool(_BLOCKED_URL_REGEX.search(url))
