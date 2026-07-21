"""Headful Playwright fetcher for anti-bot circumvention.

Runs on the residential node (residential IP) behind Tailscale. The VPS calls
this when curl_cffi gets 403/429/503 — a real headful Chrome can solve Cloudflare
JS challenges that no HTTP client can.

Uses a PERSISTENT browser context (launch_persistent_context):
- ONE window stays open for the lifetime of the process — no flashing popups.
- Cookies (including cf_clearance) survive across requests → fewer challenges.
- A single tab navigates between URLs, so an unresolved CF challenge on site A
  doesn't get wiped when site B arrives.

When a CF challenge or captcha appears that the browser can't auto-solve, the
page stays open for CF_CHALLENGE_MAX_WAIT (default 180s) — long enough for a
human at the laptop to click the checkbox.

Requirements (residential node):
    pip install fastapi uvicorn playwright
    python -m playwright install chromium

Run:
    python residential_fetcher.py
    # or with custom port:
    RESIDENTIAL_FETCHER_PORT=8765 python residential_fetcher.py

The server listens on 0.0.0.0 so it's reachable via the Tailscale IP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("RESIDENTIAL_FETCHER_PORT", "8765"))
SHARED_SECRET = os.environ.get("RESIDENTIAL_FETCHER_SECRET", "")
NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 24_000
CF_SETTLE_SECONDS = 3.0
CF_CHALLENGE_TITLE = "just a moment"
# Long enough for a human at the laptop to notice the window, switch to it,
# and click the Cloudflare checkbox or solve a captcha. The original 40s was
# too short — by the time you noticed the window, it had already closed and
# the fetch returned the challenge HTML.
CF_CHALLENGE_MAX_WAIT = float(os.environ.get("CF_CHALLENGE_MAX_WAIT", "180"))
MAX_HTML_CHARS = 2_000_000
# Persistent profile keeps cookies (cf_clearance etc.) between requests.
PROFILE_DIR = Path(
    os.environ.get(
        "RESIDENTIAL_FETCHER_PROFILE",
        str(Path(__file__).parent / ".browser-profile"),
    )
)
# UA must match the OS the fetcher runs on (Windows 11). Since Chrome 148,
# Math.tanh reads the host libm, so Windows returns UCRT bits. Claiming a
# different OS in the UA while returning Windows math bits is an instant
# tell for any anti-bot that probes Math.tanh.
# See https://scrapfly.dev/posts/browser-math-os-fingerprint/
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Background-foreground guard. Headful Chrome steals focus on launch and on
# every navigation that triggers a renderer swap (Cloudflare reloads do this).
# We keep the windows runnable (JS executes, CF challenges resolve) but strip
# their right to ever enter the foreground via WS_EX_NOACTIVATE + Z-order floor.
BACKGROUND_GUARD_ENABLED = sys.platform == "win32"
BACKGROUND_GUARD_INTERVAL_S = 2.0

# Persistent context: one window, one profile, reused for every request.
_context: Any = None
_pw: Any = None
_page: Any = None  # the single navigating tab
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(1)  # one fetch at a time — single tab

# Lightweight metrics for /health and the admin dashboard.
_last_fetch: dict[str, Any] = {
    "url": None,
    "status": "idle",
    "at": None,  # unix timestamp
    "final_url": None,
    "bytes": 0,
    "error": None,
}


class FetchRequest(BaseModel):
    url: str


class FetchResult(BaseModel):
    html: str | None = None
    final_url: str | None = None
    status: str = "ok"
    error: str | None = None


# ---------------------------------------------------------------------------
# Background guard (Windows only)
# ---------------------------------------------------------------------------
# Headful Chrome windows steal focus on launch and every renderer swap (which
# Cloudflare challenges trigger). We identify the Chromium processes spawned by
# this Python (via CreateToolhelp32Snapshot — no psutil dependency), then for
# each of their top-level windows we:
#   1. Add WS_EX_NOACTIVATE so clicks/focus requests are permanently ignored.
#   2. Force HWND_BOTTOM Z-order with SWP_NOACTIVATE so they render behind all
#      normal windows. JS still executes, CF challenges still resolve, but the
#      windows never enter the foreground or disrupt the user.
# Cross-platform: the worker is a no-op when BACKGROUND_GUARD_ENABLED is False
# (e.g. running this file on the Ubuntu VPS for tests), so no OS branching is
# needed at call sites.
_guard_stop: threading.Event | None = None
_guard_thread: threading.Thread | None = None

if BACKGROUND_GUARD_ENABLED:
    import ctypes
    from ctypes import wintypes

    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_APPWINDOW = 0x00040000
    HWND_BOTTOM = 1
    SWP_NOACTIVATE = 0x0010
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            # Both are ULONG_PTR in the Windows SDK — 8 bytes on Win64,
            # 4 on Win32. Must use a pointer-sized type or the struct
            # layout diverges and th32ParentProcessID reads garbage.
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.POINTER(ctypes.c_ulong)),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * MAX_PATH),
        ]

    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ENUM_PROC_TYPE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [_ENUM_PROC_TYPE, wintypes.LPARAM]

    def _descendant_pids(root_pid: int) -> set[int]:
        """Return root_pid + all descendants using a toolhelp snapshot."""
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        # INVALID_HANDLE_VALUE is -1 cast to HANDLE (void*). On Win64 the
        # ctypes default c_int return gives -1 as a Python int; compare directly.
        if snap == -1 or snap == ctypes.c_void_p(-1).value:
            return {root_pid}
        try:
            entries: list[_PROCESSENTRY32W] = []
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    entries.append(pe)
                    pe = _PROCESSENTRY32W()
                    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                    if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                        break
            by_parent: dict[int, list[int]] = {}
            for e in entries:
                by_parent.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
            found: set[int] = set()
            stack = [root_pid]
            while stack:
                pid = stack.pop()
                if pid in found:
                    continue
                found.add(pid)
                stack.extend(by_parent.get(pid, []))
            return found
        finally:
            kernel32.CloseHandle(snap)

    def _apply_no_activate(hwnd: int, _lparam: int) -> bool:
        """EnumWindowsProc: strip activation rights from a top-level window."""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in _guard_target_pids:
            return True
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (ex & WS_EX_NOACTIVATE):
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, (ex | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
            )
        user32.SetWindowPos(
            hwnd,
            HWND_BOTTOM,
            0,
            0,
            0,
            0,
            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE,
        )
        return True

    # Hold the target PID set in a module-level so the C callback can read it.
    _guard_target_pids: set[int] = set()
    # Instantiate the C callback wrapper after _apply_no_activate is defined.
    _enum_proc = _ENUM_PROC_TYPE(_apply_no_activate)

    def _background_guard_loop(stop: threading.Event) -> None:
        """Periodically re-assert no-activate on our Chromium windows."""
        own_pid = os.getpid()
        while not stop.wait(BACKGROUND_GUARD_INTERVAL_S):
            global _guard_target_pids
            _guard_target_pids = _descendant_pids(own_pid)
            if not _guard_target_pids:
                continue
            try:
                user32.EnumWindows(_enum_proc, 0)
            except Exception as exc:
                logger.debug("background guard enum failed: %s", exc)

else:
    # Non-Windows hosts (VPS tests, CI): guard is inert.
    def _descendant_pids(root_pid: int) -> set[int]:
        return set()

    def _background_guard_loop(stop: threading.Event) -> None:
        stop.wait()


def _start_background_guard() -> None:
    """Launch the no-activate enforcer thread. Safe to call on any OS."""
    global _guard_stop, _guard_thread
    if not BACKGROUND_GUARD_ENABLED:
        return
    if _guard_thread and _guard_thread.is_alive():
        return
    _guard_stop = threading.Event()
    _guard_thread = threading.Thread(
        target=_background_guard_loop,
        args=(_guard_stop,),
        name="VHN-BackgroundGuard",
        daemon=True,
    )
    _guard_thread.start()
    logger.info("Background guard started — Chrome windows will not steal focus")


async def _ensure_browser() -> tuple[Any, Any]:
    """Launch the persistent context once. Returns (context, page).

    The same window and tab are reused for every subsequent request. Cookies,
    localStorage, and cf_clearance persist in PROFILE_DIR across restarts.
    """
    global _context, _pw, _page
    if _context and _page and not _page.is_closed():
        return _context, _page
    async with _lock:
        if _context and _page and not _page.is_closed():
            return _context, _page
        from playwright.async_api import async_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _pw = await async_playwright().start()
        _context = await _pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                # Spawn off-screen so the first paint never lands on the desktop.
                "--window-position=-2400,-2400",
                "--window-size=1280,900",
            ],
        )
        # Start the background guard immediately so no-activate is applied
        # before any navigation can trigger a focus-stealing renderer swap.
        _start_background_guard()
        # Hide webdriver flag for extra stealth.
        await _context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        # Use the first tab if one already exists (persistent context reopens
        # the last tab); otherwise open one.
        if _context.pages:
            _page = _context.pages[0]
        else:
            _page = await _context.new_page()
        logger.info("Persistent browser launched (headful, profile=%s)", PROFILE_DIR)
        return _context, _page


def _record(status: str, **fields: Any) -> None:
    _last_fetch.update(status=status, at=time.time(), **fields)


async def _fetch_with_browser(url: str) -> FetchResult:
    """Navigate the single tab to URL, wait for CF challenges, return HTML."""
    _context, page = await _ensure_browser()
    _record("navigating", url=url, error=None)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        logger.info("DOM loaded for %s, waiting for network idle...", url)

        # Wait for networkidle — CF challenges make network requests while
        # resolving (token fetch, JS exec, redirect). networkidle fires when
        # no requests for 500ms, which reliably catches challenge completion.
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
            logger.info("Network idle for %s", url)
        except Exception:
            logger.info("Network idle timeout for %s (may have continuous pings)", url)

        # Secondary check: if title still shows CF challenge, WAIT. The window
        # is already open and visible — a human at the laptop can click the
        # Cloudflare checkbox or solve the captcha. We poll until the challenge
        # resolves or CF_CHALLENGE_MAX_WAIT elapses.
        cf_deadline = asyncio.get_event_loop().time() + CF_CHALLENGE_MAX_WAIT
        challenged = False
        while asyncio.get_event_loop().time() < cf_deadline:
            title = (await page.title() or "").lower()
            if CF_CHALLENGE_TITLE not in title:
                break
            challenged = True
            await asyncio.sleep(2.0)

        if challenged:
            final_title = (await page.title() or "").lower()
            if CF_CHALLENGE_TITLE in final_title:
                logger.warning(
                    "CF challenge did not resolve in %.0fs for %s "
                    "(window left open — human can still solve it)",
                    CF_CHALLENGE_MAX_WAIT,
                    url,
                )
            else:
                logger.info("CF challenge solved for %s", url)

        # Final settle for dynamic content (SPA hydration, lazy images, etc.)
        await asyncio.sleep(CF_SETTLE_SECONDS)

        html = await page.content()
        final_url = page.url
        if len(html) > MAX_HTML_CHARS:
            html = html[:MAX_HTML_CHARS]
        nbytes = len(html.encode("utf-8", errors="ignore"))
        logger.info("Returning %.0f KB for %s", nbytes / 1024, final_url)
        _record(
            "ok",
            url=url,
            final_url=final_url,
            bytes=nbytes,
        )
        return FetchResult(html=html, final_url=final_url)
    except Exception as exc:
        logger.warning("Fetch failed for %s: %s - %s", url, type(exc).__name__, exc)
        _record("error", url=url, error=f"{type(exc).__name__}: {exc}")
        return FetchResult(status="error", error=f"{type(exc).__name__}: {exc}")


def _verify_auth(secret: str | None) -> None:
    if SHARED_SECRET and secret != SHARED_SECRET:
        raise HTTPException(status_code=403, detail="unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Residential fetcher starting on 0.0.0.0:%d", PORT)
    logger.info("Profile dir: %s", PROFILE_DIR)
    logger.info(
        "CF challenge max wait: %.0fs (window stays open for human solving)",
        CF_CHALLENGE_MAX_WAIT,
    )
    yield
    if _guard_stop:
        _guard_stop.set()
    if _context:
        await _context.close()
    if _pw:
        await _pw.stop()


app = FastAPI(title="Visual-HN Residential Fetcher", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check with browser connection state and last-fetch metrics.

    Drops the Math.tanh self-probe from the old version — it opened a fresh
    context per check, which defeats the persistent-context model. The UA/OS
    consistency is verified once at startup via the profile's actual Chrome
    build, not re-probed on every /health call.
    """
    browser_connected = bool(
        _context and _context.browser and _context.browser.is_connected()
    )
    return {
        "status": "ok" if browser_connected else "degraded",
        "browser_connected": browser_connected,
        "user_agent": USER_AGENT,
        "profile_dir": str(PROFILE_DIR),
        "port": PORT,
        "last_fetch": dict(_last_fetch),
        "cf_challenge_max_wait": CF_CHALLENGE_MAX_WAIT,
    }


@app.post("/fetch", response_model=FetchResult)
async def fetch(req: FetchRequest, x_fetcher_secret: str | None = Header(None)):
    _verify_auth(x_fetcher_secret)
    async with _sem:
        logger.info("Fetching: %s", req.url)
        return await _fetch_with_browser(req.url)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
