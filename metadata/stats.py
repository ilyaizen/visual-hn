"""Fallback-layer statistics for the admin dashboard.

Counters are incremented by orchestrator.py and fetcher.py at each decision
point in the metadata fallback chain. The scraper resets them at the start
of each 15-min cycle via ``reset_fallback_stats()`` so the dashboard shows
per-cycle resolution distribution.
"""

from __future__ import annotations

from typing import Any

# Per-cycle counters. Keys correspond to the layer that *ultimately resolved*
# the image (or failed to). ``residential_*`` track the residential fetcher
# invocation regardless of whether its HTML ultimately yielded an og:image.
fallback_stats: dict[str, Any] = {
    "og_image": 0,  # og:image found in curl_cffi or residential HTML
    "wayback_og": 0,  # og:image found in Wayback Machine snapshot
    "screenshot": 0,  # local Playwright screenshot
    "favicon": 0,  # favicon composite card
    "placeholder": 0,  # all layers failed
    "pdf": 0,  # PDF first-page render
    "cached": 0,  # served from in-memory cache (no fetch)
    "skipped": 0,  # non-public URL or deadline-exhausted, no image
    "residential_calls": 0,  # residential fetcher invoked
    "residential_ok": 0,  # residential fetcher returned usable HTML
    "residential_fail": 0,  # residential fetcher failed/timeout/unreachable
    "cycle_started_at": None,  # monotonic timestamp of cycle start
}


def reset_fallback_stats() -> None:
    """Zero all counters for a new scrape cycle."""
    for key in fallback_stats:
        if key == "cycle_started_at":
            continue
        fallback_stats[key] = 0
