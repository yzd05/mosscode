import asyncio
from contextlib import aclosing, suppress
from time import monotonic, perf_counter

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from .agents.tasks import STATUS_LABELS, Announcer
from .appearance import (
    BRAND_COLOR,
    COMMAND_HIGHLIGHT_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    OPERATION_COLOR,
    PARAMETER_COLOR,
    RULE_COLOR,
    TERMINAL_CSS,
    TOOL_SUCCESS_COLOR,
    USER_MESSAGE_BACKGROUND,
    USER_MESSAGE_COLOR,
    compact_notice,
    startup_banner,
)
from .builtin_commands import (
    BUILTIN,
    clean_text,
    confirm_tasks,
    dispatch,
    offer_trust,
    open_sessions,
)
from .catalog import tokens_label
from .choice_list import KeyboardOptions
from .clipboard import read_clipboard_async
from .commands import CommandRegistry, Hint, parse_input, unknown_note
from .config import Settings
from .conversation import Conversation, MemoryUpdated, Notice
from .coordinator import BADGE as COORDINATOR_BADGE
from .goal import GOAL, GoalStep, elapsed_text
from .images import ProcessedImage
from .mcp_surface import register_mcp_prompts, register_mcp_prompts_from_manager
from .member_ui import VIEW_SHARE, MemberPanel, MemberView
from .mentions import FileIndex
from .messages import (
    STOPPED_EARLY,
    AutoContinue,
    CompactFailed,
    CompactFinished,
    CompactStarted,
    Completed,
    ContextCollapsed,
    Delta,
    FilesAttached,
    IterationProgress,
    ProviderError,
    ReplyRetry,
    ResponseBoundary,
    ResultsCleared,
    Retrying,
    RunStopped,
    ToolProgress,
    UsageUpdate,
    UserInput,
)
from .permissions import CHOICES, MODE_BADGES, Answer, ApprovalRequest, QuestionRequest
from .sandbox import badge as sandbox_badge
from .scrollback import StreamingMarkdown, TranscriptMarkdown, commit_transcript, message_row, redraw_transcript
from .teams.runtime import MemberRow
from .todo import completed as todos_completed
from .tools import shell_label
from .tui_connect import _ConnectMixin
from .tui_hints import _PromptInputMixin
from .tui_model_picker import _ModelPickerMixin
from .tui_panels import _CommandUIMixin
from .tui_teams import _TeamViewMixin
from .tui_views import (
    CANCELLED_COMPACT,
    CHOICE_LABELS,
    DECLINED_HINT,
    DETAIL_ROWS,
    DIFF_ROWS,
    DOUBLE_ESCAPE,
    MODE_COLORS,
    MODE_CYCLE,
    NEW_FILE_ROWS,
    RECENT_TOOLS,
    RESUMED_MESSAGES,
    SHELL_HEAD,
    SHELL_TAIL,
    SPINNER,
    STAGE_ROOM,
    TODO_PANEL_ROWS,
    TOOL_MARKS,
    Attachment,
    ChatEntry,
    CompactRow,
    ConfigTrustApp,
    ConnectPanel,
    MCPTrustApp,
    ModelPanel,
    PlanEntry,
    PluginTrustApp,
    PromptArea,
    ProviderSelectApp,
    QuestionOptions,
    SessionInfo,
    ShellRow,
    TerminalApp,
    ToolEntry,
    _writes_only,
    activity_line,
    activity_row,
    agent_detail,
    answer_lines,
    attached_line,
    choice_label,
    choice_prompt,
    describe_call,
    diff_rows,
    image_lines,
    memory_line,
    plan_text,
    preview_text,
    recent_tools_line,
    resumed_rows,
    rewound_image,
    shell_heading,
    shell_name,
    shell_output,
    todo_panel,
    tool_state,
    turn_details_block,
    user_row,
)
from .usage import approx_tokens, cache_hit_rate

__all__ = [
    "Attachment",
    "CANCELLED_COMPACT",
    "CHOICE_LABELS",
    "ChatEntry",
    "CompactRow",
    "ConfigTrustApp",
    "ConnectPanel",
    "DECLINED_HINT",
    "DETAIL_ROWS",
    "DIFF_ROWS",
    "DOUBLE_ESCAPE",
    "MCPTrustApp",
    "MODE_COLORS",
    "MODE_CYCLE",
    "ModelPanel",
    "MossCodeApp",
    "NEW_FILE_ROWS",
    "PlanEntry",
    "PluginTrustApp",
    "PromptArea",
    "ProviderSelectApp",
    "QuestionOptions",
    "RECENT_TOOLS",
    "RESUMED_MESSAGES",
    "SHELL_HEAD",
    "SHELL_TAIL",
    "SPINNER",
    "STAGE_ROOM",
    "SessionInfo",
    "ShellRow",
    "StreamingMarkdown",
    "TODO_PANEL_ROWS",
    "TOOL_MARKS",
    "TerminalApp",
    "ToolEntry",
    "TranscriptMarkdown",
    "_writes_only",
    "activity_line",
    "activity_row",
    "agent_detail",
    "answer_lines",
    "attached_line",
    "choice_label",
    "choice_prompt",
    "describe_call",
    "diff_rows",
    "image_lines",
    "memory_line",
    "message_row",
    "monotonic",
    "perf_counter",
    "plan_text",
    "preview_text",
    "recent_tools_line",
    "resumed_rows",
    "rewound_image",
    "shell_heading",
    "shell_name",
    "shell_output",
    "todo_panel",
    "tool_state",
    "turn_details_block",
    "user_row",
]


class MossCodeApp(
    _PromptInputMixin, _CommandUIMixin, _ModelPickerMixin, _ConnectMixin, _TeamViewMixin, TerminalApp[None]
):
    TITLE = "MossCode"
    SUB_TITLE = ""
    muted_color = MUTED_COLOR

    def _now(self) -> float:
        return monotonic()

    async def _read_clipboard(self):
        return await read_clipboard_async()

    CSS = (
        TERMINAL_CSS
        + """
    Screen { background: $background; color: $foreground; padding: 0 2; overflow: hidden hidden; }
    Screen:inline { height: auto; border: none; }
    #banner { height: auto; margin: 1 0; }
    /* Each block in the scrollback ends with a blank row: that row is the gap above whatever is live, so nothing
       live adds one of its own at the top. */
    #chat { height: auto; max-height: 100vh; scrollbar-size: 1 0; }
    .entry { height: auto; padding: 0; margin-bottom: 1; background: $background; }
    .message-row { height: auto; }
    .user-entry .message-row, .user-entry .message-row > Static {
        background: USER_MESSAGE_BACKGROUND !important; color: USER_MESSAGE_COLOR !important;
    }
    .message-marker { width: 2; height: 1; text-style: bold; }
    .user-message, .response-content { width: 1fr; height: auto; }
    .thinking, .answer { height: auto; padding: 0; margin: 0; background: $background; }
    .thinking { height: auto; padding: 0 0 0 2; margin-bottom: 1; }
    .thinking-label { height: auto; margin-bottom: 1; color: MUTED_COLOR !important; }
    .turn-summary { height: auto; margin-top: 1; color: MUTED_COLOR !important; }
    #status { height: auto; margin-top: 0; color: $text-muted; }
    #queue { height: auto; margin-bottom: 1; padding: 0 1; }
    #todo-panel { height: auto; max-height: 6; margin-bottom: 1; padding: 0 1; }
    #attachments { height: auto; margin-bottom: 1; padding: 0 1; color: MUTED_COLOR !important; }
    .image-notes { height: auto; }
    #activity { height: 1; padding: 0 1; }
    #activity.idle { height: 0; }
    #input-row { height: auto; padding: 0 1; border-top: solid RULE_COLOR; border-bottom: solid RULE_COLOR; }
    #input-row.mode-plan { border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR; }
    #input-row.mode-acceptEdits { border-top: solid TOOL_SUCCESS_COLOR; border-bottom: solid TOOL_SUCCESS_COLOR; }
    #input-row.mode-bypassPermissions { border-top: solid ERROR_COLOR; border-bottom: solid ERROR_COLOR; }
    #input-prefix { width: 2; height: 1; color: BRAND_COLOR !important; text-style: bold; }
    #input-prefix.shell { color: OPERATION_COLOR !important; }
    #prompt { width: 1fr; height: auto; max-height: 8; border: none; padding: 0; background: $background; }
    #prompt > .text-area--placeholder { color: MUTED_COLOR !important; }
    #prompt:focus { border: none; background-tint: transparent; }
    #command-hints { height: auto; max-height: 7; border: none; padding: 0 1; }
    #command-hints > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    /* Grey like the status line below it; a blank row keeps the two apart. */
    #command-help { height: 1; margin-bottom: 1; color: MUTED_COLOR !important; padding: 0 1; }
    #model-panel { width: 1fr; height: auto; padding: 0 1; border-top: solid RULE_COLOR; border-bottom: solid RULE_COLOR; }
    #model-title, #model-help { height: 1; }
    #model-filter { height: 1; border: none; padding: 0; background: $background; margin: 0; }
    #model-filter:focus { border: none; background-tint: transparent; }
    #model-help, #approval-help, #question-help, #member-help, #connect-help { color: MUTED_COLOR !important; }
    #models { height: auto; max-height: 8; border: none; padding: 0; }
    #models > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #connect-panel {
        width: 1fr; height: auto; padding: 0 1;
        border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR;
    }
    #connect-title, #connect-help { height: 1; }
    #connect-status { height: auto; color: MUTED_COLOR !important; }
    #connect-input { height: 1; border: none; padding: 0; background: $background; margin: 0; }
    #connect-input:focus { border: none; background-tint: transparent; }
    #connect-options { height: auto; max-height: 8; border: none; padding: 0; }
    #connect-options > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    /* A card waiting for an answer takes the prompt's place, between two rules as the prompt is. */
    #approval-panel {
        height: auto; max-height: 16; padding: 0 1;
        border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR;
    }
    #approval-detail { height: auto; max-height: 20; }
    #approval-options { height: 4; margin-top: 1; border: none; padding: 0; }
    #approval-options > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #approval-help { height: auto; }
    #question-panel { height: auto; padding: 0 1; border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR; }
    #question-detail, #question-help { height: auto; }
    #question-options { height: auto; border: none; padding: 0; }
    #question-options > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    .local-note { height: auto; margin: 0 0 1 2; color: $text-muted; }
    .resumed-entry { height: auto; margin-bottom: 1; }
    .tool-entry { height: auto; margin-bottom: 1; }
    #member-panel { height: auto; padding: 0 1; }
    #member-compact { height: auto; }
    #member-list { height: auto; max-height: 8; border: none; padding: 0; }
    #member-list > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #member-help { height: 1; }
    #member-view { height: auto; padding: 0 1; }
    #member-head { height: 1; }
    #member-log { height: 1fr; scrollbar-size: 1 0; background: $background; }
    #member-running { height: auto; max-height: 6; }
    #member-view-help { height: 1; }
    /* A long list scrolls with a thin bar in the rules' grey; the theme's would be a block of near-black that
       stands out against the terminal's own background. */
    OptionList {
        background: $background; scrollbar-size-vertical: 1;
        scrollbar-color: RULE_COLOR; scrollbar-color-hover: MUTED_COLOR; scrollbar-color-active: MUTED_COLOR;
    }
    """.replace("COMMAND_HIGHLIGHT_COLOR", COMMAND_HIGHLIGHT_COLOR)
        .replace("PARAMETER_COLOR", PARAMETER_COLOR)
        .replace("USER_MESSAGE_BACKGROUND", USER_MESSAGE_BACKGROUND)
        .replace("USER_MESSAGE_COLOR", USER_MESSAGE_COLOR)
        .replace("OPERATION_COLOR", OPERATION_COLOR)
        .replace("TOOL_SUCCESS_COLOR", TOOL_SUCCESS_COLOR)
        .replace("ERROR_COLOR", ERROR_COLOR)
        .replace("BRAND_COLOR", BRAND_COLOR)
        .replace("MUTED_COLOR", MUTED_COLOR)
        .replace("RULE_COLOR", RULE_COLOR)
    )
    BINDINGS = [
        Binding("escape", "cancel", "取消生成", priority=True, show=False),
        Binding("ctrl+c", "cancel_task", "取消任务", priority=True, show=False),
        Binding("ctrl+q", "quit", "退出", priority=True, show=False),
        Binding("up", "move_hint(-1)", priority=True, show=False),
        Binding("down", "move_hint(1)", priority=True, show=False),
        Binding("tab", "complete_hint", priority=True, show=False),
        Binding("shift+tab", "cycle_mode", priority=True, show=False),
        Binding("ctrl+b", "background", priority=True, show=False),
        Binding("ctrl+o", "toggle_details", "展开详情", priority=True, show=False),
        Binding("ctrl+t", "members", priority=True, show=False),
        Binding("alt+m", "open_models", priority=True, show=False),
        Binding("ctrl+d", "model_set_default", priority=True, show=False),
        Binding("ctrl+f", "model_toggle_favorite", priority=True, show=False),
        Binding("ctrl+n", "model_open_connect", priority=True, show=False),
        Binding("enter", "complete_hint(False)", priority=True, show=False),
    ]

    def __init__(
        self,
        conversation: Conversation,
        provider_name: str,
        *,
        settings: Settings | None = None,
        model: str = "",
        provider_factory=None,
        resume: str | None = None,
        commands: CommandRegistry | None = None,
        initial_prompt: str | None = None,
    ) -> None:
        super().__init__()
        self.conversation = conversation
        # --resume ID / --continue restore at startup; "" opens the session list once started.
        self._resume = resume
        # `moss "..."` sends this once startup is done, as if typed.
        self._initial_prompt = initial_prompt
        self._panel_rows = 0
        self._pick = None  # The open picker's on_pick (CommandUI.choose).
        self._on_cancel = None  # Its on_cancel, run by Esc.
        self._model_picker_active = False
        self._model_picker_choices = []
        self._model_picker_filtered = []
        self._connect_step = ""
        self._connect_history = []
        self._connect_choices = []
        self._connect_status_text = Text("")
        self._model_switch_lock = asyncio.Lock()
        self._hooks_confirmed = False  # Asked once at startup, before session_start.
        # A table of this run's own: skill commands are added here and replaced by /skills reload.
        self.commands = commands if commands is not None else BUILTIN.with_skills(conversation.skills)[0]
        if conversation.mcp_manager is not None:
            register_mcp_prompts_from_manager(self.commands, conversation.mcp_manager)
        self.provider_name = self._banner_provider = provider_name
        self.settings, self.model = settings, model
        # As configured: /model <name>/<model> replaces a provider for this session, /model <name> brings it back.
        self._configured_providers = settings.providers if settings is not None else ()
        if provider_factory is None:
            from .providers import create_provider as provider_factory
        self.provider_factory = provider_factory
        self._reply_task: asyncio.Task[None] | None = None
        self._generating = False
        self._active_reply: ChatEntry | None = None
        self._closing = False
        self.started = 0.0
        self._iteration = 0
        self._iteration_limit = 25
        self._usage = conversation.total_usage
        self._status_note = ""
        self._stage_note = ""  # What the running task is doing, shown on the line above the prompt.
        self._activity_shown: tuple | None = None
        # The rows of the latest main-conversation tool calls, oldest first; each knows how its call ended.
        self._recent_tools: dict[ToolEntry, None] = {}
        self._mcp_notices: list[str] = []  # MCP servers that settled while a task was running.
        self._retry_notice = ""  # Held until the next event so the ticker does not hide it.
        self._hint_until = 0.0  # Short notes shown over the running status until this time.
        self._escape_at = 0.0  # The last Esc on an empty, idle prompt: a second one soon after opens /rewind.
        self._hint_choices: list[Hint] = []
        self._hint_value = ""
        # Per @ mention hint: where the cursor goes after it, as an index into the prompt's text.
        self._mention_cursors: list[int] = []
        self._file_index: FileIndex | None = None
        self._indexing = False
        self._dismissed_prompt: str | None = None
        self._scrollback_lock = asyncio.Lock()
        self._history: list = []  # Everything committed to scrollback, re-rendered after a width change.
        self._width: int | None = None
        self._redraw_timer = None
        self._approval_request: ApprovalRequest | None = None
        self._approval_text = Text()  # What the card on screen says above its choices.
        self._approval_queue: list[ApprovalRequest] = []  # Shown one at a time, in arrival order.
        self._question: QuestionRequest | None = None  # ask_user's questions on screen.
        self._question_waiting: QuestionRequest | None = None  # Arrived while a member's approval card was up.
        self._question_step = 0
        self._question_answers: list[Answer | None] = []
        self._question_checked: set[int] = set()
        self._question_typing = False  # The answer is being typed in the prompt ("其他").
        self._question_draft = ""  # The prompt's text before typing an answer; put back afterwards.
        self._todo_shown: tuple | None = None  # The checklist the panel shows; None when hidden.
        self._todo_dismissed: tuple = ()  # A finished list the user has moved on from (sent a message since).
        self._startup_task = None
        self._compacting: float | None = None  # Start time while a compaction runs.
        self._compact_row: CompactRow | None = None
        self._shell_row: ShellRow | None = None  # While a ! command runs.
        # (estimate, window, percent of the effective window, near the compaction threshold)
        self._context_share: tuple[str, str, int, bool] | None = None
        self._agent_rows: dict[str, ToolEntry] = {}  # Agent tool rows by call id.
        self._notify_waiting = False  # A notification turn waits for the running task.
        self._goal_checking = False  # The /goal evaluation after a turn is running as _reply_task.
        self._announce = Announcer(self._task_note)
        # The member panel's rows, read off the loop, and the member view.
        self._member_rows: list[MemberRow] = []
        self._members_task: asyncio.Task | None = None
        self._members_again = False
        self._member_ticks = 0
        self._stop_armed: tuple[str, str, float] | None = None  # (team, member, deadline) for x-twice stop.
        self._view_lock = asyncio.Lock()
        self._show_details = False  # Ctrl+O expands thinking blocks and full tool outputs/diffs.
        self._last_turn_entries: list[ChatEntry | ToolEntry] = []
        if getattr(self.conversation, "loops", None) is None and callable(
            getattr(self.conversation, "attach_loops", None)
        ):
            self.conversation.attach_loops(is_busy=self._is_loop_busy, trigger=self._trigger_loop)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat"):
            yield SessionInfo(startup_banner(self.provider_name), id="banner")
        view = MemberView(id="member-view")
        view.display = False
        yield view
        todo = Static("", id="todo-panel", markup=False)
        todo.display = False
        yield todo
        queue = Static("", id="queue", markup=False)
        queue.display = False
        yield queue
        attachments = Static("", id="attachments", markup=False)
        attachments.display = False
        yield attachments
        # The running stage, on the row above the prompt; it takes no row while idle, where the blank row that
        # ends the scrollback already sits above the prompt.
        yield Static("", id="activity", classes="idle", markup=False)
        with Horizontal(id="input-row"):
            yield Static("❯", id="input-prefix")
            prompt = PromptArea(placeholder="输入消息，或 / 查看命令；Shift+Enter 换行", id="prompt")
            prompt.take_queued = self._take_back_queued
            prompt.root = self._workspace_root
            yield prompt
        panel = ModelPanel(id="model-panel")
        panel.display = False
        yield panel
        connect = ConnectPanel(id="connect-panel")
        connect.display = False
        yield connect
        with Vertical(id="approval-panel") as approval:
            approval.display = False
            yield Static("", id="approval-detail", markup=False)
            yield KeyboardOptions(*(choice_prompt(CHOICE_LABELS[choice]) for choice in CHOICES), id="approval-options")
            yield Static("↑ / ↓ 选择 · Enter 确认 · Esc 拒绝本次 · Ctrl+C 取消任务", id="approval-help", markup=False)
        with Vertical(id="question-panel") as question:
            question.display = False
            yield Static("", id="question-detail", markup=False)
            yield QuestionOptions(id="question-options")
            yield Static("", id="question-help", markup=False)
        hints = KeyboardOptions(id="command-hints")
        hints.can_focus = False
        hints.display = False
        yield hints
        help_line = Static("", id="command-help", markup=False)
        help_line.display = False
        yield help_line
        members = MemberPanel(id="member-panel")
        members.display = False
        yield members
        yield SessionInfo(Text(""), id="status")

    def on_mount(self) -> None:
        super().on_mount()
        if self.conversation.registry:
            self.conversation.registry.permissions.interactive = True
        self.conversation.on_memory = self._on_memory
        self.conversation.on_tasks = self._schedule_notify
        self.conversation.tasks.on_change = self._on_task_change
        subagents = getattr(self.conversation, "subagents", None)
        if subagents is not None:
            subagents.on_approval = self._member_approval
            subagents.on_approval_settled = self._withdraw_approval
        teams = getattr(self.conversation, "teams", None)
        if teams is not None:  # Members ask and report while the lead runs or waits.
            teams.on_approval = self._member_approval
            teams.on_note = lambda text: self._closing or self.call_later(self._local_note, "└ " + text)
            teams.on_change = lambda: self._closing or self.call_later(self._members_changed)
            teams.on_withdraw = lambda request_id: self._closing or self.call_later(self._withdraw_approval, request_id)
            teams.on_commit = self._member_committed
        if self.conversation.context_root is not None:
            self.conversation.start_worktree_cleanup(
                lambda report: self.conversation._hook_notice(clean_text(report, automatic=True))
            )
        self.query_one("#prompt", PromptArea).focus()
        self._width = self.size.width
        self._status("")
        self.set_interval(0.2, self._tick)
        self.set_interval(0.5, self._member_tick)
        self.set_interval(0.5, self._refresh_todos)
        self.call_later(self._refresh_layout)
        self.call_after_refresh(self._commit_ready_transcript)
        # The prompt stays usable: a first task waits only for project context, and MCP
        # servers join in the background as each one becomes ready.
        if self._resume:
            # Restoring loads the project context itself; MCP and the other notices follow.
            self._start_resume(self._resume, startup=True)
        elif self.conversation.mcp_manager is not None or self.conversation.context_root is not None:
            self._startup_task = asyncio.create_task(self._initialize())
        elif self._resume == "":
            self.call_later(open_sessions, self._command_context())
        elif self._initial_prompt:
            self.call_later(self._send_initial)

    async def _send_initial(self) -> None:
        text, self._initial_prompt = self._initial_prompt, None
        if text and text.strip() and not self._closing:
            field = self.query_one("#prompt", PromptArea)
            await self.on_prompt_area_submitted(PromptArea.Submitted(field, text))

    async def _confirm_hooks(self) -> None:
        """Untrusted project hook files are confirmed before the first session_start."""
        if self._hooks_confirmed:
            return
        self._hooks_confirmed = True
        done = asyncio.get_running_loop().create_future()

        async def finished() -> None:
            if not done.done():
                done.set_result(None)

        await offer_trust(self, self.conversation.hooks, finished)
        await done

    async def _initialize(self):
        try:
            await self._confirm_hooks()
            for notice in await self.conversation.start(wait_mcp=False, on_mcp=self._on_mcp_update):
                await self._local_note("└ " + notice)
            await self._commit_ready_transcript()
        except Exception:
            await self._local_note("└ 启动初始化失败；请检查配置后重新启动。")
        finally:
            if not self._closing and not self._generating:
                self._status("")
        if self._initial_prompt and not self._closing:
            await self._send_initial()
        if self._resume == "" and not self._closing:
            self._resume = None
            await open_sessions(self._command_context())

    def _refresh_mcp_hints(self) -> None:
        if self._closing or not self.query("#prompt"):
            return
        value = self.query_one("#prompt", PromptArea).text
        if value.lstrip().startswith("/") or "@" in value:
            self._refresh_hints(value)

    def _on_mcp_update(self, status) -> None:
        if self._closing:
            return
        if status.state == "ready":
            # Like Claude Code: only problems are announced; /mcp lists connected servers.
            skipped: list[str] = []
            added = register_mcp_prompts(self.commands, status, self.conversation.mcp_manager, skipped=skipped)
            if added:
                self.call_later(self._refresh_mcp_hints)
            for note in skipped:
                if self._task_running:
                    self._mcp_notices.append(note)
                else:
                    self.call_later(self._local_note, "└ " + note)
        elif self._task_running:
            # Keep the running reply contiguous; report once the task ends.
            self._mcp_notices.append(status.notice())
        else:
            self.call_later(self._local_note, "└ " + status.notice())
        if not self._generating:
            self._status(self._status_note)

    def _on_memory(self, event) -> None:
        """From the background extractor; a running task's output stays contiguous, so wait for it."""
        if self._closing:
            return
        if isinstance(event, MemoryUpdated):
            text, style = "✎ 已更新记忆：" + "、".join(event.names), PARAMETER_COLOR
        else:
            text, style = "! " + event.text, OPERATION_COLOR
        if self._task_running:
            self._reply_task.add_done_callback(
                lambda _: self._closing or self.call_later(self._local_note, text, style)
            )
        else:
            self.call_later(self._local_note, text, style)
        self._usage = self.conversation.total_usage  # Extraction requests are a real cost.
        if not self._generating:
            self._status(self._status_note)

    # Sub-agents -------------------------------------------------------------------

    def _on_task_change(self, task) -> None:
        if self._closing:
            return
        loop_info = getattr(task, "loop", None)
        if loop_info is not None:
            if task.status != "running" and not getattr(task, "stopped", False) and task.id not in self._announce.ended:
                self._announce.ended.add(task.id)
                reason = f"（{loop_info.end_reason}）" if getattr(loop_info, "end_reason", "") else ""
                self._task_note(f"定时巡检 {task.id} {STATUS_LABELS.get(task.status, task.status)}{reason}")
        else:
            self._announce(task)
        row = self._agent_rows.get(task.call_id) if task.call_id else None
        if row is not None:
            row.subagent = task
            row.progress(task)
        if not self._generating:
            self._status(self._status_note)

    def _is_loop_busy(self) -> bool:
        return bool(
            self._closing
            or self._generating
            or self._task_running
            or self.conversation.busy
            or self._approval_request is not None
            or self._question is not None
            or (self.query("#connect-panel") and self.query_one("#connect-panel").display)
            or (self.conversation.goal is not None and self.conversation.goal.active)
            or self.conversation.notifications
        )

    def _notify_loops_idle(self) -> None:
        if self._closing:
            return
        loops = getattr(self.conversation, "loops", None)
        if loops is not None and not self._is_loop_busy():
            loops.notify_idle()

    async def _await_loop_reply(self, task, reply_task: asyncio.Task | None) -> None:
        if reply_task is None or reply_task.done():
            return
        try:
            await asyncio.shield(reply_task)
        except asyncio.CancelledError:
            job = getattr(task, "job", None)
            if getattr(task, "stopped", False) or self._closing or (job is not None and job.cancelling()):
                raise
        except Exception:
            pass

    async def _trigger_loop(self, task, prompt: str) -> None:
        if self._closing:
            return
        before = self._reply_task
        runs = getattr(getattr(task, "loop", None), "runs", 1)
        first = next((line.strip() for line in prompt.splitlines() if line.strip()), prompt.strip())
        await self._local_note(f"⏰ 定时巡检 {task.id}（第 {runs} 次）：{first[:100]}", PARAMETER_COLOR)
        if prompt.lstrip().startswith("/"):
            parsed = parse_input(prompt, self.commands)
            if parsed.kind == "unknown":
                await self._local_note(unknown_note(parsed, self.commands), OPERATION_COLOR)
                self._status(self._status_note)
                return
            if parsed.kind == "command":
                try:
                    await dispatch(self._command_context(), parsed)
                except ProviderError as exc:
                    await self._local_note(f"! {exc}", OPERATION_COLOR)
                self._status(self._status_note)
                if self._reply_task is not before:
                    await self._await_loop_reply(task, self._reply_task)
                return
        try:
            from .loop_schedule import LOOP_SYNTHETIC, format_loop_prompt

            rendered = format_loop_prompt(task)
            synthetic = LOOP_SYNTHETIC
        except ImportError:
            rendered = f"[定时任务 {task.id} · 第 {runs} 次]\n{prompt}"
            synthetic = "loop"
        await self.submit(rendered, synthetic=synthetic)
        self._status(self._status_note)
        if self._reply_task is not before:
            await self._await_loop_reply(task, self._reply_task)

    def _task_note(self, text: str) -> None:
        """Like memory notes, a running task's output stays contiguous."""
        if self._task_running:
            self._reply_task.add_done_callback(
                lambda _: self._closing or self.call_later(self._local_note, "└ " + text)
            )
        else:
            self.call_later(self._local_note, "└ " + text)

    def _schedule_notify(self) -> None:
        """Background results wait while the main agent is busy, then start a turn of their own."""
        if self._closing:
            return
        if not self._task_running:
            self.call_later(self._start_notify)
        elif not self._notify_waiting:
            self._notify_waiting = True
            self._reply_task.add_done_callback(self._notify_after)

    def _notify_after(self, _) -> None:
        self._notify_waiting = False
        if not self._closing:
            self.call_later(self._start_notify)

    async def _start_notify(self) -> None:
        if self._closing or not self.conversation.notifications:
            return
        if self._task_running:
            self._schedule_notify()
            return
        await self._start_request("", show_user=False, notify=True)

    def action_background(self) -> None:
        """Ctrl+B moves every foreground command and sub-agent to the background; Esc still cancels."""
        commands = getattr(self.conversation, "commands", None)
        moved = commands.detach_foreground() if commands is not None else 0
        count = self.conversation.tasks.detach_foreground()
        parts = [f"{moved} 个命令" if moved else "", f"{count} 个子 Agent" if count else ""]
        self._hint_until = monotonic() + 2
        if moved or count:
            moved_text = "和".join(part for part in parts if part)
            # A space only after a Latin word: "1 个子 Agent 转入后台" but "1 个命令转入后台".
            self._status(f"已把 {moved_text}{' ' if moved_text[-1].isascii() else ''}转入后台")
        else:
            self._status("没有在前台运行的命令或子 Agent")

    async def action_toggle_details(self) -> None:
        """Ctrl+O toggles expanded thinking blocks and full tool outputs/diffs."""
        self._show_details = not self._show_details
        chat = self.query_one("#chat", VerticalScroll)
        live = [w for w in chat.children if isinstance(w, (ChatEntry, ToolEntry))]
        for widget in live:
            widget.set_details(self._show_details)
        has_live_turn = any(isinstance(w, ToolEntry) or (isinstance(w, ChatEntry) and w.speaker != "You") for w in live)
        if self._show_details and not has_live_turn and self._last_turn_entries:
            block = turn_details_block(self._last_turn_entries)
            if block is not None and not self._closing and not self._exit and chat.is_attached:
                note = Static(block, classes="local-note")
                note.transcript = block
                await chat.mount(note)
                chat.anchor()
                await self._commit_ready_transcript()
        self._hint("详情视图已开启（Ctrl+O 收起）" if self._show_details else "详情视图已收起（Ctrl+O 展开）")
        self.call_later(self._fit_chat)

    async def action_quit(self) -> None:
        async def leave() -> None:
            self.exit()

        await confirm_tasks(self, self.conversation, leave)

    def on_resize(self, event: events.Resize) -> None:
        self.call_later(self._refresh_layout)
        width, self._width = self._width, event.size.width
        if self.is_inline and width is not None and width != event.size.width:
            # A drag sends many resizes; repaint once the width settles.
            if self._redraw_timer is not None:
                self._redraw_timer.stop()
            self._redraw_timer = self.set_timer(0.2, self._redraw_after_resize)

    async def _redraw_after_resize(self) -> None:
        self._redraw_timer = None
        if self._closing:
            return
        async with self._scrollback_lock:
            with self.batch_update():
                redraw_transcript(self, self._history)

    def on_session_info_resized(self) -> None:
        self._fit_chat()

    def _refresh_layout(self) -> None:
        if self.query("#banner"):
            self.query_one("#banner", Static).update(startup_banner(self.provider_name))
        self._status(self._status_note)
        self._fit_chat()
        if self._hint_choices:
            self._refresh_hints(self.query_one("#prompt", PromptArea).value)

    def _fit_chat(self) -> None:
        if not self.is_running or not self.query("#chat") or not self.query("#status"):
            return
        # Reserve the actual controls, rather than a percentage or spare rows.
        input_row = self.query_one("#input-row")
        # The prompt grows with its text; its region may still hold the previous height.
        prompt_rows = min(8, max(1, self.query_one("#prompt", PromptArea).wrapped_document.height))
        reserved = self.screen.styles.gutter.height + max(1, self.query_one("#status").outer_size.height)
        if input_row.display:  # Hidden while a member view is open or a card stands in its place.
            # Its two rules and the prompt.
            reserved += max(2 + prompt_rows, input_row.region.height) + input_row.styles.margin.height
        activity = self.query_one("#activity")
        if activity.display and not activity.has_class("idle"):  # The running stage, above prompt or card.
            reserved += 1
        members = self.query_one("#member-panel", MemberPanel)
        if members.display:
            reserved += members.lines
        queue = self.query_one("#queue")
        if queue.display:
            reserved += 1 + min(4, len(self.conversation.queued))
        todo = self.query_one("#todo-panel")
        if todo.display:
            reserved += 1 + (
                1 if self._todo_shown is None else min(6, len(str(todo_panel(self._todo_shown)).splitlines()))
            )
        if self.query_one("#model-panel").display:
            extra = 1 if self.query_one("#model-filter").display else 0
            reserved += 4 + extra + min(8, self._panel_rows)
        if self.query_one("#connect-panel").display:
            opt_rows = (
                min(8, max(1, len(self._connect_choices)))
                if self.query_one("#connect-options").display
                else (1 if self.query_one("#connect-input").display else 0)
            )
            # Two rules, title, help, plus options/input rows; cap #connect-status so choices never clip.
            fixed = 4 + opt_rows
            limit = max(fixed, self.size.height - reserved - 1)
            self.query_one("#connect-panel").styles.max_height = limit
            status_widget = self.query_one("#connect-status")
            status_rows = 0
            if status_widget.display and self._connect_status_text.plain:
                status_cap = max(1, limit - fixed)
                status_widget.styles.max_height = status_cap
                width = max(10, self.size.width - self.screen.styles.gutter.width - 2)
                status_rows = min(status_cap, len(self._connect_status_text.wrap(self.console, width)))
            else:
                status_widget.styles.max_height = None
            reserved += min(limit, fixed + status_rows)
        if self.query_one("#approval-panel").display:
            # Keep the choices and status reachable in a short terminal; only
            # the preview shrinks. Completed history already lives in scrollback.
            # Two rules, the blank row above the choices, one row per choice and up to two rows of help.
            fixed = 5 + (len(self._approval_request.choices) if self._approval_request else 4)
            limit = max(fixed, self.size.height - reserved - 1)
            self.query_one("#approval-panel").styles.max_height = limit
            self.query_one("#approval-detail").styles.max_height = min(20, max(1, limit - fixed))
            # Its current size may predate the new preview, so its rows are counted from the text. Only what it
            # needs is reserved: the tool row it asks about stays in sight above it.
            width = max(10, self.size.width - self.screen.styles.gutter.width - 2)
            preview = len(self._approval_text.wrap(self.console, width))
            reserved += min(limit, fixed + min(20, preview))
        if self.query_one("#question-panel").display and self._question is not None:
            question = self._question.questions[self._question_step]
            width = max(10, self.size.width - 2)
            # Two rules, heading, the wrapped question, one row per option and "其他", help.
            reserved += 4 + -(-cell_len(question.question) // width) + len(question.options) + 1
        hints = self.query_one("#command-hints")
        hint_limit = min(8, max(1, self.size.height - reserved - 3))
        if hints.styles.max_height is None or hints.styles.max_height.value != hint_limit:
            hints.styles.max_height = hint_limit
        # Reserve hints before layout: their old size may still be zero on show.
        hint_rows = min(len(self._hint_choices), hint_limit)
        hints.styles.height = max(1, hint_rows)
        if hints.display:  # The list, its key help and the blank row below that.
            reserved += hint_rows + 2
        chat = self.query_one("#chat")
        available = max(1, self.size.height - reserved)
        view = self.query_one("#member-view", MemberView)
        if view.display:  # The whole height but the status line (and a card).
            height = max(3, int(available * VIEW_SHARE))
            if view.styles.height is None or view.styles.height.value != height:
                view.styles.height = height
            return
        if chat.styles.max_height is None or chat.styles.max_height.value != available:
            chat.styles.max_height = available

    async def _commit_ready_transcript(self) -> None:
        if not self.is_inline or self._closing:
            return
        async with self._scrollback_lock:
            chat = self.query_one("#chat", VerticalScroll)
            ready, renderables = [], []
            for widget in list(chat.children):
                if isinstance(widget, ChatEntry):
                    if widget.speaker != "You" and widget.phase not in {"complete", "tools", "interrupted"}:
                        break
                    rendered = widget.transcript()
                elif isinstance(widget, ToolEntry):
                    if widget.elapsed is None:
                        break
                    rendered = widget.transcript
                elif isinstance(widget, CompactRow):
                    if widget.transcript is None:
                        break
                    rendered = widget.transcript
                elif widget.id == "banner":
                    rendered = startup_banner(self.provider_name)
                elif hasattr(widget, "transcript"):
                    rendered = widget.transcript
                else:
                    break
                ready.append(widget)
                if rendered is not None:
                    renderables.append(rendered)
            if ready:
                with self.batch_update():
                    if renderables:
                        commit_transcript(self, renderables)
                        self._history.extend(renderables)
                    await chat.remove_children(ready)
                    self._fit_chat()

    def _stage(self, note: str) -> None:
        """What the running task is doing, on the line above the prompt. A short hint keeps its place in
        the status bar until it expires; with no task running the note is an ordinary status note."""
        if not self._generating:
            self._status(note)
            return
        self._stage_note = note
        self._status(self._status_note if monotonic() < self._hint_until else "")

    def _paint_activity(self) -> None:
        activity = self.query("#activity")
        if not activity:
            return
        if not self._generating:
            self._stage_note = ""
        if activity.first().has_class("idle") == self._generating:
            activity.set_class(not self._generating, "idle")
            self.call_later(self._fit_chat)  # One row more or less above the prompt.
        now = monotonic()
        line = activity.first(Static)
        width = line.content_size.width
        states = tuple(row.state for row in self._recent_tools)
        shown = (self._stage_note, int(now * 5) if self._stage_note else 0, states, width)
        if shown != self._activity_shown:
            self._activity_shown = shown
            line.update(activity_row(self._stage_note, now, states, width) if self._stage_note else Text(""))

    def _record_tool(self, row: ToolEntry) -> None:
        """Keep a finished call's row for the activity line; a call reported twice keeps its place."""
        self._recent_tools[row] = None
        while len(self._recent_tools) > RECENT_TOOLS:
            del self._recent_tools[next(iter(self._recent_tools))]

    def _status(self, note: str) -> None:
        self._status_note = note
        self._paint_activity()
        muted = MUTED_COLOR
        text = Text()
        registry = self.conversation.registry
        mode = registry.permissions.mode if registry is not None else None
        for row in self.query("#input-row"):
            for name in MODE_COLORS:
                row.set_class(name == mode, f"mode-{name}")
        if registry is None:
            text.append("未启用工具", style="bold")
        else:
            color = MODE_COLORS.get(mode)
            text.append(MODE_BADGES[mode], style=f"bold {color}" if color else "bold")
            if getattr(self.conversation, "coordinator_mode", getattr(self.conversation, "coordinator", False)):
                text.append(" " + COORDINATOR_BADGE, style=f"bold {OPERATION_COLOR}")
            if badge := sandbox_badge():
                text.append(" · ", style=muted)
                # Partial confines writes only: amber, like an operation still awaiting approval.
                colors = {"沙箱": TOOL_SUCCESS_COLOR, "沙箱·部分": OPERATION_COLOR}
                text.append(badge, style=colors.get(badge, ERROR_COLOR))
            if not self._generating and self.size.width >= 80:
                text.append(" (Shift+Tab 切换)", style=muted)
        if self.provider_name != self._banner_provider:
            # The banner names the model; after /model it is stale, so the status bar names the new one.
            text.append(" · " + self.provider_name, style=muted)
        if (thinking := getattr(self.conversation, "thinking", None)) is not None:
            unsupported = self.conversation.thinking_problem() is not None
            text.append(f" · 思考 {thinking}" + ("（不支持）" if unsupported else ""), style=muted)
        if (goal := getattr(self.conversation, "goal", None)) is not None:
            if goal.paused:
                text.append(" · ◎ 目标已暂停", style=OPERATION_COLOR)
            else:
                text.append(f" · ◎ 目标 {elapsed_text(monotonic() - goal.started)}", style=TOOL_SUCCESS_COLOR)
        if note:
            stage, separator, rest = note.partition(" · ")
            stage_color = (
                ERROR_COLOR
                if stage.startswith("失败")
                else OPERATION_COLOR
                if self._generating or stage.startswith(("已取消", "已停止"))
                else None
            )
            text.append(" · ", style=muted)
            text.append(stage, style=stage_color)
            if separator:
                text.append(separator + rest, style=muted)
        manager = self.conversation.mcp_manager
        if manager is not None and manager.pending:
            total = len(manager.connections)
            text.append(f" · MCP 连接中 {total - manager.pending}/{total}", style=OPERATION_COLOR)
        queued = len(self.conversation.queued)
        if queued:
            text.append(f" · 已排队 {queued} 条", style=OPERATION_COLOR)
        tasks = self.conversation.tasks
        loops = getattr(self.conversation, "loops", None)
        active_loops = (
            loops.active()
            if loops is not None and callable(getattr(loops, "active", None))
            else [t for t in tasks.running() if getattr(t, "loop", None) is not None]
        )
        background = len([t for t in tasks.background() if getattr(t, "loop", None) is None])
        if background:
            text.append(f" · 后台任务 {background}", style=OPERATION_COLOR)
        if active_loops:
            text.append(f" · 定时 {len(active_loops)}", style=TOOL_SUCCESS_COLOR)
        commands = getattr(self.conversation, "commands", None)
        if any(not task.background and task.origin == "agent" for task in tasks.running()) or (
            commands is not None and commands.foreground_count()
        ):
            text.append(" · Ctrl+B 转入后台", style=OPERATION_COLOR)
        teams = getattr(self.conversation, "teams", None)
        if teams is not None and teams.current is not None:
            running, idle = teams.counts()
            text.append(f" · 团队 {teams.current.name} 运行 {running} / 空闲 {idle}", style=muted)
            asking = sum(self._asking().values())  # Pane members' too.
            if asking:
                text.append(f" · 成员审批 {asking}", style=OPERATION_COLOR)
            if (
                self.query("#member-panel")
                and self.query_one("#member-panel").display
                and not self.query_one("#member-panel", MemberPanel).expanded
            ):
                text.append(" · Ctrl+T 成员", style=muted)
        usage = Text()
        if self._usage.requests:
            if self._context_share is not None:
                used, window, percent, near = self._context_share
                usage.append(f"上下文 约 {used} / {window}（{percent}%）", style=OPERATION_COLOR if near else muted)
            if (rate := cache_hit_rate(self._usage)) is not None:
                usage.append((" · " if usage else "") + f"缓存命中 {rate}%", style=muted)
        if usage:
            # The numbers sit at the right edge when the line has room, and follow the rest when it does not.
            gap = self.size.width - self.screen.styles.gutter.width - text.cell_len - usage.cell_len
            text.append(" " * gap if gap >= 2 else " · ", style=muted)
            text.append_text(usage)
        status = self.query("#status")
        if not status:  # Timers and MCP callbacks can fire while the screen is torn down.
            return
        status.first(Static).update(text)
        self.call_later(self._fit_chat)

    def _refresh_context(self) -> None:
        """Cached: the ticker repaints every 0.2s, the estimate changes only with the history."""
        estimate = self.conversation.context_estimate() if self._usage.requests else None
        if estimate is None:
            self._context_share = None
            return
        budget = self.conversation.budget
        self._context_share = (
            approx_tokens(estimate),
            tokens_label(budget.window),
            round(100 * estimate / budget.effective),
            estimate >= 0.8 * budget.auto_threshold,
        )

    def _tick(self) -> None:
        if not self.is_running:
            return  # Textual stops running before it unmounts the screen widgets.
        self._paint_activity()  # Keeps the spinner turning in stages that have nothing else to refresh.
        loops = getattr(self.conversation, "loops", None)
        if loops is not None and any(getattr(t.loop, "pending_fire", False) for t in loops.active()):
            # A turn's end can race the reply task's completion; the ticker retries a coalesced wakeup.
            self._notify_loops_idle()
        if self._approval_request is not None and not self._waiting(self._approval_request):
            # Withdrawn without an answer, e.g. a sandboxed command ended while asking to connect.
            self._hide_approval()
        if self._question is not None and not self._waiting(self._question):
            self._hide_question()  # The turn was cancelled while it waited.
        if self._generating and self._shell_row is not None:
            self._shell_row.tick()
            if self._approval_request is not None:  # A sandboxed command asking to connect.
                self._stage("等待授权 · Esc 拒绝本次 · Ctrl+C 取消任务")
                return
            self._stage(f"执行命令中 · {monotonic() - self._shell_row.started:.1f}s · Esc 取消")
            return
        if self._generating and self._goal_checking:
            self._stage(f"判断目标是否达成 · {monotonic() - self.started:.1f}s · Esc 暂停目标")
            return
        if self._generating and self._compacting is not None:
            if self._compact_row is not None:
                self._compact_row.tick()
            self._stage(f"压缩上下文中 · {monotonic() - self._compacting:.1f}s · Esc 取消")
            return
        if self._generating and self._active_reply is not None:
            if self._approval_request is not None:
                self._stage("等待授权 · Esc 拒绝本次 · Ctrl+C 取消任务")
                return
            if self._question is not None:
                self._stage(
                    "等待回答 · 输入回答后 Enter 提交 · Esc 返回选项"
                    if self._question_typing
                    else "等待回答 · Esc 不回答 · Ctrl+C 取消任务"
                )
                return
            if self._retry_notice:
                self._stage(self._retry_notice)
                return
            self._active_reply.refresh_timer()
            for row in self._agent_rows.values():
                if row.subagent is not None:
                    row.progress(row.subagent)
            label = {"waiting": "连接中", "thinking": "思考中", "streaming": "输出中", "tools": "执行工具中"}.get(
                self._active_reply.phase, "处理中"
            )
            self._stage(
                f"{label} · {self._iteration}/{self._iteration_limit} 轮 · {monotonic() - self.started:.1f}s · Esc 取消"
            )

    @property
    def _task_running(self) -> bool:
        return self._reply_task is not None and not self._reply_task.done()

    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        prompt = event.value
        field = event.text_area
        if self._question is not None and self._question_typing:
            if prompt.strip():
                question = self._question.questions[self._question_step]
                checked = tuple(question.options[i][0] for i in sorted(self._question_checked))
                self._next_question(Answer(checked, prompt.strip()))
            return
        parsed = parse_input(prompt, self.commands)
        if parsed.kind == "empty":
            return
        running_ok = (
            parsed.kind == "command"
            and parsed.command is not None
            and parsed.command.runs_while_running(parsed.argument)
        )
        if parsed.kind == "shell" and not parsed.text:
            self._hint_until = monotonic() + 2
            self._status("在 ! 后面输入要执行的命令；!! 开头只执行，不加入对话")
            return
        if self._task_running and not running_ok:
            if parsed.kind == "shell":
                self._hint_until = monotonic() + 2
                self._status("运行中不能执行 ! 命令，请在完成后发送 · Esc 取消")
                return
            if parsed.kind != "message":
                # Commands change state the running task depends on; keep the text for later.
                self._hint_until = monotonic() + 2
                self._status(
                    "运行中只能排队消息和使用 /tasks、/todo、/goal、/status、/worktree list，其他命令请在完成后发送 · Esc 取消"
                )
                return
            field.remember(prompt)
            field.clear()
            self.conversation.queue(parsed.text, field.images_in(parsed.text))
            self._refresh_queue()
            return
        field.remember(prompt)
        field.clear()
        self._dismissed_prompt = None
        self._refresh_hints("")
        if parsed.kind == "shell":
            await self._start_shell(parsed.text, keep=parsed.word == "!")
            return
        if parsed.kind != "message":
            await self._command(prompt.strip(), parsed)
            return
        await self._start_request(parsed.text, images=field.images_in(parsed.text))

    async def _start_request(
        self,
        prompt: str,
        *,
        show_user: bool = True,
        shown: tuple[str, ...] = (),
        command: str | None = None,
        fork=None,
        notify: bool = False,
        images: dict[str, ProcessedImage] | None = None,
        synthetic: str | None = None,
    ) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        reply = ChatEntry("MossCode", show_details=self._show_details)
        self._active_reply = reply
        self._last_turn_entries = [reply]
        if show_user:
            for text in shown or (prompt,):
                await chat.mount(ChatEntry("You", text, self._image_notes(text)))
        await chat.mount(reply)
        chat.anchor()
        self.started = monotonic()
        self._iteration = 0
        self._generating = True
        self._stage("连接中…")
        await self._commit_ready_transcript()
        self._reply_task = asyncio.create_task(self._respond(prompt, reply, command, fork, notify, images, synthetic))

    async def _respond(
        self,
        prompt: str,
        reply: ChatEntry,
        command: str | None = None,
        fork=None,
        notify: bool = False,
        images: dict[str, ProcessedImage] | None = None,
        synthetic: str | None = None,
    ) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        tool_rows: dict[str, ToolEntry] = {}
        continuation = False
        completed = False
        if not notify:
            # A finished checklist stays until the user moves on to the next request.
            items = self.conversation.todos
            if items and todos_completed(items) == len(items):
                self._todo_dismissed = items
            self._refresh_todos()

        try:
            turn = (
                self.conversation.notify()
                if notify
                else self.conversation.reply(prompt, command=command, fork=fork, images=images, synthetic=synthetic)
            )
            async with aclosing(turn) as stream:
                async for event in stream:
                    if not isinstance(event, Retrying):
                        self._retry_notice = ""
                    if isinstance(event, IterationProgress):
                        self._iteration, self._iteration_limit = event.iteration, event.max_iterations
                        reply.phase = "waiting"
                        self._tick()
                    elif isinstance(event, UsageUpdate):
                        self._usage = event.total
                        self._refresh_context()
                        self._tick()
                    elif isinstance(event, CompactStarted):
                        # Before a reply that has not started yet, so the row reads in time order.
                        await self._show_compact(event.manual, None if continuation else reply)
                    elif isinstance(event, (CompactFinished, CompactFailed, ResultsCleared, ContextCollapsed)):
                        await self._end_compact(
                            compact_notice(event), self._compact_style(event), None if continuation else reply
                        )
                        self._refresh_context()
                    elif isinstance(event, RunStopped):
                        completed = event.reason == "completed"
                        for result in event.results:
                            if result.call_id in tool_rows:
                                tool_rows[result.call_id].complete(result)
                                self._record_tool(tool_rows[result.call_id])
                        if event.reason in STOPPED_EARLY:
                            await self._local_note("└ " + event.notice)
                            self._status("已停止 · 可以继续提问")
                    elif isinstance(event, Retrying):
                        self._retry_notice = f"{event.reason} · {event.delay:.0f} 秒后重试（{event.attempt}/{event.max_attempts}）· Esc 取消"
                        self._stage(self._retry_notice)
                    elif isinstance(event, ReplyRetry):
                        if not continuation:  # Deltas of the dropped reply already went into this entry.
                            reply.discard()
                        self._hint(f"{event.reason}，重新请求（{event.attempt}/{event.max_attempts}）")
                    elif isinstance(event, AutoContinue):
                        # The cut reply is finished; the next piece gets its own entry, after this note.
                        await self._local_note("↻ " + event.text, PARAMETER_COLOR)
                        continuation = True
                        self._generating = True
                    elif isinstance(event, Delta):
                        if continuation:
                            reply = ChatEntry("MossCode", show_details=self._show_details)
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                            self._last_turn_entries.append(reply)
                            continuation = False
                        reply.append(event)
                        self._tick()
                    elif isinstance(event, ResponseBoundary):
                        if continuation:
                            reply = ChatEntry("MossCode", show_details=self._show_details)
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                            self._last_turn_entries.append(reply)
                        await reply.seal_tool_request(event.message)
                        tool_rows = {}
                        continuation = True
                    elif isinstance(event, ToolProgress):
                        if event.phase == "parsing":
                            self._stage("解析工具参数… · Esc 取消")
                        else:
                            if event.phase == "running":
                                reply.phase = "tools"
                            row = tool_rows.get(event.call.id)
                            if row is None:
                                if event.call.name == "exit_plan_mode":
                                    plan = plan_text(event.call.arguments)
                                    if plan:
                                        await chat.mount(PlanEntry(plan))
                                row = ToolEntry(
                                    event,
                                    shell_label(self.conversation.registry),
                                    show_details=self._show_details,
                                )
                                tool_rows[event.call.id] = row
                                self._last_turn_entries.append(row)
                                if event.call.name == "agent":
                                    self._agent_rows[event.call.id] = row
                                await chat.mount(row)
                            if event.result is not None:
                                row.complete(event.result, event.elapsed)
                                self._record_tool(row)
                                if event.call.name == "todo_write":
                                    self._refresh_todos()
                                reply.phase = (
                                    "tools" if any(r.elapsed is None for r in tool_rows.values()) else "waiting"
                                )
                            self._tick()
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    elif isinstance(event, FilesAttached):
                        await chat.mount(self._note(attached_line(event.entries), PARAMETER_COLOR), before=reply)
                    elif isinstance(event, UserInput):
                        # Queued messages joined the context at this iteration boundary.
                        for text in event.texts:
                            await chat.mount(ChatEntry("You", text, self._image_notes(text)))
                        chat.anchor()
                        self._refresh_queue()
                    elif isinstance(event, ApprovalRequest):
                        self._show_approval(event)
                        if event.call_id in tool_rows:
                            row = tool_rows[event.call_id]
                            row.update(row._heading("running") + Text("\n  └ 等待用户确认…", style=OPERATION_COLOR))
                    elif isinstance(event, QuestionRequest):
                        self._show_question(event)
                        if event.call_id in tool_rows:
                            row = tool_rows[event.call_id]
                            row.update(row._heading("running") + Text("\n  └ 等待用户回答…", style=OPERATION_COLOR))
                    elif isinstance(event, Completed):
                        if continuation:
                            reply = ChatEntry("MossCode", show_details=self._show_details)
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                            self._last_turn_entries.append(reply)
                        self._generating = False
                        await reply.finish(event.message, cut=event.stop_reason == "max_tokens")
                        self._status("")
                    await self._commit_ready_transcript()
        except asyncio.CancelledError:
            if not self._closing:
                reply.interrupted("已取消，部分回复未加入对话历史。已执行的工具结果保留。")
                self._status("已取消 · 可以重新发送")
        except ProviderError as exc:
            reply.interrupted(str(exc))
            self._status("失败 · 未完成正文未保存，已执行工具结果保留")
        except Exception:
            reply.interrupted("出现内部错误，请检查安装环境。")
            self._status("失败 · 未完成正文未保存，已执行工具结果保留")
        finally:
            if self._compact_row is not None:
                await self._end_compact(CANCELLED_COMPACT)
            self._generating = False
            self._compacting = None
            self._retry_notice = ""
            self._active_reply = None
            self._agent_rows = {}
            self._refresh_context()
            if not self._closing:
                self._hide_question()
                self._hide_approval()
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                field.disabled = False
                if self._approval_request is None:  # A member's card still waiting keeps the keys.
                    self._focus_home()  # A member view or the open panel keeps them too.
                queued = self.conversation.take_queued()
                if queued and completed:
                    # The model answered before the queue could join; send it as the next task.
                    text = "\n\n".join(queued)
                    self.call_later(self._start_request, text, shown=queued, images=field.images_in(text))
                elif queued:
                    # Stopped, cancelled or failed: never resend automatically.
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                if getattr(self.conversation, "goal", None) is not None and not (queued and completed):
                    self.call_later(self._start_goal_check)
                self._refresh_queue()
                notices, self._mcp_notices = self._mcp_notices, []
                for notice in notices:
                    await self._local_note("└ " + notice)
                self.call_later(self._notify_loops_idle)

    async def _start_goal_check(self) -> None:
        """After a turn while a goal is set: judge it, then continue, end or pause."""
        if self._closing or self._task_running or getattr(self.conversation, "goal", None) is None:
            return
        self.started = monotonic()
        self._generating = self._goal_checking = True
        self._tick()
        self._reply_task = asyncio.create_task(self._check_goal())

    async def _check_goal(self) -> None:
        step = GoalStep("none")
        try:
            step = await self.conversation.goal_after_turn()
        except asyncio.CancelledError:
            if self._closing:
                raise
            step = self.conversation.pause_goal("判断时按了 Esc")
        finally:
            self._generating = self._goal_checking = False
        if self._closing:
            return
        if step.note:
            style = (
                TOOL_SUCCESS_COLOR if step.action == "met" else OPERATION_COLOR if step.action != "continue" else None
            )
            await self._local_note(step.note, style)
        self._status("")
        field = self.query_one("#prompt", PromptArea)
        queued = self.conversation.take_queued()
        if queued:
            # Typed while the goal was judged: the user's message goes first and resumes a paused goal.
            text = "\n\n".join(queued)
            self.call_later(self._start_request, text, shown=queued, images=field.images_in(text))
            self._refresh_queue()
        elif step.action == "continue" and not self.conversation.notifications:
            self.call_later(self._start_request, step.prompt, show_user=False, synthetic=GOAL)
        else:
            self.call_later(self._notify_loops_idle)

    def action_cycle_mode(self) -> None:
        registry = self.conversation.registry
        if (
            registry is None
            or self._approval_request
            or self._question
            or self.query_one("#model-panel").display
            or self.query_one("#connect-panel").display
        ):
            return
        if self._generating:
            self._hint_until = monotonic() + 2
            self._status("运行中不能切换权限档位 · Esc 取消")
            return
        self.conversation.set_permission_mode(MODE_CYCLE[registry.permissions.mode])
        self._status("")

    def action_cancel(self) -> None:
        if self._question is not None:
            if self._question_typing:  # Back to the options.
                self._question_typing = False
                self.query_one("#prompt", PromptArea).value = self._question_draft
                other = len(self._question.questions[self._question_step].options)
                self._render_question(other)  # Still on "其他".
            else:
                self._finish_question()  # The rest stay unanswered.
            return
        if self._approval_request:
            self._answer_approval("deny")
            return
        if self.query_one("#member-view").display:  # Back to the panel.
            self._close_view()
            return
        if self.query_one("#member-panel", MemberPanel).expanded:
            self._collapse_members()
            return
        if self.query_one("#connect-panel").display:
            self._connect_back()
            return
        if self.query_one("#model-panel").display:
            cancel = self._on_cancel
            self._close_model_panel()
            if cancel is not None:
                self.call_later(cancel)
            return
        if self._hint_choices:
            self._dismissed_prompt = self.query_one("#prompt", PromptArea).value
            self._refresh_hints(self._dismissed_prompt)
            return
        queued = self._take_back_queued()
        if queued:
            # The first Esc only withdraws queued messages; the next one cancels the task.
            self.query_one("#prompt", PromptArea).restore_queued(queued)
            self._hint_until = monotonic() + 2
            self._status("已取回排队消息，任务继续 · 再按 Esc 取消任务")
            return
        if not self._task_running and not self._generating and not self.query_one("#prompt", PromptArea).value:
            now = monotonic()
            if now - self._escape_at <= DOUBLE_ESCAPE:
                self._escape_at = 0.0
                self._open_rewind()
            else:
                self._escape_at = now
            return
        self.action_cancel_task()

    def action_cancel_task(self) -> None:
        if self._generating and self._reply_task and not self._reply_task.done():
            if not self._reply_task.cancelling():
                self._reply_task.cancel()

    async def on_unmount(self) -> None:
        self._closing = True
        if self._members_task is not None and not self._members_task.done():
            self._members_task.cancel()
        if self.conversation.registry:
            self.conversation.registry.permissions.interactive = False
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        if self._reply_task and not self._reply_task.done():
            if not self._reply_task.cancelling():
                self._reply_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reply_task
        await self.conversation.close()
