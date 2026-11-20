"""Provider connectivity probing, live /models discovery, local server detection, and model preferences."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from filelock import FileLock

from .config import OFFICIAL, ConfigError, is_loopback_url, load_settings, render_config
from .credentials import redact
from .paths import mosscode_home

LOCAL_SERVERS: tuple[tuple[str, str, str, str], ...] = (
    ("ollama", "Ollama", "http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/api/tags"),
    ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1/models"),
)
MAX_RECENT_MODELS = 12
PREFS_FILE = "model_prefs.json"
PREFS_LOCK = "model_prefs.json.lock"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing an endpoint and optionally listing its available models."""

    ok: bool
    models: tuple[str, ...] = ()
    latency_ms: int = 0
    detail: str = ""
    error: str = ""
    status_code: int | None = None


@dataclass(frozen=True)
class DetectedLocalServer:
    """A running local OpenAI-compatible inference server discovered on loopback."""

    preset_key: str
    label: str
    base_url: str
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelPrefs:
    """User-level favorite and recently used provider/model keys."""

    favorites: tuple[str, ...] = ()
    recent: tuple[str, ...] = ()


def model_key(provider_name: str, model: str) -> str:
    """Canonical preference key: `<provider>/<model>`."""
    return f"{provider_name}/{model}"


def fuzzy_score(query: str, text: str) -> int | None:
    """Score how well `query` matches `text` (higher is better), or None when unmatched.

    Supports multi-token queries (every whitespace-separated token must match) and
    ranks exact/prefix/substring hits ahead of ordered character subsequences.
    """
    tokens = [tok for tok in query.casefold().split() if tok]
    if not tokens:
        return 0
    haystack = text.casefold()
    total = 0
    for token in tokens:
        if haystack == token:
            total += 1000
            continue
        idx = haystack.find(token)
        if idx != -1:
            # Prefer matches at start or after a separator ('/', '-', '_', ' ').
            boundary = 200 if idx == 0 or haystack[idx - 1] in "/-_ ." else 0
            total += 400 + boundary - min(idx, 120)
            continue
        pos = 0
        streak = 0
        sub_score = 0
        for ch in token:
            found = haystack.find(ch, pos)
            if found == -1:
                return None
            if found == pos:
                streak += 1
                sub_score += 12 + streak * 4
            else:
                streak = 0
                sub_score += 8 if found == 0 or haystack[found - 1] in "/-_ ." else 3
            pos = found + 1
        total += sub_score - min(pos, 100)
    return total


def _models_url(protocol: str, base_url: str | None) -> str:
    raw = (base_url or OFFICIAL[protocol]).rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("服务地址必须是以 http:// 或 https:// 开头的有效 URL。")
    if protocol == "anthropic-messages":
        path = parsed.path.rstrip("/")
        suffix = "/models" if path.endswith("/v1") else "/v1/models"
        return urlunsplit((parsed.scheme, parsed.netloc, path + suffix, "", ""))
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/models", "", ""))


def _ollama_tags_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.port == 11434 or is_loopback_url(base_url) and "ollama" in base_url.lower():
        return urlunsplit((parsed.scheme, parsed.netloc, "/api/tags", "", ""))
    return None


def _parse_model_ids(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raw_items = payload.get("models")
    if not isinstance(raw_items, list):
        return ()
    seen: dict[str, None] = {}
    for entry in raw_items:
        if isinstance(entry, dict):
            mid = entry.get("id") or entry.get("name") or entry.get("model")
        elif isinstance(entry, str):
            mid = entry
        else:
            mid = None
        if isinstance(mid, str) and mid.strip():
            seen.setdefault(mid.strip(), None)
    return tuple(seen)


async def probe_provider(
    protocol: str,
    base_url: str | None,
    api_key: str | None,
    *,
    auth_type: str = "api_key",
    headers: Mapping[str, str] | None = None,
    timeout: float = 8.0,
    transport: Any = None,
) -> ProbeResult:
    """Probe provider connectivity and discover available model IDs via GET /models."""
    import httpx

    if protocol not in OFFICIAL:
        return ProbeResult(False, error=f"不支持的协议：{protocol}")
    if auth_type == "none" and not is_loopback_url(base_url):
        return ProbeResult(False, error="auth_type: none 仅允许用于本机回环地址。")
    try:
        url = _models_url(protocol, base_url)
    except ValueError as exc:
        return ProbeResult(False, error=str(exc))

    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update({k: v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)})
    if auth_type != "none" and api_key:
        if protocol == "anthropic-messages":
            req_headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
            if auth_type == "bearer":
                req_headers["Authorization"] = f"Bearer {api_key}"
            else:
                req_headers["x-api-key"] = api_key
        else:
            req_headers["Authorization"] = f"Bearer {api_key}"

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True) as client:
            response = await client.get(url, headers=req_headers)
            elapsed = int((time.perf_counter() - started) * 1000)
            if response.status_code in {401, 403}:
                return ProbeResult(
                    False,
                    latency_ms=elapsed,
                    status_code=response.status_code,
                    error=f"认证失败（HTTP {response.status_code}），请检查 API 密钥或权限。",
                )
            if response.status_code == 200:
                try:
                    models = _parse_model_ids(response.json())
                except ValueError:
                    models = ()
                if not models and (tags_url := _ollama_tags_url(base_url)):
                    try:
                        tags_resp = await client.get(tags_url, headers={"Accept": "application/json"})
                        if tags_resp.status_code == 200:
                            models = _parse_model_ids(tags_resp.json())
                    except Exception:
                        pass
                detail = f"已连通（{elapsed}ms），发现 {len(models)} 个模型" if models else f"已连通（{elapsed}ms）"
                return ProbeResult(True, models=models, latency_ms=elapsed, detail=detail, status_code=200)
            if response.status_code in {404, 405, 501}:
                # Some compatible relays do not expose /models even though chat completions work.
                tags_url = _ollama_tags_url(base_url)
                if tags_url:
                    try:
                        tags_resp = await client.get(tags_url, headers={"Accept": "application/json"})
                        if tags_resp.status_code == 200:
                            models = _parse_model_ids(tags_resp.json())
                            detail = f"已连通（{elapsed}ms），发现 {len(models)} 个本地模型"
                            return ProbeResult(True, models=models, latency_ms=elapsed, detail=detail, status_code=200)
                    except Exception:
                        pass
                return ProbeResult(
                    True,
                    latency_ms=elapsed,
                    status_code=response.status_code,
                    detail=f"服务端可达（{elapsed}ms，该服务未提供 /models 列表，请手动选择或填写模型名）",
                )
            return ProbeResult(
                False,
                latency_ms=elapsed,
                status_code=response.status_code,
                error=f"服务端返回 HTTP {response.status_code}。",
            )
    except httpx.TimeoutException:
        elapsed = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, latency_ms=elapsed, error="连接超时，请检查网络或服务地址。")
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        safe = redact(f"{type(exc).__name__}: {exc}")
        if api_key:
            safe = safe.replace(api_key, "[REDACTED]")
        return ProbeResult(False, latency_ms=elapsed, error=f"无法连接服务：{safe}")


async def detect_local_servers(
    timeout: float = 0.5,
    *,
    transport: Any = None,
) -> list[DetectedLocalServer]:
    """Quickly probe known loopback inference servers (Ollama, LM Studio) in parallel."""
    import httpx

    async def _probe_one(preset_key: str, label: str, base_url: str, probe_url: str) -> DetectedLocalServer | None:
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False) as client:
                resp = await client.get(probe_url, headers={"Accept": "application/json"})
                if resp.status_code != 200:
                    return None
                models = _parse_model_ids(resp.json())
                return DetectedLocalServer(preset_key, label, base_url, models)
        except Exception:
            return None

    results = await asyncio.gather(*(_probe_one(*entry) for entry in LOCAL_SERVERS))
    return [item for item in results if item is not None]


class ModelPrefsStore:
    """Atomic, file-locked persistence for favorite and recently used models."""

    def __init__(self, home: Path | None = None) -> None:
        base = mosscode_home(home)
        self.path = base / PREFS_FILE
        self._lock_path = base / PREFS_LOCK

    def _read_unlocked(self) -> ModelPrefs:
        if not self.path.is_file():
            return ModelPrefs()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ModelPrefs()
        if not isinstance(raw, dict):
            return ModelPrefs()
        favs = tuple(x for x in raw.get("favorites", ()) if isinstance(x, str) and x.strip())
        recent = tuple(x for x in raw.get("recent", ()) if isinstance(x, str) and x.strip())
        return ModelPrefs(favorites=favs, recent=recent[:MAX_RECENT_MODELS])

    def _write_unlocked(self, prefs: ModelPrefs) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "favorites": list(prefs.favorites), "recent": list(prefs.recent[:MAX_RECENT_MODELS])},
            ensure_ascii=False,
            indent=2,
        )
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(payload + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def load(self) -> ModelPrefs:
        return self._read_unlocked()

    def toggle_favorite(self, provider_name: str, model: str) -> bool:
        """Toggle favorite status for `<provider>/<model>`; returns True if now favorited."""
        key = model_key(provider_name, model)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            prefs = self._read_unlocked()
            favs = list(prefs.favorites)
            if key in favs:
                favs.remove(key)
                added = False
            else:
                favs.insert(0, key)
                added = True
            self._write_unlocked(ModelPrefs(favorites=tuple(favs), recent=prefs.recent))
            return added

    def record_recent(self, provider_name: str, model: str) -> None:
        """Record `<provider>/<model>` at the front of the MRU list."""
        key = model_key(provider_name, model)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            prefs = self._read_unlocked()
            recent = [key, *(item for item in prefs.recent if item != key)][:MAX_RECENT_MODELS]
            self._write_unlocked(ModelPrefs(favorites=prefs.favorites, recent=tuple(recent)))


def _replace_default_in_text(original: str, provider_name: str, model: str | None) -> str | None:
    """Update `default_provider` (and optionally a non-follow provider's `model`) while preserving YAML comments."""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()

    # 1. Update or insert top-level default_provider.
    updated_default = False
    for idx, line in enumerate(lines):
        match = re.match(r"^default_provider:\s*([^#\r\n]*?)(\s*#.*)?$", line)
        if match:
            comment = match.group(2) or ""
            lines[idx] = f"default_provider: {provider_name}{comment}"
            updated_default = True
            break
    if not updated_default:
        insert_at = next((i for i, line in enumerate(lines) if re.match(r"^providers:\s*(#.*)?$", line)), 0)
        lines.insert(insert_at, f"default_provider: {provider_name}")

    # 2. Optionally update the target provider's `model:` inside block-style providers list.
    if model is not None:
        start = next((i for i, line in enumerate(lines) if re.match(r"^providers:\s*(#.*)?$", line)), None)
        if start is None:
            return None
        entry_ranges: list[tuple[int, int, int]] = []
        indent = None
        cur_start = None
        for idx in range(start + 1, len(lines)):
            line = lines[idx]
            stripped = line.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                continue
            depth = len(line) - len(stripped)
            if indent is None:
                if not stripped.startswith("- "):
                    return None
                indent = depth
            if depth < indent or (depth == indent and not stripped.startswith("- ")):
                break
            if depth == indent and stripped.startswith("- "):
                if cur_start is not None:
                    entry_ranges.append((cur_start, idx, indent))
                cur_start = idx
        if cur_start is not None and indent is not None:
            end_idx = len(lines)
            for idx in range(cur_start + 1, len(lines)):
                line = lines[idx]
                stripped = line.lstrip(" ")
                if stripped and not stripped.startswith("#") and (len(line) - len(stripped)) < indent:
                    end_idx = idx
                    break
            entry_ranges.append((cur_start, end_idx, indent))

        replaced_model = False
        for seg_start, seg_end, base_indent in entry_ranges:
            block_text = "\n".join(lines[seg_start:seg_end])
            # Match `name: <provider_name>` (quoted or unquoted).
            name_pat = rf"(?:^|\n)[ ]{{{base_indent}}}(?:-\s+|\s+)name:\s*['\"]?{re.escape(provider_name)}['\"]?\s*(?:#.*)?(?:\n|$)"
            if not re.search(name_pat, block_text):
                continue
            if re.search(rf"(?:^|\n)[ ]{{{base_indent}}}(?:-\s+|\s+)follow:\s*", block_text):
                # Do not override model on a follow provider unless explicitly written.
                replaced_model = True
                break
            field_indent = " " * (base_indent + 2)
            for idx in range(seg_start, seg_end):
                m_line = re.match(r"^([ ]*(?:-\s+)?)model:\s*([^#\r\n]*?)(\s*#.*)?$", lines[idx])
                if m_line:
                    prefix, _, comment = m_line.groups()
                    lines[idx] = f"{prefix}model: {model}{comment or ''}"
                    replaced_model = True
                    break
            if not replaced_model:
                # Preset entry without explicit model line: insert right after the `- ` start line.
                lines.insert(seg_start + 1, f"{field_indent}model: {model}")
                replaced_model = True
            break
        if not replaced_model:
            return None

    return newline.join(lines) + newline


def set_default_model(
    config_path: Path,
    provider_name: str,
    model: str | None = None,
    *,
    home: Path | None = None,
) -> tuple[bool, str]:
    """Persist `default_provider` (and optionally `model`) into `config_path` atomically."""
    import yaml

    if not config_path.is_file():
        return False, f"配置文件不存在：{config_path}"
    lock = FileLock(str(config_path.with_name(f".{config_path.name}.lock")))
    with lock:
        try:
            original = config_path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(original)
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            return False, f"无法读取配置文件：{exc}"
        if not isinstance(loaded, dict) or not isinstance(loaded.get("providers"), list):
            return False, "配置文件缺少有效的 providers 列表。"
        target_entry = next(
            (item for item in loaded["providers"] if isinstance(item, dict) and item.get("name") == provider_name),
            None,
        )
        if target_entry is None:
            return False, f"配置文件中没有名为 {provider_name} 的 Provider。"
        expected = dict(loaded)
        expected["default_provider"] = provider_name
        updated_providers = []
        for item in loaded["providers"]:
            if isinstance(item, dict) and item.get("name") == provider_name and model and not item.get("follow"):
                cloned = dict(item)
                cloned["model"] = model
                updated_providers.append(cloned)
            else:
                updated_providers.append(item)
        expected["providers"] = updated_providers

        rendered = _replace_default_in_text(
            original,
            provider_name,
            model if (model and not target_entry.get("follow")) else None,
        )
        kept = False
        if rendered is not None:
            try:
                kept = yaml.safe_load(rendered) == expected
            except yaml.YAMLError:
                kept = False
        if not kept:
            rendered = (
                "# MossCode 配置。\n"
                "# 字段说明见 config.example.yaml；追加 Provider：运行 moss setup --add，或在 providers 下再写一项。\n"
                + yaml.safe_dump(expected, sort_keys=False, allow_unicode=True)
            )
        has_plaintext = any(isinstance(item, dict) and "api_key" in item for item in expected["providers"])
        tmp = config_path.with_name(f".config-{secrets.token_hex(4)}.yaml.tmp")
        try:
            with tmp.open("x", encoding="utf-8", newline="") as stream:
                stream.write(rendered)
            if has_plaintext and os.name != "nt":
                tmp.chmod(0o600)
            load_settings(tmp, home)
            if not kept:
                shutil.copyfile(config_path, config_path.with_name(config_path.name + ".bak"))
            os.replace(tmp, config_path)
        except (ConfigError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            return False, f"写入默认配置失败：{exc}"
    shown = f"{provider_name}/{model}" if model else provider_name
    return True, f"已将 {shown} 设为默认模型（{config_path}）。"


def insert_provider_text(original: str, provider: dict[str, Any]) -> str | None:
    """`original` with `provider` added as the last item of its top-level block `providers:` list, or None.

    Editing the text, rather than dumping the parsed YAML again, keeps the user's comments, key order and quoting.
    """
    import yaml

    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"providers:\s*(#.*)?", line)), None)
    if start is None:
        return None
    indent, last = None, None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        depth = len(line) - len(stripped)
        if indent is None:
            if not stripped.startswith("- "):
                return None  # A flow sequence or something unexpected: leave it to the fallback.
            indent = depth
        if depth > indent or (depth == indent and stripped.startswith("- ")):
            last = index
            continue
        break
    if indent is None or last is None:
        return None
    item = yaml.safe_dump([provider], sort_keys=False, allow_unicode=True).splitlines()
    lines[last + 1 : last + 1] = [" " * indent + row for row in item]
    return newline.join(lines) + newline


def save_provider_entry(
    home: Path | None,
    provider: dict[str, Any],
    *,
    pending_key: str | None = None,
    set_default: bool = False,
    vault: Any = None,
    config_path: Path | None = None,
) -> tuple[Path, str]:
    """Create or append `provider` in user config atomically, validating via load_settings().

    Returns `(config_path, final_provider_name)`. Raises `ConfigError` or `OSError` on failure.
    """
    import yaml

    from .credentials import KeyringError, keyring_set

    path = config_path or (mosscode_home(home) / "config.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path.with_name(f".{path.name}.lock")))

    def _unique(base: str, taken: set[str]) -> str:
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    with lock:
        entry = dict(provider)
        if not path.exists():
            content = render_config(entry)
            tmp = path.with_name(f".config-{secrets.token_hex(4)}.yaml.tmp")
            try:
                with tmp.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                if "api_key" in entry and os.name != "nt":
                    tmp.chmod(0o600)
                load_settings(tmp, home)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            if pending_key is not None:
                try:
                    if vault is not None:
                        vault.store(entry["api_key_keyring"], pending_key)
                    else:
                        keyring_set(entry["api_key_keyring"], pending_key)
                except KeyringError as exc:
                    tmp.unlink(missing_ok=True)
                    raise ConfigError(f"无法保存密钥：{exc}") from None
            try:
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return path, str(entry["name"])

        original = path.read_text(encoding="utf-8")
        try:
            loaded = yaml.safe_load(original)
        except yaml.YAMLError as exc:
            raise ConfigError(f"无法解析现有配置 {path}：{exc}") from None
        if not isinstance(loaded, dict) or not isinstance(loaded.get("providers"), list) or not loaded["providers"]:
            raise ConfigError(f"现有配置 {path} 格式无效（缺少 providers 列表）。")
        settings = load_settings(path, home)
        existing_names = {item.name for item in settings.providers}
        entry["name"] = _unique(str(entry["name"]), existing_names)
        if "api_key_keyring" in entry:
            taken = existing_names | {
                item["api_key_keyring"]
                for item in loaded["providers"]
                if isinstance(item, dict) and isinstance(item.get("api_key_keyring"), str)
            }
            entry["api_key_keyring"] = _unique(str(entry["api_key_keyring"]), taken)

        expected = {**loaded, "providers": [*loaded["providers"], entry]}
        if set_default:
            expected["default_provider"] = entry["name"]

        # Preserve comments via block insert when possible.
        rendered = insert_provider_text(original, entry)
        if rendered is not None and set_default:
            rendered = _replace_default_in_text(rendered, str(entry["name"]), None)
        kept = False
        if rendered is not None:
            try:
                kept = yaml.safe_load(rendered) == expected
            except yaml.YAMLError:
                kept = False
        if not kept:
            rendered = (
                "# MossCode 配置。\n"
                "# 字段说明见 config.example.yaml；追加 Provider：运行 moss setup --add，或在 providers 下再写一项。\n"
                + yaml.safe_dump(expected, sort_keys=False, allow_unicode=True)
            )
        has_plaintext = any(isinstance(item, dict) and "api_key" in item for item in expected["providers"])
        tmp = path.with_name(f".config-{secrets.token_hex(4)}.yaml.tmp")
        try:
            with tmp.open("x", encoding="utf-8", newline="") as stream:
                stream.write(rendered)
            if has_plaintext and os.name != "nt":
                tmp.chmod(0o600)
            load_settings(tmp, home)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if pending_key is not None:
            try:
                if vault is not None:
                    vault.store(entry["api_key_keyring"], pending_key)
                else:
                    keyring_set(entry["api_key_keyring"], pending_key)
            except KeyringError as exc:
                tmp.unlink(missing_ok=True)
                raise ConfigError(f"无法保存密钥：{exc}") from None
        try:
            if not kept:
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return path, str(entry["name"])
