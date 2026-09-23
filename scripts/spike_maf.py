"""Feasibility spike: run the GroupChat room without any LLM.

A stub chat client replaces Qwen (canned agent answers) and the router is
monkeypatched, so this exercises the real framework wiring: participants,
selection_func, termination_condition, FunctionTool schemas, ContextProvider
hooks, and the ToolExecutor approval gate.

Run: python scripts/spike_maf.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework import (  # noqa: E402
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from backend.app.db import init_db, session_scope  # noqa: E402
from backend.app.events import load_events  # noqa: E402
from backend.app.models import Action, Ticket  # noqa: E402
from backend.tools.catalog import get_catalog  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class StubChatClient(FunctionInvocationLayer, BaseChatClient):
    """Returns a scripted tool call, then a scripted JSON answer.

    Mixes in FunctionInvocationLayer exactly like OpenAIChatClient does, so the
    framework runs its real tool-calling loop against the scripted calls."""

    def __init__(self, agent_name: str, steps: list[dict[str, Any]]) -> None:
        super().__init__()
        self._agent = agent_name
        self._steps = steps
        self._idx = 0

    async def _inner_get_response(  # type: ignore[override]
        self, *, messages: Sequence[Message], stream: bool, options: Any, **kwargs: Any
    ) -> ChatResponse:
        agent_name, idx = self._agent, self._idx
        self._idx += 1
        step = self._steps[idx] if idx < len(self._steps) else {"type": "text", "text": "{}"}

        if step["type"] == "tool":
            return ChatResponse(
                messages=[
                    Message(
                        role="assistant",
                        contents=[
                            Content.from_function_call(
                                call_id=f"call-{agent_name}-{idx}",
                                name=step["name"],
                                arguments=step["args"],
                            )
                        ],
                    )
                ]
            )
        return ChatResponse(messages=[Message(role="assistant", contents=[step["text"]])])


def main() -> int:
    init_db()
    get_catalog().refresh_mcp_sync()

    # Start from a clean slate so event counts are meaningful.
    from sqlmodel import delete

    from backend.app.models import RoomEvent

    with session_scope() as s:
        s.exec(delete(RoomEvent).where(RoomEvent.ticket_id == "TK-SPIKE01"))
        s.exec(delete(Action).where(Action.ticket_id == "TK-SPIKE01"))

    # ---------------------------------------------------------------- fixtures
    from backend.agents import registry

    ticket_id = "TK-SPIKE01"
    with session_scope() as s:
        if s.get(Ticket, ticket_id) is None:
            s.add(
                Ticket(
                    id=ticket_id,
                    domain_id="vinhomes",
                    resident_id="CD001",
                    apartment_id="S1-1203",
                    fields={"mo_ta": "Trần nhà tắm nhỏ giọt liên tục", "vi_tri": "Nhà tắm"},
                    summary="Rò nước trần nhà tắm",
                    priority="KHAN_CAP",
                    status="dang_xu_ly",
                    session_id="spike",
                )
            )
    for spec in (
        dict(id="ky_thuat", display_name="Kỹ thuật",
             capability="Xử lý hỏng hóc điện nước, điều hòa, thang máy; lập phiếu sửa chữa và điều kỹ thuật viên",
             tools=["tra_lich_ktv", "tao_phieu_sua_chua", "dieu_ktv_khan_cap"]),
        dict(id="an_ninh", display_name="An ninh",
             capability="Xử lý sự việc an ninh trật tự, trích xuất camera và lập biên bản sự việc",
             tools=["tra_nhat_ky_camera", "tao_bien_ban_an_ninh"]),
    ):
        if registry.get_agent(spec["id"]) is None:
            registry.create_agent(
                registry.AgentCreate(**spec, business_prompt="Làm đúng năng lực.", status="active")
            )

    # ---------------------------------------------------------------- the spike
    from backend.core import maf_room

    script = {
        "ky_thuat": [
            {"type": "tool", "name": "tao_phieu_sua_chua",
             "args": {"ma_can_ho": "HACK-9999", "mo_ta_su_co": "Rò nước trần nhà tắm",
                      "chuyen_mon": "dien_nuoc", "muc_do": "khan_cap"}},
            {"type": "tool", "name": "dieu_ktv_khan_cap",
             "args": {"chuyen_mon": "dien_nuoc", "ly_do": "Rò nước lớn"}},
            {"type": "text", "text": json.dumps(
                {"ket_luan": "Đã lập phiếu sửa chữa, đã gửi duyệt điều kỹ thuật viên khẩn cấp",
                 "da_thuc_hien": ["Lập phiếu sửa chữa"], "de_xuat": [],
                 "can_them_agent": ["an_ninh"], "can_hoi_them_nguoi_bao": None,
                 "nguon": ["quy_trinh_sua_chua.md"]}, ensure_ascii=False)},
        ],
        "an_ninh": [
            {"type": "text", "text": json.dumps(
                {"ket_luan": "Không ghi nhận yếu tố an ninh liên quan",
                 "da_thuc_hien": [], "de_xuat": [], "can_them_agent": [],
                 "can_hoi_them_nguoi_bao": None, "nguon": []}, ensure_ascii=False)},
        ],
    }
    stubs = {name: StubChatClient(name, steps) for name, steps in script.items()}
    maf_room.MafRoom._chat_client = (  # type: ignore[method-assign]
        lambda self, agent_row: stubs[agent_row["id"]]
    )

    # Scripted router: ky_thuat -> an_ninh -> stop. Replaces the LLM orchestrator.
    plan = [("goi_agent", "ky_thuat"), ("goi_agent", "an_ninh"), ("ket_thuc", "")]
    calls = {"n": 0}

    from backend.core.orchestrator import RouterDecision

    def fake_decide(self, **kw):  # noqa: ANN001
        i = min(calls["n"], len(plan) - 1)
        calls["n"] += 1
        action, agent_id = plan[i]
        return RouterDecision(hanh_dong=action, agent_id=agent_id,
                              chi_dan="Xử lý phần việc của bạn", ly_do="kịch bản spike")

    from backend.core.orchestrator import Orchestrator

    Orchestrator.decide = fake_decide  # type: ignore[method-assign]

    # Receptionist mode B also needs an LLM; stub it out.
    from backend.core.receptionist import Receptionist

    Receptionist.summarize = lambda self, *a, **k: "(tin nhắn cho người báo — bỏ qua trong spike)"  # type: ignore

    result = maf_room.run_room_maf(ticket_id)

    # ---------------------------------------------------------------- assertions
    print()
    check("GroupChat workflow chạy hết không lỗi", "loi" not in result, result.get("loi", ""))
    check("Đúng 2 lượt agent", result.get("so_luot") == 2, f"so_luot={result.get('so_luot')}")
    speakers = [t["agent_id"] for t in result.get("transcript", [])]
    check("Điều phối gọi đúng thứ tự", speakers == ["ky_thuat", "an_ninh"], str(speakers))

    events = load_events(ticket_id)
    types = [e["type"] for e in events]
    for t in ("room_start", "router_decision", "agent_start", "rag_hits",
              "tool_call", "tool_result", "action_pending", "agent_output", "room_end"):
        check(f"phát sự kiện '{t}'", t in types, f"x{types.count(t)}")

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    injected = next((e for e in tool_calls if e["payload"]["tool"] == "tao_phieu_sua_chua"), None)
    check(
        "context param bị ghi đè (LLM gửi HACK-9999)",
        bool(injected) and injected["payload"]["args"].get("ma_can_ho") == "S1-1203",
        f"ma_can_ho={injected['payload']['args'].get('ma_can_ho') if injected else '?'}",
    )

    with session_scope() as s:
        acts = [(a.tool, a.status) for a in s.exec(select_actions(ticket_id)).all()]
    check("tool cần duyệt KHÔNG tự chạy",
          len(acts) == 1 and all(st == "cho_duyet" for _, st in acts), str(acts))
    check("ticket chuyển sang cho_duyet", result.get("trang_thai") == "cho_duyet", result.get("trang_thai", ""))

    # FunctionTool schema really hides context params
    spec = get_catalog().get("dieu_ktv_khan_cap")
    from agent_framework import FunctionTool

    ft = FunctionTool(name=spec.name, description=spec.manager_description,
                      func=lambda **k: "{}", input_model=spec.llm_schema())
    props = set((ft.parameters() or {}).get("properties", {}))
    check("FunctionTool ẩn context param khỏi schema", "ma_can_ho" not in props, f"props={sorted(props)}")

    print()
    failed = [n for n, ok, _ in CHECKS if not ok]
    print("=" * 70)
    if failed:
        print(f"KẾT QUẢ: {len(CHECKS) - len(failed)}/{len(CHECKS)} PASS. Lỗi: {failed}")
        return 1
    print(f"KẾT QUẢ: {len(CHECKS)}/{len(CHECKS)} PASS — GroupChat của Agent Framework chạy được.")
    return 0


def select_actions(ticket_id: str):
    from sqlmodel import select

    return select(Action).where(Action.ticket_id == ticket_id)


if __name__ == "__main__":
    raise SystemExit(main())
