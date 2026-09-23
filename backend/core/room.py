"""The meeting room loop.

The orchestrator only *proposes* who speaks; every safety rule below is enforced
in code, so a bad LLM decision can never run away with the room.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlmodel import select

from backend.agents import registry
from backend.agents.runner import AgentRunner
from backend.app import trace
from backend.app.db import session_scope
from backend.app.events import bus
from backend.app.models import Action, Ticket
from backend.core.orchestrator import Orchestrator
from backend.core.receptionist import Receptionist, ticket_to_dict
from backend.core import followup, workflow
from backend.domain.loader import load_domain

logger = logging.getLogger("room")

MAX_CALLS_PER_AGENT = 3


class Room:
    def __init__(self) -> None:
        self.domain = load_domain()
        self.orchestrator = Orchestrator(self.domain)
        self.runner = AgentRunner(self.domain)
        self.receptionist = Receptionist(self.domain)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _pending_actions(ticket_id: str) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.exec(
                select(Action).where(Action.ticket_id == ticket_id, Action.status == "cho_duyet")
            ).all()
            return [{"id": r.id, "tool": r.tool, "agent_id": r.agent_id, "args": dict(r.args or {})} for r in rows]

    @staticmethod
    def _set_status(ticket_id: str, status: str) -> None:
        with session_scope() as s:
            t = s.get(Ticket, ticket_id)
            if t:
                t.status = status
                t.updated_at = datetime.utcnow()
                s.add(t)

    # ------------------------------------------------------------------ main loop
    def run(self, ticket_id: str, extra_agent_id: str | None = None) -> dict[str, Any]:
        ticket = ticket_to_dict(ticket_id)
        if not ticket:
            raise ValueError("Không tìm thấy phản ánh")

        members = registry.active_specialists(ticket["domain_id"])
        # Xem ghi chú tương ứng trong maf_room.py: cho agent draft "ngồi" trong phòng
        # trong lúc đánh giá sandbox, chỉ khi vé đánh dấu is_eval.
        if extra_agent_id and ticket.get("is_eval") and extra_agent_id not in {m["id"] for m in members}:
            candidate = registry.get_agent(extra_agent_id)
            if candidate:
                members = members + [candidate]
        max_turns = self.domain.max_room_turns
        bus.emit(
            ticket_id,
            "room_start",
            {
                "uu_tien": ticket["priority"],
                "so_thanh_vien": len(members),
                "thanh_vien": [{"id": m["id"], "display_name": m["display_name"]} for m in members],
                "max_turns": max_turns,
            },
            actor="dieu_phoi",
        )

        transcript: list[dict[str, Any]] = []
        call_count: dict[str, int] = {}
        pending_suggestions: list[str] = []
        stop_reason = ""
        pending_questions: list[str] = []
        retry_used = False
        chain_retry_used = False

        turns = 0
        while turns < max_turns:
            available = [m for m in members if call_count.get(m["id"], 0) < MAX_CALLS_PER_AGENT]
            if not available:
                stop_reason = "Mọi thành viên đều đã phát biểu tối đa số lượt cho phép"
                break

            decision = self.orchestrator.decide(
                ticket=ticket,
                members=available,
                transcript=transcript,
                pending_suggestions=pending_suggestions,
                pending_actions=[a["tool"] for a in self._pending_actions(ticket_id)],
                turns_used=turns,
                max_turns=max_turns,
            )
            bus.emit(ticket_id, "router_decision", decision.to_dict(), actor="dieu_phoi")

            if decision.hanh_dong == "ket_thuc":
                # Guard: quy trình xác nhận đã bắt đầu thì không được đóng phòng giữa chừng.
                note = workflow.blocking_note(ticket_id, self.domain)
                if note and not chain_retry_used:
                    chain_retry_used = True
                    bus.emit(
                        ticket_id,
                        "guard_triggered",
                        {"guard": "quy_trinh_xac_nhan_chua_xong", "chi_tiet": note,
                         "xu_ly": "nhắc Điều phối làm nốt bước còn thiếu trước khi kết thúc"},
                        actor="dieu_phoi",
                    )
                    decision = self.orchestrator.decide(
                        ticket=ticket, members=available, transcript=transcript,
                        pending_suggestions=pending_suggestions,
                        pending_actions=[a["tool"] for a in self._pending_actions(ticket_id)],
                        turns_used=turns, max_turns=max_turns, extra_note=note,
                    )
                    bus.emit(ticket_id, "router_decision", decision.to_dict(), actor="dieu_phoi")
                if decision.hanh_dong == "ket_thuc":
                    stop_reason = decision.ly_do or "Điều phối kết thúc phiên"
                    break

            agent = next((m for m in available if m["id"] == decision.agent_id), None)
            if agent is None:
                # Guard: the orchestrator named someone who is not selectable.
                bus.emit(
                    ticket_id,
                    "guard_triggered",
                    {
                        "guard": "agent_khong_hop_le",
                        "agent_id_da_chon": decision.agent_id,
                        "agent_hop_le": [m["id"] for m in available],
                        "xu_ly": "nhắc lại Điều phối" if not retry_used else "kết thúc phòng họp",
                    },
                    actor="dieu_phoi",
                )
                if retry_used:
                    stop_reason = "Điều phối chọn thành viên không hợp lệ hai lần liên tiếp"
                    break
                retry_used = True
                decision = self.orchestrator.decide(
                    ticket=ticket,
                    members=available,
                    transcript=transcript,
                    pending_suggestions=pending_suggestions,
                    pending_actions=[a["tool"] for a in self._pending_actions(ticket_id)],
                    turns_used=turns,
                    max_turns=max_turns,
                    extra_note=f"'{decision.agent_id}' KHÔNG có trong danh sách. Chỉ được chọn: "
                               f"{', '.join(m['id'] for m in available)}.",
                )
                bus.emit(ticket_id, "router_decision", decision.to_dict(), actor="dieu_phoi")
                if decision.hanh_dong == "ket_thuc":
                    stop_reason = decision.ly_do or "Điều phối kết thúc phiên"
                    break
                agent = next((m for m in available if m["id"] == decision.agent_id), None)
                if agent is None:
                    bus.emit(
                        ticket_id,
                        "guard_triggered",
                        {"guard": "agent_khong_hop_le", "agent_id_da_chon": decision.agent_id,
                         "xu_ly": "kết thúc phòng họp"},
                        actor="dieu_phoi",
                    )
                    stop_reason = "Điều phối chọn thành viên không hợp lệ hai lần liên tiếp"
                    break

            result = self.runner.run(
                agent, ticket=ticket, instruction=decision.chi_dan, transcript=transcript,
                sandbox=bool(ticket.get("is_eval")),
            )
            turns += 1
            call_count[agent["id"]] = call_count.get(agent["id"], 0) + 1
            transcript.append(
                {"agent_id": agent["id"], "display_name": agent["display_name"], "output": result.output}
            )

            if call_count[agent["id"]] >= MAX_CALLS_PER_AGENT:
                bus.emit(
                    ticket_id,
                    "guard_triggered",
                    {"guard": "gioi_han_luot_moi_thanh_vien", "agent_id": agent["id"],
                     "so_lan": call_count[agent["id"]], "xu_ly": "loại khỏi lựa chọn tiếp theo"},
                    actor="dieu_phoi",
                )

            suggested = result.output.get("can_them_agent") or []
            pending_suggestions = [s for s in suggested if s]

            if result.output.get("can_hoi_them_nguoi_bao"):
                cau_hoi = result.output["can_hoi_them_nguoi_bao"]
                ly_do_chan = followup.block_reason(ticket_id, cau_hoi)
                if ly_do_chan:
            # Guard: đã hỏi ý này rồi (và đã được trả lời), hoặc đã hỏi quá số vòng cho
            # phép. Bỏ câu hỏi khỏi output luôn, nếu không Lễ tân vẫn đem nó đi hỏi lại.
                    result.output["can_hoi_them_nguoi_bao"] = None
                    bus.emit(
                        ticket_id,
                        "guard_triggered",
                        {"guard": "khong_hoi_lai_nguoi_bao", "agent_id": agent["id"],
                         "cau_hoi": cau_hoi, "ly_do": ly_do_chan,
                         "xu_ly": "bỏ câu hỏi, buộc kết luận với thông tin đang có"},
                        actor=agent["id"],
                    )
                else:
                    # Xem ghi chú tương ứng trong maf_room.py: ghim câu hỏi lại và chạy tiếp,
                    # để một bộ phận cần hỏi lại không làm các bộ phận còn việc mất lượt.
                    pending_questions.append(cau_hoi)
                    bus.emit(
                        ticket_id,
                        "guard_triggered",
                        {"guard": "can_hoi_nguoi_bao", "agent_id": agent["id"],
                         "cau_hoi": cau_hoi,
                         "xu_ly": "ghim câu hỏi, chạy tiếp các bộ phận còn việc"},
                        actor=agent["id"],
                    )

        if turns >= max_turns and not stop_reason:
            bus.emit(
                ticket_id,
                "guard_triggered",
                {"guard": "vuot_so_luot_toi_da", "max_turns": max_turns, "xu_ly": "buộc kết thúc"},
                actor="dieu_phoi",
            )
            stop_reason = f"Đã dùng hết {max_turns} lượt"

        # ---------------------------------------------------------- wrap-up
        pending = self._pending_actions(ticket_id)
        if pending_questions:
            followup.pin(ticket_id, pending_questions)
            final_status = "cho_cu_dan"
        elif pending:
            final_status = "cho_duyet"
        else:
            final_status = "hoan_tat"
        self._set_status(ticket_id, final_status)

        bus.emit(
            ticket_id,
            "room_end",
            {
                "so_luot": turns,
                "ly_do_ket_thuc": stop_reason,
                "trang_thai_ticket": final_status,
                "so_hanh_dong_cho_duyet": len(pending),
            },
            actor="dieu_phoi",
        )

        ticket = ticket_to_dict(ticket_id)
        cau_hoi = "\n".join(pending_questions)
        reply = self.receptionist.summarize(
            ticket,
            transcript,
            pending_actions=pending,
            extra_note=f"Cần hỏi thêm người báo: {cau_hoi}" if cau_hoi else "",
        )
        return {
            "ticket_id": ticket_id,
            "so_luot": turns,
            "ly_do_ket_thuc": stop_reason,
            "trang_thai": final_status,
            "transcript": transcript,
            "tin_nhan_gui_nguoi_bao": reply,
            "hanh_dong_cho_duyet": pending,
        }


def run_room(ticket_id: str, extra_agent_id: str | None = None) -> dict[str, Any]:
    """Entry point used by background tasks; never raises into the caller."""
    try:
        return Room().run(ticket_id, extra_agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Phòng họp lỗi cho ticket=%s", ticket_id)
        bus.emit(ticket_id, "room_end", {"loi": str(exc)[:300], "ly_do_ket_thuc": "lỗi hệ thống"}, actor="dieu_phoi")
        return {"ticket_id": ticket_id, "loi": str(exc)[:300]}


def run_room_dispatch(ticket_id: str, extra_agent_id: str | None = None) -> dict[str, Any]:
    """Pick the room engine configured in .env (ROOM_ENGINE=maf | builtin)."""
    from backend.app.config import settings

    # Cửa chung của cả hai engine, nên gắn ticket_id ở đây là mọi lệnh gọi LLM bên trong
    # phiên — Điều phối lẫn từng agent — đều ghép lại được thành một phiên trong trace.
    with trace.scope(ticket_id=ticket_id):
        if settings.room_engine == "maf":
            from backend.core.maf_room import run_room_maf

            return run_room_maf(ticket_id, extra_agent_id)
        return run_room(ticket_id, extra_agent_id)
