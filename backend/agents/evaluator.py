"""Evaluator: generates test cases for a specialist agent, runs them end-to-end in
sandbox mode, grades the result, and checks routing regression before an agent is
allowed to go live.

Like Builder, Evaluator is an admin-tier service — no row in `Agent`, never in the
meeting room. Everything it runs is a real ticket flow (real Điều phối, real RAG,
real tools) but flagged `is_eval=True`, which `ToolExecutor` turns into: no real
`Action`, EVAL- coded write-tool output, no resident ever sees any of it (isolated
synthetic session ids).

Two kinds of checks, per principle 5 ("kiểm tra được bằng code thì để code kiểm
tra"): deterministic checks in `_code_checks()`, and a qualitative LLM judge in
`_judge()` for the parts code cannot verify.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from sqlmodel import select

from backend.agents import registry
from backend.app import trace
from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.events import bus, load_events
from backend.app.models import EvalCase, EvalResult, EvalRun
from backend.core.orchestrator import Orchestrator
from backend.core.receptionist import Receptionist, ticket_to_dict
from backend.core.room import run_room_dispatch
from backend.domain.loader import load_domain, load_mock
from backend.knowledge import library
from backend.llm import qwen_client as llm
from backend.tools.catalog import get_catalog

logger = logging.getLogger("evaluator")

_LEAK_PATTERNS = (re.compile(r"\bACT-[A-Z0-9]{4,}\b"), re.compile(r"\bEVAL-[A-Z0-9]{4,}\b"))
_FALLBACK_KET_LUAN = "(agent không đưa ra kết luận)"
_SERIOUS_GUARDS = {"agent_khong_hop_le", "vuot_so_luot_toi_da", "loi_framework"}

# ---------------------------------------------------------------------- prompts
GEN_SYSTEM_TEMPLATE = """Bạn là trợ lý sinh bộ test cho một agent chuyên môn trong platform phòng họp xử lý phản ánh của {org_name}.
Sinh {n} tình huống (case) để kiểm tra agent "{agent_id}" có được Điều phối gọi đúng hay không, và
có tận dụng đúng tài liệu tham khảo hay không. KHÔNG được biết prompt nghiệp vụ của agent — chỉ dựa
vào mô tả năng lực, catalog tool, và tóm tắt tài liệu đã liệt kê dưới đây.

Tỉ lệ bắt buộc trong {n} case:
- Khoảng 3-4 case "dung_nang_luc": tình huống agent này PHẢI được gọi.
- 1-2 case "ngoai_nang_luc": tình huống GẦN GIỐNG nhưng thực ra thuộc agent khác — agent này KHÔNG được gọi. Đây là loại case bắt lỗi mô tả năng lực quá rộng.
- Nếu hợp lý, 1 case "lien_bo_phan": agent này phải được gọi CÙNG một agent khác.

BẮT BUỘC: mỗi case phải gắn với đúng MỘT cư dân trong danh sách cư dân thật bên dưới (mục "CƯ DÂN CÓ THẬT
TRONG HỆ THỐNG") — ticket sẽ được chạy thật với dữ liệu của cư dân đó. TUYỆT ĐỐI không tự bịa mã căn hộ,
tên tòa nhà hay số phòng nào khác ngoài danh sách này; nếu ticket_text cần nhắc đến căn hộ, phải dùng đúng
mã căn hộ (trường "ma_can_ho") của cư dân đã chọn.

Với mỗi case, viết:
- "resident_id": mã cư dân (trường "ma_cu_dan") lấy từ danh sách CƯ DÂN CÓ THẬT bên dưới — chọn cư dân khác
  nhau giữa các case khi hợp lý, đừng dùng cùng một cư dân cho mọi case.
- "ticket_text": nội dung tin nhắn đầu tiên của người báo, bằng {audience_lower}, tự nhiên.
- "phan_hoi_bo_sung": 1-2 câu trả lời tiếp theo nếu Lễ tân hỏi thêm (để chạy tự động không cần người thật).
- "expected_agents" / "forbidden_agents": danh sách id agent. QUAN TRỌNG — trước khi cấm một agent, hãy đọc
  mô tả năng lực của các agent trong "expected_agents": nếu quy trình của họ nói rõ là phải phối hợp với
  agent kia mới làm được việc (ví dụ kế toán chỉ chốt được chi phí SAU KHI kỹ thuật xác nhận vật tư), thì
  KHÔNG được cấm agent kia — case sẽ không bao giờ thắng được dù hệ thống chạy đúng. Trường hợp đó phải để
  "lien_bo_phan" và đưa cả hai vào "expected_agents".
- "expected_tools": QUAN TRỌNG — chỉ được chọn tool NẰM TRONG danh sách tool CỦA CHÍNH agent "{agent_id}"
  liệt kê bên dưới (mục "TOOL CỦA AGENT NÀY"). TUYỆT ĐỐI không liệt kê tool của agent khác vào đây, kể cả
  khi tool đó có tên nghe hợp lý — agent "{agent_id}" không có quyền gọi tool nó chưa được cấp.
  Nếu việc xử lý đúng cần một agent KHÁC gọi tool của chính agent đó (ví dụ bàn giao chuyên môn), hãy để
  case đó thuộc loại "lien_bo_phan", thêm agent đó vào "expected_agents", và không được vừa liệt kê agent
  đó vào "forbidden_agents" vừa mong đợi tool của nó xuất hiện — tự mâu thuẫn.
  Chỉ liệt kê tool THỰC SỰ BẮT BUỘC: thiếu nó thì coi như chưa xử lý xong việc (ví dụ tool tạo phiếu, tool
  điều người). Đừng liệt kê tool tra cứu chỉ vì nghe có liên quan — hệ thống đã tự tiêm sẵn mã căn hộ / mã
  cư dân của ticket vào tool, nên một agent làm đúng vẫn có thể bỏ qua bước tra cứu đó.
- "expected_sources": tên tệp tài liệu (nếu agent có tài liệu liên quan bên dưới) mà câu trả lời nên trích dẫn — để trống nếu agent chưa có tài liệu nào.
- "rubric": 1-2 câu mô tả tiêu chí đúng/sai cho case này, để giám khảo sau này chấm.
- "loai": "dung_nang_luc" | "ngoai_nang_luc" | "lien_bo_phan".

Chỉ trả về JSON: {{"cases": [{{"resident_id": str, "ticket_text": str, "phan_hoi_bo_sung": [str], "expected_agents": [str], "forbidden_agents": [str], "expected_tools": [str], "expected_sources": [str], "rubric": str, "loai": str}}]}}

CƯ DÂN CÓ THẬT TRONG HỆ THỐNG (chỉ được chọn resident_id và mã căn hộ từ đây):
{residents_block}

MÔ TẢ NĂNG LỰC CỦA AGENT "{agent_id}":
{capability}

TOOL CỦA AGENT NÀY (chỉ được chọn expected_tools từ đây):
{own_tools_block}

CATALOG TOOL ĐẦY ĐỦ CỦA HỆ THỐNG (chỉ để hiểu bối cảnh, KHÔNG dùng để chọn expected_tools):
{catalog_block}

TÓM TẮT TÀI LIỆU ĐÃ GẮN CHO AGENT (nếu có):
{docs_block}

CÁC AGENT KHÁC ĐANG CÓ:
{agents_block}"""

JUDGE_SYSTEM_TEMPLATE = """Bạn là giám khảo độc lập, chấm điểm một lượt xử lý phản ánh của platform phòng họp xử lý phản ánh cho {org_name}.
Chấm thang 1-5 cho từng tiêu chí sau, dựa HOÀN TOÀN vào bằng chứng được cung cấp — không suy đoán ngoài dữ liệu:
- "bam_nguon": kết luận có dựa trên tài liệu tham khảo và kết quả tool đã gọi không, hay đang bịa số liệu/thông tin.
- "dung_quy_trinh": có làm đúng quy trình mô tả trong tiêu chí đánh giá (rubric) bên dưới không.
- "dung_pham_vi": có làm đúng việc thuộc năng lực của mình không, có bàn giao đúng cho bộ phận khác khi cần không.
- "phan_hoi_nguoi_bao": câu trả lời cuối cùng gửi người báo có rõ ràng, không hứa hẹn quá mức, không lộ thông tin nội bộ (tên bộ phận, tên tool, mã nội bộ) không.
Với mọi điểm dưới 3, PHẢI trích dẫn bằng chứng cụ thể (số thứ tự sự kiện #seq hoặc đoạn văn bản) vào "bang_chung".
Chỉ trả về JSON: {{"diem": {{"bam_nguon": n, "dung_quy_trinh": n, "dung_pham_vi": n, "phan_hoi_nguoi_bao": n}}, "ly_do": {{"bam_nguon": str, "dung_quy_trinh": str, "dung_pham_vi": str, "phan_hoi_nguoi_bao": str}}, "bang_chung": {{}}}}

TIÊU CHÍ ĐÁNH GIÁ (RUBRIC) CHO CASE NÀY:
{rubric}

TICKET:
{ticket_block}

DIỄN BIẾN PHÒNG HỌP:
{transcript_block}

CÂU TRẢ LỜI CUỐI GỬI NGƯỜI BÁO:
{final_reply}"""


# ---------------------------------------------------------------------- generation
def _docs_block(agent_id: str) -> str:
    docs = library.list_agent_docs(agent_id)
    if not docs:
        return "(agent chưa gắn tài liệu nào)"
    return "\n".join(f"- {d['filename']}: {d['summary'] or '(chưa có tóm tắt)'}" for d in docs)


def _catalog_block() -> str:
    return "\n".join(f"- {s.name}: {s.manager_description}" for s in get_catalog().all())


def _residents() -> list[dict[str, Any]]:
    return load_mock("cu_dan.json")


def _residents_block() -> str:
    residents = _residents()
    if not residents:
        return "(không có dữ liệu cư dân mẫu)"
    return "\n".join(
        f"- resident_id={r['ma_cu_dan']} | ma_can_ho={r['ma_can_ho']} | tên={r['ho_ten']}"
        for r in residents
    )


def _own_tools_block(agent: dict[str, Any]) -> str:
    descriptions = {s.name: s.manager_description for s in get_catalog().all()}
    tools = agent.get("tools") or []
    if not tools:
        return "(agent này chưa được cấp tool nào)"
    return "\n".join(f"- {t}: {descriptions.get(t, '(không có mô tả)')}" for t in tools)


def _other_agents_block(domain_id: str, exclude_id: str) -> str:
    agents = [a for a in registry.list_agents(domain_id) if a["status"] == "active" and a["id"] != exclude_id]
    if not agents:
        return "(không có agent nào khác)"
    return "\n".join(
        f"- {a['id']}: {a['capability']} | tool của agent này: {', '.join(a['tools']) or '(không có)'}"
        for a in agents
    )


def _agent_tool_set(agent_id: str, own_tools: set[str], own_id: str) -> set[str]:
    if agent_id == own_id:
        return own_tools
    a = registry.get_agent(agent_id)
    return set(a["tools"] or []) if a else set()


def _sanitize_case_tools(agent_id: str, own_tools: set[str], case: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: an LLM told to only pick this agent's own tools can still
    hallucinate a tool belonging to another agent (same class of mistake already
    seen and fixed in Builder). Strip anything not actually grantable to one of
    this case's expected_agents, rather than trust the prompt alone."""
    expected_agents = case.get("expected_agents") or [agent_id]
    allowed: set[str] = set()
    for aid in expected_agents:
        allowed |= _agent_tool_set(aid, own_tools, agent_id)
    expected_tools = case.get("expected_tools") or []
    kept = [t for t in expected_tools if t in allowed]
    dropped = [t for t in expected_tools if t not in allowed]
    if dropped:
        logger.warning(
            "Case sinh cho agent '%s' đòi tool ngoài quyền hạn của các agent liên quan (%s) — đã loại bỏ: %s",
            agent_id, expected_agents, dropped,
        )
    case["expected_tools"] = kept
    return case


_warned_shared_model = False


def _warn_if_shared_model() -> None:
    """QWEN_EVALUATOR_MODEL để trống nghĩa là judge chấm bằng chính model của agent
    nó đang chấm — một giám khảo không độc lập. In cảnh báo đúng một lần."""
    global _warned_shared_model
    if not settings._evaluator_model_explicit and not _warned_shared_model:
        _warned_shared_model = True
        logger.warning(
            "QWEN_EVALUATOR_MODEL chưa được cấu hình — Evaluator (sinh case + judge) đang dùng "
            "chung model '%s' với agent chuyên môn. Judge chấm bằng chính model đang được chấm "
            "sẽ kém tin cậy hơn một giám khảo độc lập; nên đặt QWEN_EVALUATOR_MODEL sang một "
            "model khác (khuyến nghị mạnh hơn) trong .env.",
            settings.evaluator_model,
        )


def generate_cases(agent_id: str, *, n: int | None = None, auto_approve: bool = False) -> list[dict[str, Any]]:
    _warn_if_shared_model()
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise ValueError(f"Không tìm thấy agent '{agent_id}'")
    domain = load_domain(agent["domain_id"])
    n = n or settings.eval_cases_per_agent

    own_tools = set(agent["tools"] or [])
    residents = _residents()
    resident_ids = {r["ma_cu_dan"] for r in residents}
    system = GEN_SYSTEM_TEMPLATE.format(
        org_name=domain.display_name, agent_id=agent_id, n=n,
        audience_lower=domain.audience, capability=agent["capability"],
        own_tools_block=_own_tools_block(agent),
        catalog_block=_catalog_block(), docs_block=_docs_block(agent_id),
        agents_block=_other_agents_block(agent["domain_id"], agent_id),
        residents_block=_residents_block(),
    )
    try:
        raw = llm.chat_json(
            [{"role": "system", "content": system}, {"role": "user", "content": "Sinh bộ test."}],
            role="eval_generate", model=settings.evaluator_model,
        )
    except llm.LLMError as exc:
        raise RuntimeError(f"Sinh test case thất bại: {exc}") from exc

    cases_raw = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(cases_raw, list):
        raise RuntimeError("Model không trả về danh sách case hợp lệ")

    saved = []
    with session_scope() as s:
        for i, c in enumerate(cases_raw):
            if not isinstance(c, dict) or not str(c.get("ticket_text") or "").strip():
                continue
            # Defense in depth, same reasoning as `_sanitize_case_tools`: an LLM told to
            # only use residents from the real list can still invent one. A case whose
            # ticket_text talks about an apartment that doesn't exist in the real mock
            # data will always run against `residents[0]` instead (see `_pick_resident`),
            # producing a final reply that contradicts the ticket's own real fields and
            # tanking the judge's `bam_nguon` score for reasons that have nothing to do
            # with the agent under test. Round-robin over real residents as a fallback
            # rather than trust the model picked a valid one.
            resident_id = str(c.get("resident_id") or "")
            if resident_id not in resident_ids and residents:
                resident_id = residents[i % len(residents)]["ma_cu_dan"]
            # `or [agent_id]` would be wrong here: for "ngoai_nang_luc" cases the model
            # is meant to return expected_agents=[] (this agent must NOT be called), and
            # `[] or default` treats that empty-but-intentional list as falsy, silently
            # putting agent_id back in — which then contradicts forbidden_agents=[agent_id]
            # and makes the case unwinnable no matter what the agent does. Only a missing
            # key (None) should fall back to the default.
            raw_expected = c.get("expected_agents")
            c["expected_agents"] = [str(x) for x in (raw_expected if raw_expected is not None else [agent_id])]
            c["expected_tools"] = [str(x) for x in (c.get("expected_tools") or [])]
            c = _sanitize_case_tools(agent_id, own_tools, c)
            case_id = f"EC-{uuid.uuid4().hex[:8].upper()}"
            row = EvalCase(
                id=case_id, agent_id=agent_id,
                ticket_text=str(c.get("ticket_text", "")).strip(),
                resident_id=resident_id,
                phan_hoi_bo_sung=[str(x) for x in (c.get("phan_hoi_bo_sung") or [])],
                expected_agents=c["expected_agents"],
                forbidden_agents=[str(x) for x in (c.get("forbidden_agents") or [])],
                expected_tools=c["expected_tools"],
                forbidden_tools=[str(x) for x in (c.get("forbidden_tools") or [])],
                expected_sources=[str(x) for x in (c.get("expected_sources") or [])],
                rubric=str(c.get("rubric", "")).strip(),
                source="generated",
                approved=auto_approve,
                loai=c.get("loai") if c.get("loai") in
                     ("dung_nang_luc", "ngoai_nang_luc", "lien_bo_phan") else "dung_nang_luc",
            )
            s.add(row)
            saved.append(row)
        s.flush()
        return [_case_to_dict(r) for r in saved]


def _case_to_dict(c: EvalCase) -> dict[str, Any]:
    return {
        "id": c.id, "agent_id": c.agent_id, "ticket_text": c.ticket_text,
        "resident_id": c.resident_id,
        "phan_hoi_bo_sung": c.phan_hoi_bo_sung, "expected_agents": c.expected_agents,
        "forbidden_agents": c.forbidden_agents, "expected_tools": c.expected_tools,
        "forbidden_tools": c.forbidden_tools, "expected_sources": c.expected_sources,
        "rubric": c.rubric, "source": c.source, "approved": c.approved, "loai": c.loai,
        "created_at": c.created_at.isoformat(),
    }


def list_cases(agent_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.exec(select(EvalCase).where(EvalCase.agent_id == agent_id)).all()
        return [_case_to_dict(r) for r in rows]


def approve_case(case_id: str, approved: bool = True) -> dict[str, Any]:
    with session_scope() as s:
        row = s.get(EvalCase, case_id)
        if row is None:
            raise ValueError("Không tìm thấy case")
        row.approved = approved
        s.add(row)
        s.flush()
        return _case_to_dict(row)


def create_manual_case(payload: dict[str, Any]) -> dict[str, Any]:
    case_id = payload.get("id") or f"EC-{uuid.uuid4().hex[:8].upper()}"
    with session_scope() as s:
        row = EvalCase(
            id=case_id, agent_id=payload["agent_id"], ticket_text=payload["ticket_text"],
            resident_id=payload.get("resident_id", ""),
            phan_hoi_bo_sung=payload.get("phan_hoi_bo_sung", []),
            expected_agents=payload.get("expected_agents", [payload["agent_id"]]),
            forbidden_agents=payload.get("forbidden_agents", []),
            expected_tools=payload.get("expected_tools", []),
            forbidden_tools=payload.get("forbidden_tools", []),
            expected_sources=payload.get("expected_sources", []),
            rubric=payload.get("rubric", ""), source="manual",
            approved=payload.get("approved", True), loai=payload.get("loai", "dung_nang_luc"),
        )
        s.add(row)
        s.flush()
        return _case_to_dict(row)


# ---------------------------------------------------------------------- running one case
def _pick_resident(resident_id: str) -> dict[str, Any]:
    residents = load_mock("cu_dan.json")
    if resident_id:
        found = next((r for r in residents if r["ma_cu_dan"] == resident_id), None)
        if found:
            return found
    return residents[0] if residents else {"ma_cu_dan": "", "ma_can_ho": ""}


def _run_ticket_flow(case: dict[str, Any], run_id: str) -> tuple[dict[str, Any] | None, str]:
    """Drive the receptionist intake (auto-answering follow-ups) then the room.
    Returns (ticket_dict_or_None, error_message)."""
    receptionist = Receptionist()
    resident = _pick_resident(case.get("resident_id", ""))
    session_id = f"eval-{run_id}-{case['id']}"
    # Hand-written cases can't always anticipate every required intake field the
    # domain pack asks for (e.g. "vị trí cụ thể" makes little sense for a billing
    # question) — generic filler turns as a safety net, same pattern already used
    # by scripts/smoke_test.py's open_ticket() helper.
    turns = [case["ticket_text"]] + list(case.get("phan_hoi_bo_sung") or []) + [
        "Mức độ nghiêm trọng, khung giờ nào cũng được ạ.",
        "Tôi không có thêm thông tin gì khác.",
    ]

    result = None
    for msg in turns:
        result = receptionist.intake(
            session_id, resident["ma_cu_dan"], resident["ma_can_ho"], msg,
            # Cùng bộ thông tin người báo mà tầng API gửi, để case đánh giá đi đúng
            # đường của một phiên thật (Lễ tân không hỏi lại thứ hệ thống đã biết).
            requester_profile={k: resident[k] for k in ("ho_ten", "ma_can_ho", "dien_thoai", "vai_tro")
                               if resident.get(k)},
            is_eval=True,
        )
        if result.get("ticket"):
            break
        if result.get("loai") == "hoi_thong_tin":
            # A quick-answer means no ticket will ever open for this message; that
            # itself may be exactly what a case wants to test (handled by the
            # caller reading `quick_answer` back), so stop here rather than loop.
            return None, "quick_answer"
    if not result or not result.get("ticket"):
        return None, "Lễ tân không tạo được ticket sau khi dùng hết phan_hoi_bo_sung"

    ticket_id = result["ticket"]["id"]
    try:
        run_room_dispatch(ticket_id, extra_agent_id=case["agent_id"])
    except Exception as exc:  # noqa: BLE001 - a crash must still produce a failed result, not a 500
        logger.exception("Phòng họp lỗi khi đánh giá case, ticket=%s", ticket_id)
        return ticket_to_dict(ticket_id), f"Phòng họp lỗi: {str(exc)[:200]}"
    return ticket_to_dict(ticket_id), ""


def _code_checks(case: dict[str, Any], ticket: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    agent_starts = [e for e in events if e["type"] == "agent_start"]
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    rag_hits = [e for e in events if e["type"] == "rag_hits"]
    agent_outputs = [e for e in events if e["type"] == "agent_output"]
    guards = [e for e in events if e["type"] == "guard_triggered"]
    replies = [e for e in events if e["type"] == "receptionist_reply"]

    spoken = {e["payload"]["agent_id"] for e in agent_starts}
    tools_used = {e["payload"]["tool"] for e in tool_calls}
    own_sources = {
        h["filename"]
        for e in rag_hits if e["payload"].get("agent_id") == case["agent_id"]
        for h in e["payload"].get("ket_qua", [])
    }

    missing_agents = [a for a in case["expected_agents"] if a not in spoken]
    unwanted_agents = [a for a in case["forbidden_agents"] if a in spoken]
    missing_tools = [t for t in case["expected_tools"] if t not in tools_used]
    unwanted_tools = [t for t in case["forbidden_tools"] if t in tools_used]
    missing_sources = [f for f in case["expected_sources"] if f not in own_sources]

    bad_outputs = [
        e for e in agent_outputs
        if not isinstance(e["payload"].get("output"), dict)
        or e["payload"]["output"].get("ket_luan") == _FALLBACK_KET_LUAN
    ]
    serious_guards = [g for g in guards if g["payload"].get("guard") in _SERIOUS_GUARDS]

    apartment = ticket.get("apartment_id", "")
    resident = ticket.get("resident_id", "")
    bad_context = [
        e for e in tool_calls
        if (e["payload"]["args"].get("ma_can_ho") not in (None, apartment))
        or (e["payload"]["args"].get("ma_cu_dan") not in (None, resident))
    ]

    final_reply = replies[-1]["payload"]["tin_nhan"] if replies else ""
    leaked = [name for name in (spoken | tools_used) if name and name.lower() in final_reply.lower()]
    leaked += [p.pattern for p in _LEAK_PATTERNS if p.search(final_reply)]

    checks = {
        "expected_agents_ok": not missing_agents,
        "forbidden_agents_ok": not unwanted_agents,
        "expected_tools_ok": not missing_tools,
        "forbidden_tools_ok": not unwanted_tools,
        "expected_sources_ok": not missing_sources,
        "outputs_valid_ok": not bad_outputs,
        "no_serious_guard_ok": not serious_guards,
        "context_params_ok": not bad_context,
        "no_leak_ok": not leaked,
        "detail": {
            "missing_agents": missing_agents, "unwanted_agents": unwanted_agents,
            "missing_tools": missing_tools, "unwanted_tools": unwanted_tools,
            "missing_sources": missing_sources, "so_output_loi": len(bad_outputs),
            "serious_guards": [g["payload"].get("guard") for g in serious_guards],
            "bad_context_calls": len(bad_context), "leaked": leaked,
        },
        "final_reply": final_reply,
    }
    checks["passed"] = all(
        checks[k] for k in (
            "expected_agents_ok", "forbidden_agents_ok", "expected_tools_ok", "forbidden_tools_ok",
            "expected_sources_ok", "outputs_valid_ok", "no_serious_guard_ok", "context_params_ok", "no_leak_ok",
        )
    )
    return checks


def _transcript_block(events: list[dict[str, Any]]) -> str:
    lines = []
    for e in events:
        if e["type"] == "agent_output":
            o = e["payload"].get("output", {})
            lines.append(f"#{e['seq']} [{e['payload'].get('agent_id')}] {o.get('ket_luan', '')}")
        elif e["type"] == "tool_call":
            lines.append(f"#{e['seq']} tool_call {e['payload']['tool']} args={json.dumps(e['payload']['args'], ensure_ascii=False)}")
        elif e["type"] == "tool_result":
            lines.append(f"#{e['seq']} tool_result {e['payload']['tool']} ket_qua={json.dumps(e['payload']['ket_qua'], ensure_ascii=False)}")
        elif e["type"] == "rag_hits":
            files = sorted({h["filename"] for h in e["payload"].get("ket_qua", [])})
            lines.append(f"#{e['seq']} rag_hits [{e['payload'].get('agent_id')}] {files}")
    return "\n".join(lines) or "(không có diễn biến)"


def _judge(domain, case: dict[str, Any], ticket: dict[str, Any], events: list[dict[str, Any]], final_reply: str) -> dict[str, Any]:
    system = JUDGE_SYSTEM_TEMPLATE.format(
        org_name=domain.display_name, rubric=case.get("rubric") or "(không có tiêu chí riêng)",
        ticket_block=json.dumps({"summary": ticket.get("summary"), "fields": ticket.get("fields")}, ensure_ascii=False),
        transcript_block=_transcript_block(events), final_reply=final_reply or "(không có)",
    )
    try:
        raw = llm.chat_json(
            [{"role": "system", "content": system}, {"role": "user", "content": "Chấm điểm."}],
            role="eval_judge", model=settings.evaluator_model,
        )
    except llm.LLMError as exc:
        logger.warning("Judge lỗi: %s", exc)
        return {"diem": {}, "ly_do": {"loi": str(exc)[:200]}, "bang_chung": {}, "loi": True}
    if not isinstance(raw, dict):
        return {"diem": {}, "ly_do": {}, "bang_chung": {}, "loi": True}
    return raw


def run_case(agent_id: str, case: dict[str, Any], run_id: str) -> dict[str, Any]:
    with trace.scope(run_id=run_id, case_id=case["id"], agent_id=agent_id):
        return _run_case(agent_id, case, run_id)


def _run_case(agent_id: str, case: dict[str, Any], run_id: str) -> dict[str, Any]:
    ticket, err = _run_ticket_flow(case, run_id)
    if err == "quick_answer":
        checks = {"passed": case["loai"] == "ngoai_nang_luc", "detail": {"note": "Lễ tân trả lời nhanh, không mở ticket"}}
        return {"case_id": case["id"], "ticket_id": "", "checks": checks, "judge": {}, "passed": checks["passed"]}
    if ticket is None or err:
        checks = {"passed": False, "detail": {"loi": err}}
        return {"case_id": case["id"], "ticket_id": (ticket or {}).get("id", ""), "checks": checks, "judge": {}, "passed": False}

    events = load_events(ticket["id"])
    checks = _code_checks(case, ticket, events)

    domain = load_domain(ticket["domain_id"])
    judge = _judge(domain, case, ticket, events, checks["final_reply"])
    judge_scores = judge.get("diem", {}) if isinstance(judge.get("diem"), dict) else {}
    judge_ok = bool(judge_scores) and all(
        isinstance(v, (int, float)) and v >= settings.eval_judge_min for v in judge_scores.values()
    )

    passed = checks["passed"] and judge_ok and not judge.get("loi")
    return {
        "case_id": case["id"], "ticket_id": ticket["id"], "checks": checks,
        "judge": judge, "passed": passed,
    }


# ---------------------------------------------------------------------- running a full evaluation
def run_agent(agent_id: str, *, trigger: str = "manual") -> dict[str, Any]:
    _warn_if_shared_model()
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise ValueError(f"Không tìm thấy agent '{agent_id}'")
    cases = [c for c in list_cases(agent_id) if c["approved"]]
    if not cases:
        raise ValueError("Chưa có case nào được duyệt để chạy — sinh case và duyệt trước")

    run_id = f"ER-{uuid.uuid4().hex[:8].upper()}"
    started = datetime.utcnow()
    with session_scope() as s:
        s.add(EvalRun(id=run_id, agent_id=agent_id, agent_version=agent["version"],
                      trigger=trigger, status="running", started_at=started))

    bus.emit(f"EVALRUN-{run_id}", "eval_run_start", {"agent_id": agent_id, "so_case": len(cases)})

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=settings.eval_concurrency) as pool:
        futures = {pool.submit(run_case, agent_id, c, run_id): c for c in cases}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001 - one case failing must not kill the run
                logger.exception("Case %s lỗi", c["id"])
                r = {"case_id": c["id"], "ticket_id": "", "checks": {"passed": False, "detail": {"loi": str(exc)[:200]}}, "judge": {}, "passed": False}
            results.append(r)
            with session_scope() as s:
                s.add(EvalResult(run_id=run_id, case_id=r["case_id"], ticket_id=r["ticket_id"],
                                 checks=r["checks"], judge=r["judge"], passed=r["passed"]))
            bus.emit(f"EVALRUN-{run_id}", "eval_case_done",
                     {"case_id": r["case_id"], "passed": r["passed"], "ticket_id": r["ticket_id"]})

    passed = all(r["passed"] for r in results)
    finished = datetime.utcnow()
    summary = {
        "so_case": len(results), "so_dat": sum(1 for r in results if r["passed"]),
        "case_fail": [r["case_id"] for r in results if not r["passed"]],
    }
    with session_scope() as s:
        run = s.get(EvalRun, run_id)
        run.status, run.passed = "done", passed
        run.finished_at = finished
        run.duration_ms = int((finished - started).total_seconds() * 1000)
        run.summary = summary
        s.add(run)

    bus.emit(f"EVALRUN-{run_id}", "eval_run_end", {"passed": passed, "summary": summary})
    return {"run_id": run_id, "agent_id": agent_id, "agent_version": agent["version"],
            "passed": passed, "results": results, "summary": summary}


def get_run(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        run = s.get(EvalRun, run_id)
        if run is None:
            raise ValueError("Không tìm thấy lần chạy")
        results = s.exec(select(EvalResult).where(EvalResult.run_id == run_id)).all()
        return {
            "id": run.id, "agent_id": run.agent_id, "agent_version": run.agent_version,
            "trigger": run.trigger, "status": run.status, "passed": run.passed,
            "summary": run.summary, "tokens": run.tokens, "duration_ms": run.duration_ms,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "results": [
                {"case_id": r.case_id, "ticket_id": r.ticket_id, "checks": r.checks,
                 "judge": r.judge, "passed": r.passed}
                for r in results
            ],
        }


def list_runs(agent_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.exec(select(EvalRun).where(EvalRun.agent_id == agent_id).order_by(EvalRun.started_at.desc())).all()
        return [
            {"id": r.id, "agent_version": r.agent_version, "trigger": r.trigger, "status": r.status,
             "passed": r.passed, "summary": r.summary, "tokens": r.tokens, "duration_ms": r.duration_ms,
             "started_at": r.started_at.isoformat()}
            for r in rows
        ]


def latest_passing_run(agent_id: str, agent_version: int) -> dict[str, Any] | None:
    with session_scope() as s:
        rows = s.exec(
            select(EvalRun)
            .where(EvalRun.agent_id == agent_id, EvalRun.status == "done", EvalRun.passed == True)  # noqa: E712
            .order_by(EvalRun.started_at.desc())
        ).all()
        for r in rows:
            if r.agent_version == agent_version:
                return {"id": r.id, "agent_version": r.agent_version, "started_at": r.started_at.isoformat()}
        return None


# ---------------------------------------------------------------------- regression
def _route_once(ticket_text: str, members: list[dict[str, Any]]) -> str:
    fake_ticket = {
        "id": f"REG-{uuid.uuid4().hex[:6]}", "domain_id": "", "resident_id": "", "apartment_id": "",
        "fields": {"mo_ta": ticket_text}, "summary": ticket_text, "priority": "BINH_THUONG",
        "status": "sandbox", "session_id": "",
    }
    decision = Orchestrator().decide(
        ticket=fake_ticket, members=members, transcript=[], pending_suggestions=[],
        pending_actions=[], turns_used=0, max_turns=1,
    )
    return decision.agent_id if decision.hanh_dong == "goi_agent" else ""


def _route_majority(ticket_text: str, members: list[dict[str, Any]], repeats: int) -> tuple[str, float]:
    picks = [_route_once(ticket_text, members) for _ in range(repeats)]
    best = max(set(picks), key=picks.count) if picks else ""
    stability = picks.count(best) / len(picks) if picks else 0.0
    return best, stability


def regression(candidate_agent_id: str | None = None, domain_id: str | None = None) -> dict[str, Any]:
    domain_id = domain_id or settings.domain_id
    active = registry.active_specialists(domain_id)
    candidate = registry.get_agent(candidate_agent_id) if candidate_agent_id else None

    baseline_members = active
    candidate_members = active if (candidate is None or candidate["id"] in {a["id"] for a in active}) else active + [candidate]

    # Lấy mẫu tối đa N case/agent thay vì toàn bộ — chi phí một lần regression phải
    # có trần, không được lớn dần vô hạn khi domain tích lũy thêm case theo thời
    # gian (đo thật: không giới hạn từng khiến builder/auto vượt 30 phút không
    # xong). Ưu tiên case viết tay (source=manual) vì đó là bộ hồi quy gốc đáng tin
    # nhất; case tự sinh chỉ lấp đầy phần còn lại nếu chưa đủ N.
    cap = settings.eval_regression_max_cases_per_agent

    def _sample(agent_id: str) -> list[dict[str, Any]]:
        approved = [c for c in list_cases(agent_id) if c["approved"]]
        manual = [c for c in approved if c["source"] == "manual"]
        rest = [c for c in approved if c["source"] != "manual"]
        return (manual + rest)[:cap]

    all_cases = []
    for a in active:
        all_cases.extend(_sample(a["id"]))
    if candidate and candidate["id"] not in {a["id"] for a in active}:
        all_cases.extend(_sample(candidate["id"]))

    per_agent = {a["id"]: {"before_ok": 0, "after_ok": 0, "total": 0} for a in (active + ([candidate] if candidate else []))}
    stolen: list[dict[str, Any]] = []
    stabilities = []

    for case in all_cases:
        expected = case["expected_agents"][0] if case["expected_agents"] else None
        if not expected or expected not in per_agent:
            continue
        per_agent[expected]["total"] += 1

        before, s1 = _route_majority(case["ticket_text"], baseline_members, settings.eval_router_repeats)
        after, s2 = _route_majority(case["ticket_text"], candidate_members, settings.eval_router_repeats)
        stabilities.extend([s1, s2])

        if before == expected:
            per_agent[expected]["before_ok"] += 1
        if after == expected:
            per_agent[expected]["after_ok"] += 1
        if before == expected and after != expected and after and candidate and after == candidate["id"]:
            stolen.append({"case_id": case["id"], "ticket_text": case["ticket_text"],
                           "truoc": before, "sau": after})

    report = {
        "candidate_agent_id": candidate_agent_id,
        "do_on_dinh_dinh_tuyen": round(sum(stabilities) / len(stabilities), 2) if stabilities else 1.0,
        "theo_agent": {
            aid: {
                "total": v["total"],
                "do_chinh_xac_truoc": round(v["before_ok"] / v["total"], 2) if v["total"] else None,
                "do_chinh_xac_sau": round(v["after_ok"] / v["total"], 2) if v["total"] else None,
                "giam": (v["before_ok"] - v["after_ok"]) if v["total"] else 0,
            }
            for aid, v in per_agent.items()
        },
        "case_bi_cuop": stolen,
    }
    max_drop = max((v["giam"] for v in report["theo_agent"].values()), default=0)
    report["dat"] = max_drop <= settings.eval_regression_max_drop and not stolen
    return report
