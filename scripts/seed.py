"""Seed demo data through the public HTTP API — never straight into the DB.

That is the point: the seeded specialists are created with exactly the endpoints
the Agent Builder UI calls, which proves agents are data rather than code.

Since Phase 7, knowledge is a domain-wide library: documents are uploaded once and
*linked* to whichever agents need them — `quy_trinh_sua_chua.md` below is linked to
both Kỹ thuật and Kế toán but embedded exactly once.

Chạy: python scripts/seed.py   (backend phải đang chạy)
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import yaml

BASE = os.getenv("DEMO_BASE_URL", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "domains" / "vinhomes" / "knowledge"
DOMAIN_ID = "vinhomes"

# filename -> (scope, [agent_id, ...])
# quy_dinh_ve_sinh.md and quy_dinh_cay_xanh.md are intentionally left out — the
# manager uploads and links those themselves during the live demo.
LIBRARY_DOCS: dict[str, tuple[str, list[str]]] = {
    "faq_chung.md": ("domain", []),
    "noi_quy_an_ninh.md": ("domain", []),
    "bang_phi_dich_vu.md": ("domain", []),
    "bang_gia_sua_chua.md": ("restricted", ["ke_toan"]),
    "quy_trinh_sua_chua.md": ("restricted", ["ky_thuat", "ke_toan"]),
}

SEED_AGENTS = [
    {
        "id": "ky_thuat",
        "display_name": "Kỹ thuật",
        "capability": (
            "Xử lý sự cố kỹ thuật trong căn hộ và khu vực chung: rò rỉ nước, chập điện, mất điện, "
            "điều hòa, thang máy, đường ống. Chẩn đoán mức độ, lập phiếu sửa chữa, tra lịch và "
            "điều kỹ thuật viên theo chuyên môn."
        ),
        "business_prompt": """## Vai trò
Bộ phận kỹ thuật của tòa nhà, chịu trách nhiệm khắc phục hỏng hóc hạ tầng và thiết bị.

## Nhiệm vụ
1. Phân loại mức độ sự cố theo quy trình sửa chữa: khẩn cấp / bình thường / thấp.
2. Xác định sự cố thuộc phần sở hữu riêng hay phần sở hữu chung.
3. Luôn lập phiếu sửa chữa cho mọi yêu cầu, ghi rõ chuyên môn cần (dien_nuoc / dieu_hoa / thang_may).
4. Với mức khẩn cấp: đề nghị điều kỹ thuật viên khẩn cấp ngoài lịch.
5. Với mức bình thường: tra lịch kỹ thuật viên và đề xuất khung giờ còn trống.
6. **Luôn nêu rõ nhóm chịu chi phí** (một trong năm nhóm ở tài liệu quy trình sửa chữa).
7. **Nếu chi phí do người báo chịu — sở hữu riêng hết bảo hành, tài sản cá nhân, hoặc hư hỏng
   phần chung do lỗi người báo — thì BẮT BUỘC ghi "ke_toan" vào can_them_agent.** Đây không phải
   tùy chọn. Bạn không được tự nêu số tiền.
8. Nếu diễn biến phòng họp cho thấy kế toán đã báo giá dự kiến và cần bạn xác nhận: liệt kê cụ thể
   vật tư dự kiến và số giờ công, rồi ghi lại "ke_toan" vào can_them_agent để chốt giá cuối.

## Quy tắc riêng
- Không cam kết thời gian ngoài SLA ghi trong tài liệu quy trình sửa chữa.
- Không báo giá nếu chưa khảo sát thực tế; chỉ nêu nguyên tắc ai chịu chi phí.
- Sự cố có dấu hiệu do tác động từ bên ngoài hoặc phá hoại: ghi "an_ninh" vào can_them_agent.""",
        "tools": ["tra_lich_ktv", "tao_phieu_sua_chua", "dieu_ktv_khan_cap", "tra_can_ho"],
    },
    {
        "id": "ke_toan",
        "display_name": "Kế toán",
        "capability": (
            "Phụ trách mọi vấn đề tiền bạc. Giải đáp và đối soát các khoản thu: phí dịch vụ, phí "
            "gửi xe, phí nước, công nợ và lịch sử thanh toán của căn hộ; giải thích thay đổi đơn "
            "giá và đề xuất miễn giảm. Với sự cố sửa chữa: xác định bên chịu chi phí theo quy "
            "định, lập báo giá dự kiến, và chốt chi phí cuối cùng sau khi bộ phận kỹ thuật xác "
            "nhận vật tư và giờ công thực tế."
        ),
        "business_prompt": """## Vai trò
Bộ phận kế toán, phụ trách mọi thắc mắc và khiếu nại về hóa đơn, phí và công nợ.

## Nhiệm vụ A — thắc mắc hóa đơn, phí, công nợ
1. Luôn tra dữ liệu phí thực tế trước khi trả lời, không ước lượng.
2. Khi có thắc mắc phí tăng: so sánh các tháng gần nhau, chỉ rõ khoản nào thay đổi và vì sao.
3. Trích dẫn bảng phí dịch vụ khi giải thích đơn giá và chính sách.
4. Chỉ đề xuất miễn giảm khi trường hợp khớp đúng một mục trong chính sách miễn giảm.

## Nhiệm vụ B — chi phí sửa chữa (quy trình hai bước)
1. **Xác định bên chịu chi phí TRƯỚC**, theo bảng phân nhóm trong tài liệu bảng giá sửa chữa.
   - Thuộc phần sở hữu chung, hoặc còn bảo hành: nêu rõ người báo KHÔNG phải trả phí, kết thúc.
   - Là tài sản cá nhân người báo tự mua: nêu rõ Ban quản lý không nhận sửa, chỉ kiểm tra sơ bộ
     miễn phí và hướng dẫn liên hệ bảo hành của hãng. KHÔNG lập báo giá.
2. **Bước 1 — báo giá dự kiến**: chỉ khi chi phí do người báo chịu. Chọn hạng mục vật tư và số giờ
   công từ bảng đơn giá trong tài liệu, rồi gọi tool `lap_bao_gia_sua_chua` với `loai="du_kien"`.
   Ghi rõ đây là ước tính, chưa gồm vật tư phát sinh khi mở ra kiểm tra.
   Sau đó ghi "ky_thuat" vào can_them_agent để kỹ thuật xác nhận vật tư và giờ công thực tế.
3. **Bước 2 — chốt giá cuối**: chỉ lập khi diễn biến phòng họp đã có xác nhận vật tư và giờ công
   của kỹ thuật. Gọi lại `lap_bao_gia_sua_chua` với `loai="chot"`. Nêu ngưỡng phê duyệt mà tool
   trả về. Nếu vượt báo giá dự kiến quá 20% thì nói rõ cần xác nhận lại của người báo.

## Quy tắc riêng
- Mọi số tiền phải do tool tính ra, tuyệt đối không tự cộng nhẩm hay bịa.
- **Phân biệt rõ hai loại việc.** Chi phí SỬA CHỮA chỉ dùng `lap_bao_gia_sua_chua`. Thắc mắc
  HÓA ĐƠN / PHÍ DỊCH VỤ / CÔNG NỢ mới dùng `tra_phi_dich_vu`, `tra_cong_no`, `mien_giam_phi`.
  Khi phòng họp đang bàn chi phí sửa chữa, TUYỆT ĐỐI không gọi `tra_cong_no` hay `mien_giam_phi`
  và không đề xuất miễn giảm phí dịch vụ — hai việc đó không liên quan đến nhau.
- Không gộp bước 1 và bước 2. Chưa có xác nhận của kỹ thuật thì KHÔNG chốt giá cuối.
- Miễn giảm luôn phải qua quản lý duyệt; không hứa trước kết quả với người báo.""",
        "tools": ["tra_cong_no", "tra_phi_dich_vu", "mien_giam_phi", "lap_bao_gia_sua_chua"],
    },
    {
        "id": "an_ninh",
        "display_name": "An ninh",
        "capability": (
            "Xử lý sự việc an ninh trật tự: người lạ xâm nhập, mất cắp, hư hỏng tài sản chung, "
            "gây rối, đỗ xe sai quy định. Tra nhật ký camera theo khu vực và lập biên bản sự việc."
        ),
        "business_prompt": """## Vai trò
Bộ phận an ninh, phụ trách kiểm soát ra vào, giám sát camera và lập biên bản sự việc.

## Nhiệm vụ
1. Tra nhật ký camera đúng khu vực và khung thời gian liên quan đến sự việc.
2. Lập biên bản cho mọi sự việc thuộc danh mục bắt buộc trong nội quy an ninh.
3. Nêu rõ mức độ: nghiêm trọng hay bình thường.

## Quy tắc riêng
- Không tiết lộ nội dung hình ảnh camera chi tiết; chỉ nêu có hay không ghi nhận sự việc.
- Sự việc gây thiệt hại hạ tầng kỹ thuật: ghi "ky_thuat" vào can_them_agent, không chờ điều tra xong.""",
        "tools": ["tra_nhat_ky_camera", "tao_bien_ban_an_ninh"],
    },
]


def seed_library(client: httpx.Client) -> dict[str, str]:
    """Upload every library doc once; return filename -> doc_id.

    Re-running the seed (or running it right after the v2 migration, which always
    creates docs as "restricted") also reconciles scope back to the declared value.
    """
    existing_docs = {d["filename"]: d for d in client.get("/api/knowledge").json()}
    by_filename: dict[str, str] = {}
    for filename, (scope, _agents) in LIBRARY_DOCS.items():
        if filename in existing_docs:
            doc = existing_docs[filename]
            by_filename[filename] = doc["id"]
            if doc["scope"] != scope:
                r = client.put(f"/api/knowledge/{doc['id']}/scope", json={"scope": scope})
                note = f"-> đổi scope thành {scope}" if r.status_code < 400 else f"-> ĐỔI SCOPE LỖI: {r.text[:120]}"
                print(f"· thư viện: {filename} đã có (scope cũ={doc['scope']}) {note}")
            else:
                print(f"· thư viện: {filename} đã có (scope={scope}), bỏ qua")
            continue
        path = KNOWLEDGE / filename
        if not path.exists():
            print(f"✗ thiếu tệp {path}")
            continue
        print(f"… nạp vào thư viện: {filename} (scope={scope}, tính embedding, chờ chút)")
        r = client.post(
            "/api/knowledge",
            data={"scope": scope, "domain_id": DOMAIN_ID},
            files={"file": (filename, path.read_bytes(), "text/markdown")},
        )
        if r.status_code >= 400:
            print(f"✗ {filename}: {r.status_code} {r.text[:200]}")
            continue
        doc = r.json()
        by_filename[filename] = doc["id"]
        print(f"✓ {filename}: {doc['num_chunks']} đoạn — \"{doc['title']}\"")
    return by_filename


def seed_eval_cases(client: httpx.Client) -> None:
    """Bộ hồi quy gốc: case viết tay cho 3 agent seed, tự duyệt luôn (source=manual)."""
    path = ROOT / "domains" / "vinhomes" / "eval_cases.yaml"
    if not path.exists():
        print("(không có eval_cases.yaml, bỏ qua)")
        return
    cases = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    existing_by_agent: dict[str, list[str]] = {}
    n_created = 0
    for spec in cases:
        agent_id = spec["agent_id"]
        if agent_id not in existing_by_agent:
            existing = client.get(f"/api/eval/agents/{agent_id}/cases")
            existing_by_agent[agent_id] = [c["ticket_text"] for c in existing.json()] if existing.status_code == 200 else []
        if spec["ticket_text"] in existing_by_agent[agent_id]:
            continue
        r = client.post("/api/eval/cases", json={**spec, "approved": True})
        if r.status_code >= 400:
            print(f"  ✗ case cho {agent_id} lỗi: {r.status_code} {r.text[:150]}")
            continue
        existing_by_agent[agent_id].append(spec["ticket_text"])
        n_created += 1
    print(f"✓ bộ hồi quy gốc: {n_created} case mới (tổng {len(cases)} khai báo trong eval_cases.yaml)")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=180)
    try:
        health = client.get("/api/health").json()
    except Exception as exc:  # noqa: BLE001
        print(f"Không kết nối được backend tại {BASE}. Chạy ./run.sh trước đã.\n  {exc}")
        return 2
    print(f"Backend OK · model={health['chat_model']} · phòng họp={health['room_engine']}\n")

    doc_ids = seed_library(client)
    print()

    for spec in SEED_AGENTS:
        existing = client.get(f"/api/agents/{spec['id']}")
        if existing.status_code == 200:
            # Re-running the seed converges an existing agent back to the declared spec.
            r = client.put(f"/api/agents/{spec['id']}", json={
                "display_name": spec["display_name"],
                "capability": spec["capability"],
                "business_prompt": spec["business_prompt"],
                "tools": spec["tools"],
            })
            if r.status_code >= 400:
                print(f"✗ {spec['id']}: {r.status_code} {r.text[:200]}")
                return 1
            print(f"✓ cập nhật agent {spec['id']} (đã có sẵn)")
        else:
            r = client.post("/api/agents", json={**spec, "status": "draft"})
            if r.status_code >= 400:
                print(f"✗ {spec['id']}: {r.status_code} {r.text[:200]}")
                return 1
            print(f"✓ tạo agent {spec['id']}")

        linked = {d["id"] for d in client.get(f"/api/agents/{spec['id']}/knowledge").json()}
        for filename, (_scope, agent_ids) in LIBRARY_DOCS.items():
            if spec["id"] not in agent_ids or filename not in doc_ids:
                continue
            if doc_ids[filename] in linked:
                print(f"  · {filename}: đã gắn, bỏ qua")
                continue
            r = client.post(f"/api/agents/{spec['id']}/knowledge/link", json={"doc_id": doc_ids[filename]})
            if r.status_code >= 400:
                print(f"  ✗ gắn {filename} lỗi: {r.status_code} {r.text[:200]}")
                return 1
            print(f"  ✓ gắn tài liệu {filename}")

        # Phase 9 gate: activate() giờ đòi một EvalRun đạt trên đúng version hiện tại.
        # Seed chỉ tạo dữ liệu ban đầu, không cõng chi phí LLM chạy đánh giá đầy đủ ở
        # đây — bật thẳng bằng force=true, đúng như DECISIONS.md đã ghi. Bộ case viết
        # tay (seed_eval_cases, chạy ngay dưới) vẫn được nạp sẵn để quản lý tự chạy
        # đánh giá thật bất kỳ lúc nào sau đó qua UI.
        r = client.post(f"/api/agents/{spec['id']}/activate", params={"force": "true"})
        print(f"  ✓ bật agent {spec['id']} (force=true, seed chưa chạy đánh giá)" if r.status_code < 400
              else f"  ✗ bật lỗi: {r.text[:150]}")

    print()
    seed_eval_cases(client)

    print("\nCố ý KHÔNG seed agent Vệ sinh / Cây xanh — đó là màn demo tạo agent qua Builder.")
    print("quy_dinh_ve_sinh.md và quy_dinh_cay_xanh.md vẫn nằm sẵn trong domains/vinhomes/knowledge/")
    print("để quản lý tự upload lên thư viện và tự gắn cho agent mới trong lúc demo.")
    agents = client.get("/api/agents").json()
    print(f"\nTổng: {len(agents)} agent — " + ", ".join(f"{a['id']}({a['status']})" for a in agents))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
