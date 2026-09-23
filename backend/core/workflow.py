"""Trạng thái quy trình xác nhận của một phản ánh.

Quy trình (ai phải xác nhận, theo thứ tự nào) là DỮ LIỆU của domain pack
(`quy_trinh_xac_nhan` trong domain.yaml), không phải luật viết cứng trong lõi:
lõi chỉ biết "bước này xong khi có một hành động đã thực thi của một trong các
tool thuộc bước đó". Thêm một bên mới = thêm tool vào đúng bước, không sửa code.

Chỉ tính là quy trình ĐÃ BẮT ĐẦU khi có ít nhất một bước xong. Một phản ánh chỉ
hỏi thông tin (thắc mắc phí, hỏi nội quy) không bao giờ bước vào quy trình này,
nên không bị chặn đóng phòng.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import select

from backend.app.db import session_scope
from backend.app.models import Action
from backend.domain.loader import DomainPack, load_domain


def _actions(ticket_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.exec(select(Action).where(Action.ticket_id == ticket_id)).all()
        return [{"id": r.id, "tool": r.tool, "status": r.status, "vai_tro": r.approver_role or "bql",
                 "created_at": r.created_at.isoformat()} for r in rows]


def chain_state(ticket_id: str, domain: DomainPack | None = None) -> list[dict[str, Any]]:
    """Từng bước kèm trạng thái: xong / đang chờ duyệt / chưa tới."""
    domain = domain or load_domain()
    acts = _actions(ticket_id)
    out: list[dict[str, Any]] = []
    for step in domain.quy_trinh_xac_nhan:
        mine = [a for a in acts if a["tool"] in step.tools]
        done = next((a for a in mine if a["status"] == "da_thuc_hien"), None)
        pending = next((a for a in mine if a["status"] in ("cho_duyet", "da_duyet")), None)
        rejected = next((a for a in mine if a["status"] == "tu_choi"), None)
        out.append({
            "id": step.id, "label": step.label, "vai_tro": step.vai_tro,
            "vai_tro_label": domain.role_label(step.vai_tro),
            "tools": step.tools,
            "trang_thai": "xong" if done else "cho_duyet" if pending else "tu_choi" if rejected else "chua_toi",
            "action_id": (done or pending or rejected or {}).get("id", ""),
            "tool_da_dung": (done or pending or rejected or {}).get("tool", ""),
        })
    return out


def started(state: list[dict[str, Any]]) -> bool:
    return any(s["trang_thai"] in ("xong", "cho_duyet") for s in state)


def unfinished(state: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Các bước còn thiếu — chỉ có nghĩa khi quy trình đã bắt đầu."""
    return [s for s in state if s["trang_thai"] != "xong"]


def blocking_note(ticket_id: str, domain: DomainPack | None = None) -> str:
    """Câu nhắc cho Điều phối khi nó định kết thúc giữa chừng. Rỗng = được phép kết thúc."""
    domain = domain or load_domain()
    state = chain_state(ticket_id, domain)
    if not started(state):
        return ""
    missing = unfinished(state)
    if not missing:
        return ""
    buoc = missing[0]
    return (f"Quy trình xác nhận của phản ánh này đã bắt đầu nhưng chưa xong: còn "
            f"{len(missing)} bước, gần nhất là \"{buoc['label']}\" ({buoc['vai_tro_label']}). "
            f"Hãy gọi bộ phận phụ trách để làm bước đó bằng một trong các tool: "
            f"{', '.join(buoc['tools'])}. Chỉ kết thúc khi mọi bước đã xong hoặc thực sự bế tắc.")
