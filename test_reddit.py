"""Unit tests for the pure logic in reddit_sentiment.py (no network)."""
from reddit_sentiment import analyze, build_query, clean_company_name


def post(title, ups=10, selftext='', sub='stocks', nc=3):
    return {'title': title, 'selftext': selftext, 'score': ups,
            'subreddit': sub, 'permalink': '/r/x/1', 'num_comments': nc}


def test_clean_company_name_strips_legal_suffixes():
    assert clean_company_name('Webster Financial Corporation') == 'Webster Financial'
    assert clean_company_name('GalaxyCore Inc. Class A') == 'GalaxyCore'
    assert clean_company_name('Coca-Cola Company (The)') == 'Coca-Cola'
    assert clean_company_name('SA') == ''            # too short after cleaning


def test_build_query_symbol_forms():
    q = build_query('NYSE:WBS', 'Webster Financial Corporation')
    assert '"$WBS"' in q and '"WBS"' in q and '"Webster Financial"' in q
    # wordy symbol: no bare form
    q = build_query('NASDAQ:OPEN', 'Opendoor Technologies Inc')
    assert '"$OPEN"' in q and ' "OPEN"' not in q
    # numeric Asian symbol: company name only
    q = build_query('TSE:3436', 'SUMCO Corporation')
    assert '$' not in q and '"SUMCO"' in q


def test_analyze_weights_by_upvotes_and_keeps_receipts():
    r = analyze([
        post('This company is amazing, great earnings, love it', ups=500),
        post('terrible awful stock, avoid this disaster', ups=2),
    ])
    assert r['mentions'] == 2 and r['score'] > 0          # big post dominates
    assert r['posts'][0]['ups'] == 500                    # sorted by upvotes
    assert all(k in r['posts'][0] for k in ('t', 's', 'u', 'ups', 'nc', 'sent'))


def test_analyze_quiet_and_noise_filtered():
    assert analyze([]) is None
    assert analyze([post('meh', ups=0)]) is None          # below MIN_UPS
