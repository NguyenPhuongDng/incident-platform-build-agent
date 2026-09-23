# Quy trình tiếp nhận và xử lý yêu cầu sửa chữa

## 1. Phạm vi áp dụng

Áp dụng cho mọi yêu cầu sửa chữa phát sinh trong phạm vi tòa nhà, bao gồm phần sở hữu riêng
của từng căn hộ và phần sở hữu chung (hành lang, thang máy, hầm xe, hệ thống cấp thoát nước
trục chính, hệ thống điện tổng).

## 2. Phân loại mức độ

### 2.1 Mức KHẨN CẤP

Các trường hợp sau được xếp mức khẩn cấp và phải điều kỹ thuật viên trong vòng **30 phút**
kể từ khi tiếp nhận:

- Rò rỉ nước lớn, nước chảy thành dòng, thấm lan sang căn hộ khác hoặc xuống tầng dưới.
- Chập điện, cháy khét thiết bị điện, mất điện toàn căn hoặc toàn tầng.
- Kẹt thang máy có người bên trong.
- Rò rỉ khí gas, có mùi khét, khói.
- Vỡ đường ống cấp nước trục chính, ngập hầm xe.

### 2.2 Mức BÌNH THƯỜNG

Hỏng hóc không gây nguy hiểm và không lan rộng: vòi nước nhỏ giọt, bóng đèn hành lang cháy,
điều hòa kém lạnh, cửa bị kẹt nhẹ, ổ cắm lỏng. Thời hạn xử lý: **trong vòng 24 giờ làm việc**.

### 2.3 Mức THẤP

Yêu cầu mang tính cải thiện, thẩm mỹ, hoặc hỏi thông tin: sơn dặm tường, chỉnh lại bản lề,
tư vấn lắp đặt. Thời hạn xử lý: **trong vòng 3 ngày làm việc**.

## 3. Cam kết thời gian (SLA)

| Mức độ | Thời gian điều kỹ thuật viên | Thời gian hoàn tất |
|---|---|---|
| Khẩn cấp | 30 phút | 4 giờ (xử lý tạm thời ngay, khắc phục triệt để sau) |
| Bình thường | Trong ca làm việc gần nhất | 24 giờ làm việc |
| Thấp | Theo lịch hẹn với cư dân | 3 ngày làm việc |

## 4. Các bước xử lý

1. **Tiếp nhận:** ghi nhận mô tả, vị trí, mức độ ảnh hưởng và khung giờ thuận tiện.
2. **Phân loại:** xác định mức độ theo mục 2 và xác định phần sở hữu riêng hay chung.
3. **Lập phiếu sửa chữa:** mỗi yêu cầu tương ứng một phiếu, có mã phiếu để tra cứu.
4. **Điều kỹ thuật viên:** chọn kỹ thuật viên theo chuyên môn (điện nước, điều hòa, thang máy)
   và theo khung giờ còn trống. Với mức khẩn cấp, được phép điều kỹ thuật viên ngoài lịch,
   nhưng phải có phê duyệt của quản lý ca.
5. **Thực hiện:** kỹ thuật viên có mặt, chụp ảnh hiện trạng trước và sau, ghi vật tư đã dùng.
6. **Nghiệm thu:** cư dân xác nhận. Nếu chưa đạt, mở lại phiếu trong vòng 7 ngày không tính
   là yêu cầu mới.

## 5. Chi phí và bên chịu trách nhiệm

Bộ phận kỹ thuật **bắt buộc** phân loại sự cố vào đúng một trong năm nhóm sau, vì nhóm quyết định
ai trả tiền:

| Nhóm | Bên chịu chi phí |
|---|---|
| Phần sở hữu chung | Ban quản lý chịu toàn bộ |
| Phần sở hữu riêng, còn bảo hành chủ đầu tư | Chủ đầu tư chịu, cư dân không trả phí |
| Phần sở hữu riêng, đã hết bảo hành | Cư dân chịu vật tư và nhân công |
| Tài sản cá nhân do cư dân tự mua | Cư dân chịu toàn bộ; Ban quản lý không nhận sửa |
| Hư hỏng phần chung do lỗi cư dân | Cư dân chịu chi phí khắc phục |

**Tài sản cá nhân** gồm các thiết bị cư dân tự mua sắm, không thuộc danh mục bàn giao của chủ đầu
tư: tủ lạnh, máy giặt, lò vi sóng, tivi, máy lọc nước, điều hòa lắp thêm, đồ gia dụng. Với nhóm
này, kỹ thuật chỉ kiểm tra sơ bộ miễn phí và hướng dẫn cư dân liên hệ trung tâm bảo hành của hãng;
không lập báo giá sửa chữa.

### 5.1 Quy tắc chuyển bộ phận kế toán

**Bộ phận kỹ thuật không được tự báo giá hay nêu bất kỳ số tiền nào.** Ngay khi xác định chi phí
do cư dân chịu — tức thuộc nhóm *sở hữu riêng đã hết bảo hành*, *tài sản cá nhân*, hoặc *hư hỏng
phần chung do lỗi cư dân* — kỹ thuật phải chuyển bộ phận kế toán để xác định chi phí, bằng cách
ghi `ke_toan` vào trường `can_them_agent`.

Sau khi kế toán đưa ra báo giá dự kiến, kỹ thuật có trách nhiệm xác nhận lại danh mục vật tư thực
dùng và số giờ công ước tính, rồi chuyển lại kế toán để chốt giá cuối.

## 6. Trường hợp cần phối hợp nhiều bộ phận

Khi sự cố có dấu hiệu liên quan đến an ninh (tác động từ bên ngoài, phá hoại, người lạ),
bộ phận kỹ thuật lập phiếu sửa chữa phần kỹ thuật và đồng thời chuyển thông tin cho bộ phận
an ninh lập biên bản. Hai bộ phận xử lý song song, không chờ nhau.
