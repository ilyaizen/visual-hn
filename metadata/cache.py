"""In-memory metadata cache with bounded LRU eviction.

No dependencies on other metadata sub-modules.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any, Dict

logger = logging.getLogger(__name__)

metadata_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()

PLACEHOLDER_IMAGE = "/static/images/placeholder.jpg"
METADATA_CACHE_MAX_ITEMS = int(os.environ.get("VHN_METADATA_CACHE_MAX_ITEMS", "300"))
METADATA_MAX_RETRIES = int(os.environ.get("VHN_METADATA_MAX_RETRIES", "3"))


def cache_metadata(url: str, metadata: Dict[str, Any]) -> None:
    """Store metadata in a bounded LRU cache so long-running servers cannot grow forever."""
    if METADATA_CACHE_MAX_ITEMS <= 0:
        return
    metadata_cache[url] = metadata
    metadata_cache.move_to_end(url)
    while len(metadata_cache) > METADATA_CACHE_MAX_ITEMS:
        evicted_url, _ = metadata_cache.popitem(last=False)
        logger.debug("Evicted metadata cache entry for %s", evicted_url)


def get_cached_metadata(url: str) -> Dict[str, Any] | None:
    cached = metadata_cache.get(url)
    if cached is not None:
        metadata_cache.move_to_end(url)
    return cached


def should_use_cached_metadata(cached: Dict[str, Any] | None) -> bool:
    """Return True when cached metadata is good enough to skip refetching."""
    if not cached:
        return False
    # A remote og:image counts as a good result even though nothing is stored
    # locally; otherwise only a real local fallback (non-placeholder) qualifies.
    if cached.get("og_image_url"):
        return True
    if cached.get("image_url") != PLACEHOLDER_IMAGE:
        return True
    # Placeholder stuck after N attempts — stop retrying to avoid endless re-fetch.
    if cached.get("retries", 0) >= METADATA_MAX_RETRIES:
        return True
    return False
