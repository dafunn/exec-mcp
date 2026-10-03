"""Header prefixes `run_task` emits. The driver keeps its own copy, since the
two sides share no runtime; `wire_contract.toml` beside this file declares
them and both sides assert against it. Change a prefix here and the contract
must change with it.
"""
from __future__ import annotations

TASK_DONE_PREFIX = "[task done]"
TASK_TIMEOUT_PREFIX = "[task timeout]"
TASK_KILLED_PREFIX = "[task killed]"
