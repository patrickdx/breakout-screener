"""Global 52-week-high breakout screener.

One TradingView scanner query pulls every primary-listed common stock above
MIN_MARKET_CAP across ~45 markets trading within UNIVERSE_PCT of its 52-week
high, pre-enriched with sector, industry, market cap (USD), country, currency
and logo. No per-ticker downloads, no API key.

Classification:
  Breakout       close crossed ABOVE the prior session's 52-week high (the
                 classic event definition: a conviction close through the old
                 ceiling, so intraday wicks don't count) with relative volume
                 > VOLUME_THRESHOLD. The prior ceiling comes from
                 data/ceilings.json, written by the previous run; tickers
                 without a stored ceiling (first run, or a >UNIVERSE_PCT gap)
                 fall back to the state rule: within BREAKOUT_PCT of the
                 current high on volume.
  Near Breakout  within PROXIMITY_PCT of the current high (watchlist state).

The old state-rule flag stays derivable from history (dist_pct <= 0.5 and
rel_volume > 1.2), so the two definitions can be compared on forward returns.

The repo is the data store:
  data/history.csv    dated log of every run; feeds the streak columns
  data/ceilings.json  per-ticker 52-week highs from the last two runs; feeds
                      the prior-ceiling comparison (two generations so a
                      same-day re-run never sees its own ceilings)
  docs/data.json      today's lists + trend series, rendered by
                      docs/index.html (GitHub Pages)

Streak semantics: continuity is counted in consecutive *runs* (a skipped run
doesn't reset a streak) but length is counted in distinct exchange *sessions*,
so a run that re-serves stale data (US holiday, same-day re-run) can never
inflate a streak. Prices are in each listing's local currency; market cap is
normalized to USD by TradingView.
"""
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

import pandas as pd
from tradingview_screener import Query, col

BREAKOUT_PCT = 0.5        # fallback state rule: % below 52w high
PROXIMITY_PCT = 5.0       # % below 52w high to stay on screen at all
UNIVERSE_PCT = 25.0       # server-side net; also the ceiling-tracking band
VOLUME_THRESHOLD = 1.2    # today's volume vs 10-day average
MIN_MARKET_CAP = 2_000_000_000   # USD
HISTORY_MAX_RUNS = 500    # prune history beyond this many run dates
ARCHIVE_MAX_RUNS = 120    # per-run JSON archives kept in docs/runs/
TREND_RUNS = 90           # runs shown in the dashboard trend chart
TRAIL_RUNS = 40           # per-ticker appearance trail for the detail panel

# --- forward-return tracking ---------------------------------------------------
# Every ticker that fired a Breakout within the last COHORT_RUNS runs keeps
# getting its close logged daily — even after it falls off screen — so failed
# breakouts stay measurable (no survivorship bias). Benchmarks ride along as
# ordinary rows; excess return = signal return minus BENCHMARK over the same
# window of runs.
BENCHMARK = 'NASDAQ:QQQ'
BENCHMARK_TICKERS = ['NASDAQ:QQQ', 'NASDAQ:ACWI']
HORIZONS = (5, 20, 60)    # forward windows, in runs (~trading days)
COHORT_RUNS = 70          # how long a signal's ticker stays in the price log
PRICES_MAX_RUNS = 500     # prune the price log beyond this many run dates
SPLIT_GUARD_PCT = 40      # a 1-run move beyond this voids the window (split?)

MARKETS = [
    # Americas
    'america', 'canada', 'mexico', 'brazil', 'chile', 'colombia', 'peru', 'argentina',
    # Europe
    'uk', 'germany', 'france', 'italy', 'spain', 'netherlands', 'belgium',
    'switzerland', 'austria', 'portugal', 'ireland', 'sweden', 'norway',
    'denmark', 'finland', 'poland', 'greece', 'turkey', 'israel',
    # Middle East / Africa
    'uae', 'ksa', 'qatar', 'egypt', 'southafrica',
    # Asia-Pacific
    'japan', 'korea', 'china', 'hongkong', 'taiwan', 'india', 'indonesia',
    'malaysia', 'philippines', 'singapore', 'thailand', 'vietnam',
    'australia', 'newzealand',
]

# Scanner field -> row key for the detail panel's performance strip.
PERF_FIELDS = {'Perf.W': 'perf_w', 'Perf.1M': 'perf_1m', 'Perf.3M': 'perf_3m',
               'Perf.6M': 'perf_6m', 'Perf.YTD': 'perf_ytd', 'Perf.Y': 'perf_1y'}

FIELDS = [
    'name', 'description', 'close', 'currency', 'change',
    'price_52_week_high', 'relative_volume_10d_calc', 'market_cap_basic',
    'sector', 'industry', 'country', 'exchange', 'logoid', 'time',
    'earnings_release_next_date',
    *PERF_FIELDS,
]

EARNINGS_MAX_DAYS = 90    # ignore earnings dates further out than this
EARNINGS_SOON_DAYS = 7    # dashboard/notification warning threshold

HISTORY_COLUMNS = ['run_date', 'session_date', 'list', 'ticker',
                   'price', 'dist_pct', 'rel_volume', 'rs']

DASHBOARD_URL = 'https://patrickdx.github.io/breakout-screener/'

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / 'data' / 'history.csv'
DATA_JSON_PATH = ROOT / 'docs' / 'data.json'
RUNS_DIR = ROOT / 'docs' / 'runs'
TRAILS_PATH = ROOT / 'docs' / 'trails.json'
CEILINGS_PATH = ROOT / 'data' / 'ceilings.json'
PRICES_PATH = ROOT / 'data' / 'prices.csv'
PERFORMANCE_PATH = ROOT / 'docs' / 'performance.json'


def fetch() -> pd.DataFrame:
    """One scanner request: the whole filtered global universe."""
    _, df = (
        Query()
        .set_markets(*MARKETS)
        .select(*FIELDS)
        .where(
            col('market_cap_basic') > MIN_MARKET_CAP,
            col('is_primary') == True,           # dedupe cross-listings
            col('typespecs').has('common'),      # common stock only
            col('close').above_pct('price_52_week_high', 1 - UNIVERSE_PCT / 100),
        )
        .order_by('market_cap_basic', ascending=False)
        .limit(20000)
        .get_scanner_data()
    )
    return df


def classify(df: pd.DataFrame, run_date: str,
             prior_ceilings: dict[str, float] | None = None) -> pd.DataFrame:
    """Normalize scanner rows, tag Breakout/Near, drop off-screen rows.

    Breakout = close crossed above the prior session's 52-week high on
    volume (falling back to the within-BREAKOUT_PCT state rule when no prior
    ceiling is stored). A crosser is kept even if a same-day spike leaves the
    close more than PROXIMITY_PCT below the *new* high. session_date is the
    UTC date of the row's latest exchange session; when it differs from
    run_date the data is stale (e.g. a US holiday).
    """
    high = df['price_52_week_high']
    session = pd.to_datetime(df['time'], unit='s', utc=True).dt.strftime('%Y-%m-%d')
    out = pd.DataFrame({
        'ticker': df['ticker'],
        'symbol': df['name'],
        'name': df['description'],
        'price': df['close'].astype(float).round(4),
        'currency': df['currency'].fillna('USD'),
        'change': df['change'].astype(float).round(2),
        'high_52w': high.astype(float).round(4),
        'dist_pct': ((high - df['close']) / high * 100).astype(float).round(2),
        'rel_volume': df['relative_volume_10d_calc'].astype(float).round(2),
        'mcap': df['market_cap_basic'].astype(float),
        'sector': df['sector'].fillna(''),
        'industry': df['industry'].fillna(''),
        'country': df['country'].fillna(''),
        'exchange': df['exchange'],
        'logoid': df['logoid'].fillna(''),
        'session_date': session.fillna(run_date),
    })
    for src, dst in PERF_FIELDS.items():
        out[dst] = df[src].astype(float).round(1)
    # Days until the next earnings report (null if unknown or > EARNINGS_MAX_DAYS).
    earn = pd.to_datetime(df['earnings_release_next_date'], unit='s', utc=True)
    days = (earn - pd.Timestamp(run_date, tz='UTC')).dt.days.astype(float)
    out['earnings_in'] = days.where((days >= 0) & (days <= EARNINGS_MAX_DAYS))
    # Relative strength: 3-month perf minus the median of the same country's
    # scanned cohort (stocks within UNIVERSE_PCT of their highs) — currency-
    # consistent leader/laggard ranking with no benchmark data needed.
    med = df.groupby(df['country'].fillna(''))['Perf.3M'].transform('median')
    out['rs'] = (df['Perf.3M'] - med).astype(float).round(1)
    prior = out['ticker'].map(prior_ceilings or {})
    vol_ok = out['rel_volume'] > VOLUME_THRESHOLD
    crossed = (out['price'] > prior) & vol_ok          # NaN prior compares False
    fallback = prior.isna() & (out['dist_pct'] <= BREAKOUT_PCT) & vol_ok
    out['list'] = 'Near'
    out.loc[crossed | fallback, 'list'] = 'Breakout'
    on_screen = (out['list'] == 'Breakout') | (out['dist_pct'] <= PROXIMITY_PCT)
    return out[on_screen].reset_index(drop=True)


def load_prior_ceilings(run_date: str) -> dict[str, float]:
    """52-week highs as of the run before this one.

    The file keeps two generations: on a same-day re-run (file already dated
    run_date) the previous generation is used, so a re-run never compares
    closes against ceilings that already include today's session.
    """
    if not CEILINGS_PATH.exists():
        return {}
    f = json.loads(CEILINGS_PATH.read_text())
    return f.get('prev_ceilings', {}) if f.get('date') == run_date else f.get('ceilings', {})


def write_ceilings(run_date: str, ceilings: dict[str, float]) -> None:
    prev_date, prev = None, {}
    if CEILINGS_PATH.exists():
        f = json.loads(CEILINGS_PATH.read_text())
        if f.get('date') == run_date:          # re-run: keep the older generation
            prev_date, prev = f.get('prev_date'), f.get('prev_ceilings', {})
        else:
            prev_date, prev = f.get('date'), f.get('ceilings', {})
    CEILINGS_PATH.parent.mkdir(exist_ok=True)
    CEILINGS_PATH.write_text(json.dumps(
        {'date': run_date, 'ceilings': ceilings,
         'prev_date': prev_date, 'prev_ceilings': prev}, separators=(',', ':')))


def load_history() -> pd.DataFrame:
    if not HISTORY_PATH.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    return pd.read_csv(HISTORY_PATH, dtype={'run_date': str, 'session_date': str,
                                            'list': str, 'ticker': str})


def compute_streaks(history: pd.DataFrame, today: pd.DataFrame,
                    run_date: str) -> pd.DataFrame:
    """Add streak / days_near / streak_start / near_start columns from history.

    streak       distinct sessions on the Breakouts list over trailing
                 consecutive runs, incl. today (0 for Near rows)
    days_near    same, but for appearing on either list
    streak_start run date the current breakout streak began ('' for Near rows)
    near_start   run date the current on-screen streak began (all rows)
    """
    prior = history[history['run_date'] != run_date]  # idempotent re-runs
    run_dates = sorted(prior['run_date'].unique())
    by_run: dict[str, dict[str, dict[str, str]]] = {}
    for d, g in prior.groupby('run_date'):
        b = g[g['list'] == 'Breakout']
        by_run[d] = {
            'breakout': dict(zip(b['ticker'], b['session_date'])),
            'onscreen': dict(zip(g['ticker'], g['session_date'])),
        }

    def trailing(key: str, t: str) -> tuple[list[str], set[str]]:
        """Walk runs newest-to-oldest while `t` stays listed under `key`."""
        runs, sessions = [], set()
        for d in reversed(run_dates):
            m = by_run[d][key]
            if t in m:
                runs.append(d)
                sessions.add(m[t])
            else:
                break
        return runs, sessions

    streaks, days_near, starts, near_starts = [], [], [], []
    for t, sess, lst in zip(today['ticker'], today['session_date'], today['list']):
        runs_on, sess_on = trailing('onscreen', t)
        days_near.append(len(sess_on | {sess}))
        near_starts.append(runs_on[-1] if runs_on else run_date)
        if lst == 'Breakout':
            runs_b, sess_b = trailing('breakout', t)
            streaks.append(len(sess_b | {sess}))
            starts.append(runs_b[-1] if runs_b else run_date)
        else:
            streaks.append(0)
            starts.append('')
    today = today.copy()
    today['streak'] = streaks
    today['days_near'] = days_near
    today['streak_start'] = starts
    today['near_start'] = near_starts
    return today


def update_history(history: pd.DataFrame, today: pd.DataFrame,
                   run_date: str) -> pd.DataFrame:
    """Replace any rows for run_date with today's, then prune old runs."""
    rows = today.reindex(columns=['session_date', 'list', 'ticker', 'price',
                                  'dist_pct', 'rel_volume', 'rs']).copy()
    rows.insert(0, 'run_date', run_date)
    hist = pd.concat([history[history['run_date'] != run_date], rows],
                     ignore_index=True)[HISTORY_COLUMNS]
    keep = sorted(hist['run_date'].unique())[-HISTORY_MAX_RUNS:]
    hist = hist[hist['run_date'].isin(keep)]
    # Stable order so daily commits diff as pure appends.
    return hist.sort_values(['run_date', 'list', 'ticker'], ignore_index=True)


def build_trails(history: pd.DataFrame, today: pd.DataFrame) -> dict:
    """Per-ticker appearance trail for the dashboard's detail panel.

    {ticker: [[run_date, "B"|"N", dist_pct, rel_volume], ...]} — the last
    TRAIL_RUNS rows for every ticker on today's screen, oldest first.
    """
    h = history[history['ticker'].isin(set(today['ticker']))]
    trails: dict[str, list] = {}
    for t, g in h.sort_values('run_date').groupby('ticker'):
        g = g.tail(TRAIL_RUNS)
        trails[t] = [
            [r.run_date, 'B' if r.list == 'Breakout' else 'N',
             None if pd.isna(r.dist_pct) else float(r.dist_pct),
             None if pd.isna(r.rel_volume) else float(r.rel_volume)]
            for r in g.itertuples()
        ]
    return trails


# --- forward-return tracking ---------------------------------------------------

def build_cohort(history: pd.DataFrame) -> list[str]:
    """Tickers whose forward prices we still need.

    Covers classic-rule Breakouts AND old-state-rule hits (which can sit on
    the Near list), so the old-vs-new comparison in compute_performance has
    no survivorship gap of its own.
    """
    sig = history[(history['list'] == 'Breakout')
                  | ((history['dist_pct'] <= BREAKOUT_PCT)
                     & (history['rel_volume'] > VOLUME_THRESHOLD))]
    recent = sorted(set(sig['run_date']))[-COHORT_RUNS:]
    return sorted(set(sig.loc[sig['run_date'].isin(recent), 'ticker']))


def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Current close per ticker, filters stripped so delisted-from-screen
    stocks and benchmark ETFs come back too."""
    closes: dict[str, float] = {}
    for i in range(0, len(tickers), 500):
        q = Query().set_tickers(*tickers[i:i + 500]).select('close').limit(600)
        q.query.pop('filter', None)     # default presets exclude funds (QQQ)
        q.query.pop('filter2', None)
        _, df = q.get_scanner_data()
        closes.update({r.ticker: float(r.close) for r in df.itertuples()
                       if pd.notna(r.close)})
    return closes


def load_prices() -> pd.DataFrame:
    if not PRICES_PATH.exists():
        return pd.DataFrame(columns=['run_date', 'ticker', 'close'])
    return pd.read_csv(PRICES_PATH, dtype={'run_date': str, 'ticker': str})


def merge_prices(prices: pd.DataFrame, closes: dict[str, float],
                 run_date: str) -> pd.DataFrame:
    """Replace run_date's rows (idempotent re-runs), prune old run dates."""
    rows = pd.DataFrame({'run_date': run_date, 'ticker': list(closes),
                         'close': list(closes.values())})
    out = pd.concat([prices[prices['run_date'] != run_date], rows],
                    ignore_index=True)
    keep = sorted(out['run_date'].unique())[-PRICES_MAX_RUNS:]
    return (out[out['run_date'].isin(keep)]
            .sort_values(['run_date', 'ticker'], ignore_index=True))


def compute_performance(history: pd.DataFrame, prices: pd.DataFrame) -> dict:
    """Benchmark-adjusted forward returns for every stored signal.

    Windows are counted in price-log runs. Every signal is accounted for:
    measured, pending (window not elapsed), missing (no exit price — delisted
    or fetch failure; reported, never silently dropped), invalid (split-guard
    tripped), or pre_tracking (signal predates the price log).
    """
    run_dates = sorted(prices['run_date'].unique())
    idx = {d: i for i, d in enumerate(run_dates)}
    px = {(r.ticker, r.run_date): float(r.close) for r in prices.itertuples()}

    def window_valid(t: str, i0: int, i1: int) -> bool:
        prev = None
        for d in run_dates[i0:i1 + 1]:
            c = px.get((t, d))
            if c is None:
                continue
            if prev and abs(c / prev - 1) * 100 > SPLIT_GUARD_PCT:
                return False
            prev = c
        return True

    def measure(sigs: pd.DataFrame) -> dict:
        out = {}
        for n in HORIZONS:
            excess, raw = [], []
            pending = missing = invalid = pre = 0
            for row in sigs.itertuples():
                d0, t, p0 = row.run_date, row.ticker, float(row.price)
                if d0 not in idx:
                    pre += 1
                    continue
                i0 = idx[d0]
                i1 = i0 + n
                if i1 >= len(run_dates):
                    pending += 1
                    continue
                d1 = run_dates[i1]
                p1, b0, b1 = (px.get((t, d1)), px.get((BENCHMARK, d0)),
                              px.get((BENCHMARK, d1)))
                if p1 is None or b0 is None or b1 is None:
                    missing += 1
                    continue
                if not window_valid(t, i0, i1):
                    invalid += 1
                    continue
                r = (p1 / p0 - 1) * 100
                raw.append(r)
                excess.append(r - (b1 / b0 - 1) * 100)
            e = pd.Series(excess, dtype=float)
            out[str(n)] = {
                'n': len(e), 'pending': pending, 'missing': missing,
                'invalid': invalid, 'pre_tracking': pre,
                'hit_rate': None if e.empty else round(float((e > 0).mean()) * 100, 1),
                'median_excess': None if e.empty else round(float(e.median()), 2),
                'mean_excess': None if e.empty else round(float(e.mean()), 2),
                'mean_raw': None if not raw else round(float(pd.Series(raw).mean()), 2),
            }
        return out

    hist_runs = sorted(set(history['run_date']))
    prev_run = {d: (hist_runs[i - 1] if i else None) for i, d in enumerate(hist_runs)}
    b_sets = {d: set(g.loc[g['list'] == 'Breakout', 'ticker'])
              for d, g in history.groupby('run_date')}
    sig = history[history['list'] == 'Breakout']
    new_mask = pd.Series(
        [r.ticker not in b_sets.get(prev_run.get(r.run_date) or '', set())
         for r in sig.itertuples()], index=sig.index, dtype=bool)
    old_rule = history[(history['dist_pct'] <= BREAKOUT_PCT)
                       & (history['rel_volume'] > VOLUME_THRESHOLD)]

    groups = [('Breakouts', sig),
              ('First-day signals (NEW)', sig[new_mask]),
              ('Continuation days', sig[~new_mask]),
              ('Old state rule (comparison)', old_rule)]
    return {
        'benchmark': BENCHMARK,
        'tracking_since': run_dates[0] if run_dates else None,
        'updated': run_dates[-1] if run_dates else None,
        'horizons': list(HORIZONS),
        'groups': [{'name': name, 'signals': int(len(s)), 'stats': measure(s)}
                   for name, s in groups],
    }


# --- notifications ---------------------------------------------------------------

def _fmt_cap(v: float) -> str:
    if v >= 1e12:
        return f'${v / 1e12:.2f}T'
    if v >= 1e9:
        return f'${v / 1e9:.1f}B'
    return f'${v / 1e6:.0f}M'


def build_notification(payload: dict, is_monday: bool,
                       history: pd.DataFrame) -> str | None:
    """Discord message for today's NEW breakouts (+ a Monday recap line).

    Returns None when there's nothing worth pinging about. Pure function so
    the formatting is testable without a webhook.
    """
    new = [r for r in payload['breakouts'] if r.get('streak') == 1]
    lines: list[str] = []
    if new:
        lines.append(f"🚀 **{len(new)} new breakout{'s' if len(new) != 1 else ''}**"
                     f" — {payload['run_date']}"
                     f" ({payload['stats']['breakouts']} on the list in total)")
        for r in new[:10]:
            chg = '' if r.get('change') is None else f" {r['change']:+.1f}%"
            rv = '' if r.get('rel_volume') is None else f" · {r['rel_volume']:.1f}× vol"
            ei = r.get('earnings_in')
            earn = (f' · ⚠️ earnings in {int(ei)}d'
                    if ei is not None and ei <= EARNINGS_SOON_DAYS else '')
            lines.append(f"• **{r['symbol']}** {(r['name'] or '')[:32]} —"
                         f"{chg}{rv} · {_fmt_cap(r['mcap'])} · {r['country']}{earn}")
        if len(new) > 10:
            lines.append(f'…and {len(new) - 10} more')
    if is_monday and not history.empty:
        week = sorted(set(history['run_date']))[-5:]
        wk = history[history['run_date'].isin(week) & (history['list'] == 'Breakout')]
        if len(wk):
            top = wk['ticker'].value_counts()
            leaders = ', '.join(t.split(':')[-1] for t in top.index[:5])
            lines.append(f"\n📅 Past {len(week)} runs: {top.size} unique breakout"
                         f" tickers · most persistent: {leaders}")
    if not lines:
        return None
    lines.append(f'\n📊 {DASHBOARD_URL}')
    return '\n'.join(lines)[:1990]      # Discord content cap is 2000 chars


def notify(payload: dict, history: pd.DataFrame, now: datetime.datetime) -> None:
    """Post to DISCORD_WEBHOOK_URL if configured. Never fails the run."""
    url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not url:
        return
    msg = build_notification(payload, now.weekday() == 0, history)
    if not msg:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({'content': msg}).encode(),
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'breakout-screener'})
        urllib.request.urlopen(req, timeout=15)
        print('Notification posted.')
    except Exception as e:                       # noqa: BLE001
        print(f'Notification failed (non-fatal): {e}', file=sys.stderr)


def build_trend(history: pd.DataFrame) -> list[dict]:
    counts = (history.groupby(['run_date', 'list']).size()
              .unstack(fill_value=0).reindex(columns=['Breakout', 'Near'], fill_value=0))
    return [{'d': d, 'b': int(r['Breakout']), 'n': int(r['Near'])}
            for d, r in counts.tail(TREND_RUNS).iterrows()]


def build_payload(today: pd.DataFrame, trend: list[dict], run_date: str,
                  generated: str, backfilled: bool = False) -> dict:
    breakouts = today[today['list'] == 'Breakout'].sort_values(
        ['streak', 'dist_pct'], ascending=[False, True])
    near = today[today['list'] == 'Near'].sort_values(
        ['days_near', 'dist_pct'], ascending=[False, True])

    def records(df: pd.DataFrame) -> list[dict]:
        cols = [c for c in df.columns if c != 'list']
        return json.loads(df[cols].to_json(orient='records'))

    payload = {
        'generated_utc': generated,
        'run_date': run_date,
        'params': {'breakout_pct': BREAKOUT_PCT, 'proximity_pct': PROXIMITY_PCT,
                   'volume_threshold': VOLUME_THRESHOLD,
                   'min_market_cap': MIN_MARKET_CAP, 'markets': len(MARKETS)},
        'stats': {'matches': len(today), 'breakouts': len(breakouts),
                  'near': len(near),
                  'new_breakouts': int((breakouts['streak'] == 1).sum()),
                  'countries': int(today['country'].nunique())},
        'trend': trend,
        'breakouts': records(breakouts),
        'near': records(near),
    }
    if backfilled:
        payload['backfilled'] = True
    return payload


def write_latest(payload: dict) -> None:
    DATA_JSON_PATH.parent.mkdir(exist_ok=True)
    DATA_JSON_PATH.write_text(json.dumps(payload, separators=(',', ':')))


def write_archive(payload: dict) -> None:
    """Store the run under docs/runs/<date>.json and rebuild the date index."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"{payload['run_date']}.json").write_text(
        json.dumps(payload, separators=(',', ':')))
    dates = sorted(p.stem for p in RUNS_DIR.glob('????-??-??.json'))
    for d in dates[:-ARCHIVE_MAX_RUNS]:
        (RUNS_DIR / f'{d}.json').unlink()
    (RUNS_DIR / 'index.json').write_text(
        json.dumps({'dates': dates[-ARCHIVE_MAX_RUNS:]}))


def main() -> int:
    now = datetime.datetime.now(datetime.UTC)
    run_date = now.strftime('%Y-%m-%d')

    raw = fetch()
    if raw.empty:
        print('Scanner returned no rows.', file=sys.stderr)
        return 1
    print(f'Universe (>${MIN_MARKET_CAP / 1e9:.0f}B, within {UNIVERSE_PCT}% of 52w high): {len(raw)}')

    prior = load_prior_ceilings(run_date)
    today = classify(raw, run_date, prior)
    print(f'On screen: {len(today)} | prior ceilings known: {len(prior)}')
    history = load_history()
    today = compute_streaks(history, today, run_date)

    history = update_history(history, today, run_date)
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    history.to_csv(HISTORY_PATH, index=False)

    payload = build_payload(today, build_trend(history), run_date,
                            now.strftime('%Y-%m-%d %H:%M UTC'))
    write_latest(payload)
    write_archive(payload)
    TRAILS_PATH.write_text(json.dumps(build_trails(history, today),
                                      separators=(',', ':')))
    write_ceilings(run_date, dict(zip(
        raw['ticker'], raw['price_52_week_high'].astype(float).round(4))))

    closes = fetch_prices(build_cohort(history) + BENCHMARK_TICKERS)
    prices = merge_prices(load_prices(), closes, run_date)
    prices.to_csv(PRICES_PATH, index=False)
    perf = compute_performance(history, prices)
    PERFORMANCE_PATH.write_text(json.dumps(perf, separators=(',', ':')))
    measured = sum(s['n'] for g in perf['groups'] if g['name'] == 'Breakouts'
                   for s in g['stats'].values())
    print(f'Cohort prices logged: {len(closes)} | measured windows: {measured}')

    notify(payload, history, now)

    n_b = int((today['list'] == 'Breakout').sum())
    stale = int((today['session_date'] != run_date).sum())
    print(f'Breakouts: {n_b} | Near: {len(today) - n_b} | stale sessions: {stale}')
    print(f'Wrote {HISTORY_PATH.relative_to(ROOT)} and {DATA_JSON_PATH.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
