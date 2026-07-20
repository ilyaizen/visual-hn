import pytest
from datetime import datetime

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import database
import main
from metadata import PLACEHOLDER_IMAGE
from models import Base, Story

# --- Pure helper tests -------------------------------------------------------


def test_parse_image_ids_ignores_non_numeric():
    assert main.parse_image_ids("44123456,abc, 44123457 ,,x9") == [44123456, 44123457]


def test_parse_image_ids_truncates_over_cap():
    raw = ",".join(str(i) for i in range(100))
    parsed = main.parse_image_ids(raw, cap=60)
    assert len(parsed) == 60
    assert parsed[0] == 0 and parsed[-1] == 59


def test_build_image_payload_absolute_and_excludes_placeholder():
    base = "https://hn.is-ai-good-yet.com/"
    stories = {
        1: Story(id=1, title="Has image", image_url="/static/images/abc.jpg"),
        2: Story(id=2, title="Placeholder", image_url=PLACEHOLDER_IMAGE),
        3: Story(id=3, title="No image", image_url=None),
    }
    payload = main.build_image_payload(stories, base)
    assert set(payload["images"].keys()) == {"1"}
    entry = payload["images"]["1"]
    assert entry["image_url"] == "https://hn.is-ai-good-yet.com/static/images/abc.jpg"
    assert entry["title"] == "Has image"
    assert entry["is_placeholder"] is False


def test_build_image_payload_includes_favicon_domain_and_description():
    base = "https://hn.is-ai-good-yet.com"
    stories = {
        1: Story(
            id=1,
            title="Story",
            url="https://www.example.com/article",
            description="A short OG description.",
            image_url="/static/images/abc.jpg",
        ),
    }
    entry = main.build_image_payload(stories, base)["images"]["1"]
    assert entry["domain"] == "example.com"
    assert (
        entry["favicon"]
        == "https://www.google.com/s2/favicons?domain=example.com&sz=64"
    )
    assert entry["description"] == "A short OG description."


def test_build_image_payload_tolerates_missing_url_and_description():
    base = "https://hn.is-ai-good-yet.com"
    stories = {1: Story(id=1, title="No url", image_url="/static/images/abc.jpg")}
    entry = main.build_image_payload(stories, base)["images"]["1"]
    assert entry["domain"] == ""
    assert entry["favicon"] == ""
    assert entry["description"] == ""


def test_build_image_payload_prefers_remote_og_image():
    """A remote og:image is served verbatim (loaded client-side, not from our box)."""
    base = "https://hn.is-ai-good-yet.com"
    stories = {
        1: Story(
            id=1,
            title="Remote",
            url="https://example.com/a",
            og_image_url="https://cdn.example.com/og.png",
            image_url="/static/images/local.jpg",  # local fallback ignored when og exists
        ),
    }
    entry = main.build_image_payload(stories, base)["images"]["1"]
    assert entry["image_url"] == "https://cdn.example.com/og.png"
    assert entry["is_remote"] is True


def test_build_image_payload_uses_local_screenshot_when_no_og():
    """With no og:image, the locally stored screenshot is served absolute from our box."""
    base = "https://hn.is-ai-good-yet.com"
    stories = {
        1: Story(
            id=1, title="Shot", og_image_url=None, image_url="/static/images/shot.jpg"
        ),
        2: Story(id=2, title="None", og_image_url=None, image_url=PLACEHOLDER_IMAGE),
    }
    payload = main.build_image_payload(stories, base)
    assert set(payload["images"].keys()) == {"1"}
    entry = payload["images"]["1"]
    assert entry["image_url"] == "https://hn.is-ai-good-yet.com/static/images/shot.jpg"
    assert entry["is_remote"] is False


# --- DB helper tests ---------------------------------------------------------


@pytest.fixture
async def patched_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Point database.async_session at the in-memory test DB.
    monkeypatch.setattr(database, "async_session", session_factory)

    async with session_factory() as seed:
        seed.add_all(
            [
                Story(
                    id=10,
                    title="Live",
                    image_url="/static/images/live.jpg",
                    current_position=1,
                    time_posted=datetime.now(),
                ),
                Story(
                    id=11,
                    title="Fallen off",
                    image_url="/static/images/fallen.jpg",
                    current_position=None,
                    time_posted=datetime.now(),
                ),
            ]
        )
        await seed.commit()

    yield session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_get_story_images_returns_only_existing(patched_session):
    result = await database.get_story_images([10, 11, 99999])
    assert set(result.keys()) == {10, 11}


async def test_get_story_images_includes_fallen_off(patched_session):
    """Fallen-off stories (current_position IS NULL) are still served."""
    result = await database.get_story_images([11])
    assert 11 in result
    assert result[11].current_position is None


async def test_get_story_images_empty():
    assert await database.get_story_images([]) == {}


# --- Route tests -------------------------------------------------------------


def _fake_request(
    query: str = "", headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "server": ("hn.is-ai-good-yet.com", 443),
        "path": "/api/story-images",
        "headers": headers or [],
        "query_string": query.encode(),
    }
    return Request(scope)


async def test_story_images_route_happy_path(monkeypatch):
    async def fake_get(ids):
        return {
            1: Story(id=1, title="A", image_url="/static/images/a.jpg"),
            2: Story(id=2, title="P", image_url=PLACEHOLDER_IMAGE),
        }

    monkeypatch.setattr(main, "get_story_images", fake_get)
    response = await main.story_images(_fake_request(), ids="1,2")

    import json

    body = json.loads(response.body)
    assert list(body["images"].keys()) == ["1"]
    assert body["images"]["1"]["image_url"] == (
        "https://hn.is-ai-good-yet.com/static/images/a.jpg"
    )
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "public, max-age=300"


async def test_story_images_route_no_valid_ids(monkeypatch):
    async def fail(ids):  # should never be called when no valid ids
        raise AssertionError("DB hit with no valid ids")

    monkeypatch.setattr(main, "get_story_images", fail)
    response = await main.story_images(_fake_request(), ids="abc,xyz")
    import json

    assert json.loads(response.body) == {"images": {}}


def test_public_base_url_uses_forwarded_https_headers():
    request = _fake_request(
        headers=[
            (b"host", b"<tailscale-ip>:8091"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"hn.is-ai-good-yet.com"),
        ]
    )

    assert main.public_base_url(request) == "https://hn.is-ai-good-yet.com"


def test_public_base_url_env_override(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/")

    assert main.public_base_url(_fake_request()) == "https://example.com"
