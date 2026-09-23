"""Chroma-backed knowledge store, one collection per domain.

A document lives in the domain's library, not under any one agent. What an agent
can retrieve is decided by two things: which docs it explicitly links to (via
AgentDoc), plus every "domain"-scope doc, which is visible to all agents of that
domain. Embeddings are computed with Qwen, not by Chroma.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from sqlmodel import select

from backend.app.config import settings
from backend.llm.qwen_client import embed

logger = logging.getLogger("knowledge")

_client = None
_collections: dict[str, Any] = {}
# The Evaluator runs several cases concurrently (ThreadPoolExecutor), and each one
# calls into this module. Chroma's PersistentClient is not safe to construct twice
# concurrently against the same path — two threads racing through the lazy "if
# None: create" checks below corrupted the client with errors as opaque as
# "'RustBindingsAPI' object has no attribute 'bindings'". A single lock around the
# one-time construction (not around every query) fixes it without serializing
# actual retrieval calls.
_init_lock = threading.RLock()  # reentrant: _collection() calls _get_client() while holding it


def _get_client():
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:  # re-check: another thread may have won the race
                import chromadb

                settings.chroma_path.mkdir(parents=True, exist_ok=True)
                _client = chromadb.PersistentClient(path=str(settings.chroma_path))
    return _client


def _collection(domain_id: str):
    name = f"knowledge_{domain_id}"
    if name not in _collections:
        with _init_lock:
            if name not in _collections:
                _collections[name] = _get_client().get_or_create_collection(
                    name=name,
                    embedding_function=None,          # we supply vectors ourselves
                    metadata={"hnsw:space": "cosine"},
                )
    return _collections[name]


def add_chunks(
    domain_id: str, doc_id: str, filename: str, chunks: list[str], *, scope: str, doc_version: int
) -> int:
    if not chunks:
        return 0
    vectors = embed(chunks, role=f"embed:{doc_id}")
    _collection(domain_id).add(
        ids=[f"{doc_id}:{i}" for i in range(len(chunks))],
        documents=chunks,
        embeddings=vectors,
        metadatas=[
            {
                "doc_id": doc_id,
                "filename": filename,
                "chunk_index": i,
                "scope": scope,
                "doc_version": doc_version,
            }
            for i in range(len(chunks))
        ],
    )
    logger.info("ingest domain=%s doc=%s file=%s chunks=%d v=%d", domain_id, doc_id, filename, len(chunks), doc_version)
    return len(chunks)


def delete_doc(domain_id: str, doc_id: str) -> None:
    """Delete every chunk of a doc (all versions share the same doc_id)."""
    try:
        _collection(domain_id).delete(where={"doc_id": doc_id})
    except Exception:  # noqa: BLE001 - a missing/empty collection is not an error here
        logger.debug("Không có chunk nào để xóa cho doc=%s (domain=%s)", doc_id, domain_id)


def delete_domain(domain_id: str) -> None:
    try:
        _get_client().delete_collection(f"knowledge_{domain_id}")
        _collections.pop(f"knowledge_{domain_id}", None)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- lookups
def _agent_domain_and_docs(agent_id: str) -> tuple[str, list[str]]:
    """Resolve an agent's domain_id and the list of doc_ids it has linked."""
    from backend.app.db import session_scope
    from backend.app.models import Agent, AgentDoc

    with session_scope() as s:
        agent = s.get(Agent, agent_id)
        domain_id = agent.domain_id if agent else settings.domain_id
        doc_ids = [
            r.doc_id for r in s.exec(select(AgentDoc).where(AgentDoc.agent_id == agent_id)).all()
        ]
        return domain_id, doc_ids


def _run_query(domain_id: str, where: dict[str, Any], text: str, top_k: int) -> list[dict[str, Any]]:
    col = _collection(domain_id)
    try:
        if col.count() == 0:
            return []
        vec = embed([text], role=f"embed_query:{domain_id}")[0]
        res = col.query(
            query_embeddings=[vec],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001 - RAG must never break a room turn
        logger.warning("Truy vấn knowledge lỗi: %s", exc)
        return []

    hits: list[dict[str, Any]] = []
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        if dist is not None and dist > settings.rag_max_distance:
            continue
        hits.append(
            {
                "noi_dung": doc,
                "filename": meta.get("filename", ""),
                "doc_id": meta.get("doc_id", ""),
                "chunk_index": meta.get("chunk_index", 0),
                "khoang_cach": round(float(dist), 4) if dist is not None else None,
            }
        )
    return hits


def query(agent_id: str, text: str, top_k: int | None = None) -> list[dict[str, Any]]:
    """What a specialist agent can see: its linked docs, plus every domain-scope doc."""
    if not text.strip():
        return []
    top_k = top_k or settings.top_k
    domain_id, doc_ids = _agent_domain_and_docs(agent_id)
    where = (
        {"$or": [{"doc_id": {"$in": doc_ids}}, {"scope": "domain"}]}
        if doc_ids
        else {"scope": "domain"}
    )
    return _run_query(domain_id, where, text, top_k)


def query_domain_scope(domain_id: str, text: str, top_k: int | None = None) -> list[dict[str, Any]]:
    """Domain-wide FAQ lookup for the receptionist: only scope="domain" docs."""
    if not text.strip():
        return []
    top_k = top_k or settings.top_k
    return _run_query(domain_id, {"scope": "domain"}, text, top_k)
