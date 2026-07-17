"""SQLite storage layer: data/screener.db is the screener's memory.

Replaces the three flat files the screener used to commit (history.csv,
prices.csv, ceilings.json) with one SQLite database — real tables, primary
keys and transactions, so a crashed run can't leave history updated while
ceilings go stale. The db lives in data/ as the backend source of truth;
the dashboard never reads it. screener.py exports the small docs/*.json
files the static GitHub Pages site actually fetches.

Tables
  history   dated log of every run; feeds the streak columns
  prices    daily close log for the forward-return cohort + benchmarks
  ceilings  per-ticker 3-month highs from the last two runs; feeds the
            prior-ceiling comparison (two generations so a same-day re-run
            never sees its own ceilings)

The compute functions in screener.py stay pure (DataFrame in, DataFrame
out); this module is only the load/save edge. Whole-table saves are fine at
this scale (~30k rows) and keep the pruning/idempotency logic in the pure
functions where it's unit-tested.
"""
import sqlite3
from pathlib import Path

import pandas as pd

HISTORY_COLUMNS = ['run_date', 'session_date', 'list', 'ticker',
                   'price', 'dist_pct', 'rel_volume', 'rs']

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    run_date     TEXT NOT NULL,
    session_date TEXT NOT NULL,
    list         TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    price        REAL,
    dist_pct     REAL,
    rel_volume   REAL,
    rs           REAL,
    PRIMARY KEY (run_date, ticker)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS prices (
    run_date TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    close    REAL NOT NULL,
    PRIMARY KEY (run_date, ticker)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ceilings (
    run_date TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    high     REAL NOT NULL,
    PRIMARY KEY (run_date, ticker)
) WITHOUT ROWID;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _rows(df: pd.DataFrame, cols: list[str]) -> list[list]:
    """DataFrame -> parameter rows with native Python types and NaN -> None
    (numpy scalars can't be bound by sqlite3; to_json normalizes both)."""
    import json
    return json.loads(df.reindex(columns=cols).to_json(orient='values'))


# --- history -------------------------------------------------------------------

def load_history(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        'SELECT * FROM history ORDER BY run_date, list, ticker', con)


def save_history(con: sqlite3.Connection, hist: pd.DataFrame) -> None:
    with con:
        con.execute('DELETE FROM history')
        con.executemany('INSERT INTO history VALUES (?,?,?,?,?,?,?,?)',
                        _rows(hist, HISTORY_COLUMNS))


# --- prices --------------------------------------------------------------------

def load_prices(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql('SELECT * FROM prices ORDER BY run_date, ticker', con)


def save_prices(con: sqlite3.Connection, prices: pd.DataFrame) -> None:
    with con:
        con.execute('DELETE FROM prices')
        con.executemany('INSERT INTO prices VALUES (?,?,?)',
                        _rows(prices, ['run_date', 'ticker', 'close']))


# --- ceilings --------------------------------------------------------------------

def load_prior_ceilings(con: sqlite3.Connection, run_date: str) -> dict[str, float]:
    """{ticker: 3-month high} as of the run before this one.

    The table keeps two generations (run dates). On a normal day the newest
    stored generation is yesterday's — use it. On a same-day re-run, rows
    for run_date are already stored, and comparing today's closes against
    them would hide every breakout — so use the newest generation whose
    run_date is NOT this run's. MAX() over no rows is NULL, which matches
    nothing, so the first ever run gets {} for free.
    """
    return dict(con.execute(
        """SELECT ticker, high FROM ceilings WHERE run_date =
               (SELECT MAX(run_date) FROM ceilings WHERE run_date != ?)""",
        (run_date,)).fetchall())


def write_ceilings(con: sqlite3.Connection, run_date: str,
                   ceilings: dict[str, float]) -> None:
    """Store today's generation, keeping only the two newest run dates.

    Replacing run_date's own rows first makes a same-day re-run overwrite
    its earlier write instead of stacking a third generation — the
    generation before today always survives.
    """
    with con:
        con.execute('DELETE FROM ceilings WHERE run_date = ?', (run_date,))
        con.executemany('INSERT INTO ceilings VALUES (?,?,?)',
                        [(run_date, t, h) for t, h in ceilings.items()])
        con.execute("""DELETE FROM ceilings WHERE run_date NOT IN (
                           SELECT DISTINCT run_date FROM ceilings
                           ORDER BY run_date DESC LIMIT 2)""")
