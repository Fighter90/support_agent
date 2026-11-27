from app.quotes import _tier


def test_tier_range_from_feed_nodes():
    assert _tier({'nodes': '50, 51, 52, 98, 99'}) == (50, 99)
    assert _tier({'nodes': '250, 499'}) == (250, 499)


def test_tier_without_range_allows_any_quantity():
    assert _tier({'nodes': ''}) == (1, 10 ** 6)
    assert _tier({'nodes': '1'}) == (1, 10 ** 6)
