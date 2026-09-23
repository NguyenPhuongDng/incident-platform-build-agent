"""Agent registry: agents are data rows, never code.

Every specialist agent — including the seeded ones — is created through this
registry via the same API the builder UI uses.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlmodel import select

from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.models import Agent, AgentVersion
from backend.tools.catalog import get_catalog

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


class AgentError(ValueError):
    pass


class AgentCreate(BaseModel):
    id: str
    display_name: str
    capability: str
    business_prompt: str = ""
    tools: list[str] = Field(default_factory=list)
    status: str = "draft"
    domain_id: str | None = None

    @field_validator("id")
    @classmethod
    def check_id(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("id phải là slug chữ thường, chỉ gồm a-z 0-9 _ - và dài 2-41 ký tự")
        return v

    @field_validator("capability")
    @classmethod
    def check_capability(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError("Mô tả năng lực phải dài ít nhất 20 ký tự để Điều phối định tuyến được")
        return v.strip()

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in {"draft", "active", "disabled"}:
            raise ValueError("status phải là draft, active hoặc disabled")
        return v


class AgentUpdate(BaseModel):
    display_name: str | None = None
    capability: str | None = None
    business_prompt: str | None = None
    tools: list[str] | None = None
    status: str | None = None

    @field_validator("capability")
    @classmethod
    def check_capability(cls, v: str | None) -> str | None:
        if v is not None and len(v.strip()) < 20:
            raise ValueError("Mô tả năng lực phải dài ít nhất 20 ký tự")
        return v.strip() if v else v


def _validate_tools(tools: list[str]) -> list[str]:
    catalog = get_catalog()
    unknown = [t for t in tools if catalog.get(t) is None]
    if unknown:
        raise AgentError(f"Tool không có trong catalog: {', '.join(unknown)}")
    if len(tools) > settings.max_agent_tools:
        raise AgentError(f"Mỗi agent chỉ được gắn tối đa {settings.max_agent_tools} tool")
    return list(dict.fromkeys(tools))


def to_dict(a: Agent, *, with_docs: bool = False) -> dict[str, Any]:
    data = {
        "id": a.id,
        "domain_id": a.domain_id,
        "display_name": a.display_name,
        "capability": a.capability,
        "business_prompt": a.business_prompt,
        "tools": list(a.tools or []),
        "status": a.status,
        "is_core": a.is_core,
        "version": a.version,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }
    if with_docs:
        from backend.knowledge import library

        data["knowledge"] = library.list_agent_docs(a.id)
    return data


def _snapshot(session, agent: Agent) -> None:
    session.add(
        AgentVersion(agent_id=agent.id, version=agent.version, snapshot=to_dict(agent))
    )


# ----------------------------------------------------------------------- queries
def list_agents(domain_id: str | None = None, status: str | None = None) -> list[dict]:
    with session_scope() as s:
        stmt = select(Agent).where(Agent.domain_id == (domain_id or settings.domain_id))
        if status:
            stmt = stmt.where(Agent.status == status)
        return [to_dict(a) for a in s.exec(stmt).all()]


def get_agent(agent_id: str, *, with_docs: bool = False) -> dict | None:
    with session_scope() as s:
        a = s.get(Agent, agent_id)
        return to_dict(a, with_docs=with_docs) if a else None


def active_specialists(domain_id: str | None = None) -> list[dict]:
    """The dynamic registry the orchestrator routes over."""
    return [
        a
        for a in list_agents(domain_id, status="active")
        if not a["is_core"]
    ]


# ----------------------------------------------------------------------- mutations
def create_agent(payload: AgentCreate, *, is_core: bool = False) -> dict:
    tools = _validate_tools(payload.tools)
    with session_scope() as s:
        if s.get(Agent, payload.id):
            raise AgentError(f"Agent id '{payload.id}' đã tồn tại")
        agent = Agent(
            id=payload.id,
            domain_id=payload.domain_id or settings.domain_id,
            display_name=payload.display_name.strip(),
            capability=payload.capability,
            business_prompt=payload.business_prompt,
            tools=tools,
            status=payload.status,
            is_core=is_core,
            version=1,
        )
        s.add(agent)
        s.flush()
        _snapshot(s, agent)
        return to_dict(agent)


def update_agent(agent_id: str, payload: AgentUpdate) -> dict:
    with session_scope() as s:
        agent = s.get(Agent, agent_id)
        if agent is None:
            raise AgentError("Không tìm thấy agent")
        if agent.is_core:
            raise AgentError("Agent lõi (Lễ tân, Điều phối) không sửa được qua builder")
        _snapshot(s, agent)  # snapshot the state BEFORE the edit
        if payload.display_name is not None:
            agent.display_name = payload.display_name.strip()
        if payload.capability is not None:
            agent.capability = payload.capability
        if payload.business_prompt is not None:
            agent.business_prompt = payload.business_prompt
        if payload.tools is not None:
            agent.tools = _validate_tools(payload.tools)
        if payload.status is not None:
            if payload.status not in {"draft", "active", "disabled"}:
                raise AgentError("status không hợp lệ")
            agent.status = payload.status
        agent.version += 1
        agent.updated_at = datetime.utcnow()
        s.add(agent)
        s.flush()
        return to_dict(agent)


def set_status(agent_id: str, status: str) -> dict:
    return update_agent(agent_id, AgentUpdate(status=status))


def delete_agent(agent_id: str) -> None:
    from backend.knowledge import library

    with session_scope() as s:
        agent = s.get(Agent, agent_id)
        if agent is None:
            raise AgentError("Không tìm thấy agent")
        if agent.is_core:
            raise AgentError("Không xóa được agent lõi")
        s.delete(agent)
    # Only the links are removed — library documents may still be used by other agents.
    try:
        library.unlink_all_for_agent(agent_id)
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


def list_versions(agent_id: str) -> list[dict]:
    with session_scope() as s:
        rows = s.exec(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version.desc())
        ).all()
        return [
            {"version": r.version, "created_at": r.created_at.isoformat(), "snapshot": r.snapshot}
            for r in rows
        ]


def rollback(agent_id: str, version: int) -> dict:
    with session_scope() as s:
        row = s.exec(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version == version)
        ).first()
        if row is None:
            raise AgentError(f"Không có phiên bản {version}")
        snap = row.snapshot
    return update_agent(
        agent_id,
        AgentUpdate(
            display_name=snap.get("display_name"),
            capability=snap.get("capability"),
            business_prompt=snap.get("business_prompt", ""),
            tools=list(snap.get("tools") or []),
            status=snap.get("status"),
        ),
    )


# ----------------------------------------------------------------------- knowledge
def list_docs(agent_id: str) -> list[dict]:
    """Kept for backward compatibility; delegates to the library module."""
    from backend.knowledge import library

    return library.list_agent_docs(agent_id)
