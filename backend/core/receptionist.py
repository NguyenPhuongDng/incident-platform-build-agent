"""Receptionist: the only member that talks to the person who filed the report.

Mode A — intake: collect the fields the domain pack declares, then open a ticket.
Mode B — wrap-up: turn the room's internal output into one message for that person.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlmodel import select

from backend.app import trace
from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.events import bus
from backend.app.models import ChatMessage, Ticket
from backend.core import followup
from backend.domain.loader import DomainPack, load_domain
from backend.knowledge import store
from backend.llm import qwen_client as llm

logger = logging.getLogger("receptionist")

ROLE = "le_tan"

INTAKE_TEMPLATE = """Bạn là Lễ tân của {org_name}, tiếp nhận tin nhắn từ {audience}. Xưng "{self_reference}", gọi người dùng là "{honorific}".

Trước tiên, phân loại tin nhắn mới nhất vào "loai":
- "hoi_thong_tin": câu hỏi về THÔNG TIN CHUNG, áp dụng như nhau cho mọi người, không cần tra cứu
  riêng cho người hỏi (ví dụ: giờ làm việc, hotline, lịch thu rác, quy định, công thức tính phí
  nói chung).
- "phan_anh": báo sự cố, khiếu nại, hoặc bất kỳ câu hỏi nào nhắc đến tình huống RIÊNG của người
  hỏi và cần tra cứu dữ liệu cá nhân của họ mới trả lời đúng được — ví dụ "phí CỦA TÔI sao tăng",
  "hóa đơn CỦA TÔI sao cao vậy", "chỗ TÔI ở bị...". Loại này LUÔN LÀ "phan_anh" dù câu chữ nghe
  giống một câu hỏi, vì trả lời đúng cần xem đúng dữ liệu của riêng người đó, không phải chính
  sách chung. Khi không chắc, chọn "phan_anh".

Nếu "loai" là "hoi_thong_tin" VÀ tài liệu tham khảo bên dưới có câu trả lời: trả lời thẳng trong
"tra_loi", ghi tên tệp đã dùng vào "nguon". Không tạo phản ánh cho trường hợp này.
Nếu tài liệu tham khảo KHÔNG đủ trả lời, hoặc đây là "phan_anh": xử lý như tiếp nhận phản ánh —
thu thập đủ các trường sau, hỏi tự nhiên, mỗi lượt tối đa 1–2 câu hỏi:
{intake_spec}
Quy tắc ưu tiên: {priority_rules}
Không hứa hẹn thời gian xử lý hay kết quả. Không tự giải quyết vấn đề kỹ thuật.

CÁCH HỎI (quan trọng, sai ở đây là người báo bỏ đi):
- TRƯỚC KHI HỎI: điền vào "truong_da_co" mọi trường đã suy ra được từ lời người báo — dù họ diễn
  đạt không trùng nhãn trường — và từ THÔNG TIN NGƯỜI BÁO bên dưới. Chỉ hỏi phần còn thiếu thật.
- Trường nào người báo đã nói tới, dù rất ngắn, thì COI NHƯ ĐÃ CÓ: ghi lại đúng lời họ vào
  "truong_da_co", không đòi họ diễn đạt chi tiết hơn. Chỉ hỏi thêm khi thiếu nó thì không bộ phận
  nào xử lý nổi.
- "truong_da_co" phải chứa CẢ những trường đã thu thập ở các lượt trước (xem khối ĐÃ THU THẬP
  ĐƯỢC), không được bỏ bớt.
- Người báo nói sự cố nằm ở chỗ của chính họ thì trường vị trí COI NHƯ ĐÃ CÓ: hệ thống đã biết mã
  đối tượng của họ, ghi luôn vào "truong_da_co", không hỏi lại.
- Không bao giờ hỏi thứ đã có trong THÔNG TIN NGƯỜI BÁO (tên, mã, số điện thoại, nơi ở…).
- Không hỏi chi tiết hiển nhiên hoặc không làm thay đổi cách xử lý (ví dụ chỗ đứng của một thiết bị
  gắn cố định). Hỏi kiểu đó chỉ làm người báo thấy bị tra khảo.
- Có hỏi thì câu hỏi phải nằm NGUYÊN VĂN trong "tra_loi". TUYỆT ĐỐI không viết "vui lòng cung cấp
  thêm thông tin" rồi để trống: "tra_loi" không được kết thúc bằng dấu hai chấm, không dùng markdown
  hay danh sách gạch đầu dòng.
- Trường không bắt buộc chỉ được hỏi TỐI ĐA MỘT LẦN; người dùng không nêu thì bỏ qua.
Ngay khi đã có đủ mọi trường BẮT BUỘC, đặt du_thong_tin = true, "tra_loi" KHÔNG được chứa câu hỏi
nào nữa, chỉ thông báo đã tiếp nhận và đang chuyển bộ phận liên quan.

{requester_block}
{collected_block}

TÀI LIỆU THAM KHẢO (chỉ tài liệu dùng chung toàn domain):
{faq_block}

Chỉ trả về JSON đúng schema: {{"loai": "hoi_thong_tin|phan_anh", "tra_loi": str, "nguon": [str], "truong_da_co": object, "du_thong_tin": bool, "uu_tien": "KHAN_CAP|BINH_THUONG|THAP", "tom_tat": str}}
Trong "truong_da_co", key là mã trường ({field_keys}), value là nội dung đã thu thập được cho tới lúc này."""

SUMMARY_TEMPLATE = """Bạn là Lễ tân của {org_name}. Các bộ phận nội bộ đã trao đổi xong về phản ánh này.
Hãy soạn MỘT tin nhắn gửi {audience}. Xưng "{self_reference}", gọi người nhận là "{honorific}".
Ràng buộc bắt buộc:
- Không nêu tên bộ phận nội bộ, tên agent, tên tool, mã hành động nội bộ hay chi tiết kỹ thuật nội bộ.
- Mã phiếu, mã yêu cầu, khung giờ hẹn, số tiền: chỉ nêu nếu nội dung nội bộ có ghi rõ; tuyệt đối không bịa.
- Việc đang chờ quản lý duyệt thì diễn đạt là "đang được xem xét", không hứa kết quả.
- Nếu có câu cần hỏi thêm, đặt câu hỏi đó ở cuối tin nhắn, ngắn gọn.
- Giọng điệu lịch sự, ngắn gọn, tối đa khoảng 150 từ, không dùng markdown.
Chỉ trả về JSON: {{"tin_nhan": str, "co_cau_hoi": bool}}"""


class Receptionist:
    def __init__(self, domain: DomainPack | None = None) -> None:
        self.domain = domain or load_domain()

    # --------------------------------------------------------------- prompt bits
    @staticmethod
    def _requester_block(requester_id: str, subject_id: str,
                         profile: dict[str, Any] | None) -> str:
        """Những gì hệ thống ĐÃ biết về người báo, để Lễ tân không hỏi lại.

        Cố tình nhận một dict tự do do tầng API truyền vào chứ không tự đi đọc mock
        data: lõi không được biết tên tệp hay tên trường của một domain cụ thể.
        """
        rows: list[tuple[str, Any]] = [("ma_nguoi_bao", requester_id),
                                       ("ma_doi_tuong_lien_quan", subject_id)]
        seen = {v for _, v in rows if v}
        for k, v in (profile or {}).items():
            if str(v).strip() and v not in seen:
                rows.append((str(k), v))
                seen.add(v)
        lines = [f"- {k}: {v}" for k, v in rows if str(v).strip()]
        if not lines:
            return ""
        return ("THÔNG TIN NGƯỜI BÁO — hệ thống đã biết sẵn, TUYỆT ĐỐI KHÔNG hỏi lại:\n"
                + "\n".join(lines))

    # Dấu hiệu một tin nhắn vẫn đang xin thêm thông tin. Tiếng Việt là ngôn ngữ của
    # platform (mọi prompt lõi đều tiếng Việt), nên dò từ khóa ở đây không làm lõi dính
    # vào domain nào; chỉ có "vui lòng"/"cho biết" là chung cho mọi khách hàng.
    _DAU_HIEU_XIN_THEM = ("?", "vui lòng", "cho biết", "cung cấp", "bổ sung", "xác nhận")

    @classmethod
    def _dang_xin_them(cls, reply: str) -> bool:
        low = reply.lower()
        return reply.rstrip().endswith(":") or any(d in low for d in cls._DAU_HIEU_XIN_THEM)

    def _ensure_questions(self, reply: str, missing: list[str]) -> str:
        """Guard: model hứa "cung cấp thêm thông tin" rồi không hỏi gì — hội thoại tắc.

        Đã gặp thật (trace 2026-09-23): tin nhắn kết thúc bằng dấu hai chấm, không một
        câu hỏi nào, người báo không biết phải trả lời gì. Câu hỏi bù được dựng từ nhãn
        trường trong domain pack nên vẫn không có chữ nào của domain nằm trong lõi.

        Cố tình hẹp: chỉ chữa khi tin nhắn thực sự bế tắc (cụt ở dấu hai chấm, hoặc
        không có dấu hiệu nào là đang hỏi). Một câu nhờ ở thể mệnh lệnh — "Vui lòng mô
        tả giúp tình trạng…" — là câu hỏi hợp lệ, chèn thêm vào chỉ thành lải nhải.
        """
        text = reply.rstrip()
        if not missing:
            return reply
        if not text.endswith(":") and self._dang_xin_them(text):
            return reply
        labels = [f.label for f in self.domain.intake_fields if f.key in missing]
        if not labels:
            return reply
        logger.warning("Lễ tân không hỏi gì dù còn thiếu %s — bù câu hỏi từ domain pack",
                       ", ".join(missing))
        text = text.rstrip(":.").rstrip()
        joined = "; ".join(labels)
        return f"{text}. {self.domain.honorific.capitalize()} cho biết thêm giúp: {joined}?"

    def _closing_reply(self, reply: str) -> str:
        """Guard: đã đủ trường để mở phản ánh mà tin nhắn vẫn đang hỏi tiếp.

        Cũng đã gặp thật: model vừa điền đủ trường, vừa nhắc "vui lòng mô tả chi tiết"
        — người báo trả lời một câu hỏi đã hết giá trị, trong lúc phòng họp đã chạy.
        Code đã là chỗ quyết định lúc nào tiếp nhận xong, nên câu chốt cũng thuộc về code.
        """
        if not self._dang_xin_them(reply):
            return reply
        logger.warning("Lễ tân vẫn hỏi thêm dù đã đủ trường — thay bằng câu chốt tiếp nhận")
        return (f"{self.domain.self_reference} đã ghi nhận đầy đủ thông tin của "
                f"{self.domain.honorific} và đang chuyển bộ phận liên quan xử lý.")

    # --------------------------------------------------------------- mode A
    def _history(self, session_id: str) -> list[dict[str, str]]:
        with session_scope() as s:
            rows = s.exec(
                select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
            ).all()
            return [
                {"role": "user" if r.role == "user" else "assistant", "content": r.content}
                for r in rows
            ]

    def _resident_words(self, session_id: str) -> str:
        return "\n".join(m["content"] for m in self._history(session_id) if m["role"] == "user")

    def _save_message(self, session_id: str, role: str, content: str, ticket_id: str | None = None,
                      data: dict[str, Any] | None = None) -> None:
        with session_scope() as s:
            s.add(ChatMessage(session_id=session_id, ticket_id=ticket_id, role=role,
                              content=content, data=data or {}))

    def _collected(self, session_id: str) -> dict[str, Any]:
        """Các trường Lễ tân đã thu thập được ở những lượt trước của chính phiên này."""
        with session_scope() as s:
            rows = s.exec(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id, ChatMessage.role == "receptionist")
                .order_by(ChatMessage.id.desc())
            ).all()
            # Đọc ngay trong session: ra khỏi khối này instance đã detach. Lùi qua những
            # lượt không mang trạng thái (ví dụ một câu trả lời nhanh về thông tin chung
            # xen vào giữa) thay vì coi lượt gần nhất là mất sạch những gì đã thu thập.
            for row in rows:
                got = dict(row.data or {}).get("truong_da_co")
                if isinstance(got, dict) and got:
                    return dict(got)
        return {}

    def _user_turn_count(self, session_id: str) -> int:
        return sum(1 for m in self._history(session_id) if m["role"] == "user")

    @staticmethod
    def _collected_block(known: dict[str, Any]) -> str:
        if not known:
            return ""
        lines = "\n".join(f"- {k}: {v}" for k, v in known.items())
        return ("ĐÃ THU THẬP ĐƯỢC Ở CÁC LƯỢT TRƯỚC — giữ nguyên trong \"truong_da_co\", KHÔNG hỏi lại:\n"
                + lines)

    def intake(self, session_id: str, requester_id: str, subject_id: str, message: str,
               *, requester_profile: dict[str, Any] | None = None,
               is_eval: bool = False) -> dict[str, Any]:
        """Handle one incoming message. Returns the reply and, if opened, the ticket."""
        # Các lượt hỏi–đáp của Lễ tân diễn ra TRƯỚC khi ticket tồn tại, nên nếu chỉ gắn
        # ticket_id thì chúng nằm rời rạc trong trace, không ghép lại thành một cuộc hội
        # thoại — mà đây lại đúng là đoạn quyết định ticket được mở với nội dung gì.
        with trace.scope(session_id=session_id):
            return self._intake(session_id, requester_id, subject_id, message,
                                requester_profile=requester_profile, is_eval=is_eval)

    def _intake(self, session_id: str, requester_id: str, subject_id: str, message: str,
                *, requester_profile: dict[str, Any] | None = None,
                is_eval: bool = False) -> dict[str, Any]:
        self._save_message(session_id, "user", message)

        known = self._collected(session_id)
        faq_hits = store.query_domain_scope(self.domain.id, message)
        faq_block = (
            "\n\n".join(f"[{h['filename']}]\n{h['noi_dung']}" for h in faq_hits)
            if faq_hits
            else "(không có)"
        )

        system = INTAKE_TEMPLATE.format(
            org_name=self.domain.display_name,
            audience=self.domain.audience,
            self_reference=self.domain.self_reference,
            honorific=self.domain.honorific,
            intake_spec=self.domain.intake_spec(),
            priority_rules=self.domain.priority_rules,
            field_keys=", ".join(f.key for f in self.domain.intake_fields),
            requester_block=self._requester_block(requester_id, subject_id, requester_profile),
            collected_block=self._collected_block(known),
            faq_block=faq_block,
        )
        messages = [{"role": "system", "content": system}] + self._history(session_id)

        try:
            raw = llm.chat_json(messages, role=ROLE)
        except llm.LLMError as exc:
            logger.warning("Lễ tân lỗi: %s", exc)
            reply = (f"Hệ thống đang bận, mong {self.domain.honorific} nhắn lại giúp "
                     "nội dung vừa rồi.")
            self._save_message(session_id, "receptionist", reply)
            return {"tra_loi": reply, "du_thong_tin": False, "ticket": None}
        if not isinstance(raw, dict):
            raw = {}

        reply = (str(raw.get("tra_loi") or "").strip()
                 or f"{self.domain.self_reference} đã ghi nhận thông tin.")

        # Code decides whether this was really answerable, not the model: a
        # "hoi_thong_tin" verdict with no domain-scope hits is treated as a report
        # instead, per the platform guard (no matching FAQ chunk -> always intake).
        loai = str(raw.get("loai") or "").strip()
        if loai == "hoi_thong_tin" and faq_hits:
            nguon = [str(x) for x in (raw.get("nguon") or []) if str(x).strip()] or sorted(
                {h["filename"] for h in faq_hits}
            )
            self._save_message(session_id, "receptionist", reply)
            bus.emit(
                f"SESSION-{session_id}",
                "receptionist_quick_answer",
                {"cau_hoi": message, "tra_loi": reply, "nguon": nguon},
                actor=ROLE,
            )
            return {
                "tra_loi": reply,
                "loai": "hoi_thong_tin",
                "nguon": nguon,
                "du_thong_tin": False,
                "ticket": None,
            }

        fields = raw.get("truong_da_co") if isinstance(raw.get("truong_da_co"), dict) else {}
        fields = {k: v for k, v in fields.items() if str(v).strip()}
        # Cộng dồn, không ghi đè: model quên một trường ở lượt sau là chuyện thường, và
        # để nó quên thì Lễ tân hỏi lại đúng câu vừa hỏi (đã đo bằng model thật).
        fields = {**known, **fields}
        priority = str(raw.get("uu_tien") or "BINH_THUONG").upper()
        if priority not in {"KHAN_CAP", "BINH_THUONG", "THAP"}:
            priority = "BINH_THUONG"
        summary = str(raw.get("tom_tat") or "").strip()

        # The code, not the model, decides when intake is done: having every required
        # field is both necessary and sufficient. Models tend to keep asking politely.
        missing = [k for k in self.domain.required_keys() if not str(fields.get(k, "")).strip()]
        # Chống hỏi vòng vo: người báo đã nhắn đủ số lượt mà model vẫn đòi thêm thì mở
        # phản ánh với những gì họ đã nói. Nguyên văn lời họ luôn được đính kèm
        # (`loi_nguoi_bao`), và phòng họp vẫn hỏi lại được qua `can_hoi_them_nguoi_bao`
        # — bắt người báo trả lời mãi một câu hỏi là cách chắc chắn nhất để mất họ.
        if missing and self._user_turn_count(session_id) >= settings.intake_max_turns:
            logger.warning("Tiếp nhận quá %d lượt vẫn thiếu %s — mở phản ánh với thông tin đã có",
                           settings.intake_max_turns, ", ".join(missing))
            for k in missing:
                fields[k] = "(người báo chưa nêu rõ)"
            missing = []
        complete = not missing

        reply = self._ensure_questions(reply, missing) if not complete else self._closing_reply(reply)

        state = {"truong_da_co": fields}
        ticket_payload = None
        if complete:
            ticket_payload = self._open_ticket(session_id, requester_id, subject_id, fields, priority, summary, is_eval)
            self._save_message(session_id, "receptionist", reply, ticket_payload["id"], data=state)
        else:
            self._save_message(session_id, "receptionist", reply, data=state)

        return {
            "tra_loi": reply,
            "loai": "phan_anh",
            "truong_da_co": fields,
            "thieu_truong": missing,
            "du_thong_tin": complete,
            "uu_tien": priority,
            "tom_tat": summary,
            "ticket": ticket_payload,
        }

    def _open_ticket(self, session_id, requester_id, subject_id, fields, priority, summary,
                      is_eval: bool = False) -> dict[str, Any]:
        prefix = "EVAL" if is_eval else "TK"
        ticket_id = f"{prefix}-{uuid.uuid4().hex[:8].upper()}"
        # Nguyên văn lời người báo, kèm theo bản chắt lọc chứ không thay nó. Các trường
        # intake chỉ giữ phần sự cố: một tin nhắn hai ý ("sửa giúp" + "cho hỏi chi phí")
        # bị chắt xuống còn mỗi ý đầu, và phần rơi mất thì Điều phối không thể biết là
        # còn ai chưa xử lý. Để trong `fields` nên nó tự chảy tới agent, Điều phối và
        # giám khảo — thêm bộ phận mới sau này không phải khai thêm gì.
        fields = {**fields, "loi_nguoi_bao": self._resident_words(session_id)}
        with session_scope() as s:
            s.add(
                Ticket(
                    id=ticket_id,
                    domain_id=self.domain.id,
                    resident_id=requester_id,
                    apartment_id=subject_id,
                    fields=fields,
                    summary=summary,
                    priority=priority,
                    status="dang_xu_ly",
                    session_id=session_id,
                    is_eval=is_eval,
                )
            )
        logger.info("Mở ticket %s uu_tien=%s", ticket_id, priority)
        return ticket_to_dict(ticket_id)

    def add_followup(self, ticket_id: str, answer: str) -> dict[str, Any]:
        """Fold a follow-up answer into the ticket so the room can run again."""
        with session_scope() as s:
            t = s.get(Ticket, ticket_id)
            if t is None:
                raise ValueError("Không tìm thấy phản ánh")
            fields = dict(t.fields or {})
            history = fields.get("thong_tin_bo_sung") or []
            if isinstance(history, str):
                history = [history]
            history.append(answer)
            fields["thong_tin_bo_sung"] = history
            # Gắn câu trả lời vào ĐÚNG câu hỏi đã ghim. Không có bước này thì lượt sau
            # agent chỉ thấy một danh sách câu trả lời rời rạc, không biết mình đã hỏi gì.
            fields = followup.fold_answer(fields, answer)
            t.fields = fields
            t.status = "dang_xu_ly"
            t.updated_at = datetime.utcnow()
            s.add(t)
            session_id = t.session_id
        self._save_message(session_id, "user", answer, ticket_id)
        return ticket_to_dict(ticket_id)

    # --------------------------------------------------------------- mode B
    def summarize(self, ticket: dict[str, Any], transcript: list[dict[str, Any]],
                  *, pending_actions: list[dict[str, Any]] | None = None,
                  extra_note: str = "") -> str:
        internal = json.dumps(
            {
                "tom_tat_phan_anh": ticket.get("summary"),
                "noi_dung": ticket.get("fields"),
                "ket_qua_noi_bo": [
                    {
                        "ket_luan": (t.get("output") or {}).get("ket_luan"),
                        "da_thuc_hien": (t.get("output") or {}).get("da_thuc_hien"),
                        "de_xuat": (t.get("output") or {}).get("de_xuat"),
                        "cau_hoi_cho_nguoi_bao": (t.get("output") or {}).get("can_hoi_them_nguoi_bao"),
                    }
                    for t in transcript
                ],
                "so_hanh_dong_cho_duyet": len(pending_actions or []),
                "ghi_chu_them": extra_note,
            },
            ensure_ascii=False,
            indent=1,
        )
        system = SUMMARY_TEMPLATE.format(
            org_name=self.domain.display_name,
            audience=self.domain.audience,
            self_reference=self.domain.self_reference,
            honorific=self.domain.honorific,
        )
        try:
            raw = llm.chat_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Nội dung nội bộ:\n{internal}"}],
                role=ROLE,
            )
            text = str(raw.get("tin_nhan") or "").strip() if isinstance(raw, dict) else ""
        except llm.LLMError as exc:
            logger.warning("Lễ tân tổng hợp lỗi: %s", exc)
            text = ""
        if not text:
            text = f"{self.domain.self_reference} đã tiếp nhận và đang xử lý phản ánh của {self.domain.honorific}."

        ticket_id = ticket.get("id", "")
        session_id = ticket.get("session_id", "")
        if session_id:
            self._save_message(session_id, "receptionist", text, ticket_id)
        if ticket_id:
            bus.emit(ticket_id, "receptionist_reply", {"tin_nhan": text}, actor=ROLE)
        return text

    def notify_action_result(self, ticket: dict[str, Any], action: dict[str, Any]) -> str:
        """Short update after a manager-approved action has run."""
        internal = json.dumps(
            {"tom_tat_phan_anh": ticket.get("summary"),
             "ket_qua_vua_thuc_hien": action.get("result"),
             "thanh_cong": action.get("status") == "da_thuc_hien"},
            ensure_ascii=False,
        )
        system = SUMMARY_TEMPLATE.format(
            org_name=self.domain.display_name,
            audience=self.domain.audience,
            self_reference=self.domain.self_reference,
            honorific=self.domain.honorific,
        )
        try:
            raw = llm.chat_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content":
                  f"Soạn tin CẬP NHẬT NGẮN (tối đa 60 từ) báo rằng việc đang chờ xem xét đã được "
                  f"xử lý xong. Nội dung nội bộ:\n{internal}"}],
                role=ROLE,
            )
            text = str(raw.get("tin_nhan") or "").strip() if isinstance(raw, dict) else ""
        except llm.LLMError:
            text = ""
        if not text:
            text = f"{self.domain.self_reference} xin cập nhật: nội dung {self.domain.honorific} phản ánh đã được xử lý."
        ticket_id, session_id = ticket.get("id", ""), ticket.get("session_id", "")
        if session_id:
            self._save_message(session_id, "receptionist", text, ticket_id)
        if ticket_id:
            bus.emit(ticket_id, "receptionist_reply", {"tin_nhan": text, "loai": "cap_nhat"}, actor=ROLE)
        return text


def ticket_to_dict(ticket_id: str) -> dict[str, Any]:
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            return {}
        return {
            "id": t.id,
            "domain_id": t.domain_id,
            "resident_id": t.resident_id,
            "apartment_id": t.apartment_id,
            "fields": dict(t.fields or {}),
            "summary": t.summary,
            "priority": t.priority,
            "status": t.status,
            "session_id": t.session_id,
            "is_eval": t.is_eval,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
        }
