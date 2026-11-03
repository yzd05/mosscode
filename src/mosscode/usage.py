"""Normalize service-reported counters without inventing missing measurements."""

from .messages import Usage, UsageTotals


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


def accumulated(value: int, missing: int, requests: int, unknown: str = "未提供") -> str:
    if missing == requests:
        return unknown
    return ("≥" if missing else "") + str(value)


def cache_summary(total: UsageTotals) -> str:
    read = accumulated(total.cached_input_tokens, total.missing_cached, total.requests)
    text = f"缓存读 {read}"
    if total.missing_cache_write < total.requests:
        text += " / 写 " + accumulated(total.cache_write_tokens, total.missing_cache_write, total.requests)
    return text


def request_usage_text(usage: Usage, iteration: int) -> str:
    def show(value):
        return "未提供" if value is None else str(value)

    return (
        f"第 {iteration} 次请求 · Token 入 {show(usage.input_tokens)} / 出 {show(usage.output_tokens)}"
        f" · 缓存读 {show(usage.cached_input_tokens)} / 写 {show(usage.cache_write_tokens)}"
        f" · 未缓存输入 {show(usage.uncached_input_tokens)}"
    )
