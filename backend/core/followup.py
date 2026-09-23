"""Hỏi lại người báo: ghép cặp hỏi–đáp và chặn vòng lặp hỏi mãi một chuyện.

Lỗi thật đã gặp (vé TK-3656061B, 2026-09-23): phòng họp hỏi người báo một câu,
người báo trả lời, phiên sau agent lại hỏi tiếp một câu tương tự, phòng đóng sau
đúng MỘT lượt, và vòng đó lặp ba lần liền. Hai nguyên nhân:

  1. Câu trả lời được cất vào `thong_tin_bo_sung` dưới dạng danh sách văn bản rời,
     không gắn với câu hỏi nào. Lượt sau agent nhìn vào ticket không thể biết nó
     ĐÃ hỏi gì và đã được trả lời ra sao, nên hỏi tiếp.
  2. Không có gì đếm số vòng. Một vé có thể bật qua lại `cho_cu_dan` vô hạn.

Module này giữ cả hai đầu: ghim câu hỏi lúc phòng đóng, ghép nó với câu trả lời
lúc người báo nhắn lại, và trả lời câu "có được hỏi thêm nữa không".
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.models import Ticket

logger = logging.getLogger("followup")

PENDING_KEY = "cau_hoi_dang_cho"      # câu hỏi phòng họp vừa ghim, chờ người báo trả lời
HISTORY_KEY = "hoi_dap"               # [{hoi, dap, luc}] đã khép kín


# Từ lịch sự/hư từ hay gặp trong câu hỏi của agent — bỏ đi thì phần còn lại mới là nội dung.
_STOP = set("""kinh thua quy cu dan vui long xin anh chi ban quan ly cho biet them cua
de duoc nay cac mot hai toi chung hay khong phai the nao neu con nhu tai voi sau truoc
tren duoi vao bang theo tu den moi khi da dang cung lam nhung nen sự viec hoi tra loi giup
nhe ma thi xac nhan vien""".split())


def _norm(text: str) -> set[str]:
    """Bỏ dấu, bỏ hư từ, còn lại tập từ nội dung."""
    s = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return {w for w in re.split(r"[^a-z0-9]+", s) if len(w) > 2 and w not in _STOP}


def is_repeat(a: str, b: str, nguong: float = 0.8) -> bool:
    """Hai câu hỏi gần như trùng NGUYÊN VĂN hay không.

    Ngưỡng để cao có chủ đích. Đã đo trên ba câu hỏi thật của vé lỗi: so khớp theo
    từ khóa KHÔNG tách được "hỏi lại đúng ý cũ" với "hỏi một ý khác về cùng cái máy
    giặt" — cặp trùng ý đạt overlap 0.50 trong khi một cặp khác ý lại đạt 0.75. Hạ
    ngưỡng để bắt được cặp trùng ý sẽ chặn nhầm câu hỏi chính đáng, nên ở đây chỉ
    bắt trường hợp chắc chắn: câu hỏi lặp lại gần như y hệt. Việc chặn "trùng ý" do
    hai thứ khác lo: trần số vòng (luật xác định, không đoán) và khối ĐÃ HỎI NGƯỜI
    BÁO bơm thẳng câu trả lời vào TICKET cho agent đọc.
    """
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= nguong


def _fields(ticket_id: str) -> dict[str, Any]:
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        return dict(t.fields or {}) if t else {}


def _write_fields(ticket_id: str, fields: dict[str, Any]) -> None:
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            return
        t.fields = fields
        t.updated_at = datetime.utcnow()
        s.add(t)


def history(ticket_id: str) -> list[dict[str, str]]:
    rows = _fields(ticket_id).get(HISTORY_KEY) or []
    return [r for r in rows if isinstance(r, dict)]


def rounds(ticket_id: str) -> int:
    """Số lần người báo ĐÃ được hỏi (kể cả câu đang chờ trả lời)."""
    f = _fields(ticket_id)
    return len([r for r in (f.get(HISTORY_KEY) or []) if isinstance(r, dict)]) + \
        len(f.get(PENDING_KEY) or [])


def pin(ticket_id: str, questions: list[str]) -> None:
    """Ghim câu hỏi lúc phòng họp đóng lại chờ người báo."""
    questions = [q for q in questions if str(q).strip()]
    if not ticket_id or not questions:
        return
    fields = _fields(ticket_id)
    fields[PENDING_KEY] = questions
    _write_fields(ticket_id, fields)


def fold_answer(fields: dict[str, Any], answer: str) -> dict[str, Any]:
    """Ghép câu trả lời với câu hỏi đang chờ. Trả về `fields` đã cập nhật.

    Nhận/trả dict thay vì tự ghi DB để `Receptionist.add_followup` gộp chung một
    transaction với những thay đổi khác của nó.
    """
    pending = [q for q in (fields.get(PENDING_KEY) or []) if str(q).strip()]
    rows = [r for r in (fields.get(HISTORY_KEY) or []) if isinstance(r, dict)]
    luc = datetime.utcnow().isoformat(timespec="seconds")
    if pending:
        rows.append({"hoi": "\n".join(pending), "dap": answer, "luc": luc})
    else:
        # Người báo nhắn thêm khi không có câu hỏi nào đang chờ: vẫn giữ lại,
        # ghi rõ là thông tin tự bổ sung chứ không phải trả lời câu nào.
        rows.append({"hoi": "(người báo tự bổ sung, không phải trả lời câu hỏi)", "dap": answer, "luc": luc})
    fields[HISTORY_KEY] = rows
    fields[PENDING_KEY] = []
    return fields


def block_reason(ticket_id: str, question: str) -> str:
    """Có được hỏi người báo câu này nữa không. Rỗng = được hỏi.

    Đây là guard ở tầng code, không phải lời nhắc trong prompt: model nhỏ vẫn hỏi
    lại dù prompt đã cấm, và mỗi vòng hỏi lại tốn của người báo một lượt chờ.
    """
    if not ticket_id or not str(question).strip():
        return ""
    for r in history(ticket_id):
        if is_repeat(question, r.get("hoi", "")):
            return (f"Câu hỏi này lặp lại gần như y hệt câu đã hỏi và người báo ĐÃ trả lời: "
                    f"\"{str(r.get('dap'))[:160]}\"")
    if rounds(ticket_id) >= settings.max_followup_rounds:
        return (f"Người báo đã được hỏi {rounds(ticket_id)} lần (trần "
                f"{settings.max_followup_rounds}); phải kết luận với thông tin đang có.")
    return ""


def format_history(ticket_id: str) -> str:
    rows = history(ticket_id)
    if not rows:
        return ""
    return "\n".join(f"  · đã hỏi: {r.get('hoi', '')}\n    người báo đáp: {r.get('dap', '')}" for r in rows)
