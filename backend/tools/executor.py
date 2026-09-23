"""ToolExecutor: the single gate every tool call goes through.

Three platform guarantees live here, not in any prompt:
  1. an agent can only call tools it was granted;
  2. context params are injected from the ticket and overwrite anything the LLM sent;
  3. tools marked `requires_approval` never execute — they create a pending Action
     and, when the caller asks for it (`ToolContext.wait_for_approval`), the room
     turn blocks right here until a manager decides.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from backend.app.config import settings
from backend.app.db import session_scope
from backend.app.events import bus
from backend.app.models import Action, Ticket
from backend.tools.approval_gate import gate
from backend.tools.catalog import ToolSpec, get_catalog
from backend.tools.local_tools import get_local_tool
from backend.tools.mcp_client import mcp_client

logger = logging.getLogger("executor")


@dataclass
class ToolContext:
    """Values the platform injects; an LLM can never set these."""

    ticket_id: str = ""
    ma_can_ho: str = ""
    ma_cu_dan: str = ""
    agent_id: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    sandbox: bool = False   # sandbox runs never create real Actions
    emit_events: bool = True
    # HITL: dừng lượt của agent tại chỗ cho tới khi quản lý duyệt/từ chối. Chỉ phòng
    # họp thật bật cờ này — sandbox và đánh giá không có ai ngồi duyệt, bật lên là treo.
    wait_for_approval: bool = False
    # Audit trail of what actually ran. Callers must report from here, never from
    # the raw LLM arguments, or an injected context param looks like it got through.
    trace: list[dict[str, Any]] = field(default_factory=list)

    def value_for(self, param: str) -> Any:
        return {
            "ticket_id": self.ticket_id,
            "ma_can_ho": self.ma_can_ho,
            "ma_cu_dan": self.ma_cu_dan,
            "sandbox": self.sandbox,
        }.get(param)


class ToolExecutor:
    def __init__(self) -> None:
        self.catalog = get_catalog()

    # ------------------------------------------------------------------ helpers
    def _emit(self, ctx: ToolContext, type_: str, payload: dict) -> None:
        if ctx.emit_events and ctx.ticket_id:
            bus.emit(ctx.ticket_id, type_, payload, actor=ctx.agent_id)

    @staticmethod
    def _inject(spec: ToolSpec, llm_args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], list[str]]:
        args = {k: v for k, v in (llm_args or {}).items()}
        overridden = []
        # "sandbox" is injected into every write tool that declares it, the same
        # way context_params are — it isn't listed per-tool in catalog.yaml because
        # every write tool needs it uniformly, not just some.
        hidden_params = set(spec.context_params) | (
            {"sandbox"} if "sandbox" in spec.raw_schema().get("properties", {}) else set()
        )
        for p in hidden_params:
            if p in args:
                overridden.append(p)
            args[p] = ctx.value_for(p)
        return args, overridden

    @staticmethod
    def _validate(spec: ToolSpec, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if spec.mcp_server:
            schema = spec.raw_schema()
            missing = [r for r in schema.get("required", []) if args.get(r) in (None, "")]
            if missing:
                return None, f"Thiếu tham số bắt buộc: {', '.join(missing)}"
            allowed = set(schema.get("properties", {}))
            return ({k: v for k, v in args.items() if k in allowed} if allowed else args), ""
        entry = get_local_tool(spec.name)
        if not entry:
            return None, f"Tool '{spec.name}' chưa được cài đặt"
        model_cls = entry[0]
        try:
            model = model_cls(**args)
        except ValidationError as exc:
            return None, f"Tham số không hợp lệ: {exc.errors()[:3]}"
        return model.model_dump(), ""

    # ------------------------------------------------------------------ main API
    def execute(self, tool_name: str, llm_args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        spec = self.catalog.get(tool_name)
        if spec is None:
            return {"loi": f"Không có tool tên '{tool_name}' trong catalog"}
        if ctx.allowed_tools and tool_name not in ctx.allowed_tools:
            logger.warning("agent=%s gọi tool ngoài quyền: %s", ctx.agent_id, tool_name)
            denied = {"loi": f"Bạn không được cấp tool '{tool_name}'. Chỉ dùng: {ctx.allowed_tools}"}
            self._trace(ctx, tool_name, llm_args, {}, denied, [])
            return denied
        if not spec.is_available():
            return {"loi": f"Tool '{tool_name}' hiện không khả dụng"}

        args, overridden = self._inject(spec, llm_args, ctx)
        if overridden:
            logger.info("ghi đè tham số hệ thống %s cho tool=%s agent=%s", overridden, tool_name, ctx.agent_id)

        validated, err = self._validate(spec, args)
        if validated is None:
            self._emit(ctx, "tool_result", {"tool": tool_name, "ket_qua": {"loi": err}})
            self._trace(ctx, tool_name, llm_args, args, {"loi": err}, overridden)
            return {"loi": err}

        self._emit(
            ctx,
            "tool_call",
            {
                "tool": tool_name,
                "provider": spec.provider,
                "args": validated,
                "context_params_da_tiem": spec.context_params,
                "tham_so_bi_ghi_de": overridden,
                "requires_approval": spec.requires_approval,
            },
        )

        if spec.requires_approval:
            result = self._park_for_approval(spec, validated, ctx)
            self._trace(ctx, tool_name, llm_args, validated, result, overridden)
            return result

        result = self._run(spec, validated)
        self._emit(ctx, "tool_result", {"tool": tool_name, "ket_qua": result})
        self._trace(ctx, tool_name, llm_args, validated, result, overridden)
        return result

    @staticmethod
    def _trace(ctx: ToolContext, tool: str, llm_args: dict, effective: dict,
               result: dict, overridden: list[str]) -> None:
        ctx.trace.append(
            {
                "tool": tool,
                "args": effective,              # what actually ran
                "args_llm_gui": llm_args,       # what the model asked for
                "tham_so_bi_ghi_de": overridden,
                "result": result,
            }
        )

    def _park_for_approval(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.sandbox:
            return {
                "status": "cho_duyet",
                "action_id": "SANDBOX",
                "ghi_chu": "Chạy thử: hành động sẽ được gửi quản lý duyệt, không thực thi thật",
            }
        action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
        with session_scope() as s:
            s.add(
                Action(
                    id=action_id,
                    ticket_id=ctx.ticket_id,
                    agent_id=ctx.agent_id,
                    tool=spec.name,
                    args=args,
                    status="cho_duyet",
                )
            )
        blocking = bool(ctx.wait_for_approval and settings.hitl_wait_for_approval and ctx.ticket_id)
        self._emit(
            ctx,
            "action_pending",
            {
                "action_id": action_id,
                "tool": spec.name,
                "args": args,
                "phong_hop_dang_cho": blocking,
                "han_cho_giay": settings.hitl_approval_timeout if blocking else 0,
            },
        )
        if not blocking:
            return {
                "status": "cho_duyet",
                "action_id": action_id,
                "ghi_chu": "Hành động đã gửi quản lý duyệt",
            }
        return self._wait_for_manager(spec, action_id, args, ctx)

    def _wait_for_manager(self, spec: ToolSpec, action_id: str, args: dict[str, Any],
                          ctx: ToolContext) -> dict[str, Any]:
        """Block this room turn until a manager decides, or the deadline passes.

        Chạy trong luồng nền của phòng họp, không phải luồng phục vụ HTTP, nên việc
        chờ ở đây không chặn API — bản thân nút Duyệt vẫn bấm được trong lúc chờ.
        """
        timeout = settings.hitl_approval_timeout
        waiter = gate.register(action_id, ticket_id=ctx.ticket_id, tool=spec.name, agent_id=ctx.agent_id)
        started = time.monotonic()
        _set_ticket_status(ctx.ticket_id, "cho_duyet")
        self._emit(
            ctx,
            "room_waiting",
            {
                "action_id": action_id,
                "tool": spec.name,
                "args": args,
                "han_cho_giay": timeout,
                "ghi_chu": f"Phòng họp tạm dừng, chờ quản lý duyệt '{spec.name}'",
            },
        )
        logger.info("phòng họp ticket=%s dừng chờ duyệt action=%s (tối đa %ss)",
                    ctx.ticket_id, action_id, timeout)
        try:
            waiter.decided.wait(timeout if timeout > 0 else None)
            # Lấy quyết định qua gate chứ không đọc waiter.decision: chỉ ở đây mới
            # chốt được "hết hạn" một cách an toàn với lệnh duyệt đến cùng lúc.
            decision = gate.abandon(action_id)
            if decision == "da_duyet":
                result = self.execute_approved(action_id)
                waiter.result = result
                out = {
                    "trang_thai_duyet": "da_duyet",
                    "action_id": action_id,
                    "ket_qua": result,
                    "ghi_chu": "Quản lý đã duyệt, hành động ĐÃ được thực thi. Hãy dùng dữ liệu trong 'ket_qua'.",
                }
            elif decision == "tu_choi":
                out = {
                    "trang_thai_duyet": "tu_choi",
                    "action_id": action_id,
                    "ghi_chu": "Quản lý TỪ CHỐI hành động này; nó KHÔNG được thực hiện. "
                               "Hãy nêu phương án thay thế hoặc kết luận rõ là việc này chưa được duyệt.",
                }
            else:
                _mark_timed_out(action_id)
                self._emit(
                    ctx,
                    "guard_triggered",
                    {"guard": "het_han_cho_duyet", "action_id": action_id, "tool": spec.name,
                     "han_cho_giay": timeout, "xu_ly": "chạy tiếp phòng họp, hành động vẫn nằm ở hàng chờ duyệt"},
                )
                out = {
                    "trang_thai_duyet": "cho_duyet",
                    "action_id": action_id,
                    "ghi_chu": f"Chờ quá {timeout:.0f} giây chưa có quyết định của quản lý. Hành động vẫn "
                               f"nằm ở hàng chờ duyệt; hãy kết luận theo hướng việc này còn chờ duyệt.",
                }
        finally:
            # Luôn mở chốt: bên duyệt đang chờ kết quả, một ngoại lệ ở đây mà không
            # set thì endpoint duyệt treo tới hết timeout của nó.
            waiter.executed.set()
            gate.abandon(action_id)

        waited = int(time.monotonic() - started)
        _set_ticket_status(ctx.ticket_id, "dang_xu_ly")
        self._emit(
            ctx,
            "room_resumed",
            {
                "action_id": action_id,
                "tool": spec.name,
                "quyet_dinh": out["trang_thai_duyet"],
                "cho_bao_lau_giay": waited,
            },
        )
        logger.info("phòng họp ticket=%s chạy tiếp sau %ss, quyết định=%s",
                    ctx.ticket_id, waited, out["trang_thai_duyet"])
        return out

    def _run(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if spec.mcp_server:
                return mcp_client.call(spec.mcp_server, spec.name, args)
            entry = get_local_tool(spec.name)
            model_cls, fn = entry  # type: ignore[misc]
            return fn(model_cls(**args))
        except Exception as exc:  # noqa: BLE001 - a tool failure must never kill the room
            logger.exception("Tool '%s' lỗi", spec.name)
            return {"loi": f"Tool '{spec.name}' gặp lỗi: {str(exc)[:200]}"}

    def execute_approved(self, action_id: str) -> dict[str, Any]:
        """Run a tool that a manager approved, bypassing the approval gate."""
        with session_scope() as s:
            action = s.get(Action, action_id)
            if action is None:
                return {"loi": "Không tìm thấy hành động"}
            spec = self.catalog.get(action.tool)
            args = dict(action.args)
            ticket_id, agent_id, tool_name = action.ticket_id, action.agent_id, action.tool
        if spec is None:
            return {"loi": f"Tool '{tool_name}' không còn trong catalog"}
        result = self._run(spec, args)
        ok = "loi" not in result
        with session_scope() as s:
            action = s.get(Action, action_id)
            action.status = "da_thuc_hien" if ok else "loi"
            action.result = result
            s.add(action)
        bus.emit(
            ticket_id,
            "action_executed",
            {"action_id": action_id, "tool": tool_name, "ket_qua": result, "thanh_cong": ok},
            actor=agent_id,
        )
        return result


def _set_ticket_status(ticket_id: str, status: str) -> None:
    """Phản ánh trạng thái chờ ra ngoài giao diện ngay lúc phòng họp dừng, thay vì
    chỉ khi phiên kết thúc. Phòng họp tự tính lại trạng thái cuối khi đóng phiên."""
    if not ticket_id:
        return
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if t and t.status != status:
            t.status = status
            t.updated_at = datetime.utcnow()
            s.add(t)


def _mark_timed_out(action_id: str) -> None:
    """Ghi lại việc phòng họp đã thôi chờ. Hành động vẫn ở 'cho_duyet' — quản lý duyệt
    muộn thì nó vẫn chạy, chỉ là chạy ngoài phiên họp."""
    with session_scope() as s:
        action = s.get(Action, action_id)
        if action is not None and action.status == "cho_duyet":
            result = dict(action.result or {})
            result["ghi_chu_he_thong"] = "Phòng họp đã hết hạn chờ và chạy tiếp; duyệt muộn vẫn thực thi được."
            action.result = result
            s.add(action)


_executor: ToolExecutor | None = None


def get_executor() -> ToolExecutor:
    global _executor
    if _executor is None:
        _executor = ToolExecutor()
    return _executor
