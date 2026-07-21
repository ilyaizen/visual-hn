from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
import secrets
import time
from typing import Any, AsyncGenerator
from datetime import datetime
import os
import re
import logging
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request as UrlRequest, urlopen

import aiohttp

from metadata import favicon_url, source_domain, PLACEHOLDER_IMAGE
from hn_scraper import start_scraper
from database import get_stories, get_story_images, init_db
from hcker_proxy import EXTENSION_DIR, register_routes

# Max HN ids accepted per /api/story-images request (bounds query size).
MAX_IMAGE_IDS = 60

# ── Admin auth ──────────────────────────────────────────────────────────────
# Password-walled /admin route. Set VHN_ADMIN_PASSWORD env var to enable; if
# unset, /admin returns 404. Sessions use random tokens (secrets.token_hex)
# stored in-memory, so cookie theft does not reveal the password hash and
# sessions can be invalidated by restarting the process.
ADMIN_COOKIE_NAME = "vhn_admin"
ADMIN_SESSION_MAX_AGE = 60 * 60 * 8  # 8h
ADMIN_LOGIN_MAX_ATTEMPTS = 5
ADMIN_LOGIN_WINDOW_S = 300  # 5 min window for rate-limiting

NODE_HEALTH_CACHE_TTL = 15  # seconds — node health is polled at most this often

# In-memory session store: token → expiry timestamp. Cleared on restart.
_admin_sessions: dict[str, float] = {}

# Brute-force protection: (IP → list of attempt timestamps).
_admin_login_attempts: dict[str, list[float]] = {}


def _admin_password() -> str | None:
    pw = os.environ.get("VHN_ADMIN_PASSWORD", "").strip()
    return pw or None


def _is_admin_authorized(request: Request) -> bool:
    pw = _admin_password()
    if not pw:
        return False
    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if not cookie:
        return False
    expiry = _admin_sessions.get(cookie)
    if expiry is None:
        return False
    if time.time() > expiry:
        _admin_sessions.pop(cookie, None)
        return False
    return True


def _admin_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _admin_rate_limited(ip: str) -> bool:
    """Return True if IP has exceeded ADMIN_LOGIN_MAX_ATTEMPTS in the window."""
    now = time.time()
    attempts = _admin_login_attempts.get(ip, [])
    # Prune old entries.
    attempts = [t for t in attempts if now - t < ADMIN_LOGIN_WINDOW_S]
    _admin_login_attempts[ip] = attempts
    return len(attempts) >= ADMIN_LOGIN_MAX_ATTEMPTS


def _admin_record_attempt(ip: str) -> None:
    _admin_login_attempts.setdefault(ip, []).append(time.time())


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
    return templates.TemplateResponse(request, "yahnc.html", context)


# ── Admin dashboard ─────────────────────────────────────────────────────────


# In-memory cache of the node's /health response. The admin page may be polled
# by a browser; we don't want every page load to trigger a network round-trip
# to the residential node.
_node_health_cache: dict[str, Any] = {"data": None, "at": 0.0}


async def _fetch_node_health() -> dict[str, Any]:
    """Probe the residential node's /health endpoint. Cached for NODE_HEALTH_CACHE_TTL."""
    node_url = os.environ.get("VHN_RESIDENTIAL_FETCHER_URL", "").rstrip("/")
    if not node_url:
        return {
            "configured": False,
            "status": "not_configured",
            "detail": "VHN_RESIDENTIAL_FETCHER_URL not set on the VPS",
        }

    now = time.time()
    if (
        _node_health_cache["data"]
        and now - _node_health_cache["at"] < NODE_HEALTH_CACHE_TTL
    ):
        return _node_health_cache["data"]

    health_url = node_url.replace("/health", "") + "/health"
    secret = os.environ.get("VHN_RESIDENTIAL_FETCHER_SECRET", "")
    headers = {"X-Fetcher-Secret": secret} if secret else {}
    result: dict[str, Any] = {
        "configured": True,
        "url": health_url,
        "checked_at": now,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(health_url, headers=headers) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    result.update(body)
                    result["reachable"] = True
                    # Normalize: 'ok' from node means node-healthy
                    result["status"] = body.get("status", "ok")
                else:
                    result["reachable"] = False
                    result["status"] = "http_error"
                    result["detail"] = f"node returned HTTP {resp.status}"
    except asyncio.TimeoutError:
        result["reachable"] = False
        result["status"] = "timeout"
        result["detail"] = "node did not respond in 8s (laptop may be asleep)"
    except Exception as exc:
        result["reachable"] = False
        result["status"] = "unreachable"
        result["detail"] = f"{type(exc).__name__}: {exc}"

    _node_health_cache["data"] = result
    _node_health_cache["at"] = now
    return result


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _admin_password():
        return HTMLResponse("Not Found", status_code=404)
    if not _is_admin_authorized(request):
        return templates.TemplateResponse(request, "admin.html", {"request": request, "authed": False})
    node = await _fetch_node_health()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"request": request, "authed": True, "node": node},
    )


@app.get("/admin/api/node-health")
async def admin_node_health_api(request: Request):
    """JSON endpoint for the admin page to poll node health without full reload."""
    if not _is_admin_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Bypass the cache for explicit API polls — the caller controls frequency.
    _node_health_cache["at"] = 0.0
    return JSONResponse(await _fetch_node_health())


@app.post("/admin/login")
async def admin_login(request: Request):
    if not _admin_password():
        return HTMLResponse("Not Found", status_code=404)
    ip = _admin_client_ip(request)
    if _admin_rate_limited(ip):
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"request": request, "authed": False, "error": "Too many attempts. Wait a few minutes."},
            status_code=429,
        )
    form = await request.form()
    password = str(form.get("password", ""))
    expected_pw = _admin_password() or ""
    if secrets.compare_digest(password, expected_pw):
        token = secrets.token_hex(32)
        _admin_sessions[token] = time.time() + ADMIN_SESSION_MAX_AGE
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie(
            ADMIN_COOKIE_NAME,
            token,
            max_age=ADMIN_SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return resp
    _admin_record_attempt(ip)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"request": request, "authed": False, "error": "Wrong password"},
    )


@app.post("/admin/logout")
async def admin_logout(request: Request):
    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if cookie:
        _admin_sessions.pop(cookie, None)
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE_NAME)
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    logger.info(f"Starting Uvicorn server on 0.0.0.0:{port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
