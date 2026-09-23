"""Phase 0 preflight: verify the configured Qwen models actually work.

Checks, in order: plain chat -> function calling -> JSON mode -> embeddings.
Run: python scripts/check_qwen.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import settings  # noqa: E402
from backend.llm import qwen_client as q  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def check_chat() -> None:
    try:
        text = q.chat(
            [{"role": "user", "content": "Trả lời đúng một từ: xin chào bằng tiếng Anh là gì?"}],
            role="check",
            max_tokens=32,
        )
        record(f"chat ({settings.chat_model})", bool(text.strip()), text.strip()[:80])
    except Exception as exc:  # noqa: BLE001
        record(f"chat ({settings.chat_model})", False, str(exc)[:300])


def check_function_calling() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "tra_thoi_tiet",
                "description": "Tra thời tiết theo thành phố",
                "parameters": {
                    "type": "object",
                    "properties": {"thanh_pho": {"type": "string"}},
                    "required": ["thanh_pho"],
                },
            },
        }
    ]
    try:
        msg = q.chat_with_tools(
            [{"role": "user", "content": "Thời tiết Hà Nội hôm nay thế nào? Hãy dùng tool."}],
            tools,
            role="check",
        )
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            c = calls[0]
            record(
                f"function calling ({settings.chat_model})",
                True,
                f"{c.function.name}({c.function.arguments})",
            )
        else:
            record(f"function calling ({settings.chat_model})", False, "model không gọi tool")
    except Exception as exc:  # noqa: BLE001
        record(f"function calling ({settings.chat_model})", False, str(exc)[:300])


def check_json_mode(model: str, label: str) -> None:
    try:
        data = q.chat_json(
            [
                {
                    "role": "system",
                    "content": 'Chỉ trả về JSON dạng {"hanh_dong": string, "ly_do": string}.',
                },
                {"role": "user", "content": "Chọn hành động 'ket_thuc' và nêu lý do ngắn."},
            ],
            role="check",
            model=model,
        )
        ok = isinstance(data, dict) and "hanh_dong" in data
        record(f"json mode ({label}={model})", ok, json.dumps(data, ensure_ascii=False)[:120])
    except Exception as exc:  # noqa: BLE001
        record(f"json mode ({label}={model})", False, str(exc)[:300])


def check_embed() -> None:
    try:
        vecs = q.embed(["Quy trình sửa chữa khẩn cấp", "Bảng phí dịch vụ tòa nhà"], role="check")
        dim = len(vecs[0]) if vecs else 0
        ok = dim == settings.embed_dim
        record(
            f"embedding ({settings.embed_model})",
            ok,
            f"n={len(vecs)} dim={dim} (kỳ vọng {settings.embed_dim})",
        )
    except Exception as exc:  # noqa: BLE001
        record(f"embedding ({settings.embed_model})", False, str(exc)[:300])


def main() -> int:
    print("=" * 70)
    print(f"base_url     : {settings.qwen_base_url}")
    print(f"chat model   : {settings.chat_model}")
    print(f"router model : {settings.router_model}")
    print(f"embed model  : {settings.embed_model} (dim={settings.embed_dim})")
    print(f"api key      : {'(có)' if settings.qwen_api_key else '(THIẾU)'}")
    print("=" * 70)
    if not settings.qwen_api_key:
        print("\nChưa có QWEN_API_KEY trong .env — không thể kiểm tra.")
        return 2

    check_chat()
    check_function_calling()
    check_json_mode(settings.chat_model, "chat")
    if settings.router_model != settings.chat_model:
        check_json_mode(settings.router_model, "router")
    check_embed()

    print("=" * 70)
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"KẾT QUẢ: {len(results) - len(failed)}/{len(results)} PASS. Lỗi ở: {', '.join(failed)}")
        return 1
    print(f"KẾT QUẢ: {len(results)}/{len(results)} PASS — cấu hình Qwen dùng được.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
