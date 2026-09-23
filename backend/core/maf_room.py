"""Meeting room built on Microsoft Agent Framework (GroupChat orchestration).

Mapping between platform concepts and the framework:

    registry (DB rows)      -> GroupChat participants (agent_framework.Agent)
    Điều phối (LLM router)  -> termination_condition + selection_func
    knowledge / RAG         -> ContextProvider.before_run
    tool catalog            -> FunctionTool(input_model=<schema with context params hidden>)
    ToolExecutor            -> kept as-is; it stays the security boundary
    observability           -> the same event bus, fed from the provider hooks

Framework note: the group chat asks `termination_condition` BEFORE it asks
`selection_func`, so the router runs once per round inside the termination check
and parks its choice in the session for the selector to hand back.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_framework import (
    Agent,
    ChatContext,
    ChatMiddleware,
    ContextProvider,
    FunctionTool,
    Message,
    MiddlewareFailure,
    MiddlewareTermination,
    SessionContext,
    SupportsAgentRun,
)
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework.orchestrations import GroupChatBuilder, GroupChatState
from sqlmodel import select

from backend.agents.platform_prompt import (
    PLATFORM_TEMPLATE,
    format_rag,
    format_ticket,
    format_transcript,
    normalize_output,
)
from backend.app import trace
from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.events import bus
from backend.app.models import Action, Ticket
from backend.core.orchestrator import Orchestrator
from backend.core.receptionist import Receptionist, ticket_to_dict
from backend.core import followup, workflow
from backend.domain.loader import load_domain
from backend.knowledge import store
from backend.tools.catalog import get_catalog
from backend.tools.executor import ToolContext, get_executor

logger = logging.getLogger("maf_room")

MAX_CALLS_PER_AGENT = 3


# --------------------------------------------------------------------------- session
@dataclass
class RoomSession:
    """Mutable per-run state shared by the router callbacks and the providers."""

    ticket: dict[str, Any]
    members: list[dict[str, Any]]
    transcript: list[dict[str, Any]] = field(default_factory=list)
    call_count: dict[str, int] = field(default_factory=dict)
    instruction: str = ""
    next_speaker: str = ""
    stop_reason: str = ""
    pending_questions: list[str] = field(default_factory=list)
    retry_used: bool = False
    handoff_retry_used: bool = False
    chain_retry_used: bool = False
    turns: int = 0

    @property
    def ticket_id(self) -> str:
        return self.ticket.get("id", "")

    @property
    def user_question(self) -> str:
        return "\n".join(self.pending_questions)

    def member(self, agent_id: str) -> dict[str, Any] | None:
        return next((m for m in self.members if m["id"] == agent_id), None)

    def selectable(self) -> list[dict[str, Any]]:
        return [m for m in self.members if self.call_count.get(m["id"], 0) < MAX_CALLS_PER_AGENT]


# --------------------------------------------------------------------------- RAG hook
class KnowledgeProvider(ContextProvider):
    """Injects per-turn context (ticket, transcript, router instruction, RAG) and
    captures the agent's JSON answer on the way out."""

    def __init__(self, agent_row: dict[str, Any], session: RoomSession) -> None:
        super().__init__(source_id=f"knowledge:{agent_row['id']}")
        self.agent_row = agent_row
        self.session = session

    async def before_run(self, *, agent: SupportsAgentRun, session: Any,
                         context: SessionContext, state: dict[str, Any]) -> None:
        s = self.session
        agent_id = self.agent_row["id"]
        s.turns += 1
        s.call_count[agent_id] = s.call_count.get(agent_id, 0) + 1

        bus.emit(
            s.ticket_id,
            "agent_start",
            {"agent_id": agent_id, "display_name": self.agent_row["display_name"],
             "chi_dan": s.instruction, "engine": "maf"},
            actor=agent_id,
        )

        query = f"{s.instruction}\n{s.ticket.get('summary', '')}\n" \
                f"{json.dumps(s.ticket.get('fields') or {}, ensure_ascii=False)}"
        hits = store.query(agent_id, query)
        bus.emit(
            s.ticket_id,
            "rag_hits",
            {
                "agent_id": agent_id,
                "so_ket_qua": len(hits),
                "ket_qua": [
                    {"filename": h["filename"], "khoang_cach": h["khoang_cach"],
                     "trich": h["noi_dung"][:400]}
                    for h in hits
                ],
            },
            actor=agent_id,
        )

        context.extend_instructions(
            self.source_id,
            "TÀI LIỆU THAM KHẢO:\n"
            f"{format_rag(hits)}\n\n"
            "TICKET:\n"
            f"{format_ticket(s.ticket)}\n\n"
            "DIỄN BIẾN PHÒNG HỌP ĐẾN LÚC NÀY:\n"
            f"{format_transcript(s.transcript)}\n\n"
            "CHỈ DẪN CỦA ĐIỀU PHỐI CHO LƯỢT NÀY:\n"
            f"{s.instruction or '(không có chỉ dẫn riêng, hãy xử lý theo năng lực của bạn)'}"
        )

        if s.call_count[agent_id] >= MAX_CALLS_PER_AGENT:
            bus.emit(
                s.ticket_id,
                "guard_triggered",
                {"guard": "gioi_han_luot_moi_thanh_vien", "agent_id": agent_id,
                 "so_lan": s.call_count[agent_id], "xu_ly": "loại khỏi lựa chọn tiếp theo"},
                actor="dieu_phoi",
            )

    async def after_run(self, *, agent: SupportsAgentRun, session: Any,
                        context: SessionContext, state: dict[str, Any]) -> None:
        s = self.session
        agent_id = self.agent_row["id"]
        text = _response_text(context.response)
        output = normalize_output(_parse_json(text) or text, self_id=agent_id)
        if not output["ket_luan"]:
            output["ket_luan"] = "(agent không đưa ra kết luận)"

        s.transcript.append(
            {"agent_id": agent_id, "display_name": self.agent_row["display_name"], "output": output}
        )
        bus.emit(
            s.ticket_id,
            "agent_output",
            {"agent_id": agent_id, "display_name": self.agent_row["display_name"], "output": output},
            actor=agent_id,
        )

        if output.get("can_hoi_them_nguoi_bao"):
            cau_hoi = output["can_hoi_them_nguoi_bao"]
            ly_do_chan = followup.block_reason(s.ticket_id, cau_hoi)
            if ly_do_chan:
            # Guard: đã hỏi ý này rồi (và đã được trả lời), hoặc đã hỏi quá số vòng cho
            # phép. Bỏ câu hỏi khỏi output luôn, nếu không Lễ tân vẫn đem nó đi hỏi lại.
                output["can_hoi_them_nguoi_bao"] = None
                bus.emit(
                    s.ticket_id,
                    "guard_triggered",
                    {"guard": "khong_hoi_lai_nguoi_bao", "agent_id": agent_id,
                     "cau_hoi": cau_hoi, "ly_do": ly_do_chan,
                     "xu_ly": "bỏ câu hỏi, buộc kết luận với thông tin đang có"},
                    actor=agent_id,
                )
            else:
                # Ghim câu hỏi lại thay vì dừng phòng ngay. Một phản ánh nhiều ý ("sửa giúp"
                # kèm "cho hỏi chi phí") chỉ cần một bộ phận cần hỏi lại là những bộ phận còn
                # việc mất lượt, và phần việc của họ rơi sang phiên sau. Để Điều phối chạy nốt,
                # cuối phiên Lễ tân hỏi người báo một thể thay vì hỏi làm nhiều đợt.
                s.pending_questions.append(cau_hoi)
                bus.emit(
                    s.ticket_id,
                    "guard_triggered",
                    {"guard": "can_hoi_nguoi_bao", "agent_id": agent_id,
                     "cau_hoi": cau_hoi,
                     "xu_ly": "ghim câu hỏi, chạy tiếp các bộ phận còn việc"},
                    actor=agent_id,
                )


def _tool_calls_of(response: Any) -> list[dict[str, Any]]:
    """Tên lớp content của framework đổi giữa các bản (bản đang cài không có
    `FunctionCallContent` như tài liệu mô tả), nên dò theo hình dạng — có `name` kèm
    `arguments` — thay vì import một kiểu cụ thể rồi vỡ khi nâng phiên bản."""
    calls: list[dict[str, Any]] = []
    for msg in getattr(response, "messages", None) or []:
        for c in getattr(msg, "contents", None) or []:
            ten, args = getattr(c, "name", None), getattr(c, "arguments", None)
            if ten and args is not None:
                calls.append({"tool": ten, "args": args})
    return calls


class _TraceChat(ChatMiddleware):
    """Agent trong phòng họp MAF gọi thẳng chat client chứ không qua `qwen_client`, nên
    không rơi vào trace ở đó. Đây là chỗ duy nhất nhìn thấy nguyên văn prompt của agent —
    kể cả các khối RAG/ticket/diễn biến do KnowledgeProvider bơm vào từng lượt."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    async def process(self, context: ChatContext, call_next: Any) -> None:
        started = time.time()
        loi = ""
        try:
            await call_next()
        except Exception as exc:  # noqa: BLE001 - ghi lại rồi trả nguyên lỗi cho lớp ngoài
            loi = str(exc)
            raise
        finally:
            ket_qua = getattr(context, "result", None)
            usage = getattr(ket_qua, "usage_details", None) or {}
            trace.write(
                "llm",
                role=self.agent_id,
                model=settings.chat_model,
                ms=int((time.time() - started) * 1000),
                messages=[{"role": str(m.role), "content": m.text} for m in context.messages],
                response=_response_text(ket_qua),
                tool_calls=_tool_calls_of(ket_qua),
                prompt_tokens=usage.get("input_token_count"),
                completion_tokens=usage.get("output_token_count"),
                error=loi or None,
            )


class _RetryMalformedToolCall(ChatMiddleware):
    """DashScope từ chối lệnh gọi tool có `function.arguments` không phải JSON hợp lệ, và
    qwen thỉnh thoảng sinh ra đúng như vậy. Không thử lại thì cú 400 đó thoát khỏi
    `workflow.run` và giết cả phòng họp: các bộ phận sau không kịp phát biểu, người báo
    nhận một câu trả lời cụt. Gọi lại chính request đó thường lấy mẫu ra được lệnh gọi
    đúng định dạng."""

    _DAU_HIEU = ("function.arguments", "InvalidParameter")
    _NHAC = (
        "Lượt vừa rồi bạn tạo lệnh gọi tool có tham số sai định dạng nên hệ thống từ chối. "
        "Gọi lại đi, và phần arguments phải là một object JSON hợp lệ, không kèm chữ nào khác."
    )

    def __init__(self, so_lan_thu: int = 3) -> None:
        self.so_lan_thu = so_lan_thu

    async def process(self, context: ChatContext, call_next: Any) -> None:
        for con_lai in range(self.so_lan_thu - 1, -1, -1):
            try:
                await call_next()
                return
            except (MiddlewareFailure, MiddlewareTermination):
                # Tín hiệu dừng của chính framework — nuốt nó là biến một cú abort
                # fail-closed thành vòng lặp chạy tiếp không ai canh.
                raise
            except Exception as exc:  # noqa: BLE001 - chỉ bắt đúng lỗi định dạng tool-call
                if con_lai == 0 or not any(d in str(exc) for d in self._DAU_HIEU):
                    raise
                logger.warning(
                    "Model trả tool-call sai định dạng, thử lại (còn %d lần): %s",
                    con_lai, str(exc)[:160],
                )
                # Gửi lại y nguyên request thì model lấy mẫu ra đúng lệnh gọi hỏng đó —
                # đã đo: 3 lần thử giống hệt nhau đều hỏng. Phải nhắc thêm một câu thì
                # lượt sau mới khác.
                context.messages = [*context.messages, Message(role="system", contents=[self._NHAC])]


def _response_text(response: Any) -> str:
    if response is None:
        return ""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for msg in getattr(response, "messages", []) or []:
        for content in getattr(msg, "contents", []) or []:
            piece = getattr(content, "text", None)
            if isinstance(piece, str):
                parts.append(piece)
    return "\n".join(parts)


def _parse_json(text: str) -> Any:
    from backend.llm.qwen_client import extract_json

    try:
        return extract_json(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- the room
class MafRoom:
    def __init__(self) -> None:
        self.domain = load_domain()
        self.orchestrator = Orchestrator(self.domain)
        self.receptionist = Receptionist(self.domain)
        self.catalog = get_catalog()
        self.executor = get_executor()

    # ------------------------------------------------------------------ building
    def _chat_client(self, agent_row: dict[str, Any]) -> OpenAIChatCompletionClient:
        """One client per participant, so a per-agent model override stays easy.

        Deliberately the chat-completions client, not `OpenAIChatClient`: that one
        defaults to the newer Responses API (`POST /v1/responses`), which DashScope's
        OpenAI-compatible endpoint only partially implements — it rejects the `tool`
        message role, so every tool round-trip fails with a 400.
        """
        return OpenAIChatCompletionClient(
            model=settings.chat_model,
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
        )

    def _tools_for(self, agent_row: dict[str, Any], session: RoomSession) -> list[FunctionTool]:
        """Wrap granted catalog tools as framework tools.

        The schema handed to the model is `llm_schema()`, so context params are not
        even visible; the values are injected by ToolExecutor from the ticket.
        """
        ctx = ToolContext(
            ticket_id=session.ticket_id,
            ma_can_ho=session.ticket.get("apartment_id", ""),
            ma_cu_dan=session.ticket.get("resident_id", ""),
            agent_id=agent_row["id"],
            allowed_tools=list(agent_row.get("tools") or []),
            # Ticket đánh giá (Evaluator, is_eval=True) chạy đúng luồng thật nhưng
            # không được để lại tác dụng phụ thật: không tạo Action, mã tool ghi có
            # tiền tố EVAL-. Sự kiện vẫn phát bình thường để judge có bằng chứng.
            sandbox=bool(session.ticket.get("is_eval")),
            emit_events=True,
            # HITL: tool cần duyệt sẽ dừng đúng lượt này lại chờ quản lý, trừ vé đánh giá.
            wait_for_approval=not bool(session.ticket.get("is_eval")),
        )
        tools: list[FunctionTool] = []
        for name in ctx.allowed_tools:
            spec = self.catalog.get(name)
            if spec is None or not spec.is_available():
                continue

            def make(tool_name: str):
                def call(**kwargs: Any) -> str:
                    result = self.executor.execute(tool_name, kwargs, ctx)
                    return json.dumps(result, ensure_ascii=False, default=str)

                return call

            tools.append(
                FunctionTool(
                    name=spec.name,
                    description=spec.manager_description,
                    func=make(spec.name),
                    input_model=spec.llm_schema(),
                )
            )
        return tools

    def _participant(self, agent_row: dict[str, Any], session: RoomSession) -> Agent:
        instructions = PLATFORM_TEMPLATE.format(
            display_name=agent_row["display_name"],
            org_name=self.domain.display_name,
            audience=self.domain.audience,
            capability=agent_row["capability"],
            output_schema=(
                '{"ket_luan": str, "da_thuc_hien": [str], "de_xuat": [str], '
                '"can_them_agent": [str], "can_hoi_them_nguoi_bao": str|null, "nguon": [str]}'
            ),
            business_prompt=agent_row.get("business_prompt", "").strip() or "(quản lý chưa mô tả thêm)",
            # These four blocks are re-supplied every turn by KnowledgeProvider.
            rag_block="(sẽ được cung cấp ở mỗi lượt)",
            ticket_block="(sẽ được cung cấp ở mỗi lượt)",
            transcript_block="(sẽ được cung cấp ở mỗi lượt)",
            instruction="(sẽ được cung cấp ở mỗi lượt)",
        )
        return Agent(
            self._chat_client(agent_row),
            instructions,
            name=agent_row["id"],
            description=agent_row["capability"],
            tools=self._tools_for(agent_row, session),
            context_providers=[KnowledgeProvider(agent_row, session)],
            # Retry bọc ngoài trace, nên mỗi lần thử được ghi một dòng riêng — chính là
            # thứ cần để biết lần thử nào hỏng và model nhìn thấy gì ở lần đó.
            middleware=[_RetryMalformedToolCall(), _TraceChat(agent_row["id"])],
        )

    # ------------------------------------------------------------------ router glue
    def _route(self, session: RoomSession) -> None:
        """Run the router once and park its decision on the session."""
        selectable = session.selectable()
        if not selectable:
            session.stop_reason = "Mọi thành viên đều đã phát biểu tối đa số lượt cho phép"
            session.next_speaker = ""
            return

        decision = self.orchestrator.decide(
            ticket=session.ticket,
            members=selectable,
            transcript=session.transcript,
            pending_suggestions=_last_suggestions(session),
            pending_actions=[a["tool"] for a in _pending_actions(session.ticket_id)],
            turns_used=session.turns,
            max_turns=self.domain.max_room_turns,
        )
        bus.emit(session.ticket_id, "router_decision",
                 {**decision.to_dict(), "engine": "maf"}, actor="dieu_phoi")

        if decision.hanh_dong == "ket_thuc":
            # Guard: a member asked for someone who is still selectable — that work is unfinished.
            unserved = [s for s in _last_suggestions(session) if s in {m["id"] for m in selectable}]
            if unserved and not session.handoff_retry_used:
                session.handoff_retry_used = True
                bus.emit(
                    session.ticket_id,
                    "guard_triggered",
                    {"guard": "ban_giao_chua_duoc_phuc_vu", "can_them_agent": unserved,
                     "xu_ly": "nhắc Điều phối gọi thành viên được đề xuất trước khi kết thúc"},
                    actor="dieu_phoi",
                )
                decision = self.orchestrator.decide(
                    ticket=session.ticket,
                    members=selectable,
                    transcript=session.transcript,
                    pending_suggestions=unserved,
                    pending_actions=[a["tool"] for a in _pending_actions(session.ticket_id)],
                    turns_used=session.turns,
                    max_turns=self.domain.max_room_turns,
                    extra_note=f"Thành viên vừa phát biểu đã đề nghị gọi {', '.join(unserved)} và "
                               f"người đó vẫn chọn được. Hãy gọi họ thay vì kết thúc, trừ khi việc "
                               f"đó đã thực sự xong trong diễn biến ở trên.",
                )
                bus.emit(session.ticket_id, "router_decision",
                         {**decision.to_dict(), "engine": "maf"}, actor="dieu_phoi")
            if decision.hanh_dong == "ket_thuc":
                # Guard: quy trình xác nhận đã bắt đầu thì không được đóng phòng giữa chừng.
                note = workflow.blocking_note(session.ticket_id, self.domain)
                if note and not session.chain_retry_used:
                    session.chain_retry_used = True
                    bus.emit(
                        session.ticket_id,
                        "guard_triggered",
                        {"guard": "quy_trinh_xac_nhan_chua_xong", "chi_tiet": note,
                         "xu_ly": "nhắc Điều phối làm nốt bước còn thiếu trước khi kết thúc"},
                        actor="dieu_phoi",
                    )
                    decision = self.orchestrator.decide(
                        ticket=session.ticket,
                        members=selectable,
                        transcript=session.transcript,
                        pending_suggestions=_last_suggestions(session),
                        pending_actions=[a["tool"] for a in _pending_actions(session.ticket_id)],
                        turns_used=session.turns,
                        max_turns=self.domain.max_room_turns,
                        extra_note=note,
                    )
                    bus.emit(session.ticket_id, "router_decision",
                             {**decision.to_dict(), "engine": "maf"}, actor="dieu_phoi")
                if decision.hanh_dong == "ket_thuc":
                    session.stop_reason = decision.ly_do or "Điều phối kết thúc phiên"
                    session.next_speaker = ""
                    return

        if session.member(decision.agent_id) is None or decision.agent_id not in {m["id"] for m in selectable}:
            bus.emit(
                session.ticket_id,
                "guard_triggered",
                {"guard": "agent_khong_hop_le", "agent_id_da_chon": decision.agent_id,
                 "agent_hop_le": [m["id"] for m in selectable],
                 "xu_ly": "nhắc lại Điều phối" if not session.retry_used else "kết thúc phòng họp"},
                actor="dieu_phoi",
            )
            if session.retry_used:
                session.stop_reason = "Điều phối chọn thành viên không hợp lệ hai lần liên tiếp"
                session.next_speaker = ""
                return
            session.retry_used = True
            decision = self.orchestrator.decide(
                ticket=session.ticket,
                members=selectable,
                transcript=session.transcript,
                pending_suggestions=_last_suggestions(session),
                pending_actions=[a["tool"] for a in _pending_actions(session.ticket_id)],
                turns_used=session.turns,
                max_turns=self.domain.max_room_turns,
                extra_note=f"'{decision.agent_id}' KHÔNG có trong danh sách. Chỉ được chọn: "
                           f"{', '.join(m['id'] for m in selectable)}.",
            )
            bus.emit(session.ticket_id, "router_decision",
                     {**decision.to_dict(), "engine": "maf"}, actor="dieu_phoi")
            if decision.hanh_dong == "ket_thuc" or decision.agent_id not in {m["id"] for m in selectable}:
                session.stop_reason = decision.ly_do or "Điều phối chọn thành viên không hợp lệ"
                session.next_speaker = ""
                return

        session.next_speaker = decision.agent_id
        session.instruction = decision.chi_dan

    # ------------------------------------------------------------------ main entry
    async def run_async(self, ticket_id: str, extra_agent_id: str | None = None) -> dict[str, Any]:
        ticket = ticket_to_dict(ticket_id)
        if not ticket:
            raise ValueError("Không tìm thấy phản ánh")

        from backend.agents import registry

        members = registry.active_specialists(ticket["domain_id"])
        # Đánh giá một agent còn "draft" cần nó có mặt trong phòng như thể đã active,
        # nếu không Điều phối sẽ không bao giờ thấy nó và luôn chọn agent active khác.
        # Chỉ cho phép trên vé is_eval — không bao giờ để agent draft lọt vào phòng
        # họp xử lý phản ánh thật.
        if extra_agent_id and ticket.get("is_eval") and extra_agent_id not in {m["id"] for m in members}:
            candidate = registry.get_agent(extra_agent_id)
            if candidate:
                members = members + [candidate]
        session = RoomSession(ticket=ticket, members=members)
        max_turns = self.domain.max_room_turns

        bus.emit(
            ticket_id,
            "room_start",
            {
                "engine": "maf",
                "uu_tien": ticket["priority"],
                "so_thanh_vien": len(members),
                "thanh_vien": [{"id": m["id"], "display_name": m["display_name"]} for m in members],
                "max_turns": max_turns,
            },
            actor="dieu_phoi",
        )

        if not members:
            return await self._finish(session, "Chưa có thành viên nào đang hoạt động")

        participants = [self._participant(m, session) for m in members]

        def termination_condition(messages: list[Message]) -> bool:
            """Framework calls this before every selection — the router lives here."""
            if session.stop_reason:
                return True
            if session.turns >= max_turns:
                bus.emit(
                    ticket_id,
                    "guard_triggered",
                    {"guard": "vuot_so_luot_toi_da", "max_turns": max_turns, "xu_ly": "buộc kết thúc"},
                    actor="dieu_phoi",
                )
                session.stop_reason = f"Đã dùng hết {max_turns} lượt"
                return True
            self._route(session)
            return not session.next_speaker

        def selection_func(state: GroupChatState) -> str:
            # The router already chose during the termination check.
            return session.next_speaker

        workflow = (
            GroupChatBuilder(
                participants=participants,
                selection_func=selection_func,
                termination_condition=termination_condition,
                max_rounds=max_turns,
            )
            .build()
        )

        try:
            await workflow.run(_opening_message(ticket))
        except Exception as exc:  # noqa: BLE001 - framework failure must not kill the ticket
            logger.exception("GroupChat lỗi cho ticket=%s", ticket_id)
            bus.emit(ticket_id, "guard_triggered",
                     {"guard": "loi_framework", "chi_tiet": str(exc)[:300]}, actor="dieu_phoi")
            session.stop_reason = session.stop_reason or f"Lỗi framework: {str(exc)[:200]}"

        return await self._finish(session, session.stop_reason or "Phòng họp kết thúc")

    async def _finish(self, session: RoomSession, stop_reason: str) -> dict[str, Any]:
        ticket_id = session.ticket_id
        pending = _pending_actions(ticket_id)
        if session.pending_questions:
            # Ghim câu hỏi lên ticket để lượt trả lời của người báo ghép được vào đúng câu.
            followup.pin(ticket_id, session.pending_questions)
            final_status = "cho_cu_dan"
        elif pending:
            final_status = "cho_duyet"
        else:
            final_status = "hoan_tat"
        _set_status(ticket_id, final_status)

        bus.emit(
            ticket_id,
            "room_end",
            {
                "engine": "maf",
                "so_luot": session.turns,
                "ly_do_ket_thuc": stop_reason,
                "trang_thai_ticket": final_status,
                "so_hanh_dong_cho_duyet": len(pending),
            },
            actor="dieu_phoi",
        )

        ticket = ticket_to_dict(ticket_id)
        reply = self.receptionist.summarize(
            ticket,
            session.transcript,
            pending_actions=pending,
            extra_note=f"Cần hỏi thêm người báo: {session.user_question}"
            if session.pending_questions
            else "",
        )
        return {
            "ticket_id": ticket_id,
            "engine": "maf",
            "so_luot": session.turns,
            "ly_do_ket_thuc": stop_reason,
            "trang_thai": final_status,
            "transcript": session.transcript,
            "tin_nhan_gui_nguoi_bao": reply,
            "hanh_dong_cho_duyet": pending,
        }


# --------------------------------------------------------------------------- helpers
def _opening_message(ticket: dict[str, Any]) -> str:
    return (
        "Phòng họp xử lý phản ánh sau. Mỗi thành viên khi được gọi hãy trả lời đúng schema JSON đã quy định.\n\n"
        + format_ticket(ticket)
    )


def _last_suggestions(session: RoomSession) -> list[str]:
    if not session.transcript:
        return []
    return [s for s in (session.transcript[-1].get("output") or {}).get("can_them_agent", []) if s]


def _pending_actions(ticket_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.exec(
            select(Action).where(Action.ticket_id == ticket_id, Action.status == "cho_duyet")
        ).all()
        return [{"id": r.id, "tool": r.tool, "agent_id": r.agent_id, "args": dict(r.args or {})} for r in rows]


def _set_status(ticket_id: str, status: str) -> None:
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if t:
            t.status = status
            t.updated_at = datetime.utcnow()
            s.add(t)


def run_room_maf(ticket_id: str, extra_agent_id: str | None = None) -> dict[str, Any]:
    """Blocking entry point, mirroring the built-in engine's signature."""
    import anyio

    try:
        return anyio.run(MafRoom().run_async, ticket_id, extra_agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Phòng họp MAF lỗi cho ticket=%s", ticket_id)
        bus.emit(ticket_id, "room_end",
                 {"loi": str(exc)[:300], "ly_do_ket_thuc": "lỗi hệ thống", "engine": "maf"},
                 actor="dieu_phoi")
        return {"ticket_id": ticket_id, "loi": str(exc)[:300], "engine": "maf"}
