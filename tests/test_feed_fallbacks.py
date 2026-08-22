"""Regression tests for the feed-preview fallback fixes.

Covers:
1. Favicon composite runs even when the metadata deadline is exhausted.
2. Residential fetcher budget is capped below the metadata deadline.
3. Feed enrichment retries stories stuck on placeholder on timeline re-sight.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

import metadata
import feed_enrichment
from metadata import orchestrator
from metadata.cache import PLACEHOLDER_IMAGE

# ── Fix 1: favicon composite exempt from deadline ────────────────────────────


async def test_favicon_composite_runs_after_deadline_exhausted(monkeypatch):
    """A URL whose earlier layers burned the whole deadline must still get a
    favicon card instead of a bare placeholder."""

    async def no_html(url, headers=None, deadline=None):
        return None, None

    async def no_wayback(url):
        return None, None

    async def no_screenshot(url, timeout_override=None):
        return None

    async def fake_favicon(url):
        return "fav-testcard.jpg"

    monkeypatch.setattr(orchestrator, "_curl_cffi_fetch_html", no_html)
    monkeypatch.setattr(orchestrator, "_wayback_fetch_html", no_wayback)
    monkeypatch.setattr(orchestrator, "capture_screenshot_with_timeout", no_screenshot)
    monkeypatch.setattr(orchestrator, "generate_favicon_composite", fake_favicon)
    monkeypatch.setattr(orchestrator, "ENABLE_SCREENSHOT_FALLBACK", True)
    metadata.metadata_cache.clear()
    try:
        result = await metadata.fetch_metadata(
            "https://example.com/blocked-story",
            deadline=__import__("time").monotonic() - 1,  # already exhausted
        )
    finally:
        metadata.metadata_cache.clear()

    assert result["image_url"] == "/static/images/fav-testcard.jpg"


async def test_favicon_composite_failure_still_yields_placeholder(monkeypatch):
    """If even the favicon composite fails (CDN down), the placeholder is the
    final answer and the pipeline must not crash."""

    async def no_html(url, headers=None, deadline=None):
        return None, None

    async def no_wayback(url):
        return None, None

    async def no_screenshot(url, timeout_override=None):
        return None

    async def no_favicon(url):
        return None

    monkeypatch.setattr(orchestrator, "_curl_cffi_fetch_html", no_html)
    monkeypatch.setattr(orchestrator, "_wayback_fetch_html", no_wayback)
    monkeypatch.setattr(orchestrator, "capture_screenshot_with_timeout", no_screenshot)
    monkeypatch.setattr(orchestrator, "generate_favicon_composite", no_favicon)
    metadata.metadata_cache.clear()
    try:
        result = await metadata.fetch_metadata(
            "https://example.com/doomed-story",
            deadline=__import__("time").monotonic() - 1,
        )
    finally:
        metadata.metadata_cache.clear()

    assert result["image_url"] == PLACEHOLDER_IMAGE


# ── Fix 3: residential budget capped below deadline ──────────────────────────


def test_residential_budget_capped_below_deadline():
    """The cap constant must leave headroom for screenshot + favicon inside
    the 90s metadata deadline."""
    assert metadata.RESIDENTIAL_FETCHER_MAX_BUDGET < metadata.METADATA_DEADLINE_SECONDS


async def test_curl_cffi_defers_to_residential_with_capped_budget(monkeypatch):
    """When the deadline has more time than the cap, the residential fetcher
    receives the capped budget, not the full remaining deadline."""
    import metadata.fetcher as fetcher_mod
    import time as time_mod

    captured = {}

    async def fake_residential(url, timeout_override=None):
        captured["timeout"] = timeout_override
        return None, None

    class BlockedResponse:
        status_code = 403

    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *a, **kw):
            class Exc(Exception):
                response = BlockedResponse()

            raise Exc()

    monkeypatch.setattr(fetcher_mod, "RESIDENTIAL_FETCHER_URL", "http://node:8765")
    monkeypatch.setattr(fetcher_mod, "RESIDENTIAL_FETCHER_MAX_BUDGET", 45.0)
    monkeypatch.setattr(fetcher_mod, "CurlCffiSession", FakeSession)
    monkeypatch.setattr(fetcher_mod, "_residential_fetch_html", fake_residential)

    deadline = time_mod.monotonic() + 90  # plenty of remaining time
    html, final_url = await fetcher_mod._curl_cffi_fetch_html(
        "https://example.com/hardened", headers={}, deadline=deadline
    )

    assert html is None
    assert captured["timeout"] == 45.0


# ── Fix 2: placeholder retry on timeline re-sight ────────────────────────────


@pytest.fixture
async def feed_db(monkeypatch):
    """Point feed_enrichment's async_session at an in-memory SQLite DB.

    Mirrors database.py: async_session is a sessionmaker called synchronously,
    returning an object usable as an async context manager.
    """
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # sessionmaker bound to the test engine — same call shape as production
    test_factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(feed_enrichment, "async_session", test_factory)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def _story(sid: int, image_url: str = PLACEHOLDER_IMAGE, **kw) -> dict:
    return {
        "id": sid,
        "title": f"Story {sid}",
        "url": f"https://example.com/{sid}",
        "hn_url": f"https://news.ycombinator.com/item?id={sid}",
        "score": 10,
        "poster": "tester",
        "comments_count": 1,
        "time_posted": datetime.now() - timedelta(days=5),
        "image_url": image_url,
        **kw,
    }


async def _insert_story(session, story: dict):
    from models import Story

    session.add(Story(**story))
    await session.commit()


async def test_retry_placeholder_upgrades_story(feed_db, monkeypatch):
    """_retry_placeholder re-runs metadata and writes the upgraded image."""
    from models import Story

    session = feed_enrichment.async_session()
    await _insert_story(session, _story(1001))
    await session.close()

    async def fake_fetch(url, fallback_text="", enable_screenshot=False, **kw):
        return {
            "image_url": "/static/images/abc123_screenshot.jpg",
            "og_image_url": None,
            "description": "upgraded",
        }

    monkeypatch.setattr(feed_enrichment, "fetch_metadata", fake_fetch)

    assert await feed_enrichment._retry_placeholder(1001) is True

    session = feed_enrichment.async_session()
    story = await session.get(Story, 1001)
    assert story.image_url == "/static/images/abc123_screenshot.jpg"
    await session.close()


async def test_retry_placeholder_skips_non_placeholder(feed_db):
    """Stories with a real image are never re-fetched."""
    session = feed_enrichment.async_session()
    await _insert_story(
        session, _story(1002, image_url="/static/images/real_screenshot.jpg")
    )
    await session.close()

    assert await feed_enrichment._retry_placeholder(1002) is False


async def test_retry_placeholder_keeps_placeholder_when_fetch_still_fails(
    feed_db, monkeypatch
):
    """If metadata still fails, the story stays on placeholder (no crash,
    no bogus overwrite)."""
    from models import Story

    session = feed_enrichment.async_session()
    await _insert_story(session, _story(1003))
    await session.close()

    async def fake_fetch(url, fallback_text="", enable_screenshot=False, **kw):
        return {
            "image_url": PLACEHOLDER_IMAGE,
            "og_image_url": None,
            "description": "still blocked",
        }

    monkeypatch.setattr(feed_enrichment, "fetch_metadata", fake_fetch)

    assert await feed_enrichment._retry_placeholder(1003) is False

    session = feed_enrichment.async_session()
    story = await session.get(Story, 1003)
    assert story.image_url == PLACEHOLDER_IMAGE
    await session.close()


async def test_enrich_batch_retries_placeholder_stories(feed_db, monkeypatch):
    """enrich_missing_stories picks up placeholder stories present in the
    timeline batch and retries them, bounded by RETRY_MAX_PER_CYCLE."""
    from models import Story

    session = feed_enrichment.async_session()
    for sid in range(2001, 2011):  # 10 placeholder stories, more than the cap of 5
        await _insert_story(session, _story(sid, time_posted=datetime(2026, 8, 1)))
    # one healthy story that must NOT be retried
    await _insert_story(session, _story(2099, image_url="/static/images/ok.jpg"))
    await session.close()

    retried: list[int] = []

    async def fake_retry(sid: int) -> bool:
        retried.append(sid)
        return True

    monkeypatch.setattr(feed_enrichment, "_retry_placeholder", fake_retry)
    monkeypatch.setattr(
        feed_enrichment,
        "get_known_ids",
        lambda: asyncio.sleep(0, set(range(2001, 2100))),
    )

    await feed_enrichment.enrich_missing_stories(list(range(2001, 2011)))

    assert len(retried) == feed_enrichment.RETRY_MAX_PER_CYCLE
    assert 2099 not in retried
