"""Each analyst's price target in the Tools > Analyst Ratings browser (asked 2026-09-24).

The rows come from FMP ``price-target-news``; the samples are the shape it returned for META.
"""
from ba2_trade_platform.ui.pages.tools import analyst_price_target_rows


def _news(**over):
    base = {'publishedDate': '2026-09-21T10:17:00.000Z', 'analystCompany': 'Wells Fargo',
            'analystName': 'Ken Gawrelski', 'priceTarget': 796, 'adjPriceTarget': 796,
            'priceWhenPosted': 665.75, 'newsPublisher': 'StreetInsider'}
    base.update(over)
    return base


def test_a_published_target_becomes_one_row_with_its_upside_at_the_time():
    row, = analyst_price_target_rows([_news()])
    assert row['date'] == '2026-09-21'
    assert (row['firm'], row['analyst']) == ('Wells Fargo', 'Ken Gawrelski')
    assert (row['target'], row['posted']) == ('$796.00', '$665.75')
    assert row['upside'] == '+19.6%' and row['upside_pct'] > 0


def test_missing_figures_are_dashes_never_zero():
    row, = analyst_price_target_rows([_news(analystName='', priceTarget=None, adjPriceTarget=None,
                                            priceWhenPosted=0)])
    assert (row['analyst'], row['target'], row['posted'], row['upside']) == ('—', '—', '—', '—')
    assert row['upside_pct'] is None


def test_an_unnamed_firm_falls_back_to_the_publisher():
    row, = analyst_price_target_rows([_news(analystCompany='', newsPublisher='TheFly')])
    assert row['firm'] == 'TheFly'


def test_a_split_adjusted_target_is_shown_adjusted_but_gets_no_upside():
    """The posted price is on the old basis, so a ratio against the adjusted target is wrong."""
    row, = analyst_price_target_rows([_news(priceTarget=1200, adjPriceTarget=120,
                                            priceWhenPosted=1000)])
    assert row['target'] == '$120.00' and row['upside'] == '—'


def test_rows_keep_a_unique_key_even_on_the_same_day():
    rows = analyst_price_target_rows([_news(), _news(analystCompany='UBS')])
    assert [r['key'] for r in rows] == [0, 1]


def test_no_data_is_no_rows():
    assert analyst_price_target_rows(None) == [] and analyst_price_target_rows([]) == []
