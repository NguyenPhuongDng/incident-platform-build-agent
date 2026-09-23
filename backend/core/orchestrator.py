"""Orchestrator: picks who speaks next. It never solves anything itself.

Domain-neutral by construction: the member list comes from the registry at call
time and every organisation-specific word comes from the domain pack.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from backend.app.config import settings
from backend.domain.loader import DomainPack, load_domain
from backend.llm import qwen_client as llm

logger = logging.getLogger("orchestrator")

ROUTER_ROLE = "dieu_phoi"

ROUTER_TEMPLATE = """Bạn là Điều phối của phòng họp xử lý phản ánh cho {org_name}. Bạn không tự giải quyết vấn đề; bạn chỉ quyết định ai nói tiếp.
Các thành viên hiện có (chỉ được chọn trong danh sách này):
{registry}
Nguyên tắc:
- Chọn thành viên có năng lực phù hợp nhất với phần việc CHƯA được giải quyết.
- Khi thành viên vừa phát biểu ghi tên ai đó vào "can_them_agent" và người đó còn nằm trong danh
  sách chọn được, bạn PHẢI gọi người đó trước khi kết thúc. Đó là một phần việc chưa ai làm.
- Phần việc chưa xong KHÔNG chỉ là điều người báo hỏi. Mô tả năng lực ở trên là nguồn duy nhất về quy
  trình: nếu năng lực của một thành viên nói rằng họ có một bước phải làm SAU KHI thành viên khác làm
  xong bước của mình (ví dụ "chốt chi phí sau khi kỹ thuật xác nhận vật tư"), thì bước đó vẫn chưa xong
  kể cả khi người báo không hề nhắc tới.
- Không gọi lại một thành viên nếu không có thông tin mới cho họ.
- Trước khi kết thúc, đối chiếu diễn biến với năng lực của TỪNG thành viên còn chọn được: ai có bước đã
  đến lượt mà chưa làm thì phải gọi họ. Việc không ai ghi "can_them_agent" KHÔNG phải là căn cứ để kết thúc.
- Kết thúc khi mọi khía cạnh của ticket đã có kết luận, hoặc cần hỏi thêm người báo, hoặc không còn thành viên phù hợp.
- "chi_dan" phải cụ thể, nói rõ thành viên cần làm gì ở lượt này.
Chỉ trả về JSON đúng schema: {{"hanh_dong": "goi_agent|ket_thuc", "agent_id": "...", "chi_dan": "...", "ly_do": "..."}}"""

STATE_TEMPLATE = """TICKET:
{ticket_block}

DIỄN BIẾN ĐẾN LÚC NÀY:
{transcript_block}

ĐỀ XUẤT GỌI THÊM BỘ PHẬN ĐANG CHỜ: {pending_suggestions}
HÀNH ĐỘNG ĐANG CHỜ DUYỆT: {pending_actions}
SỐ LƯỢT ĐÃ DÙNG: {turns_used}/{max_turns}
{extra_note}
Quyết định lượt tiếp theo."""


@dataclass
class RouterDecision:
    hanh_dong: str
    agent_id: str = ""
    chi_dan: str = ""
    ly_do: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"hanh_dong": self.hanh_dong, "agent_id": self.agent_id,
                "chi_dan": self.chi_dan, "ly_do": self.ly_do}


class Orchestrator:
    def __init__(self, domain: DomainPack | None = None) -> None:
        self.domain = domain or load_domain()

    @staticmethod
    def _registry_block(members: list[dict[str, Any]]) -> str:
        if not members:
            return "(không có thành viên nào đang hoạt động)"
        return "\n".join(f"- {m['id']} | {m['display_name']}: {m['capability']}" for m in members)

    @staticmethod
    def _ticket_block(ticket: dict[str, Any]) -> str:
        lines = [f"- uu_tien: {ticket.get('priority', '')}"]
        if ticket.get("summary"):
            lines.append(f"- tom_tat: {ticket['summary']}")
        for k, v in (ticket.get("fields") or {}).items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    @staticmethod
    def _transcript_block(turns: list[dict[str, Any]]) -> str:
        if not turns:
            return "(chưa có ai phát biểu)"
        blocks = []
        for t in turns:
            out = t.get("output") or {}
            blocks.append(
                f"[{t.get('display_name', t.get('agent_id'))}] kết luận: {out.get('ket_luan', '')}\n"
                f"  đã thực hiện: {out.get('da_thuc_hien') or '(không)'}\n"
                f"  đề xuất: {out.get('de_xuat') or '(không)'}\n"
                f"  cần thêm bộ phận: {out.get('can_them_agent') or '(không)'}"
            )
        return "\n".join(blocks)

    def decide(
        self,
        *,
        ticket: dict[str, Any],
        members: list[dict[str, Any]],
        transcript: list[dict[str, Any]],
        pending_suggestions: list[str],
        pending_actions: list[str],
        turns_used: int,
        max_turns: int,
        extra_note: str = "",
    ) -> RouterDecision:
        system = ROUTER_TEMPLATE.format(
            org_name=self.domain.display_name,
            registry=self._registry_block(members),
        )
        user = STATE_TEMPLATE.format(
            ticket_block=self._ticket_block(ticket),
            transcript_block=self._transcript_block(transcript),
            pending_suggestions=", ".join(pending_suggestions) or "(không)",
            pending_actions=", ".join(pending_actions) or "(không)",
            turns_used=turns_used,
            max_turns=max_turns,
            extra_note=f"\nLƯU Ý: {extra_note}\n" if extra_note else "",
        )
        try:
            raw = llm.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                role=ROUTER_ROLE,
                model=settings.router_model,
            )
        except llm.LLMError as exc:
            logger.warning("Điều phối lỗi, buộc kết thúc: %s", exc)
            return RouterDecision("ket_thuc", ly_do=f"Lỗi khi gọi Điều phối: {exc}")

        if not isinstance(raw, dict):
            return RouterDecision("ket_thuc", ly_do="Điều phối trả về dữ liệu không hợp lệ")
        action = str(raw.get("hanh_dong") or "").strip()
        if action not in {"goi_agent", "ket_thuc"}:
            action = "goi_agent" if raw.get("agent_id") else "ket_thuc"
        return RouterDecision(
            hanh_dong=action,
            agent_id=str(raw.get("agent_id") or "").strip(),
            chi_dan=str(raw.get("chi_dan") or "").strip(),
            ly_do=str(raw.get("ly_do") or "").strip(),
        )
