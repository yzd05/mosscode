"""`moss models`: search the model catalog, show what each provider matched, refresh the user's copy."""

import argparse
import io
import json
import sys
from pathlib import Path

import httpx

from .catalog import HOST_PROVIDERS, SOURCE, Catalog, ModelInfo, decode, encode, load_catalog, save, slim, tokens_label

LIMIT = 30


def download(url: str = SOURCE, *, transport: httpx.BaseTransport | None = None) -> bytes:
    # httpx reads HTTPS_PROXY and friends from the environment.
    with httpx.Client(transport=transport, timeout=60, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _price(value: float | None) -> str:
    return "—" if value is None else f"${value * 1_000_000:.4g}"


def _vision(value: bool | None) -> str:
    return "?" if value is None else "是" if value else "否"


def _table(rows: list[list[str]]) -> str:
    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, pad_edge=False, show_edge=False)
    for header in ("条目", "服务商", "窗口", "输出上限", "输入/百万", "输出/百万", "图片"):
        table.add_column(header, no_wrap=True)
    for row in rows:
        table.add_row(*row)
    console = Console(width=200, file=io.StringIO())
    console.print(table)
    return "\n".join(line.rstrip() for line in console.file.getvalue().splitlines())


def _row(info: ModelInfo) -> list[str]:
    return [
        info.name,
        info.provider,
        tokens_label(info.context_window),
        tokens_label(info.max_output_tokens),
        _price(info.input_price),
        _price(info.output_price),
        _vision(info.vision),
    ]


def _origin(catalog: Catalog) -> str:
    where = str(catalog.path) if catalog.path else "随程序发布"
    return f"模型目录：{len(catalog.models)} 个模型，数据日期 {catalog.fetched}（{where}）；来自 LiteLLM，MIT 许可。"


OFFICIAL = {provider for providers in HOST_PROVIDERS.values() for provider in providers}


def _rank(info: ModelInfo) -> tuple:
    # The model makers' own services first; resellers and cloud regions list the same models many times over.
    return info.provider not in OFFICIAL, info.name.count("/"), info.name


def search(keyword: str, catalog: Catalog) -> str:
    found = sorted(catalog.search(keyword), key=_rank)
    if not found:
        return f"目录中没有名字包含 {keyword} 的模型。"
    lines = [_table([_row(info) for info in found[:LIMIT]])]
    if len(found) > LIMIT:
        lines.append(f"另有 {len(found) - LIMIT} 个，请换一个更具体的关键词。")
    lines.append("把条目名填到 Provider 的 catalog_model，即可使用它的窗口、输出上限和价格。")
    return "\n".join(lines)


def matches(config: str | None) -> tuple[str, int]:
    from .config import ConfigError, find_config, load_settings

    try:
        path = Path(config) if config is not None else find_config()
        settings = load_settings(path)
    except ConfigError as exc:
        return f"无法读取配置：{exc}", 1
    lines = [f"配置 {path} 中的 Provider：", ""]
    for provider in settings.providers:
        info = provider.catalog
        if info is None:
            match = "目录中没有，按 128K 估算上下文"
        else:
            how = "catalog_model" if provider.catalog_model else "按模型名"
            match = f"{info.name}（{how}，窗口 {tokens_label(provider.context_window)}，输出上限 {tokens_label(provider.max_output_tokens)}）"
        lines.append(f"  {provider.name}：{provider.model} → {match}")
    lines += ["", "查找条目：moss models <关键词>"]
    return "\n".join(lines), 0


def update(home: Path | None = None, fetch=download) -> tuple[str, int]:
    try:
        raw = json.loads(fetch())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        blob = encode(slim(raw))
        fresh = decode(blob)
    except httpx.HTTPError as exc:
        return f"下载失败：{exc}；原有目录没有改动。", 1
    except (ValueError, TypeError) as exc:
        return f"下载的数据无法解析（{exc}）；原有目录没有改动。", 1
    if not fresh.models:
        return "下载的数据中没有聊天模型；原有目录没有改动。", 1
    old = load_catalog(home).models
    added = len(fresh.models.keys() - old.keys())
    removed = len(old.keys() - fresh.models.keys())
    changed = sum(1 for name, info in fresh.models.items() if name in old and old[name] != info)
    try:
        path = save(blob, home)
    except OSError as exc:
        return f"无法写入模型目录：{exc}", 1
    return (
        f"已更新模型目录：{len(fresh.models)} 个模型（新增 {added}，删除 {removed}，变化 {changed}），保存到 {path}",
        0,
    )


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(
        prog="moss models",
        description="查找模型目录中的窗口、输出上限和价格；moss models update 从 LiteLLM 下载最新数据。",
    )
    command.add_argument("keyword", nargs="?", help="条目名中包含的文字；不填时显示每个 Provider 匹配到的条目")
    command.add_argument("--config", help="读取这个配置文件，而不是按默认顺序查找")
    args = command.parse_args(argv)
    if args.keyword == "update":
        text, code = update()
    elif args.keyword:
        catalog = load_catalog()
        text, code = "\n".join(filter(None, [catalog.problem, search(args.keyword, catalog), _origin(catalog)])), 0
    else:
        catalog = load_catalog()
        text, code = matches(args.config)
        text = "\n".join(filter(None, [catalog.problem, text, _origin(catalog)]))
    try:
        print(text)
    except UnicodeEncodeError:  # A console that cannot show every character.
        print(text.encode(sys.stdout.encoding or "ascii", "replace").decode(sys.stdout.encoding or "ascii"))
    return code
