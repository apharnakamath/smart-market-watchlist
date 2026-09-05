"""
Smart Market Watchlist -- API layer.

This file wires everything else together:
  - REST endpoints for managing a watchlist and reading the triage digest
  - An SSE stream for live "something changed" pushes while the tab is open
  - Startup: create tables, seed the mock symbol universe, rebuild the
    in-process reverse index from the DB, and kick off the simulated feed

Identity: there's no auth here (out of scope for the hackathon window --
see README "cut entirely"). A user is identified by an `X-User` header
(the frontend asks for a name once and remembers it in localStorage). The
important design point this still demonstrates: read state
(UserSymbolSeen) lives on the SERVER keyed by user+symbol, not in
localStorage -- so two browsers/devices using the same username see
consistent "what changed since I left" state. Swapping the header for a
real session/JWT wouldn't touch anything below the `get_current_user`
dependency.
"""
import asyncio
import datetime as dt
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import models
from .cache import cache
from .database import Base, engine, SessionLocal, get_db
from .simulator import run_forever

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

Base.metadata.create_all(bind=engine)


def _rebuild_reverse_index():
    """In-memory cache is empty on (re)start; repopulate it from the DB
    so fan-out keeps working immediately without waiting for re-subscribes."""
    db = SessionLocal()
    try:
        for item in db.query(models.WatchlistItem).all():
            cache.subscribe_user_to_symbol(item.user_id, item.symbol_id)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _rebuild_reverse_index()
    task = asyncio.create_task(run_forever())
    logger.info("Cache backend: %s", cache.backend)
    yield
    task.cancel()


app = FastAPI(title="Smart Market Watchlist", lifespan=lifespan)


# ---------------------------------------------------------------- identity --
def get_current_user(
    x_user: str = Header(default="demo"),
    user_qs: str | None = Query(default=None, alias="user"),
    db: Session = Depends(get_db),
) -> models.User:
    # EventSource (used for the SSE stream) can't set custom headers, so
    # that endpoint identifies the user via a query param instead; every
    # other endpoint uses the X-User header. Query param wins if present.
    raw = user_qs or x_user or "demo"
    username = raw.strip().lower()[:64] or "demo"
    user = db.query(models.User).filter_by(username=username).first()
    if not user:
        user = models.User(username=username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


# ------------------------------------------------------------------ symbols --
@app.get("/api/symbols")
def list_symbols(exclude_watched: bool = True, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    watched_ids = set()
    if exclude_watched:
        watched_ids = {w.symbol_id for w in db.query(models.WatchlistItem).filter_by(user_id=user.id)}
    symbols = db.query(models.Symbol).filter_by(is_index_proxy=False).all()
    return [
        {"ticker": s.ticker, "name": s.name, "sector": s.sector}
        for s in symbols if s.id not in watched_ids
    ]


class AddSymbolBody(BaseModel):
    ticker: str


def _get_or_create_symbol(db: Session, ticker: str) -> models.Symbol:
    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 10:
        raise HTTPException(400, "invalid ticker")
    symbol = db.query(models.Symbol).filter_by(ticker=ticker).first()
    if not symbol:
        symbol = models.Symbol(ticker=ticker, name=ticker, sector="custom")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
    return symbol


# ---------------------------------------------------------------- watchlist --
def _serialize_watchlist_item(db: Session, user: models.User, symbol: models.Symbol) -> dict:
    stats = db.get(models.SymbolStats, symbol.id)
    seen = (
        db.query(models.UserSymbolSeen)
        .filter_by(user_id=user.id, symbol_id=symbol.id)
        .first()
    )
    last_seen_version = seen.last_seen_version if seen else 0
    current_version = stats.current_version if stats else 0

    last_obs = (
        db.query(models.PriceObservation)
        .filter_by(symbol_id=symbol.id)
        .order_by(models.PriceObservation.observed_at.desc())
        .first()
    )
    latest_event = (
        db.query(models.ChangeEvent)
        .filter_by(symbol_id=symbol.id)
        .order_by(models.ChangeEvent.created_at.desc())
        .first()
    )

    staleness_seconds = None
    if last_obs:
        staleness_seconds = (dt.datetime.utcnow() - last_obs.observed_at).total_seconds()

    return {
        "ticker": symbol.ticker,
        "name": symbol.name,
        "sector": symbol.sector,
        "price": stats.last_price if stats else None,
        "has_unseen_change": current_version > last_seen_version,
        "unseen_count": max(0, current_version - last_seen_version),
        "staleness_seconds": staleness_seconds,
        "confidence_tier": last_obs.confidence_tier if last_obs else None,
        "latest_event": _serialize_event(latest_event) if latest_event else None,
    }


def _serialize_event(e: models.ChangeEvent) -> dict:
    return {
        "id": e.id,
        "ticker": e.symbol.ticker if e.symbol else None,
        "version": e.version,
        "created_at": e.created_at.isoformat() + "Z",
        "pct_change": round(e.pct_change, 3),
        "z_score": round(e.z_score, 2),
        "volume_z": round(e.volume_z, 2),
        "is_market_wide": e.is_market_wide,
        "attribution_kind": e.attribution_kind,
        "attribution_detail": e.attribution_detail,
        "attribution_confidence": e.attribution_confidence,
        "attention_score": e.attention_score,
    }


@app.get("/api/watchlist")
def get_watchlist(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    items = db.query(models.WatchlistItem).filter_by(user_id=user.id).all()
    rows = [_serialize_watchlist_item(db, user, item.symbol) for item in items]
    rows.sort(key=lambda r: (r["latest_event"] or {}).get("attention_score", 0), reverse=True)
    return {"user": user.username, "cache_backend": cache.backend, "items": rows}


@app.post("/api/watchlist")
def add_to_watchlist(body: AddSymbolBody, db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    symbol = _get_or_create_symbol(db, body.ticker)
    existing = db.query(models.WatchlistItem).filter_by(user_id=user.id, symbol_id=symbol.id).first()
    if not existing:
        db.add(models.WatchlistItem(user_id=user.id, symbol_id=symbol.id))
        db.commit()
    cache.subscribe_user_to_symbol(user.id, symbol.id)
    return _serialize_watchlist_item(db, user, symbol)


@app.delete("/api/watchlist/{ticker}")
def remove_from_watchlist(ticker: str, db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    symbol = db.query(models.Symbol).filter_by(ticker=ticker.upper()).first()
    if not symbol:
        raise HTTPException(404, "unknown ticker")
    db.query(models.WatchlistItem).filter_by(user_id=user.id, symbol_id=symbol.id).delete()
    db.commit()
    cache.unsubscribe_user_from_symbol(user.id, symbol.id)
    return {"ok": True}


@app.post("/api/watchlist/{ticker}/seen")
def mark_seen(ticker: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    symbol = db.query(models.Symbol).filter_by(ticker=ticker.upper()).first()
    if not symbol:
        raise HTTPException(404, "unknown ticker")
    stats = db.get(models.SymbolStats, symbol.id)
    current_version = stats.current_version if stats else 0
    row = db.query(models.UserSymbolSeen).filter_by(user_id=user.id, symbol_id=symbol.id).first()
    if not row:
        row = models.UserSymbolSeen(user_id=user.id, symbol_id=symbol.id)
        db.add(row)
    row.last_seen_version = current_version
    db.commit()
    return {"ok": True, "last_seen_version": current_version}


@app.post("/api/digest/seen-all")
def mark_all_seen(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    items = db.query(models.WatchlistItem).filter_by(user_id=user.id).all()
    for item in items:
        stats = db.get(models.SymbolStats, item.symbol_id)
        current_version = stats.current_version if stats else 0
        row = db.query(models.UserSymbolSeen).filter_by(user_id=user.id, symbol_id=item.symbol_id).first()
        if not row:
            row = models.UserSymbolSeen(user_id=user.id, symbol_id=item.symbol_id)
            db.add(row)
        row.last_seen_version = current_version
    db.commit()
    return {"ok": True}


# -------------------------------------------------------------------- digest --
@app.get("/api/digest")
def get_digest(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """The triage list: every ChangeEvent the user hasn't seen yet, across
    their whole watchlist, ranked by attention_score -- not chronologically."""
    items = db.query(models.WatchlistItem).filter_by(user_id=user.id).all()
    results = []
    for item in items:
        seen = db.query(models.UserSymbolSeen).filter_by(user_id=user.id, symbol_id=item.symbol_id).first()
        last_seen_version = seen.last_seen_version if seen else 0
        events = (
            db.query(models.ChangeEvent)
            .filter(models.ChangeEvent.symbol_id == item.symbol_id,
                    models.ChangeEvent.version > last_seen_version)
            .all()
        )
        results.extend(events)
    results.sort(key=lambda e: e.attention_score, reverse=True)
    return [_serialize_event(e) for e in results[:50]]


@app.get("/api/symbols/{ticker}/history")
def symbol_history(ticker: str, db: Session = Depends(get_db)):
    symbol = db.query(models.Symbol).filter_by(ticker=ticker.upper()).first()
    if not symbol:
        raise HTTPException(404, "unknown ticker")
    obs = (
        db.query(models.PriceObservation)
        .filter_by(symbol_id=symbol.id)
        .order_by(models.PriceObservation.observed_at.desc())
        .limit(200)
        .all()
    )
    events = (
        db.query(models.ChangeEvent)
        .filter_by(symbol_id=symbol.id)
        .order_by(models.ChangeEvent.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "ticker": symbol.ticker,
        "prices": [
            {"price": o.price, "observed_at": o.observed_at.isoformat() + "Z"}
            for o in reversed(obs)
        ],
        "events": [_serialize_event(e) for e in events],
    }


# ---------------------------------------------------------------------- SSE --
@app.get("/api/events")
async def sse_stream(user: models.User = Depends(get_current_user)):
    queue = cache.register_listener(user.id)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            cache.unregister_listener(user.id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------- static --
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
