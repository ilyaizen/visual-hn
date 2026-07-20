from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
from typing import AsyncGenerator
from datetime import datetime
import os
import re
import logging
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request as UrlRequest, urlopen

from metadata import favicon_url, source_domain, PLACEHOLDER_IMAGE
from hn_scraper import start_scraper
from database import get_stories, get_story_images, init_db
from hcker_proxy import EXTENSION_DIR, register_routes

# Max HN ids accepted per /api/story-images request (bounds query size).
MAX_IMAGE_IDS = 60


# --- Logging Configuration ---
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)

if not root_logger.handlers:
    root_logger.addHandler(handler)

uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def lifespan(app: FastAPI) -> AsyncGenerator:
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database initialized.")

    logger.info("Starting scraper task...")
    app.state.scraper_task = asyncio.create_task(start_scraper())
    logger.info("Scraper task started.")

    yield

    logger.info("Shutting down. Cancelling scraper task...")
    app.state.scraper_task.cancel()
    try:
        await app.state.scraper_task
    except asyncio.CancelledError:
        logger.info("Scraper task cancelled successfully.")
    except Exception as e:
        logger.error(f"Error during scraper task cancellation: {e}", exc_info=True)

    logger.info("Application shutdown complete.")


app = FastAPI(title="Visual-HN", lifespan=lifespan)

# ── Rate limiting ──────────────────────────────────────────────────────────
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from fastapi.responses import PlainTextResponse
from hcker_proxy import limiter

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return PlainTextResponse(f"Rate limit exceeded: {exc.detail}", status_code=429)


# Mount the static directory
app.mount("/static", StaticFiles(directory="static"), name="static")
if EXTENSION_DIR.exists():
    app.mount(
        "/visual-hn-previews",
        StaticFiles(directory=str(EXTENSION_DIR)),
        name="visual_hn_previews",
    )

# Register hcker.news proxy routes
register_routes(app)

# Set up Jinja2 templates
templates = Jinja2Templates(directory="templates")


def time_ago(dt: datetime) -> str:
    now = datetime.now()
    diff = now - dt

    if diff.total_seconds() < 0:
        return "in the future"

    if diff.days > 365:
        years = diff.days // 365
        return f"{years} year{'s' if years > 1 else ''} ago"
    if diff.days > 30:
        months = diff.days // 30
        return f"{months} month{'s' if months > 1 else ''} ago"
    if diff.days > 0:
        return f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
    if diff.seconds > 3600:
        hours = diff.seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    if diff.seconds > 60:
        minutes = diff.seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    if diff.seconds >= 5:
        return f"{diff.seconds} second{'s' if diff.seconds > 1 else ''} ago"
    return "just now"


def get_trend_symbol(trend: str) -> str:
    return {"up": "▲", "down": "▼"}.get(trend, "")


def parse_image_ids(raw: str, cap: int = MAX_IMAGE_IDS) -> list[int]:
    """Parse a CSV of HN ids into a capped list of ints. Non-numeric tokens are ignored."""
    ids: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            ids.append(int(token))
    if len(ids) > cap:
        logger.info("story-images: truncating %d ids to cap %d", len(ids), cap)
        ids = ids[:cap]
    return ids


def public_base_url(request: Request) -> str:
    """Return the public base URL for absolute image URLs behind proxies."""
    configured = os.environ.get("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")

    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{scheme}://{host}".rstrip("/")


def build_image_payload(stories: dict[int, "object"], base_url: str) -> dict:
    """Build the {images: {...}} payload, excluding missing and placeholder images."""
    base = base_url.rstrip("/")
    images: dict[str, dict] = {}
    for story_id, story in stories.items():
        og_image_url = getattr(story, "og_image_url", None)
        local_image_url = getattr(story, "image_url", None)
        if og_image_url:
            src = og_image_url
            is_remote = True
        elif local_image_url and local_image_url != PLACEHOLDER_IMAGE:
            src = (
                base + local_image_url
                if local_image_url.startswith("/")
                else local_image_url
            )
            is_remote = False
        else:
            continue
        url = getattr(story, "url", None)
        images[str(story_id)] = {
            "image_url": src,
            "is_remote": is_remote,
            "title": getattr(story, "title", None),
            "url": url,
            "description": getattr(story, "description", None) or "",
            "domain": source_domain(url),
            "favicon": favicon_url(url),
            "is_placeholder": False,
            # HN front-page rank (1-30) + movement since last scrape, so the
            # extension can render rank numbers and trend arrows.
            "position": getattr(story, "current_position", None),
            "trend": getattr(story, "trend", None) or "same",
        }
    return {"images": images}


@app.get("/api/story-images")
@limiter.limit("30/minute")
async def story_images(request: Request, ids: str = Query(...)):
    parsed_ids = parse_image_ids(ids)
    stories = await get_story_images(parsed_ids) if parsed_ids else {}
    payload = build_image_payload(stories, public_base_url(request))
    return JSONResponse(
        payload,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Cache-Control": "public, max-age=300",
        },
    )


def normalize_hn_comments_url(url: str) -> str | None:
    parsed = urlsplit(url)
    host = parsed.netloc.lower()

    if parsed.scheme not in {"http", "https"}:
        return None
    if host not in {"news.ycombinator.com", "www.news.ycombinator.com"}:
        return None
    if parsed.path != "/item":
        return None

    story_id = parse_qs(parsed.query).get("id", [""])[0]
    if not story_id.isdigit():
        return None

    return f"https://news.ycombinator.com/item?id={story_id}"


def fetch_hn_comments_html(url: str) -> str:
    request = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="replace")

    html = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", html)
    if "<head>" in html:
        html = html.replace(
            "<head>",
            '<head><base href="https://news.ycombinator.com/" />',
            1,
        )

    return html


@app.get("/hn-comments", response_class=HTMLResponse)
async def hn_comments_proxy(url: str = Query(...)):
    hn_url = normalize_hn_comments_url(url)
    if not hn_url:
        return HTMLResponse(
            "<!doctype html><html><body>Invalid HN comments URL.</body></html>",
            status_code=400,
        )

    try:
        html = await asyncio.to_thread(fetch_hn_comments_html, hn_url)
    except Exception as exc:
        logger.warning("Failed to load HN comments page %s: %s", hn_url, exc)
        return HTMLResponse(
            "<!doctype html><html><body>Unable to load HN comments right now.</body></html>",
            status_code=502,
        )

    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/mossy-velvet", response_class=HTMLResponse)
async def read_legacy_frontend(request: Request):
    stories = await get_stories()
    context = {
        "request": request,
        "stories": stories,
        "time_ago": time_ago,
        "get_trend_symbol": get_trend_symbol,
        "source_domain": source_domain,
        "favicon_url": favicon_url,
    }
    return templates.TemplateResponse(request, "index.html", context)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    logger.info(f"Starting Uvicorn server on 0.0.0.0:{port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
