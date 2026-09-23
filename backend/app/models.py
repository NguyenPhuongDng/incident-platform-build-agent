"""SQLite tables (SQLModel)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, Text
from sqlmodel import JSON, Field, SQLModel


def _now() -> datetime:
    return datetime.utcnow()


class Agent(SQLModel, table=True):
    id: str = Field(primary_key=True)                     # slug
    domain_id: str = Field(index=True)
    display_name: str
    capability: str = Field(sa_column=Column(Text))       # mô tả năng lực
    business_prompt: str = Field(default="", sa_column=Column(Text))
    tools: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    knowledge_hint: str = Field(default="")
    status: str = Field(default="draft", index=True)      # draft | active | disabled
    is_core: bool = Field(default=False)
    version: int = Field(default=1)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgentVersion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    version: int
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class KnowledgeDoc(SQLModel, table=True):
    """A document in the domain's knowledge library.

    Library, not per-agent: an agent only *links* to a doc via AgentDoc. A doc with
    scope "domain" is visible to every agent of that domain; "restricted" is visible
    only to the agents that explicitly link it.
    """

    id: str = Field(primary_key=True)
    domain_id: str = Field(index=True)
    filename: str
    title: str = Field(default="")
    summary: str = Field(default="", sa_column=Column(Text))
    scope: str = Field(default="restricted", index=True)   # domain | restricted
    version: int = Field(default=1)
    num_chunks: int = 0
    status: str = Field(default="ready")                   # ready | error
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgentDoc(SQLModel, table=True):
    """Many-to-many link: which library documents an agent has attached."""

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    doc_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=_now)


class Ticket(SQLModel, table=True):
    id: str = Field(primary_key=True)
    domain_id: str = Field(index=True)
    resident_id: str = ""
    apartment_id: str = ""
    fields: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    summary: str = Field(default="", sa_column=Column(Text))
    priority: str = Field(default="BINH_THUONG")
    status: str = Field(default="dang_tiep_nhan", index=True)
    session_id: str = Field(default="", index=True)
    is_eval: bool = Field(default=False, index=True)      # sandbox ticket created by the Evaluator
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    ticket_id: Optional[str] = Field(default=None, index=True)
    role: str                                             # user | receptionist
    content: str = Field(sa_column=Column(Text))
    # Trạng thái tiếp nhận của Lễ tân tại lượt đó (các trường đã thu thập được). Lịch sử
    # hội thoại chỉ giữ câu chữ hiển thị, nên không có chỗ này thì mỗi lượt model phải
    # tự trích lại mọi trường từ văn bản thô — đã đo: nó rơi mất trường và hỏi lại vòng
    # vo. Cột JSON được `_patch_missing_columns()` thêm vào DB cũ nên không cần migrate tay.
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class RoomEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: str = Field(index=True)
    seq: int
    type: str
    actor: str = ""
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class Action(SQLModel, table=True):
    id: str = Field(primary_key=True)
    ticket_id: str = Field(index=True)
    agent_id: str = ""
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="cho_duyet", index=True)  # cho_duyet|da_duyet|tu_choi|da_thuc_hien|loi
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    decided_at: Optional[datetime] = None


class MockRecord(SQLModel, table=True):
    """Rows created by write-type mock tools (repair orders, cleaning requests...)."""

    id: str = Field(primary_key=True)
    kind: str = Field(index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- eval


class EvalCase(SQLModel, table=True):
    """One scripted scenario the Evaluator runs against a specialist agent."""

    id: str = Field(primary_key=True)
    agent_id: str = Field(index=True)
    ticket_text: str = Field(sa_column=Column(Text))
    resident_id: str = Field(default="")
    phan_hoi_bo_sung: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    expected_agents: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    forbidden_agents: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    expected_tools: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    forbidden_tools: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    expected_sources: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    rubric: str = Field(default="", sa_column=Column(Text))
    source: str = Field(default="generated")              # generated | manual
    approved: bool = Field(default=False)
    loai: str = Field(default="dung_nang_luc")             # dung_nang_luc | ngoai_nang_luc | lien_bo_phan
    created_at: datetime = Field(default_factory=_now)


class EvalRun(SQLModel, table=True):
    id: str = Field(primary_key=True)
    agent_id: str = Field(index=True)
    agent_version: int = 0
    trigger: str = Field(default="manual")                 # manual | pre_activate | builder_loop
    status: str = Field(default="running")                 # running | done | error
    started_at: datetime = Field(default_factory=_now)
    finished_at: Optional[datetime] = None
    passed: Optional[bool] = None
    summary: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    tokens: int = 0
    duration_ms: int = 0


class EvalResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    case_id: str = Field(index=True)
    ticket_id: str = Field(default="")
    checks: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    judge: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    passed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_now)
