"""
Database engine/session setup.

SQLite is used instead of Postgres purely so the hackathon zip runs with
zero external services. Everything is written against the SQLAlchemy ORM,
so swapping DATABASE_URL to a Postgres DSN is the only change needed to
move to the "real" deployment target described in the design doc.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./watchlist.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
