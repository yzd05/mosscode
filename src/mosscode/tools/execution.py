"""Re-export L1 tool execution primitives under mosscode.tools.execution for backwards compatibility."""

from ..tool_execution import (
    PARALLEL_AGENTS as PARALLEL_AGENTS,
)
from ..tool_execution import (
    _group as _group,
)
from ..tool_execution import (
    execute_batch as execute_batch,
)
from ..tool_execution import (
    partition_calls as partition_calls,
)

__all__ = [
    "PARALLEL_AGENTS",
    "execute_batch",
    "partition_calls",
]
