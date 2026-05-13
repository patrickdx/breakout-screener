"""52-week high breakout screener.

Flags stocks that are at or near their 52-week high. Two buckets:
  - Breakouts:      within --soft-breakout of the high AND volume > --volume-threshold * 50-day avg
  - Near breakouts: within --proximity of the high (no volume requirement)

Outputs an .xlsx with one sheet per bucket.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
from io import StringIO
from pathlib import Path

import pandas as pd
import yfinance as yf
from tqdm import tqdm

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
HANGSENG_URL = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
TICKER_COL_CANDIDATES = ("Symbol", "Ticker", "Ticker Symbol")


def load_sp500() -> list[str]:
    req = urllib.request.Request(SP500_URL, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req).read().decode("utf-8")
    df = pd.read_html(StringIO(html))[0]
    return df["Symbol"].tolist()


# Patterns in Security Name that indicate non-common-stock instruments we want
# to exclude from the screener (warrants, rights, SPAC units/Class A shares,
# preferred shares, debt notes, preferred-wrapper depositary shares).
NON_COMMON_STOCK_PATTERNS = (
    "warrant",
    "rights",
    " - unit",  # matches "- Unit" and "- Units"
    "preferred",
    "notes due",
    "depositary shares representing",
    "class a ordinary share",  # SPAC pattern, matches singular + plural
)


def load_nasdaq(include_etfs: bool = False, include_non_common: bool = False) -> list[str]:
    """Fetch all Nasdaq-listed symbols from the official Nasdaq Trader feed.

    Filters out test issues and the trailing 'File Creation Time' footer line.
    By default excludes ETFs and non-common-stock instruments (SPACs, preferreds,
    warrants, rights, units, debt notes) so the screener focuses on momentum
    in actual common stock.
    """
    req = urllib.request.Request(NASDAQ_URL, headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    df = pd.read_csv(StringIO(text), sep="|")
    df = df[df["Symbol"].notna() & ~df["Symbol"].str.startswith("File Creation Time", na=False)]
    df = df[df["Test Issue"] == "N"]
    if not include_etfs:
        df = df[df["ETF"] == "N"]
    if not include_non_common:
        name_lower = df["Security Name"].str.lower()
        mask = pd.Series(False, index=df.index)
        for pat in NON_COMMON_STOCK_PATTERNS:
            mask |= name_lower.str.contains(pat, na=False, regex=False)
        df = df[~mask]
    return df["Symbol"].tolist()


def load_hangseng() -> list[str]:
    """Fetch current Hang Seng Index constituents from Wikipedia.

    Wikipedia formats tickers as 'SEHK: XXXX'. We extract the numeric code,
    zero-pad to 4 digits, and append the '.HK' suffix that yfinance requires.
    """
    req = urllib.request.Request(HANGSENG_URL, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req).read().decode("utf-8")
    tables = pd.read_html(StringIO(html))
    constituents = next(
        t for t in tables
        if "Ticker" in t.columns and t["Ticker"].astype(str).str.contains("SEHK", na=False).any()
    )
    codes = constituents["Ticker"].astype(str).str.extract(r"SEHK:?\s*(\d+)", expand=False).dropna()
    return [f"{int(c):04d}.HK" for c in codes]


def load_csv(path: Path) -> list[str]:
    df = pd.read_csv(path)
    col = next((c for c in TICKER_COL_CANDIDATES if c in df.columns), None)
    if col is None:
        raise ValueError(
            f"No ticker column in {path}. Expected one of {TICKER_COL_CANDIDATES}, got {df.columns.tolist()}"
        )
    return [t.strip().upper() for t in df[col].dropna().tolist()]


def normalize_tickers(tickers: list[str]) -> list[str]:
    # yfinance uses '-' instead of '.' for US class shares (BRK.B -> BRK-B),
    # but preserves the '.HK' exchange suffix on Hong Kong tickers (0700.HK).
    return [t if t.endswith(".HK") else t.replace(".", "-") for t in tickers]


def download_batch_with_retry(tickers, start, end, retries=3, sleep_time=2):
    for attempt in range(1, retries + 1):
        try:
            return yf.download(
                tickers, start=start, end=end,
                group_by="ticker", auto_adjust=True, progress=False, threads=True,
            )
        except Exception as e:
            print(f"  batch download failed (attempt {attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(sleep_time)
    return None


def download_all(tickers: list[str], start, end, batch_size: int, sleep_between: float):
    all_data: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    for i in tqdm(range(0, len(tickers), batch_size), desc="Downloading batches"):
        batch = tickers[i:i + batch_size]
        tqdm.write(f"  batch {i // batch_size + 1}: {batch[0]}..{batch[-1]}")
        data = download_batch_with_retry(batch, start, end)
        if data is None or data.empty:
            failed.extend(batch)
        else:
            for t in batch:
                if t in data.columns.get_level_values(0):
                    all_data[t] = data[t]
                else:
                    failed.append(t)
        time.sleep(sleep_between)
    return all_data, failed


def compute_signals(raw: dict[str, pd.DataFrame], lookback_days: int,
                    soft_breakout_pct: float, proximity_threshold: float, volume_threshold: float):
    tickers = list(raw.keys())
    data = pd.concat(raw, axis=1)

    close = pd.DataFrame({t: data[t]["Close"] for t in tickers})
    volume = {t: data[(t, "Volume")] for t in tickers}
    avg_vol_50 = {t: v.rolling(50).mean() for t, v in volume.items()}

    # Rolling highest CLOSE (not intraday high): ignores stale single-bar wicks
    # and flags every new closing high as a breakout candidate.
    rolling_high = close.rolling(lookback_days, min_periods=1).max()

    current_close = close.iloc[-1]
    prev_close = close.iloc[-2]
    rolling_high_today = rolling_high.iloc[-1]
    latest_volume = pd.Series({t: v.iloc[-1] for t, v in volume.items()})
    latest_avg_volume = pd.Series({t: a.iloc[-1] for t, a in avg_vol_50.items()})

    volume_ratio = latest_volume / latest_avg_volume
    proximity = (rolling_high_today - current_close) / rolling_high_today
    day_change = (current_close - prev_close) / prev_close * 100

    breakouts_mask = (proximity <= soft_breakout_pct) & (volume_ratio > volume_threshold)
    near_mask = (proximity <= proximity_threshold) & (~breakouts_mask) & (rolling_high_today > 0)

    def build(mask):
        return pd.DataFrame({
            "Price": current_close[mask],
            "Day Change (%)": day_change[mask].round(2),
            "52-Week High": rolling_high_today[mask],
            "Distance to High (%)": (proximity[mask] * 100).round(2),
            "Volume Ratio": volume_ratio[mask],
        }).dropna().sort_values(by="Distance to High (%)")

    return build(breakouts_mask), build(near_mask)


def _format_market_cap(mc) -> str:
    if mc is None or (isinstance(mc, float) and pd.isna(mc)):
        return ""
    try:
        mc = float(mc)
    except (TypeError, ValueError):
        return ""
    if mc >= 1e12:
        return f"{mc / 1e12:.2f}T"
    if mc >= 1e9:
        return f"{mc / 1e9:.2f}B"
    if mc >= 1e6:
        return f"{mc / 1e6:.0f}M"
    return f"{mc:,.0f}"


def enrich_with_metadata(df: pd.DataFrame, max_workers: int = 10) -> tuple[pd.DataFrame, dict[str, str]]:
    """Add 'Sector' and 'Market Cap' columns by querying yfinance Ticker.info per symbol.

    Hits Yahoo's quoteSummary endpoint once per ticker, parallelized with threads.
    Silent fallback to "" for missing/flaky metadata responses.

    Returns the enriched DataFrame and a {symbol: yfinance exchange code} dict
    (used downstream to build Google Finance hyperlinks).
    """
    if df.empty:
        df = df.copy()
        df["Sector"] = pd.Series(dtype=object)
        df["Market Cap"] = pd.Series(dtype=object)
        return df, {}

    from concurrent.futures import ThreadPoolExecutor
    from curl_cffi import requests as curl_requests

    # Browser-impersonating session bypasses Yahoo's bot-detection on cloud
    # IPs (e.g. GitHub Actions runners), which otherwise returns empty .info.
    session = curl_requests.Session(impersonate="chrome")

    symbols = df.index.tolist()

    # Pre-warm sequentially: Yahoo's first quoteSummary call from a cold session
    # often hits 401 Invalid Crumb. Letting one call go first lets yfinance's
    # internal retry establish a valid crumb cookie before N workers race for it.
    try:
        yf.Ticker(symbols[0], session=session).info
    except Exception:
        pass

    def fetch(sym):
        for attempt in range(2):
            try:
                info = yf.Ticker(sym, session=session).info or {}
                sector = info.get("sector") or ""
                mc = info.get("marketCap")
                exc = info.get("exchange") or ""
                if sector or mc is not None or exc:
                    return sym, sector, mc, exc
            except Exception:
                pass
        return sym, "", None, ""

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(tqdm(ex.map(fetch, symbols), total=len(symbols), desc="Fetching metadata"))

    missing = sum(1 for _, sec, mc, _ in results if not sec and mc is None)
    if missing:
        print(f"[enrich_with_metadata] {missing}/{len(results)} symbols returned no metadata", file=sys.stderr)

    sectors = {s: sec for s, sec, _, _ in results}
    mcaps = {s: _format_market_cap(mc) for s, _, mc, _ in results}
    exchanges = {s: exc for s, _, _, exc in results}

    out = df.copy()
    out.insert(0, "Sector", out.index.map(sectors))
    out.insert(1, "Market Cap", out.index.map(mcaps))
    return out, exchanges


# Yahoo Finance "exchange" code → Google Finance exchange suffix
YF_TO_GOOGLE_EXCHANGE = {
    "NMS": "NASDAQ",   # Nasdaq Global Select
    "NGM": "NASDAQ",   # Nasdaq Global Market
    "NCM": "NASDAQ",   # Nasdaq Capital Market
    "NYQ": "NYSE",
    "ASE": "NYSEAMERICAN",  # NYSE American (AMEX)
    "PCX": "NYSEARCA",
}


def _google_finance_hyperlink(symbol: str, yf_exchange: str = "") -> str:
    """Sheets HYPERLINK formula → Google Finance, or plain symbol if exchange unknown."""
    if symbol.endswith(".HK"):
        url = f"https://www.google.com/finance/quote/{symbol[:-3]}:HKG"
    else:
        google_ex = YF_TO_GOOGLE_EXCHANGE.get(yf_exchange)
        if not google_ex:
            return symbol
        # yfinance normalizes class shares with '-' (BRK-B); Google uses '.' (BRK.B)
        google_sym = symbol.replace("-", ".")
        url = f"https://www.google.com/finance/quote/{google_sym}:{google_ex}"
    return f'=HYPERLINK("{url}","{symbol}")'


def slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def export_xlsx(out_dir: Path, universe_name: str, breakouts: pd.DataFrame, near: pd.DataFrame) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.today().strftime("%Y-%m-%d")
    path = out_dir / f"{slugify(universe_name)}_Breakout_Screener_{today}.xlsx"
    with pd.ExcelWriter(path) as writer:
        breakouts.to_excel(writer, sheet_name="Breakouts")
        near.to_excel(writer, sheet_name="Near Breakouts")
    return path


def _load_service_account_creds(sa_json_path: str | None):
    """Resolve credentials from --sa-json path, GOOGLE_SERVICE_ACCOUNT_JSON env (raw JSON),
    or GOOGLE_APPLICATION_CREDENTIALS env (path to JSON file)."""
    from google.oauth2.service_account import Credentials

    if sa_json_path:
        return Credentials.from_service_account_file(sa_json_path, scopes=GOOGLE_SCOPES)
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return Credentials.from_service_account_file(path, scopes=GOOGLE_SCOPES)
    raise RuntimeError(
        "No Google service-account credentials found. "
        "Pass --sa-json <file>, or set GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON) "
        "or GOOGLE_APPLICATION_CREDENTIALS (file path)."
    )


def write_to_google_sheet(sheet_id: str, universe_name: str, n_screened: int,
                          breakouts: pd.DataFrame, near: pd.DataFrame,
                          sa_json_path: str | None = None,
                          tab_prefix: str = "",
                          exchanges: dict[str, str] | None = None) -> str:
    import gspread

    creds = _load_service_account_creds(sa_json_path)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    exchanges = exchanges or {}

    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    summary = pd.DataFrame({
        "Field": ["Universe", "Last Run", "Tickers Screened", "Breakouts", "Near Breakouts"],
        "Value": [universe_name, today_str, n_screened, len(breakouts), len(near)],
    })

    def _df_with_symbol(df):
        out = df.reset_index().rename(columns={df.index.name or "index": "Symbol"})
        # Drop columns the Sheet computes live via GOOGLEFINANCE so users can
        # add their own formulas (price, marketcap, AI()) without our writes
        # clobbering them.
        out = out.drop(columns=[c for c in ("Price", "Market Cap") if c in out.columns])
        # Round numerics for cleaner Sheet display
        for c in ("52-Week High", "Volume Ratio", "Day Change (%)"):
            if c in out.columns:
                out[c] = out[c].round(2)
        # Wrap each ticker in a HYPERLINK to its Google Finance page.
        out["Symbol"] = out["Symbol"].apply(
            lambda s: _google_finance_hyperlink(s, exchanges.get(s, ""))
        )
        return out

    breakouts_out = _df_with_symbol(breakouts)
    near_out = _df_with_symbol(near)

    def get_or_create_ws(title, rows, cols):
        # No ws.clear() — write_df handles range-bound updates so user
        # formulas and formatting in adjacent columns are preserved.
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows=str(max(rows, 100)), cols=str(max(cols, 10)))

    def write_df(ws, df):
        # Read column A's current length so we know how many trailing rows
        # used to have data; we'll blank those (in our column range only)
        # after writing fresh data, leaving user columns to the right intact.
        prev_rows = len(ws.col_values(1))

        if df.empty:
            rows = [["(none)"]]
        else:
            rows = [df.columns.tolist()] + df.astype(object).where(df.notna(), "").values.tolist()

        n_rows = len(rows)
        n_cols = len(rows[0])
        last_col = chr(ord("A") + n_cols - 1)
        # USER_ENTERED so the HYPERLINK() formulas in the Symbol column render as links.
        ws.update(values=rows, range_name=f"A1:{last_col}{n_rows}",
                  value_input_option="USER_ENTERED")

        if prev_rows > n_rows:
            ws.batch_clear([f"A{n_rows + 1}:{last_col}{prev_rows}"])

    write_df(get_or_create_ws(f"{tab_prefix}Summary", 10, 2), summary)
    write_df(get_or_create_ws(f"{tab_prefix}Breakouts", len(breakouts_out) + 1, 5), breakouts_out)
    write_df(get_or_create_ws(f"{tab_prefix}Near Breakouts", len(near_out) + 1, 5), near_out)

    # Drop the auto-created empty default tab if present
    try:
        sh.del_worksheet(sh.worksheet("Sheet1"))
    except gspread.WorksheetNotFound:
        pass

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


def print_summary(universe_name: str, breakouts: pd.DataFrame, near: pd.DataFrame, out_path: Path | None):
    today = datetime.datetime.today().strftime("%Y-%m-%d")
    print(f"\n=== Summary ===")
    print(f"Universe: {universe_name} | Date: {today}")
    print(f"Breakouts:      {len(breakouts)}")
    print(f"Near breakouts: {len(near)}")
    if out_path:
        print(f"Saved: {out_path}\n")
    else:
        print()

    desired = ["Sector", "Market Cap", "Price", "Day Change (%)", "Distance to High (%)", "Volume Ratio"]

    def _fmt(df):
        if df.empty:
            return "  none"
        cols = [c for c in desired if c in df.columns]
        out = df[cols].copy()
        for c in ("Price", "Day Change (%)", "Distance to High (%)", "Volume Ratio"):
            if c in out.columns:
                out[c] = out[c].round(2)
        return out.to_string()

    print("Breakouts:")
    print(_fmt(breakouts))
    print("\nNear breakouts:")
    print(_fmt(near))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--spy", action="store_true", help="Use S&P 500 (scraped from Wikipedia)")
    src.add_argument("--nasdaq", action="store_true", help="Use all Nasdaq-listed common stock (~5k tickers)")
    src.add_argument("--hangseng", action="store_true", help="Use Hang Seng Index constituents (~88 HK blue chips)")
    src.add_argument("--csv", type=Path, help="Path to CSV with a ticker column")

    p.add_argument("--include-etfs", action="store_true", help="With --nasdaq, include ETFs (default: excluded)")
    p.add_argument("--include-non-common", action="store_true",
                   help="With --nasdaq, include SPACs, preferreds, warrants, rights, units, notes (default: excluded)")
    p.add_argument("--name", default=None, help="Universe label used in filename (defaults to SPY/NASDAQ or CSV stem)")
    p.add_argument("--lookback-days", type=int, default=365, help="Window for rolling high (default: 365)")
    p.add_argument("--soft-breakout", type=float, default=0.005, help="Max proximity for breakout bucket (default: 0.005)")
    p.add_argument("--proximity", type=float, default=0.05, help="Max proximity for near-breakout bucket (default: 0.05)")
    p.add_argument("--volume-threshold", type=float, default=1.2, help="Min volume ratio for breakout bucket (default: 1.2)")
    p.add_argument("--batch-size", type=int, default=50, help="Tickers per yfinance batch (default: 50)")
    p.add_argument("--sleep", type=float, default=2.0, help="Seconds to sleep between batches (default: 2)")
    p.add_argument("--output-dir", type=Path, default=Path("outputs"), help="Output directory (default: ./outputs)")
    p.add_argument("--no-xlsx", action="store_true", help="Skip writing local .xlsx (useful in CI)")
    p.add_argument("--no-metadata", action="store_true",
                   help="Skip Sector/Market Cap enrichment (faster, but less context in output)")
    p.add_argument("--sheet-id", default=None,
                   help="Google Sheet ID to write results to. Sheet ID is the long string in the Sheet URL.")
    p.add_argument("--sa-json", default=None,
                   help="Path to Google service account JSON file. "
                        "Falls back to GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS env vars.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.spy:
        universe_name = args.name or "SPY"
        tickers = load_sp500()
    elif args.nasdaq:
        universe_name = args.name or "NASDAQ"
        tickers = load_nasdaq(include_etfs=args.include_etfs, include_non_common=args.include_non_common)
    elif args.hangseng:
        universe_name = args.name or "HANGSENG"
        tickers = load_hangseng()
    else:
        universe_name = args.name or args.csv.stem
        tickers = load_csv(args.csv)

    tickers = normalize_tickers(tickers)
    print(f"Universe: {universe_name} ({len(tickers)} tickers)")

    end_date = datetime.datetime.today()
    start_date = end_date - datetime.timedelta(days=args.lookback_days)

    raw, failed = download_all(tickers, start_date, end_date, args.batch_size, args.sleep)
    if not raw:
        print("No data downloaded.", file=sys.stderr)
        return 1
    print(f"\nDownloaded: {len(raw)} | failed: {len(failed)}")
    if failed:
        print(f"Failed tickers: {failed}")

    breakouts, near = compute_signals(
        raw, args.lookback_days, args.soft_breakout, args.proximity, args.volume_threshold,
    )

    exchanges: dict[str, str] = {}
    if not args.no_metadata:
        # Enrich both tables in one call so we only pay the crumb-warmup cost
        # once. Splitting into two separate calls causes the first call to fail
        # cold on cloud IPs, blanking out Sector/Market Cap for breakouts.
        n_breakouts = len(breakouts)
        combined = pd.concat([breakouts, near]) if not near.empty else breakouts
        combined, exchanges = enrich_with_metadata(combined)
        breakouts = combined.iloc[:n_breakouts]
        near = combined.iloc[n_breakouts:]

    out_path = None
    if not args.no_xlsx:
        out_path = export_xlsx(args.output_dir, universe_name, breakouts, near)
    print_summary(universe_name, breakouts, near, out_path)

    if args.sheet_id:
        tab_prefix = "HK " if args.hangseng else ""
        url = write_to_google_sheet(
            sheet_id=args.sheet_id, universe_name=universe_name, n_screened=len(raw),
            breakouts=breakouts, near=near, sa_json_path=args.sa_json,
            tab_prefix=tab_prefix,
            exchanges=exchanges,
        )
        print(f"\nWrote results to Google Sheet: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
