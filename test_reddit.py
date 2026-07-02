"""Unit tests for the pure logic in reddit_sentiment.py (no network)."""
from reddit_sentiment import analyze, build_query, clean_company_name, is_relevant


NOW = 1_800_000_000  # fixed clock for deterministic ages


def post(title, ups=10, selftext='', sub='stocks', nc=3, age_days=1):
    return {'title': title, 'selftext': selftext, 'score': ups,
            'subreddit': sub, 'permalink': '/r/x/1', 'num_comments': nc,
            'created_utc': NOW - age_days * 86400}


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


def test_relevance_rejects_tokenizer_false_positives():
    # Reddit's search matched these for $UNI / $MAP / $CON in the wild.
    assert not is_relevant(post('$META accepted defeat'), 'UNI', 'Unipol')
    assert not is_relevant(post('I made a road map for my investments'), 'MAP', 'Mapfre')
    assert not is_relevant(post('Could you be fooled by a con?'), 'CON', 'Concentra')


def test_relevance_accepts_real_mentions_in_title():
    assert is_relevant(post('$wbs is breaking out'), 'WBS', 'Webster Financial')
    assert is_relevant(post('MAP just reported record profits'), 'MAP', 'Mapfre')
    assert is_relevant(post('Mapfre looks undervalued', selftext='thesis…'), 'MAP', 'Mapfre')


def test_relevance_single_word_company_names_are_case_sensitive():
    # 'Investor AB' matched "...investors brace for..." in the wild
    assert not is_relevant(post('Samsung falls as investors brace for tariffs'),
                           'INVE_A', 'Investor')
    assert not is_relevant(post('every investor should know this'), 'INVE_A', 'Investor')
    assert is_relevant(post('Investor AB raises its stake'), 'INVE_A', 'Investor')
    assert is_relevant(post('Bayer Supreme Court YOLO'), 'BAYN', 'Bayer')
    # multi-word names stay case-insensitive
    assert is_relevant(post('loading up on webster financial'), 'WBS', 'Webster Financial')


def test_ampersand_names_match_as_whole_phrase():
    from reddit_sentiment import name_phrase
    assert name_phrase('Johnson & Johnson') == 'Johnson & Johnson'
    assert name_phrase('Procter & Gamble Company') == 'Procter & Gamble'
    assert name_phrase('Webster Financial Corporation') == 'Webster Financial'
    # the House Speaker is not a healthcare conglomerate
    assert not is_relevant(post('House Speaker Johnson on the shutdown'),
                           'JNJ', 'Johnson & Johnson')
    assert is_relevant(post('Johnson & Johnson beats on earnings'),
                       'JNJ', 'Johnson & Johnson')


def test_relevance_requires_title_not_body():
    # body-only mentions don't count — the title must name the stock
    assert not is_relevant(post('thoughts?', selftext='Loading up on Webster Financial'),
                           'WBS', 'Webster Financial')
    assert not is_relevant(post('my portfolio', selftext='$WBS is 20% of it'), 'WBS', '')


def test_analyze_filters_megathreads_and_weights_by_upvotes():
    r = analyze([
        post('WBS crushing it, amazing earnings, love this company', ups=500),
        post('WBS is a terrible awful disaster, avoid', ups=2),
        post('Weekly Earnings Thread 6/29 - 7/3', ups=999, selftext='$WBS $AAPL'),
    ], 'WBS', 'Webster Financial', now_ts=NOW)
    assert r['mentions'] == 2 and r['score'] > 0          # big post dominates, thread dropped
    assert r['posts'][0]['ups'] == 500                    # sorted by upvotes
    assert all(k in r['posts'][0] for k in ('t', 's', 'u', 'ups', 'nc', 'sent', 'age_d'))


def test_analyze_recency_outweighs_stale_giants():
    # An 11-month-old bullish megahit vs a fresh bearish post of equal upvotes:
    # the fresh post should dominate the gauge (half-life weighting).
    r = analyze([
        post('WBS is amazing, best bank, huge win', ups=800, age_days=330),
        post('WBS is a terrible awful disaster, avoid this bank', ups=800, age_days=2),
    ], 'WBS', '', now_ts=NOW)
    assert r['score'] < 0
    assert r['posts'][0]['age_d'] in (330, 2)             # ages recorded


def test_analyze_quiet_noise_and_irrelevant():
    assert analyze([], 'WBS', '', now_ts=NOW) is None
    assert analyze([post('WBS to the moon', ups=0)], 'WBS', '', now_ts=NOW) is None  # < MIN_UPS
    assert analyze([post('$META accepted defeat', ups=900)], 'UNI', 'Unipol',
                   now_ts=NOW) is None
