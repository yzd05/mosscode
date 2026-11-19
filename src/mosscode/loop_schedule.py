"""Session-scoped /loop recurring scheduler with idle-only coalescing and jitter."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .agents.tasks import LoopInfo, Task, TaskManager

LOOP_SYNTHETIC = "loop"
MIN_INTERVAL = 10.0
MAX_INTERVAL = 7 * 86400.0
DEFAULT_INTERVAL = 300.0
DEFAULT_INTERVAL_TEXT = "5 分钟"
MAX_PROMPT_CHARS = 4000
MAX_ACTIVE_LOOPS = 20
DEFAULT_MAX_RUNS = 100
MAX_RUNS_LIMIT = 1000
MAX_LIFESPAN = 7 * 86400.0
JITTER_RATIO = 0.10
MAX_JITTER = 30.0

_UNITS: dict[str, tuple[float, str]] = {
    "s": (1.0, "秒"),
    "sec": (1.0, "秒"),
    "secs": (1.0, "秒"),
    "second": (1.0, "秒"),
    "seconds": (1.0, "秒"),
    "秒": (1.0, "秒"),
    "秒钟": (1.0, "秒"),
    "m": (60.0, "分钟"),
    "min": (60.0, "分钟"),
    "mins": (60.0, "分钟"),
    "minute": (60.0, "分钟"),
    "minutes": (60.0, "分钟"),
    "分": (60.0, "分钟"),
    "分钟": (60.0, "分钟"),
    "h": (3600.0, "小时"),
    "hr": (3600.0, "小时"),
    "hrs": (3600.0, "小时"),
    "hour": (3600.0, "小时"),
    "hours": (3600.0, "小时"),
    "小时": (3600.0, "小时"),
    "d": (86400.0, "天"),
    "day": (86400.0, "天"),
    "days": (86400.0, "天"),
    "天": (86400.0, "天"),
}

_UNIT_PATTERN = (
    r"(?:seconds|second|secs|sec|s|minutes|minute|mins|min|m|hours|hour|hrs|hr|h|days|day|d|秒钟|秒|分钟|分|小时|天)"
)
_INTERVAL_TOKEN_RE = re.compile(rf"^(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})$", re.IGNORECASE)
_LEADING_INTERVAL_RE = re.compile(
    rf"^(?:every\s+)?(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})(?:\s+|$)",
    re.IGNORECASE,
)
_TRAILING_EVERY_RE = re.compile(
    rf"(?:^|\s+)every\s+(?:(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})|(second|minute|hour|day))\s*$",
    re.IGNORECASE,
)
_LEADING_OPT_RE = re.compile(r"^--(max-runs|count)(?:=(\S*)|(?:\s+(\S+))?)(?:\s+|$)", re.IGNORECASE)
_DANGLING_OPT_RE = re.compile(r"(?:^|\s+)--(max-runs|count)=?\s*$", re.IGNORECASE)
_TRAILING_OPT_RE = re.compile(r"(?:^|\s+)--(max-runs|count)(?:=(\S+)|\s+(\S+))\s*$", re.IGNORECASE)
_NESTED_LOOP_RE = re.compile(r"^/loop(?:\s|$)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedLoop:
    action: Literal["list", "stop", "create"]
    target: str = ""
    interval: float = DEFAULT_INTERVAL
    interval_text: str = DEFAULT_INTERVAL_TEXT
    prompt: str = ""
    max_runs: int = DEFAULT_MAX_RUNS


def format_interval(seconds: float) -> str:
    """Render an interval in seconds into a concise Chinese label."""
    for unit_seconds, label in ((86400.0, "天"), (3600.0, "小时"), (60.0, "分钟")):
        if seconds >= unit_seconds:
            quotient = seconds / unit_seconds
            if abs(quotient - round(quotient)) < 1e-9:
                return f"{int(round(quotient))} {label}"
            return f"{quotient:g} {label}"
    if abs(seconds - round(seconds)) < 1e-9:
        return f"{int(round(seconds))} 秒"
    return f"{seconds:g} 秒"


def parse_interval(token: str) -> tuple[float, str]:
    """Parse an interval token like '30s', '5m', '2h', '1d', or '5分钟'."""
    match = _INTERVAL_TOKEN_RE.match(token.strip())
    if match is None:
        raise ValueError(f"无法识别的时间间隔：{token!r}")
    return _convert_interval(match.group(1), match.group(2))


def _convert_interval(number_text: str, unit_text: str) -> tuple[float, str]:
    value = float(number_text)
    multiplier, label = _UNITS[unit_text.lower()]
    seconds = value * multiplier
    shown = f"{int(value)}" if value.is_integer() else f"{value:g}"
    return seconds, f"{shown} {label}"


def _parse_max_runs(raw_value: str | None, flag: str) -> int:
    if not raw_value:
        raise ValueError(f"--{flag} 需要指定 1 到 {MAX_RUNS_LIMIT} 之间的整数。")
    if not re.fullmatch(r"-?\d+", raw_value):
        raise ValueError(f"--{flag} 需要指定 1 到 {MAX_RUNS_LIMIT} 之间的整数。")
    count = int(raw_value)
    if count < 1 or count > MAX_RUNS_LIMIT:
        raise ValueError(f"最大运行次数必须在 1 到 {MAX_RUNS_LIMIT} 之间。")
    return count


def _extract_options(text: str, current: int | None) -> tuple[str, int | None]:
    """Strip leading and trailing --max-runs / --count flags while preserving prompt newlines."""
    rest = text.strip()
    max_runs = current
    while rest:
        match = _LEADING_OPT_RE.match(rest)
        if match is None:
            break
        flag = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        parsed = _parse_max_runs(value, flag)
        if max_runs is not None:
            raise ValueError("不要重复指定最大运行次数（--max-runs / --count）。")
        max_runs = parsed
        rest = rest[match.end() :].strip()

    while rest:
        dangling = _DANGLING_OPT_RE.search(rest)
        if dangling is not None:
            raise ValueError(f"--{dangling.group(1).lower()} 需要指定 1 到 {MAX_RUNS_LIMIT} 之间的整数。")
        match = _TRAILING_OPT_RE.search(rest)
        if match is None:
            break
        flag = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        parsed = _parse_max_runs(value, flag)
        if max_runs is not None:
            raise ValueError("不要重复指定最大运行次数（--max-runs / --count）。")
        max_runs = parsed
        rest = rest[: match.start()].strip()

    return rest, max_runs


def _validate_interval(seconds: float) -> None:
    if not math.isfinite(seconds) or seconds < MIN_INTERVAL:
        raise ValueError("轮询间隔至少为 10 秒。")
    if seconds > MAX_INTERVAL:
        raise ValueError("轮询间隔最多为 7 天。")


def parse_loop_args(raw: str) -> ParsedLoop:
    """Parse `/loop` command arguments into list, stop, or create actions."""
    text = raw.strip()
    if not text or text.lower() == "list":
        return ParsedLoop(action="list")

    parts = text.split(None, 1)
    head = parts[0].lower()
    if head in {"stop", "cancel"}:
        tail = parts[1].strip() if len(parts) > 1 else ""
        if not tail or len(tail.split()) > 1:
            raise ValueError("用法：/loop stop <任务id|all>")
        target = "all" if tail.lower() == "all" else tail
        return ParsedLoop(action="stop", target=target)

    rest, max_runs = _extract_options(text, None)
    interval = DEFAULT_INTERVAL
    interval_text = DEFAULT_INTERVAL_TEXT

    leading = _LEADING_INTERVAL_RE.match(rest)
    if leading is not None:
        interval, interval_text = _convert_interval(leading.group(1), leading.group(2))
        rest = rest[leading.end() :].strip()
    else:
        trailing = _TRAILING_EVERY_RE.search(rest)
        if trailing is not None:
            if trailing.group(3) is not None:
                interval, interval_text = _convert_interval("1", trailing.group(3))
            else:
                interval, interval_text = _convert_interval(trailing.group(1), trailing.group(2))
            rest = rest[: trailing.start()].strip()

    rest, max_runs = _extract_options(rest, max_runs)
    _validate_interval(interval)

    prompt = rest.strip()
    if not prompt:
        raise ValueError("定时任务的提示词不能为空。")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"定时任务的提示词最多 {MAX_PROMPT_CHARS} 字，当前 {len(prompt)} 字。")
    if _NESTED_LOOP_RE.match(prompt.lstrip()):
        raise ValueError("不能在 /loop 中嵌套调用 /loop。")

    return ParsedLoop(
        action="create",
        interval=interval,
        interval_text=interval_text,
        prompt=prompt,
        max_runs=max_runs if max_runs is not None else DEFAULT_MAX_RUNS,
    )


def format_loop_prompt(task: Task) -> str:
    """Wrap a non-slash loop prompt with task id, interval, and run counter."""
    info = task.loop
    if info is None:
        return task.prompt
    return f"[定时任务 {task.id} · 每 {info.interval_text} · 第 {info.runs} 次]\n{info.prompt}"


def _short_prompt(prompt: str, limit: int = 36) -> str:
    collapsed = " ".join(prompt.splitlines()[0].split()) if prompt.strip() else ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


class LoopScheduler:
    """Session-scoped scheduler that coalesces missed wakeups while busy and fires only on idle."""

    def __init__(
        self,
        tasks: TaskManager,
        *,
        is_busy: Callable[[], bool],
        trigger: Callable[[Task, str], Awaitable[None]],
        jitter_ratio: float = JITTER_RATIO,
        rng: Callable[[], float] | None = None,
        lifespan: float = MAX_LIFESPAN,
    ):
        self.tasks = tasks
        self.is_busy = is_busy
        self.trigger = trigger
        self.jitter_ratio = max(0.0, float(jitter_ratio))
        self.rng: Callable[[], float] = rng if rng is not None else random.random
        self.lifespan = float(lifespan)
        self._idle_event = asyncio.Event()
        self._fire_lock = asyncio.Lock()

    def next_delay(self, interval: float) -> float:
        """Compute non-negative jittered delay in [interval, interval + min(interval * ratio, MAX_JITTER)]."""
        if self.jitter_ratio <= 0:
            return interval
        spread = min(interval * self.jitter_ratio, MAX_JITTER)
        if spread <= 0:
            return interval
        sample = max(0.0, min(1.0, float(self.rng())))
        return interval + sample * spread

    def all_loops(self) -> list[Task]:
        return [task for task in self.tasks.tasks.values() if task.loop is not None]

    def active(self) -> list[Task]:
        return [task for task in self.tasks.tasks.values() if task.loop is not None and task.status == "running"]

    def schedule(
        self,
        interval: float,
        interval_text: str,
        prompt: str,
        *,
        max_runs: int = DEFAULT_MAX_RUNS,
    ) -> Task:
        if len(self.active()) >= MAX_ACTIVE_LOOPS:
            raise ValueError(f"活动定时任务已达上限（{MAX_ACTIVE_LOOPS} 个）；请先用 /loop stop 停止不再需要的任务。")
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("轮询间隔必须大于 0。")
        if max_runs < 1:
            raise ValueError("最大运行次数至少为 1。")
        cleaned = prompt.strip()
        if not cleaned:
            raise ValueError("定时任务的提示词不能为空。")
        if _NESTED_LOOP_RE.match(cleaned.lstrip()):
            raise ValueError("不能在 /loop 中嵌套调用 /loop。")

        label_interval = interval_text.strip() or format_interval(interval)
        now = time.monotonic()
        info = LoopInfo(
            interval=interval,
            interval_text=label_interval,
            prompt=cleaned,
            max_runs=max_runs,
            expires_at=now + self.lifespan,
            runs=0,
            next_fire=now + self.next_delay(interval),
            pending_fire=False,
        )
        task = self.tasks.new(
            origin="loop",
            agent="loop",
            agent_type="loop",
            description=f"每 {label_interval}：{_short_prompt(cleaned)}",
            prompt=cleaned,
            background=True,
            started=now,
            loop=info,
        )
        self.tasks.launch(task, lambda: self._run_loop(task))
        return task

    def notify_idle(self) -> None:
        """Signal that the session may now be idle, waking any pending_fire loops."""
        event = self._idle_event
        self._idle_event = asyncio.Event()
        event.set()

    async def stop(self, target: str) -> list[Task]:
        key = target.strip()
        if not key:
            return []
        if key.lower() == "all":
            stopped: list[Task] = []
            for task in list(self.active()):
                if await self.tasks.stop(task.id):
                    stopped.append(task)
            return stopped
        task = self.tasks.get(key)
        if task is None or task.loop is None:
            return []
        if await self.tasks.stop(task.id):
            return [task]
        return []

    async def close(self) -> None:
        """Cancel and await all running loop tasks cleanly."""
        await self.stop("all")

    def _complete(self, task: Task, reason: str) -> str:
        info = task.loop
        if info is not None:
            info.end_reason = reason
            info.next_fire = None
            info.pending_fire = False
        task.status = "completed"
        task.notified = True
        return f"已运行 {info.runs if info is not None else 0} 次（{reason}）"

    async def _run_loop(self, task: Task) -> str:
        info = task.loop
        assert info is not None
        try:
            while True:
                while info.next_fire is not None:
                    now = time.monotonic()
                    if now >= info.expires_at:
                        return self._complete(task, "已达 7 天有效期上限")
                    if now >= info.next_fire:
                        break
                    await asyncio.sleep(min(info.next_fire, info.expires_at) - now)

                while True:
                    if time.monotonic() >= info.expires_at:
                        return self._complete(task, "已达 7 天有效期上限")
                    wait_event = self._idle_event
                    if self.is_busy():
                        if not info.pending_fire or info.next_fire is not None:
                            info.pending_fire = True
                            info.next_fire = None
                            self.tasks.changed(task)
                        remaining = info.expires_at - time.monotonic()
                        if remaining <= 0:
                            return self._complete(task, "已达 7 天有效期上限")
                        try:
                            await asyncio.wait_for(wait_event.wait(), timeout=remaining)
                        except TimeoutError:
                            return self._complete(task, "已达 7 天有效期上限")
                        continue

                    async with self._fire_lock:
                        if time.monotonic() >= info.expires_at:
                            return self._complete(task, "已达 7 天有效期上限")
                        if self.is_busy():
                            continue
                        if task.stopped:
                            await asyncio.sleep(0)
                            raise asyncio.CancelledError
                        info.runs += 1
                        info.pending_fire = False
                        info.next_fire = None
                        self.tasks.changed(task)
                        await self.trigger(task, info.prompt)
                        break

                if task.stopped:
                    await asyncio.sleep(0)
                    raise asyncio.CancelledError
                if info.runs >= info.max_runs:
                    return self._complete(task, f"已达最大运行次数 {info.max_runs}")
                now = time.monotonic()
                if now >= info.expires_at:
                    return self._complete(task, "已达 7 天有效期上限")
                info.next_fire = now + self.next_delay(info.interval)
                self.tasks.changed(task)
        finally:
            info.next_fire = None
            info.pending_fire = False
