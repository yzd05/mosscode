"""Parallelize adjacent reads, preserve ordering across side-effect barriers."""

import asyncio
from copy import deepcopy
from time import monotonic

from ..messages import Message, ResponseBoundary, ToolProgress, ToolResult
from ..tool_budget import bound_message
from .base import ExecutionCancelled, ToolRegistry


PARALLEL_AGENTS = "agent"  # Ch13 G6: adjacent agent calls run together like adjacent reads.


def _group(call, registry: ToolRegistry) -> str | None:
    if call.name == PARALLEL_AGENTS and registry.get(call.name) is not None:
        return "agent"
    return "read" if registry.is_read_only(call.name) else None


def partition_calls(calls, registry: ToolRegistry, concurrency: int = 4):
    batch, kind = [], None
    for index, call in enumerate(calls):
        group = _group(call, registry)
        if batch and (group != kind or len(batch) == concurrency):
            yield batch
            batch = []
        if group is None:
            yield [index]
            continue
        batch.append(index)
        kind = group
    if batch:
        yield batch


async def execute_batch(message: Message, registry: ToolRegistry, history: list[Message], *, read_only: bool = False):
    calls = message.tool_calls
    results = [None] * len(calls)
    jobs: dict[int, asyncio.Task] = {}
    started = {}
    approval_job = None
    position = len(history)
    history.append(Message("tool", ()))

    def save():
        history[position] = Message("tool", (), tool_results=tuple(deepcopy(r) for r in results if r is not None))

    try:
        yield ResponseBoundary(message)
        for batch in partition_calls(calls, registry):
            for index in batch:
                yield ToolProgress(calls[index], "running")
            for index in batch:
                started[index] = monotonic()
                jobs[index] = asyncio.create_task(registry.execute(calls[index], read_only=read_only))
            while jobs:
                if approval_job is None:
                    approval_job = asyncio.create_task(registry.permissions.requests.get())
                done, _ = await asyncio.wait([*jobs.values(), approval_job], return_when=asyncio.FIRST_COMPLETED)
                if approval_job in done:
                    request = approval_job.result()
                    approval_job = None
                    if request.id in registry.permissions.pending:
                        yield request
                for index in sorted(i for i, task in jobs.items() if task in done):
                    result = await jobs[index]
                    del jobs[index]
                    results[index] = result
                    save()
                    yield ToolProgress(calls[index], "finished", result, monotonic() - started[index])
    finally:
        if approval_job is not None:
            approval_job.cancel()
            try:
                await approval_job
            except asyncio.CancelledError:
                pass
        for task in jobs.values():
            if not task.done():
                task.cancel()
        # Await individually to preserve ExecutionCancelled.result.
        for index, task in jobs.items():
            try:
                results[index] = await task
            except ExecutionCancelled as exc:
                results[index] = exc.result
            except asyncio.CancelledError:
                results[index] = ToolResult(calls[index].id, calls[index].name, "调用已取消。", True, "cancelled")
            except Exception:
                results[index] = ToolResult(calls[index].id, calls[index].name, "工具执行出现内部错误。", True, "internal_error")
        for index, result in enumerate(results):
            if result is None:
                results[index] = ToolResult(calls[index].id, calls[index].name, "本轮已停止；此调用未执行。", True, "not_executed")
        save()
        history[position] = bound_message(history[position], registry.session)
