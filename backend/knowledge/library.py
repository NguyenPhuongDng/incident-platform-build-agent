"""The domain knowledge library: CRUD on documents, and the agent<->doc links.

This is the layer the API and the seed script call. `store.py` only knows about
Chroma; this module owns the SQL side (KnowledgeDoc, AgentDoc) and ties the two
together, plus the one LLM call needed to title/summarize a document on upload.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import select

from backend.app.db import session_scope
from backend.app.models import Agent, AgentDoc, KnowledgeDoc
from backend.knowledge import store
from backend.knowledge.chunker import chunk_text
from backend.knowledge.loaders import UnsupportedFile, is_markdown, load_text
from backend.llm import qwen_client as llm

logger = logging.getLogger("knowledge.library")

VALID_SCOPES = {"domain", "restricted"}


class LibraryError(ValueError):
    def __init__(self, message: str, *, linked_agents: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.linked_agents = linked_agents or []


def _summarize(filename: str, text: str) -> tuple[str, str]:
    """One cheap LLM call for a human title and a 2–3 sentence summary."""
    title_fallback = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip().capitalize()
    sample = text.strip()[:3000]
    if not sample:
        return title_fallback, ""
    try:
        raw = llm.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Đọc đoạn trích tài liệu sau và trả về JSON "
                        '{"tieu_de": str, "tom_tat": str}. "tieu_de" là tên ngắn gọn cho tài '
                        'liệu (dưới 8 từ). "tom_tat" là 2-3 câu tóm tắt nội dung chính, đủ để '
                        "một người khác biết tài liệu này dùng để làm gì mà không cần đọc toàn bộ."
                    ),
                },
                {"role": "user", "content": f"Tên tệp: {filename}\n\nNội dung:\n{sample}"},
            ],
            role="knowledge_summarize",
        )
        title = str(raw.get("tieu_de") or "").strip() or title_fallback
        summary = str(raw.get("tom_tat") or "").strip()
        return title, summary
    except llm.LLMError as exc:
        logger.warning("Tóm tắt tài liệu lỗi, dùng tiêu đề mặc định: %s", exc)
        return title_fallback, ""


def _to_dict(d: KnowledgeDoc, *, linked_agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": d.id,
        "domain_id": d.domain_id,
        "filename": d.filename,
        "title": d.title,
        "summary": d.summary,
        "scope": d.scope,
        "version": d.version,
        "num_chunks": d.num_chunks,
        "status": d.status,
        "error": d.error,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
        "linked_agents": linked_agents if linked_agents is not None else _linked_agents(d.id),
    }


def _linked_agents(doc_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        links = s.exec(select(AgentDoc).where(AgentDoc.doc_id == doc_id)).all()
        out = []
        for link in links:
            agent = s.get(Agent, link.agent_id)
            if agent:
                out.append({"id": agent.id, "display_name": agent.display_name})
        return out


# --------------------------------------------------------------------------- CRUD
def create_doc(
    domain_id: str, filename: str, raw: bytes, *, scope: str = "restricted", doc_id: str | None = None
) -> dict[str, Any]:
    if scope not in VALID_SCOPES:
        raise LibraryError(f"scope phải là một trong {sorted(VALID_SCOPES)}")
    try:
        text = load_text(filename, raw)
    except UnsupportedFile as exc:
        raise LibraryError(str(exc)) from exc

    chunks = chunk_text(text, markdown=is_markdown(filename))
    if not chunks:
        raise LibraryError("Không trích được nội dung nào từ tệp này")

    doc_id = doc_id or f"DOC-{uuid.uuid4().hex[:10]}"
    title, summary = _summarize(filename, text)

    try:
        n = store.add_chunks(domain_id, doc_id, filename, chunks, scope=scope, doc_version=1)
        status, error = "ready", ""
    except Exception as exc:  # noqa: BLE001 - surfaced to the manager in the UI
        logger.exception("Nạp tài liệu lỗi")
        n, status, error = 0, "error", str(exc)[:300]

    with session_scope() as s:
        s.add(
            KnowledgeDoc(
                id=doc_id, domain_id=domain_id, filename=filename, title=title, summary=summary,
                scope=scope, version=1, num_chunks=n, status=status, error=error,
            )
        )
    if status == "error":
        raise LibraryError(f"Nạp tài liệu thất bại: {error}")
    return get_doc(doc_id)


def update_doc(doc_id: str, filename: str, raw: bytes) -> dict[str, Any]:
    """Upload a new version: old chunks are replaced, every agent that links this
    doc automatically sees the new content — nothing to re-link."""
    with session_scope() as s:
        doc = s.get(KnowledgeDoc, doc_id)
        if doc is None:
            raise LibraryError("Không tìm thấy tài liệu")
        domain_id, next_version = doc.domain_id, doc.version + 1

    try:
        text = load_text(filename, raw)
    except UnsupportedFile as exc:
        raise LibraryError(str(exc)) from exc
    chunks = chunk_text(text, markdown=is_markdown(filename))
    if not chunks:
        raise LibraryError("Không trích được nội dung nào từ tệp này")

    title, summary = _summarize(filename, text)
    store.delete_doc(domain_id, doc_id)
    try:
        n = store.add_chunks(domain_id, doc_id, filename, chunks, scope="restricted", doc_version=next_version)
        status, error = "ready", ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cập nhật tài liệu lỗi")
        n, status, error = 0, "error", str(exc)[:300]

    with session_scope() as s:
        doc = s.get(KnowledgeDoc, doc_id)
        doc.filename, doc.title, doc.summary = filename, title, summary
        doc.version, doc.num_chunks = next_version, n
        doc.status, doc.error = status, error
        from datetime import datetime

        doc.updated_at = datetime.utcnow()
        # keep the existing scope; re-ingest must not silently widen/narrow visibility
        scope = doc.scope
        s.add(doc)
    if status == "error":
        raise LibraryError(f"Cập nhật tài liệu thất bại: {error}")
    # re-ingest used the placeholder scope "restricted" above; fix it up if the doc
    # was actually "domain" scoped, without a second embedding pass.
    if scope == "domain":
        _reingest_with_scope(domain_id, doc_id, filename, chunks, next_version, scope)
    return get_doc(doc_id)


def _reingest_with_scope(domain_id, doc_id, filename, chunks, version, scope) -> None:
    store.delete_doc(domain_id, doc_id)
    store.add_chunks(domain_id, doc_id, filename, chunks, scope=scope, doc_version=version)


def set_scope(doc_id: str, scope: str) -> dict[str, Any]:
    if scope not in VALID_SCOPES:
        raise LibraryError(f"scope phải là một trong {sorted(VALID_SCOPES)}")
    with session_scope() as s:
        doc = s.get(KnowledgeDoc, doc_id)
        if doc is None:
            raise LibraryError("Không tìm thấy tài liệu")
        doc.scope = scope
        s.add(doc)
    # Chunk metadata carries scope too (used by the $or query), so it must be re-tagged.
    _retag_scope_in_chroma(doc_id)
    return get_doc(doc_id)


def _retag_scope_in_chroma(doc_id: str) -> None:
    """Chroma has no in-place metadata update in this client version; re-ingest is
    the simplest correct option and doc uploads are infrequent in this demo."""
    with session_scope() as s:
        doc = s.get(KnowledgeDoc, doc_id)
        if doc is None or doc.status != "ready":
            return
        domain_id, scope, version, filename = doc.domain_id, doc.scope, doc.version, doc.filename
    # We don't keep raw file bytes around, but Chroma still has the chunk texts.
    from backend.knowledge.store import _collection  # local: read-only re-tag helper

    col = _collection(domain_id)
    got = col.get(where={"doc_id": doc_id}, include=["documents", "metadatas"])
    ids, docs, metas = got.get("ids", []), got.get("documents", []), got.get("metadatas", [])
    if not ids:
        return
    for m in metas:
        m["scope"] = scope
    col.update(ids=ids, metadatas=metas)


def delete_doc(doc_id: str, *, force: bool = False) -> None:
    linked = _linked_agents(doc_id)
    if linked and not force:
        raise LibraryError(
            f"Tài liệu đang được {len(linked)} agent sử dụng: "
            + ", ".join(a["display_name"] for a in linked),
            linked_agents=linked,
        )
    with session_scope() as s:
        doc = s.get(KnowledgeDoc, doc_id)
        if doc is None:
            raise LibraryError("Không tìm thấy tài liệu")
        domain_id = doc.domain_id
        for link in s.exec(select(AgentDoc).where(AgentDoc.doc_id == doc_id)).all():
            s.delete(link)
        s.delete(doc)
    store.delete_doc(domain_id, doc_id)


def get_doc(doc_id: str) -> dict[str, Any]:
    with session_scope() as s:
        doc = s.get(KnowledgeDoc, doc_id)
        if doc is None:
            raise LibraryError("Không tìm thấy tài liệu")
        return _to_dict(doc)


def list_docs(domain_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.exec(select(KnowledgeDoc).where(KnowledgeDoc.domain_id == domain_id)).all()
        return [_to_dict(d) for d in rows]


# --------------------------------------------------------------------------- linking
def link(agent_id: str, doc_id: str) -> None:
    with session_scope() as s:
        agent = s.get(Agent, agent_id)
        doc = s.get(KnowledgeDoc, doc_id)
        if agent is None:
            raise LibraryError("Không tìm thấy agent")
        if doc is None:
            raise LibraryError("Không tìm thấy tài liệu")
        if doc.domain_id != agent.domain_id:
            raise LibraryError("Tài liệu không thuộc domain của agent này")
        existing = s.exec(
            select(AgentDoc).where(AgentDoc.agent_id == agent_id, AgentDoc.doc_id == doc_id)
        ).first()
        if existing is None:
            s.add(AgentDoc(agent_id=agent_id, doc_id=doc_id))


def unlink(agent_id: str, doc_id: str) -> None:
    with session_scope() as s:
        link_row = s.exec(
            select(AgentDoc).where(AgentDoc.agent_id == agent_id, AgentDoc.doc_id == doc_id)
        ).first()
        if link_row is None:
            raise LibraryError("Agent chưa gắn tài liệu này")
        s.delete(link_row)


def list_agent_docs(agent_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        doc_ids = [
            r.doc_id for r in s.exec(select(AgentDoc).where(AgentDoc.agent_id == agent_id)).all()
        ]
        docs = [s.get(KnowledgeDoc, d) for d in doc_ids]
        return [_to_dict(d) for d in docs if d is not None]


def unlink_all_for_agent(agent_id: str) -> None:
    """Used when an agent is deleted; library docs themselves are untouched."""
    with session_scope() as s:
        for link_row in s.exec(select(AgentDoc).where(AgentDoc.agent_id == agent_id)).all():
            s.delete(link_row)
