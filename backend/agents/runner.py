"""AgentRunner: one turn of one specialist agent.

RAG retrieval -> tool-calling loop -> a single JSON output in the agreed schema.
Kept deliberately explicit so every step is observable; the interface is narrow
enough that the room core could later be swapped for a group-chat framework.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.agents.platform_prompt import build_system_prompt, empty_output, normalize_output
from backend.app.config import settings
from backend.app.events import bus
from backend.domain.loader import DomainPack, load_domain
from backend.knowledge import store
from backend.llm import qwen_client as llm
from backend.tools.catalog import get_catalog
from backend.tools.executor import ToolContext, get_executor

logger = logging.getLogger("runner")


@dataclass
class AgentTurnResult:
    agent_id: str
    display_name: str
    output: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    rag_hits: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "output": self.output,
            "tool_calls": self.tool_calls,
            "rag_hits": self.rag_hits,
            "error": self.error,
        }


class AgentRunner:
    def __init__(self, domain: DomainPack | None = None) -> None:
        self.domain = domain or load_domain()
        self.catalog = get_catalog()
        self.executor = get_executor()

    def run(
        self,
        agent: dict[str, Any],
        *,
        ticket: dict[str, Any],
        instruction: str,
        transcript: list[dict[str, Any]] | None = None,
        sandbox: bool = False,
    ) -> AgentTurnResult:
        agent_id = agent["id"]
        display_name = agent["display_name"]
        ticket_id = ticket.get("id", "")
        emit = bool(ticket_id) and not sandbox

        if emit:
            bus.emit(ticket_id, "agent_start", {"agent_id": agent_id, "display_name": display_name,
                                                "chi_dan": instruction}, actor=agent_id)

        # 1) knowledge retrieval
        query_text = f"{instruction}\n{ticket.get('summary', '')}\n{json.dumps(ticket.get('fields') or {}, ensure_ascii=False)}"
        rag_hits = store.query(agent_id, query_text)
        if emit:
            bus.emit(
                ticket_id,
                "rag_hits",
                {
                    "agent_id": agent_id,
                    "so_ket_qua": len(rag_hits),
                    "ket_qua": [
                        {"filename": h["filename"], "khoang_cach": h["khoang_cach"],
                         "trich": h["noi_dung"][:400]}
                        for h in rag_hits
                    ],
                },
                actor=agent_id,
            )

        # 2) prompt
        system_prompt = build_system_prompt(
            display_name=display_name,
            capability=agent["capability"],
            business_prompt=agent.get("business_prompt", ""),
            domain=self.domain,
            rag_hits=rag_hits,
            ticket=ticket,
            transcript=transcript or [],
            instruction=instruction,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Hãy xử lý lượt của bạn."},
        ]

        ctx = ToolContext(
            ticket_id=ticket_id,
            ma_can_ho=ticket.get("apartment_id", ""),
            ma_cu_dan=ticket.get("resident_id", ""),
            agent_id=agent_id,
            allowed_tools=list(agent.get("tools") or []),
            sandbox=sandbox,
            emit_events=emit,
            # Chỉ phiên thật mới có người duyệt; sandbox/đánh giá chạy thẳng.
            wait_for_approval=not sandbox,
        )
        tools = self.catalog.openai_tools(ctx.allowed_tools)

        # 3) tool-calling loop
        try:
            for _ in range(settings.max_tool_iterations):
                msg = llm.chat_with_tools(messages, tools or None, role=agent_id)
                calls = getattr(msg, "tool_calls", None) or []
                if not calls:
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments},
                            }
                            for c in calls
                        ],
                    }
                )
                for c in calls:
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = self.executor.execute(c.function.name, args, ctx)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": c.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
        except llm.LLMError as exc:
            logger.warning("agent=%s lỗi vòng tool: %s", agent_id, exc)
            return self._fail(agent_id, display_name, rag_hits, ctx.trace, str(exc), ticket_id, emit)

        # 4) final JSON
        messages.append(
            {
                "role": "user",
                "content": "Kết thúc lượt: trả về DUY NHẤT một object JSON đúng schema đã nêu, không kèm giải thích.",
            }
        )
        try:
            raw = llm.chat_json(messages, role=agent_id)
            output = normalize_output(raw, self_id=agent_id)
        except llm.LLMError as exc:
            logger.warning("agent=%s không trả JSON: %s", agent_id, exc)
            fallback = next(
                (m["content"] for m in reversed(messages) if m["role"] == "assistant" and m.get("content")),
                "",
            )
            output = empty_output(fallback or f"(không đọc được kết luận: {exc})")

        if not output["ket_luan"]:
            output["ket_luan"] = "(agent không đưa ra kết luận)"

        if emit:
            bus.emit(ticket_id, "agent_output", {"agent_id": agent_id, "display_name": display_name,
                                                 "output": output}, actor=agent_id)
        return AgentTurnResult(agent_id, display_name, output, ctx.trace, rag_hits)

    def _fail(self, agent_id, display_name, rag_hits, tool_calls, err, ticket_id, emit) -> AgentTurnResult:
        output = empty_output(f"Không hoàn tất được lượt do lỗi hệ thống: {err[:200]}")
        if emit:
            bus.emit(ticket_id, "agent_output", {"agent_id": agent_id, "display_name": display_name,
                                                 "output": output, "loi": err[:200]}, actor=agent_id)
        return AgentTurnResult(agent_id, display_name, output, tool_calls, rag_hits, error=err[:300])
