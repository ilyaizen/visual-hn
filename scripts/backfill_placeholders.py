"""One-time backfill: re-run metadata for recent stories stuck on placeholder.

Targets stories no older than BACKFILL_MAX_AGE_DAYS. Older ones are mostly
dead links — not worth the fetch budget.

Throttling:
- Concurrency 2 (same as feed enrichment).
- Sleep between batches; each story runs the full fallback chain, which with
  the new 45s residential cap + favicon exemption is bounded per story.
- Safe to re-run: stories that already got a real image are skipped.
- Safe to run while the service is up: single-writer SQLite tolerates short
  write transactions; this script writes one row at a time.

Usage [VPS]:
    cd /srv/apps/visual-hn
    .venv/bin/python scripts/backfill_placeholders.py [--dry-run] [--limit N]

Progress is logged to stdout; interrupting (Ctrl-C) is safe and resumes where
it left off (skips are recomputed from the DB each run).
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


async def get_placeholder_ids(max_age_days: int, limit: int | None) -> list[int]:
    """IDs of stories on placeholder, newest first, within the age window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    async with async_session() as session:
        rows = await session.execute(
            select(Story.id, Story.time_posted)
            .where(Story.image_url.like("%placeholder%"))
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
    """Re-run metadata for one placeholder story. Returns True on upgrade."""
    async with async_session() as session:
        story = await session.get(Story, story_id)
        if story is None or "placeholder" not in (story.image_url or ""):
            return False  # already healed or gone
        url = story.url

    metadata = await fetch_metadata(url, enable_screenshot=True)
    new_image = metadata.get("image_url") or ""
    if "placeholder" in new_image:
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
    logger.info("Upgraded %d -> %s", story_id, new_image)
    return True


async def main(dry_run: bool, limit: int | None) -> None:
    ids = await get_placeholder_ids(BACKFILL_MAX_AGE_DAYS, limit)
    logger.info(
        "%d placeholder stories within %d days%s",
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
