"""Mock tools that run in-process.

Every tool declares its arguments with a Pydantic model; the catalog derives the
JSON schema from that model, so schemas never drift from the implementation.
Results are deterministic: they come from the domain's mock_data JSON files.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, Field

from backend.app.db import session_scope
from backend.app.models import MockRecord, Ticket
from backend.domain.loader import load_mock

ToolFn = Callable[[BaseModel], dict[str, Any]]

_REGISTRY: dict[str, tuple[type[BaseModel], ToolFn]] = {}


def tool(name: str, args_model: type[BaseModel]):
    def deco(fn: ToolFn) -> ToolFn:
        _REGISTRY[name] = (args_model, fn)
        return fn

    return deco


def get_local_tool(name: str) -> tuple[type[BaseModel], ToolFn] | None:
    return _REGISTRY.get(name)


def local_tool_names() -> list[str]:
    return list(_REGISTRY)


def _new_code(prefix: str, *, sandbox: bool = False) -> str:
    """A real record uses its normal prefix (PSC, VS, BB...). A sandbox run — the
    Evaluator, or `/api/sandbox/*` — always gets an unmistakable "EVAL-" prefix
    instead, so no fake receipt can ever be confused with a real one."""
    prefix = "EVAL" if sandbox else prefix
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


def _save_record(kind: str, code: str, data: dict[str, Any], *, sandbox: bool = False) -> None:
    if sandbox:
        return  # đánh giá không được để lại tác dụng phụ thật nào
    with session_scope() as s:
        s.add(MockRecord(id=code, kind=kind, data=data))


# --------------------------------------------------------------------------- platform


class CapNhatTrangThaiArgs(BaseModel):
    ticket_id: str = Field(description="Mã phản ánh (hệ thống tự điền)")
    trang_thai: str = Field(description="Trạng thái mới: dang_xu_ly | cho_duyet | cho_cu_dan | hoan_tat")
    ghi_chu: str = Field(default="", description="Ghi chú ngắn kèm theo")


@tool("cap_nhat_trang_thai_ticket", CapNhatTrangThaiArgs)
def cap_nhat_trang_thai_ticket(a: CapNhatTrangThaiArgs) -> dict:
    hop_le = {"dang_tiep_nhan", "dang_xu_ly", "cho_duyet", "cho_cu_dan", "hoan_tat"}
    if a.trang_thai not in hop_le:
        return {"loi": f"Trạng thái không hợp lệ. Chọn một trong: {sorted(hop_le)}"}
    with session_scope() as s:
        t = s.get(Ticket, a.ticket_id)
        if not t:
            return {"loi": "Không tìm thấy phản ánh"}
        t.status = a.trang_thai
        t.updated_at = datetime.utcnow()
        s.add(t)
    return {"ok": True, "ticket_id": a.ticket_id, "trang_thai": a.trang_thai, "ghi_chu": a.ghi_chu}


class ThongBaoNoiBoArgs(BaseModel):
    ticket_id: str = Field(description="Mã phản ánh (hệ thống tự điền)")
    bo_phan: str = Field(description="Tên bộ phận nhận thông báo")
    noi_dung: str = Field(description="Nội dung thông báo")
    sandbox: bool = Field(default=False, description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")


@tool("gui_thong_bao_noi_bo", ThongBaoNoiBoArgs)
def gui_thong_bao_noi_bo(a: ThongBaoNoiBoArgs) -> dict:
    code = _new_code("TB", sandbox=a.sandbox)
    _save_record("thong_bao_noi_bo", code, a.model_dump(), sandbox=a.sandbox)
    return {"ma_thong_bao": code, "bo_phan": a.bo_phan, "trang_thai": "da_gui"}


# --------------------------------------------------------------------------- domain


class MaCuDanArgs(BaseModel):
    ma_cu_dan: str = Field(description="Mã người gửi phản ánh (hệ thống tự điền)")


@tool("tra_thong_tin_cu_dan", MaCuDanArgs)
def tra_thong_tin_cu_dan(a: MaCuDanArgs) -> dict:
    for r in load_mock("cu_dan.json"):
        if r["ma_cu_dan"] == a.ma_cu_dan:
            return r
    return {"loi": "Không tìm thấy hồ sơ"}


class MaCanHoArgs(BaseModel):
    ma_can_ho: str = Field(description="Mã căn hộ của người báo (hệ thống tự điền)")


@tool("tra_can_ho", MaCanHoArgs)
def tra_can_ho(a: MaCanHoArgs) -> dict:
    for r in load_mock("can_ho.json"):
        if r["ma_can_ho"] == a.ma_can_ho:
            return r
    return {"loi": "Không tìm thấy căn hộ"}


class TraPhiArgs(BaseModel):
    ma_can_ho: str = Field(description="Mã căn hộ của người báo (hệ thống tự điền)")
    thang: str | None = Field(default=None, description="Tháng cần tra, dạng YYYY-MM. Bỏ trống để lấy cả 3 tháng gần nhất")


@tool("tra_phi_dich_vu", TraPhiArgs)
def tra_phi_dich_vu(a: TraPhiArgs) -> dict:
    rows = [r for r in load_mock("phi_dich_vu.json") if r["ma_can_ho"] == a.ma_can_ho]
    if a.thang:
        rows = [r for r in rows if r["thang"] == a.thang]
    rows.sort(key=lambda r: r["thang"])
    return {"ma_can_ho": a.ma_can_ho, "so_ban_ghi": len(rows), "chi_tiet": rows}


# --------------------------------------------------------------------------- business


@tool("tra_cong_no", MaCanHoArgs)
def tra_cong_no(a: MaCanHoArgs) -> dict:
    for r in load_mock("cong_no.json"):
        if r["ma_can_ho"] == a.ma_can_ho:
            return r
    return {"loi": "Không tìm thấy công nợ"}


class MienGiamArgs(BaseModel):
    ma_can_ho: str = Field(description="Mã căn hộ của người báo (hệ thống tự điền)")
    khoan_phi: str = Field(description="Khoản phí đề xuất miễn giảm, ví dụ: phí dịch vụ tháng 9/2026")
    ty_le_phan_tram: float = Field(description="Tỷ lệ miễn giảm, từ 0 đến 100")
    ly_do: str = Field(description="Lý do đề xuất, dẫn chiếu quy định nếu có")
    sandbox: bool = Field(default=False, description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")


@tool("mien_giam_phi", MienGiamArgs)
def mien_giam_phi(a: MienGiamArgs) -> dict:
    if not 0 < a.ty_le_phan_tram <= 100:
        return {"loi": "Tỷ lệ miễn giảm phải trong khoảng 0-100"}
    code = _new_code("MG", sandbox=a.sandbox)
    _save_record("mien_giam_phi", code, a.model_dump(), sandbox=a.sandbox)
    return {"ma_quyet_dinh": code, "trang_thai": "da_ap_dung", **a.model_dump(exclude={"sandbox"})}


class HangMucBaoGia(BaseModel):
    ten: str = Field(description="Tên vật tư hoặc hạng mục, lấy từ bảng đơn giá")
    so_luong: float = Field(description="Số lượng")
    don_vi: str = Field(default="cái", description="Đơn vị tính: cái, bộ, mét, kg, lần...")
    don_gia: float = Field(description="Đơn giá theo bảng giá niêm yết, đơn vị đồng")


class BaoGiaSuaChuaArgs(BaseModel):
    ticket_id: str = Field(description="Mã phản ánh (hệ thống tự điền)")
    loai: str = Field(description="du_kien = báo giá dự kiến (bước 1) | chot = giá cuối (bước 2)")
    hang_muc: list[HangMucBaoGia] = Field(default_factory=list, description="Danh mục vật tư")
    so_gio_cong: float = Field(default=0, description="Số giờ công")
    don_gia_gio_cong: float = Field(default=0, description="Đơn giá một giờ công theo bảng giá")
    ghi_chu: str = Field(default="", description="Ghi chú, ví dụ phạm vi chưa bao gồm")
    sandbox: bool = Field(default=False, description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")


@tool("lap_bao_gia_sua_chua", BaoGiaSuaChuaArgs)
def lap_bao_gia_sua_chua(a: BaoGiaSuaChuaArgs) -> dict:
    """Cộng tiền giúp LLM: model chọn hạng mục, tool tính tổng và tra ngưỡng duyệt."""
    if a.loai not in {"du_kien", "chot"}:
        return {"loi": "loai phải là 'du_kien' hoặc 'chot'"}
    tien_vat_tu = sum(h.so_luong * h.don_gia for h in a.hang_muc)
    tien_cong = a.so_gio_cong * a.don_gia_gio_cong
    tong = tien_vat_tu + tien_cong
    tham_quyen = (
        "ke_toan_tu_chot" if tong < 500_000
        else "truong_ban_quan_ly_duyet" if tong <= 3_000_000
        else "trinh_chu_dau_tu"
    )
    code = _new_code("BG-DK" if a.loai == "du_kien" else "BG-CT", sandbox=a.sandbox)
    payload = {
        "loai": a.loai,
        "ticket_id": a.ticket_id,
        "hang_muc": [h.model_dump() for h in a.hang_muc],
        "tien_vat_tu": tien_vat_tu,
        "so_gio_cong": a.so_gio_cong,
        "tien_cong": tien_cong,
        "tong_cong": tong,
        "tham_quyen_phe_duyet": tham_quyen,
        "ghi_chu": a.ghi_chu,
    }
    _save_record("bao_gia_sua_chua", code, payload, sandbox=a.sandbox)
    return {"ma_bao_gia": code, "trang_thai": "da_lap", **payload}


class CameraArgs(BaseModel):
    khu_vuc: str = Field(description="Khu vực cần tra, ví dụ: Hầm xe B1, Sảnh S1, Hành lang S1 tầng 12")
    ngay: str | None = Field(default=None, description="Ngày cần tra dạng YYYY-MM-DD. Bỏ trống để lấy 3 ngày gần nhất")


@tool("tra_nhat_ky_camera", CameraArgs)
def tra_nhat_ky_camera(a: CameraArgs) -> dict:
    kw = a.khu_vuc.lower().strip()
    rows = [r for r in load_mock("nhat_ky_camera.json") if kw in r["khu_vuc"].lower() or r["khu_vuc"].lower() in kw]
    if a.ngay:
        rows = [r for r in rows if r["ngay"] == a.ngay]
    if not rows:
        khu = sorted({r["khu_vuc"] for r in load_mock("nhat_ky_camera.json")})
        return {"so_ban_ghi": 0, "ghi_chu": "Không có dữ liệu cho khu vực này", "khu_vuc_co_san": khu}
    return {"khu_vuc": a.khu_vuc, "so_ban_ghi": len(rows), "nhat_ky": rows}


class BienBanArgs(BaseModel):
    ticket_id: str = Field(description="Mã phản ánh (hệ thống tự điền)")
    khu_vuc: str = Field(description="Khu vực xảy ra sự việc")
    noi_dung: str = Field(description="Diễn biến sự việc")
    muc_do: str = Field(default="binh_thuong", description="nghiem_trong | binh_thuong")
    sandbox: bool = Field(default=False, description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")


@tool("tao_bien_ban_an_ninh", BienBanArgs)
def tao_bien_ban_an_ninh(a: BienBanArgs) -> dict:
    code = _new_code("BB", sandbox=a.sandbox)
    _save_record("bien_ban_an_ninh", code, a.model_dump(), sandbox=a.sandbox)
    return {"ma_bien_ban": code, "trang_thai": "da_lap", "khu_vuc": a.khu_vuc, "muc_do": a.muc_do}


class YeuCauVeSinhArgs(BaseModel):
    ticket_id: str = Field(description="Mã phản ánh (hệ thống tự điền)")
    khu_vuc: str = Field(description="Khu vực cần dọn, ví dụ: Hành lang tòa S1 tầng 12")
    loai_viec: str = Field(description="Loại việc: thu_gom_rac | khu_mui | ve_sinh_tong_the | rac_cong_kenh")
    muc_do_uu_tien: str = Field(default="binh_thuong", description="gap | binh_thuong")
    ghi_chu: str = Field(default="", description="Ghi chú cho tổ vệ sinh")
    sandbox: bool = Field(default=False, description="Hệ thống tự điền: true khi đang chạy thử/đánh giá")


@tool("tao_yeu_cau_ve_sinh", YeuCauVeSinhArgs)
def tao_yeu_cau_ve_sinh(a: YeuCauVeSinhArgs) -> dict:
    code = _new_code("VS", sandbox=a.sandbox)
    to = "VS-A" if "s1" in a.khu_vuc.lower() else "VS-B"
    _save_record("yeu_cau_ve_sinh", code, {**a.model_dump(), "to_phu_trach": to}, sandbox=a.sandbox)
    return {
        "ma_yeu_cau": code,
        "trang_thai": "da_tao",
        "to_phu_trach": to,
        "khu_vuc": a.khu_vuc,
        "loai_viec": a.loai_viec,
        "muc_do_uu_tien": a.muc_do_uu_tien,
    }


class LichRacArgs(BaseModel):
    khu_vuc: str | None = Field(default=None, description="Lọc theo khu vực, ví dụ: Tòa S1. Bỏ trống để lấy toàn bộ")


@tool("tra_lich_thu_gom_rac", LichRacArgs)
def tra_lich_thu_gom_rac(a: LichRacArgs) -> dict:
    data = load_mock("lich_thu_gom_rac.json")
    if not a.khu_vuc:
        return data
    kw = a.khu_vuc.lower()
    return {
        "khung_gio_thu_gom": [r for r in data["khung_gio_thu_gom"] if kw in r["khu_vuc"].lower()],
        "diem_tap_ket": [r for r in data["diem_tap_ket"] if kw in r["khu_vuc"].lower()],
        "to_ve_sinh": [r for r in data["to_ve_sinh"] if kw in r["phu_trach"].lower()],
    }
