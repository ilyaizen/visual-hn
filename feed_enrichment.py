"""Feed-aware enrichment — background-enrich story IDs seen via proxied hcker.news timeline.

Rate-limited: at most one enrichment batch every ENRICHMENT_COOLDOWN_SECONDS, regardless
of how many visitors scroll the timeline. Without this, every concurrent visitor triggers
an independent batch — each spawning Chromium screenshots and residential fetcher calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import aiohttp
from sqlalchemy import select

from database import async_session
from metadata import fetch_metadata
from models import Story

logger = logging.getLogger(__name__)

ENRICHMENT_CONCURRENCY = 2
ENRICHMENT_BATCH_SIZE = 10
ENRICHMENT_MAX_PER_CYCLE = 15  # cap total stories enriched per triggered batch
ENRICHMENT_COOLDOWN_SECONDS = 300  # min 5 minutes between enrichment runs
RETRY_MAX_PER_CYCLE = 5  # cap placeholder retries per triggered batch
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"

# In-flight set to avoid duplicate enrichment for the same ID
_enriching: set[int] = set()
_enrichment_lock = asyncio.Lock()

# Single-flight + cooldown: only one enrichment batch runs at a time, and at most
# once per cooldown window. Without these, every visitor's timeline scroll spawns
# an independent batch of Chromium screenshots + residential fetcher calls.
_last_enrichment_time: float = 0.0
_enrichment_running = False


async def get_known_ids() -> set[int]:
    """Return all story IDs currently in the database."""
    async with async_session() as session:
        result = await session.execute(select(Story.id))
        return {row[0] for row in result}


async def _fetch_hn_story(story_id: int) -> dict | None:
    """Fetch a single story's details from the HN API."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                HN_ITEM_URL.format(story_id), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception as exc:
        logger.debug("Failed to fetch HN story %d: %s", story_id, exc)
        return None


async def _enrich_one(story_id: int) -> bool:
    """Fetch story details + metadata and insert into DB. Returns True on success."""
    story_data = await _fetch_hn_story(story_id)
    if not story_data or story_data.get("type") != "story":
        return False

    url = story_data.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
    fallback_text = story_data.get("text") or story_data.get("title") or ""

    # Timeline/feed stories need the same screenshot fallback as front-page
    # stories. Without this, any feed-only story lacking a usable og:image
    # degrades to placeholder/favicon and disappears from the image API.
    metadata = await fetch_metadata(url, fallback_text, enable_screenshot=True)
    metadata.pop("retries", None)  # bookkeeping field, not a Story column

    async with async_session() as session:
        existing = await session.get(Story, story_id)
        if existing:
            # Story already exists (scraped by top-30 loop) — skip
            return False

        story = Story(
            id=story_id,
            title=story_data.get("title"),
            url=url,
            hn_url=f"https://news.ycombinator.com/item?id={story_id}",
            score=story_data.get("score"),
            poster=story_data.get("by"),
            comments_count=story_data.get("descendants"),
            time_posted=datetime.fromtimestamp(story_data.get("time", 0)),
            text=story_data.get("text"),
            current_position=None,  # not on our tracked top-N
            last_position=None,
            trend="same",
            **metadata,
        )
        session.add(story)
        await session.commit()
        logger.info("Enriched new story %d: %s", story_id, story_data.get("title"))
        return True


async def _retry_placeholder(story_id: int) -> bool:
    """Re-run metadata for a story stuck on placeholder. Returns True on upgrade.

    Feed stories are inserted once and never revisited by the scrape cycle, so
    a transient failure (residential node off, deadline burned) leaves them on
    placeholder forever. Re-sight on the timeline is the retry trigger.
    fetch_metadata's own retry counter (METADATA_MAX_RETRIES) stops the loop
    once a URL has failed 3 times.
    """
    async with async_session() as session:
        story = await session.get(Story, story_id)
        if story is None or "placeholder" not in (story.image_url or ""):
            return False
        url = story.url

    metadata = await fetch_metadata(url, enable_screenshot=True)
    new_image = metadata.get("image_url") or ""
    if "placeholder" in new_image:
        return False

    # Drop 'retries' — bookkeeping field, not a Story column
    metadata.pop("retries", None)
    async with async_session() as session:
        story = await session.get(Story, story_id)
        if story is None:
            return False
        for key, value in metadata.items():
            if hasattr(story, key):
                setattr(story, key, value)
        await session.commit()
        logger.info("Placeholder retry upgraded story %d: %s", story_id, new_image)
        return True


async def enrich_missing_stories(timeline_ids: list[int]) -> None:
    """Enrich story IDs from the proxied timeline that are missing from local DB.

    Rate-limited: at most one concurrent batch, with a minimum cooldown between
    batches. Fire-and-forget calls from multiple visitors are silently dropped
    if a batch is running or the cooldown hasn't elapsed.

    Stories already in the DB but still on placeholder get a bounded retry
    (RETRY_MAX_PER_CYCLE per batch, oldest first).
    """
    global _last_enrichment_time, _enrichment_running

    if not timeline_ids:
        return

    # Single-flight + cooldown gate. Without this, every visitor scrolling the
    # timeline spawns an independent batch — each launching Chromium processes
    # (screenshots, residential fetcher calls) that exhaust the VPS.
    async with _enrichment_lock:
        if _enrichment_running:
            logger.debug("Enrichment already running, skipping")
            return
        now = time.monotonic()
        if now - _last_enrichment_time < ENRICHMENT_COOLDOWN_SECONDS:
            remaining = int(ENRICHMENT_COOLDOWN_SECONDS - (now - _last_enrichment_time))
            logger.debug("Enrichment cooldown (%ds left), skipping", remaining)
            return
        _enrichment_running = True

    try:
        known = await get_known_ids()
        missing = [sid for sid in timeline_ids if sid not in known]

        # Placeholder retry candidates: already in DB, still on placeholder,
        # re-sighted on the timeline. Oldest first so stale stories drain.
        placeholder_ids: list[int] = []
        async with async_session() as session:
            rows = await session.execute(
                select(Story.id, Story.time_posted).where(
                    Story.id.in_(timeline_ids),
                    Story.image_url.like("%placeholder%"),
                )
            )
            placeholder_ids = [
                sid for sid, _ in sorted(rows.all(), key=lambda r: r[1] or datetime.max)
            ][:RETRY_MAX_PER_CYCLE]

        if not missing and not placeholder_ids:
            return

        # Deduplicate with in-flight set
        async with _enrichment_lock:
            to_enrich = [sid for sid in missing if sid not in _enriching]
            _enriching.update(to_enrich)

        # Cap total stories per cycle to bound Chromium/resource usage
        if len(to_enrich) > ENRICHMENT_MAX_PER_CYCLE:
            logger.info(
                "Feed enrichment: %d missing IDs, capping to %d",
                len(to_enrich),
                ENRICHMENT_MAX_PER_CYCLE,
            )
            to_enrich = to_enrich[:ENRICHMENT_MAX_PER_CYCLE]
        else:
            logger.info("Feed enrichment: %d missing IDs to enrich", len(to_enrich))

        sem = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)

        async def _bounded_enrich(sid: int):
            async with sem:
                try:
                    await _enrich_one(sid)
                except Exception as exc:
                    logger.warning("Enrichment failed for %d: %s", sid, exc)
                finally:
                    async with _enrichment_lock:
                        _enriching.discard(sid)

        # Process in batches to avoid unbounded concurrency
        for i in range(0, len(to_enrich), ENRICHMENT_BATCH_SIZE):
            batch = to_enrich[i : i + ENRICHMENT_BATCH_SIZE]
            await asyncio.gather(*[_bounded_enrich(sid) for sid in batch])

        # Retry stories stuck on placeholder (re-sighted on the timeline)
        for sid in placeholder_ids:
            async with _enrichment_lock:
                if sid in _enriching:
                    continue
                _enriching.add(sid)
            try:
                await _retry_placeholder(sid)
            except Exception as exc:
                logger.warning("Placeholder retry failed for %d: %s", sid, exc)
            finally:
                async with _enrichment_lock:
                    _enriching.discard(sid)

        if to_enrich or placeholder_ids:
            logger.info(
                "Feed enrichment complete: %d enriched, %d placeholder retries",
                len(to_enrich),
                len(placeholder_ids),
            )
    finally:
        async with _enrichment_lock:
            _enrichment_running = False
            _last_enrichment_time = time.monotonic()
