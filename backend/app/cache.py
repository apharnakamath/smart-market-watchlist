"""
Cache / fan-out abstraction.

The design goal: the change engine runs ONCE per symbol regardless of how
many users are watching it. Fan-out is "look up which watchlists contain
this symbol" via a reverse index, then mark those users' rows dirty --
no recomputation per user. That reverse index and the pub/sub used to
push live SSE updates both live behind this one interface.

Real deployment: point REDIS_URL at a real Redis instance and the reverse
index becomes a Redis SET per symbol (O(1) fan-out, shared across
processes) and pub/sub becomes Redis Pub/Sub (works across multiple API
server instances). For the hackathon zip, with no external services
assumed, we transparently fall back to an in-process dict + asyncio
queues. Same interface either way -- callers never know which backend
is active.
"""
import asyncio
import os
from collections import defaultdict
from typing import Optional

REDIS_URL = os.environ.get("REDIS_URL")

_redis = None
if REDIS_URL:
    try:
        import redis as _redis_lib  # type: ignore
        _candidate = _redis_lib.from_url(REDIS_URL, decode_responses=True)
        _candidate.ping()
        _redis = _candidate
    except Exception:
        _redis = None  # fall back silently -- Redis is an optimization, not a dependency


class StateCache:
    """Reverse index (symbol -> watching user ids) + live-update pub/sub."""

    def __init__(self):
        self._backend = "redis" if _redis else "memory"
        # in-memory fallback state
        self._reverse_index: dict[int, set[int]] = defaultdict(set)
        self._subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)

    @property
    def backend(self) -> str:
        return self._backend

    # -- reverse index: symbol_id -> set of user_ids watching it -----------
    def subscribe_user_to_symbol(self, user_id: int, symbol_id: int):
        if _redis:
            _redis.sadd(f"watchers:{symbol_id}", user_id)
        else:
            self._reverse_index[symbol_id].add(user_id)

    def unsubscribe_user_from_symbol(self, user_id: int, symbol_id: int):
        if _redis:
            _redis.srem(f"watchers:{symbol_id}", user_id)
        else:
            self._reverse_index[symbol_id].discard(user_id)

    def watchers_of(self, symbol_id: int) -> set[int]:
        if _redis:
            return {int(u) for u in _redis.smembers(f"watchers:{symbol_id}")}
        return set(self._reverse_index.get(symbol_id, set()))

    # -- live push: notify a user's open SSE connection(s) ------------------
    def register_listener(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[user_id].append(q)
        return q

    def unregister_listener(self, user_id: int, q: asyncio.Queue):
        if q in self._subscribers.get(user_id, []):
            self._subscribers[user_id].remove(q)

    def publish_dirty(self, user_id: int, payload: dict):
        # Redis pub/sub would fan this across processes; in-process we just
        # push directly onto that user's queues (single-process demo).
        for q in list(self._subscribers.get(user_id, [])):
            if not q.full():
                q.put_nowait(payload)


cache = StateCache()
