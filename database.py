# --- START OF FILE database.py ---
from typing import List, Dict, Any
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from models import Base, Story
from sqlalchemy.engine import URL
from datetime import datetime
import logging  # Import logging

# Configure logging for this module
logger = logging.getLogger(__name__)

DATABASE_URL = URL.create(
    "sqlite+aiosqlite",
    database="visual_hn.db",
)

# Set echo=False to reduce SQLAlchemy query logging noise
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={
        "timeout": 15
    },  # SQLite busy timeout — wait up to 15s for a write lock
)
async_session: sessionmaker[AsyncSession] = sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    """Initializes the database tables."""
    async with engine.begin() as conn:
        # Check if tables already exist to avoid warnings/errors depending on setup
        # This simple approach just runs create_all, which is usually fine with SQLite
        # In a real app, you might use Alembic migrations
        await conn.run_sync(Base.metadata.create_all)
        # create_all never ALTERs an existing table, so add columns introduced
        # after the DB file was first created (no Alembic in this project).
        await _ensure_story_columns(conn)
    logger.info("Database initialized successfully.")


async def _ensure_story_columns(conn) -> None:
    """Add columns missing from a pre-existing stories table (lightweight migration)."""
    result = await conn.execute(text("PRAGMA table_info(stories)"))
    existing = {row[1] for row in result}
    if "og_image_url" not in existing:
        await conn.execute(text("ALTER TABLE stories ADD COLUMN og_image_url VARCHAR"))
        logger.info("Added missing column stories.og_image_url")


async def get_stories() -> List[Story]:
    """Fetches stories ordered by current position."""
    async with async_session() as session:
        # Only fetch stories that currently have a position
        result = await session.execute(
            select(Story)
            .where(Story.current_position.isnot(None))
            .order_by(Story.current_position)
        )
        stories = result.scalars().all()
        # logger.debug(f"Fetched {len(stories)} active stories from DB.") # Too noisy?
        return stories


async def get_story_images(ids: List[int]) -> Dict[int, Story]:
    """Fetch stories by HN id for the image API (any position, live or fallen-off)."""
    if not ids:
        return {}
    async with async_session() as session:
        result = await session.execute(select(Story).where(Story.id.in_(ids)))
        return {story.id: story for story in result.scalars().all()}


async def update_stories(new_stories: List[Dict[str, Any]]) -> None:
    """Updates stories in the database based on the latest scrape."""
    async with async_session() as session:
        logger.info(f"Starting database update for {len(new_stories)} new stories...")

        # Fetch existing stories efficiently
        existing_stories_result = await session.execute(select(Story))
        existing_stories_map = {
            story.id: story for story in existing_stories_result.scalars()
        }
        existing_ids = set(existing_stories_map.keys())
        new_ids = {story["id"] for story in new_stories}

        # Handle removed stories (those in existing_ids but not in new_ids)
        removed_ids = existing_ids - new_ids
        for story_id in removed_ids:
            story = existing_stories_map[story_id]  # Get from map
            if (
                story.current_position is not None
            ):  # Only mark as removed if it had a position
                story.last_position = story.current_position
                story.current_position = None
                story.trend = "down"  # Assume 'down' when removed from top
                session.add(story)
                logger.debug(f"Marked story ID {story_id} as removed from top.")

        # Handle new and updated stories
        for new_story_data in new_stories:
            story_id = new_story_data["id"]

            # Convert fields
            new_story_data["poster"] = new_story_data.pop("by", None)
            new_story_data["comments_count"] = new_story_data.pop("descendants", None)
            # Ensure time is handled even if 0 or missing (though HN API provides it)
            new_story_data["time_posted"] = datetime.fromtimestamp(
                new_story_data.pop("time", 0)
            )
            # Ensure text is explicitly handled, defaulting to None
            new_story_data["text"] = new_story_data.get("text")

            # Remove unnecessary fields received from HN API
            new_story_data.pop("kids", None)
            new_story_data.pop("type", None)
            new_story_data.pop(
                "parent", None
            )  # Also remove parent if present (for comments)
            new_story_data.pop(
                "parts", None
            )  # Also remove parts if present (for polls)

            existing_story = existing_stories_map.get(story_id)

            if existing_story:
                # Story exists, update it
                # Determine trend before updating current_position
                new_story_data["last_position"] = existing_story.current_position
                new_story_data["trend"] = determine_trend(
                    existing_story.current_position, new_story_data["current_position"]
                )

                # Image quality preservation: a transient fetch failure
                # (residential node offline, timeout, service restart clearing
                # the in-memory cache) must not permanently degrade a story
                # that previously had a valid preview. Prevent favicon
                # composites and nulls from overwriting og:image URLs or
                # screenshots. String matching avoids coupling to metadata.py.
                new_og = new_story_data.get("og_image_url")
                new_img = new_story_data.get("image_url", "")
                existing_og = existing_story.og_image_url
                existing_img = existing_story.image_url or ""

                if existing_og and not new_og:
                    new_story_data["og_image_url"] = existing_og
                    new_story_data["image_url"] = existing_img
                elif (
                    not existing_og
                    and not new_og
                    and "/fav-" in new_img
                    and "/fav-" not in existing_img
                    and "placeholder" not in existing_img
                ):
                    new_story_data["image_url"] = existing_img

                # Update attributes from new_story_data dictionary
                for key, value in new_story_data.items():
                    # Only update attributes that exist on the model
                    if hasattr(existing_story, key):
                        setattr(existing_story, key, value)
                    else:
                        logger.debug(
                            f"Attempted to set non-existent attribute on Story model: {key}"
                        )

                session.add(existing_story)
                logger.debug(
                    f"Updated story ID {story_id}. Trend: {new_story_data['trend']}"
                )
            else:
                # Story is new
                new_story_data["last_position"] = (
                    None  # New stories have no last position
                )
                new_story_data["trend"] = "same"  # New stories initially have no trend
                # 'retries' is metadata bookkeeping (cache-level retry counter),
                # not a Story column — fetch_metadata includes it in its return dict.
                new_story_data.pop("retries", None)
                story = Story(**new_story_data)
                session.add(story)
                logger.debug(f"Added new story ID {story_id}.")

        await session.commit()
        logger.info("Database update committed successfully.")


def determine_trend(last_pos: int | None, current_pos: int) -> str:
    """Determines the trend (up, down, same) based on position changes."""
    if last_pos is None:
        # If it's a new story in the top N, trend is 'same' initially
        return "same"
    if last_pos == current_pos:
        return "same"
    # Remember lower number is higher position (1st, 2nd, ...)
    return "up" if last_pos > current_pos else "down"


# --- END OF FILE database.py ---
