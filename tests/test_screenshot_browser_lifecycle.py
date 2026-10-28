"""Regression tests: screenshot browser singleton lifecycle.

Covers the wedged-renderer incident found 2026-10-25: a page whose JS never
settles hung the capture coroutine forever and pinned one Chromium renderer at
~100% CPU for 3.5 days. The fix adds a wall-clock capture deadline, a capped
context.close(), and periodic browser recycling.
"""

import asyncio
import time
from io import BytesIO
from types import SimpleNamespace

import playwright.async_api as pw_api
import pytest
from PIL import Image

import screenshot as ss


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Never leak fake browser state into other tests."""
    monkeypatch.setattr(ss, "_browser", None)
    monkeypatch.setattr(ss, "_playwright", None)
    # Fresh timestamp: 0.0 would read as "browser is ancient" and trigger a
    # real-Chromium relaunch inside tests that seed a connected fake browser.
    monkeypatch.setattr(ss, "_browser_started_at", time.monotonic())
    monkeypatch.setattr(ss, "_screenshots_since_launch", 0)
    yield


def _fake_png(width: int = 900, height: int = 600) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height)).save(buf, format="PNG")
    return buf.getvalue()


class FakePage:
    def __init__(self, screenshot_delay: float = 0.0, close_delay: float = 0.0):
        self._screenshot_delay = screenshot_delay
        self._close_delay = close_delay

    async def route(self, pattern, handler):
        pass

    async def goto(self, url, wait_until=None, timeout=None):
        return SimpleNamespace(ok=True, status=200)

    async def add_style_tag(self, content=None):
        pass

    async def screenshot(self, **kwargs):
        if self._screenshot_delay:
            await asyncio.sleep(self._screenshot_delay)
        return _fake_png()


class FakeContext:
    def __init__(self, page: FakePage, close_delay: float = 0.0):
        self._page = page
        self._close_delay = close_delay
        self.closed = False

    async def add_init_script(self, script):
        pass

    async def new_page(self):
        return self._page

    async def close(self):
        if self._close_delay:
            await asyncio.sleep(self._close_delay)
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext, connected: bool = True):
        self._context = context
        self._connected = connected
        self.closed = False

    def is_connected(self):
        return self._connected

    async def new_context(self, **kwargs):
        return self._context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser
        self.launch_calls = 0

    async def launch(self, args=None):
        self.launch_calls += 1
        return self._browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium):
        self.stopped = False
        self.chromium = chromium

    async def stop(self):
        self.stopped = True


class FakeAsyncPlaywright:
    """Stand-in for async_playwright(): .start() returns the instance."""

    def __init__(self, playwright: FakePlaywright):
        self._playwright = playwright

    async def start(self):
        return self._playwright


def _install_fresh_browser(monkeypatch) -> FakeChromium:
    """Point _get_browser's launch path at fakes; return the chromium fake."""
    chromium = FakeChromium(FakeBrowser(FakeContext(FakePage())))
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(
        pw_api, "async_playwright", lambda: FakeAsyncPlaywright(playwright)
    )
    return chromium


async def test_capture_deadline_returns_none_and_closes_context(monkeypatch):
    """A page whose screenshot never finishes must hit the wall-clock deadline,
    return None, and still close the context (no leaked renderer)."""
    monkeypatch.setattr(ss, "SCREENSHOT_DEADLINE_S", 1)
    browser = FakeBrowser(FakeContext(FakePage(screenshot_delay=30)))
    monkeypatch.setattr(ss, "_browser", browser)
    monkeypatch.setattr(ss, "_playwright", FakePlaywright(FakeChromium(browser)))

    result = await ss.capture_screenshot("https://example.com/page")

    assert result is None
    assert browser._context.closed


async def test_capture_success_returns_filename_and_closes_context(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(ss, "IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr(ss, "SCREENSHOT_MIN_BYTES", 1)
    monkeypatch.setattr(ss, "is_image_too_small", lambda filename: False)
    browser = FakeBrowser(FakeContext(FakePage()))
    monkeypatch.setattr(ss, "_browser", browser)
    monkeypatch.setattr(ss, "_playwright", FakePlaywright(FakeChromium(browser)))

    result = await ss.capture_screenshot("https://example.com/ok")

    assert result is not None
    assert result.endswith("_screenshot.jpg")
    assert browser._context.closed


async def test_hanging_context_close_is_capped(monkeypatch, tmp_path):
    """context.close() itself can hang on a wedged renderer — the close cap
    must bound it so the coroutine (and the screenshot lock) is released."""
    monkeypatch.setattr(ss, "IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr(ss, "SCREENSHOT_MIN_BYTES", 1)
    monkeypatch.setattr(ss, "_CLOSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ss, "is_image_too_small", lambda filename: False)
    browser = FakeBrowser(FakeContext(FakePage(), close_delay=30))
    monkeypatch.setattr(ss, "_browser", browser)
    monkeypatch.setattr(ss, "_playwright", FakePlaywright(FakeChromium(browser)))

    started = time.monotonic()
    result = await ss.capture_screenshot("https://example.com/close-hang")

    assert result is not None
    assert time.monotonic() - started < 10


async def test_browser_recycles_after_max_shots(monkeypatch):
    chromium = _install_fresh_browser(monkeypatch)
    old_browser = FakeBrowser(FakeContext(FakePage()))
    monkeypatch.setattr(ss, "_browser", old_browser)
    monkeypatch.setattr(ss, "_playwright", FakePlaywright(FakeChromium(old_browser)))
    monkeypatch.setattr(ss, "_screenshots_since_launch", ss.BROWSER_MAX_SHOTS)
    monkeypatch.setattr(ss, "_browser_started_at", time.monotonic())

    browser = await ss._get_browser()

    assert old_browser.closed, "old browser must be closed on recycle"
    assert chromium.launch_calls == 1, "a fresh browser must be launched"
    assert browser is not old_browser
    assert ss._screenshots_since_launch == 0


async def test_browser_recycles_after_max_age(monkeypatch):
    chromium = _install_fresh_browser(monkeypatch)
    old_browser = FakeBrowser(FakeContext(FakePage()))
    monkeypatch.setattr(ss, "_browser", old_browser)
    monkeypatch.setattr(ss, "_playwright", FakePlaywright(FakeChromium(old_browser)))
    monkeypatch.setattr(
        ss, "_browser_started_at", time.monotonic() - ss.BROWSER_MAX_AGE_S - 1
    )

    browser = await ss._get_browser()

    assert old_browser.closed
    assert chromium.launch_calls == 1
    assert browser is not old_browser


async def test_young_browser_is_not_recycled(monkeypatch):
    chromium = _install_fresh_browser(monkeypatch)
    browser = FakeBrowser(FakeContext(FakePage()))
    monkeypatch.setattr(ss, "_browser", browser)
    monkeypatch.setattr(ss, "_playwright", FakePlaywright(FakeChromium(browser)))
    monkeypatch.setattr(ss, "_screenshots_since_launch", 1)
    monkeypatch.setattr(ss, "_browser_started_at", time.monotonic())

    got = await ss._get_browser()

    assert got is browser
    assert chromium.launch_calls == 0
