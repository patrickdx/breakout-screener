# Breakout Screener

Daily screener for global stocks trading near their 52-week high.
Universe: every primary-listed common stock with **market cap > $2B** across
**~45 markets** (US, Europe, Japan, China, India, …).

**Dashboard:** https://patrickdx.github.io/breakout-screener/

## How it works

Every weekday at 21:00 UTC (after the US close — Asia and Europe closed hours
earlier, so it's one clean global end-of-day snapshot) a GitHub Actions job
runs [`screener.py`](screener.py):

1. **One request** to TradingView's public scanner returns the whole filtered
   universe — price, 52-week high, relative volume, sector, industry, market
   cap (USD), country, currency, logo. No API key, no per-ticker downloads.
2. Each stock is classified:
   - **Breakout** — within **0.5%** of its 52-week high **and** volume > **1.2×**
     its 10-day average.
   - **Near Breakout** — within **5%** of the high.
3. Streaks are computed from the stored history (see below), results are
   written to `docs/data.json`, archived to `docs/runs/<date>.json`, and
   appended to `data/history.csv`; the job commits everything. GitHub Pages
   serves [`docs/`](docs/) as the dashboard.

**The repo is the data store.** No database, no server, no secrets. The
dashboard's date picker cycles through the archived runs (one per trading
day, kept for the last `ARCHIVE_MAX_RUNS` = 120 runs). Clicking a row opens
a detail panel: streak stats, price performance (1W–1Y), an appearance-trail chart fed by
`docs/trails.json`, and TradingView's embedded price chart, company profile
and technicals gauge. Panels are deep-linkable (`?t=NASDAQ:AAPL`); the ↗
column jumps straight to TradingView.

## Streaks

`Breakout Streak` is real memory across runs, read back from
`data/history.csv`:

- **Continuity** is counted in consecutive *runs* — a skipped run (CI outage)
  doesn't reset a streak.
- **Length** is counted in distinct exchange *sessions* — a run that re-serves
  stale data (US holiday, same-day re-run) can never inflate a streak.
- Re-running the job on the same day replaces that day's rows (idempotent).

`Days on screen` is the same idea for appearing on either list; `streak_start`
is the date the current breakout streak began. On the very first run every
streak is 1; it accrues from there. History is pruned beyond
`HISTORY_MAX_RUNS` (500) run dates.

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
python screener.py          # writes data/history.csv + docs/data.json
python -m http.server -d docs 8000   # view at http://localhost:8000
```

Tests (pure logic, no network): `pip install pytest && pytest -q`.

## Tuning

Constants at the top of [`screener.py`](screener.py): `BREAKOUT_PCT`,
`PROXIMITY_PCT`, `VOLUME_THRESHOLD`, `MIN_MARKET_CAP`, `MARKETS`.

Adding a dashboard column: add the field to `FIELDS` in `screener.py`, carry
it through `classify()`, and add one entry to `COLUMNS` in
[`docs/index.html`](docs/index.html).

Not investment advice.
