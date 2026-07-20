from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, DateTime, Text

Base = declarative_base()


class Story(Base):
    __tablename__ = "stories"

    id = Column(Integer, primary_key=True)
    title = Column(String)
    url = Column(String)
    hn_url = Column(String)
    score = Column(Integer)
    poster = Column(String)
    comments_count = Column(Integer)
    time_posted = Column(DateTime)
    description = Column(Text)
    image_url = Column(String)  # local fallback asset (screenshot) or placeholder
    og_image_url = Column(
        String
    )  # remote og:image URL, loaded client-side (not stored on box)
    last_position = Column(Integer)
    current_position = Column(Integer)
    trend = Column(String)
    text = Column(Text)
