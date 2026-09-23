"""Knowledge: a domain-wide library (`/api/knowledge`) plus the per-agent links
(`/api/agents/{agent_id}/knowledge`). Uploading and linking documents is a manager
action — no agent, including Builder, ever calls these on its own."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from backend.agents import registry
from backend.app.config import settings
from backend.knowledge import library, store

logger = logging.getLogger("api.knowledge")

# ------------------------------------------------------------------------- library
library_router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except library.LibraryError as exc:
        detail: Any = str(exc)
        if exc.linked_agents:
            detail = {"message": str(exc), "linked_agents": exc.linked_agents}
        raise HTTPException(409 if exc.linked_agents else 400, detail) from exc


@library_router.get("")
def list_library(domain_id: str | None = None) -> list[dict[str, Any]]:
    return library.list_docs(domain_id or settings.domain_id)


@library_router.post("", status_code=201)
async def upload_to_library(
    file: UploadFile = File(...),
    scope: str = Form("restricted"),
    domain_id: str = Form(""),
) -> dict[str, Any]:
    raw = await file.read()
    filename = file.filename or "tai_lieu.txt"
    return _guard(library.create_doc, domain_id or settings.domain_id, filename, raw, scope=scope)


@library_router.get("/{doc_id}")
def get_doc(doc_id: str) -> dict[str, Any]:
    return _guard(library.get_doc, doc_id)


@library_router.put("/{doc_id}")
async def upload_new_version(doc_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    filename = file.filename or "tai_lieu.txt"
    return _guard(library.update_doc, doc_id, filename, raw)


class ScopeIn(BaseModel):
    scope: str


@library_router.put("/{doc_id}/scope")
def change_scope(doc_id: str, payload: ScopeIn) -> dict[str, Any]:
    return _guard(library.set_scope, doc_id, payload.scope)


@library_router.delete("/{doc_id}", status_code=204)
def delete_doc(doc_id: str, force: bool = Query(False)) -> None:
    _guard(library.delete_doc, doc_id, force=force)


class TestQueryIn(BaseModel):
    agent_id: str
    text: str


@library_router.post("/test-query")
def test_query(payload: TestQueryIn) -> dict[str, Any]:
    if registry.get_agent(payload.agent_id) is None:
        raise HTTPException(404, "Không tìm thấy agent")
    hits = store.query(payload.agent_id, payload.text)
    return {"agent_id": payload.agent_id, "so_ket_qua": len(hits), "ket_qua": hits}


# --------------------------------------------------------------------- agent-scoped
agent_router = APIRouter(prefix="/api/agents/{agent_id}/knowledge", tags=["knowledge"])


@agent_router.get("")
def list_agent_docs(agent_id: str) -> list[dict[str, Any]]:
    if registry.get_agent(agent_id) is None:
        raise HTTPException(404, "Không tìm thấy agent")
    return library.list_agent_docs(agent_id)


@agent_router.post("", status_code=201)
async def upload_and_link(
    agent_id: str, file: UploadFile = File(...), scope: str = Form("restricted")
) -> dict[str, Any]:
    """Convenience the Builder/agent form uses: upload straight into the library
    and immediately link it to this agent — one manager click, both effects."""
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, "Không tìm thấy agent")
    raw = await file.read()
    filename = file.filename or "tai_lieu.txt"
    doc = _guard(library.create_doc, agent["domain_id"], filename, raw, scope=scope)
    _guard(library.link, agent_id, doc["id"])
    return library.get_doc(doc["id"])


class LinkIn(BaseModel):
    doc_id: str


@agent_router.post("/link")
def link_existing(agent_id: str, payload: LinkIn) -> dict[str, Any]:
    if registry.get_agent(agent_id) is None:
        raise HTTPException(404, "Không tìm thấy agent")
    _guard(library.link, agent_id, payload.doc_id)
    return library.get_doc(payload.doc_id)


@agent_router.delete("/{doc_id}", status_code=204)
def unlink_doc(agent_id: str, doc_id: str) -> None:
    """Unlinks the doc from this agent. The document itself stays in the library —
    delete it there (`DELETE /api/knowledge/{doc_id}`) if it should be gone entirely."""
    _guard(library.unlink, agent_id, doc_id)


# combined export so main.py's `app.include_router(module.router)` still works
router = APIRouter()
router.include_router(library_router)
router.include_router(agent_router)
