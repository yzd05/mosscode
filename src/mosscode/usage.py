"""Normalize service-reported counters without inventing missing measurements."""

from typing import TYPE_CHECKING

from .messages import Usage, UsageTotals

if TYPE_CHECKING:
    from .catalog import ModelInfo


def usage_data(value) -> dict:
    if value is None:
        return {}
    return value if isinstance(value, dict) else value.model_dump()


def counter(data: dict, name: str) -> int | None:
    value = data.get(name)
    return value if type(value) is int and value >= 0 else None


def parse_usage(value, protocol: str) -> Usage:
    data = usage_data(value)
    output = counter(data, "output_tokens" if protocol != "openai-chat-completions" else "completion_tokens")
    if protocol == "anthropic-messages":
        uncached = counter(data, "input_tokens")
        read = counter(data, "cache_read_input_tokens")
        write = counter(data, "cache_creation_input_tokens")
        if data.get("cache_read_input_tokens") is None and data.get("cache_creation_input_tokens") is None:
            total = uncached  # Older compatible endpoints may report only total input.
            uncached = None  # No reliable decomposition of that reported total.
        else:
            total = uncached + read + write if all(v is not None for v in (uncached, read, write)) else None
        return Usage(total, output, read, write, uncached)
    chat = protocol == "openai-chat-completions"
    total = counter(data, "prompt_tokens" if chat else "input_tokens")
    details = data.get("prompt_tokens_details" if chat else "input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    read = counter(details, "cached_tokens")
    write = counter(details, "cache_write_tokens")
    miss = None
    if chat:
        vendor_read = counter(data, "prompt_cache_hit_tokens")
        if read is None:
            read = vendor_read
        miss = counter(data, "prompt_cache_miss_tokens")
    return Usage(total, output, read, write, miss)


def price_of(provider) -> "ModelInfo | None":
    """The catalog entry behind a provider, which carries its prices."""
    return getattr(getattr(provider, "config", None), "catalog", None)


def request_cost(usage: Usage, info: "ModelInfo | None") -> float | None:
    """US dollars for one request at base prices; None when the price or the counts are unknown."""
    if info is None or not info.priced or usage.input_tokens is None or usage.output_tokens is None:
        return None
    read, write = usage.cached_input_tokens or 0, usage.cache_write_tokens or 0
    uncached = usage.uncached_input_tokens
    if uncached is None:
        uncached = max(usage.input_tokens - read - write, 0)
    # Without a cache price the cached tokens are charged as ordinary input, which errs high.
    read_price = info.input_price if info.cache_read_price is None else info.cache_read_price
    write_price = info.input_price if info.cache_write_price is None else info.cache_write_price
    return (
        uncached * info.input_price + read * read_price + write * write_price + usage.output_tokens * info.output_price
    )


def cost_text(total: UsageTotals) -> str:
    if total.missing_cost >= total.requests:
        return "无价格信息"
    amount = f"${total.cost_usd:.4f}" if total.cost_usd < 1 else f"${total.cost_usd:.2f}"
    return ("≥ " if total.missing_cost else "约 ") + amount


def cache_hit_rate(total: UsageTotals) -> int | None:
    """Percent of the conversation's input read from the prompt cache; None unless every request reported both.

    One-off requests (summaries, goal checks, note extraction) are left out: they cannot reuse the conversation's
    cache, and counting them would hide how well the conversation itself is cached.
    """
    requests = total.requests - total.side_requests
    tokens = total.input_tokens - total.side_input_tokens
    if not requests or total.missing_input or total.missing_cached or tokens <= 0:
        return None
    return round(100 * (total.cached_input_tokens - total.side_cached_input_tokens) / tokens)


def approx_tokens(count: int) -> str:
    """8930 → 9K, 1234567 → 1.2M: a rounded figure for estimates."""
    if count < 1000:
        return str(count)
    if count < 999_500:
        return f"{round(count / 1000)}K"
    return f"{count / 1_000_000:.1f}".removesuffix(".0") + "M"


def request_usage_text(usage: Usage, iteration: int) -> str:
    def show(value):
        return "未提供" if value is None else str(value)

    return (
        f"第 {iteration} 次请求 · Token 入 {show(usage.input_tokens)} / 出 {show(usage.output_tokens)}"
        f" · 缓存读 {show(usage.cached_input_tokens)} / 写 {show(usage.cache_write_tokens)}"
        f" · 未缓存输入 {show(usage.uncached_input_tokens)}"
    )
