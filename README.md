# Breakout Screener

Daily screener for global stocks breaking out to new **3-month highs**. A
shorter lookback than the usual 52-week high surfaces emerging momentum weeks
earlier — a stock reclaims a recent base long before it reclaims a yearly
peak. Universe: every primary-listed common stock with **market cap > $1B**
across **46 markets** (US, Europe, Japan, China, India, …).

**Dashboard:** https://patrickdx.github.io/breakout-screener/

## How it works

Every weekday at 21:00 UTC (after the US close — Asia and Europe closed hours
earlier, so it's one clean global end-of-day snapshot) a GitHub Actions job
runs [`screener.py`](screener.py):

1. **One request** to TradingView's public scanner returns the whole filtered
   universe — price, 3-month high, relative volume, sector, industry, market
   cap (USD), country, currency, logo. No API key, no per-ticker downloads.
2. Each stock is classified:
   - **Breakout** — the close crossed **above the prior session's 3-month
     high** on volume > **1.2×** its 10-day average (the classic event
     definition: a conviction close through the old ceiling — intraday wicks
     don't count). Prior ceilings live in the `ceilings` table, written by
     the previous run for every stock within 25% of its high; a ticker with
     no stored ceiling falls back to the old state rule (within 0.5% of the
     current high on volume).
   - **Near** — within **5%** of the current 3-month high (the watchlist).

   The old state-rule signal stays derivable from history
   (`dist_pct <= 0.5 and rel_volume > 1.2`), so the definitions can be
   compared on forward returns later.
3. Streaks are computed from the stored history (see below), everything is
   written to the database, and the run then exports the JSON the dashboard
   reads (`docs/data.json`, `docs/runs/<date>.json`, `docs/trails.json`,
   `docs/performance.json`); the job commits it all. GitHub Pages serves
   [`docs/`](docs/) as the dashboard.

**The repo is the data store; the database is the backend's memory.** All
screener state lives in one SQLite file, [`data/screener.db`](data/)
(tables: `history`, `prices`, `ceilings` — see [`db.py`](db.py)), committed
by CI like any other artifact. No server, no secrets; each run's writes are
transactional, so a crashed run can't leave the stores torn. GitHub Pages
can't query a database — it only serves files — so the dashboard never
touches the `.db`. Instead each run exports small JSON views of the data
into `docs/`, and the static page fetches those. The date picker cycles
through the archived runs (one per trading day, kept for the last
`ARCHIVE_MAX_RUNS` = 120 runs). Clicking a row opens a detail panel: streak
stats, price performance (1W–1Y), a price chart built from `docs/trails.json`
(close at every stored run with each ⚡ flagged breakout marked — TradingView's
embed can't take custom markers, so this chart comes from the screener's own
run log, including price-log closes for runs the stock was off screen). It
renders with [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(TradingView's open-source canvas library, lazy-loaded from a pinned CDN
build) and falls back to a dependency-free inline SVG if the CDN is blocked.
Below it: TradingView's embedded price chart, company profile and technicals
gauge.
Panels are deep-linkable (`?t=NASDAQ:AAPL`); the ↗ column jumps straight to
TradingView. The country filter defaults to United States — pick "All
countries" for the global view.

## Streaks

`Breakout Streak` is real memory across runs, read back from the `history`
table:

- **Continuity** is counted in consecutive *runs* — a skipped run (CI outage)
  doesn't reset a streak.
- **Length** is counted in distinct exchange *sessions* — a run that re-serves
  stale data (US holiday, same-day re-run) can never inflate a streak.
- Re-running the job on the same day replaces that day's rows (idempotent).

The dashboard's Streak column shows one number on both tabs: consecutive
sessions the stock has appeared on screen — as a breakout or near the high
(`days_near`, anchored by `near_start`; NEW on its first day). The breakout-only
streak (`streak`, since `streak_start`) still drives NEW-breakout notifications
and shows in the detail panel. On the very first run every streak is 1; it
accrues from there. History is pruned beyond `HISTORY_MAX_RUNS` (500) run dates.

**3M Hits** (`hits_3m`) answers a different question: how many sessions did
the stock close as a Breakout within the trailing `ROLLING_RUNS` (63) runs —
roughly 3 months — *regardless of consecutiveness*. A streak resets the first
day a stock drops off the list; the rolling count doesn't, so a stock blowing
out its high repeatedly in bursts ranks high even between bursts. Both tabs
sort by it first (then streak) by default. Like streaks it counts distinct
exchange sessions, so stale re-served data never inflates it.

## Notifications

Set a `DISCORD_WEBHOOK_URL` repository secret (Discord: channel → Integrations
→ Webhooks → New Webhook → Copy URL) and every run posts the day's **new**
breakouts (streak = 1) — symbol, day move, volume, market cap, and an ⚠️ when
earnings are within 7 days. Monday runs append a recap of the past week's
most persistent breakouts. No secret → no post; a failed post never fails
the run.

## Extra columns

- **RS 3M** — 3-month performance minus the median of the same country's
  scanned cohort (stocks within 25% of their highs): a currency-consistent
  leader/laggard score, stored in history so the performance tracker can
  later test whether high-RS breakouts outperform.
- **⚠️ earnings** — days until the next earnings report (from TradingView's
  calendar), flagged on the dashboard when ≤ 7 days.

The dashboard is also an installable web app (manifest + icons) — "Add to
Home Screen" on a phone gives it an app icon and standalone window.

## Signal performance (benchmark-adjusted forward returns)

Every ticker that fires a Breakout stays in a daily price log (the `prices`
table) for `COHORT_RUNS` (70) runs — **even after it falls off
screen**, so failed breakouts stay measurable and the stats carry no
survivorship bias. Benchmark ETFs (QQQ, ACWI) are logged as ordinary rows.

Each run recomputes `docs/performance.json`: for
every signal old enough,
excess return = the stock's +5/+20/+60-run return **minus the benchmark's
return over the same window**, aggregated into hit rate / median / mean per
group (all breakouts, first-day signals, continuation days, and the old
state rule as a comparison — both definitions live side by side). Every
signal is accounted for: measured, pending, missing (delisted — reported,
never dropped), invalid (±40% one-day move = probable split), or
pre-tracking. The dashboard renders the table once numbers exist.

## Notes on the data

- Prices are in each listing's **local currency**; market cap is normalized to
  **USD** by TradingView. Don't sum the price column across countries.
- Cross-listings are deduped to the primary listing (`is_primary`), so TSMC
  shows up as `TWSE:2330`, not its US ADR.
- The scanner endpoint is unofficial (same risk class as yfinance was) —
  versions are pinned in `requirements.txt`; one query per day is far below
  any rate limit.
- A stale-session dot (●) on the dashboard marks rows whose exchange didn't
  trade on the run date (e.g. a local holiday).

## Run locally

```bash
pip install -r requirements.txt
python screener.py          # writes data/screener.db + docs/*.json
python -m http.server -d docs 8000   # view at http://localhost:8000
```

Poke at the database directly:

```bash
sqlite3 data/screener.db "SELECT ticker, COUNT(DISTINCT session_date) AS sessions
                          FROM history WHERE list='Breakout'
                          GROUP BY ticker ORDER BY sessions DESC LIMIT 10"
```

Tests (pure logic, no network): `pip install pytest && pytest -q`.

## Tuning

Constants at the top of [`screener.py`](screener.py): `HIGH_FIELD` (the
reference high — `High.3M`; swap for `price_52_week_high` to go back to a
yearly lookback), `BREAKOUT_PCT`, `PROXIMITY_PCT`, `VOLUME_THRESHOLD`,
`MIN_MARKET_CAP`, `MARKETS`.

Adding a dashboard column: add the field to `FIELDS` in `screener.py`, carry
it through `classify()` (it flows into the exported JSON automatically), and
add one entry to `COLUMNS` in [`docs/index.html`](docs/index.html).

Not investment advice. Made with Fable 5. $Swag
