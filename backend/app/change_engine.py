"""
The actual product, per the design doc: not "track price", but "decide
whether this tick is worth a human's attention, and say why."

process_tick() is called once per symbol per new data point (from the
simulator here; a real feed would call it the same way). It:

  1. Scores the move against the symbol's OWN recent volatility (z-score),
     not a flat % threshold.
  2. Applies a beta filter: if the move is explained by the broader market
     moving too, it's deprioritized, not hidden.
  3. Attempts attribution against NewsEvent (mock EDGAR/news/earnings feed)
     within a time window. No match -> "unexplained", which is treated as
     an actionable signal, not a failure state.
  4. Computes a single attention_score used to rank the digest.
  5. Bumps the symbol's monotonic version and fans the "dirty" signal out
     to every user watching it via the reverse index in cache.py.
"""
import datetime as dt

from sqlalchemy.orm import Session

from . import models
from .cache import cache

Z_THRESHOLD = 2.0          # how surprising a move must be, in units of its own vol, to matter
VOL_ALPHA = 0.06           # EWMA smoothing factor (~ a 30-tick half-life)
VOLUME_ALPHA = 0.1
ATTRIBUTION_WINDOW = dt.timedelta(hours=3)
MARKET_WIDE_INDEX_Z = 1.25  # how much the index itself has to move to call a co-move "market-wide"
MARKET_WIDE_DAMPING = 0.3   # attention multiplier applied when a move is market-wide


def _get_or_create_stats(db: Session, symbol_id: int) -> models.SymbolStats:
    stats = db.get(models.SymbolStats, symbol_id)
    if stats is None:
        stats = models.SymbolStats(symbol_id=symbol_id)
        db.add(stats)
        db.flush()
    return stats


def _attribute(db: Session, symbol_id: int, observed_at: dt.datetime):
    """Look for a news/filing/earnings event near this timestamp."""
    window_start = observed_at - ATTRIBUTION_WINDOW
    window_end = observed_at + dt.timedelta(minutes=5)
    candidates = (
        db.query(models.NewsEvent)
        .filter(
            models.NewsEvent.symbol_id == symbol_id,
            models.NewsEvent.occurred_at >= window_start,
            models.NewsEvent.occurred_at <= window_end,
        )
        .all()
    )
    if not candidates:
        return "unexplained", "", 0.0

    best = min(candidates, key=lambda n: abs((n.occurred_at - observed_at).total_seconds()))
    delta_seconds = abs((best.occurred_at - observed_at).total_seconds())
    proximity = max(0.0, 1 - delta_seconds / ATTRIBUTION_WINDOW.total_seconds())
    confidence = round(0.5 + 0.5 * proximity, 2)  # 0.5 - 1.0
    return best.kind, best.headline, confidence


def process_tick(
    db: Session,
    symbol: models.Symbol,
    price: float,
    volume: float,
    source: str,
    observed_at: dt.datetime,
    index_return: float | None,
    index_ewma_vol: float,
) -> "models.ChangeEvent | None":
    stats = _get_or_create_stats(db, symbol.id)

    # Always log the raw tick, append-only, regardless of significance.
    db.add(models.PriceObservation(
        symbol_id=symbol.id, price=price, volume=volume, source=source,
        observed_at=observed_at, ingested_at=dt.datetime.utcnow(),
        confidence_tier="high",
    ))

    if stats.last_price is None:
        stats.last_price = price
        stats.last_volume = volume
        stats.last_updated = observed_at
        db.flush()
        return None  # no prior price -> nothing to compare against yet

    ret = (price - stats.last_price) / stats.last_price

    safe_vol = max(stats.ewma_return_vol, 0.0008)
    z = ret / safe_vol

    # Volume anomaly: how far above its own recent average is today's volume,
    # expressed as a ratio rather than a second z-score to keep this simple.
    safe_avg_volume = max(stats.ewma_volume, 1.0)
    volume_z = (volume - safe_avg_volume) / safe_avg_volume

    event = None
    if abs(z) >= Z_THRESHOLD:
        is_market_wide = (
            index_return is not None
            and index_ewma_vol > 0
            and (index_return / max(index_ewma_vol, 0.0008)) * (1 if z > 0 else -1) >= MARKET_WIDE_INDEX_Z
        )

        attribution_kind, attribution_detail, attribution_conf = _attribute(db, symbol.id, observed_at)

        # Unexplained moves are still worth surfacing (per design thesis) --
        # they just don't get the extra confidence boost a matched filing gives.
        attribution_factor = 0.6 + 0.4 * attribution_conf
        volume_component = min(2.0, 1.0 + max(0.0, volume_z) * 0.15)
        attention_score = abs(z) * volume_component * attribution_factor
        if is_market_wide:
            attention_score *= MARKET_WIDE_DAMPING

        stats.current_version += 1
        event = models.ChangeEvent(
            symbol_id=symbol.id,
            version=stats.current_version,
            price_before=stats.last_price,
            price_after=price,
            pct_change=ret * 100,
            z_score=z,
            volume_z=volume_z,
            is_market_wide=is_market_wide,
            attribution_kind=attribution_kind,
            attribution_detail=attribution_detail,
            attribution_confidence=attribution_conf,
            attention_score=round(attention_score, 4),
            created_at=observed_at,
        )
        db.add(event)

        # Fan-out: look up watchers via the reverse index, ONE lookup, no
        # per-user recomputation -- this is the scaling answer from the doc.
        for user_id in cache.watchers_of(symbol.id):
            cache.publish_dirty(user_id, {
                "ticker": symbol.ticker,
                "symbol_id": symbol.id,
                "version": stats.current_version,
                "attention_score": event.attention_score,
                "attribution_kind": attribution_kind,
                "is_market_wide": is_market_wide,
            })

    # Update rolling stats AFTER scoring, so the z-score reflects surprise
    # relative to what was known before this tick.
    stats.ewma_return_vol = VOL_ALPHA * abs(ret) + (1 - VOL_ALPHA) * safe_vol
    stats.ewma_volume = VOLUME_ALPHA * volume + (1 - VOLUME_ALPHA) * safe_avg_volume
    stats.last_price = price
    stats.last_volume = volume
    stats.last_updated = observed_at
    db.flush()

    return event
