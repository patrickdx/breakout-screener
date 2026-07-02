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
WINDOW = 'week'
MAX_POSTS_STORED = 5      # per ticker, by upvotes
MIN_UPS = 2               # ignore sub-noise posts
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


def build_query(ticker: str, name: str) -> str:
    """Search terms for one stock: $SYM, bare SYM when unambiguous, company name."""
    sym = ticker.split(':')[-1].upper()
    parts = []
    if sym.isalpha():
        parts.append(f'"${sym}"')
        if len(sym) >= 3 and sym not in WORDY_SYMBOLS:
            parts.append(f'"{sym}"')
    company = clean_company_name(name)
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
                                     'sort': 'top', 't': WINDOW, 'limit': 25}))
    headers = {'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = f'bearer {token}'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=20) as r:
        return [c['data'] for c in json.load(r)['data']['children']]


def is_relevant(p: dict, sym: str, company: str) -> bool:
    """Reddit search tokenizes away '$' and case, so a search for '$MAP' pulls
    every post containing the word 'map'. Only keep posts that literally
    contain the cashtag, the symbol as an exact ALL-CAPS token, or the
    company name."""
    text = f"{p.get('title') or ''}\n{(p.get('selftext') or '')[:5000]}"
    if company and len(company) >= 5 and company.lower() in text.lower():
        return True
    if sym.isalpha():
        if re.search(rf'\${re.escape(sym)}\b', text, re.IGNORECASE):
            return True
        if len(sym) >= 3 and re.search(rf'\b{re.escape(sym)}\b', text):
            return True     # case-sensitive: 'MAP announces...' yes, 'road map' no
    return False


def analyze(posts: list[dict], sym: str, company: str) -> dict | None:
    """Upvote-weighted sentiment summary + the top posts, or None if quiet."""
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
        scored.append({'t': (p.get('title') or '')[:140],
                       's': p.get('subreddit', ''),
                       'u': p.get('permalink', ''),
                       'ups': int(p.get('score', 0)),
                       'nc': int(p.get('num_comments', 0)),
                       'sent': round(sent, 2)})
    if not scored:
        return None
    weights = [1 + math.log10(1 + p['ups']) for p in scored]
    score = sum(p['sent'] * w for p, w in zip(scored, weights)) / sum(weights)
    scored.sort(key=lambda p: p['ups'], reverse=True)
    return {'score': round(score, 2), 'mentions': len(scored),
            'posts': scored[:MAX_POSTS_STORED]}


def main() -> int:
    data = json.loads(DATA_JSON_PATH.read_text())
    targets = [(r['ticker'], r['symbol'], r['name']) for r in data['breakouts']]
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
                              ticker.split(':')[-1].upper(), clean_company_name(name))
            if summary:
                results[ticker] = summary
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
    print(f'Reddit sentiment: {len(results)}/{len(targets)} tickers with chatter '
          f'-> {REDDIT_JSON_PATH.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
