"""
SQLAlchemy ORM models.

Design notes (see README for the full rationale):

- PriceObservation is append-only. We never overwrite a price -- every
  ingested point is tagged with (source, observed_at, ingested_at,
  confidence_tier) so disagreement between sources and staleness are
  first-class, queryable facts instead of being silently lost.

- ChangeEvent is the output of the change engine, not the raw feed. A
  row only exists here when something cleared the statistical-significance
  bar. `version` is a per-symbol monotonic counter (bumped in
  SymbolVersionState) -- this is the "unread messages" trick: a client
  just needs to compare an integer, not diff timestamps.

- UserSymbolSeen stores `last_seen_version` per (user, symbol) on the
  server, so "what changed since I left" is correct across devices and
  browser sessions for free -- there is no client-side state to lose.

- NewsEvent is the mock attribution source (stands in for an EDGAR filing
  / earnings date / news headline feed). The change engine matches
  ChangeEvents against it inside a time window.
"""
import datetime as dt

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Boolean,
    UniqueConstraint, Text
)
from sqlalchemy.orm import relationship

from .database import Base


def utcnow():
    return dt.datetime.utcnow()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class Symbol(Base):
    __tablename__ = "symbols"
    id = Column(Integer, primary_key=True)
    ticker = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, default="")
    sector = Column(String, default="general")
    is_index_proxy = Column(Boolean, default=False)  # e.g. SPY, used for the beta filter


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False)
    added_at = Column(DateTime, default=utcnow)

    symbol = relationship("Symbol")

    __table_args__ = (UniqueConstraint("user_id", "symbol_id", name="uq_user_symbol"),)


class PriceObservation(Base):
    """Append-only ingest log. Never updated in place."""
    __tablename__ = "price_observations"
    id = Column(Integer, primary_key=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False, index=True)
    price = Column(Float, nullable=False)
    volume = Column(Float, default=0)
    source = Column(String, default="sim")
    observed_at = Column(DateTime, default=utcnow)   # when the price was true
    ingested_at = Column(DateTime, default=utcnow)   # when we received it
    confidence_tier = Column(String, default="high")  # high / delayed / disputed


class SymbolStats(Base):
    """Rolling state for the change engine (EWMA vol, last price, etc)."""
    __tablename__ = "symbol_stats"
    symbol_id = Column(Integer, ForeignKey("symbols.id"), primary_key=True)
    last_price = Column(Float, nullable=True)
    last_volume = Column(Float, nullable=True)
    ewma_return_vol = Column(Float, default=0.01)   # daily-scale realized vol proxy
    ewma_volume = Column(Float, default=1000.0)
    current_version = Column(Integer, default=0)     # bumped on every ChangeEvent
    last_updated = Column(DateTime, default=utcnow)


class NewsEvent(Base):
    """Mock attribution source: stands in for EDGAR filings / earnings / news."""
    __tablename__ = "news_events"
    id = Column(Integer, primary_key=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False, index=True)
    headline = Column(Text, nullable=False)
    kind = Column(String, default="news")  # news / filing / earnings
    occurred_at = Column(DateTime, default=utcnow)


class ChangeEvent(Base):
    """A move that cleared the statistical-significance bar."""
    __tablename__ = "change_events"
    id = Column(Integer, primary_key=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)  # per-symbol monotonic version at time of event
    created_at = Column(DateTime, default=utcnow, index=True)

    price_before = Column(Float)
    price_after = Column(Float)
    pct_change = Column(Float)
    z_score = Column(Float)
    volume_z = Column(Float)

    is_market_wide = Column(Boolean, default=False)  # beta filter tripped
    attribution_kind = Column(String, default="unexplained")  # news / filing / earnings / unexplained
    attribution_detail = Column(Text, default="")
    attribution_confidence = Column(Float, default=0.0)

    attention_score = Column(Float, default=0.0, index=True)

    symbol = relationship("Symbol")


class UserSymbolSeen(Base):
    """Server-side read state -- this is what makes 'changed since last
    checked' correct across devices instead of a client-local timestamp."""
    __tablename__ = "user_symbol_seen"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False)
    last_seen_version = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("user_id", "symbol_id", name="uq_user_symbol_seen"),)
