import pytest
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from models import Base, Story
from datetime import datetime

# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def test_db():
    # Create test engine
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create session
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = async_session()

    # Yield session for tests
    yield session

    # Close session
    await session.close()

    # Drop tables after tests
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_story_creation(test_db):
    """Test creating and retrieving a story from the database."""
    # Create a test story
    test_story = Story(
        id=1,
        title="Test Story",
        url="https://example.com",
        hn_url="https://news.ycombinator.com/item?id=1",
        score=100,
        poster="testuser",
        comments_count=10,
        time_posted=datetime.now(),
        description="This is a test story",
        image_url="/static/images/test.jpg",
        current_position=1,
        last_position=None,
        trend="same",
    )

    # Add story to database
    test_db.add(test_story)
    await test_db.commit()

    # Query the story
    result = await test_db.execute(select(Story).where(Story.id == 1))
    story = result.scalars().first()

    # Verify story data
    assert story is not None
    assert story.id == 1
    assert story.title == "Test Story"
    assert story.url == "https://example.com"
    assert story.score == 100
    assert story.poster == "testuser"
    assert story.comments_count == 10
    assert story.description == "This is a test story"
    assert story.image_url == "/static/images/test.jpg"
    assert story.current_position == 1
    assert story.last_position is None
    assert story.trend == "same"


if __name__ == "__main__":
    asyncio.run(pytest.main(["-xvs", "test_database.py"]))
