"""One model-call allowance shared by the agents of a local run."""

from contextlib import aclosing
from dataclasses import dataclass

from .messages import Completed, ProviderError


@dataclass
class RunBudget:
    requests: int = 0
    tokens: int = 0
    used_requests: int = 0
    used_tokens: int = 0
    unknown: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.requests or self.tokens)

    def enter(self) -> None:
        if self.requests and self.used_requests >= self.requests:
            raise ProviderError(f"本次运行已达到模型请求预算 {self.requests} 次。", code="budget_exhausted")
        if self.tokens and self.unknown:
            raise ProviderError(
                "服务未报告完整用量，无法继续核算 Token 预算；已停止新的模型请求。", code="budget_exhausted"
            )
        if self.tokens and self.used_tokens >= self.tokens:
            raise ProviderError(f"本次运行已达到 Token 预算 {self.tokens}。", code="budget_exhausted")
        # No await between checking and claiming: concurrent agents cannot claim the same request.
        self.used_requests += 1

    def record(self, usage) -> None:
        self.unknown |= usage.input_tokens is None or usage.output_tokens is None
        self.used_tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0)


async def stream(provider, messages, system, tools=(), *, allow_tools=True, limits=None):
    if limits is not None:
        limits.enter()
    completed = False
    try:
        async with aclosing(provider.stream(messages, system, tools, allow_tools=allow_tools)) as events:
            async for event in events:
                if isinstance(event, Completed):
                    completed = True
                    if limits is not None:
                        limits.record(event.usage)
                yield event
    finally:
        if limits is not None and not completed:
            limits.unknown = True
