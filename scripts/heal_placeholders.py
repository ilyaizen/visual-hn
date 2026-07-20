#!/usr/bin/env python3
"""Batch-heal recent placeholder stories by re-running fetch_metadata.

Re-processes stories still stuck on placeholder.jpg through the full
fetch_metadata pipeline (curl_cffi → screenshot → favicon composite).
With the DuckDuckGo favicon fallback and screenshot enabled, most should
get a real preview now.

Usage:
    .venv/bin/python scripts/heal_placeholders.py [--min-id 48900000] [--limit 500]
"""

import argparse
import asyncio
import sqlite3
import sys

sys.path.insert(0, "/srv/apps/visual-hn")
from metadata import fetch_metadata

DB = "/srv/apps/visual-hn/visual_hn.db"
BATCH_CONCURRENCY = 3


async def heal_one(story_id: str, url: str, title: str, sem: asyncio.Semaphore):
    async with sem:
        try:
            metadata = await fetch_metadata(url, title, enable_screenshot=True)
            metadata.pop("retries", None)
            new_img = metadata.get("image_url", "")
            new_og = metadata.get("og_image_url")
            new_desc = metadata.get("description", "")

            if "placeholder" not in new_img or new_og:
                conn = sqlite3.connect(DB)
                conn.execute(
                    "UPDATE stories SET image_url=?, og_image_url=?, description=? WHERE id=?",
                    (new_img, new_og, new_desc, story_id),
                )
                conn.commit()
                conn.close()
                tag = f"og:image" if new_og else new_img.split("/")[-1][:30]
                return story_id, True, tag
            else:
                return story_id, False, "still placeholder"
        except Exception as e:
            return story_id, False, f"{type(e).__name__}: {e}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-id", type=int, default=48900000)
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """SELECT id, url, title FROM stories
           WHERE image_url LIKE '%placeholder%' AND id > ?
           ORDER BY id DESC LIMIT ?""",
        (args.min_id, args.limit),
    ).fetchall()
    conn.close()

    print(f"Healing {len(rows)} placeholder stories...\n", flush=True)
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)
    healed = 0
    failed = 0

    # Process in batches for progress visibility
    batch_size = 30
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        tasks = [heal_one(sid, url, title, sem) for sid, url, title in batch]
        results = await asyncio.gather(*tasks)
        for sid, ok, tag in results:
            status = "✓" if ok else "✗"
            if ok:
                healed += 1
            else:
                failed += 1
            print(f"  [{sid}] {status} {tag}", flush=True)
        print(
            f"  -- batch {i//batch_size+1}/{(len(rows)-1)//batch_size+1} done ({healed} healed)\n",
            flush=True,
        )

    print(f"\nDone: {healed} healed, {failed} still placeholder", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
