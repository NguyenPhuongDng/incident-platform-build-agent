"""Builder API — draft and revise a specialist-agent proposal.

Nothing here writes to the `Agent` table. A draft is data the UI pours into the
Agent Builder form; the manager still has to review it and hit Save, which is
the ordinary `POST /api/agents` path with its ordinary validation.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.agents import builder
from backend.app.config import settings

router = APIRouter(prefix="/api/builder", tags=["builder"])


class DraftIn(BaseModel):
    domain_id: str | None = None
    yeu_cau: str = Field(min_length=5)
    agent_id_goi_y: str | None = None


@router.post("/draft")
def make_draft(payload: DraftIn) -> dict[str, Any]:
    try:
        out = builder.draft(payload.domain_id or settings.domain_id, payload.yeu_cau, payload.agent_id_goi_y)
    except Exception as exc:  # noqa: BLE001 - a Builder hiccup must not 500 the UI
        raise HTTPException(502, f"Builder không soạn được bản nháp: {str(exc)[:300]}") from exc
    return out.to_dict()


class ReviseIn(BaseModel):
    draft: dict[str, Any]
    bao_cao_danh_gia: dict[str, Any] = Field(default_factory=dict)
    domain_id: str | None = None


@router.post("/revise")
def revise_draft(payload: ReviseIn) -> dict[str, Any]:
    domain_id = payload.domain_id or payload.draft.get("domain_id") or settings.domain_id
    try:
        out = builder.revise(domain_id, payload.draft, payload.bao_cao_danh_gia)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Builder không sửa được bản nháp: {str(exc)[:300]}") from exc
    return out.to_dict()


class AutoIn(BaseModel):
    domain_id: str | None = None
    yeu_cau: str = Field(min_length=5)
    doc_ids: list[str] = Field(default_factory=list)


@router.post("/auto")
async def auto(payload: AutoIn) -> dict[str, Any]:
    """Builder soạn nháp → Evaluator chấm → Builder tự sửa nếu trượt (tối đa
    BUILDER_MAX_ITERATIONS vòng). Không tự bật agent. Tiến độ theo dõi qua
    GET /api/eval/runs/{run_id}/events cho từng vòng, hoặc chờ kết quả cuối ở đây."""
    import asyncio

    try:
        return await asyncio.to_thread(
            builder.auto_loop, payload.domain_id or settings.domain_id, payload.yeu_cau, payload.doc_ids
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Vòng lặp Builder/Evaluator lỗi: {str(exc)[:300]}") from exc
