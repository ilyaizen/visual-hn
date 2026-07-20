#!/usr/bin/env python3
"""Batch-screenshot HN-internal stories that have branded cards instead of screenshots.

Runs capture_screenshot() directly against the story URLs. HN pages are
simple server-rendered HTML — headless Chromium handles them fine.

Usage:
    .venv/bin/python scripts/heal_hn_screenshots.py [--limit 315]
"""

import argparse
import asyncio
import sqlite3
import sys

sys.path.insert(0, "/srv/apps/visual-hn")
from screenshot import capture_screenshot

DB = "/srv/apps/visual-hn/visual_hn.db"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=315)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """SELECT id, url, title FROM stories
           WHERE url LIKE '%news.ycombinator.com%' AND image_url LIKE '%hn-%'
           ORDER BY id DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()

    print(f"Screenshotting {len(rows)} HN stories...\n", flush=True)
    done = 0
    failed = 0

    for story_id, url, title in rows:
        print(f"[{story_id}] {title[:50]}", end="", flush=True)
        try:
            filename = await capture_screenshot(url)
            if filename:
                conn.execute(
                    "UPDATE stories SET image_url=? WHERE id=?",
                    (f"/static/images/{filename}", story_id),
                )
                conn.commit()
                print(f" → {filename}", flush=True)
                done += 1
            else:
                print(" → SKIP (None)", flush=True)
                failed += 1
        except Exception as e:
            print(f" → FAIL: {e}", flush=True)
            failed += 1

    conn.close()
    print(f"\nDone: {done} screenshotted, {failed} failed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
