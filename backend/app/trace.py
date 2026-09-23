"""Ghi lại nguyên văn từng lệnh gọi LLM ra file, để soi lại sau khi sự việc đã xảy ra.

Log hiện có chỉ ghi metadata (role, model, ms, tokens) nên khi một lượt cho kết quả lạ
thì không cách nào biết model đã *nhìn thấy* gì — muốn biết phải dựng lại trạng thái rồi
gọi lại, mà gọi lại thì model đã lấy mẫu khác. Trace này giữ cả messages gửi đi lẫn
nguyên văn trả về, nên một phiên chạy hỏng có thể mổ lại nguội.

Định dạng JSON Lines: mỗi lệnh gọi một dòng, chỉ ghi nối đuôi. Chọn nó thay vì một mảng
JSON vì phiên chạy có thể chết giữa chừng (đã gặp: lỗi 400 của DashScope giết cả phòng
họp) — file JSONL đứt dở vẫn đọc được tới dòng cuối, mảng JSON thì hỏng cả file.
"""
from __future__ import annotations

import json
import threading
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.config import settings

# Ngữ cảnh nghiệp vụ của lệnh gọi (ticket nào, case nào). `_call` trong qwen_client không
# hề biết những thứ này, mà thiếu chúng thì trace chỉ là một đống lệnh gọi rời rạc không
# ghép lại được thành một phiên. ContextVar chạy được cho cả async (phòng họp MAF) lẫn
# thread (Evaluator chạy nhiều case song song), miễn là đặt từ bên trong.
_ctx: ContextVar[dict[str, Any]] = ContextVar("trace_ctx", default={})

_lock = threading.Lock()


class scope:
    """Gắn ngữ cảnh cho mọi lệnh gọi LLM bên trong khối `with`."""

    def __init__(self, **fields: Any) -> None:
        self.fields = {k: v for k, v in fields.items() if v}
        self.token: Any = None

    def __enter__(self) -> "scope":
        self.token = _ctx.set({**_ctx.get(), **self.fields})
        return self

    def __exit__(self, *exc: Any) -> None:
        _ctx.reset(self.token)


def _path() -> Path:
    d = settings.data_dir / "trace"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now():%Y-%m-%d}.jsonl"


def write(kind: str, **payload: Any) -> None:
    if not settings.trace_llm:
        return
    record = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "kind": kind,
        "ctx": _ctx.get(),
        **payload,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        with _path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
