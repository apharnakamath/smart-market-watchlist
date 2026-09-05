"""
Simulated market feed.

We don't have network access to a real quote/EDGAR/news API in this
environment, so this stands in for "free-tier quote API + EDGAR + a news
API" from the design doc's simple-for-hackathon stack. It calls
change_engine.process_tick() exactly the way a real webhook/poller would,
so swapping this module for a real feed adapter doesn't touch the engine,
schema, or frontend at all.

What it does, every tick (default every 4s):
  - Advances every known symbol with a small random-walk return.
  - Advances an index proxy ("SPY") the same way -- used by the beta filter.
  - Occasionally injects a larger shock into one symbol, sometimes preceded
    by a mock NewsEvent (so some shocks are attributable, others are
    deliberately left "unexplained" to exercise that path).
  - Occasionally injects a market-wide shock (moves several symbols + the
    index together) to exercise the beta filter's deprioritization path.
"""
import asyncio
import datetime as dt
import logging
import random

from sqlalchemy.orm import Session

from . import models
from .change_engine import process_tick
from .database import SessionLocal

logger = logging.getLogger("simulator")

INDEX_TICKER = "SPY"

SEED_SYMBOLS = [
    ("AAPL", "Apple Inc.", "tech"),
    ("MSFT", "Microsoft Corp.", "tech"),
    ("NVDA", "NVIDIA Corp.", "tech"),
    ("TSLA", "Tesla Inc.", "auto"),
    ("AMZN", "Amazon.com Inc.", "retail"),
    ("JPM", "JPMorgan Chase", "finance"),
    ("XOM", "Exxon Mobil", "energy"),
    ("PFE", "Pfizer Inc.", "healthcare"),
    ("NFLX", "Netflix Inc.", "media"),
    ("DIS", "Walt Disney Co.", "media"),
]

MOCK_HEADLINES = {
    "earnings": "Reports quarterly earnings, {beat_miss} analyst estimates",
    "filing": "Files 8-K disclosing {topic}",
    "news": "{topic} reported by wire services",
}

FILING_TOPICS = ["a leadership change", "a new credit facility", "an acquisition agreement", "a restructuring plan"]
NEWS_TOPICS = ["Regulatory scrutiny", "A major product recall", "An analyst upgrade", "A supply chain disruption"]


def ensure_seed_symbols(db: Session):
    if not db.query(models.Symbol).filter_by(ticker=INDEX_TICKER).first():
        db.add(models.Symbol(ticker=INDEX_TICKER, name="S&P 500 ETF (proxy)", sector="index", is_index_proxy=True))
    for ticker, name, sector in SEED_SYMBOLS:
        if not db.query(models.Symbol).filter_by(ticker=ticker).first():
            db.add(models.Symbol(ticker=ticker, name=name, sector=sector))
    db.commit()


def _random_return(scale: float = 1.0) -> float:
    return random.gauss(0, 0.004) * scale


def _maybe_attach_news(db: Session, symbol: models.Symbol, now: dt.datetime, shock_kind: str):
    """~60% of injected shocks get a matching mock news/filing event."""
    if random.random() > 0.6:
        return  # left deliberately unexplained
    if shock_kind == "earnings":
        headline = MOCK_HEADLINES["earnings"].format(beat_miss=random.choice(["beating", "missing"]))
        kind = "earnings"
    elif shock_kind == "filing":
        headline = MOCK_HEADLINES["filing"].format(topic=random.choice(FILING_TOPICS))
        kind = "filing"
    else:
        headline = MOCK_HEADLINES["news"].format(topic=random.choice(NEWS_TOPICS))
        kind = "news"
    db.add(models.NewsEvent(
        symbol_id=symbol.id, headline=f"{symbol.ticker}: {headline}", kind=kind,
        occurred_at=now - dt.timedelta(minutes=random.randint(0, 20)),
    ))
    db.commit()


async def run_forever(interval_seconds: float = 4.0):
    db = SessionLocal()
    try:
        ensure_seed_symbols(db)
    finally:
        db.close()

    logger.info("Market simulator started (interval=%ss)", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        db = SessionLocal()
        try:
            _tick_all(db)
        except Exception:
            logger.exception("simulator tick failed")
        finally:
            db.close()


def _tick_all(db: Session):
    now = dt.datetime.utcnow()
    symbols = db.query(models.Symbol).all()
    index_symbol = next((s for s in symbols if s.is_index_proxy), None)
    others = [s for s in symbols if not s.is_index_proxy]

    # 1) Advance the index proxy first so `others` can react to it.
    index_return = _random_return(scale=1.0)
    market_wide_event = random.random() < 0.06  # ~6% of ticks: a market-wide move
    if market_wide_event:
        index_return += random.choice([-1, 1]) * random.uniform(0.015, 0.03)

    index_stats = None
    index_event = None
    if index_symbol:
        index_stats_before = db.get(models.SymbolStats, index_symbol.id)
        index_price_before = index_stats_before.last_price if index_stats_before and index_stats_before.last_price else 450.0
        index_price = round(index_price_before * (1 + index_return), 4)
        index_event = process_tick(
            db, index_symbol, index_price, volume=random.uniform(5e7, 8e7), source="sim",
            observed_at=now, index_return=None, index_ewma_vol=0.0,
        )
        index_stats = db.get(models.SymbolStats, index_symbol.id)

    idx_ret_for_beta = index_return
    idx_vol_for_beta = index_stats.ewma_return_vol if index_stats else 0.006

    # 2) Advance every other symbol.
    for sym in others:
        stats_before = db.get(models.SymbolStats, sym.id)
        price_before = stats_before.last_price if stats_before and stats_before.last_price else random.uniform(50, 400)

        ret = _random_return(scale=1.0)
        shock_kind = None

        if market_wide_event:
            # Co-move with the market, with some idiosyncratic noise.
            ret += index_return * random.uniform(0.6, 1.1)
        elif random.random() < 0.03:
            # Idiosyncratic shock: single-stock news/filing/earnings type move.
            ret += random.choice([-1, 1]) * random.uniform(0.02, 0.05)
            shock_kind = random.choice(["earnings", "filing", "news"])

        price = round(max(price_before * (1 + ret), 0.01), 4)
        volume = max(1.0, random.gauss(1_500_000, 400_000) * (3 if shock_kind else 1))

        if shock_kind:
            _maybe_attach_news(db, sym, now, shock_kind)

        process_tick(
            db, sym, price, volume=volume, source="sim", observed_at=now,
            index_return=idx_ret_for_beta, index_ewma_vol=idx_vol_for_beta,
        )

    db.commit()
