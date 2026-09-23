"""Manager approval queue.

Approving is the only path that lets a `requires_approval` tool actually run.

Hai đường chạy, tùy có phòng họp nào đang ngồi chờ hành động này hay không
(`approval_gate`):

  * đang chờ  — trả quyết định cho phòng họp, chính nó chạy tool rồi họp tiếp với
    kết quả thật trong tay. Không gửi tin riêng cho người báo: cuối phiên Lễ tân
    tổng hợp một thể.
  * không ai chờ (HITL tắt, hết hạn chờ, hoặc hành động còn lại từ phiên trước) —
    endpoint tự chạy tool rồi báo người báo, đúng như hành vi trước khi có HITL.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.models import Action
from backend.tools.approval_gate import gate
from backend.core.receptionist import Receptionist, ticket_to_dict
from backend.tools.executor import get_executor

logger = logging.getLogger("api.actions")
router = APIRouter(prefix="/api/actions", tags=["actions"])


def _to_dict(a: Action) -> dict[str, Any]:
    return {
        "id": a.id,
        "ticket_id": a.ticket_id,
        "agent_id": a.agent_id,
        "tool": a.tool,
        "args": dict(a.args or {}),
        "status": a.status,
        "result": dict(a.result or {}),
        "created_at": a.created_at.isoformat(),
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        # Giao diện cần phân biệt: hành động này đang giữ cả một phòng họp đứng im.
        "phong_hop_dang_cho": gate.is_waiting(a.id),
    }


@router.get("")
def list_actions(status: str | None = "cho_duyet") -> list[dict[str, Any]]:
    with session_scope() as s:
        stmt = select(Action).order_by(Action.created_at.desc())
        if status:
            stmt = stmt.where(Action.status == status)
        return [_to_dict(a) for a in s.exec(stmt).all()]


@router.post("/{action_id}/approve")
async def approve(action_id: str) -> dict[str, Any]:
    with session_scope() as s:
        action = s.get(Action, action_id)
        if action is None:
            raise HTTPException(404, "Không tìm thấy hành động")
        if action.status != "cho_duyet":
            raise HTTPException(400, f"Hành động đang ở trạng thái '{action.status}'")
        action.status = "da_duyet"
        action.decided_at = datetime.utcnow()
        s.add(action)
        ticket_id = action.ticket_id

    waiter = gate.decide(action_id, "da_duyet")
    if waiter is not None:
        # Phòng họp đang dừng ở đúng lượt gọi tool này: để nó chạy tool, rồi họp tiếp.
        await asyncio.to_thread(waiter.executed.wait, settings.hitl_execute_timeout)
        with session_scope() as s:
            payload = _to_dict(s.get(Action, action_id))
        return {
            "action": payload,
            "ket_qua": waiter.result or payload["result"],
            "phong_hop_tiep_tuc": True,
        }

    result = await asyncio.to_thread(get_executor().execute_approved, action_id)

    with session_scope() as s:
        payload = _to_dict(s.get(Action, action_id))

    # Tell the reporter, without leaking internal names.
    try:
        ticket = ticket_to_dict(ticket_id)
        if ticket:
            await asyncio.to_thread(Receptionist().notify_action_result, ticket, payload)
    except Exception:  # noqa: BLE001 - the action already ran; a failed notice must not 500
        logger.exception("Không gửi được tin cập nhật cho ticket=%s", ticket_id)

    return {"action": payload, "ket_qua": result, "phong_hop_tiep_tuc": False}


@router.post("/{action_id}/reject")
def reject(action_id: str) -> dict[str, Any]:
    with session_scope() as s:
        action = s.get(Action, action_id)
        if action is None:
            raise HTTPException(404, "Không tìm thấy hành động")
        if action.status != "cho_duyet":
            raise HTTPException(400, f"Hành động đang ở trạng thái '{action.status}'")
        action.status = "tu_choi"
        action.decided_at = datetime.utcnow()
        s.add(action)
        s.flush()
        payload = _to_dict(action)

    # Phòng họp (nếu đang chờ) nhận lời từ chối và họp tiếp — agent phải tự tìm
    # phương án khác thay vì coi như việc đã xong.
    resumed = gate.decide(action_id, "tu_choi") is not None
    payload["phong_hop_tiep_tuc"] = resumed
    return payload
