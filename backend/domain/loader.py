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


class DomainPack(BaseModel):
    id: str
    display_name: str
    audience: str
    honorific: str
    self_reference: str
    intake_fields: list[IntakeField] = Field(default_factory=list)
    priority_rules: str = ""
    max_room_turns: int = 8

    def intake_spec(self) -> str:
        lines = []
        for f in self.intake_fields:
            mark = "bắt buộc" if f.required else "không bắt buộc"
            lines.append(f"- {f.key}: {f.label} ({mark})")
        return "\n".join(lines)

    def required_keys(self) -> list[str]:
        return [f.key for f in self.intake_fields if f.required]


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
