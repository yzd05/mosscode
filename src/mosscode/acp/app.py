"""Main ACP Agent implementation for MossCode."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import acp
from acp import RequestError
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AudioContentBlock,
    AuthenticateResponse,
    ClientCapabilities,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    TextContentBlock,
)

from ..credentials import redact
from ..launcher import version as mosscode_version
from ..session import list_sessions as list_project_sessions
from .auth import authenticate_method, build_auth_methods, load_session_settings, resolve_auth_cwd
from .session import AcpLaunchOptions, AcpSession

if TYPE_CHECKING:
    from acp import Client

McpServerParam = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio


class MossCodeAcpAgent:
    """Implements the ACP Agent interface backed by isolated MossCode sessions."""

    def __init__(self, options: AcpLaunchOptions | None = None) -> None:
        self.options = options or AcpLaunchOptions()
        self._client: Client | None = None
        self.client_capabilities: ClientCapabilities | None = None
        self.client_info: Implementation | None = None
        self.sessions: dict[str, AcpSession] = {}

    def on_connect(self, conn: Client) -> None:
        self._client = conn

    def _require_client(self) -> Client:
        if self._client is None:
            raise RequestError.internal_error({"reason": "ACP 客户端连接尚未就绪。"})
        return self._client

    def _get_session(self, session_id: str) -> AcpSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise RequestError.invalid_params({"sessionId": session_id, "reason": f"未找到会话：{session_id}"})
        return session

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        self.client_capabilities = client_capabilities
        self.client_info = client_info
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=Implementation(
                name="mosscode",
                title="MossCode",
                version=mosscode_version(),
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    audio=False,
                    embedded_context=True,
                ),
                mcp_capabilities=McpCapabilities(
                    http=True,
                    sse=False,
                    acp=False,
                ),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    close=SessionCloseCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
            auth_methods=build_auth_methods(client_capabilities),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse:
        return authenticate_method(
            method_id,
            None,
            explicit_config=self.options.config,
            provider_name=self.options.provider,
            trust_project_config=self.options.trust_project_config,
            home=self.options.home,
            settings=self.options.settings,
        )

    def _schedule_initial_updates(self, session: AcpSession) -> None:
        async def _send_after_response() -> None:
            # Yield enough event-loop turns so acp.Connection._run_request wakes from
            # asyncio.wait({handler}) and writes the JSON-RPC response onto the transport first.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if session._closed or self.sessions.get(session.session_id) is not session:
                return
            try:
                await session.send_initial_updates()
            except Exception as exc:
                print(f"MossCode ACP：发送初始会话通知失败：{redact(str(exc))}", file=sys.stderr)

        task = asyncio.create_task(_send_after_response())
        session._update_tasks.add(task)
        task.add_done_callback(session._update_tasks.discard)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerParam] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        client = self._require_client()
        session = AcpSession.create(
            cwd=Path(cwd),
            client=client,
            options=self.options,
            mcp_servers=mcp_servers,
        )
        self.sessions[session.session_id] = session
        self._schedule_initial_updates(session)
        return session.new_session_response()

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerParam] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        client = self._require_client()
        if existing := self.sessions.pop(session_id, None):
            await existing.close()
        session = AcpSession.create(
            cwd=Path(cwd),
            client=client,
            options=self.options,
            mcp_servers=mcp_servers,
            resume_session_id=session_id,
        )
        try:
            response = await session.load_and_replay(replay=True)
        except BaseException:
            await session.close()
            raise
        self.sessions[session.session_id] = session
        return response

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerParam] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        client = self._require_client()
        if existing := self.sessions.pop(session_id, None):
            await existing.close()
        session = AcpSession.create(
            cwd=Path(cwd),
            client=client,
            options=self.options,
            mcp_servers=mcp_servers,
            resume_session_id=session_id,
        )
        try:
            loaded = await session.load_and_replay(replay=False)
        except BaseException:
            await session.close()
            raise
        self.sessions[session.session_id] = session
        self._schedule_initial_updates(session)
        return ResumeSessionResponse(modes=loaded.modes, config_options=loaded.config_options)

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        target: Path | None = None
        if cwd is not None:
            target = Path(cwd)
            if not target.is_absolute():
                raise RequestError.invalid_params({"cwd": cwd, "reason": "cwd 必须是绝对路径。"})
        auth_cwd = (
            target.resolve()
            if target is not None and target.is_dir()
            else resolve_auth_cwd(None, trust_project_config=self.options.trust_project_config, home=self.options.home)
        )
        settings, _, _ = load_session_settings(
            auth_cwd,
            explicit_config=self.options.config,
            provider_name=self.options.provider,
            trust_project_config=self.options.trust_project_config,
            home=self.options.home,
            settings=self.options.settings,
        )
        if not settings.persist_sessions:
            return ListSessionsResponse(sessions=[])
        if target is not None:
            if not target.is_dir():
                return ListSessionsResponse(sessions=[])
            resolved = target.resolve()
            summaries = list_project_sessions(resolved, home=self.options.home)
            return ListSessionsResponse(
                sessions=[
                    SessionInfo(
                        session_id=item.id,
                        cwd=str(resolved),
                        title=None if not item.title or item.title == "（无标题）" else item.title,
                        updated_at=item.modified.astimezone().isoformat(timespec="seconds"),
                    )
                    for item in summaries
                ]
            )
        return ListSessionsResponse(
            sessions=[
                SessionInfo(
                    session_id=s.session_id,
                    cwd=str(s.cwd),
                )
                for s in self.sessions.values()
            ]
        )

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await session.close()
        return CloseSessionResponse()

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> SetSessionModeResponse:
        session = self._get_session(session_id)
        return await session.set_mode(mode_id)

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse:
        session = self._get_session(session_id)
        return await session.set_config_option(config_id, value)

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        **kwargs: Any,
    ) -> PromptResponse:
        session = self._get_session(session_id)
        return await session.prompt(prompt)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            await session.cancel()

    async def close_all(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            await session.close()
