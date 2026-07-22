# Visual-HN Scraper Overhaul Plan

> Created: 2026-07-22 · Status: **DRAFT** · Owner: Ilya
>
> Scope: `residential_fetcher.py`, `screenshot.py`, `metadata.py`, `hcker_proxy.py`
> Out of scope: `hn_scraper.py` (working), `feed_enrichment.py` (working), `database.py`, frontend

---

## Context

Visual-HN's scraper has grown organically. The core pipeline (curl_cffi → residential → Wayback → screenshot → favicon composite) is sound, but the infrastructure around it has accumulated real bugs, security gaps, and maintenance debt. This document catalogs every issue found during a code audit on 2026-07-22, ranked worst → least, with concrete fixes.

The sister project `is-ai-good-yet` has a better-structured scraping infrastructure (typed errors, per-redirect SSRF, constant-time auth, single browser stack). Where applicable, fixes reference its patterns as proven alternatives.

---

## Issue Catalog

### P0-1: nodriver Ghost Window on Windows (REAL BUG)

**File:** `residential_fetcher.py:142-147`

**Symptom:** An unfocusable, unclickable Chrome window appears on the residential node (Windows 11) and stays open forever. It never closes because the browser singleton is designed to persist for the service's lifetime — but it should never be *visible*.

**Root cause:** nodriver (`headless=True`) does not reliably suppress the Chrome window on Windows. This is a known nodriver/undetected-chromedriver regression:

- GitHub `ultrafunkamsterdam/undetected-chromedriver` #2242 — headless detection + window sizing bugs
- GitHub `xtekky/gpt4free` #2582 — "persistent chrome windows opened, stays there using resources"
- Stack Overflow #79470828 — "Nodriver Cannot Start Headless Mode"

nodriver passes `headless=True` via CDP flags, but Chrome on Windows sometimes ignores the flag or uses the old headless implementation that still renders a window frame.

**Impact:** Annoying for daily laptop use (the residential node is a laptop under daily use). No functional impact on scraping — the browser works, it's just visible.

**Fix — Option A (Quick, minimal risk):**

Add `--headless=new` to `browser_args` in `residential_fetcher.py:146`:

```python
# Before
browser_args=["--no-first-run"],

# After
browser_args=["--no-first-run", "--headless=new"],
```

`--headless=new` uses Chrome's newer headless implementation (Chrome 112+) that properly suppresses the window on Windows. Tradeoff: marginally more detectable by advanced fingerprinting, but nodriver's value is its CDP approach (not headless obscurity), so the practical impact for CF challenge solving is negligible.

**Fix — Option B (Strategic, eliminates the bug class entirely):**

Replace nodriver with Playwright for the residential fetcher. Port the architecture from `is-ai-good-yet/pipeline/src/residential_service.py`:

- `ResidentialSettings` dataclass (env-driven config)
- `ResidentialBrowser` class (singleton browser, new context per request)
- Playwright `chromium.launch(headless=True)` — works correctly on Windows
- Keep the CF checkbox-solve logic as a post-navigation step (Playwright can search iframes too)
- One browser stack for both fetcher and screenshots — eliminates an entire class of bugs

Option B is the right long-term move but is a larger change. **Recommendation: ship Option A now, plan Option B as part of P0-2.**

---

### P0-2: Two Browser Stacks — nodriver + Playwright (ARCHITECTURE DEBT)

**Files:** `residential_fetcher.py` (nodriver), `screenshot.py` (Playwright)

**Problem:** Two different browser automation frameworks with different lifecycles, different config, different failure modes, different stealth approaches. Any browser-related bug fix or Chrome version bump needs to be applied in two places.

- `residential_fetcher.py`: nodriver, singleton `_browser`, single tab reused, CF auto-solve
- `screenshot.py`: Playwright, new browser per screenshot, Fanboy filter-list blocking

**Fix:** Consolidate to Playwright only. This is the single highest-leverage refactor in the scraper.

Concretely:

1. Port `residential_fetcher.py` to Playwright (see P0-1 Option B)
2. Extract the screenshot browser into a shared singleton (see P1-1)
3. Both the fetcher and screenshot path share one Playwright instance, using separate contexts for isolation
4. Delete the nodriver dependency from `requirements.txt`

**Effort:** ~4-6 hours. The CF checkbox-solve logic is the only non-trivial port — Playwright's `page.frames()` can search iframes, which replaces nodriver's `tab.find()`.

---

### P0-3: Timing-Vulnerable Auth Comparison (SECURITY)

**File:** `residential_fetcher.py:240`

```python
def _verify_auth(secret: str | None) -> None:
    if SHARED_SECRET and secret != SHARED_SECRET:
        raise HTTPException(status_code=403, detail="unauthorized")
```

**Problem:** String `!=` comparison short-circuits on first byte mismatch. An attacker can measure response timing to recover the secret byte-by-byte (timing side-channel attack).

**Fix:** Use `secrets.compare_digest`:

```python
import secrets

def _verify_auth(secret: str | None) -> None:
    if SHARED_SECRET:
        if secret is None or not secrets.compare_digest(secret, SHARED_SECRET):
            raise HTTPException(status_code=403, detail="unauthorized")
```

Also add a minimum secret length check (is-ai-good-yet enforces ≥24 chars) so a weak secret fails loudly at startup, not silently in production.

---

### P1-1: No Browser Reuse in screenshot.py (PERFORMANCE)

**File:** `screenshot.py:87-95`

**Problem:** Every `capture_screenshot()` call launches a full Chrome process (`async_playwright()` + `chromium.launch()`), takes the screenshot, then closes it. `browser.close()` IS called (line 154), so there's no leak — but each screenshot pays 2-5 seconds of process spawn + teardown overhead.

**Fix:** Extract a browser singleton (or reuse the one created in P0-2). The screenshot path creates a new **context** (with its own adblock route handler) per screenshot, but reuses the browser process:

```python
# Pseudocode — actual implementation depends on P0-2 consolidation
_browser: Browser | None = None
_browser_lock = asyncio.Lock()

async def _get_browser() -> Browser:
    global _browser
    if _browser and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser and _browser.is_connected():
            return _browser
        playwright = await async_playwright().start()
        _browser = await playwright.chromium.launch(args=[...])
        return _browser
```

**Impact:** Eliminates ~2-5s overhead per screenshot. With 30 stories per scrape cycle and ~20% needing screenshots, that's ~12-30s saved per cycle.

---

### P1-2: Blind Redirect Following (SECURITY — SSRF)

**File:** `metadata.py:133-134` (`_curl_cffi_fetch_html`)

```python
response = await cffi_session.get(
    url,
    headers=headers,
    allow_redirects=True,  # ← follows all redirects without SSRF check
)
```

**Problem:** curl_cffi follows redirects automatically. If a malicious URL redirects to an internal address (`http://169.254.169.254/...` for cloud metadata, or `http://100.64.x.x` for Tailscale CGNAT), the scraper will follow it. The post-hoc `is_public_http_url(final_url)` check on line 138 catches the *final* URL, but the redirect chain itself is unguarded — intermediate requests may already have been made.

**Fix:** Manual redirect loop with SSRF check on each hop. Port the pattern from `is-ai-good-yet/pipeline/src/article_fetch.py:198-263` (`CurlCffiHtmlFetcher.fetch`):

```python
# Set allow_redirects=False, then loop:
current_url = url
for _ in range(6):  # max 6 redirects
    if not is_public_http_url(current_url):
        return None, current_url  # SSRF blocked
    response = await session.get(current_url, allow_redirects=False)
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        await response.aclose()
        if not location:
            return None, current_url
        current_url = urljoin(current_url, location)  # resolve relative
        if not is_public_http_url(current_url):
            return None, current_url  # redirect to unsafe target
        continue
    # Non-redirect response — process it
    ...
```

Apply the same fix to `download_and_resize_image()` in `metadata.py:834-838`.

---

### P1-3: SSRF Coverage Gap — Missing CGNAT Range (SECURITY)

**File:** `metadata.py:340-385` (`is_public_http_url`)

**Problem:** The function checks `is_private`, `is_loopback`, `is_link_local`, `is_reserved`, `is_multicast` — but misses **CGNAT** (`100.64.0.0/10`), which is how Tailscale assigns IPs. A URL could redirect to a Tailscale address (`100.x.x.x`) and pass the current check.

`100.64.0.0/10` is technically `is_private=False, is_global=True` in Python's `ipaddress` module, so it slips through.

**Fix:** Use `is_global` with an explicit multicast check (the is-ai-good-yet pattern):

```python
def _is_non_global_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_multicast or not ip.is_global
```

`is_global` covers private, loopback, link-local, reserved, unspecified, AND CGNAT ranges. Replace the manual `is_private or is_loopback or ...` chain throughout the function.

---

### P1-4: No Per-URL Timeout Budget Across Fallback Chain (PERFORMANCE)

**File:** `metadata.py:585-800` (`fetch_metadata`)

**Problem:** The fallback chain has no shared timeout budget. Worst case for a single blocked URL:

| Stage | Timeout | Cumulative |
|-------|---------|------------|
| curl_cffi | 25s | 25s |
| residential fetcher | 120s | 145s |
| Wayback Machine | 25s | 170s |
| screenshot (Playwright) | 20s | 190s |
| favicon composite | ~2s | 192s |

A single URL can consume **3+ minutes** before giving up. With `METADATA_CONCURRENCY=4` and 30 stories, a bad batch can stall the scrape cycle for a long time.

**Fix:** Add a deadline-based budget. Pass an `asyncio.Timeout` or absolute deadline through the chain:

```python
async def fetch_metadata(url: str, ..., deadline: float | None = None):
    if deadline is None:
        deadline = time.monotonic() + 60.0  # 60s total budget per URL

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return placeholder_result(url)

    # curl_cffi: min(CFFI_TIMEOUT, remaining)
    # residential: min(RESIDENTIAL_FETCHER_TIMEOUT, remaining)
    # etc.
```

**Impact:** Bounds worst-case per-URL time to ~60s regardless of how many fallback stages fire. Tradeoff: some URLs that *would* succeed given 190s will now time out. For a 15-minute scrape cycle, 60s is the right ceiling.

---

### P2-1: metadata.py is 1296 Lines (MAINTAINABILITY)

**File:** `metadata.py`

**Problem:** Single file handles: HTTP fetching (curl_cffi + aiohttp), HTML parsing (OG tags, descriptions, JSON-LD), image downloading + resizing, PDF rendering, screenshot fallback delegation, Wayback fetching, favicon composite generation, HN card generation, cache management, URL safety validation.

**Fix:** Split into focused modules. Proposed structure:

```
metadata/
  __init__.py          # re-exports fetch_metadata for backward compat
  fetcher.py           # curl_cffi + residential + Wayback HTML fetching
  parser.py            # OG tags, descriptions, JSON-LD extraction
  images.py            # download_and_resize, favicon composite, HN card
  safety.py            # is_public_http_url, resolve_metadata_url
  cache.py             # metadata_cache, should_use_cached_metadata
```

`fetch_metadata()` becomes a thin orchestrator that calls into these modules.

**Effort:** ~3-4 hours. Low risk — pure refactor, no behavior change. Verify with existing `test_metadata.py` (467 lines).

---

### P2-2: hcker_proxy.py Sync urlopen in Async Server (CORRECT BUT FRAGILE)

**File:** `hcker_proxy.py:114-142` (`fetch_hcker_news_html`), `145-173` (`fetch_hcker_news_bytes`)

**Problem:** These functions use `urllib.request.urlopen()` (synchronous) inside an async FastAPI server. They ARE correctly wrapped in `asyncio.to_thread()` at the call sites (lines 387, 514), so they don't block the event loop.

The risk: under burst traffic (60+ concurrent visitors), `asyncio.to_thread` consumes threads from the default `ThreadPoolExecutor` (typically 40 threads on CPython). If many requests hit the proxy simultaneously and hcker.news is slow, the thread pool can exhaust, causing new requests to queue.

**Fix (low priority):** Replace `urllib.request.urlopen()` with `aiohttp`. This makes the proxy fully async, eliminating the thread pool dependency:

```python
async def fetch_hcker_news_html(query: bytes = b"") -> str:
    cache_key = "home:html:view=frontpage" if query == b"view=frontpage" else "home:html"
    if _cache.is_fresh(cache_key):
        return _cache.get(cache_key)
    try:
        url = HCKER_NEWS_ORIGIN + "/"
        if query:
            url += "?" + query.decode("utf-8", errors="ignore")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
        _cache.set(cache_key, html, CACHE_HTML_SOFT, CACHE_HTML_HARD)
        return html
    except Exception as exc:
        ...
```

**Impact:** Marginal under normal load. Meaningful only under burst traffic. Defer unless you see thread pool exhaustion in production.

---

### P3-1: Error Handling — String Tuples Instead of Typed Results (MAINTAINABILITY)

**File:** `metadata.py` (throughout)

**Problem:** Functions return `tuple[str | None, str | None]` for `(html, final_url)` or `tuple[Optional[dict], Optional[str], Optional[str]]` for `(content, method, error)`. Callers can't distinguish "blocked by anti-bot" from "network timeout" from "non-HTML response" without parsing error strings.

**Fix:** Port the `HtmlFetchResult` + `FetchFailure` enum pattern from `is-ai-good-yet/pipeline/src/article_fetch.py`:

```python
class FetchFailure(str, Enum):
    BLOCKED = "blocked"       # 401/403/429/503
    TIMEOUT = "timeout"
    DNS = "dns"
    NON_HTML = "non_html"
    TOO_LARGE = "too_large"
    EMPTY = "empty"
    UNSAFE_URL = "unsafe_url"
    NETWORK = "network"

@dataclass(frozen=True)
class HtmlFetchResult:
    html: str | None
    final_url: str | None
    method: str
    failure: FetchFailure | None = None
    detail: str | None = None
```

This enables smarter fallback decisions (e.g., only try residential on `BLOCKED`, not on `NON_HTML`).

**Effort:** ~2 hours. Best done as part of the P2-1 module split.

---

## Implementation Order

Phased so each phase is independently shippable and verifiable.

### Phase 1: Stop the Bleeding (security + bugs)
| Task | Issue | Effort | Risk |
|------|-------|--------|------|
| Add `--headless=new` to nodriver args | P0-1 | 5 min | Minimal |
| Fix auth to `secrets.compare_digest` | P0-3 | 10 min | Minimal |
| Fix SSRF CGNAT gap (`is_global`) | P1-3 | 30 min | Low |

**Verify:** `residential_fetcher.py` runs without visible window. Auth rejects wrong secrets. `is_public_http_url("http://100.64.0.1")` returns `False`.

### Phase 2: Security Hardening
| Task | Issue | Effort | Risk |
|------|-------|--------|------|
| Manual redirect loop with per-hop SSRF | P1-2 | 1-2 hr | Medium (test redirect chains) |
| Timeout budget per URL | P1-4 | 1-2 hr | Low |

**Verify:** `test_metadata.py` passes. Redirect to `169.254.169.254` is blocked. A single URL never takes >60s.

### Phase 3: Performance + Architecture
| Task | Issue | Effort | Risk |
|------|-------|--------|------|
| Browser singleton in screenshot.py | P1-1 | 1-2 hr | Low |
| Consolidate to Playwright (kill nodriver) | P0-2 | 4-6 hr | Medium (CF solve port) |

**Verify:** Screenshots are faster (benchmark before/after). No visible Chrome window. CF challenges still solve.

### Phase 4: Code Quality (optional, defer if shipping)
| Task | Issue | Effort | Risk |
|------|-------|--------|------|
| Split metadata.py into modules | P2-1 | 3-4 hr | Low (pure refactor) |
| Typed FetchResult enum | P3-1 | 2 hr | Low |
| Migrate proxy to aiohttp | P2-2 | 1-2 hr | Low |

---

## What NOT to Change

These were flagged during the audit and confirmed as **not issues**:

- **`screenshot.py` doesn't close the browser** — FALSE. `browser.close()` is called on line 154 inside the `async with` block. The `except` blocks on lines 155-162 return `None` before reaching the save logic, so the browser is always cleaned up.
- **`tracemalloc.start(10)` leaks memory** — FALSE. `hn_scraper.py:79-80` guards it with `if not tracemalloc.is_tracing()`. It's idempotent.
- **Hardcoded font path in `_generate_hn_card`** — NOT A BUG. `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` exists on the Ubuntu VPS. The function is VPS-only (not run on Windows).
- **Metadata cache is 100 entries** — FALSE. It's 300 (`METADATA_CACHE_MAX_ITEMS`, line 60).
- **No screenshot retry** — FALSE. Retry happens at `fetch_metadata` level via `METADATA_MAX_RETRIES=3` and `should_use_cached_metadata()` (lines 569-582).
- **Feed enrichment has no concurrency control** — FALSE. `feed_enrichment.py` has single-flight locking, cooldown, batch caps, and per-story deduplication.

---

## Reference: is-ai-good-yet Scraper Architecture (for porting)

The sister project's scraper infrastructure has several patterns worth porting:

| Pattern | File | What it does better |
|---------|------|-------------------|
| `ResidentialSettings` dataclass | `residential_service.py:29-50` | Env-driven config, frozen, validated |
| `ResidentialBrowser` class | `residential_service.py:53-161` | Singleton browser, new context per request, clean lifecycle |
| `HtmlFetchResult` + `FetchFailure` | `article_fetch.py:16-42` | Typed errors instead of None tuples |
| `CurlCffiHtmlFetcher` | `article_fetch.py:178-286` | Manual redirect loop, SSRF per hop, streaming read |
| `is_public_http_url` | `article_fetch.py:87-109` | Uses `is_global` (covers CGNAT), cleaner logic |
| `secrets.compare_digest` | `residential_service.py:20-26` | Constant-time auth |
| `redact_url` | `article_fetch.py:45-53` | Strips query params from logs |
| `validate_html_response` | `article_fetch.py:112-175` | Centralized validation |

**What is-ai-good-yet does NOT have that visual-hn should keep:**
- Fanboy filter-list blocking for screenshots (network + cosmetic CSS)
- CF challenge auto-solve (nodriver checkbox click — port to Playwright)
- Favicon composite fallback
- HN card generation for Ask HN posts
- Wayback Machine image URL unwrapping

---

## Open Questions

1. **nodriver vs Playwright for CF bypass:** nodriver's CDP approach auto-passes most CF challenges without explicit solving. Playwright + `playwright_stealth` may need more manual intervention. If CF bypass success rate drops after consolidation, we may need to keep nodriver as a fallback. **Decision needed after Phase 3 testing.**

2. **Timeout budget value:** 60s is proposed. The current residential fetcher timeout is 120s (for hard CF challenges). If we cap at 60s, some slow-but-solvable challenges will fail. **Recommendation: 90s as a compromise.**

3. **Should the screenshot browser and the fetcher browser be the same instance?** Sharing reduces resource usage but couples failure modes. If the screenshot path crashes the browser, the fetcher loses its persistent profile (cf_clearance cookie). **Recommendation: separate browser instances, same framework (Playwright).**
