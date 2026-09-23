"""HITL gate: the room thread parks on an Action until a manager decides.

Without this, `requires_approval` only meant "don't run it now" — the room carried
on, the agent wrote its conclusion around a tool result it never got, and the
manager's approval landed after the meeting was already over. With it, the turn
that called the tool blocks inside `ToolExecutor`, so the approval becomes part
of the conversation instead of an afterthought.

The room runs in a worker thread (`asyncio.to_thread`) while the approve/reject
endpoints run on the API event loop, so the handshake is a plain
`threading.Event` pair, not an asyncio primitive:

    room                                  manager (API)
    ----                                  -------------
    register()                            decide(action_id, "da_duyet")
    waiter.decided.wait(timeout)  <------ sets .decided
    run the tool, publish result -------> waiter.executed.wait(timeout)
    abandon()                             reads waiter.result

`abandon()` and `decide()` share one lock, so a decision arriving exactly as the
wait times out is never lost *and* never executed twice: whoever takes the lock
first owns the action, and a `decide()` that finds no waiter tells the API to run
the tool itself (the pre-HITL path, still used for actions nobody is waiting on).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("approval_gate")


@dataclass
class ApprovalWaiter:
    action_id: str
    ticket_id: str = ""
    tool: str = ""
    agent_id: str = ""
    decision: str = ""                       # "" | da_duyet | tu_choi
    result: dict[str, Any] = field(default_factory=dict)
    decided: threading.Event = field(default_factory=threading.Event)
    executed: threading.Event = field(default_factory=threading.Event)


class ApprovalGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, ApprovalWaiter] = {}

    # ---------------------------------------------------------------- room side
    def register(self, action_id: str, *, ticket_id: str = "", tool: str = "",
                 agent_id: str = "") -> ApprovalWaiter:
        waiter = ApprovalWaiter(action_id=action_id, ticket_id=ticket_id, tool=tool, agent_id=agent_id)
        with self._lock:
            self._waiters[action_id] = waiter
        return waiter

    def abandon(self, action_id: str) -> str:
        """Unregister and report the decision, if one arrived. Returns "" on timeout.

        Atomic with `decide()`: after this returns "", no decision can reach the
        room any more, so the API is free to execute the action itself.
        """
        with self._lock:
            waiter = self._waiters.pop(action_id, None)
        return waiter.decision if waiter else ""

    # ------------------------------------------------------------- manager side
    def decide(self, action_id: str, decision: str) -> ApprovalWaiter | None:
        """Hand a decision to a waiting room. None = nobody is waiting on it."""
        with self._lock:
            waiter = self._waiters.get(action_id)
            if waiter is None:
                return None
            waiter.decision = decision
        waiter.decided.set()
        logger.info("chuyển quyết định '%s' cho phòng họp đang chờ action=%s", decision, action_id)
        return waiter

    def is_waiting(self, action_id: str) -> bool:
        with self._lock:
            return action_id in self._waiters

    def waiting_ids(self) -> set[str]:
        with self._lock:
            return set(self._waiters)

    def waiting_for_ticket(self, ticket_id: str) -> list[ApprovalWaiter]:
        with self._lock:
            return [w for w in self._waiters.values() if w.ticket_id == ticket_id]


gate = ApprovalGate()
