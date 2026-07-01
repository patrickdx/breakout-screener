# Breakout Screener

Daily 52-week-high breakout screener for NASDAQ common stock. Results are
published to a Google Sheet.

## Architecture

Every weekday a GitHub Actions job runs [`momentum_update.py`](momentum_update.py):

1. Builds the NASDAQ common-stock universe (drops ETFs, warrants, units, preferreds, …).
2. Downloads ~1 year of daily bars per ticker via `yfinance`.
3. Classifies each stock as a Breakout or Near-Breakout for today (pure pandas).
4. Reads the **History** tab back and computes the streak from those stored rows.
5. Writes the Summary / Breakouts / Near tabs and appends today's rows to History.

**The sheet is the data store.** `Breakout Streak` is the number of *consecutive
prior runs* a symbol has appeared on the Breakouts list, plus today — read from
the History tab, not guessed from a single day. On the first ever run History is
empty and every streak is `1`; it accrues from there. Because it counts
consecutive *runs* (not calendar days), a skipped run doesn't reset it.
`Days Near High` is the same idea across either list (on-screen at all), and
`Streak Start` is the date the current breakout streak began.

Two lists:
- **Breakouts** — within 0.5% of the 52-week high **and** volume > 1.2× the 50-day average.
- **Near Breakouts** — within 5% of the high (no volume requirement).

## Sheet tabs

| Tab | Contents |
|---|---|
| Summary | Run metadata (universe, last run, counts) |
| Breakouts | Today's breakouts, sorted by streak then distance |
| Near Breakouts | Today's near-breakouts, sorted by distance |
| History | Append-only dated log of every run, for trend analysis |

Result columns: `Symbol, Price, 52-Week High, Distance to High (%), Volume Ratio,
Breakout Streak, Days Near High, Streak Start, Sector, Market Cap, Daily Change (%), Link`.
`Link` is a Google Sheets `HYPERLINK` formula (Yahoo Finance by default — change
`LINK_BASE` at the top of the script).

## Setup

The job needs two GitHub Actions secrets:
- `SHEET_ID` — the target spreadsheet's ID (the long string in its URL).
- `GOOGLE_SERVICE_ACCOUNT_JSON` — a Google service-account key (JSON). Share the
  sheet with the service account's email as an Editor.

## Run locally
```bash
pip install -r requirements.txt
export SHEET_ID=...            # spreadsheet id
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
python momentum_update.py
```

## Tuning
Thresholds are constants at the top of [`momentum_update.py`](momentum_update.py):
`SOFT_BREAKOUT_PCT`, `PROXIMITY_THRESHOLD`, `VOLUME_THRESHOLD`, `LOOKBACK_DAYS`.

Not investment advice.
