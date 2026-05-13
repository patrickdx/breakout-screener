"""52-week high breakout screener for Nasdaq common stock.

Flags stocks at or near their 52-week high and writes three tabs to a Google Sheet:
  - Summary:        run metadata (universe, last run time, counts)
  - Breakouts:      within SOFT_BREAKOUT_PCT of the high AND volume > VOLUME_THRESHOLD * 50-day avg
  - Near Breakouts: within PROXIMITY_THRESHOLD of the high (no volume requirement)
"""
import datetime
import json
import os
import sys
import time
import urllib.request
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

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
    tickers = list(raw.keys())
    data = pd.concat(raw, axis=1)

    close = pd.DataFrame({t: data[t]["Close"] for t in tickers})
    volume = {t: data[(t, "Volume")] for t in tickers}
    avg_vol_50 = {t: v.rolling(50).mean() for t, v in volume.items()}

    rolling_high = close.rolling(LOOKBACK_DAYS, min_periods=1).max()
    current_close = close.iloc[-1]
    rolling_high_today = rolling_high.iloc[-1]
    latest_volume = pd.Series({t: v.iloc[-1] for t, v in volume.items()})
    latest_avg_volume = pd.Series({t: a.iloc[-1] for t, a in avg_vol_50.items()})

    volume_ratio = latest_volume / latest_avg_volume
    proximity = (rolling_high_today - current_close) / rolling_high_today

    breakouts_mask = (proximity <= SOFT_BREAKOUT_PCT) & (volume_ratio > VOLUME_THRESHOLD)
    near_mask = (proximity <= PROXIMITY_THRESHOLD) & (~breakouts_mask) & (rolling_high_today > 0)

    def build(mask):
        return pd.DataFrame({
            "Price": current_close[mask].round(2),
            "52-Week High": rolling_high_today[mask].round(2),
            "Distance to High (%)": (proximity[mask] * 100).round(2),
            "Volume Ratio": volume_ratio[mask].round(2),
        }).dropna().sort_values("Distance to High (%)")

    return build(breakouts_mask), build(near_mask)


def write_to_sheet(sheet_id: str, n_screened: int,
                   breakouts: pd.DataFrame, near: pd.DataFrame) -> str:
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
    sh = gspread.authorize(creds).open_by_key(sheet_id)

    def get_or_create(title: str):
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows="100", cols="10")

    def write_table(title: str, df: pd.DataFrame):
        ws = get_or_create(title)
        ws.clear()
        if df.empty:
            ws.update(values=[["(none)"]], range_name="A1")
            return
        rows = [["Symbol"] + df.columns.tolist()]
        rows.extend([[idx] + list(row) for idx, row in df.iterrows()])
        ws.update(values=rows, range_name="A1")

    last_run = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    summary_rows = [
        ["Field", "Value"],
        ["Universe", "NASDAQ"],
        ["Last Run", last_run],
        ["Tickers Screened", n_screened],
        ["Breakouts", len(breakouts)],
        ["Near Breakouts", len(near)],
    ]
    ws = get_or_create("Summary")
    ws.clear()
    ws.update(values=summary_rows, range_name="A1")

    write_table("Breakouts", breakouts)
    write_table("Near Breakouts", near)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


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

    url = write_to_sheet(sheet_id, len(raw), breakouts, near)
    print(f"Wrote results to {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
