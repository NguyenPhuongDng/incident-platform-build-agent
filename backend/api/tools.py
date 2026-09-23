"""Tool catalog, with live availability of the MCP-provided ones."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.tools.catalog import get_catalog

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
def list_tools() -> list[dict[str, Any]]:
    return [spec.to_public() for spec in get_catalog().all()]


@router.post("/refresh")
def refresh() -> dict[str, Any]:
    catalog = get_catalog()
    catalog.refresh_mcp_sync()
    return {"ok": True, "tools": [s.to_public() for s in catalog.all()]}
