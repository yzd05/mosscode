"""Local ephemeral tokenized Web configuration UI (`moss config --web`), bound strictly to 127.0.0.1."""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import secrets
import threading
import time
from contextlib import suppress
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import (
    AuthError,
    DeviceCodeChallenge,
    LoopbackCallbackServer,
    build_openrouter_auth_url,
    exchange_copilot_session_token_async,
    exchange_openrouter_code,
    generate_pkce_pair,
    persist_oauth_result,
    poll_copilot_device_token_once,
    start_copilot_device_flow,
)
from .config import OFFICIAL, ConfigError, is_loopback_url, load_settings
from .credentials import keyring_label, keyring_problem
from .model_discovery import detect_local_servers, probe_provider, save_provider_entry, set_default_model
from .paths import mosscode_home
from .presets import PRESETS
from .redaction import redact

MAX_BODY_BYTES = 64 * 1024
SESSION_COOKIE = "mosscode_web_session"


def _sanitize_provider(provider: Any) -> dict[str, Any]:
    """Return non-sensitive metadata for one configured ProviderConfig (never any key or secret header)."""
    if provider.follow:
        cred_kind = f"跟随 {provider.follow}"
    elif provider.auth_type == "none":
        cred_kind = "免密钥（本机回环）"
    elif provider.auth_type == "github-copilot":
        cred_kind = f"GitHub Copilot（{provider.api_key_keyring or provider.api_key_env or '配置文件'}）"
    elif provider.api_key_keyring:
        cred_kind = f"凭据管理器：{provider.api_key_keyring}"
    elif provider.api_key_env:
        cred_kind = f"环境变量：{provider.api_key_env}"
    elif provider.api_key:
        cred_kind = "配置文件明文密钥"
    else:
        cred_kind = "未配置"
    return {
        "name": provider.name,
        "protocol": provider.protocol,
        "model": provider.model,
        "base_url": provider.base_url or OFFICIAL.get(provider.protocol, ""),
        "auth_type": provider.auth_type,
        "preset": provider.preset,
        "follow": provider.follow,
        "credential_kind": cred_kind,
    }


def _render_web_page(csrf_token: str, nonce: str) -> bytes:
    """Self-contained offline HTML/CSS/JS configuration UI with CSP script nonce and CSRF meta token."""
    safe_csrf = html.escape(csrf_token, quote=True)
    safe_nonce = html.escape(nonce, quote=True)
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{safe_csrf}">
<title>MossCode 本地配置面板</title>
<style>
:root {{
  --bg:#0b1020;--card:#131b31;--card2:#1a2542;--border:#273558;
  --text:#e6edf7;--muted:#94a3b8;--accent:#38bdf8;--ok:#22c55e;--err:#ef4444;--warn:#f59e0b;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); line-height:1.5; }}
.wrap {{ max-width:960px; margin:0 auto; padding:24px 18px 56px; }}
header {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:20px; flex-wrap:wrap; }}
h1 {{ margin:0; font-size:22px; letter-spacing:.2px; }}
.badge {{ display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; background:var(--card2); color:var(--accent); border:1px solid var(--border); }}
.grid {{ display:grid; grid-template-columns:1fr; gap:16px; }}
@media (min-width:820px) {{ .grid-2 {{ grid-template-columns:1fr 1fr; }} }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px 20px; }}
.card h2 {{ margin:0 0 12px; font-size:16px; }}
.muted {{ color:var(--muted); font-size:13px; }}
.row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.provider-item {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px 12px; border:1px solid var(--border); border-radius:10px; background:var(--card2); margin-bottom:8px; }}
.provider-meta {{ font-size:12px; color:var(--muted); }}
label {{ display:block; font-size:13px; color:var(--muted); margin:10px 0 4px; }}
input, select {{ width:100%; padding:9px 11px; border-radius:8px; border:1px solid var(--border); background:#0d1426; color:var(--text); font-size:14px; }}
button {{ padding:8px 14px; border-radius:8px; border:1px solid var(--border); background:var(--card2); color:var(--text); cursor:pointer; font-size:13px; font-weight:500; }}
button.primary {{ background:#0284c7; border-color:#0284c7; color:#fff; }}
button.danger {{ background:#7f1d1d; border-color:#991b1b; color:#fee2e2; }}
button:disabled {{ opacity:.5; cursor:not-allowed; }}
.status {{ margin-top:10px; padding:10px 12px; border-radius:8px; font-size:13px; background:#0d1426; border:1px solid var(--border); white-space:pre-wrap; display:none; }}
.status.show {{ display:block; }}
.status.ok {{ border-color:var(--ok); color:#bbf7d0; }}
.status.err {{ border-color:var(--err); color:#fecaca; }}
.code-box {{ font-family:ui-monospace,Consolas,monospace; font-size:20px; letter-spacing:3px; padding:10px 14px; background:#0d1426; border:1px dashed var(--accent); border-radius:10px; display:inline-block; margin:8px 0; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>MossCode 本地配置中心</h1>
      <div class="muted">仅监听本机 127.0.0.1 · 会话令牌隔离 · 密钥写入系统凭据管理器</div>
    </div>
    <div class="row">
      <span class="badge" id="cfg-path">加载中…</span>
      <button class="danger" id="btn-shutdown">完成并关闭</button>
    </div>
  </header>

  <div class="grid">
    <section class="card">
      <h2>已配置的模型服务</h2>
      <div id="provider-list" class="muted">加载中…</div>
    </section>

    <div class="grid grid-2">
      <section class="card">
        <h2>浏览器一键登录（OAuth）</h2>
        <p class="muted">无需手动复制 API Key，授权完成后自动加密保存并写入用户配置。</p>
        <div class="row">
          <button class="primary" id="btn-or-login">OpenRouter OAuth 登录</button>
          <button class="primary" id="btn-cp-login">GitHub Copilot 设备码登录</button>
        </div>
        <div id="oauth-status" class="status"></div>
      </section>

      <section class="card">
        <h2>本机运行中的免密钥服务</h2>
        <p class="muted">自动探测本机 127.0.0.1 上的 Ollama / LM Studio。</p>
        <div id="local-list" class="muted">正在探测本机服务…</div>
      </section>
    </div>

    <section class="card">
      <h2>添加模型服务（厂商预设或自定义端点）</h2>
      <div class="grid grid-2">
        <div>
          <label for="f-preset">预设或自定义</label>
          <select id="f-preset"></select>
        </div>
        <div>
          <label for="f-name">Provider 名称</label>
          <input id="f-name" placeholder="例如 deepseek / my-gateway">
        </div>
        <div>
          <label for="f-protocol">协议</label>
          <select id="f-protocol">
            <option value="openai-chat-completions">OpenAI Chat Completions</option>
            <option value="anthropic-messages">Anthropic Messages</option>
            <option value="openai-responses">OpenAI Responses</option>
          </select>
        </div>
        <div>
          <label for="f-url">服务地址 base_url（远程凭据必须使用 https://）</label>
          <input id="f-url" placeholder="留空使用官方地址">
        </div>
        <div>
          <label for="f-cred-method">凭据方式</label>
          <select id="f-cred-method">
            <option value="keyring">系统凭据管理器（推荐）</option>
            <option value="env">环境变量名</option>
            <option value="none">免密钥（仅本机回环地址）</option>
            <option value="plain">直接写入配置文件（明文）</option>
          </select>
        </div>
        <div>
          <label for="f-secret" id="l-secret">API 密钥（不回显）</label>
          <input id="f-secret" type="password" autocomplete="off" placeholder="粘贴 API Key 或输入环境变量名">
        </div>
        <div>
          <label for="f-model">模型 ID</label>
          <input id="f-model" placeholder="可先点击探测拉取 /models">
        </div>
        <div>
          <label for="f-discovered">在线发现的模型</label>
          <select id="f-discovered"><option value="">（点击下方“探测连通性”后自动填充）</option></select>
        </div>
      </div>
      <div class="row" style="margin-top:14px">
        <button id="btn-probe">探测连通性与模型</button>
        <button class="primary" id="btn-save">保存配置</button>
        <button class="primary" id="btn-save-default">保存并设为默认</button>
      </div>
      <div id="form-status" class="status"></div>
    </section>
  </div>
</div>
<script nonce="{safe_nonce}">
const CSRF = document.querySelector('meta[name="csrf-token"]').content;
let STATE = null;

async function api(path, opts = {{}}) {{
  const headers = Object.assign({{'X-MossCode-Token': CSRF}}, opts.headers || {{}});
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, Object.assign({{credentials: 'same-origin'}}, opts, {{headers}}));
  const data = await res.json().catch(() => ({{}}));
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}}

function setStatus(id, text, kind = '') {{
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'status' + (text ? ' show ' + kind : '');
}}

function renderProviders() {{
  const box = document.getElementById('provider-list');
  box.textContent = '';
  const list = (STATE && STATE.providers) || [];
  if (!list.length) {{
    box.textContent = '尚未配置任何 Provider，请使用下方 OAuth 登录或表单添加。';
    return;
  }}
  for (const p of list) {{
    const row = document.createElement('div');
    row.className = 'provider-item';
    const left = document.createElement('div');
    const title = document.createElement('div');
    title.textContent = p.name + (p.name === STATE.default_provider ? '  ★ 默认' : '');
    const meta = document.createElement('div');
    meta.className = 'provider-meta';
    meta.textContent = p.protocol + ' / ' + p.model + ' · ' + p.base_url + ' · ' + p.credential_kind;
    left.append(title, meta);
    const btn = document.createElement('button');
    btn.textContent = p.name === STATE.default_provider ? '当前默认' : '设为默认';
    btn.disabled = p.name === STATE.default_provider;
    btn.onclick = async () => {{
      try {{
        const out = await api('/api/default', {{method:'POST', body: JSON.stringify({{provider: p.name}})}});
        setStatus('form-status', out.message || '已设为默认', 'ok');
        await refreshState();
      }} catch (e) {{
        setStatus('form-status', e.message, 'err');
      }}
    }};
    row.append(left, btn);
    box.append(row);
  }}
}}

function renderLocalServers() {{
  const box = document.getElementById('local-list');
  box.textContent = '';
  const list = (STATE && STATE.local_servers) || [];
  if (!list.length) {{
    box.textContent = '未检测到本机运行中的 Ollama 或 LM Studio。';
    return;
  }}
  for (const s of list) {{
    const row = document.createElement('div');
    row.className = 'provider-item';
    const left = document.createElement('div');
    const title = document.createElement('div');
    title.textContent = s.label + '（' + s.base_url + '）';
    const meta = document.createElement('div');
    meta.className = 'provider-meta';
    meta.textContent = s.models.length ? ('发现模型：' + s.models.slice(0, 4).join(', ')) : '运行中 · 免密钥';
    left.append(title, meta);
    const btn = document.createElement('button');
    btn.className = 'primary';
    btn.textContent = '一键添加';
    btn.onclick = async () => {{
      try {{
        await api('/api/providers', {{
          method: 'POST',
          body: JSON.stringify({{
            preset: s.preset_key,
            name: s.preset_key,
            protocol: 'openai-chat-completions',
            base_url: s.base_url,
            model: s.models[0] || 'llama3.1',
            cred_method: 'none',
            set_default: true
          }})
        }});
        setStatus('form-status', '已添加本机 ' + s.label + ' 并设为默认。', 'ok');
        await refreshState();
      }} catch (e) {{
        setStatus('form-status', e.message, 'err');
      }}
    }};
    row.append(left, btn);
    box.append(row);
  }}
}}

function fillPresets() {{
  const sel = document.getElementById('f-preset');
  sel.textContent = '';
  const customOpt = document.createElement('option');
  customOpt.value = '';
  customOpt.textContent = '自定义服务';
  sel.append(customOpt);
  for (const p of (STATE && STATE.presets) || []) {{
    const opt = document.createElement('option');
    opt.value = p.key;
    opt.textContent = p.label + ' (' + p.key + ')';
    sel.append(opt);
  }}
}}

function syncCredInput() {{
  const method = document.getElementById('f-cred-method').value;
  const label = document.getElementById('l-secret');
  const inp = document.getElementById('f-secret');
  if (method === 'env') {{
    label.textContent = '环境变量名（不要填密钥本身）';
    inp.type = 'text';
    inp.disabled = false;
  }} else if (method === 'none') {{
    label.textContent = '免密钥（仅本机回环地址）';
    inp.value = '';
    inp.disabled = true;
  }} else {{
    label.textContent = method === 'keyring' ? 'API 密钥（存入系统凭据管理器）' : 'API 密钥（明文写入配置文件）';
    inp.type = 'password';
    inp.disabled = false;
  }}
}}

document.getElementById('f-cred-method').onchange = syncCredInput;
document.getElementById('f-preset').onchange = () => {{
  const key = document.getElementById('f-preset').value;
  const p = ((STATE && STATE.presets) || []).find(x => x.key === key);
  if (!p) return;
  document.getElementById('f-name').value = p.key;
  document.getElementById('f-protocol').value = p.protocol;
  document.getElementById('f-url').value = p.base_url || '';
  document.getElementById('f-model').value = p.model;
  document.getElementById('f-cred-method').value = p.auth_type === 'none' ? 'none' : (STATE.keyring_ok ? 'keyring' : 'env');
  if (document.getElementById('f-cred-method').value === 'env') {{
    document.getElementById('f-secret').value = p.api_key_env || '';
  }} else {{
    document.getElementById('f-secret').value = '';
  }}
  syncCredInput();
}};
document.getElementById('f-discovered').onchange = (e) => {{
  if (e.target.value) document.getElementById('f-model').value = e.target.value;
}};

async function refreshState() {{
  STATE = await api('/api/state');
  document.getElementById('cfg-path').textContent = STATE.config_path;
  renderProviders();
  renderLocalServers();
  if (!document.getElementById('f-preset').options.length) fillPresets();
  syncCredInput();
}}

document.getElementById('btn-probe').onclick = async () => {{
  setStatus('form-status', '正在探测连通性并拉取 /models…');
  try {{
    const method = document.getElementById('f-cred-method').value;
    const out = await api('/api/probe', {{
      method: 'POST',
      body: JSON.stringify({{
        protocol: document.getElementById('f-protocol').value,
        base_url: document.getElementById('f-url').value.trim() || null,
        cred_method: method,
        secret: document.getElementById('f-secret').value.trim()
      }})
    }});
    const sel = document.getElementById('f-discovered');
    sel.textContent = '';
    const models = out.models || [];
    if (models.length) {{
      for (const m of models.slice(0, 60)) {{
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        sel.append(opt);
      }}
      if (!document.getElementById('f-model').value.trim()) {{
        document.getElementById('f-model').value = models[0];
      }}
    }} else {{
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（未返回模型列表，请手动填写）';
      sel.append(opt);
    }}
    setStatus('form-status', out.ok ? ('✓ ' + out.detail) : ('! ' + out.error), out.ok ? 'ok' : 'err');
  }} catch (e) {{
    setStatus('form-status', e.message, 'err');
  }}
}};

async function saveForm(setDefault) {{
  setStatus('form-status', '正在校验并保存配置…');
  try {{
    const out = await api('/api/providers', {{
      method: 'POST',
      body: JSON.stringify({{
        preset: document.getElementById('f-preset').value || null,
        name: document.getElementById('f-name').value.trim(),
        protocol: document.getElementById('f-protocol').value,
        base_url: document.getElementById('f-url').value.trim() || null,
        model: document.getElementById('f-model').value.trim(),
        cred_method: document.getElementById('f-cred-method').value,
        secret: document.getElementById('f-secret').value.trim(),
        set_default: setDefault
      }})
    }});
    document.getElementById('f-secret').value = '';
    setStatus('form-status', '✓ 已保存 Provider ' + out.name + ' 到 ' + out.path, 'ok');
    await refreshState();
  }} catch (e) {{
    setStatus('form-status', e.message, 'err');
  }}
}}
document.getElementById('btn-save').onclick = () => saveForm(false);
document.getElementById('btn-save-default').onclick = () => saveForm(true);

document.getElementById('btn-or-login').onclick = async () => {{
  setStatus('oauth-status', '正在启动 OpenRouter PKCE 授权…');
  try {{
    const start = await api('/api/auth/openrouter/start', {{method:'POST', body: JSON.stringify({{set_default:true}})}});
    window.open(start.auth_url, '_blank', 'noopener');
    setStatus('oauth-status', '已打开 OpenRouter 授权页；若未自动打开，请手动访问：\\n' + start.auth_url);
    const poll = async () => {{
      const st = await api('/api/auth/openrouter/status?job=' + encodeURIComponent(start.job_id));
      if (st.status === 'pending') {{
        setTimeout(poll, 1200);
      }} else if (st.status === 'ok') {{
        setStatus('oauth-status', '✓ OpenRouter 登录成功，已保存 Provider ' + st.name, 'ok');
        await refreshState();
      }} else {{
        setStatus('oauth-status', '! ' + (st.error || '登录失败'), 'err');
      }}
    }};
    setTimeout(poll, 1200);
  }} catch (e) {{
    setStatus('oauth-status', e.message, 'err');
  }}
}};

document.getElementById('btn-cp-login').onclick = async () => {{
  setStatus('oauth-status', '正在请求 GitHub Copilot 设备码…');
  try {{
    const start = await api('/api/auth/copilot/start', {{method:'POST', body: JSON.stringify({{}})}});
    const box = document.getElementById('oauth-status');
    box.textContent = '';
    box.className = 'status show';
    const p1 = document.createElement('div');
    p1.textContent = '请访问 GitHub 设备验证页并输入以下验证码：';
    const code = document.createElement('div');
    code.className = 'code-box';
    code.textContent = start.user_code;
    const link = document.createElement('a');
    link.href = start.verification_uri;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.style.color = '#38bdf8';
    link.textContent = start.verification_uri;
    box.append(p1, code, document.createElement('br'), link);
    let intervalMs = Math.max(2000, (start.interval || 5) * 1000);
    const poll = async () => {{
      try {{
        const st = await api('/api/auth/copilot/poll', {{
          method:'POST',
          body: JSON.stringify({{job_id: start.job_id, set_default: true}})
        }});
        if (st.status === 'pending') {{
          setTimeout(poll, intervalMs);
        }} else if (st.status === 'slow_down') {{
          intervalMs += 5000;
          setTimeout(poll, intervalMs);
        }} else if (st.status === 'ok') {{
          setStatus('oauth-status', '✓ GitHub Copilot 登录成功，已保存 Provider ' + st.name, 'ok');
          await refreshState();
        }} else {{
          setStatus('oauth-status', '! ' + (st.error || '登录失败'), 'err');
        }}
      }} catch (err) {{
        setStatus('oauth-status', '! ' + err.message, 'err');
      }}
    }};
    setTimeout(poll, intervalMs);
  }} catch (e) {{
    setStatus('oauth-status', e.message, 'err');
  }}
}};

document.getElementById('btn-shutdown').onclick = async () => {{
  try {{
    await api('/api/shutdown', {{method:'POST', body:'{{}}'}});
    document.body.textContent = 'MossCode 本地配置服务已关闭，现在可以关闭此标签页。';
  }} catch (e) {{
    setStatus('form-status', e.message, 'err');
  }}
}};

refreshState().catch(e => setStatus('form-status', '初始化失败：' + e.message, 'err'));
</script>
</body>
</html>
"""
    return page.encode("utf-8")


class ConfigWebServer:
    """Loopback-only tokenized local Web server for configuring MossCode providers."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        port: int = 0,
        vault: Any = None,
        transport: Any = None,
        idle_timeout: float = 1800.0,
    ) -> None:
        self.home = home
        self.config_path = mosscode_home(home) / "config.yaml"
        self.vault = vault
        self.transport = transport
        self.idle_timeout = float(idle_timeout)
        self.boot_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(24)
        self._boot_token_used = False
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._stop_event = threading.Event()
        self._closed = False
        self._or_jobs: dict[str, dict[str, Any]] = {}
        self._cp_jobs: dict[str, DeviceCodeChallenge] = {}
        self._server = self._build_server(port)
        self.port = int(self._server.server_address[1])
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        self.url = f"http://127.0.0.1:{self.port}/?token={self.boot_token}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="mosscode-config-web",
            daemon=True,
        )

    def _touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def _valid_host(self, host_header: str | None) -> bool:
        return bool(host_header and host_header.strip().lower() in self.allowed_hosts)

    def _valid_origin(self, origin_header: str | None, sec_fetch_site: str | None = None) -> bool:
        if sec_fetch_site and sec_fetch_site.strip().lower() not in {"same-origin", "none"}:
            return False
        if not origin_header:
            return True
        return origin_header.strip().lower() in self.allowed_origins

    def _has_valid_session_cookie(self, cookie_header: str | None) -> bool:
        if not cookie_header:
            return False
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:
            return False
        morsel = jar.get(SESSION_COOKIE)
        return morsel is not None and hmac.compare_digest(morsel.value, self.session_token)

    def _authorized_api(self, headers: Any) -> bool:
        auth = headers.get("Authorization")
        if isinstance(auth, str) and auth.startswith("Bearer "):
            token = auth[len("Bearer ") :].strip()
            with self._lock:
                # Once exchanged into a browser session cookie, the one-time boot token is retired.
                if (not self._boot_token_used) and hmac.compare_digest(token, self.boot_token):
                    return True
        csrf = headers.get("X-MossCode-Token")
        if not isinstance(csrf, str) or not hmac.compare_digest(csrf, self.csrf_token):
            return False
        return self._has_valid_session_cookie(headers.get("Cookie"))

    def _state_payload(self) -> dict[str, Any]:
        providers_out: list[dict[str, Any]] = []
        default_provider: str | None = None
        if self.config_path.is_file():
            with suppress(ConfigError, OSError):
                settings = load_settings(self.config_path, self.home)
                default_provider = settings.default_provider
                providers_out = [_sanitize_provider(p) for p in settings.providers]
        local = asyncio.run(detect_local_servers(timeout=0.35, transport=self.transport))
        problem = (
            self.vault.problem()
            if (self.vault is not None and callable(getattr(self.vault, "problem", None)))
            else keyring_problem()
        )
        return {
            "config_path": str(self.config_path),
            "default_provider": default_provider,
            "providers": providers_out,
            "keyring_ok": problem is None,
            "keyring_label": keyring_label() if problem is None else "",
            "keyring_problem": problem or "",
            "presets": [
                {
                    "key": p.key,
                    "label": p.label,
                    "protocol": p.protocol,
                    "base_url": p.base_url or "",
                    "model": p.model,
                    "api_key_env": p.api_key_env,
                    "auth_type": p.auth_type or "api_key",
                }
                for p in PRESETS.values()
            ],
            "local_servers": [
                {
                    "preset_key": srv.preset_key,
                    "label": srv.label,
                    "base_url": srv.base_url,
                    "models": list(srv.models),
                }
                for srv in local
            ],
        }

    def _handle_probe(self, body: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        protocol = str(body.get("protocol") or "").strip()
        base_url = body.get("base_url")
        if isinstance(base_url, str):
            base_url = base_url.strip() or None
        cred_method = str(body.get("cred_method") or "keyring").strip()
        secret = str(body.get("secret") or "").strip()
        auth_type = "none" if cred_method == "none" else "api_key"
        api_key: str | None = None
        if cred_method == "env":
            import os

            api_key = os.environ.get(secret) if secret else None
        elif cred_method in {"keyring", "plain"}:
            api_key = secret or None
        if auth_type != "none" and not api_key:
            return HTTPStatus.OK, {"ok": True, "models": [], "detail": "未提供立即可用的密钥，已跳过在线探测。"}
        res = asyncio.run(
            probe_provider(
                protocol,
                base_url,
                api_key,
                auth_type=auth_type,
                timeout=8.0,
                transport=self.transport,
            )
        )
        return HTTPStatus.OK, {
            "ok": res.ok,
            "models": list(res.models),
            "latency_ms": res.latency_ms,
            "detail": res.detail,
            "error": res.error,
        }

    def _handle_save_provider(self, body: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        preset_key = body.get("preset")
        name = str(body.get("name") or "").strip()
        protocol = str(body.get("protocol") or "").strip()
        base_url = body.get("base_url")
        if isinstance(base_url, str):
            base_url = base_url.strip() or None
        model = str(body.get("model") or "").strip()
        cred_method = str(body.get("cred_method") or "keyring").strip()
        secret = str(body.get("secret") or "").strip()
        set_default = bool(body.get("set_default", False))

        if not name or not model:
            return HTTPStatus.BAD_REQUEST, {"error": "Provider 名称和模型名不能为空。"}
        if cred_method not in {"keyring", "env", "plain", "none"}:
            return HTTPStatus.BAD_REQUEST, {"error": "不支持的凭据保存方式。"}
        if cred_method == "none" and not is_loopback_url(base_url):
            return HTTPStatus.BAD_REQUEST, {"error": "免密钥模式仅允许用于本机回环地址。"}

        entry: dict[str, Any] = {"name": name, "model": model}
        if isinstance(preset_key, str) and preset_key in PRESETS:
            preset = PRESETS[preset_key]
            entry["preset"] = preset_key
            if base_url and base_url != preset.base_url:
                entry["base_url"] = base_url
        else:
            if not protocol:
                return HTTPStatus.BAD_REQUEST, {"error": "自定义服务必须指定 protocol。"}
            entry["protocol"] = protocol
            if base_url:
                entry["base_url"] = base_url

        pending_key: str | None = None
        if cred_method == "none":
            entry["auth_type"] = "none"
        elif cred_method == "keyring":
            if not secret:
                return HTTPStatus.BAD_REQUEST, {"error": "API 密钥不能为空。"}
            problem = (
                self.vault.problem()
                if (self.vault is not None and callable(getattr(self.vault, "problem", None)))
                else keyring_problem()
            )
            if problem is not None:
                return HTTPStatus.BAD_REQUEST, {"error": f"系统凭据管理器不可用：{problem}"}
            entry["api_key_keyring"] = name
            pending_key = secret
        elif cred_method == "env":
            if not secret:
                return HTTPStatus.BAD_REQUEST, {"error": "请填写环境变量名。"}
            entry["api_key_env"] = secret
        else:
            if not secret:
                return HTTPStatus.BAD_REQUEST, {"error": "API 密钥不能为空。"}
            entry["api_key"] = secret

        try:
            saved_path, saved_name = save_provider_entry(
                self.home,
                entry,
                pending_key=pending_key,
                set_default=set_default,
                vault=self.vault,
                config_path=self.config_path,
            )
        except (ConfigError, OSError) as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        return HTTPStatus.OK, {"ok": True, "name": saved_name, "path": str(saved_path)}

    def _handle_set_default(self, body: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        provider = str(body.get("provider") or "").strip()
        model = body.get("model")
        model_arg = str(model).strip() if isinstance(model, str) and model.strip() else None
        if not provider:
            return HTTPStatus.BAD_REQUEST, {"error": "缺少 provider 名称。"}
        ok, msg = set_default_model(self.config_path, provider, model_arg, home=self.home)
        return (HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST), {"ok": ok, "message": msg, "error": "" if ok else msg}

    def _handle_openrouter_start(self, body: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        problem = (
            self.vault.problem()
            if (self.vault is not None and callable(getattr(self.vault, "problem", None)))
            else keyring_problem()
        )
        if problem is not None:
            return HTTPStatus.BAD_REQUEST, {"error": f"系统凭据管理器不可用（{problem}），无法安全保存 OAuth 密钥。"}

        set_default = bool(body.get("set_default", True))
        verifier, challenge = generate_pkce_pair()
        callback = LoopbackCallbackServer().start()
        auth_url = build_openrouter_auth_url(callback.callback_url, challenge)
        job_id = secrets.token_urlsafe(12)
        job: dict[str, Any] = {"status": "pending", "callback": callback, "name": "", "error": ""}
        stale_callbacks: list[LoopbackCallbackServer] = []
        with self._lock:
            # Close any previously pending callback servers to avoid accumulating idle loopback sockets.
            for old_job in self._or_jobs.values():
                if old_job.get("status") == "pending" and old_job.get("callback") is not None:
                    stale_callbacks.append(old_job["callback"])
                    old_job["status"] = "error"
                    old_job["error"] = "已被新的登录请求取代。"
            while len(self._or_jobs) >= 8:
                oldest = next(iter(self._or_jobs))
                self._or_jobs.pop(oldest, None)
            self._or_jobs[job_id] = job
        for old_cb in stale_callbacks:
            with suppress(Exception):
                old_cb.close()

        def _worker() -> None:
            try:
                code = asyncio.run(callback.wait_for_code(timeout=180.0))
                api_key = asyncio.run(
                    exchange_openrouter_code(code, verifier, callback.callback_url, transport=self.transport)
                )
                from .auth import OPENROUTER_DEFAULT_MODEL, OAuthLoginResult

                result = OAuthLoginResult(
                    provider_key="openrouter",
                    preset="openrouter",
                    credential=api_key,
                    auth_type="api_key",
                    default_model=OPENROUTER_DEFAULT_MODEL,
                    base_url="https://openrouter.ai/api/v1",
                )
                _, saved_name = persist_oauth_result(
                    result,
                    home=self.home,
                    set_default=set_default,
                    vault=self.vault,
                    config_path=self.config_path,
                )
                with self._lock:
                    job["status"] = "ok"
                    job["name"] = saved_name
            except Exception as exc:
                with self._lock:
                    job["status"] = "error"
                    job["error"] = redact(str(exc))
            finally:
                callback.close()

        threading.Thread(target=_worker, name=f"mosscode-or-{job_id[:6]}", daemon=True).start()
        return HTTPStatus.OK, {"job_id": job_id, "auth_url": auth_url}

    def _handle_openrouter_status(self, query: str) -> tuple[HTTPStatus, dict[str, Any]]:
        params = parse_qs(query, keep_blank_values=False)
        job_id = (params.get("job") or [""])[0]
        with self._lock:
            job = self._or_jobs.get(job_id)
            if job is None:
                return HTTPStatus.NOT_FOUND, {"error": "未找到对应的登录任务。"}
            return HTTPStatus.OK, {"status": job["status"], "name": job["name"], "error": job["error"]}

    def _handle_copilot_start(self) -> tuple[HTTPStatus, dict[str, Any]]:
        problem = (
            self.vault.problem()
            if (self.vault is not None and callable(getattr(self.vault, "problem", None)))
            else keyring_problem()
        )
        if problem is not None:
            return HTTPStatus.BAD_REQUEST, {"error": f"系统凭据管理器不可用（{problem}），无法安全保存 OAuth 令牌。"}
        try:
            challenge = asyncio.run(start_copilot_device_flow(transport=self.transport))
        except AuthError as exc:
            return HTTPStatus.BAD_GATEWAY, {"error": str(exc)}
        job_id = secrets.token_urlsafe(12)
        with self._lock:
            while len(self._cp_jobs) >= 8:
                oldest = next(iter(self._cp_jobs))
                self._cp_jobs.pop(oldest, None)
            self._cp_jobs[job_id] = challenge
        return HTTPStatus.OK, {
            "job_id": job_id,
            "user_code": challenge.user_code,
            "verification_uri": challenge.verification_uri,
            "interval": challenge.interval,
            "expires_in": challenge.expires_in,
        }

    def _handle_copilot_poll(self, body: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        job_id = str(body.get("job_id") or "").strip()
        set_default = bool(body.get("set_default", True))
        with self._lock:
            challenge = self._cp_jobs.get(job_id)
        if challenge is None:
            return HTTPStatus.NOT_FOUND, {"error": "未找到对应的设备码登录任务。"}
        try:
            status, value = asyncio.run(poll_copilot_device_token_once(challenge.device_code, transport=self.transport))
            if status in {"pending", "slow_down"}:
                return HTTPStatus.OK, {"status": status}
            if status != "ok" or not value:
                with self._lock:
                    self._cp_jobs.pop(job_id, None)
                return HTTPStatus.OK, {"status": "error", "error": value or "授权失败。"}
            _, expires_at, endpoint = asyncio.run(exchange_copilot_session_token_async(value, transport=self.transport))
            from .auth import COPILOT_DEFAULT_MODEL, COPILOT_HEADERS, OAuthLoginResult

            result = OAuthLoginResult(
                provider_key="copilot",
                preset="copilot",
                credential=value,
                auth_type="github-copilot",
                default_model=COPILOT_DEFAULT_MODEL,
                base_url=endpoint,
                headers=COPILOT_HEADERS,
                expires_at=expires_at,
            )
            _, saved_name = persist_oauth_result(
                result,
                home=self.home,
                set_default=set_default,
                vault=self.vault,
                config_path=self.config_path,
            )
            with self._lock:
                self._cp_jobs.pop(job_id, None)
            return HTTPStatus.OK, {"status": "ok", "name": saved_name}
        except AuthError as exc:
            with self._lock:
                self._cp_jobs.pop(job_id, None)
            return HTTPStatus.OK, {"status": "error", "error": str(exc)}

    def _build_server(self, port: int) -> ThreadingHTTPServer:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _common_headers(self, *, csp: str = "default-src 'none'; frame-ancestors 'none'") -> None:
                self.send_header("Content-Security-Policy", csp)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")

            def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self._common_headers()
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> tuple[dict[str, Any] | None, HTTPStatus | None, str]:
                raw_len = self.headers.get("Content-Length")
                if raw_len is None:
                    return {}, None, ""
                try:
                    length = int(raw_len)
                except ValueError:
                    return None, HTTPStatus.BAD_REQUEST, "无效的 Content-Length。"
                if length < 0:
                    return None, HTTPStatus.BAD_REQUEST, "无效的 Content-Length。"
                if length > MAX_BODY_BYTES:
                    return None, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大。"
                data = self.rfile.read(length) if length else b"{}"
                try:
                    parsed = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return None, HTTPStatus.BAD_REQUEST, "请求体不是合法 JSON。"
                if not isinstance(parsed, dict):
                    return None, HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象。"
                return parsed, None, ""

            def _check_host(self) -> bool:
                if not parent._valid_host(self.headers.get("Host")):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝非法 Host 请求。"})
                    return False
                return True

            def do_GET(self) -> None:
                parent._touch()
                if not self._check_host():
                    return
                parsed = urlsplit(self.path)
                if parsed.path == "/":
                    params = parse_qs(parsed.query, keep_blank_values=False)
                    token = (params.get("token") or [""])[0]
                    if token:
                        with parent._lock:
                            valid = (not parent._boot_token_used) and hmac.compare_digest(token, parent.boot_token)
                            if valid:
                                parent._boot_token_used = True
                        if not valid:
                            self._send_json(HTTPStatus.FORBIDDEN, {"error": "启动令牌无效或已使用。"})
                            return
                        self.send_response(int(HTTPStatus.SEE_OTHER))
                        self.send_header("Location", "/")
                        self.send_header(
                            "Set-Cookie",
                            f"{SESSION_COOKIE}={parent.session_token}; HttpOnly; SameSite=Strict; Path=/",
                        )
                        self._common_headers()
                        self.end_headers()
                        return
                    if not parent._has_valid_session_cookie(self.headers.get("Cookie")):
                        self._send_json(
                            HTTPStatus.FORBIDDEN, {"error": "缺少有效会话令牌，请通过终端输出的完整地址访问。"}
                        )
                        return
                    nonce = secrets.token_urlsafe(16)
                    body = _render_web_page(parent.csrf_token, nonce)
                    self.send_response(int(HTTPStatus.OK))
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._common_headers(
                        csp=(
                            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                            "base-uri 'none'; form-action 'self'"
                        )
                    )
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path == "/api/state":
                    if not parent._authorized_api(self.headers):
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "未授权的 API 请求。"})
                        return
                    self._send_json(HTTPStatus.OK, parent._state_payload())
                    return

                if parsed.path == "/api/auth/openrouter/status":
                    if not parent._authorized_api(self.headers):
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "未授权的 API 请求。"})
                        return
                    status, payload = parent._handle_openrouter_status(parsed.query)
                    self._send_json(status, payload)
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "未找到请求的资源。"})

            def do_POST(self) -> None:
                parent._touch()
                if not self._check_host():
                    return
                if not parent._valid_origin(self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨源请求。"})
                    return
                if not parent._authorized_api(self.headers):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "未授权的 API 请求。"})
                    return
                body, err_status, err_msg = self._read_json()
                if err_status is not None or body is None:
                    self._send_json(err_status or HTTPStatus.BAD_REQUEST, {"error": err_msg})
                    return

                path = urlsplit(self.path).path
                if path == "/api/probe":
                    status, payload = parent._handle_probe(body)
                    self._send_json(status, payload)
                    return
                if path == "/api/providers":
                    status, payload = parent._handle_save_provider(body)
                    self._send_json(status, payload)
                    return
                if path == "/api/default":
                    status, payload = parent._handle_set_default(body)
                    self._send_json(status, payload)
                    return
                if path == "/api/auth/openrouter/start":
                    status, payload = parent._handle_openrouter_start(body)
                    self._send_json(status, payload)
                    return
                if path == "/api/auth/copilot/start":
                    status, payload = parent._handle_copilot_start()
                    self._send_json(status, payload)
                    return
                if path == "/api/auth/copilot/poll":
                    status, payload = parent._handle_copilot_poll(body)
                    self._send_json(status, payload)
                    return
                if path == "/api/shutdown":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    parent._stop_event.set()
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "未找到请求的接口。"})

        class _LoopbackServer(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        return _LoopbackServer(("127.0.0.1", port), _Handler)

    def start(self) -> ConfigWebServer:
        self._thread.start()
        return self

    def wait_until_stopped(self) -> None:
        """Block until `/api/shutdown` is invoked or idle timeout elapses."""
        while not self._stop_event.wait(timeout=0.5):
            with self._lock:
                idle = time.monotonic() - self._last_activity
            if self.idle_timeout > 0 and idle >= self.idle_timeout:
                self._stop_event.set()
                break

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callbacks = [job["callback"] for job in self._or_jobs.values() if job.get("callback") is not None]
            self._or_jobs.clear()
            self._cp_jobs.clear()
        self._stop_event.set()
        for cb in callbacks:
            with suppress(Exception):
                cb.close()
        with suppress(Exception):
            self._server.shutdown()
        with suppress(Exception):
            self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> ConfigWebServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()
