"""Ticket listing and the live room event stream."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import select

from backend.app.db import session_scope
from backend.app.events import bus, load_events, sse_format
from backend.app.models import ChatMessage, Ticket
from backend.core import workflow
from backend.core.receptionist import ticket_to_dict

router = APIRouter(prefix="/api", tags=["tickets"])


@router.get("/tickets")
def list_tickets(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with session_scope() as s:
        stmt = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Ticket.status == status)
        return [
            {
                "id": t.id,
                "resident_id": t.resident_id,
                "apartment_id": t.apartment_id,
                "summary": t.summary,
                "priority": t.priority,
                "status": t.status,
                "fields": dict(t.fields or {}),
                "created_at": t.created_at.isoformat(),
            }
            for t in s.exec(stmt).all()
        ]


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict[str, Any]:
    ticket = ticket_to_dict(ticket_id)
    if not ticket:
        raise HTTPException(404, "Không tìm thấy phản ánh")
    ticket["events"] = load_events(ticket_id)
    with session_scope() as s:
        msgs = s.exec(
            select(ChatMessage).where(ChatMessage.ticket_id == ticket_id).order_by(ChatMessage.id)
        ).all()
        ticket["messages"] = [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in msgs
        ]
    return ticket


@router.get("/tickets/{ticket_id}/workflow")
def get_workflow(ticket_id: str) -> dict[str, Any]:
    """Quy trình xác nhận của phản ánh: bước nào xong, bước nào đang chờ ai."""
    state = workflow.chain_state(ticket_id)
    return {
        "ticket_id": ticket_id,
        "da_bat_dau": workflow.started(state),
        "con_thieu": [s["id"] for s in workflow.unfinished(state)] if workflow.started(state) else [],
        "cac_buoc": state,
    }


@router.get("/tickets/{ticket_id}/events")
async def stream_events(ticket_id: str) -> StreamingResponse:
    """Replay everything recorded so far, then follow live."""
    queue = bus.subscribe(ticket_id)

    async def generator():
        try:
            for record in load_events(ticket_id):
                yield sse_format(record)
            yield ": bat dau theo doi truc tiep\n\n"
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=15)
                    yield sse_format(record)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:  # client disconnected
            raise
        finally:
            bus.unsubscribe(ticket_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
