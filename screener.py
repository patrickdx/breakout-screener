"""Global 52-week-high breakout screener.

One TradingView scanner query pulls every primary-listed common stock above
MIN_MARKET_CAP across ~45 markets trading within PROXIMITY_PCT of its 52-week
high, pre-enriched with sector, industry, market cap (USD), country, currency
and logo. No per-ticker downloads, no API key.

Classification (same thresholds as the old NASDAQ/yfinance version):
  Breakout       distance to high <= BREAKOUT_PCT and relative volume > VOLUME_THRESHOLD
  Near Breakout  distance to high <= PROXIMITY_PCT

The repo is the data store:
  data/history.csv  dated log of every run; feeds the streak columns
  docs/data.json    today's lists + trend series, rendered by docs/index.html
                    (GitHub Pages)

Streak semantics: continuity is counted in consecutive *runs* (a skipped run
doesn't reset a streak) but length is counted in distinct exchange *sessions*,
so a run that re-serves stale data (US holiday, same-day re-run) can never
inflate a streak. Prices are in each listing's local currency; market cap is
normalized to USD by TradingView.
"""
import datetime
import json
import sys
from pathlib import Path

import pandas as pd
from tradingview_screener import Query, col

BREAKOUT_PCT = 0.5        # % below 52w high to count as a breakout
PROXIMITY_PCT = 5.0       # % below 52w high to stay on screen at all
VOLUME_THRESHOLD = 1.2    # today's volume vs 10-day average
MIN_MARKET_CAP = 2_000_000_000   # USD
HISTORY_MAX_RUNS = 500    # prune history beyond this many run dates
ARCHIVE_MAX_RUNS = 120    # per-run JSON archives kept in docs/runs/
TREND_RUNS = 90           # runs shown in the dashboard trend chart
TRAIL_RUNS = 40           # per-ticker appearance trail for the detail panel

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

FIELDS = [
    'name', 'description', 'close', 'currency', 'change',
    'price_52_week_high', 'relative_volume_10d_calc', 'market_cap_basic',
    'sector', 'industry', 'country', 'exchange', 'logoid', 'time',
]

HISTORY_COLUMNS = ['run_date', 'session_date', 'list', 'ticker',
                   'price', 'dist_pct', 'rel_volume']

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / 'data' / 'history.csv'
DATA_JSON_PATH = ROOT / 'docs' / 'data.json'
RUNS_DIR = ROOT / 'docs' / 'runs'
TRAILS_PATH = ROOT / 'docs' / 'trails.json'


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
            col('close').above_pct('price_52_week_high', 1 - PROXIMITY_PCT / 100),
        )
        .order_by('market_cap_basic', ascending=False)
        .limit(20000)
        .get_scanner_data()
    )
    return df


def classify(df: pd.DataFrame, run_date: str) -> pd.DataFrame:
    """Normalize scanner rows and tag each as Breakout or Near.

    session_date is the UTC date of the row's latest exchange session; when it
    differs from run_date the data is stale (e.g. a US holiday).
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
    is_breakout = (out['dist_pct'] <= BREAKOUT_PCT) & (out['rel_volume'] > VOLUME_THRESHOLD)
    out['list'] = 'Near'
    out.loc[is_breakout, 'list'] = 'Breakout'
    return out


def load_history() -> pd.DataFrame:
    if not HISTORY_PATH.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    return pd.read_csv(HISTORY_PATH, dtype={'run_date': str, 'session_date': str,
                                            'list': str, 'ticker': str})


def compute_streaks(history: pd.DataFrame, today: pd.DataFrame,
                    run_date: str) -> pd.DataFrame:
    """Add streak / days_near / streak_start columns from stored history.

    streak       distinct sessions on the Breakouts list over trailing
                 consecutive runs, incl. today (0 for Near rows)
    days_near    same, but for appearing on either list
    streak_start run date the current breakout streak began ('' for Near rows)
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

    streaks, days_near, starts = [], [], []
    for t, sess, lst in zip(today['ticker'], today['session_date'], today['list']):
        runs_on, sess_on = trailing('onscreen', t)
        days_near.append(len(sess_on | {sess}))
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
    return today


def update_history(history: pd.DataFrame, today: pd.DataFrame,
                   run_date: str) -> pd.DataFrame:
    """Replace any rows for run_date with today's, then prune old runs."""
    rows = today[['session_date', 'list', 'ticker', 'price',
                  'dist_pct', 'rel_volume']].copy()
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


def build_trend(history: pd.DataFrame) -> list[dict]:
    counts = (history.groupby(['run_date', 'list']).size()
              .unstack(fill_value=0).reindex(columns=['Breakout', 'Near'], fill_value=0))
    return [{'d': d, 'b': int(r['Breakout']), 'n': int(r['Near'])}
            for d, r in counts.tail(TREND_RUNS).iterrows()]


def build_payload(today: pd.DataFrame, trend: list[dict], run_date: str,
                  generated: str, backfilled: bool = False) -> dict:
    breakouts = today[today['list'] == 'Breakout'].sort_values(
        ['streak', 'dist_pct'], ascending=[False, True])
    near = today[today['list'] == 'Near'].sort_values('dist_pct')

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
    print(f'Universe (>${MIN_MARKET_CAP / 1e9:.0f}B, within {PROXIMITY_PCT}% of 52w high): {len(raw)}')

    today = classify(raw, run_date)
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

    n_b = int((today['list'] == 'Breakout').sum())
    stale = int((today['session_date'] != run_date).sum())
    print(f'Breakouts: {n_b} | Near: {len(today) - n_b} | stale sessions: {stale}')
    print(f'Wrote {HISTORY_PATH.relative_to(ROOT)} and {DATA_JSON_PATH.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
