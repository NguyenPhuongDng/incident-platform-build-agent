"""Load the domain pack (domain.yaml + mock data) for the active domain.

The platform core reads every domain-specific string from here; nothing about a
particular customer is hard-coded in core code or core prompts.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from backend.app.config import settings


class IntakeField(BaseModel):
    key: str
    label: str
    required: bool = False


class ApprovalRole(BaseModel):
    """Một hàng đợi duyệt. `hien_o` chỉ là gợi ý cho giao diện, không phải phân quyền."""

    id: str
    label: str
    mo_ta: str = ""
    hien_o: str = "console"          # console | resident


class ConfirmStep(BaseModel):
    id: str
    label: str
    vai_tro: str
    tools: list[str] = Field(default_factory=list)


class DomainPack(BaseModel):
    id: str
    display_name: str
    audience: str
    honorific: str
    self_reference: str
    intake_fields: list[IntakeField] = Field(default_factory=list)
    priority_rules: str = ""
    max_room_turns: int = 8
    approval_roles: list[ApprovalRole] = Field(default_factory=list)
    quy_trinh_xac_nhan: list[ConfirmStep] = Field(default_factory=list)

    def intake_spec(self) -> str:
        lines = []
        for f in self.intake_fields:
            mark = "bắt buộc" if f.required else "không bắt buộc"
            lines.append(f"- {f.key}: {f.label} ({mark})")
        return "\n".join(lines)

    def required_keys(self) -> list[str]:
        return [f.key for f in self.intake_fields if f.required]

    def role(self, role_id: str) -> ApprovalRole | None:
        return next((r for r in self.approval_roles if r.id == role_id), None)

    def role_label(self, role_id: str) -> str:
        r = self.role(role_id)
        return r.label if r else role_id

    def step_for_tool(self, tool_name: str) -> ConfirmStep | None:
        return next((s for s in self.quy_trinh_xac_nhan if tool_name in s.tools), None)


@lru_cache(maxsize=8)
def load_domain(domain_id: str | None = None) -> DomainPack:
    did = domain_id or settings.domain_id
    path = settings.domains_dir / did / "domain.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy domain pack: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return DomainPack(**data)


def domain_path(*parts: str, domain_id: str | None = None) -> Path:
    return settings.domains_dir.joinpath(domain_id or settings.domain_id, *parts)


@lru_cache(maxsize=32)
def load_mock(name: str, domain_id: str | None = None) -> Any:
    """Read a JSON file from the domain's mock_data folder."""
    path = domain_path("mock_data", name, domain_id=domain_id)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
