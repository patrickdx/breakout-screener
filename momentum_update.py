"""52-week high breakout screener for Nasdaq common stock.

Flags stocks at or near their 52-week high and renders a static dashboard
(site/index.html, published via GitHub Pages):
  - Summary:        run metadata (universe, last run time, counts)
  - Breakouts:      within SOFT_BREAKOUT_PCT of the high AND volume > VOLUME_THRESHOLD * 50-day avg
  - Near Breakouts: within PROXIMITY_THRESHOLD of the high (no volume requirement)
Each table is enriched with ApeWisdom Reddit mention buzz.
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

# Social buzz (Reddit mention volume) via ApeWisdom. Free, no auth. The list is
# ranked by mention count and paginated 100/page; we pull the whole list and join
# it to the breakout tickers. Tickers absent from the list simply get 0 mentions.
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"
APEWISDOM_MAX_PAGES = 15

# Columns rendered in each results table, in order. Index (Symbol) is prepended.
TABLE_COLUMNS = ["Symbol", "Price", "52-Week High", "Distance to High (%)",
                 "Volume Ratio", "Mentions", "Mentions 24h Δ%"]

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


def fetch_social_buzz() -> dict[str, dict]:
    """Pull the full ApeWisdom mention-ranked list into {ticker: {...}}.

    Each value holds mentions, upvotes, rank, and 24h-ago counts so callers can
    compute mention momentum. Failures are non-fatal: returns whatever pages
    succeeded (possibly empty) so a buzz outage never blocks the screen.
    """
    buzz: dict[str, dict] = {}
    for page in range(1, APEWISDOM_MAX_PAGES + 1):
        url = APEWISDOM_URL.format(page=page)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            payload = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
        except Exception as e:
            print(f"  ApeWisdom page {page} failed: {e}", file=sys.stderr)
            break
        results = payload.get("results", [])
        if not results:
            break
        for r in results:
            buzz[r["ticker"].upper()] = r
        if page >= payload.get("pages", page):
            break
        time.sleep(0.5)
    return buzz


def add_social_columns(df: pd.DataFrame, buzz: dict[str, dict]) -> pd.DataFrame:
    """Append Mentions and 24h mention-change columns, joined by ticker (index)."""
    if df.empty:
        return df
    mentions, momentum = [], []
    for sym in df.index:
        row = buzz.get(str(sym).upper())
        if not row:
            mentions.append(0)
            momentum.append("")
            continue
        now = row.get("mentions", 0)
        prior = row.get("mentions_24h_ago", 0) or 0
        mentions.append(now)
        momentum.append(round((now - prior) / max(prior, 1) * 100, 1) if prior else "")
    df = df.copy()
    df["Mentions"] = mentions
    df["Mentions 24h Δ%"] = momentum
    return df


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


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NASDAQ Breakout Screener</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridjs/dist/theme/mermaid.min.css">
<style>
  :root { --bg:#0e1117; --card:#161b22; --line:#30363d; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  h1 { font-size:22px; margin:0 0 4px; }
  h2 { font-size:17px; margin:32px 0 12px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .stats { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:8px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px 16px; min-width:120px; }
  .stat .n { font-size:22px; font-weight:600; }
  .stat .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .pos { color:#3fb950; } .neg { color:#f85149; }
  .gridjs-wrapper, .gridjs-table { background:var(--card); }
  table.gridjs-table td, table.gridjs-table th { color:var(--fg); }
  .gridjs-th { background:var(--card); }
  footer { margin-top:32px; color:var(--muted); font-size:12px; }
</style>
</head>
<body>
  <h1>NASDAQ 52-Week High Breakout Screener</h1>
  <div class="sub">Last run: {{LAST_RUN}}</div>
  <div class="stats">
    <div class="stat"><div class="n">{{N_SCREENED}}</div><div class="l">Screened</div></div>
    <div class="stat"><div class="n">{{N_BREAKOUTS}}</div><div class="l">Breakouts</div></div>
    <div class="stat"><div class="n">{{N_NEAR}}</div><div class="l">Near Breakouts</div></div>
  </div>

  <h2>Breakouts <span class="sub">&mdash; at the high on elevated volume</span></h2>
  <div id="breakouts"></div>

  <h2>Near Breakouts <span class="sub">&mdash; within range, no volume filter</span></h2>
  <div id="near"></div>

  <footer>Data: Yahoo Finance (prices) &middot; ApeWisdom (Reddit mentions). Symbols link to Finviz.</footer>

<script src="https://cdn.jsdelivr.net/npm/gridjs/dist/gridjs.umd.js"></script>
<script>
  const BREAKOUTS = {{BREAKOUTS_JSON}};
  const NEAR = {{NEAR_JSON}};

  const symbolCol = {
    name: "Symbol",
    formatter: (c) => gridjs.html(
      `<a href="https://finviz.com/quote.ashx?t=${c}" target="_blank" rel="noopener">${c}</a>`)
  };
  const momentumCol = {
    name: "Mentions 24h \\u0394%",
    formatter: (c) => {
      if (c === "" || c === null || c === undefined) return "";
      const cls = c > 0 ? "pos" : (c < 0 ? "neg" : "");
      const sign = c > 0 ? "+" : "";
      return gridjs.html(`<span class="${cls}">${sign}${c}%</span>`);
    }
  };
  const columns = [symbolCol, "Price", "52-Week High", "Distance to High (%)",
                   "Volume Ratio", "Mentions", momentumCol];

  function render(id, data) {
    new gridjs.Grid({
      columns: columns,
      data: data,
      search: true,
      sort: true,
      pagination: { limit: 25 },
      language: { noRecordsFound: "No matches" },
      style: { table: { "font-size": "14px" } }
    }).render(document.getElementById(id));
  }
  render("breakouts", BREAKOUTS);
  render("near", NEAR);
</script>
</body>
</html>
"""


def _records(df: pd.DataFrame) -> list[list]:
    """Turn a results frame into a JSON-safe 2D array in TABLE_COLUMNS order."""
    import math

    def cell(v):
        if isinstance(v, str):
            return v
        try:
            f = float(v)
            return None if math.isnan(f) else f
        except (TypeError, ValueError):
            return v

    if df.empty:
        return []
    rows = []
    for idx, row in df.iterrows():
        rows.append([str(idx)] + [cell(v) for v in row.tolist()])
    return rows


def write_dashboard(out_dir: str, n_screened: int,
                    breakouts: pd.DataFrame, near: pd.DataFrame) -> str:
    """Render a self-contained static dashboard to out_dir/index.html.

    Sortable/searchable tables are powered by Grid.js loaded from a CDN; the
    screener data is embedded inline as JSON so the page is fully static.
    """
    os.makedirs(out_dir, exist_ok=True)
    last_run = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    def dump(df):
        # Escape "</" so embedded JSON can't break out of the <script> context.
        return json.dumps(_records(df)).replace("</", "<\\/")

    html = (DASHBOARD_TEMPLATE
            .replace("{{LAST_RUN}}", last_run)
            .replace("{{N_SCREENED}}", str(n_screened))
            .replace("{{N_BREAKOUTS}}", str(len(breakouts)))
            .replace("{{N_NEAR}}", str(len(near)))
            .replace("{{BREAKOUTS_JSON}}", dump(breakouts))
            .replace("{{NEAR_JSON}}", dump(near)))

    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def main() -> int:
    out_dir = os.environ.get("OUTPUT_DIR", "site")

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

    buzz = fetch_social_buzz()
    print(f"Social buzz: {len(buzz)} tickers from ApeWisdom")
    breakouts = add_social_columns(breakouts, buzz)
    near = add_social_columns(near, buzz)

    path = write_dashboard(out_dir, len(raw), breakouts, near)
    print(f"Wrote dashboard to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
