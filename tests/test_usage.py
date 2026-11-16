import pytest

from mosscode.agent import add_usage
from mosscode.catalog import ModelInfo
from mosscode.messages import Usage, UsageTotals
from mosscode.usage import approx_tokens, cache_hit_rate, cost_text, parse_usage, request_cost


def info(input_price=3e-6, output_price=15e-6, read=3e-7, write=3.75e-6):
    return ModelInfo("m", "anthropic", 200_000, 64_000, input_price, output_price, read, write, True)


def test_anthropic_cost_splits_uncached_cache_read_cache_write_and_output():
    usage = parse_usage(
        {"input_tokens": 100, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200, "output_tokens": 50},
        "anthropic-messages",
    )
    assert request_cost(usage, info()) == pytest.approx(100 * 3e-6 + 1000 * 3e-7 + 200 * 3.75e-6 + 50 * 15e-6)


def test_openai_input_total_includes_cached_tokens():
    usage = parse_usage(
        {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 800}, "output_tokens": 10}, "openai-responses"
    )
    assert request_cost(usage, info()) == pytest.approx(200 * 3e-6 + 800 * 3e-7 + 10 * 15e-6)


def test_missing_cache_prices_charge_cached_tokens_as_input():
    usage = Usage(1000, 10, cached_input_tokens=800, cache_write_tokens=100)
    assert request_cost(usage, info(read=None, write=None)) == pytest.approx(1000 * 3e-6 + 10 * 15e-6)


@pytest.mark.parametrize(
    "usage,price",
    [(Usage(10, 5), None), (Usage(10, 5), info(input_price=None)), (Usage(None, 5), info()), (Usage(10, None), info())],
)
def test_no_cost_without_a_price_or_counts(usage, price):
    assert request_cost(usage, price) is None


def test_totals_and_their_text_say_when_the_cost_is_partial_or_unknown():
    total = add_usage(UsageTotals(), Usage(1000, 100), info(input_price=1e-6, output_price=2e-6))
    assert total.cost_usd == pytest.approx(0.0012) and total.missing_cost == 0
    assert cost_text(total) == "约 $0.0012"
    partial = add_usage(total, Usage(10, 10), None)
    assert partial.missing_cost == 1 and cost_text(partial) == "≥ $0.0012"
    assert cost_text(add_usage(UsageTotals(), Usage(10, 10))) == "无价格信息"
    big = add_usage(UsageTotals(), Usage(1_000_000, 0), info(input_price=3e-6))
    assert cost_text(big) == "约 $3.00"


def test_cache_hit_rate_needs_every_request_to_report_input_and_cache_reads():
    total = UsageTotals()
    assert cache_hit_rate(total) is None
    total = add_usage(total, Usage(8930, 11, 2944, 0))
    assert cache_hit_rate(total) == 33
    total = add_usage(total, Usage(9000, 20, 8900, 0))
    assert cache_hit_rate(total) == 66
    total = add_usage(total, Usage(100, 1))
    assert cache_hit_rate(total) is None
    zero = UsageTotals()
    zero = add_usage(zero, Usage(500, 5, 0, 0))
    assert cache_hit_rate(zero) == 0


def test_one_off_requests_count_in_usage_but_not_in_the_hit_rate():
    total = add_usage(UsageTotals(), Usage(10_000, 10, 9_000, 0))
    total = add_usage(total, Usage(6_000, 300, 0, 0), side=True)  # A summary or a note extraction.
    assert total.requests == 2 and total.input_tokens == 16_000 and total.cached_input_tokens == 9_000
    assert (total.side_requests, total.side_input_tokens, total.side_cached_input_tokens) == (1, 6_000, 0)
    assert cache_hit_rate(total) == 90
    only = add_usage(UsageTotals(), Usage(6_000, 300, 0, 0), side=True)
    assert cache_hit_rate(only) is None  # No conversation request yet: nothing to rate.


def test_approximate_token_counts():
    assert [approx_tokens(n) for n in (0, 999, 1000, 8930, 999_499, 999_500, 1_234_567, 2_000_000)] == [
        "0",
        "999",
        "1K",
        "9K",
        "999K",
        "1M",
        "1.2M",
        "2M",
    ]
