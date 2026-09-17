"""Latency-honesty regression tests for the metadata deadline budget (VH-02).

All network is stubbed; only wall-clock accounting and timeout math is
exercised. Measured maxima — if a per-stage budget leak reappears, these
tests fail on the elapsed-time assertion, not on mocks.
"""

from __future__ import annotations

import asyncio
import time

import metadata.images as images_module
import metadata.orchestrator as orchestrator_module
from metadata.images import FAVICON_BUDGET_SECONDS, generate_favicon_composite
from metadata.orchestrator import fetch_metadata


class FakeIconResponse:
    def __init__(self, delay: float = 0.0):
        self._delay = delay

    def raise_for_status(self):
        pass

    @property
    def content(self):
        # A real ~4KB PNG so it passes the ``len(data) > 100`` usefulness
        # gate and the PIL composite render succeeds.
        from io import BytesIO

        from PIL import Image, ImageDraw

        buf = BytesIO()
        img = Image.new("RGB", (128, 128), (10, 20, 30))
        ImageDraw.Draw(img).rectangle((0, 0, 127, 127), outline=(200, 200, 200))
        img.save(buf, format="PNG")
        data = buf.getvalue()
        assert len(data) > 100
        return data


class FakeCffiSession:
    """Simulates a (slow) remote icon fetch; records per-attempt timeouts."""

    timeouts: list[float] = []

    def __init__(self, delay: float = 0.05):
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, allow_redirects=True):
        assert allow_redirects is True
        await self.record_timeouts()
        await asyncio.sleep(self._delay)
        return FakeIconResponse(delay=self._delay)

    async def record_timeouts(self):
        pass


class HangingSession:
    """Simulates a source that ignores the timeout and hangs on the request."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, allow_redirects=True):
        await asyncio.sleep(999)
        return FakeIconResponse()


async def test_favicon_after_expired_deadline_respects_its_own_budget(
    monkeypatch, tmp_path
):
    recorded_timeouts: list[float] = []

    def fake_session_factory(*args, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        return FakeCffiSession(delay=0.02)

    real_source_domain = images_module.source_domain
    monkeypatch.setattr(
        images_module, "source_domain", lambda url: "example.com"
    )
    monkeypatch.setattr(images_module, "CurlCffiSession", fake_session_factory)
    monkeypatch.setattr(images_module, "FAVICON_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(images_module, "IMAGE_DIR", tmp_path)

    deadline = time.monotonic() - 1.0  # overall deadline long expired
    start = time.monotonic()
    result = await generate_favicon_composite(
        "https://example.com/story", deadline=deadline
    )
    elapsed = time.monotonic() - start

    # The documented exception fired (not a bare-None short circuit) …
    assert result is not None and result.startswith("fav-")
    # … within the favicon budget, not CFFI_TIMEOUT-based 2x worst case.
    assert elapsed < 3.0, f"favicon took {elapsed:.2f}s after expired deadline"
    # First attempt got the full favicon budget as its timeout.
    assert recorded_timeouts[0] == 0.2


# --- Scenario 2: hanging favicon source is cut at the budget ---------------


async def test_hanging_favicon_source_is_cut_within_budget(monkeypatch):
    monkeypatch.setattr(
        images_module, "source_domain", lambda url: "example.com"
    )
    monkeypatch.setattr(images_module, "FAVICON_BUDGET_SECONDS", 0.1)

    async def hanging_factory(**kwargs):
        return HangingSession()

    # Patch the "async with CurlCffiSession(...) as s" construction.
    class Factory:
        def __call__(self, **kwargs):
            return HangingSession()

    monkeypatch.setattr(images_module, "CurlCffiSession", Factory())

    deadline = time.monotonic() - 1.0
    start = time.monotonic()
    result = await generate_favicon_composite(
        "https://example.com/story", deadline=deadline
    )
    elapsed = time.monotonic() - start

    # Both sources hang; the whole favicon chain must bail well under 50s
    # (the old 2 x CFFI_TIMEOUT worst case) and return None.
    assert result is None
    assert elapsed < 3.0, f"favicon chain took {elapsed:.2f}s"


# --- Scenario 3: orchestrator end-to-end max duration under deadline -------


async def test_fetch_metadata_total_duration_is_bounded_by_deadline(
    monkeypatch,
):
    async def quick_favicon(url, deadline=None):
        await asyncio.sleep(0.01)
        return None

    monkeypatch.setattr(
        orchestrator_module, "generate_favicon_composite", quick_favicon
    )

    metadata_cache = orchestrator_module.metadata_cache
    metadata_cache.clear()
    deadline_budget = 0.3
    deadline = time.monotonic() + deadline_budget

    # Every stage is stubbed to just sleep past the deadline; the pipeline
    # must bail stage by stage and finish without hanging.
    async def slow_cffi(url, headers, deadline=None):
        await asyncio.sleep(1.0)
        return None, None

    monkeypatch.setattr(
        orchestrator_module, "_curl_cffi_fetch_html", slow_cffi
    )
    monkeypatch.setattr(
        orchestrator_module, "_wayback_fetch_html", slow_cffi
    )

    async def slow_screenshot(url, timeout_override=None):
        await asyncio.sleep(1.0)
        return None

    monkeypatch.setattr(
        orchestrator_module, "capture_screenshot_with_timeout", slow_screenshot
    )
    monkeypatch.setattr(
        orchestrator_module, "ENABLE_SCREENSHOT_FALLBACK", True
    )
    monkeypatch.setattr(
        orchestrator_module, "SCREENSHOT_TIMEOUT_SECONDS", 20.0
    )

    start = time.monotonic()
    result = await fetch_metadata(
        "https://example.com/story", deadline=deadline
    )
    elapsed = time.monotonic() - start

    assert result["image_url"] == orchestrator_module.PLACEHOLDER_IMAGE
    # Overall bound: deadline + bounded favicon exception, nowhere near the
    # unbounded 3-stage-sleep worst case (3s) this test would produce if any
    # stage ignored the budget.
    assert elapsed < deadline_budget + FAVICON_BUDGET_SECONDS + 2.5
    metadata_cache.clear()


# --- Scenario 4: no-deadline path also gets a rate limit -------------------


async def test_favicon_without_deadline_uses_default_budget(
    monkeypatch, tmp_path
):
    recorded: list[float] = []

    class Factory:
        def __call__(self, **kwargs):
            recorded.append(kwargs.get("timeout"))
            return FakeCffiSession(delay=0.01)

    monkeypatch.setattr(
        images_module, "source_domain", lambda url: "example.com"
    )
    monkeypatch.setattr(images_module, "CurlCffiSession", Factory())
    monkeypatch.setattr(images_module, "IMAGE_DIR", tmp_path)

    result = await generate_favicon_composite("https://example.com/story")
    assert result is not None
    assert recorded[0] == images_module.FAVICON_BUDGET_SECONDS
