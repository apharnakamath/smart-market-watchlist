# Smart Market Watchlist

Live Demo : https://smart-watchlist-xfx7.onrender.com/

> **A watchlist that tells you what deserves your attention, not just what changed.**

Most market watchlists answer one question: **What are my stocks doing?**

This project answers a more useful one:

> **"What meaningfully changed since I last checked, and what should I look at first?"**

Instead of presenting a chronological grid of prices, Smart Market Watchlist turns a user's watchlist into a **market triage system**. It detects statistically unusual moves, looks for possible explanations, accounts for market-wide movement, and ranks the resulting events by an attention score.

---

## Why this exists

A flat rule such as `show every stock that moved more than 3%` is not very useful.

A 3% move can be:

- extremely unusual for a normally quiet stock,
- completely normal for a highly volatile stock, or
- part of a market-wide move that does not deserve the same level of attention.

This project therefore separates **movement** from **meaningful movement**.

The core product loop is:

```text
Market observations
        ↓
Statistical change detection
        ↓
Attribution + market-wide context
        ↓
Attention score
        ↓
What deserves your attention?
        ↓
Server-side unread state
        ↓
What changed since you last checked?
```

---

## What the application does

### 1. Create and manage a watchlist

Users can:

- add symbols to their watchlist,
- remove symbols,
- see the latest observed price,
- see how recently that price was observed, and
- immediately identify symbols with unread meaningful events.

### 2. Detect meaningful changes

The system does **not** use a fixed percentage threshold.

For each symbol, the change engine maintains an exponentially weighted estimate of its recent return volatility (EWMA volatility).

For a new return:

```text
z-score = return / expected volatility
```

A move is promoted to a `ChangeEvent` when:

```text
abs(z-score) >= 2
```

This means the threshold adapts to each stock's own behaviour.

For example, a 3% move can be highly significant for a low-volatility stock but much less surprising for a stock that regularly moves several percent.

### 3. Explain the move when possible

Qualifying moves are checked against recent mock:

- news,
- filings, and
- earnings events.

If a matching event exists, it is attached to the change event. If there is no explanation, the move is explicitly labelled **unexplained** rather than silently discarded.

This is intentional: an unexplained statistically unusual move can itself be worth investigating.

### 4. Deprioritize market-wide noise

The simulator also provides an index proxy (`SPY`). If a stock moves strongly in the same direction as the broader market, the event is marked `is_market_wide` and its attention score is damped.

The event is **not deleted**. It is simply ranked lower because the movement is less idiosyncratic.

### 5. Rank by attention, not chronology

Every meaningful event receives an attention score based on:

- statistical surprise,
- abnormal volume,
- attribution confidence, and
- whether the movement appears market-wide.

The result is a **triage list rather than a price grid**.

---

## What changed since I last checked?

This is the main state-management feature of the project.

The application does not compare browser timestamps or rely on client-only read state.

Each symbol has a monotonically increasing:

```text
current_version
```

in `SymbolStats`.

The version increments only when the change engine produces a meaningful `ChangeEvent`.

For each user/symbol pair, the server stores:

```text
last_seen_version
```

in `UserSymbolSeen`.

Therefore:

```text
current_version > last_seen_version
```

means:

> **Something meaningful happened since this user last checked this symbol.**

This is deliberately similar to an unread-message counter: it is more robust than trying to diff timestamps.

Because the read state is stored in the database, the state survives browser sessions and can be shared across devices when the same username is used.

---

# Data quality: stale, delayed and conflicting data

Market data should not be treated as if every observation were equally trustworthy.

`PriceObservation` is therefore **append-only**. Every observation retains:

- `source` : where the observation came from,
- `observed_at` : when the price was observed,
- `ingested_at` : when the system received it, and
- `confidence_tier` : e.g. `high`, `delayed`, or `disputed`.

The UI exposes observation age as a staleness indicator instead of hiding it.

The schema also supports disputed observations from multiple sources through the confidence model. In this submission, the running demo intentionally uses a **single simulated source**, so the full multi-source disagreement path is represented in the data model but is not actively populated by two independent external providers.

This keeps the demo deterministic and dependency-free while leaving the ingestion boundary clean for a real provider.

---

# Scaling strategy

The key scaling decision is:

> **Compute a market event once per symbol, then fan it out to interested users.**

Suppose 10,000 users watch AAPL.

The system does **not** run the change engine 10,000 times.

Instead:

```text
AAPL observation
      ↓
change engine (once)
      ↓
ChangeEvent
      ↓
reverse index: AAPL → interested users
      ↓
fan-out notification
```

`cache.py` maintains the reverse index:

```text
symbol_id → set(user_ids)
```

The implementation uses an in-process cache by default, with an optional Redis backend for a multi-process deployment.

For a larger production deployment, the natural evolution is:

```text
                    ┌─ API instances ─┐
Market feed → queue ┤                 ├→ Redis/pub-sub → clients
                    └─ change worker ─┘
                           ↓
                        Postgres
```

The important architectural boundary is already present: **raw market observations are separate from derived change events and per-user read state.**

---

# Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                         Frontend                            │
│                 HTML / CSS / JavaScript                     │
│                                                             │
│   Watchlist  →  Attention Digest  →  Symbol Detail         │
│                         ↑                                   │
│                    SSE / fallback                          │
└─────────────────────────┬───────────────────────────────────┘
                          │ HTTP / SSE
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                         FastAPI                             │
│                                                             │
│  Watchlist API   Digest API   History API   SSE stream      │
│                         │                                   │
│                         ↓                                   │
│                  Change Engine                             │
│          z-score + volume + attribution                     │
│                    + market filter                          │
│                         │                                   │
│                         ↓                                   │
│                    SQLite / SQLAlchemy                      │
│                                                             │
│ Users | Symbols | Watchlist | Observations                  │
│ Stats | ChangeEvents | NewsEvents | UserSymbolSeen          │
└─────────────────────────┬───────────────────────────────────┘
                          ↑
                          │
                    Simulated Feed
                    (~4 second ticks)
```

---

# Technology stack

### Backend

- **Python 3.10+**
- **FastAPI** : REST API and SSE streaming
- **SQLAlchemy** : database abstraction / ORM
- **SQLite** : zero-setup persistence for the demo
- **Redis (optional)** : distributed reverse-index / pub-sub backend

### Frontend

- Plain **HTML / CSS / JavaScript**
- No React or frontend build step
- Native `EventSource` for live SSE updates
- Canvas-based charting

### Market simulation

The repository includes a deterministic product-level simulation rather than depending on an external market-data API.

The simulator generates:

- normal random-walk movement,
- idiosyncratic shocks,
- abnormal volume,
- market-wide shocks, and
- mock news / filing / earnings events.

The important boundary is that the simulator calls the same `process_tick()` pipeline a real market-data adapter would call.

---

# Running locally

## Requirements

- Python **3.10+**
- `pip`

No database server, Node.js installation, Redis instance, or frontend build system is required for the default demo.

## Start the application

```bash
cd backend
./run.sh
```

Then open:

```text
http://localhost:8000
```

The simulator starts automatically and produces new observations roughly every four seconds.

The seeded universe includes:

```text
AAPL  MSFT  NVDA  TSLA  AMZN
JPM   XOM   PFE   NFLX  DIS
```

You can also add another ticker string to the watchlist; it will participate in the simulator after being created.

### If port 8000 is already in use

```bash
cd backend
uvicorn app.main:app --port 8001
```

Then visit:

```text
http://localhost:8001
```

---

# API overview

The backend exposes endpoints for:

```text
GET    /api/symbols
GET    /api/watchlist
POST   /api/watchlist
DELETE /api/watchlist/{ticker}
GET    /api/digest
GET    /api/symbol/{ticker}
GET    /api/events
```

The exact request/response behaviour can be inspected directly in FastAPI's generated API documentation while the server is running:

```text
http://localhost:8000/docs
```

---

# Database model

The main entities are:

### `User`
Identifies a user for the prototype.

### `Symbol`
Stores ticker metadata and identifies the index proxy.

### `WatchlistItem`
Many-to-many relationship between users and symbols, with a uniqueness constraint preventing duplicate entries.

### `PriceObservation`
Append-only raw market observations.

### `SymbolStats`
Rolling state used by the change engine, including EWMA volatility and the current meaningful-event version.

### `NewsEvent`
Mock attribution events representing news, filings and earnings.

### `ChangeEvent`
Derived events that actually cleared the meaningful-change threshold.

### `UserSymbolSeen`
Per-user read state for the "since you last checked" experience.

---

# Design decisions and trade-offs

## Why a simulator instead of a live API?

The repository is intentionally self-contained and does not require external market-data credentials or network access.

`simulator.py` acts as the ingestion adapter and feeds the exact same change-processing pipeline that a real provider would use.

In production, it can be replaced with adapters for a quote provider, news source, and filings source without changing the product's core change-detection model.

## Why no authentication?

Authentication is intentionally simplified for the hackathon prototype. The frontend supplies a username and the backend uses it to persist per-user state.

This demonstrates the important state-management architecture without spending the project budget on a full authentication system.

In production, `get_current_user()` would be replaced with a session/JWT/OIDC-backed identity provider; the downstream watchlist and read-state model does not need to change.

## Why SQLite?

SQLite makes the submission completely runnable with zero infrastructure.

The application uses SQLAlchemy, so moving to Postgres is primarily a database configuration/deployment change rather than a rewrite of the application model.

## Why SSE instead of WebSockets?

The browser mainly needs a one-way notification:

> "A meaningful event happened; refresh your digest."

Server-Sent Events are simpler than WebSockets for that interaction. A lightweight polling fallback is also retained for reliability.

## What was intentionally not built?

The project deliberately avoids unnecessary infrastructure such as Kafka, Kubernetes, a frontend build pipeline, or multiple microservices.

The complexity is concentrated where it affects the user experience:

- meaningful-change detection,
- attribution,
- attention ranking,
- stale-data visibility, and
- persistent unread state.

---

# Limitations / production next steps

This is a hackathon prototype, not a production trading system.

The main production upgrades would be:

1. **Real market data** — replace the simulator with authenticated quote/feed adapters.
2. **Real news and filings** — connect attribution to trusted external sources such as filings and news providers.
3. **Multi-source reconciliation** — ingest the same quote from multiple providers and surface disagreements explicitly.
4. **Authentication** — replace the prototype username with OIDC/JWT/session authentication.
5. **Postgres + Redis** — move persistence and fan-out to managed infrastructure for horizontal scaling.
6. **Background workers** — separate feed ingestion and change computation from API processes.
7. **Observability** — add metrics, structured logs, health checks and alerting.
8. **Market-calendar awareness** — distinguish market-open, delayed and stale conditions instead of treating every elapsed second equally.

These are deliberate extensions rather than hidden dependencies of the current demo.

---

# Project structure

```text
backend/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI routes, identity, SSE and startup
│   ├── models.py          # SQLAlchemy data model
│   ├── database.py        # Database engine/session configuration
│   ├── change_engine.py   # Meaningful-change detection + scoring
│   ├── simulator.py       # Self-contained market/news simulator
│   ├── cache.py           # Reverse index + optional Redis fan-out
│   └── static/
│       ├── index.html     # Frontend
│       ├── style.css      # UI styling
│       └── app.js         # Frontend behaviour / API integration
├── requirements.txt
└── run.sh
```

---

# Product thesis

A market watchlist should not make users scan ten prices and decide what matters.

It should do the first layer of triage for them.

**Smart Market Watchlist is built around that idea: detect the unusual, add context, remember what the user has already seen, and put the most actionable changes first.**
