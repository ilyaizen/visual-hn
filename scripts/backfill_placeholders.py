"""One-time backfill: re-run metadata for recent stories stuck on placeholder.

Targets stories no older than BACKFILL_MAX_AGE_DAYS that are TRULY broken:
image_url is a placeholder AND og_image_url is NULL. Stories with a remote
og:image keep placeholder in image_url by design (clients load the remote URL
client-side) — they are healthy and skipped.

Throttling:
- Concurrency 2 (same as feed enrichment).
- Sleep between batches; each story runs the full fallback chain, which with
  the 45s residential cap + favicon exemption is bounded per story.
- Safe to re-run: skips are recomputed from the DB each run.
- Safe to run while the service is up: single-writer SQLite tolerates short
  write transactions; this script writes one row at a time.

Usage [VPS]:
    cd /srv/apps/visual-hn
    .venv/bin/python scripts/backfill_placeholders.py [--dry-run] [--limit N]

Progress is logged to stdout; interrupting (Ctrl-C) is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from sqlalchemy import select

from database import async_session
from metadata import fetch_metadata
from models import Story

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

BACKFILL_MAX_AGE_DAYS = 14
BATCH_SIZE = 5
CONCURRENCY = 2
SLEEP_BETWEEN_BATCHES_SECONDS = 10


async def get_broken_ids(max_age_days: int, limit: int | None) -> list[int]:
    """IDs of truly-broken stories, newest first, within the age window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    async with async_session() as session:
        rows = await session.execute(
            select(Story.id, Story.time_posted)
            .where(
                Story.image_url.like("%placeholder%"),
                Story.og_image_url.is_(None),
            )
            .order_by(Story.time_posted.desc())
        )
        ids = [
            sid
            for sid, posted in rows.all()
            if posted is not None and posted.replace(tzinfo=timezone.utc) >= cutoff
        ]
    if limit:
        ids = ids[:limit]
    return ids


async def backfill_one(story_id: int) -> bool:
    """Re-run metadata for one broken story. Returns True on upgrade.

    An upgrade is either a local image (screenshot/favicon card) or a newly
    discovered remote og:image — both make the story render a real preview.
    """
    async with async_session() as session:
        story = await session.get(Story, story_id)
        if story is None or "placeholder" not in (story.image_url or ""):
            return False  # already healed or gone
        if story.og_image_url:
            return False  # healthy via remote og:image, skip
        url = story.url

    metadata = await fetch_metadata(url, enable_screenshot=True)
    new_image = metadata.get("image_url") or ""
    new_og = metadata.get("og_image_url")
    if "placeholder" in new_image and not new_og:
        return False

    metadata.pop("retries", None)  # bookkeeping field, not a Story column
    async with async_session() as session:
        story = await session.get(Story, story_id)
        if story is None:
            return False
        for key, value in metadata.items():
            if hasattr(story, key):
                setattr(story, key, value)
        await session.commit()
    logger.info("Upgraded %d -> image=%s og=%s", story_id, new_image, bool(new_og))
    return True


async def main(dry_run: bool, limit: int | None) -> None:
    ids = await get_broken_ids(BACKFILL_MAX_AGE_DAYS, limit)
    logger.info(
        "%d broken stories within %d days%s",
        len(ids),
        BACKFILL_MAX_AGE_DAYS,
        " (dry run)" if dry_run else "",
    )
    if dry_run or not ids:
        return

    upgraded = 0
    sem = asyncio.Semaphore(CONCURRENCY)

    async def bounded(sid: int) -> bool:
        async with sem:
            try:
                return await backfill_one(sid)
            except Exception as exc:
                logger.warning("Failed %d: %s", sid, exc)
                return False

    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i : i + BATCH_SIZE]
        results = await asyncio.gather(*[bounded(sid) for sid in batch])
        upgraded += sum(results)
        done = min(i + BATCH_SIZE, len(ids))
        logger.info("Progress: %d/%d processed, %d upgraded", done, len(ids), upgraded)
        if i + BATCH_SIZE < len(ids):
            await asyncio.sleep(SLEEP_BETWEEN_BATCHES_SECONDS)

    logger.info("Backfill complete: %d/%d upgraded", upgraded, len(ids))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.limit))
