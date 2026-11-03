"""Model facts (context window, output limit, prices, vision) from a snapshot of LiteLLM's model list.

Only the data is used, never the LiteLLM package. Lookups are exact: a near miss could give a window
twice too large or too small, which is worse than the plain default and a hint to configure it.
"""

import gzip
import json
import os
import secrets
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .paths import mosscode_home

SOURCE = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LICENSE = (
    "Model data from LiteLLM (https://github.com/BerriAI/litellm), MIT License, Copyright (c) 2023 Berri AI. "
    "Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated "
    'documentation files (the "Software"), to deal in the Software without restriction, including without '
    "limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the "
    "Software, and to permit persons to whom the Software is furnished to do so, subject to the following "
    "conditions: The above copyright notice and this permission notice shall be included in all copies or "
    'substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, '
    "EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR "
    "PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, "
    "DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN "
    "CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."
)
MODES = {"chat", "responses"}
# Kept per model, in this order.
FIELDS = (
    "litellm_provider",
    "max_input_tokens",
    "max_output_tokens",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "supports_vision",
)
FILE_NAME = "models.json.gz"
# Service host → LiteLLM provider. Hosts not listed fall back to the bare model name.
HOST_PROVIDERS = {
    "api.anthropic.com": ("anthropic",),
    "api.openai.com": ("openai",),
    "api.deepseek.com": ("deepseek",),
    "api.moonshot.cn": ("moonshot",),
    "api.moonshot.ai": ("moonshot",),
    "api.z.ai": ("zai",),
    "open.bigmodel.cn": ("bigmodel", "zai"),
    "dashscope.aliyuncs.com": ("dashscope",),
    "dashscope-intl.aliyuncs.com": ("dashscope",),
    "generativelanguage.googleapis.com": ("gemini",),
    "api.x.ai": ("xai",),
    "api.mistral.ai": ("mistral",),
    "api.minimax.io": ("minimax",),
    "api.minimaxi.com": ("minimax",),
    "ark.cn-beijing.volces.com": ("volcengine",),
    "localhost:11434": ("ollama",),
    "127.0.0.1:11434": ("ollama",),
}
PROTOCOL_PROVIDERS = {
    "anthropic-messages": ("anthropic",),
    "openai-responses": ("openai",),
    "openai-chat-completions": ("openai",),
}


@dataclass(frozen=True)
class ModelInfo:
    name: str  # The catalog entry, e.g. "deepseek/deepseek-v4-flash".
    provider: str
    context_window: int | None
    max_output_tokens: int | None
    input_price: float | None  # US dollars per token.
    output_price: float | None
    cache_read_price: float | None
    cache_write_price: float | None
    vision: bool | None  # None: the catalog does not say.

    @property
    def priced(self) -> bool:
        return self.input_price is not None and self.output_price is not None


@dataclass(frozen=True)
class Catalog:
    models: dict[str, ModelInfo]
    fetched: str  # Download date of the data, YYYY-MM-DD.
    path: Path | None  # The user's copy; None for the packaged snapshot.
    problem: str | None = None  # Why the user's copy was not used.

    def get(self, name: str) -> ModelInfo | None:
        return self.models.get(name)

    def search(self, keyword: str) -> list[ModelInfo]:
        keyword = keyword.casefold()
        return [info for name, info in self.models.items() if keyword in name.casefold()]


def _number(value) -> float | None:
    return float(value) if type(value) in (int, float) and value >= 0 else None


def _count(value) -> int | None:
    return int(value) if type(value) in (int, float) and value > 0 else None


def slim(raw: dict, fetched: str | None = None) -> dict:
    """LiteLLM's file reduced to chat models and the fields used here, with its source and license."""
    models = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or entry.get("mode") not in MODES or not isinstance(name, str):
            continue
        row = [entry.get(field) for field in FIELDS]
        if not isinstance(row[0], str):
            continue
        row[1:3] = [_count(value) for value in row[1:3]]
        row[3:7] = [_number(value) for value in row[3:7]]
        row[7] = row[7] if isinstance(row[7], bool) else None
        models[name] = row
    return {
        "source": SOURCE,
        "fetched": fetched or date.today().isoformat(),
        "license": LICENSE,
        "fields": FIELDS,
        "models": models,
    }


def encode(data: dict) -> bytes:
    # mtime=0 keeps the packaged snapshot byte-identical when the data has not changed.
    return gzip.compress(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), mtime=0)


def decode(blob: bytes, path: Path | None = None) -> Catalog:
    data = json.loads(gzip.decompress(blob).decode("utf-8"))
    if list(data.get("fields", ())) != list(FIELDS) or not isinstance(data.get("models"), dict):
        raise ValueError("unexpected layout")
    models = {name: ModelInfo(name, *row) for name, row in data["models"].items()}
    return Catalog(models, str(data.get("fetched", "")), path)


def user_path(home: Path | None = None) -> Path:
    return mosscode_home(home) / FILE_NAME


@lru_cache(maxsize=1)
def _packaged() -> Catalog:
    return decode((files("mosscode") / "data" / FILE_NAME).read_bytes())


@lru_cache(maxsize=4)
def _user(path: Path, stamp: tuple[int, int]) -> Catalog:
    return decode(path.read_bytes(), path)


def load_catalog(home: Path | None = None) -> Catalog:
    """The user's copy from `moss models update` when it exists and parses, otherwise the packaged one."""
    path = user_path(home)
    try:
        stat = path.stat()
    except OSError:
        return _packaged()
    try:
        return _user(path, (stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError, TypeError, EOFError):
        packaged = _packaged()
        problem = f"模型目录 {path} 无法读取，已改用随程序发布的目录；重新运行 moss models update 可修复。"
        return Catalog(packaged.models, packaged.fetched, None, problem)


def providers_for(base_url: str | None, protocol: str | None) -> tuple[str, ...]:
    if base_url:
        parts = urlsplit(base_url if "://" in base_url else f"https://{base_url}")
        netloc, host = parts.netloc.lower(), (parts.hostname or "").lower()
        return HOST_PROVIDERS.get(netloc) or HOST_PROVIDERS.get(host, ())
    return PROTOCOL_PROVIDERS.get(protocol or "", ())


def lookup(
    model: str,
    *,
    base_url: str | None = None,
    protocol: str | None = None,
    alias: str | None = None,
    catalog: Catalog | None = None,
) -> ModelInfo | None:
    """The entry for a configured model; alias (catalog_model) names the entry outright."""
    catalog = catalog or load_catalog()
    if alias:
        # The entry name as listed, or a bare name resolved like a model name.
        return catalog.get(alias) or lookup(alias, base_url=base_url, protocol=protocol, catalog=catalog)
    for provider in providers_for(base_url, protocol):
        info = catalog.get(f"{provider}/{model}")
        if info is None and (bare := catalog.get(model)) is not None and bare.provider == provider:
            info = bare
        if info is not None:
            return info
    if (info := catalog.get(model)) is not None:
        return info
    # "anthropic/claude-sonnet-5-5" through a gateway: the provider's own entry has no prefix.
    prefix, _, rest = model.partition("/")
    if rest and (info := catalog.get(rest)) is not None and info.provider == prefix:
        return info
    return None


def save(blob: bytes, home: Path | None = None) -> Path:
    """Replace the user's copy atomically; a failed write leaves the old one."""
    path = user_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{secrets.token_hex(4)}.tmp")
    try:
        partial.write_bytes(blob)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def tokens_label(count: int | None) -> str:
    """1000000 → 1M, 1048576 → 1M, 393216 → 384K, 128000 → 128K."""
    if count is None:
        return "?"
    for unit, sizes in (("M", (1_000_000, 1_048_576)), ("K", (1000, 1024))):
        for size in sizes:
            if count >= size and count % size == 0:
                return f"{count // size}{unit}"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.3g}M"
    return f"{count / 1000:.3g}K" if count >= 1000 else str(count)
