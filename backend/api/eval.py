"""Evaluator API — case management, running an evaluation, and regression."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agents import evaluator
from backend.app.events import bus, load_events, sse_format

logger = logging.getLogger("api.eval")
router = APIRouter(prefix="/api/eval", tags=["eval"])


class GenerateIn(BaseModel):
    n: int | None = None
    auto_approve: bool = False


@router.post("/agents/{agent_id}/generate")
async def generate(agent_id: str, payload: GenerateIn) -> dict[str, Any]:
    try:
        cases = await asyncio.to_thread(evaluator.generate_cases, agent_id, n=payload.n, auto_approve=payload.auto_approve)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"agent_id": agent_id, "cases": cases}


@router.get("/agents/{agent_id}/cases")
def list_cases(agent_id: str) -> list[dict[str, Any]]:
    return evaluator.list_cases(agent_id)


class ManualCaseIn(BaseModel):
    agent_id: str
    ticket_text: str = Field(min_length=3)
    resident_id: str = ""
    phan_hoi_bo_sung: list[str] = Field(default_factory=list)
    expected_agents: list[str] = Field(default_factory=list)
    forbidden_agents: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_sources: list[str] = Field(default_factory=list)
    rubric: str = ""
    loai: str = "dung_nang_luc"
    approved: bool = True


@router.post("/cases", status_code=201)
def create_case(payload: ManualCaseIn) -> dict[str, Any]:
    return evaluator.create_manual_case(payload.model_dump())


class ApproveIn(BaseModel):
    approved: bool = True


@router.put("/cases/{case_id}/approve")
def approve(case_id: str, payload: ApproveIn) -> dict[str, Any]:
    try:
        return evaluator.approve_case(case_id, payload.approved)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


class RunIn(BaseModel):
    trigger: str = "manual"


@router.post("/agents/{agent_id}/run")
async def run(agent_id: str, payload: RunIn) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(evaluator.run_agent, agent_id, trigger=payload.trigger)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@router.get("/agents/{agent_id}/runs")
def list_runs(agent_id: str) -> list[dict[str, Any]]:
    return evaluator.list_runs(agent_id)


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        return evaluator.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    """Same SSE mechanism the ticket timeline uses, keyed under a synthetic id."""
    key = f"EVALRUN-{run_id}"
    queue = bus.subscribe(key)

    async def generator():
        try:
            for record in load_events(key):
                yield sse_format(record)
            yield ": bat dau theo doi truc tiep\n\n"
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=15)
                    yield sse_format(record)
                    if record["type"] == "eval_run_end":
                        return
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(key, queue)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class RegressionIn(BaseModel):
    candidate_agent_id: str | None = None
    domain_id: str | None = None


@router.post("/regression")
async def regression(payload: RegressionIn) -> dict[str, Any]:
    return await asyncio.to_thread(evaluator.regression, payload.candidate_agent_id, payload.domain_id)
