"""Sandbox: try one agent, or ask the router where a ticket would go.

Used by the builder before an agent is switched on. Approval-gated tools do not
create real pending actions here.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.agents import registry
from backend.agents.runner import AgentRunner
from backend.core.orchestrator import Orchestrator
from backend.domain.loader import load_mock

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


class SandboxAgentIn(BaseModel):
    ticket_text: str = Field(min_length=1)
    instruction: str = ""
    resident_id: str = ""


class SandboxRouterIn(BaseModel):
    ticket_text: str = Field(min_length=1)
    include_draft_id: str | None = None


def _fake_ticket(text: str, resident_id: str) -> dict[str, Any]:
    resident = next((r for r in load_mock("cu_dan.json") if r["ma_cu_dan"] == resident_id), None)
    if resident is None:
        residents = load_mock("cu_dan.json")
        resident = residents[0] if residents else {"ma_cu_dan": "", "ma_can_ho": ""}
    return {
        "id": f"SANDBOX-{uuid.uuid4().hex[:6].upper()}",
        "domain_id": "",
        "resident_id": resident["ma_cu_dan"],
        "apartment_id": resident["ma_can_ho"],
        "fields": {"mo_ta": text},
        "summary": text,
        "priority": "BINH_THUONG",
        "status": "sandbox",
        "session_id": "",
    }


@router.post("/agent/{agent_id}")
async def sandbox_agent(agent_id: str, payload: SandboxAgentIn) -> dict[str, Any]:
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, "Không tìm thấy agent")

    ticket = _fake_ticket(payload.ticket_text, payload.resident_id)
    result = await asyncio.to_thread(
        lambda: AgentRunner().run(
            agent,
            ticket=ticket,
            instruction=payload.instruction or "Hãy xử lý phản ánh này theo năng lực của bạn.",
            transcript=[],
            sandbox=True,
        )
    )
    return {"ticket_gia": ticket, **result.to_dict()}


@router.post("/router")
async def sandbox_router(payload: SandboxRouterIn) -> dict[str, Any]:
    members = registry.active_specialists()
    if payload.include_draft_id:
        draft = registry.get_agent(payload.include_draft_id)
        if draft is None:
            raise HTTPException(404, "Không tìm thấy agent nháp")
        if all(m["id"] != draft["id"] for m in members):
            members = members + [draft]

    if not members:
        raise HTTPException(400, "Chưa có thành viên nào để định tuyến")

    ticket = _fake_ticket(payload.ticket_text, "")
    decision = await asyncio.to_thread(
        lambda: Orchestrator().decide(
            ticket=ticket,
            members=members,
            transcript=[],
            pending_suggestions=[],
            pending_actions=[],
            turns_used=0,
            max_turns=1,
        )
    )
    return {
        "quyet_dinh": decision.to_dict(),
        "thanh_vien_xet_den": [
            {"id": m["id"], "display_name": m["display_name"], "capability": m["capability"]}
            for m in members
        ],
    }
