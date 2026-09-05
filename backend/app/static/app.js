const state = {
  username: localStorage.getItem("watchlist_user") || "demo",
};

const usernameInput = document.getElementById("username");
const cacheBadge = document.getElementById("cache-badge");
const digestList = document.getElementById("digest-list");
const digestCount = document.getElementById("digest-count");
const watchlistEl = document.getElementById("watchlist");
const addForm = document.getElementById("add-form");
const addInput = document.getElementById("add-input");
const suggestions = document.getElementById("symbol-suggestions");
const markAllSeenBtn = document.getElementById("mark-all-seen");
const modal = document.getElementById("detail-modal");
const closeModalBtn = document.getElementById("close-modal");

usernameInput.value = state.username;

function headers(extra = {}) {
  return { "X-User": state.username, "Content-Type": "application/json", ...extra };
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: headers(opts.headers) });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.status === 204 ? null : res.json();
}

function fmtPct(p) {
  const sign = p > 0 ? "+" : "";
  return `${sign}${p.toFixed(2)}%`;
}

function fmtStale(seconds) {
  if (seconds == null) return "no data";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function attributionTag(ev) {
  if (ev.attribution_kind === "unexplained") {
    return `<span class="tag unexplained">unexplained</span>`;
  }
  return `<span class="tag attributed">${ev.attribution_kind} · ${Math.round(ev.attribution_confidence * 100)}% match</span>`;
}

function renderDigest(events) {
  digestCount.textContent = events.length;
  if (!events.length) {
    digestList.innerHTML = `<div class="empty-state">No unread signals right now — everything you're watching is within its normal range, or already marked read.</div>`;
    return;
  }
  digestList.innerHTML = events.map((ev, i) => {
    const dir = ev.pct_change >= 0 ? "up" : "down";
    const headline = ev.is_market_wide
      ? `${ev.ticker} moved ${fmtPct(ev.pct_change)} — but so did the broader market; likely not stock-specific.`
      : `${ev.ticker} moved ${fmtPct(ev.pct_change)}, a ${Math.abs(ev.z_score).toFixed(1)}× surprise against its normal daily swing.`;
    return `
      <div class="digest-row" data-ticker="${ev.ticker}">
        <div class="digest-rank">${i + 1}</div>
        <div class="digest-ticker">${ev.ticker}</div>
        <div class="digest-main">
          <div class="headline">${headline}</div>
          <div class="meta">
            <span class="pct ${dir}">${fmtPct(ev.pct_change)}</span>
            <span>vol z ${ev.volume_z >= 0 ? "+" : ""}${ev.volume_z.toFixed(1)}</span>
            ${attributionTag(ev)}
            ${ev.is_market_wide ? '<span class="tag market-wide">market-wide</span>' : ""}
          </div>
        </div>
        <div class="attention-score">${ev.attention_score.toFixed(2)}<span class="label">attention</span></div>
      </div>`;
  }).join("");

  digestList.querySelectorAll(".digest-row").forEach(row => {
    row.addEventListener("click", () => openDetail(row.dataset.ticker));
  });
}

function renderWatchlist(items) {
  if (!items.length) {
    watchlistEl.innerHTML = `<div class="empty-state">Nothing watched yet. Add a ticker below — AAPL, MSFT, NVDA, TSLA, AMZN, JPM, XOM, PFE, NFLX and DIS are already live in the simulated feed.</div>`;
    return;
  }
  watchlistEl.innerHTML = items.map(item => `
    <div class="watchlist-row" data-ticker="${item.ticker}">
      <div class="wl-ticker">${item.ticker}<span class="name">${item.name}</span></div>
      <div></div>
      <div class="wl-price">${item.price != null ? "$" + item.price.toFixed(2) : "—"}
        <div class="wl-stale ${item.staleness_seconds != null && item.staleness_seconds > 60 ? "warn" : ""}">${fmtStale(item.staleness_seconds)}</div>
      </div>
      <div class="dot ${item.has_unseen_change ? "unread" : ""}" title="${item.has_unseen_change ? item.unseen_count + " unread change(s)" : "up to date"}"></div>
      <button class="remove-btn" title="Remove" data-remove="${item.ticker}">✕</button>
    </div>
  `).join("");

  watchlistEl.querySelectorAll(".watchlist-row").forEach(row => {
    row.addEventListener("click", (e) => {
      if (e.target.dataset.remove) return;
      openDetail(row.dataset.ticker);
    });
  });
  watchlistEl.querySelectorAll("[data-remove]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/watchlist/${btn.dataset.remove}`, { method: "DELETE" });
      refreshAll();
    });
  });
}

async function refreshAll() {
  const [wl, digest, symbols] = await Promise.all([
    api("/api/watchlist"),
    api("/api/digest"),
    api("/api/symbols"),
  ]);
  cacheBadge.textContent = `fan-out: ${wl.cache_backend}`;
  renderWatchlist(wl.items);
  renderDigest(digest);
  suggestions.innerHTML = symbols.map(s => `<option value="${s.ticker}">${s.name}</option>`).join("");
}

addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const ticker = addInput.value.trim();
  if (!ticker) return;
  addInput.value = "";
  await api("/api/watchlist", { method: "POST", body: JSON.stringify({ ticker }) });
  refreshAll();
});

markAllSeenBtn.addEventListener("click", async () => {
  await api("/api/digest/seen-all", { method: "POST" });
  refreshAll();
});

usernameInput.addEventListener("change", () => {
  state.username = usernameInput.value.trim().toLowerCase() || "demo";
  localStorage.setItem("watchlist_user", state.username);
  connectSSE();
  refreshAll();
});

// ---- detail modal ----
async function openDetail(ticker) {
  const data = await api(`/api/symbols/${ticker}/history`);
  document.getElementById("modal-ticker").textContent = ticker;
  drawChart(data.prices);
  const eventsEl = document.getElementById("modal-events");
  eventsEl.innerHTML = data.events.length
    ? data.events.map(ev => `
        <div class="modal-event">
          <div>${fmtPct(ev.pct_change)} · attention ${ev.attention_score.toFixed(2)} ${ev.is_market_wide ? "(market-wide)" : ""}</div>
          <div class="meta">${new Date(ev.created_at).toLocaleTimeString()} · z ${ev.z_score.toFixed(2)} · ${ev.attribution_kind}${ev.attribution_detail ? ": " + ev.attribution_detail : ""}</div>
        </div>`).join("")
    : `<div class="empty-state">No significant moves recorded yet for ${ticker}.</div>`;

  await api(`/api/watchlist/${ticker}/seen`, { method: "POST" });
  modal.showModal();
  refreshAll();
}

closeModalBtn.addEventListener("click", () => modal.close());
modal.addEventListener("click", (e) => { if (e.target === modal) modal.close(); });

function drawChart(prices) {
  const canvas = document.getElementById("modal-chart");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (prices.length < 2) {
    ctx.fillStyle = "#728077";
    ctx.font = "13px monospace";
    ctx.fillText("Not enough data yet", 12, h / 2);
    return;
  }
  const values = prices.map(p => p.price);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = 16;
  const span = (max - min) || 1;

  ctx.strokeStyle = "#57b6c2";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / span) * (h - pad * 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = "#728077";
  ctx.font = "11px monospace";
  ctx.fillText(`$${max.toFixed(2)}`, 2, 12);
  ctx.fillText(`$${min.toFixed(2)}`, 2, h - 4);
}

// ---- live updates via SSE ----
let sse = null;
function connectSSE() {
  if (sse) sse.close();
  // EventSource can't send custom headers, so this endpoint accepts the
  // username as a query param instead (see get_current_user on the server).
  sse = new EventSource(`/api/events?user=${encodeURIComponent(state.username)}`);
  sse.onmessage = () => refreshAll(); // a symbol went dirty -> pull the fresh state
  sse.onerror = () => { /* browser auto-reconnects; the 5s poll covers the gap */ };
}
connectSSE();

refreshAll();
setInterval(refreshAll, 5000); // simple, robust fallback: poll every 5s
