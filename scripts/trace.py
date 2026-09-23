"""Đọc trace LLM đã ghi ở data/trace/*.jsonl.

Chạy: python scripts/trace.py                       # tóm tắt các phiên trong ngày
      python scripts/trace.py --ticket EVAL-3D3BE4E8 # các lượt của một ticket
      python scripts/trace.py --case EC-C31726CD -v  # kèm nguyên văn prompt + trả lời
      python scripts/trace.py --role dieu_phoi --errors
      python scripts/trace.py --ngay 2026-09-22
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
TRACE_DIR = BASE_DIR / "data" / "trace"


def doc(ngay: str) -> list[dict]:
    f = TRACE_DIR / f"{ngay}.jsonl"
    if not f.exists():
        sys.exit(f"Không có trace cho ngày {ngay} ({f})")
    ban_ghi = []
    for dong in f.read_text(encoding="utf-8").splitlines():
        if not dong.strip():
            continue
        try:
            ban_ghi.append(json.loads(dong))
        except json.JSONDecodeError:
            # Dòng cuối có thể đứt dở nếu tiến trình chết giữa chừng — bỏ qua, phần
            # trước đó vẫn đọc được. Đây là lý do chọn JSONL thay vì một mảng JSON.
            continue
    return ban_ghi


def loc(ban_ghi: list[dict], args) -> list[dict]:
    def hop(r: dict) -> bool:
        ctx = r.get("ctx") or {}
        if args.ticket and ctx.get("ticket_id") != args.ticket:
            return False
        if args.case and ctx.get("case_id") != args.case:
            return False
        if args.role and r.get("role") != args.role:
            return False
        if args.errors and not r.get("error"):
            return False
        return True

    return [r for r in ban_ghi if hop(r)]


def tom_tat_phien(ban_ghi: list[dict]) -> None:
    phien: dict[str, list[dict]] = {}
    for r in ban_ghi:
        khoa = (r.get("ctx") or {}).get("ticket_id") or (r.get("ctx") or {}).get("case_id") or "(ngoài phiên)"
        phien.setdefault(khoa, []).append(r)

    print(f"{len(ban_ghi)} lệnh gọi / {len(phien)} phiên\n")
    for khoa, rs in phien.items():
        ms = sum(r.get("ms") or 0 for r in rs)
        tok = sum(r.get("prompt_tokens") or 0 for r in rs)
        loi = sum(1 for r in rs if r.get("error"))
        vai = ", ".join(f"{v}x{n}" for v, n in Counter(r.get("role") for r in rs).items())
        canh = "  <-- CÓ LỖI" if loi else ""
        print(f"{khoa:22} {len(rs):3} lượt  {ms/1000:6.1f}s  {tok or '?':>7} tok  {vai}{canh}")


def in_chi_tiet(ban_ghi: list[dict], day_du: bool) -> None:
    for r in ban_ghi:
        ctx = r.get("ctx") or {}
        dau = f"[{r['ts'][11:19]}] {r.get('role')} ({r.get('model')}) {r.get('ms')}ms"
        if r.get("attempt", 1) > 1:
            dau += f" lần-thử-{r['attempt']}"
        if r.get("error"):
            dau += "  LỖI"
        print("=" * 100)
        print(dau, "|", " ".join(f"{k}={v}" for k, v in ctx.items()))
        if r.get("tool_calls"):
            for c in r["tool_calls"]:
                print(f"  -> gọi tool {c['tool']} {c['args']}")
        if r.get("error"):
            print("  LỖI:", r["error"][:400])
        if day_du:
            for m in r.get("messages") or []:
                print(f"  --- {m.get('role')} ---")
                print("  " + str(m.get("content") or "").replace("\n", "\n  "))
            print("  --- trả về ---")
            print("  " + str(r.get("response") or "").replace("\n", "\n  "))
        else:
            tl = (r.get("response") or "").replace("\n", " ")
            print("  trả về:", tl[:200])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ngay", default=f"{datetime.now():%Y-%m-%d}")
    p.add_argument("--ticket")
    p.add_argument("--case")
    p.add_argument("--role", help="dieu_phoi, le_tan, eval_judge, eval_generate, hoặc id agent")
    p.add_argument("--errors", action="store_true", help="chỉ hiện lượt lỗi")
    p.add_argument("-v", "--verbose", action="store_true", help="in nguyên văn prompt và trả lời")
    args = p.parse_args()

    ban_ghi = loc(doc(args.ngay), args)
    if not ban_ghi:
        print("Không có bản ghi nào khớp.")
        return
    if args.ticket or args.case or args.role or args.errors:
        in_chi_tiet(ban_ghi, args.verbose)
    else:
        tom_tat_phien(ban_ghi)


if __name__ == "__main__":
    main()
