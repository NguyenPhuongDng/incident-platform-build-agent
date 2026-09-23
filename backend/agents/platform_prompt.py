"""The fixed platform layer of a specialist agent's prompt.

Domain-specific wording comes from the domain pack; nothing about any particular
customer or industry may appear in this file.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from backend.domain.loader import DomainPack

logger = logging.getLogger("platform_prompt")

OUTPUT_SCHEMA = (
    '{"ket_luan": str, "da_thuc_hien": [str], "de_xuat": [str], '
    '"can_them_agent": [str], "can_hoi_them_nguoi_bao": str|null, "nguon": [str]}'
)

PLATFORM_TEMPLATE = """Bạn là "{display_name}", một thành viên trong phòng họp xử lý phản ánh của {org_name}.

LUẬT PHÒNG HỌP (ưu tiên cao nhất; nếu phần NHIỆM VỤ NGHIỆP VỤ bên dưới mâu thuẫn với luật này, làm theo luật này):
1. Bạn KHÔNG nói chuyện trực tiếp với {audience}. Mọi phản hồi tới {audience} do Lễ tân đảm nhận.
2. Chỉ xử lý phần việc thuộc năng lực của bạn: {capability}. Phần thuộc bộ phận khác thì ghi vào "can_them_agent", không tự xử lý.
3. Cần dữ liệu thì gọi tool. Tuyệt đối không bịa số liệu, mã phiếu, lịch hẹn hay số tiền.
4. Tool cần duyệt sẽ dừng lượt của bạn lại chờ quản lý quyết định, rồi trả về "trang_thai_duyet":
   - "da_duyet": hành động ĐÃ chạy thật, dữ liệu thật nằm trong "ket_qua" — dùng nó để kết luận.
   - "tu_choi": quản lý KHÔNG đồng ý, hành động không hề xảy ra. Không được nói là đã làm; nêu
     phương án thay thế hoặc ghi rõ việc này bị từ chối.
   - "cho_duyet" (hoặc trạng thái "cho_duyet" cũ): chưa có quyết định, ghi nhận là ĐANG CHỜ DUYỆT.
   Mọi trường hợp: KHÔNG gọi lại tool đó trong cùng lượt.
5. Nếu dùng TÀI LIỆU THAM KHẢO, ghi tên file vào "nguon". Nếu tài liệu không có thông tin cần thiết, nói rõ là không tìm thấy, không suy đoán.
6. Thiếu thông tin chỉ người báo mới cung cấp được: điền câu hỏi vào "can_hoi_them_nguoi_bao".
   NHƯNG nếu ý đó đã nằm trong mục "ĐÃ HỎI NGƯỜI BÁO VÀ ĐÃ CÓ CÂU TRẢ LỜI" của TICKET thì KHÔNG
   được hỏi lại: dùng chính câu trả lời đó mà kết luận. Người báo đã trả lời mà vẫn bị hỏi lại là
   lỗi nặng nhất trong phòng họp này. Thiếu thông tin không sống còn thì nêu giả định rồi xử lý
   tiếp, đừng dừng lại để hỏi.
7. Kết thúc lượt bằng DUY NHẤT một JSON đúng schema:
{output_schema}

NHIỆM VỤ NGHIỆP VỤ (do quản lý định nghĩa):
{business_prompt}

TÀI LIỆU THAM KHẢO:
{rag_block}

TICKET:
{ticket_block}

DIỄN BIẾN PHÒNG HỌP ĐẾN LÚC NÀY:
{transcript_block}

CHỈ DẪN CỦA ĐIỀU PHỐI CHO LƯỢT NÀY:
{instruction}"""


def format_rag(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "(không có)"
    return "\n\n".join(
        f"[{h.get('filename', 'tài liệu')}] (độ gần: {h.get('khoang_cach')})\n{h.get('noi_dung', '')}"
        for h in hits
    )


def format_ticket(ticket: dict[str, Any]) -> str:
    fields = ticket.get("fields") or {}
    lines = [f"- ma_phan_anh: {ticket.get('id', '')}", f"- uu_tien: {ticket.get('priority', '')}"]
    # Several tools take a date (lịch kỹ thuật viên, nhật ký camera) but the model has
    # no clock: left to guess, it picks a date near its training cutoff, the lookup comes
    # back empty, and the agent then improvises around data that does exist.
    if ticket.get("created_at"):
        lines.append(f"- ngay_hom_nay: {str(ticket['created_at'])[:10]}")
    if ticket.get("summary"):
        lines.append(f"- tom_tat: {ticket['summary']}")
    hoi_dap = [r for r in (fields.get("hoi_dap") or []) if isinstance(r, dict)]
    for k, v in fields.items():
        if k in ("hoi_dap", "cau_hoi_dang_cho"):
            continue          # in riêng bên dưới cho dễ đọc, xem khối ĐÃ HỎI NGƯỜI BÁO
        lines.append(f"- {k}: {v}")
    if hoi_dap:
        lines.append("- ĐÃ HỎI NGƯỜI BÁO VÀ ĐÃ CÓ CÂU TRẢ LỜI (TUYỆT ĐỐI KHÔNG hỏi lại những ý này):")
        for r in hoi_dap:
            lines.append(f"    · đã hỏi: {r.get('hoi', '')}")
            lines.append(f"      người báo đáp: {r.get('dap', '')}")
    return "\n".join(lines)


def format_transcript(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return "(bạn là thành viên đầu tiên phát biểu)"
    blocks = []
    for t in turns:
        out = t.get("output") or {}
        blocks.append(
            f"[{t.get('display_name', t.get('agent_id', ''))}]\n"
            f"- kết luận: {out.get('ket_luan', '')}\n"
            f"- đã thực hiện: {out.get('da_thuc_hien') or '(không)'}\n"
            f"- đề xuất: {out.get('de_xuat') or '(không)'}\n"
            f"- cần thêm bộ phận: {out.get('can_them_agent') or '(không)'}"
        )
    return "\n\n".join(blocks)


def build_system_prompt(
    *,
    display_name: str,
    capability: str,
    business_prompt: str,
    domain: DomainPack,
    rag_hits: list[dict[str, Any]],
    ticket: dict[str, Any],
    transcript: list[dict[str, Any]],
    instruction: str,
) -> str:
    return PLATFORM_TEMPLATE.format(
        display_name=display_name,
        org_name=domain.display_name,
        audience=domain.audience,
        capability=capability,
        output_schema=OUTPUT_SCHEMA,
        business_prompt=business_prompt.strip() or "(quản lý chưa mô tả thêm)",
        rag_block=format_rag(rag_hits),
        ticket_block=format_ticket(ticket),
        transcript_block=format_transcript(transcript),
        instruction=instruction.strip() or "(không có chỉ dẫn riêng, hãy xử lý theo năng lực của bạn)",
    )


def empty_output(ket_luan: str = "") -> dict[str, Any]:
    return {
        "ket_luan": ket_luan,
        "da_thuc_hien": [],
        "de_xuat": [],
        "can_them_agent": [],
        "can_hoi_them_nguoi_bao": None,
        "nguon": [],
    }


def _live_agent_ids() -> set[str]:
    from backend.agents import registry  # local import: registry pulls in the DB layer

    return {a["id"] for a in registry.list_agents()}


def normalize_output(raw: Any, *, self_id: str = "") -> dict[str, Any]:
    """Coerce whatever the model returned into the agreed schema.

    `self_id` drops the agent from its own `can_them_agent`: models sometimes name
    themselves, which only adds noise to the orchestrator's next decision.

    Ids that no longer exist in the registry are dropped too. Builder renames and
    deletes agents at runtime, so a peer's business prompt can keep naming a dead
    agent; handed that id, the orchestrator does not refuse it — it fuzzy-matches
    the closest live agent and calls it, dragging a wrong department into the room.
    A stale handoff must become no handoff, not the wrong one.
    """
    if isinstance(raw, str):
        return empty_output(raw.strip())
    if not isinstance(raw, dict):
        return empty_output(json.dumps(raw, ensure_ascii=False))

    def as_list(v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        return [str(v)] if str(v).strip() else []

    hoi_them = raw.get("can_hoi_them_nguoi_bao")
    if isinstance(hoi_them, str) and not hoi_them.strip():
        hoi_them = None
    if isinstance(hoi_them, list):
        hoi_them = " ".join(str(x) for x in hoi_them) or None

    suggested = [a for a in as_list(raw.get("can_them_agent")) if a != self_id]
    live = _live_agent_ids()
    stale = [a for a in suggested if a not in live]
    if stale:
        logger.warning(
            "agent=%s đề xuất bàn giao cho agent không còn tồn tại: %s — đã bỏ qua",
            self_id or "(không rõ)", stale,
        )

    return {
        "ket_luan": str(raw.get("ket_luan") or "").strip(),
        "da_thuc_hien": as_list(raw.get("da_thuc_hien")),
        "de_xuat": as_list(raw.get("de_xuat")),
        "can_them_agent": [a for a in suggested if a in live],
        "can_hoi_them_nguoi_bao": hoi_them,
        "nguon": as_list(raw.get("nguon")),
    }
