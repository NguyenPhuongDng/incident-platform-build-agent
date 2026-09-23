"""Agent builder API — the same endpoints the UI uses and the seed script uses."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from backend.agents import evaluator, registry
from backend.agents.registry import AgentCreate, AgentError, AgentUpdate

logger = logging.getLogger("api.agents")

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AgentError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, str(exc.errors()[:3])) from exc


@router.get("")
def list_agents(status: str | None = None) -> list[dict[str, Any]]:
    return registry.list_agents(status=status)


@router.post("", status_code=201)
def create_agent(payload: AgentCreate) -> dict[str, Any]:
    return _guard(registry.create_agent, payload)


@router.get("/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    agent = registry.get_agent(agent_id, with_docs=True)
    if agent is None:
        raise HTTPException(404, "Không tìm thấy agent")
    return agent


@router.put("/{agent_id}")
def update_agent(agent_id: str, payload: AgentUpdate) -> dict[str, Any]:
    return _guard(registry.update_agent, agent_id, payload)


@router.delete("/{agent_id}", status_code=204)
def delete_agent(agent_id: str) -> None:
    _guard(registry.delete_agent, agent_id)


@router.post("/{agent_id}/activate")
def activate(agent_id: str, force: bool = False) -> dict[str, Any]:
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, "Không tìm thấy agent")

    if force:
        logger.warning("Bật agent '%s' bằng force=True, bỏ qua chốt chặn đánh giá", agent_id)
        result = _guard(registry.set_status, agent_id, "active")
        return {**result, "forced": True}

    run = evaluator.latest_passing_run(agent_id, agent["version"])
    if run is None:
        raise HTTPException(
            409,
            {
                "message": f"Agent chưa có lần đánh giá nào đạt trên phiên bản hiện tại (v{agent['version']}). "
                           "Chạy đánh giá và đạt trước khi bật, hoặc dùng force=true.",
                "agent_version": agent["version"],
            },
        )
    reg = evaluator.regression(candidate_agent_id=agent_id, domain_id=agent["domain_id"])
    if not reg["dat"]:
        raise HTTPException(
            409,
            {
                "message": "Agent trượt kiểm tra hồi quy định tuyến — có thể làm giảm độ chính xác của "
                           "agent khác hoặc cướp ticket. Xem chi tiết, hoặc dùng force=true.",
                "regression": reg,
            },
        )
    result = _guard(registry.set_status, agent_id, "active")
    return {**result, "eval_run_id": run["id"], "regression": reg}


@router.post("/{agent_id}/disable")
def disable(agent_id: str) -> dict[str, Any]:
    return _guard(registry.set_status, agent_id, "disabled")


@router.get("/{agent_id}/versions")
def versions(agent_id: str) -> list[dict[str, Any]]:
    return registry.list_versions(agent_id)


@router.post("/{agent_id}/rollback/{version}")
def rollback(agent_id: str, version: int) -> dict[str, Any]:
    return _guard(registry.rollback, agent_id, version)
