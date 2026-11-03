"""Search services behind web_search; each one maps its own API to a list of results."""

from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import WebSearchConfig


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class SearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SearchBackend(Protocol):
    name: str

    async def search(
        self, query: str, *, max_results: int, include_domains: list[str], exclude_domains: list[str]
    ) -> list[SearchResult]: ...


class TavilyBackend:
    name = "tavily"
    url = "https://api.tavily.com/search"

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0):
        self._key = api_key
        self._transport = transport
        self._timeout = timeout

    async def search(self, query, *, max_results, include_domains, exclude_domains):
        payload = {"query": query, "max_results": max_results, "search_depth": "basic"}
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(self.url, json=payload, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.TimeoutException:
            raise SearchError("search_failed", "搜索服务响应超时。") from None
        except httpx.HTTPError:
            raise SearchError("search_failed", "无法连接搜索服务。") from None
        # Only the status goes into messages: a body could echo the request, key included.
        if response.status_code in (401, 403):
            raise SearchError("search_auth", f"搜索服务拒绝了凭据（HTTP {response.status_code}），请检查 API 密钥。")
        if response.status_code == 429:
            raise SearchError("search_rate_limited", "搜索服务的请求次数或额度已用完（HTTP 429）。")
        if response.status_code != 200:
            raise SearchError("search_failed", f"搜索服务返回 HTTP {response.status_code}。")
        try:
            items = response.json().get("results", [])
            return [
                SearchResult(str(item.get("title") or ""), str(item["url"]), str(item.get("content") or ""))
                for item in items
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ]
        except (ValueError, AttributeError):
            raise SearchError("search_failed", "搜索服务返回的内容无法解析。") from None


BACKENDS = {"tavily": TavilyBackend}


def create_backend(config: WebSearchConfig) -> SearchBackend | None:
    """None when the key is missing; web_search is then not offered."""
    key = config.credential()
    return BACKENDS[config.provider](key) if key else None
