"""Unit tests for the pure logic in screener.py (no network)."""
import pandas as pd
import pytest

from screener import (HISTORY_COLUMNS, classify, compute_streaks,
                      update_history)

RUN = '2026-07-01'


def scanner_frame(rows: list[dict]) -> pd.DataFrame:
    """Minimal fake of a TradingView scanner response."""
    defaults = {
        'ticker': 'NASDAQ:TEST', 'name': 'TEST', 'description': 'Test Corp',
        'close': 99.5, 'currency': 'USD', 'change': 1.0,
        'price_52_week_high': 100.0, 'relative_volume_10d_calc': 2.0,
        'market_cap_basic': 5e9, 'sector': 'Finance', 'industry': 'Banks',
        'country': 'United States', 'exchange': 'NASDAQ', 'logoid': 'test',
        'time': pd.Timestamp(f'{RUN} 13:30', tz='UTC').timestamp(),
        'Perf.W': 2.5, 'Perf.1M': 8.0, 'Perf.3M': 15.0, 'Perf.6M': 30.0,
        'Perf.YTD': 30.0, 'Perf.Y': 60.0,
    }
    return pd.DataFrame([{**defaults, **r} for r in rows])


def hist(rows: list[tuple]) -> pd.DataFrame:
    """(run_date, session_date, list, ticker) -> history frame."""
    return pd.DataFrame(
        [{'run_date': r, 'session_date': s, 'list': l, 'ticker': t,
          'price': 1.0, 'dist_pct': 0.1, 'rel_volume': 2.0}
         for r, s, l, t in rows], columns=HISTORY_COLUMNS)


def streak_of(today: pd.DataFrame, ticker: str) -> tuple[int, int, str]:
    row = today[today['ticker'] == ticker].iloc[0]
    return int(row['streak']), int(row['days_near']), row['streak_start']


# --- classify -----------------------------------------------------------------

def test_classify_splits_breakout_and_near():
    df = classify(scanner_frame([
        {'ticker': 'A', 'close': 99.6},                              # 0.4% off, relvol 2.0 -> Breakout
        {'ticker': 'B', 'close': 99.6, 'relative_volume_10d_calc': 1.0},  # low volume -> Near
        {'ticker': 'C', 'close': 96.0},                              # 4% off -> Near
    ]), RUN)
    assert df.set_index('ticker')['list'].to_dict() == {'A': 'Breakout', 'B': 'Near', 'C': 'Near'}
    assert df.set_index('ticker')['dist_pct']['C'] == 4.0
    assert df.iloc[0]['perf_1m'] == 8.0 and df.iloc[0]['perf_1y'] == 60.0


def test_classify_missing_relvol_is_near_not_dropped():
    df = classify(scanner_frame([{'ticker': 'A', 'close': 99.9,
                                  'relative_volume_10d_calc': None}]), RUN)
    assert df.iloc[0]['list'] == 'Near'


def test_classify_session_date_from_bar_time():
    stale = pd.Timestamp('2026-06-30 13:30', tz='UTC').timestamp()
    df = classify(scanner_frame([{'ticker': 'A', 'time': stale},
                                 {'ticker': 'B', 'time': None}]), RUN)
    assert df.set_index('ticker')['session_date'].to_dict() == {'A': '2026-06-30', 'B': RUN}


# --- compute_streaks ----------------------------------------------------------

def today_frame(rows: list[tuple]) -> pd.DataFrame:
    """(ticker, list, session_date) -> minimal classified frame."""
    return pd.DataFrame([{'ticker': t, 'list': l, 'session_date': s,
                          'price': 1.0, 'dist_pct': 0.1, 'rel_volume': 2.0}
                         for t, l, s in rows])


def test_first_run_streaks_are_one():
    today = compute_streaks(hist([]), today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (1, 1, RUN)


def test_consecutive_runs_accrue():
    h = hist([('2026-06-29', '2026-06-29', 'Breakout', 'A'),
              ('2026-06-30', '2026-06-30', 'Breakout', 'A')])
    today = compute_streaks(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (3, 3, '2026-06-29')


def test_missed_run_resets_streak():
    h = hist([('2026-06-29', '2026-06-29', 'Breakout', 'A'),
              ('2026-06-30', '2026-06-30', 'Near', 'B')])  # A absent on 06-30
    today = compute_streaks(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (1, 1, RUN)


def test_skipped_run_date_does_not_reset():
    # No run at all on 06-30 (CI outage) -> the 06-29 run is still "consecutive".
    h = hist([('2026-06-29', '2026-06-29', 'Breakout', 'A')])
    today = compute_streaks(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (2, 2, '2026-06-29')


def test_stale_session_does_not_inflate_streak():
    # 06-30 run re-served the 06-29 session (holiday): 3 runs, 2 real sessions.
    h = hist([('2026-06-29', '2026-06-29', 'Breakout', 'A'),
              ('2026-06-30', '2026-06-29', 'Breakout', 'A')])
    today = compute_streaks(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (2, 2, '2026-06-29')


def test_near_rows_track_days_near_but_no_streak():
    h = hist([('2026-06-30', '2026-06-30', 'Breakout', 'A')])
    today = compute_streaks(h, today_frame([('A', 'Near', RUN)]), RUN)
    assert streak_of(today, 'A') == (0, 2, '')


def test_breakout_streak_survives_near_days_in_days_near_only():
    # A was Near on 06-29, Breakout on 06-30: breakout streak 2, on-screen 3.
    h = hist([('2026-06-29', '2026-06-29', 'Near', 'A'),
              ('2026-06-30', '2026-06-30', 'Breakout', 'A')])
    today = compute_streaks(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (2, 3, '2026-06-30')


def test_same_day_rerun_ignores_own_rows():
    h = hist([(RUN, RUN, 'Breakout', 'A')])  # earlier run today
    today = compute_streaks(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert streak_of(today, 'A') == (1, 1, RUN)


# --- update_history -----------------------------------------------------------

def test_update_history_is_idempotent_for_reruns():
    h = hist([('2026-06-30', '2026-06-30', 'Breakout', 'A'),
              (RUN, RUN, 'Breakout', 'STALE_ROW')])
    out = update_history(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert list(out['ticker']) == ['A', 'A']
    assert 'STALE_ROW' not in set(out['ticker'])


def test_update_history_prunes_old_runs(monkeypatch):
    import screener
    monkeypatch.setattr(screener, 'HISTORY_MAX_RUNS', 2)
    h = hist([('2026-06-27', '2026-06-27', 'Breakout', 'A'),
              ('2026-06-30', '2026-06-30', 'Breakout', 'A')])
    out = screener.update_history(h, today_frame([('A', 'Breakout', RUN)]), RUN)
    assert sorted(out['run_date'].unique()) == ['2026-06-30', RUN]


def test_build_trails_covers_screen_tickers_only_oldest_first():
    from screener import build_trails
    h = hist([('2026-06-30', '2026-06-30', 'Near', 'A'),
              (RUN, RUN, 'Breakout', 'A'),
              (RUN, RUN, 'Near', 'GONE')])   # not on today's screen
    trails = build_trails(h, today_frame([('A', 'Breakout', RUN)]))
    assert set(trails) == {'A'}
    assert [(e[0], e[1]) for e in trails['A']] == [('2026-06-30', 'N'), (RUN, 'B')]
    assert trails['A'][0][2] == 0.1 and trails['A'][0][3] == 2.0


