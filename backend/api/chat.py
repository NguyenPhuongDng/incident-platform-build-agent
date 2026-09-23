"""Resident-facing chat: the receptionist, and the room it kicks off."""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlmodel import select

from backend.app.db import session_scope
from backend.app.models import ChatMessage, Ticket
from backend.core.receptionist import Receptionist
from backend.core.room import run_room_dispatch
from backend.domain.loader import load_domain, load_mock

logger = logging.getLogger("api.chat")
router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1)
    resident_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


@router.get("/residents")
def list_residents() -> list[dict[str, Any]]:
    """Mock people the demo can speak as; the apartment comes with them."""
    return load_mock("cu_dan.json")


@router.get("/domain")
def get_domain() -> dict[str, Any]:
    d = load_domain()
    return {
        "id": d.id,
        "display_name": d.display_name,
        "audience": d.audience,
        "honorific": d.honorific,
        "self_reference": d.self_reference,
        "intake_fields": [f.model_dump() for f in d.intake_fields],
        "max_room_turns": d.max_room_turns,
    }


@router.get("/chat/{session_id}")
def history(session_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.exec(
            select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
        ).all()
        return [
            {"role": m.role, "content": m.content, "ticket_id": m.ticket_id,
             "created_at": m.created_at.isoformat()}
            for m in rows
        ]


def _awaiting_ticket(session_id: str) -> str | None:
    """A ticket in this session that is waiting for the reporter to answer."""
    with session_scope() as s:
        row = s.exec(
            select(Ticket)
            .where(Ticket.session_id == session_id, Ticket.status == "cho_cu_dan")
            .order_by(Ticket.created_at.desc())
        ).first()
        return row.id if row else None


async def _run_room_background(ticket_id: str) -> None:
    """The room is synchronous; keep the request loop free."""
    try:
        await asyncio.to_thread(run_room_dispatch, ticket_id)
    except Exception:  # noqa: BLE001
        logger.exception("Phòng họp nền lỗi cho ticket=%s", ticket_id)


@router.post("/chat")
async def chat(payload: ChatIn) -> dict[str, Any]:
    receptionist = Receptionist()

    # Follow-up to a question the room asked earlier.
    awaiting = _awaiting_ticket(payload.session_id)
    if awaiting:
        ticket = receptionist.add_followup(awaiting, payload.message)
        asyncio.create_task(_run_room_background(awaiting))
        return {
            "tra_loi": "Ban quản lý đã ghi nhận thông tin bổ sung, đang chuyển lại bộ phận liên quan.",
            "du_thong_tin": True,
            "ticket": ticket,
            "phong_hop_dang_chay": True,
        }

    resident = next(
        (r for r in load_mock("cu_dan.json") if r["ma_cu_dan"] == payload.resident_id), None
    )
    subject_id = resident["ma_can_ho"] if resident else ""
    # Lễ tân phải biết người báo là ai, nếu không nó đi hỏi lại đúng những thứ hệ thống
    # đã có (đã gặp: hỏi "căn hộ nào", "vị trí ở đâu"). Chỉ đưa những trường cần cho
    # việc tiếp nhận — email/ngày vào ở không giúp gì nên không gửi sang LLM.
    profile = {k: resident[k] for k in ("ho_ten", "ma_can_ho", "dien_thoai", "vai_tro")
               if resident and resident.get(k)} if resident else None

    try:
        result = await asyncio.to_thread(
            functools.partial(
                receptionist.intake,
                payload.session_id, payload.resident_id, subject_id, payload.message,
                requester_profile=profile,
            )
        )
    except Exception:  # noqa: BLE001 - a demo must never show a raw 500 to the reporter
        logger.exception("Lễ tân lỗi khi tiếp nhận session=%s", payload.session_id)
        domain = load_domain()
        return {
            "tra_loi": f"Hệ thống đang gặp trục trặc, mong {domain.honorific} thử lại sau giây lát.",
            "du_thong_tin": False,
            "ticket": None,
            "loi_he_thong": True,
        }

    if result.get("ticket"):
        asyncio.create_task(_run_room_background(result["ticket"]["id"]))
        result["phong_hop_dang_chay"] = True
    return result
