# Visual-HN

Visual-HN is a FastAPI proxy that sits in front of hcker.news, enriches stories with preview images and Open Graph metadata, and exposes the data through a clean API consumed by the [Visual-HN](https://github.com/ilyaizen/visual-hn/tree/main/visual-hn-previews) Chrome extension.

**Live:** [hn.is-ai-good-yet.com](https://hn.is-ai-good-yet.com)

![Visual-HN screenshot](static/screenshot.png)

## Features

- Proxies the hcker.news homepage with preview images injected
- Fetches Open Graph metadata and resizes images for consistent display
- Multi-layer anti-scraping pipeline for reliable metadata extraction
- Exposes story metadata and image assets through the Visual-HN API
- Tracks story position trends over time
- Minimal landing page at `/`

## Tech Stack

- **Backend:** Python 3.10+, FastAPI, SQLAlchemy, aiosqlite, aiohttp, curl_cffi
- **Scraping:** curl_cffi (TLS fingerprint impersonation), BeautifulSoup4, Pillow, Playwright
- **Frontend:** Proxied hcker.news runtime; legacy HTML/Tailwind page (retired)
- **Database:** SQLite with async access

## Setup

> The proxy/scraper runs on Ubuntu. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full operational guide. Commands below are VPS-only (Ubuntu/bash).

```bash
git clone https://github.com/ilyaizen/visual-hn.git
cd visual-hn
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://localhost:8000`.

## Project Structure

| File                   | Purpose                                       |
| ---------------------- | --------------------------------------------- |
| `main.py`              | FastAPI app, routes, lifespan setup           |
| `hcker_proxy.py`       | hcker.news proxy with preview asset injection |
| `hn_scraper.py`        | Fetches top stories from the HN Firebase API  |
| `database.py`          | Async persistence, trend calculation          |
| `metadata.py`          | Open Graph parsing, image download/resize     |
| `models.py`            | SQLAlchemy ORM models                         |
| `templates/`           | Legacy HTML templates                         |
| `static/`              | CSS, images, favicon                          |
| `visual-hn-previews/` | Chrome extension for hcker.news               |

## Contributing

Contributions are welcome. Current focus is the hcker.news proxy, preview assets, and the Visual-HN API.

## License

MIT
