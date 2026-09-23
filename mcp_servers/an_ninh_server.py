"""MCP server mô phỏng hệ thống của đội an ninh.

Cùng vai trò với `ky_thuat_server.py`: đây là hệ thống của MỘT BÊN KHÁC cắm vào
platform qua MCP. Ba tool trong chuỗi xác nhận (điều tổ, tiếp nhận, báo xong) đều
được khai `approval_role` trong `backend/tools/catalog.yaml` — server này không
biết gì về việc duyệt, nó chỉ thực thi khi được gọi.

Chạy: python mcp_servers/an_ninh_server.py
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
DOMAIN = os.getenv("DOMAIN_ID", "vinhomes")
CAMERA = BASE_DIR / "domains" / DOMAIN / "mock_data" / "nhat_ky_camera.json"
STORE = BASE_DIR / "data" / "mcp_an_ninh_records.json"

PORT = int(os.getenv("MCP_AN_NINH_PORT", "8102"))
mcp = MCPServer("an_ninh")

CA_TRUC = [
    {"ma_to": "AN-A", "ten_to": "Tổ tuần tra khu S1-S2", "ca": "06:00-14:00", "quan_so": 4},
    {"ma_to": "AN-B", "ten_to": "Tổ tuần tra khu S3 + hầm xe", "ca": "14:00-22:00", "quan_so": 3},
    {"ma_to": "AN-C", "ten_to": "Tổ trực đêm toàn khu", "ca": "22:00-06:00", "quan_so": 3},
]


def _new_code(prefix: str, *, sandbox: bool = False) -> str:
    return f"{'EVAL' if sandbox else prefix}-{uuid.uuid4().hex[:6].upper()}"


def _save(kind: str, code: str, payload: dict, *, sandbox: bool = False) -> None:
    if sandbox:
        return  # chạy thử/đánh giá không để lại tác dụng phụ thật
    STORE.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
    rows.append({"ma": code, "loai": kind, "thoi_diem": datetime.now().isoformat(timespec="seconds"), **payload})
    STORE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def _find(code: str) -> dict | None:
    if not STORE.exists():
        return None
    return next((r for r in json.loads(STORE.read_text(encoding="utf-8")) if r["ma"] == code), None)


@mcp.tool()
def tra_ca_truc_an_ninh(
    khu_vuc: Annotated[str | None, Field(description="Lọc theo khu vực, ví dụ S1, hầm xe. Bỏ trống là tất cả")] = None,
) -> dict:
    """Xem các tổ an ninh đang trực và ca trực của họ."""
    rows = CA_TRUC if not khu_vuc else [c for c in CA_TRUC if khu_vuc.lower() in c["ten_to"].lower()]
    return {"khu_vuc": khu_vuc or "tat_ca", "so_to": len(rows), "cac_to": rows}


@mcp.tool()
def dieu_to_an_ninh(
    ma_can_ho: Annotated[str, Field(description="Mã căn hộ của người báo (hệ thống tự điền)")],
    vi_tri: Annotated[str, Field(description="Vị trí cần có mặt, ví dụ sảnh S2, hầm B1")],
    ly_do: Annotated[str, Field(description="Lý do điều tổ an ninh")],
    muc_do: Annotated[str, Field(description="khan_cap | binh_thuong")] = "binh_thuong",
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Điều một tổ an ninh tới hiện trường."""
    if muc_do not in {"khan_cap", "binh_thuong"}:
        return {"loi": "muc_do phải là khan_cap hoặc binh_thuong"}
    to = CA_TRUC[0] if muc_do == "khan_cap" else CA_TRUC[1]
    code = _new_code("ANL", sandbox=sandbox)
    payload = {
        "ma_can_ho": ma_can_ho, "vi_tri": vi_tri, "ly_do": ly_do, "muc_do": muc_do,
        "ma_to": to["ma_to"], "ten_to": to["ten_to"], "quan_so": to["quan_so"],
        "cam_ket_co_mat_truoc": (datetime.now() + timedelta(minutes=15 if muc_do == "khan_cap" else 45)).strftime("%H:%M"),
        "trang_thai": "da_dieu",
    }
    _save("dieu_to_an_ninh", code, payload, sandbox=sandbox)
    return {"ma_lenh_dieu": code, **payload}


@mcp.tool()
def an_ninh_xac_nhan_tiep_nhan(
    ma_lenh_dieu: Annotated[str, Field(description="Mã lệnh điều tổ an ninh cần xác nhận")],
    ghi_chu: Annotated[str, Field(description="Ghi chú của tổ khi nhận việc")] = "",
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Tổ an ninh xác nhận đã tiếp nhận lệnh điều và đang trên đường."""
    lenh = _find(ma_lenh_dieu)
    if lenh is None and not sandbox:
        return {"loi": f"Không tìm thấy lệnh điều '{ma_lenh_dieu}'"}
    code = _new_code("ANX", sandbox=sandbox)
    payload = {"ma_lenh_dieu": ma_lenh_dieu, "ma_to": (lenh or {}).get("ma_to", ""),
               "ghi_chu": ghi_chu, "trang_thai": "da_tiep_nhan"}
    _save("an_ninh_tiep_nhan", code, payload, sandbox=sandbox)
    return {"ma_xac_nhan": code, **payload}


@mcp.tool()
def an_ninh_bao_hoan_thanh(
    ma_lenh_dieu: Annotated[str, Field(description="Mã lệnh điều tổ an ninh đã xử lý xong")],
    ket_qua: Annotated[str, Field(description="Mô tả kết quả xử lý tại hiện trường")],
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Tổ an ninh báo đã xử lý xong việc tại hiện trường."""
    lenh = _find(ma_lenh_dieu)
    if lenh is None and not sandbox:
        return {"loi": f"Không tìm thấy lệnh điều '{ma_lenh_dieu}'"}
    code = _new_code("ANH", sandbox=sandbox)
    payload = {"ma_lenh_dieu": ma_lenh_dieu, "ket_qua": ket_qua, "trang_thai": "da_hoan_thanh",
               "thoi_diem_hoan_thanh": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save("an_ninh_hoan_thanh", code, payload, sandbox=sandbox)
    return {"ma_bao_cao": code, **payload}


if __name__ == "__main__":
    print(f"MCP server 'an_ninh' chạy streamable HTTP tại http://127.0.0.1:{PORT}/mcp", file=sys.stderr)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=PORT)
