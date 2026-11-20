"""Provider connectivity probing, live /models discovery, local server detection, and model preferences."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .config import OFFICIAL, ConfigError, is_loopback_url, load_settings, render_config
from .paths import mosscode_home
from .redaction import COPILOT_URL_ERROR, is_approved_copilot_url, is_cleartext_remote_credential_url, redact

LOCAL_SERVERS: tuple[tuple[str, str, str, str], ...] = (
    ("ollama", "Ollama", "http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/api/tags"),
    ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1/models"),
)
MAX_RECENT_MODELS = 12
PREFS_FILE = "model_prefs.json"
PREFS_LOCK = "model_prefs.json.lock"
ANTHROPIC_VERSION = "2023-06-01"
CONFIG_LOCK_TIMEOUT = 10.0


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing an endpoint and optionally listing its available models."""

    ok: bool
    models: tuple[str, ...] = ()
    latency_ms: int = 0
    detail: str = ""
    error: str = ""
    status_code: int | None = None
    resolved_base_url: str | None = None


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
    """Collision-free canonical preference key keeping provider and model identities separate."""
    return json.dumps([provider_name, model], ensure_ascii=False, separators=(",", ":"))


def fuzzy_score(query: str, text: str) -> int | None:
    """Score how well `query` matches `text` (higher is better), or None when unmatched.

    Supports multi-token queries (every whitespace-separated token must match). Any query where
    all tokens match literally (exact/substring) strictly outranks subsequence matches regardless
    of query length.
    """
    tokens = [tok for tok in query.casefold().split() if tok]
    if not tokens:
        return 0
    haystack = text.casefold()
    all_literal = True
    literal_total = 0
    subseq_total = 0
    for token in tokens:
        if haystack == token:
            literal_total += 1_000_000
            continue
        idx = haystack.find(token)
        if idx != -1:
            # Prefer matches at start or after a separator ('/', '-', '_', ' ').
            boundary = 300_000 if idx == 0 or haystack[idx - 1] in "/-_ ." else 0
            literal_total += 200_000 + boundary - min(idx, 1000)
            continue
        all_literal = False
        pos = 0
        streak = 0
        sub_score = 0
        for ch in token:
            found = haystack.find(ch, pos)
            if found == -1:
                return None
            if found == pos:
                streak += 1
                sub_score += 12 + min(streak, 8) * 4
            else:
                streak = 0
                sub_score += 8 if found == 0 or haystack[found - 1] in "/-_ ." else 3
            pos = found + 1
        subseq_total += min(10_000, max(1, sub_score - min(pos, 100)))
    if all_literal:
        return 10_000_000 + literal_total
    # Cap mixed/subsequence score strictly below all-literal matches.
    return min(9_000_000, subseq_total + min(literal_total, 5_000_000))


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
    if not base_url or not is_loopback_url(base_url):
        return None
    parsed = urlsplit(base_url)
    if parsed.port == 11434 or "ollama" in base_url.lower():
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
    if auth_type == "github-copilot" and base_url is not None and not is_approved_copilot_url(base_url):
        return ProbeResult(False, error=COPILOT_URL_ERROR)
    try:
        url = _models_url(protocol, base_url)
    except ValueError as exc:
        return ProbeResult(False, error=str(exc))

    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        for k, v in headers.items():
            if isinstance(k, str) and isinstance(v, str):
                req_headers[k] = v
    if is_cleartext_remote_credential_url(
        url,
        auth_type=auth_type,
        headers=req_headers,
        has_api_key=bool(api_key),
    ):
        return ProbeResult(
            False,
            error="出于安全原因，向非本机回环的 HTTP 地址发送凭据的连通性探测已被拒绝，请改用 HTTPS（https://）。",
        )
    resolved_base_url: str | None = None
    if auth_type != "none" and api_key:
        if auth_type == "github-copilot":
            from .auth import COPILOT_HEADERS, AuthError, exchange_copilot_session_token_async

            for hk, hv in COPILOT_HEADERS:
                req_headers.setdefault(hk, hv)
            try:
                session_token, _, endpoint = await exchange_copilot_session_token_async(
                    api_key,
                    timeout=timeout,
                    transport=transport,
                )
            except AuthError as exc:
                return ProbeResult(False, error=str(exc))
            target_base = base_url or endpoint
            if not is_approved_copilot_url(target_base):
                return ProbeResult(False, error=COPILOT_URL_ERROR)
            resolved_base_url = target_base
            try:
                url = _models_url(protocol, target_base)
            except ValueError as exc:
                return ProbeResult(False, error=str(exc))
            req_headers["Authorization"] = f"Bearer {session_token}"
        elif protocol == "anthropic-messages":
            req_headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
            if auth_type == "bearer":
                req_headers["Authorization"] = f"Bearer {api_key}"
            else:
                req_headers["x-api-key"] = api_key
        else:
            req_headers["Authorization"] = f"Bearer {api_key}"

    if auth_type == "github-copilot" and not is_approved_copilot_url(url):
        return ProbeResult(False, error=COPILOT_URL_ERROR)

    # Never auto-follow redirects and never route loopback probes through environment proxies.
    trust_env = auth_type != "none" and not is_loopback_url(url)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=trust_env,
        ) as client:
            response = await client.get(url, headers=req_headers)
            elapsed = int((time.perf_counter() - started) * 1000)
            if 300 <= response.status_code < 400:
                return ProbeResult(
                    False,
                    latency_ms=elapsed,
                    status_code=response.status_code,
                    error=f"服务端返回重定向（HTTP {response.status_code}），为保护凭据与本地回环边界未自动跟随，请填写最终地址。",
                )
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
                return ProbeResult(
                    True,
                    models=models,
                    latency_ms=elapsed,
                    detail=detail,
                    status_code=200,
                    resolved_base_url=resolved_base_url,
                )
            if response.status_code in {404, 405, 501}:
                # Some compatible relays do not expose /models even though chat completions work.
                tags_url = _ollama_tags_url(base_url)
                if tags_url:
                    try:
                        tags_resp = await client.get(tags_url, headers={"Accept": "application/json"})
                        if tags_resp.status_code == 200:
                            models = _parse_model_ids(tags_resp.json())
                            detail = f"已连通（{elapsed}ms），发现 {len(models)} 个本地模型"
                            return ProbeResult(
                                True,
                                models=models,
                                latency_ms=elapsed,
                                detail=detail,
                                status_code=200,
                                resolved_base_url=resolved_base_url,
                            )
                    except Exception:
                        pass
                return ProbeResult(
                    True,
                    latency_ms=elapsed,
                    status_code=response.status_code,
                    detail=f"服务端可达（{elapsed}ms，该服务未提供 /models 列表，请手动选择或填写模型名）",
                    resolved_base_url=resolved_base_url,
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

    @staticmethod
    def _parse_key_list(raw_items: Any) -> tuple[str, ...]:
        if not isinstance(raw_items, list):
            return ()
        out: list[str] = []
        for item in raw_items:
            if isinstance(item, str) and item.strip():
                out.append(item)
            elif (
                isinstance(item, list)
                and len(item) == 2
                and all(isinstance(part, str) and part.strip() for part in item)
            ):
                out.append(model_key(item[0], item[1]))
        return tuple(out)

    def _read_unlocked(self) -> ModelPrefs:
        if not self.path.is_file():
            return ModelPrefs()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ModelPrefs()
        if not isinstance(raw, dict):
            return ModelPrefs()
        favs = self._parse_key_list(raw.get("favorites"))
        recent = self._parse_key_list(raw.get("recent"))
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
        with FileLock(str(self._lock_path), timeout=CONFIG_LOCK_TIMEOUT):
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
        with FileLock(str(self._lock_path), timeout=CONFIG_LOCK_TIMEOUT):
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


def config_lock_path(config_path: Path) -> Path:
    """Shared cross-process lock file for all writers touching `config_path`."""
    return config_path.with_name(f".{config_path.name}.lock")


def keyring_lock_path(home: Path | None = None) -> Path:
    """Shared cross-process lock serializing credential allocation/commit across all config files."""
    base = mosscode_home(home)
    base.mkdir(parents=True, exist_ok=True)
    return base / ".keyring.lock"


def write_temp_file(tmp: Path, content: str, *, private: bool) -> None:
    """Create `tmp` exclusively, enforcing 0600 permissions before writing bytes when `private` on POSIX."""
    if os.name != "nt" and private:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
        except BaseException:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        return
    with tmp.open("x", encoding="utf-8", newline="") as stream:
        stream.write(content)


def write_private_backup(source: Path, backup: Path) -> None:
    """Atomically back up `source` to `backup` with private 0600 permissions on POSIX before writing bytes."""
    data = source.read_bytes()
    tmp = backup.with_name(f".{backup.name}.{secrets.token_hex(4)}.tmp")
    try:
        if os.name != "nt":
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        else:
            with tmp.open("xb") as stream:
                stream.write(data)
        os.replace(tmp, backup)
    except BaseException:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _keyring_entry_exists(name: str, vault: Any) -> bool:
    """Fail closed: propagate credential backend lookup errors rather than treating unknown state as free."""
    if vault is not None:
        exists_fn = getattr(vault, "exists", None)
        if callable(exists_fn):
            return bool(exists_fn(name))
        return False
    from .credentials import keyring_get

    return keyring_get(name) is not None


def allocate_keyring_name(base: str, taken: set[str], vault: Any = None) -> str:
    """Allocate a keyring entry name unused by both `taken` YAML entries and the active credential store."""
    candidate = base
    suffix = 2
    while candidate in taken or _keyring_entry_exists(candidate, vault):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def rollback_keyring_entry(name: str, vault: Any = None) -> None:
    """Best-effort cleanup of a newly stored keyring entry if committing config fails."""
    if vault is not None:
        delete_fn = getattr(vault, "delete", None)
        if callable(delete_fn):
            with suppress(Exception):
                delete_fn(name)
        return
    from .credentials import keyring_delete

    with suppress(Exception):
        keyring_delete(name)


def set_default_model(
    config_path: Path,
    provider_name: str,
    model: str | None = None,
    *,
    home: Path | None = None,
    lock_timeout: float = CONFIG_LOCK_TIMEOUT,
) -> tuple[bool, str]:
    """Persist `default_provider` (and optionally `model`) into `config_path` atomically."""
    import yaml

    if not config_path.is_file():
        return False, f"配置文件不存在：{config_path}"
    lock = FileLock(str(config_lock_path(config_path)), timeout=lock_timeout)
    try:
        lock.acquire()
    except FileLockTimeout:
        return False, "配置文件正在被其他进程修改，请稍后重试。"
    except OSError as exc:
        return False, f"无法打开配置锁文件：{exc}"
    try:
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
            write_temp_file(tmp, rendered, private=has_plaintext)
            load_settings(tmp, home)
            if not kept:
                write_private_backup(config_path, config_path.with_name(config_path.name + ".bak"))
            os.replace(tmp, config_path)
        except (ConfigError, OSError) as exc:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            return False, f"写入默认配置失败：{exc}"
    finally:
        lock.release()
    persisted_model = model if (model and not target_entry.get("follow")) else None
    if model and target_entry.get("follow"):
        return True, f"已将 {provider_name} 设为默认提供商（{config_path}；follow 条目不写入模型覆盖 {model}）。"
    shown = f"{provider_name}/{persisted_model}" if persisted_model else provider_name
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
    extra_taken_names: set[str] | frozenset[str] | None = None,
    lock_timeout: float = CONFIG_LOCK_TIMEOUT,
) -> tuple[Path, str]:
    """Create or append `provider` in user config atomically, validating via load_settings().

    Returns `(config_path, final_provider_name)`. Raises `ConfigError` or `OSError` on failure.
    """
    import yaml

    from .credentials import KeyringError, keyring_set

    path = config_path or (mosscode_home(home) / "config.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _unique(base: str, taken: set[str]) -> str:
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def _stage_append(original: str, loaded: dict[str, Any], entry: dict[str, Any]) -> tuple[Path, bool]:
        expected = {**loaded, "providers": [*loaded["providers"], entry]}
        if set_default:
            expected["default_provider"] = entry["name"]
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
            write_temp_file(tmp, rendered, private=has_plaintext)
            load_settings(tmp, home)
        except BaseException:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        return tmp, kept

    def _stage_new(entry: dict[str, Any]) -> Path:
        content = render_config(entry)
        tmp = path.with_name(f".config-{secrets.token_hex(4)}.yaml.tmp")
        try:
            write_temp_file(tmp, content, private="api_key" in entry)
            load_settings(tmp, home)
        except BaseException:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        return tmp

    with ExitStack() as stack:
        try:
            stack.enter_context(FileLock(str(config_lock_path(path)), timeout=lock_timeout))
        except FileLockTimeout:
            raise ConfigError("配置文件正在被其他进程修改，请稍后重试。") from None

        entry = dict(provider)
        extra_taken = set(extra_taken_names or ())
        if not path.exists():
            original_name = str(entry["name"])
            if extra_taken:
                entry["name"] = _unique(original_name, extra_taken)
                if "api_key_keyring" in entry and str(entry["api_key_keyring"]) == original_name:
                    entry["api_key_keyring"] = str(entry["name"])
            # Validate draft BEFORE consulting or writing the credential backend.
            tmp = _stage_new(entry)
            stored_keyring: str | None = None
            if pending_key is not None:
                try:
                    stack.enter_context(FileLock(str(keyring_lock_path(home)), timeout=lock_timeout))
                except FileLockTimeout:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    raise ConfigError("凭据管理器正在被其他进程使用，请稍后重试。") from None
                except OSError:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    raise
                try:
                    allocated = allocate_keyring_name(str(entry["api_key_keyring"]), extra_taken, vault)
                except KeyringError as exc:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    raise ConfigError(f"无法读取系统凭据管理器：{exc}") from None
                if allocated != entry["api_key_keyring"]:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    entry["api_key_keyring"] = allocated
                    tmp = _stage_new(entry)
                try:
                    if vault is not None:
                        vault.store(entry["api_key_keyring"], pending_key)
                    else:
                        keyring_set(entry["api_key_keyring"], pending_key)
                    stored_keyring = str(entry["api_key_keyring"])
                except KeyringError as exc:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    raise ConfigError(f"无法保存密钥：{exc}") from None
            try:
                os.replace(tmp, path)
            except BaseException:
                if stored_keyring is not None:
                    rollback_keyring_entry(stored_keyring, vault)
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise
            return path, str(entry["name"])

        try:
            original = path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(original)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"无法解析现有配置 {path}：{exc}") from None
        if not isinstance(loaded, dict) or not isinstance(loaded.get("providers"), list) or not loaded["providers"]:
            raise ConfigError(f"现有配置 {path} 格式无效（缺少 providers 列表）。")
        settings = load_settings(path, home)
        existing_names = {item.name for item in settings.providers} | extra_taken
        original_name = str(entry["name"])
        entry["name"] = _unique(original_name, existing_names)
        taken: set[str] = set()
        keyring_base = ""
        if "api_key_keyring" in entry:
            taken = existing_names | {
                item["api_key_keyring"]
                for item in loaded["providers"]
                if isinstance(item, dict) and isinstance(item.get("api_key_keyring"), str)
            }
            keyring_base = (
                str(entry["name"]) if str(entry["api_key_keyring"]) == original_name else str(entry["api_key_keyring"])
            )
            entry["api_key_keyring"] = _unique(keyring_base, taken)

        # Validate candidate YAML BEFORE consulting or writing the credential store.
        tmp, kept = _stage_append(original, loaded, entry)
        stored_keyring = None
        if pending_key is not None:
            try:
                stack.enter_context(FileLock(str(keyring_lock_path(home)), timeout=lock_timeout))
            except FileLockTimeout:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise ConfigError("凭据管理器正在被其他进程使用，请稍后重试。") from None
            except OSError:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise
            try:
                allocated = allocate_keyring_name(keyring_base or str(entry["api_key_keyring"]), taken, vault)
            except KeyringError as exc:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise ConfigError(f"无法读取系统凭据管理器：{exc}") from None
            if allocated != entry["api_key_keyring"]:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
                entry["api_key_keyring"] = allocated
                tmp, kept = _stage_append(original, loaded, entry)
            try:
                if vault is not None:
                    vault.store(entry["api_key_keyring"], pending_key)
                else:
                    keyring_set(entry["api_key_keyring"], pending_key)
                stored_keyring = str(entry["api_key_keyring"])
            except KeyringError as exc:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise ConfigError(f"无法保存密钥：{exc}") from None
        try:
            if not kept:
                write_private_backup(path, path.with_name(path.name + ".bak"))
            os.replace(tmp, path)
        except BaseException:
            if stored_keyring is not None:
                rollback_keyring_entry(stored_keyring, vault)
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        return path, str(entry["name"])
