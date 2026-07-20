#!/usr/bin/env python3
"""Heal stories with favicon-composite previews by re-fetching via residential node.

Targets recent Bloomberg/NYT stories that have favicon composites but no
og:image. Uses the residential fetcher to get real og:image URLs.

Usage:
    .venv/bin/python scripts/heal_degraded_previews.py [--limit 30] [--domains bloomberg.com,nytimes.com]

Runs one-at-a-time (fetcher semaphore=1). ~24s per story.
"""

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.request

DB = "/srv/apps/visual-hn/visual_hn.db"
FETCHER = os.environ.get("VHN_RESIDENTIAL_FETCHER_URL", "http://<tailscale-ip>:8765")
SECRET = "hello"
TIMEOUT = 120


def extract_og_image(html):
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def extract_description(html, fallback=""):
    for prop in ["og:description", "twitter:description"]:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)[:200]
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    return m.group(1)[:200] if m else fallback


def fetch_via_residential(url):
    req = urllib.request.Request(
        f"{FETCHER}/fetch",
        data=json.dumps({"url": url}).encode(),
        headers={"Content-Type": "application/json", "X-Fetcher-Secret": SECRET},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--domains", default="bloomberg.com,nytimes.com")
    args = ap.parse_args()

    domain_clauses = " OR ".join(
        f"url LIKE '%{d.strip()}%'" for d in args.domains.split(",")
    )

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        f"""SELECT id, url, title FROM stories
            WHERE image_url LIKE '%fav-%' AND og_image_url IS NULL
              AND ({domain_clauses})
            ORDER BY id DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()

    print(f"Healing {len(rows)} stories (limit={args.limit})...\n", flush=True)
    healed = 0
    failed = 0

    for story_id, url, title in rows:
        print(f"[{story_id}] {title[:50]}", flush=True)
        try:
            data = fetch_via_residential(url)
            html = data.get("html", "")
            if not html:
                print("  SKIP: no HTML", flush=True)
                failed += 1
                continue

            og = extract_og_image(html)
            if og:
                desc = extract_description(html, title)
                conn.execute(
                    "UPDATE stories SET og_image_url=?, image_url=NULL, description=? WHERE id=?",
                    (og, desc, story_id),
                )
                conn.commit()
                print(f"  HEALED: {og[:70]}", flush=True)
                healed += 1
            else:
                print(f"  SKIP: no og:image in {len(html)} bytes", flush=True)
                failed += 1
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}", flush=True)
            failed += 1
        time.sleep(2)

    conn.close()
    print(f"\nDone: {healed} healed, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
