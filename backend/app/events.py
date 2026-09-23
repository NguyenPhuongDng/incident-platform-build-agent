"""Room event bus: persist to SQLite and fan out to SSE subscribers."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlmodel import select

from backend.app.db import session_scope
from backend.app.models import RoomEvent

logger = logging.getLogger("events")

EVENT_TYPES = {
    "room_start",
    "router_decision",
    "agent_start",
    "rag_hits",
    "tool_call",
    "tool_result",
    "action_pending",
    "room_waiting",
    "room_resumed",
    "agent_output",
    "guard_triggered",
    "room_end",
    "receptionist_reply",
    "receptionist_quick_answer",
    "action_executed",
}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, ticket_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(ticket_id, []).append(q)
        return q

    def unsubscribe(self, ticket_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(ticket_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            self._subscribers.pop(ticket_id, None)

    def emit(self, ticket_id: str, type_: str, payload: dict[str, Any], actor: str = "") -> dict:
        """Persist an event and push it to live subscribers. Safe to call from any thread."""
        with session_scope() as s:
            seq = len(s.exec(select(RoomEvent).where(RoomEvent.ticket_id == ticket_id)).all()) + 1
            ev = RoomEvent(ticket_id=ticket_id, seq=seq, type=type_, actor=actor, payload=payload)
            s.add(ev)
            s.flush()
            record = {
                "id": ev.id,
                "ticket_id": ticket_id,
                "seq": seq,
                "type": type_,
                "actor": actor,
                "payload": payload,
                "created_at": ev.created_at.isoformat(),
            }
        logger.info("event ticket=%s seq=%d type=%s actor=%s", ticket_id, seq, type_, actor)
        self._push(ticket_id, record)
        return record

    def _push(self, ticket_id: str, record: dict) -> None:
        for q in list(self._subscribers.get(ticket_id, [])):
            try:
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(q.put_nowait, record)
                else:
                    q.put_nowait(record)
            except Exception:  # noqa: BLE001 - a slow subscriber must not break the room
                logger.debug("bỏ qua subscriber đầy hàng đợi")


bus = EventBus()


def load_events(ticket_id: str) -> list[dict]:
    with session_scope() as s:
        rows = s.exec(
            select(RoomEvent).where(RoomEvent.ticket_id == ticket_id).order_by(RoomEvent.seq)
        ).all()
        return [
            {
                "id": r.id,
                "ticket_id": r.ticket_id,
                "seq": r.seq,
                "type": r.type,
                "actor": r.actor,
                "payload": r.payload,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


def sse_format(record: dict) -> str:
    return f"event: {record['type']}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
