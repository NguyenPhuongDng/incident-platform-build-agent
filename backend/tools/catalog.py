"""Tool catalog: YAML metadata + schemas from the implementations.

Schemas are never written by hand. Local tools expose a Pydantic model, MCP tools
expose `list_tools`. Before a schema reaches the LLM, every `context_params` entry
is stripped from `properties` and `required` so the model cannot choose them.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

from backend.app.config import settings
from backend.tools.local_tools import get_local_tool
from backend.tools.mcp_client import mcp_client

logger = logging.getLogger("catalog")

CATALOG_PATH = settings.base_dir / "backend" / "tools" / "catalog.yaml"


@dataclass
class ToolSpec:
    name: str
    provider: str                      # "local" | "mcp:<server>"
    scope: str                         # platform | domain | business
    requires_approval: bool = False
    # Ai phải duyệt tool này. Chuỗi tự do do domain pack định nghĩa (xem
    # `approval_roles` trong domain.yaml) — lõi không biết "cư dân" hay "BQL" là gì,
    # chỉ biết định tuyến hành động chờ duyệt tới đúng hàng đợi mang tên đó.
    approval_role: str = ""
    context_params: list[str] = field(default_factory=list)
    manager_description: str = ""

    @property
    def mcp_server(self) -> str | None:
        return self.provider.split(":", 1)[1] if self.provider.startswith("mcp:") else None

    def raw_schema(self) -> dict[str, Any]:
        """Full schema including context params (used by the executor for validation)."""
        if self.mcp_server:
            return copy.deepcopy(mcp_client.schema_for(self.name) or {"type": "object", "properties": {}})
        entry = get_local_tool(self.name)
        if not entry:
            return {"type": "object", "properties": {}}
        schema = entry[0].model_json_schema()
        return _flatten_refs(schema)

    def llm_schema(self) -> dict[str, Any]:
        """Schema handed to the LLM: context params removed entirely.

        "sandbox" is a second, always-hidden pseudo context-param: any write tool
        that declares it (to tag its output with an EVAL- code during a sandbox or
        Evaluator run) never lets the LLM see or set it either — same mechanism as
        `context_params`, just applied uniformly instead of per-tool in the YAML.
        """
        schema = self.raw_schema()
        hidden = set(self.context_params) | {"sandbox"}
        props = schema.get("properties", {})
        for p in hidden:
            props.pop(p, None)
        schema["properties"] = props
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in hidden]
        schema.pop("title", None)
        return schema

    def is_available(self) -> bool:
        if self.mcp_server:
            return mcp_client.is_available(self.mcp_server)
        return get_local_tool(self.name) is not None

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.manager_description,
                "parameters": self.llm_schema(),
            },
        }

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "scope": self.scope,
            "requires_approval": self.requires_approval,
            "approval_role": self.approval_role,
            "context_params": self.context_params,
            "manager_description": self.manager_description,
            "available": self.is_available(),
            "unavailable_reason": ""
            if self.is_available()
            else (mcp_client.error(self.mcp_server) if self.mcp_server else "Tool local chưa được cài đặt"),
            "schema": self.llm_schema(),
        }


def _flatten_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $defs/anyOf so the resulting schema is simple enough for tool APIs."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                key = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs.get(key, {"type": "object"})))
            if "anyOf" in node:
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                merged = walk(options[0]) if options else {"type": "string"}
                for k in ("description", "default", "title"):
                    if k in node:
                        merged.setdefault(k, node[k])
                return merged
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class Catalog:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self.reload()

    def reload(self) -> None:
        rows = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or []
        self._specs = {
            r["name"]: ToolSpec(
                name=r["name"],
                provider=r.get("provider", "local"),
                scope=r.get("scope", "business"),
                requires_approval=bool(r.get("requires_approval", False)) or bool(r.get("approval_role")),
                approval_role=str(r.get("approval_role") or ("bql" if r.get("requires_approval") else "")),
                context_params=list(r.get("context_params") or []),
                manager_description=r.get("manager_description", ""),
            )
            for r in rows
        }

    async def refresh_mcp(self) -> None:
        await mcp_client.refresh()

    def refresh_mcp_sync(self) -> None:
        mcp_client.refresh_sync()

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    def openai_tools(self, names: list[str]) -> list[dict[str, Any]]:
        out = []
        for n in names:
            spec = self.get(n)
            if spec and spec.is_available():
                out.append(spec.to_openai_tool())
        return out


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog
