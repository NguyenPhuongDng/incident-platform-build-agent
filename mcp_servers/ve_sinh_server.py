"""MCP server mô phỏng hệ thống của công ty vệ sinh (nhà thầu bên ngoài).

Cùng khuôn với `ky_thuat_server.py` và `an_ninh_server.py`: một bên thứ ba cắm
vào platform qua MCP. Việc "ai phải duyệt tool nào" nằm ở `catalog.yaml`, không
nằm trong server này.

Chạy: python mcp_servers/ve_sinh_server.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

BASE_DIR = Path(__file__).resolve().parents[1]
STORE = BASE_DIR / "data" / "mcp_ve_sinh_records.json"

PORT = int(os.getenv("MCP_VE_SINH_PORT", "8103"))
mcp = MCPServer("ve_sinh")

TO_VE_SINH = [
    {"ma_to": "VS-1", "ten_to": "Tổ vệ sinh tòa S1", "ca": "06:00-14:00", "quan_so": 5},
    {"ma_to": "VS-2", "ten_to": "Tổ vệ sinh tòa S2-S3", "ca": "06:00-14:00", "quan_so": 6},
    {"ma_to": "VS-CD", "ten_to": "Tổ xử lý đột xuất (rác cồng kềnh, tràn đổ)", "ca": "08:00-20:00", "quan_so": 3},
]
DICH_VU = {
    "thu_gom_dot_xuat": "Thu gom rác đột xuất ngoài lịch",
    "tong_ve_sinh": "Tổng vệ sinh khu vực",
    "xu_ly_tran_do": "Xử lý tràn đổ, chất thải lỏng",
    "khu_mui": "Khử mùi, phun khử khuẩn",
}


def _new_code(prefix: str, *, sandbox: bool = False) -> str:
    return f"{'EVAL' if sandbox else prefix}-{uuid.uuid4().hex[:6].upper()}"


def _save(kind: str, code: str, payload: dict, *, sandbox: bool = False) -> None:
    if sandbox:
        return
    STORE.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
    rows.append({"ma": code, "loai": kind, "thoi_diem": datetime.now().isoformat(timespec="seconds"), **payload})
    STORE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def _find(code: str) -> dict | None:
    if not STORE.exists():
        return None
    return next((r for r in json.loads(STORE.read_text(encoding="utf-8")) if r["ma"] == code), None)


@mcp.tool()
def tra_to_ve_sinh(
    khu_vuc: Annotated[str | None, Field(description="Lọc theo khu vực/tòa, ví dụ S1. Bỏ trống là tất cả")] = None,
) -> dict:
    """Xem các tổ vệ sinh đang trực, ca làm và các loại dịch vụ đột xuất."""
    rows = TO_VE_SINH if not khu_vuc else [t for t in TO_VE_SINH if khu_vuc.lower() in t["ten_to"].lower()]
    return {"khu_vuc": khu_vuc or "tat_ca", "so_to": len(rows), "cac_to": rows, "dich_vu_dot_xuat": DICH_VU}


@mcp.tool()
def dieu_to_ve_sinh(
    vi_tri: Annotated[str, Field(description="Vị trí cần xử lý, ví dụ hành lang tầng 12 tòa S1")],
    loai_dich_vu: Annotated[str, Field(description="thu_gom_dot_xuat | tong_ve_sinh | xu_ly_tran_do | khu_mui")],
    ly_do: Annotated[str, Field(description="Lý do cần điều tổ ngoài lịch")],
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Điều một tổ vệ sinh đi xử lý ngoài lịch cố định."""
    if loai_dich_vu not in DICH_VU:
        return {"loi": f"loai_dich_vu phải thuộc: {', '.join(DICH_VU)}"}
    to = TO_VE_SINH[2] if loai_dich_vu in {"thu_gom_dot_xuat", "xu_ly_tran_do"} else TO_VE_SINH[0]
    code = _new_code("VSL", sandbox=sandbox)
    payload = {
        "vi_tri": vi_tri, "loai_dich_vu": loai_dich_vu, "ten_dich_vu": DICH_VU[loai_dich_vu],
        "ly_do": ly_do, "ma_to": to["ma_to"], "ten_to": to["ten_to"],
        "cam_ket_co_mat_truoc": (datetime.now() + timedelta(minutes=60)).strftime("%H:%M"),
        "trang_thai": "da_dieu",
    }
    _save("dieu_to_ve_sinh", code, payload, sandbox=sandbox)
    return {"ma_lenh_dieu": code, **payload}


@mcp.tool()
def ve_sinh_xac_nhan_tiep_nhan(
    ma_lenh_dieu: Annotated[str, Field(description="Mã lệnh điều tổ vệ sinh cần xác nhận")],
    ghi_chu: Annotated[str, Field(description="Ghi chú của tổ khi nhận việc")] = "",
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Tổ vệ sinh xác nhận đã tiếp nhận lệnh điều."""
    lenh = _find(ma_lenh_dieu)
    if lenh is None and not sandbox:
        return {"loi": f"Không tìm thấy lệnh điều '{ma_lenh_dieu}'"}
    code = _new_code("VSX", sandbox=sandbox)
    payload = {"ma_lenh_dieu": ma_lenh_dieu, "ma_to": (lenh or {}).get("ma_to", ""),
               "ghi_chu": ghi_chu, "trang_thai": "da_tiep_nhan"}
    _save("ve_sinh_tiep_nhan", code, payload, sandbox=sandbox)
    return {"ma_xac_nhan": code, **payload}


@mcp.tool()
def ve_sinh_bao_hoan_thanh(
    ma_lenh_dieu: Annotated[str, Field(description="Mã lệnh điều tổ vệ sinh đã xử lý xong")],
    ket_qua: Annotated[str, Field(description="Mô tả kết quả xử lý tại hiện trường")],
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Tổ vệ sinh báo đã xử lý xong."""
    lenh = _find(ma_lenh_dieu)
    if lenh is None and not sandbox:
        return {"loi": f"Không tìm thấy lệnh điều '{ma_lenh_dieu}'"}
    code = _new_code("VSH", sandbox=sandbox)
    payload = {"ma_lenh_dieu": ma_lenh_dieu, "ket_qua": ket_qua, "trang_thai": "da_hoan_thanh",
               "thoi_diem_hoan_thanh": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save("ve_sinh_hoan_thanh", code, payload, sandbox=sandbox)
    return {"ma_bao_cao": code, **payload}


if __name__ == "__main__":
    print(f"MCP server 've_sinh' chạy streamable HTTP tại http://127.0.0.1:{PORT}/mcp", file=sys.stderr)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=PORT)
