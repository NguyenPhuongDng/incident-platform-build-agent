"""MCP server mô phỏng hệ thống của bộ phận kỹ thuật.

Chạy độc lập với backend, giao tiếp qua streamable HTTP. Mục đích: chứng minh
tool của một nhóm khác cắm vào platform qua MCP mà không phải sửa code lõi.

Chạy: python mcp_servers/ky_thuat_server.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

BASE_DIR = Path(__file__).resolve().parents[1]
DATA = BASE_DIR / "domains" / os.getenv("DOMAIN_ID", "vinhomes") / "mock_data" / "lich_ktv.json"
STORE = BASE_DIR / "data" / "mcp_ky_thuat_records.json"

PORT = int(os.getenv("MCP_PORT", "8101"))
mcp = MCPServer("ky_thuat")


def _load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _save_record(kind: str, code: str, payload: dict, *, sandbox: bool = False) -> None:
    if sandbox:
        return  # đánh giá/sandbox không được để lại tác dụng phụ thật nào
    STORE.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
    rows.append({"ma": code, "loai": kind, "thoi_diem": datetime.now().isoformat(timespec="seconds"), **payload})
    STORE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def _new_code(prefix: str, *, sandbox: bool = False) -> str:
    """Cùng quy ước với backend/tools/local_tools.py: chạy sandbox/đánh giá luôn
    trả mã có tiền tố EVAL-, không bao giờ lẫn với mã phiếu thật."""
    return f"{'EVAL' if sandbox else prefix}-{uuid.uuid4().hex[:6].upper()}"


@mcp.tool()
def tra_lich_ktv(
    ngay: Annotated[str | None, Field(description="Ngày cần tra dạng YYYY-MM-DD. Bỏ trống là hôm nay")] = None,
    chuyen_mon: Annotated[str | None, Field(description="Lọc theo chuyên môn: dien_nuoc | dieu_hoa | thang_may")] = None,
) -> dict:
    """Xem lịch trống của kỹ thuật viên theo ngày và chuyên môn."""
    data = _load()
    ngay = ngay or date.today().isoformat()
    rows = [r for r in data["lich"] if r["ngay"] == ngay]
    if chuyen_mon:
        rows = [r for r in rows if r["chuyen_mon"] == chuyen_mon]
    if not rows:
        ngay_co = sorted({r["ngay"] for r in data["lich"]})
        return {"ngay": ngay, "so_slot_trong": 0, "ghi_chu": "Không có lịch cho ngày này", "ngay_co_du_lieu": ngay_co}
    trong = [r for r in rows if r["trang_thai"] == "trong"]
    return {
        "ngay": ngay,
        "chuyen_mon": chuyen_mon or "tat_ca",
        "so_slot_trong": len(trong),
        "slot_trong": trong[:12],
    }


@mcp.tool()
def tao_phieu_sua_chua(
    ma_can_ho: Annotated[str, Field(description="Mã căn hộ của người báo (hệ thống tự điền)")],
    mo_ta_su_co: Annotated[str, Field(description="Mô tả sự cố cần sửa")],
    chuyen_mon: Annotated[str, Field(description="Chuyên môn cần: dien_nuoc | dieu_hoa | thang_may")],
    muc_do: Annotated[str, Field(description="khan_cap | binh_thuong | thap")] = "binh_thuong",
    khung_gio_hen: Annotated[str | None, Field(description="Khung giờ hẹn, ví dụ 2026-09-21 13:30-15:30")] = None,
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Tạo phiếu sửa chữa cho căn hộ của người báo."""
    if muc_do not in {"khan_cap", "binh_thuong", "thap"}:
        return {"loi": "muc_do phải là khan_cap, binh_thuong hoặc thap"}
    code = _new_code("PSC", sandbox=sandbox)
    sla = {"khan_cap": "4 giờ", "binh_thuong": "24 giờ làm việc", "thap": "3 ngày làm việc"}[muc_do]
    payload = {
        "ma_can_ho": ma_can_ho,
        "mo_ta_su_co": mo_ta_su_co,
        "chuyen_mon": chuyen_mon,
        "muc_do": muc_do,
        "khung_gio_hen": khung_gio_hen,
        "han_hoan_tat": sla,
    }
    _save_record("phieu_sua_chua", code, payload, sandbox=sandbox)
    return {"ma_phieu": code, "trang_thai": "da_tao", **payload}


@mcp.tool()
def dieu_ktv_khan_cap(
    ma_can_ho: Annotated[str, Field(description="Mã căn hộ của người báo (hệ thống tự điền)")],
    chuyen_mon: Annotated[str, Field(description="Chuyên môn cần: dien_nuoc | dieu_hoa | thang_may")],
    ly_do: Annotated[str, Field(description="Lý do cần điều khẩn cấp ngoài lịch")],
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Điều kỹ thuật viên đi xử lý khẩn cấp ngoài lịch."""
    data = _load()
    ung_vien = [k for k in data["ky_thuat_vien"] if k["chuyen_mon"] == chuyen_mon]
    if not ung_vien:
        return {"loi": f"Không có kỹ thuật viên chuyên môn {chuyen_mon}"}
    chon = ung_vien[0]
    code = _new_code("DKC", sandbox=sandbox)
    den_truoc = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
    payload = {
        "ma_can_ho": ma_can_ho,
        "ma_ktv": chon["ma_ktv"],
        "ho_ten_ktv": chon["ho_ten"],
        "chuyen_mon": chuyen_mon,
        "ly_do": ly_do,
        "cam_ket_co_mat_truoc": den_truoc,
    }
    _save_record("dieu_khan_cap", code, payload, sandbox=sandbox)
    return {"ma_lenh_dieu": code, "trang_thai": "da_dieu", **payload}


def _find_record(code: str) -> dict | None:
    if not STORE.exists():
        return None
    return next((r for r in json.loads(STORE.read_text(encoding="utf-8")) if r["ma"] == code), None)


@mcp.tool()
def ktv_xac_nhan_tiep_nhan(
    ma_lenh: Annotated[str, Field(description="Mã lệnh điều KTV hoặc mã phiếu sửa chữa cần xác nhận")],
    ma_ktv: Annotated[str, Field(description="Mã kỹ thuật viên nhận việc")] = "",
    ghi_chu: Annotated[str, Field(description="Ghi chú khi nhận việc, ví dụ thời gian có mặt dự kiến")] = "",
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Kỹ thuật viên xác nhận đã tiếp nhận lệnh và sẽ tới hiện trường."""
    lenh = _find_record(ma_lenh)
    if lenh is None and not sandbox:
        return {"loi": f"Không tìm thấy lệnh/phiếu '{ma_lenh}'"}
    code = _new_code("KTX", sandbox=sandbox)
    payload = {"ma_lenh": ma_lenh, "ma_ktv": ma_ktv or (lenh or {}).get("ma_ktv", ""),
               "ghi_chu": ghi_chu, "trang_thai": "da_tiep_nhan"}
    _save_record("ktv_tiep_nhan", code, payload, sandbox=sandbox)
    return {"ma_xac_nhan": code, **payload}


@mcp.tool()
def ktv_bao_hoan_thanh(
    ma_lenh: Annotated[str, Field(description="Mã lệnh điều KTV hoặc mã phiếu sửa chữa đã làm xong")],
    ket_qua: Annotated[str, Field(description="Đã làm gì, thay vật tư gì, còn tồn tại gì không")],
    vat_tu_da_thay: Annotated[str, Field(description="Danh sách vật tư đã thay, để trống nếu không có")] = "",
    sandbox: Annotated[bool, Field(description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")] = False,
) -> dict:
    """Kỹ thuật viên báo đã sửa xong, chờ người báo nghiệm thu."""
    lenh = _find_record(ma_lenh)
    if lenh is None and not sandbox:
        return {"loi": f"Không tìm thấy lệnh/phiếu '{ma_lenh}'"}
    code = _new_code("KTH", sandbox=sandbox)
    payload = {"ma_lenh": ma_lenh, "ket_qua": ket_qua, "vat_tu_da_thay": vat_tu_da_thay,
               "trang_thai": "da_hoan_thanh",
               "thoi_diem_hoan_thanh": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save_record("ktv_hoan_thanh", code, payload, sandbox=sandbox)
    return {"ma_bao_cao": code, **payload}


if __name__ == "__main__":
    print(f"MCP server 'ky_thuat' chạy streamable HTTP tại http://127.0.0.1:{PORT}/mcp", file=sys.stderr)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=PORT)
