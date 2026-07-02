"""Retail sentiment from Reddit for today's breakouts.

For every ticker on the Breakouts list, searches the past week of SUBREDDITS
for the symbol / company name, scores each post with VADER (lexicon sentiment
tuned for social text), and writes docs/reddit.json:

  {generated, window, subs, results: {ticker: {score, mentions, posts[]}}}

`score` is the upvote-weighted mean VADER compound (-1..1); posts carry their
own score so the dashboard can show the gauge next to the receipts.

Auth: uses Reddit's free OAuth app credentials when REDDIT_CLIENT_ID /
REDDIT_CLIENT_SECRET are set (required on cloud IPs — create a "script" app
at reddit.com/prefs/apps); falls back to the public JSON endpoint otherwise
(fine from residential IPs). Never fails the pipeline: on total failure the
previous reddit.json is left untouched and the exit code is still 0.
"""
import base64
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

SUBREDDITS = ['stocks', 'wallstreetbets', 'investing', 'StockMarket', 'ValueInvesting']
WINDOW = 'year'
SEARCH_LIMIT = 50         # posts pulled per ticker before relevance filtering
MAX_POSTS_STORED = 8      # per ticker, by upvotes
MIN_UPS = 2               # ignore sub-noise posts
HALF_LIFE_DAYS = 60       # sentiment weight halves every N days of post age
BULL, BEAR = 0.15, -0.15  # weighted-score thresholds (dashboard mirrors these)
USER_AGENT = 'breakout-screener/1.0 (github.com/patrickdx/breakout-screener)'

# Symbols that read as English words drown in false matches — for these only
# the $-prefixed form and the company name are searched.
WORDY_SYMBOLS = {
    'ALL', 'ANY', 'ARE', 'BEST', 'BIG', 'CAN', 'CAR', 'CASH', 'COST', 'DAY',
    'EAT', 'EDIT', 'FAST', 'FOR', 'FREE', 'FUN', 'GO', 'GOOD', 'HAS', 'HE',
    'HOME', 'IT', 'LIFE', 'LOVE', 'LOW', 'MAN', 'MIND', 'NEW', 'NICE', 'NOW',
    'ON', 'ONE', 'OPEN', 'OR', 'OUT', 'PLAY', 'REAL', 'RUN', 'SAFE', 'SAVE',
    'SEE', 'SHE', 'SO', 'TELL', 'TRUE', 'TWO', 'UP', 'WELL', 'WISH', 'YOU',
}
NAME_STOP_TOKENS = {
    'inc', 'inc.', 'corp', 'corp.', 'corporation', 'company', 'co', 'co.',
    'co.,', 'ltd', 'ltd.', 'limited', 'plc', 'sa', 's.a.', 'se', 'ag', 'nv',
    'n.v.', 'ab', 'asa', 'oyj', 'spa', 's.p.a.', 'holdings', 'holding',
    'group', 'class', 'the', '&', '(the)',
}

MEGATHREAD_RE = re.compile(
    r'(daily|weekly).{0,30}(thread|discussion)|megathread|what are your moves',
    re.IGNORECASE)

ROOT = Path(__file__).resolve().parent
DATA_JSON_PATH = ROOT / 'docs' / 'data.json'
REDDIT_JSON_PATH = ROOT / 'docs' / 'reddit.json'

_vader = SentimentIntensityAnalyzer()


def clean_company_name(name: str) -> str:
    """First words of the company name, minus legal-suffix noise."""
    words = []
    for w in str(name or '').split():
        if w.lower() in NAME_STOP_TOKENS:
            break
        words.append(w)
        if len(words) == 3:
            break
    out = ' '.join(words)
    return out if len(out) >= 4 else ''


def name_phrase(name: str) -> str:
    """The company-name phrase used for matching. Ampersand names stay whole
    ('Johnson & Johnson'), else 'Johnson' alone matches every headline about
    the House Speaker."""
    m = re.match(r'^([A-Za-z.\-]+(?: [A-Za-z.\-]+)? & [A-Za-z.\-]+)', str(name or ''))
    return m.group(1) if m else clean_company_name(name)


def build_query(ticker: str, name: str) -> str:
    """Search terms for one stock: $SYM, bare SYM when unambiguous, company name."""
    sym = ticker.split(':')[-1].upper()
    parts = []
    if sym.isalpha():
        parts.append(f'"${sym}"')
        if len(sym) >= 3 and sym not in WORDY_SYMBOLS:
            parts.append(f'"{sym}"')
    company = name_phrase(name)
    if company:
        parts.append(f'"{company}"')
    return ' OR '.join(parts)


def get_token() -> str | None:
    cid = os.environ.get('REDDIT_CLIENT_ID')
    secret = os.environ.get('REDDIT_CLIENT_SECRET')
    if not cid or not secret:
        return None
    req = urllib.request.Request(
        'https://www.reddit.com/api/v1/access_token',
        data=b'grant_type=client_credentials',
        headers={'Authorization': 'Basic ' + base64.b64encode(
                     f'{cid}:{secret}'.encode()).decode(),
                 'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)['access_token']


def search_posts(query: str, token: str | None) -> list[dict]:
    """One multireddit search; returns raw post dicts."""
    base = ('https://oauth.reddit.com' if token else 'https://www.reddit.com')
    url = (f'{base}/r/{"+".join(SUBREDDITS)}/search.json?'
           + urllib.parse.urlencode({'q': query, 'restrict_sr': 'true',
                                     'sort': 'top', 't': WINDOW,
                                     'limit': SEARCH_LIMIT}))
    headers = {'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = f'bearer {token}'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=20) as r:
        return [c['data'] for c in json.load(r)['data']['children']]


def is_relevant(p: dict, sym: str, company: str) -> bool:
    """A post counts only if its TITLE names the stock: the cashtag, the
    symbol as an exact ALL-CAPS token, or the company name. (Reddit search
    tokenizes away '$' and case, so a search for '$MAP' pulls every post
    containing the word 'map' — body mentions are too loose to trust.)"""
    title = p.get('title') or ''
    phrase = name_phrase(company)
    if phrase and len(phrase) >= 5:
        # single-word names ('Investor', 'Apple') must match case-sensitively,
        # else 'Investor AB' matches every title containing 'investors'
        flags = re.IGNORECASE if ' ' in phrase else 0
        if re.search(rf'\b{re.escape(phrase)}\b', title, flags):
            return True
    if sym.isalpha():
        if re.search(rf'\${re.escape(sym)}\b', title, re.IGNORECASE):
            return True
        if len(sym) >= 3 and re.search(rf'\b{re.escape(sym)}\b', title):
            return True     # case-sensitive: 'MAP announces...' yes, 'road map' no
    return False


def analyze(posts: list[dict], sym: str, company: str,
            now_ts: float | None = None) -> dict | None:
    """Sentiment summary + the top posts, or None if quiet.

    The gauge is weighted by upvotes AND recency (half-life HALF_LIFE_DAYS),
    so a year-old thread still shows in the receipts but barely moves the
    needle on *current* retail mood.
    """
    now_ts = now_ts or time.time()
    scored = []
    for p in posts:
        if p.get('score', 0) < MIN_UPS:
            continue
        if MEGATHREAD_RE.search(p.get('title') or ''):
            continue        # daily/weekly threads mention every ticker at once
        if not is_relevant(p, sym, company):
            continue
        text = (p.get('title') or '') + '. ' + (p.get('selftext') or '')[:280]
        sent = _vader.polarity_scores(text)['compound']
        created = p.get('created_utc')
        age_d = max(0, int((now_ts - created) / 86400)) if created else None
        scored.append({'t': (p.get('title') or '')[:140],
                       's': p.get('subreddit', ''),
                       'u': p.get('permalink', ''),
                       'ups': int(p.get('score', 0)),
                       'nc': int(p.get('num_comments', 0)),
                       'age_d': age_d,
                       'sent': round(sent, 2)})
    if not scored:
        return None
    weights = [(1 + math.log10(1 + p['ups']))
               * 0.5 ** ((p['age_d'] or 0) / HALF_LIFE_DAYS) for p in scored]
    score = sum(p['sent'] * w for p, w in zip(scored, weights)) / sum(weights)
    scored.sort(key=lambda p: p['ups'], reverse=True)
    return {'score': round(score, 2), 'mentions': len(scored),
            'posts': scored[:MAX_POSTS_STORED]}


NEAR_BY_MCAP = 60     # biggest near-breakout names also scanned (AAPL etc.)
NEAR_BY_VOLUME = 20   # plus the highest relative-volume near names


def pick_targets(data: dict) -> list[dict]:
    """All breakouts + the largest / most active near names, deduped."""
    near = data['near']
    picked: dict[str, dict] = {}
    for r in (list(data['breakouts'])
              + sorted(near, key=lambda r: -(r.get('mcap') or 0))[:NEAR_BY_MCAP]
              + sorted(near, key=lambda r: -(r.get('rel_volume') or 0))[:NEAR_BY_VOLUME]):
        picked.setdefault(r['ticker'], r)
    return list(picked.values())


def main() -> int:
    data = json.loads(DATA_JSON_PATH.read_text())
    targets = [(r['ticker'], r['symbol'], r['name']) for r in pick_targets(data)]
    if not targets:
        print('No breakouts today; nothing to search.')
        return 0

    try:
        token = get_token()
    except Exception as e:                                     # noqa: BLE001
        print(f'Reddit auth failed: {e}', file=sys.stderr)
        token = None
    print(f'Reddit auth: {"oauth" if token else "unauthenticated fallback"}')

    results, failures = {}, 0
    for ticker, sym, name in targets:
        q = build_query(ticker, name)
        if not q:
            continue
        try:
            summary = analyze(search_posts(q, token),
                              ticker.split(':')[-1].upper(), name)
            # scanned-but-quiet tickers get an empty entry so the dashboard can
            # distinguish "no chatter" from "not scanned"
            results[ticker] = summary or {'score': None, 'mentions': 0, 'posts': []}
        except urllib.error.HTTPError as e:
            failures += 1
            print(f'  {sym}: HTTP {e.code}', file=sys.stderr)
            if e.code in (401, 403) or failures >= 5:
                print('Reddit is refusing requests — keeping previous reddit.json.',
                      file=sys.stderr)
                return 0
            if e.code == 429:
                time.sleep(10)
        except Exception as e:                                 # noqa: BLE001
            failures += 1
            print(f'  {sym}: {e}', file=sys.stderr)
        time.sleep(0.8)                                        # stay polite

    REDDIT_JSON_PATH.write_text(json.dumps({
        'generated': data['run_date'], 'window': WINDOW, 'subs': SUBREDDITS,
        'results': results}, separators=(',', ':')))
    loud = sum(1 for r in results.values() if r['mentions'])
    print(f'Reddit sentiment: {loud}/{len(targets)} scanned tickers with chatter '
          f'-> {REDDIT_JSON_PATH.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
