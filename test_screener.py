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


def test_classify_classic_rule_crosser_kept_even_far_from_new_high():
    # Spiked to 110 (new high) but closed 99.6 — >5% off the NEW high, yet the
    # close crossed the PRIOR ceiling of 95, so it's a breakout and stays on screen.
    df = classify(scanner_frame([{'ticker': 'A', 'close': 99.6,
                                  'price_52_week_high': 110.0}]),
                  RUN, {'A': 95.0})
    assert df.iloc[0]['list'] == 'Breakout' and df.iloc[0]['dist_pct'] > 5


def test_classify_classic_rule_at_high_but_below_prior_ceiling_is_near():
    # Within 0.4% of today's high on volume — the OLD rule would fire — but the
    # close never crossed yesterday's ceiling of 100.5, so: Near.
    df = classify(scanner_frame([{'ticker': 'A', 'close': 99.6}]),
                  RUN, {'A': 100.5})
    assert df.iloc[0]['list'] == 'Near'


def test_classify_crosser_needs_volume():
    df = classify(scanner_frame([{'ticker': 'A', 'close': 99.6,
                                  'relative_volume_10d_calc': 1.0}]),
                  RUN, {'A': 95.0})
    assert df.iloc[0]['list'] == 'Near'


def test_classify_off_screen_rows_dropped():
    df = classify(scanner_frame([{'ticker': 'A', 'close': 90.0}]), RUN, {'A': 99.0})
    assert df.empty                                    # 10% off high, no cross


def test_ceilings_two_generation_rotation(tmp_path, monkeypatch):
    import screener
    monkeypatch.setattr(screener, 'CEILINGS_PATH', tmp_path / 'ceilings.json')
    screener.write_ceilings('2026-06-30', {'A': 100.0})
    screener.write_ceilings('2026-07-01', {'A': 110.0})
    assert screener.load_prior_ceilings('2026-07-02') == {'A': 110.0}
    # same-day re-run must see the generation BEFORE today's earlier write
    assert screener.load_prior_ceilings('2026-07-01') == {'A': 100.0}
    screener.write_ceilings('2026-07-01', {'A': 111.0})   # re-run overwrite
    assert screener.load_prior_ceilings('2026-07-01') == {'A': 100.0}
    assert screener.load_prior_ceilings('2026-07-02') == {'A': 111.0}


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


# --- forward-return tracking ----------------------------------------------------

D = ['2026-07-01', '2026-07-02', '2026-07-03', '2026-07-06', '2026-07-07']


def price_frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([{'run_date': d, 'ticker': t, 'close': c}
                         for d, t, c in rows])


def sig_hist(rows: list[tuple]) -> pd.DataFrame:
    """(run_date, list, ticker, price, dist_pct, rel_volume)"""
    return pd.DataFrame([{'run_date': d, 'session_date': d, 'list': l, 'ticker': t,
                          'price': p, 'dist_pct': dist, 'rel_volume': rv}
                         for d, l, t, p, dist, rv in rows])


def perf(history, prices, monkeypatch, horizons=(2,)):
    import screener
    monkeypatch.setattr(screener, 'HORIZONS', horizons)
    monkeypatch.setattr(screener, 'BENCHMARK', 'Q')
    return screener.compute_performance(history, prices)


def bench_rows(closes):
    return [(d, 'Q', c) for d, c in zip(D, closes)]


def test_performance_excess_return_vs_benchmark(monkeypatch):
    h = sig_hist([(D[0], 'Breakout', 'A', 100.0, 0.1, 2.0)])
    p = price_frame([(D[0], 'A', 100), (D[2], 'A', 120)] + bench_rows([100, 105, 110, 112, 115]))
    s = perf(h, p, monkeypatch)['groups'][0]['stats']['2']
    assert s['n'] == 1 and s['hit_rate'] == 100.0
    assert s['mean_raw'] == 20.0 and s['mean_excess'] == 10.0   # 20% - 10% bench


def test_performance_missing_exit_counted_not_dropped(monkeypatch):
    # Ticker vanished (delisted) — must surface as missing, not silently skew stats.
    h = sig_hist([(D[0], 'Breakout', 'GONE', 100.0, 0.1, 2.0)])
    p = price_frame([(D[0], 'GONE', 100)] + bench_rows([100, 100, 100, 100, 100]))
    s = perf(h, p, monkeypatch)['groups'][0]['stats']['2']
    assert (s['n'], s['missing']) == (0, 1)


def test_performance_pending_and_pre_tracking(monkeypatch):
    h = sig_hist([(D[3], 'Breakout', 'A', 100.0, 0.1, 2.0),       # window not elapsed
                  ('2026-06-01', 'Breakout', 'B', 50.0, 0.1, 2.0)])  # before price log
    p = price_frame(bench_rows([100, 100, 100, 100, 100]))
    s = perf(h, p, monkeypatch)['groups'][0]['stats']['2']
    assert (s['pending'], s['pre_tracking'], s['n']) == (1, 1, 0)


def test_performance_split_guard(monkeypatch):
    h = sig_hist([(D[0], 'Breakout', 'A', 100.0, 0.1, 2.0)])
    p = price_frame([(D[0], 'A', 100), (D[1], 'A', 300), (D[2], 'A', 310)]
                    + bench_rows([100, 100, 100, 100, 100]))
    s = perf(h, p, monkeypatch)['groups'][0]['stats']['2']
    assert (s['n'], s['invalid']) == (0, 1)


def test_performance_new_vs_continuation_and_old_rule(monkeypatch):
    h = sig_hist([(D[0], 'Breakout', 'A', 100.0, 0.1, 2.0),
                  (D[1], 'Breakout', 'A', 105.0, 0.1, 2.0),      # continuation
                  (D[1], 'Near', 'B', 50.0, 0.2, 3.0)])          # old-rule hit, Near list
    p = price_frame(bench_rows([100, 100, 100, 100, 100]))
    g = {x['name']: x for x in perf(h, p, monkeypatch)['groups']}
    assert g['Breakouts']['signals'] == 2
    assert g['First-day signals (NEW)']['signals'] == 1
    assert g['Continuation days']['signals'] == 1
    assert g['Old state rule (comparison)']['signals'] == 3      # A x2 + B


def test_build_cohort_and_merge_prices(monkeypatch):
    import screener
    monkeypatch.setattr(screener, 'PRICES_MAX_RUNS', 2)
    h = sig_hist([(D[0], 'Breakout', 'A', 100.0, 0.1, 2.0),
                  (D[1], 'Near', 'B', 50.0, 3.0, 1.0)])
    assert screener.build_cohort(h) == ['A']                     # breakouts only
    p = price_frame([(D[0], 'A', 1), (D[1], 'A', 2), (D[1], 'STALE', 9)])
    out = screener.merge_prices(p, {'A': 3.0}, D[1])             # re-run D[1] + prune
    assert list(out['ticker']) == ['A', 'A'] and list(out['close']) == [1, 3.0]


def test_build_trails_covers_screen_tickers_only_oldest_first():
    from screener import build_trails
    h = hist([('2026-06-30', '2026-06-30', 'Near', 'A'),
              (RUN, RUN, 'Breakout', 'A'),
              (RUN, RUN, 'Near', 'GONE')])   # not on today's screen
    trails = build_trails(h, today_frame([('A', 'Breakout', RUN)]))
    assert set(trails) == {'A'}
    assert [(e[0], e[1]) for e in trails['A']] == [('2026-06-30', 'N'), (RUN, 'B')]
    assert trails['A'][0][2] == 0.1 and trails['A'][0][3] == 2.0


