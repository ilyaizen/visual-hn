# --- START OF FILE hn_scraper.py ---
import asyncio
import gc
import aiohttp
from typing import List, Dict, Any
import logging  # Import logging
import os
import sys
import tracemalloc

from metadata import fetch_metadata
from database import update_stories

# Configure logging for this module
logger = logging.getLogger(__name__)
METADATA_CONCURRENCY = int(os.environ.get("VHN_METADATA_CONCURRENCY", "4"))
ENABLE_MEMORY_LOGS = os.environ.get("VHN_MEMORY_LOGS", "1").lower() not in {
    "0",
    "false",
    "no",
}

HACKER_NEWS_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HACKER_NEWS_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"


def _rss_mb() -> float | None:
    """Return current process RSS/private working set in MiB when available."""
    if sys.platform != "win32":
        try:
            import resource

            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return rss_kb / 1024
        except Exception:
            return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return counters.PrivateUsage / (1024 * 1024)
    except Exception:
        return None
    return None


def log_memory(label: str) -> None:
    if not ENABLE_MEMORY_LOGS:
        return
    if not tracemalloc.is_tracing():
        tracemalloc.start(10)
    current, peak = tracemalloc.get_traced_memory()
    rss = _rss_mb()
    rss_part = f", rss={rss:.1f}MiB" if rss is not None else ""
    logger.info(
        "memory[%s]: py_current=%.1fMiB, py_peak=%.1fMiB%s, gc=%s",
        label,
        current / (1024 * 1024),
        peak / (1024 * 1024),
        rss_part,
        gc.get_count(),
    )


async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    logger.debug(f"Fetching JSON from {url}")
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
        response.raise_for_status()  # Raise HTTPError for bad responses
        return await response.json()


async def fetch_top_stories() -> List[Dict[str, Any]]:
    """Fetches the top N stories from Hacker News API."""
    try:
        async with aiohttp.ClientSession() as session:
            logger.info(f"Fetching top story IDs from {HACKER_NEWS_TOP_STORIES_URL}")
            top_story_ids = await fetch_json(session, HACKER_NEWS_TOP_STORIES_URL)
            # Limiting to 30 as before, handle if less are returned
            story_ids_to_fetch = top_story_ids[: min(len(top_story_ids), 30)]
            logger.info(
                f"Fetched {len(top_story_ids)} IDs, fetching details for {len(story_ids_to_fetch)}."
            )

            tasks = [
                fetch_json(session, HACKER_NEWS_ITEM_URL.format(story_id))
                for story_id in story_ids_to_fetch
            ]
            stories = await asyncio.gather(*tasks)
            # Filter out any None results if fetching failed for an individual item
            stories = [story for story in stories if story]
            logger.info(f"Successfully fetched details for {len(stories)} stories.")
            return stories
    except Exception as e:
        logger.error(f"Error fetching top stories from HN API: {e}", exc_info=True)
        return []  # Return empty list on failure


async def fetch_metadata_limited(
    semaphore: asyncio.Semaphore, url: str, fallback_text: str
) -> Dict[str, Any]:
    async with semaphore:
        return await fetch_metadata(url, fallback_text)


async def start_scraper() -> None:
    """Main scraper loop."""
    logger.info("Scraper started.")
    while True:
        try:
            logger.info("Starting a new scraping cycle...")
            log_memory("cycle-start")
            stories = await fetch_top_stories()
            log_memory("after-hn-api")

            if not stories:
                logger.warning(
                    "No stories fetched from HN API. Skipping metadata and database update."
                )
                await asyncio.sleep(60)  # Sleep for a shorter time if HN API failed
                continue  # Skip the rest of the loop and try again sooner

            # Fetch metadata concurrently, but cap concurrency so slow screenshots or
            # large pages cannot create a large pile-up of browser/network tasks.
            # Each fetch_metadata call uses curl_cffi internally (with Chrome TLS
            # fingerprint) — no shared aiohttp session needed for external URLs.
            metadata_tasks = []
            metadata_semaphore = asyncio.Semaphore(METADATA_CONCURRENCY)
            for index, story in enumerate(stories, start=1):
                # Pre-process story data before fetching metadata
                story["hn_url"] = f"https://news.ycombinator.com/item?id={story['id']}"

                # Handle Ask HN, Show HN, etc. by using hn_url as primary url if link is missing
                if "url" not in story or not story["url"]:
                    story["url"] = story["hn_url"]  # Use HN URL if no external link

                story["current_position"] = index

                # Add metadata task. Use the HN text/title as a friendly description
                # fallback so the UI never shows scraper error copy when page metadata
                # is blocked, missing, or non-HTML.
                fallback_text = story.get("text") or story.get("title") or ""
                metadata_tasks.append(
                    fetch_metadata_limited(
                        metadata_semaphore, story["url"], fallback_text
                    )
                )

            logger.info(
                "Fetching metadata for %d stories with concurrency limit %d...",
                len(metadata_tasks),
                METADATA_CONCURRENCY,
            )
            metadata_results = await asyncio.gather(*metadata_tasks)
            logger.info("Finished fetching metadata.")
            log_memory("after-metadata")

            # Combine stories with their metadata results
            processed_stories = []
            for story, metadata in zip(stories, metadata_results):
                # Apply metadata to the original story dictionary
                # fetch_metadata already returns placeholder/error if it failed
                story.update(metadata)
                # Add any other necessary fields here
                # Ensure 'text' field is present, defaulting to None if not from HN API
                story["text"] = story.get("text")

                processed_stories.append(story)

            logger.info(f"Updating database with {len(processed_stories)} stories...")
            await update_stories(processed_stories)
            logger.info(f"Updated {len(processed_stories)} stories successfully.")
            gc.collect()
            log_memory("cycle-end-after-gc")

        except asyncio.CancelledError:
            logger.info("Scraper task cancelled.")
            break  # Exit loop on cancellation
        except Exception as e:
            logger.error(
                f"An unexpected error occurred in the scraper loop: {e}", exc_info=True
            )
        logger.info("Scraping cycle finished. Sleeping for 15 minutes.")
        await asyncio.sleep(900)  # Sleep for 15 minutes


# --- END OF FILE hn_scraper.py ---
