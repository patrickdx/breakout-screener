"""52-week high breakout screener for Nasdaq common stock.

Every run pulls a year of daily bars, classifies each stock as a Breakout or
Near-Breakout for today, and writes the result to a Google Sheet. The sheet is
also the data store: a History tab keeps an append-only, dated log of every run.

Momentum ("Breakout Streak") is computed from those stored rows -- the number of
consecutive prior runs a symbol has appeared on the Breakouts list, plus today.
So the streak is real memory across runs, not a same-day guess. On the first
ever run History is empty and every streak is 1; it builds from there.

Tabs written:
  - Summary:        run metadata (universe, last run time, counts)
  - Breakouts:      today's breakouts, sorted by streak then distance to high
  - Near Breakouts: today's near-breakouts, sorted by distance to high
  - History:        append-only dated log feeding the streak calculation

Result columns: Symbol, Price, 52-Week High, Distance to High (%), Volume Ratio,
Breakout Streak, Days Near High, Streak Start, Sector, Market Cap,
Daily Change (%), Link.
"""
import datetime
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

import pandas as pd
import yfinance as yf
from tqdm import tqdm

SOFT_BREAKOUT_PCT = 0.005
PROXIMITY_THRESHOLD = 0.05
VOLUME_THRESHOLD = 1.2
LOOKBACK_DAYS = 365
BATCH_SIZE = 50
SLEEP_BETWEEN = 2.0
SECTOR_WORKERS = 8

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Full column order the script owns end-to-end (it owns the whole sheet).
RESULT_COLUMNS = [
    "Symbol", "Price", "52-Week High", "Distance to High (%)", "Volume Ratio",
    "Breakout Streak", "Days Near High", "Streak Start",
    "Sector", "Market Cap", "Daily Change (%)", "Link",
]
# Columns produced directly from the price panel (streaks + meta added after).
PRICE_COLUMNS = [
    "Symbol", "Price", "52-Week High", "Distance to High (%)",
    "Volume Ratio", "Daily Change (%)", "Link",
]
# Streak columns, derived from the stored History rows.
STREAK_COLUMNS = ("Breakout Streak", "Days Near High", "Streak Start")
# Columns derived from yf.Ticker().info (resolved once, threaded).
META_COLUMNS = ("Sector", "Market Cap")
# History tab layout — the dated log the streak calculation reads back.
HISTORY_COLUMNS = [
    "Date", "List", "Symbol", "Price", "52-Week High", "Distance to High (%)",
    "Volume Ratio", "Breakout Streak", "Daily Change (%)", "Market Cap", "Sector",
]

# Link cell is a Google Sheets HYPERLINK formula. Swap the base if you prefer
# TradingView / Finviz / etc.
LINK_BASE = "https://finance.yahoo.com/quote/{symbol}"

# Security Name patterns that indicate non-common-stock instruments (warrants,
# rights, SPAC units/Class A, preferreds, debt notes, preferred-wrapper ADRs).
NON_COMMON_STOCK_PATTERNS = (
    "warrant",
    "rights",
    " - unit",
    "preferred",
    "notes due",
    "depositary shares representing",
    "class a ordinary share",
)


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def load_nasdaq() -> list[str]:
    req = urllib.request.Request(NASDAQ_URL, headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    df = pd.read_csv(StringIO(text), sep="|")
    df = df[df["Symbol"].notna() & ~df["Symbol"].str.startswith("File Creation Time", na=False)]
    df = df[df["Test Issue"] == "N"]
    df = df[df["ETF"] == "N"]
    name_lower = df["Security Name"].str.lower()
    mask = pd.Series(False, index=df.index)
    for pat in NON_COMMON_STOCK_PATTERNS:
        mask |= name_lower.str.contains(pat, na=False, regex=False)
    df = df[~mask]
    return df["Symbol"].tolist()


def download_all(tickers: list[str], start, end) -> tuple[dict[str, pd.DataFrame], list[str]]:
    all_data: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    for i in tqdm(range(0, len(tickers), BATCH_SIZE), desc="Downloading batches"):
        batch = tickers[i:i + BATCH_SIZE]
        try:
            data = yf.download(
                batch, start=start, end=end,
                group_by="ticker", auto_adjust=True, progress=False, threads=True,
            )
        except Exception as e:
            print(f"  batch failed: {e}", file=sys.stderr)
            failed.extend(batch)
            continue
        for t in batch:
            if t in data.columns.get_level_values(0):
                all_data[t] = data[t]
            else:
                failed.append(t)
        time.sleep(SLEEP_BETWEEN)
    return all_data, failed


def compute_signals(raw: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Classify today's Breakouts / Near-Breakouts with price-derived columns.

    Streak and metadata columns are added later; this stage is pure price math.
    """
    tickers = list(raw.keys())
    data = pd.concat(raw, axis=1)

    close = pd.DataFrame({t: data[(t, "Close")] for t in tickers})
    volume = pd.DataFrame({t: data[(t, "Volume")] for t in tickers})
    avg_vol_50 = volume.rolling(50).mean()
    rolling_high = close.rolling(LOOKBACK_DAYS, min_periods=1).max()

    price = close.iloc[-1]
    prev = close.iloc[-2]
    high = rolling_high.iloc[-1]
    vr_today = volume.iloc[-1] / avg_vol_50.iloc[-1]
    dist_today = (high - price) / high
    valid = high.notna() & price.notna() & (high > 0)

    breakout_mask = (dist_today <= SOFT_BREAKOUT_PCT) & (vr_today > VOLUME_THRESHOLD)
    near_mask = (dist_today <= PROXIMITY_THRESHOLD) & (~breakout_mask)

    def daily_change(t: str):
        p0 = prev[t]
        if pd.isna(p0) or p0 == 0:
            return None
        return round((float(price[t]) / float(p0) - 1) * 100, 2)

    def row(t: str) -> dict:
        return {
            "Symbol": t,
            "Price": round(float(price[t]), 2),
            "52-Week High": round(float(high[t]), 2),
            "Distance to High (%)": round(float(dist_today[t]) * 100, 2),
            "Volume Ratio": None if pd.isna(vr_today[t]) else round(float(vr_today[t]), 2),
            "Daily Change (%)": daily_change(t),
            "Link": f'=HYPERLINK("{LINK_BASE.format(symbol=t)}", "{t}")',
        }

    def frame(mask: pd.Series) -> pd.DataFrame:
        rows = [row(t) for t in tickers if bool(valid.get(t, False)) and bool(mask[t])]
        return pd.DataFrame(rows, columns=PRICE_COLUMNS)

    return frame(breakout_mask), frame(near_mask)


def compute_streaks(history: pd.DataFrame, breakout_syms: set[str],
                    near_syms: set[str], run_date: str) -> dict[str, tuple[int, int, str]]:
    """Streaks from stored rows: consecutive prior runs a symbol was listed.

    Returns {symbol: (breakout_streak, days_near_high, streak_start)}.
      breakout_streak  - consecutive runs on the Breakouts list, incl. today.
      days_near_high   - consecutive runs on either list (on-screen), incl. today.
      streak_start     - date the current breakout streak began ("" if not a breakout).
    Walks the ordered prior run-dates, so a skipped run doesn't reset a streak;
    it counts consecutive *runs*, not calendar days.
    """
    have = (not history.empty
            and {"Date", "List", "Symbol"} <= set(history.columns))
    if have:
        h = history[history["Date"].astype(str) != run_date]
        prior_dates = sorted(set(h["Date"].astype(str)))
        breakout_sets = {
            d: set(h.loc[(h["Date"].astype(str) == d) & (h["List"] == "Breakout"), "Symbol"])
            for d in prior_dates
        }
        onscreen_sets = {
            d: set(h.loc[h["Date"].astype(str) == d, "Symbol"]) for d in prior_dates
        }
    else:
        prior_dates, breakout_sets, onscreen_sets = [], {}, {}

    def trailing(sets: dict[str, set[str]], sym: str) -> int:
        n = 0
        for d in reversed(prior_dates):
            if sym in sets.get(d, ()):
                n += 1
            else:
                break
        return n

    result: dict[str, tuple[int, int, str]] = {}
    for sym in breakout_syms | near_syms:
        days_near = 1 + trailing(onscreen_sets, sym)
        if sym in breakout_syms:
            bs = 1 + trailing(breakout_sets, sym)
            start = run_date if bs <= 1 else prior_dates[len(prior_dates) - (bs - 1)]
        else:
            bs, start = 0, ""
        result[sym] = (bs, days_near, start)
    return result


def add_streaks(df: pd.DataFrame, streaks: dict[str, tuple[int, int, str]]) -> pd.DataFrame:
    df = df.copy()
    for i, col in enumerate(STREAK_COLUMNS):
        df[col] = df["Symbol"].map(lambda s: streaks[s][i]) if not df.empty else []
    return df


def fetch_meta(tickers: list[str]) -> dict[str, dict]:
    """Resolve Sector + Market Cap per ticker via yfinance (threaded, one call each).

    Sector uses the granular `industry`, falling back to the broad `sector`.
    Failures are non-fatal: an unresolved ticker gets a blank sector / None cap
    so a slow or rate-limited lookup never blocks the screen.
    """
    def one(t: str) -> tuple[str, dict]:
        try:
            info = yf.Ticker(t).info
            return t, {
                "Sector": info.get("industry") or info.get("sector") or "",
                "Market Cap": info.get("marketCap"),
            }
        except Exception:
            return t, {"Sector": "", "Market Cap": None}

    meta: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=SECTOR_WORKERS) as pool:
        for t, m in tqdm(pool.map(one, tickers), total=len(tickers), desc="Meta"):
            meta[t] = m
    return meta


def add_meta(df: pd.DataFrame, meta: dict[str, dict]) -> pd.DataFrame:
    df = df.copy()
    for col in META_COLUMNS:
        default = "" if col == "Sector" else None
        df[col] = df["Symbol"].map(lambda s: meta.get(s, {}).get(col, default)) if not df.empty else []
    return df.reindex(columns=RESULT_COLUMNS)


# --- Google Sheets I/O --------------------------------------------------------

def open_sheet(sheet_id: str):
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
    return gspread.authorize(creds).open_by_key(sheet_id)


def _get_or_create(sh, title: str, rows: int = 100, cols: int = 20):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))


def read_history(sh) -> pd.DataFrame:
    """Read the History tab into a DataFrame (empty if the tab is absent/empty)."""
    import gspread
    try:
        ws = sh.worksheet("History")
    except gspread.WorksheetNotFound:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    values = ws.get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    return pd.DataFrame(values[1:], columns=values[0])


def write_results(sh, n_screened: int, breakouts: pd.DataFrame,
                  near: pd.DataFrame, last_run: str) -> None:
    from gspread_dataframe import set_with_dataframe

    def write_df(title: str, df: pd.DataFrame):
        # The script owns the whole sheet, so a full clear + one write is safe.
        # set_with_dataframe resizes the worksheet to fit the frame exactly.
        ws = _get_or_create(sh, title)
        ws.clear()
        set_with_dataframe(ws, df, include_index=False,
                           include_column_header=True, resize=True)

    summary = pd.DataFrame(
        [["Universe", "NASDAQ"], ["Last Run", last_run],
         ["Tickers Screened", n_screened], ["Breakouts", len(breakouts)],
         ["Near Breakouts", len(near)]],
        columns=["Field", "Value"])
    write_df("Summary", summary)
    write_df("Breakouts", breakouts)
    write_df("Near Breakouts", near)


def append_history(sh, breakouts: pd.DataFrame, near: pd.DataFrame, run_date: str) -> None:
    """Append today's rows to the append-only History log (feeds the streak calc)."""
    def tagged(df: pd.DataFrame, label: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        out = df.copy()
        out.insert(0, "List", label)
        out.insert(0, "Date", run_date)
        return out.reindex(columns=HISTORY_COLUMNS)

    combined = pd.concat([tagged(breakouts, "Breakout"), tagged(near, "Near")],
                         ignore_index=True)
    if combined.empty:
        return
    hist = _get_or_create(sh, "History", rows=2000, cols=len(HISTORY_COLUMNS))
    if not hist.get_all_values():
        hist.append_row(HISTORY_COLUMNS, value_input_option="RAW")
    rows = combined.where(pd.notna(combined), "").values.tolist()
    hist.append_rows(rows, value_input_option="RAW")


def main() -> int:
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("SHEET_ID env var not set", file=sys.stderr)
        return 1

    end = datetime.datetime.today()
    start = end - datetime.timedelta(days=LOOKBACK_DAYS)

    tickers = load_nasdaq()
    print(f"NASDAQ universe: {len(tickers)} tickers")

    raw, failed = download_all(tickers, start, end)
    print(f"Downloaded: {len(raw)} | failed: {len(failed)}")
    if not raw:
        print("No data downloaded.", file=sys.stderr)
        return 1

    breakouts, near = compute_signals(raw)
    print(f"Breakouts: {len(breakouts)} | Near Breakouts: {len(near)}")

    now = _now()
    run_date = now.strftime("%Y-%m-%d")
    last_run = now.strftime("%Y-%m-%d %H:%M UTC")

    # Streaks come from the stored History rows, so read the sheet first.
    sh = open_sheet(sheet_id)
    history = read_history(sh)
    streaks = compute_streaks(history, set(breakouts["Symbol"]),
                              set(near["Symbol"]), run_date)
    breakouts = add_streaks(breakouts, streaks)
    near = add_streaks(near, streaks)

    # Metadata is only needed for the result rows, not the whole universe.
    result_tickers = sorted(set(breakouts["Symbol"]) | set(near["Symbol"]))
    meta = fetch_meta(result_tickers)
    print(f"Sectors resolved: {sum(1 for m in meta.values() if m['Sector'])}/{len(result_tickers)}")
    breakouts = add_meta(breakouts, meta)
    near = add_meta(near, meta)

    breakouts = breakouts.sort_values(
        ["Breakout Streak", "Distance to High (%)"], ascending=[False, True])
    near = near.sort_values("Distance to High (%)")

    write_results(sh, len(raw), breakouts, near, last_run)
    append_history(sh, breakouts, near, run_date)
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    print(f"Wrote results to {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
