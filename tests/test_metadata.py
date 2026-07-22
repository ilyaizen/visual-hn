import asyncio
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

import metadata


def test_extract_description_uses_standard_meta_description_when_og_missing():
    html = """
    <html><head>
      <meta name="description" content="A useful plain meta description." />
    </head><body></body></html>
    """

    assert (
        metadata.extract_description_from_html(html, "")
        == "A useful plain meta description."
    )


def test_extract_description_falls_back_to_substantial_paragraph():
    html = """
    <html><body>
      <p>Too short.</p>
      <p>This paragraph is long enough to work as a readable fallback summary for a link card.</p>
    </body></html>
    """

    assert metadata.extract_description_from_html(html, "").startswith(
        "This paragraph is long enough"
    )


def test_build_fallback_description_uses_hn_text_without_html_or_error_copy():
    fallback = metadata.build_fallback_description(
        "https://example.com/story", "<p>Hello <b>HN</b> readers &amp; builders.</p>"
    )

    assert fallback == "Hello HN readers & builders."
    assert "Could not fetch description" not in fallback


def test_build_fallback_description_uses_domain_copy_instead_of_error_copy():
    fallback = metadata.build_fallback_description("https://example.com/story", "")

    assert fallback == "Read the full story on example.com."
    assert "Could not fetch description" not in fallback


def test_source_domain_strips_common_web_prefix_and_keeps_context():
    assert metadata.source_domain("https://www.example.com/story?id=1") == "example.com"
    assert (
        metadata.source_domain("https://blog.example.co.uk/post")
        == "blog.example.co.uk"
    )
    assert (
        metadata.source_domain("https://news.ycombinator.com/item?id=1")
        == "news.ycombinator.com"
    )


def test_favicon_url_uses_source_domain_and_gracefully_skips_invalid_urls():
    assert (
        metadata.favicon_url("https://www.example.com/story")
        == "https://www.google.com/s2/favicons?domain=example.com&sz=64"
    )
    assert metadata.favicon_url("not a url") == ""


def test_placeholder_metadata_cache_is_not_reused_even_when_screenshot_fallback_is_disabled():
    cached = {
        "image_url": metadata.PLACEHOLDER_IMAGE,
        "description": "Cached during a temporary image-download failure.",
    }

    assert metadata.should_use_cached_metadata(cached) is False


def test_placeholder_metadata_cache_is_not_reused_when_screenshot_fallback_is_enabled(
    monkeypatch,
):
    cached = {
        "image_url": metadata.PLACEHOLDER_IMAGE,
        "description": "Cached during a temporary screenshot failure.",
    }
    monkeypatch.setattr(metadata, "ENABLE_SCREENSHOT_FALLBACK", True)

    assert metadata.should_use_cached_metadata(cached) is False


def test_screenshot_fallback_is_enabled_by_default_for_story_images():
    assert metadata.ENABLE_SCREENSHOT_FALLBACK is True


def test_metadata_cache_is_bounded_lru(monkeypatch):
    import metadata.cache as cache_mod

    monkeypatch.setattr(cache_mod, "METADATA_CACHE_MAX_ITEMS", 2)
    metadata.metadata_cache.clear()

    metadata.cache_metadata("https://example.com/1", {"description": "one"})
    metadata.cache_metadata("https://example.com/2", {"description": "two"})
    assert metadata.get_cached_metadata("https://example.com/1") == {
        "description": "one"
    }
    metadata.cache_metadata("https://example.com/3", {"description": "three"})

    assert list(metadata.metadata_cache) == [
        "https://example.com/1",
        "https://example.com/3",
    ]
    assert metadata.get_cached_metadata("https://example.com/2") is None
    metadata.metadata_cache.clear()


def test_non_placeholder_metadata_cache_is_reused():
    cached = {
        "image_url": "/static/images/story-card.jpg",
        "description": "Good metadata.",
    }

    assert metadata.should_use_cached_metadata(cached) is True


def test_resolve_metadata_url_handles_root_relative_images_against_page_url():
    assert (
        metadata.resolve_metadata_url(
            "/social/card.png", "https://example.com/articles/post"
        )
        == "https://example.com/social/card.png"
    )


def test_aiohttp_request_url_preserves_signed_image_query_encoding():
    signed_url = "https://img.example/card.jpg?overlay-align=bottom%2Cleft&s=abc123"

    request_url = metadata.aiohttp_request_url(signed_url)

    assert request_url.raw_query_string == "overlay-align=bottom%2Cleft&s=abc123"
    assert str(request_url).endswith("overlay-align=bottom%2Cleft&s=abc123")


async def test_fetch_metadata_uses_browser_headers_with_shared_session(monkeypatch):
    class Content:
        def __init__(self):
            self.sent = False

        async def read(self, n=-1):
            if self.sent:
                return b""
            self.sent = True
            return b"<html><head><title>Story</title></head><body></body></html>"

    class FakeResponse:
        url = "https://example.com/story"
        headers = {"Content-Type": "text/html"}
        charset = "utf-8"
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            pass

    class FakeSession:
        def __init__(self):
            self.kwargs = None

        def get(self, *args, **kwargs):
            self.kwargs = kwargs
            return FakeResponse()

    metadata.metadata_cache.clear()
    fake_session = FakeSession()
    try:
        await metadata.fetch_metadata("https://example.com/story", session=fake_session)
    finally:
        metadata.metadata_cache.clear()

    assert fake_session.kwargs["headers"]["User-Agent"] == metadata.USER_AGENT
    assert "text/html" in fake_session.kwargs["headers"]["Accept"]


def test_extract_image_urls_from_html_returns_multiple_candidates_in_priority_order():
    html = """
    <html><head>
      <meta property="og:image" content="/broken-og.jpg" />
      <meta property="og:image:secure_url" content="https://cdn.example.com/secure.jpg" />
      <meta name="twitter:image" content="twitter.jpg" />
      <link rel="image_src" href="/fallback-link.jpg" />
    </head><body>
      <img src="/body-image.jpg" />
    </body></html>
    """

    assert metadata.extract_image_urls_from_html(
        html, "https://example.com/post/page"
    ) == [
        "https://example.com/broken-og.jpg",
        "https://cdn.example.com/secure.jpg",
        "https://example.com/post/twitter.jpg",
        "https://example.com/fallback-link.jpg",
        "https://example.com/body-image.jpg",
    ]


def test_extract_image_urls_from_html_deduplicates_candidates():
    html = """
    <html><head>
      <meta property="og:image" content="/card.jpg" />
      <meta name="twitter:image" content="https://example.com/card.jpg" />
    </head></html>
    """

    assert metadata.extract_image_urls_from_html(html, "https://example.com/post") == [
        "https://example.com/card.jpg"
    ]


def test_extract_og_image_url_picks_first_public_card_image_and_ignores_body_imgs():
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example.com/og.png" />
    </head><body>
      <img src="https://example.com/body.jpg" />
    </body></html>
    """
    assert (
        metadata.extract_og_image_url(html, "https://example.com/post")
        == "https://cdn.example.com/og.png"
    )


def test_extract_og_image_url_skips_private_targets_and_returns_none():
    html = '<html><head><meta property="og:image" content="http://127.0.0.1/og.png" /></head></html>'
    assert metadata.extract_og_image_url(html, "https://example.com/post") is None


def test_extract_og_image_url_returns_none_when_no_card_image():
    html = "<html><head></head><body><img src='https://example.com/body.jpg' /></body></html>"
    assert metadata.extract_og_image_url(html, "https://example.com/post") is None


async def test_download_and_resize_image_reads_chunked_response_until_complete(
    monkeypatch, tmp_path
):
    image_buffer = BytesIO()
    Image.new("RGB", (640, 360), "orange").save(image_buffer, format="JPEG")
    image_bytes = image_buffer.getvalue()

    class ChunkedContent:
        def __init__(self, payload):
            self.payload = payload
            self.offset = 0

        async def read(self, n=-1):
            if self.offset >= len(self.payload):
                return b""
            chunk_size = 17 if n < 0 else min(17, n)
            chunk = self.payload[self.offset : self.offset + chunk_size]
            self.offset += len(chunk)
            return chunk

    class FakeResponse:
        url = "https://cdn.example.com/card.jpg"
        headers = {"Content-Type": "image/jpeg"}

        def __init__(self):
            self.content = ChunkedContent(image_bytes)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            pass

    class FakeSession:
        def __init__(self):
            self.kwargs = None

        def get(self, *args, **kwargs):
            self.kwargs = kwargs
            return FakeResponse()

    monkeypatch.setattr(metadata, "IMAGE_DIR", str(tmp_path))

    fake_session = FakeSession()
    filename = await metadata.download_and_resize_image(
        "https://cdn.example.com/card.jpg", session=fake_session
    )

    assert fake_session.kwargs["headers"]["User-Agent"] == metadata.USER_AGENT
    assert fake_session.kwargs["headers"]["Accept"].startswith("image/")
    assert filename is not None
    assert (tmp_path / filename).exists()


def test_screenshot_capture_uses_domcontentloaded_to_avoid_waiting_for_slow_assets():
    """The Playwright capture must not wait for every subresource.

    Mirrors the old Selenium 'eager' page-load strategy: ``domcontentloaded``
    lets the page paint useful content without stalling on pending
    stylesheets / images / ad-network requests.
    """
    import inspect

    import screenshot

    source = inspect.getsource(screenshot.capture_screenshot)
    assert 'wait_until="domcontentloaded"' in source


async def test_capture_screenshot_with_timeout_returns_none_when_browser_hangs(
    monkeypatch,
):
    cancelled = asyncio.Event()

    async def hanging_capture(url):
        try:
            await asyncio.sleep(60)
            return "late.jpg"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(metadata, "capture_screenshot", hanging_capture)
    monkeypatch.setattr(metadata, "SCREENSHOT_TIMEOUT_SECONDS", 0.01)

    assert await metadata.capture_screenshot_with_timeout("https://example.com") is None
    assert cancelled.is_set()


def test_is_public_http_url_rejects_private_and_local_targets():
    assert not metadata.is_public_http_url("http://localhost/story")
    assert not metadata.is_public_http_url("https://127.0.0.1/story")
    assert not metadata.is_public_http_url("https://192.168.1.10/story")
    assert not metadata.is_public_http_url("https://100.64.0.1/story")
    assert not metadata.is_public_http_url("https://169.254.169.254/latest/meta-data/")
    assert metadata.is_public_http_url("https://example.com/story")


def test_template_uses_wide_two_column_grid():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")

    assert "max-w-7xl" in template
    assert "lg:grid-cols-2" in template


def test_template_story_title_is_linked_hover_only_and_typographically_balanced():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")

    assert 'target="_blank"' in template
    assert "no-underline" in template
    assert "hover:underline" in template
    assert "underline-offset-4" in template
    assert "text-lg" in template
    assert "sm:text-xl" in template
    assert "leading-snug" in template
    assert "mt-auto" in template
    assert "aspect-[4/3]" in template
    assert "border-b-2 border-white/10" in template
    assert "border-t-2 border-white/10" not in template
    assert "via-slate-950/30" not in template


def test_story_image_hover_group_underlines_story_title():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")

    assert "story-image-link" in template
    assert ".story-card:has(.story-image-link:hover) .story-title" in template
    assert "text-decoration-line: underline" in template


def test_template_renders_favicon_before_title_and_muted_domain_after_title():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")

    favicon_idx = template.index("story-favicon")
    title_idx = template.index("{{ story.title }}", favicon_idx)
    domain_idx = template.index("({{ source_domain(story.url) }})")

    assert favicon_idx < title_idx < domain_idx
    assert "{{ favicon_url(story.url) }}" in template
    favicon_markup = template[favicon_idx - 300 : favicon_idx + 300]
    assert "rounded-full" in favicon_markup
    assert "h-8 w-8" in favicon_markup
    assert "border-2 border-white/70" in favicon_markup
    assert "bg-white/95" in favicon_markup
    assert "shadow-lg shadow-black/30" in favicon_markup
    assert "ring-1 ring-black/10" in favicon_markup
    assert "text-slate-500" in favicon_markup
    assert "text-slate-400" in template[domain_idx - 200 : domain_idx + 200]
    assert (
        "onerror=\"this.classList.add('hidden'); this.nextElementSibling.classList.remove('hidden');\""
        in template
    )


def test_template_never_truncates_story_titles():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")
    title_start = template.index("<h2 class=")
    title_end = template.index("</h2>", title_start)
    title_markup = template[title_start:title_end]

    assert "line-clamp" not in title_markup
    assert "{{ story.title }}" in title_markup


def test_background_uses_organic_floating_ambient_blobs():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")
    css = Path("static/css/input.css").read_text(encoding="utf-8")

    assert "ambient-background" in template
    assert template.count("ambient-blob") >= 3
    assert "ambient-blob--orange" in template
    assert "ambient-blob--gold" in template
    assert "ambient-blob--blue" in template
    assert "@keyframes ambient-float" in css
    assert "border-radius: 45% 55% 60% 40%" in css
    assert "animation: ambient-float" in css
    assert "mix-blend-mode: screen" in css
    assert "prefers-reduced-motion" in css


def test_story_image_css_restores_zoom_and_blend_effects():
    css = Path("static/css/input.css").read_text(encoding="utf-8")

    assert "mix-blend-mode" in css
    assert "transform 900ms cubic-bezier(0.2, 0.8, 0.2, 1)" in css
    assert ".story-image-container:hover .story-image" in css
    assert ".group:hover .story-image" in css


def test_template_place_badges_use_emojis_and_prioritize_points_and_comments():
    template = Path("templates/yahnc.html").read_text(encoding="utf-8")

    assert "🥇" in template
    assert "🥈" in template
    assert "🥉" in template

    points_idx = template.index("{{ story.score }} points")
    comments_idx = template.index("{{ story.comments_count }} comments")
    by_idx = template.index("by {{ story.poster }}")
    time_idx = template.index("{{ time_ago(story.time_posted) }}")

    assert points_idx < comments_idx < by_idx < time_idx


def test_is_image_too_small_requires_at_least_400_px_width(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(metadata, "IMAGE_DIR", str(tmp_path))

    small_path = tmp_path / "small.jpg"
    Image.new("RGB", (399, 600), color="white").save(small_path, "JPEG")

    large_path = tmp_path / "large.jpg"
    Image.new("RGB", (400, 600), color="white").save(large_path, "JPEG")

    assert metadata.is_image_too_small("small.jpg") is True
    assert metadata.is_image_too_small("large.jpg") is False
