# Nhật ký quyết định

Ghi lại các chỗ mơ hồ trong đề bài và phương án đã chọn.

## Phase 0

- **Python 3.12** thay vì 3.11: máy có sẵn 3.11/3.12/3.14; chọn 3.12 vì chromadb chưa hỗ trợ 3.14
  và 3.12 mới hơn mức tối thiểu 3.11 mà đề bài yêu cầu.
- **Retry tự viết trong `qwen_client`** (`max_retries=0` ở SDK) để log được từng lần thử kèm
  model / vai trò / độ trễ / token, đúng tiêu chí ở mục 11.
- **`extra_body={"enable_thinking": False}`** được gửi mặc định; nếu endpoint báo lỗi vì tham số
  này thì client tự bỏ đi và gọi lại, nên cấu hình dùng được cho cả model Qwen2.5 lẫn Qwen3.
- **Một bảng `MockRecord`** duy nhất cho mọi bản ghi do tool ghi tạo ra (phiếu sửa chữa, yêu cầu
  vệ sinh, biên bản an ninh...) thay vì mỗi loại một bảng — đơn giản hơn, đủ cho demo.
- **MCP SDK là `mcp` 2.2.0, không phải v1**: trong SDK 2.x, `FastMCP` được đổi tên thành
  `MCPServer` và client cấp cao là `mcp.Client(url)`. Đề bài viết "FastMCP" theo tên cũ.
  Chọn dùng 2.x (SDK chính thức, bản hiện hành) thay vì ghim `mcp<2`; kiến trúc không đổi,
  chỉ khác tên lớp. Các khác biệt đã xử lý: `Tool.inputSchema` → `Tool.input_schema`,
  `CallToolResult.isError` → `is_error`, `structuredContent` → `structured_content`
  (code vẫn đọc cả hai tên để không phụ thuộc phiên bản).
- **Schema MCP được "làm phẳng"** trước khi đưa cho LLM: Pydantic sinh `anyOf` cho tham số
  `str | None`, một số API tool-calling không thích dạng này, nên `_flatten_refs` gộp lại.
- **Tên cột DB dùng tiếng Anh trung tính** (`resident_id`, `apartment_id`) để lõi không dính
  từ khóa domain; giá trị nghiệp vụ (`ma_cu_dan`, `ma_can_ho`) chỉ xuất hiện ở tầng tool.

## Đổi hướng: dùng Microsoft Agent Framework cho lõi phòng họp

Đề bài ban đầu (mục 1) yêu cầu KHÔNG dùng framework agent. Quản lý đã quyết định ngược lại,
để đánh giá luôn tính khả thi của chính thư viện. Ghi nhận và đã chuyển.

- **Gói cài**: `agent-framework-core` 1.19.0 + `agent-framework-orchestrations` 1.2.0 +
  `agent-framework-openai` 1.14.4. Gói `agent-framework` tổng kéo rất nhiều provider không cần.
- **API thực tế khác tài liệu trên learn.microsoft.com**: trang docs mô tả `GroupChatBuilder`
  có `.participants()` / `.set_select_speakers_func()` và `ChatAgent`, `ai_function`. Bản 1.19
  cài từ PyPI thì: lớp agent tên là `Agent`, tool là `FunctionTool`/`@tool`, và
  `GroupChatBuilder` nhận `participants=` / `selection_func=` qua **constructor**.
  → Phải đọc code cài thật, không tin trang docs.
- **Chỗ đặt Điều phối**: GroupChat gọi `termination_condition` TRƯỚC `selection_func` ở mỗi
  vòng, và `selection_func` bắt buộc trả về một participant hợp lệ (trả `None` gây RuntimeError,
  khác với ví dụ trong docs). Nên Điều phối chạy bên trong `termination_condition`, ghi lựa chọn
  vào `RoomSession`, còn `selection_func` chỉ trả lại lựa chọn đó. Mỗi vòng vẫn đúng 1 lần gọi LLM.
- **Giữ nguyên `ToolExecutor` làm ranh giới bảo mật**: tool của agent được bọc thành
  `FunctionTool(input_model=spec.llm_schema())` — schema đã ẩn `context_params`. Framework có
  sẵn `approval_mode="always_require"` và `ToolApprovalMiddleware`, nhưng **không dùng**: cơ chế
  duyệt phải nằm ở tầng platform, không phụ thuộc framework, đúng nguyên tắc 4 của mục 2.
- **RAG gắn vào `ContextProvider.before_run`**, đồng thời bơm ticket + diễn biến + chỉ dẫn của
  Điều phối cho từng lượt. `after_run` parse JSON output và phát sự kiện. Nhờ vậy toàn bộ
  observability (SSE) giữ nguyên như engine tự viết.
- **Giữ cả hai engine**: `ROOM_ENGINE=maf` (mặc định) hoặc `builtin` (vòng lặp tự viết ở
  `backend/core/room.py`). Cho phép so sánh trực tiếp khi demo, và có đường lui nếu framework
  trục trặc.
- **Khác biệt hành vi cần biết**: GroupChat tự broadcast hội thoại cho mọi participant, nên
  agent thấy output thô của nhau; engine tự viết thì chỉ đưa bản tóm tắt đã chuẩn hóa.
  Hệ quả: context dài hơn theo số lượt, và tốn token hơn.

## Hai lỗi thật phát hiện khi chạy end-to-end

### 1. `OpenAIChatClient` mặc định dùng Responses API — không hợp với DashScope

Triệu chứng: mọi vòng tool-calling trong phòng họp MAF chết với
`400 InvalidParameter ... role: tool must be one of user,assistant,system,function`.

Điều tra: dựng một server giả ghi lại request thì thấy framework gọi `POST /v1/responses`,
không phải `/v1/chat/completions`. Gọi thẳng bằng SDK `openai` với `role="tool"` qua
`/v1/chat/completions` thì **chạy tốt** — tức lỗi ở endpoint chứ không ở định dạng message.

Khắc phục: dùng `OpenAIChatCompletionClient` thay cho `OpenAIChatClient` trong
`backend/core/maf_room.py`. Ghi chú ngay trong docstring để người sau không đổi lại.

Bài học: khi ghép framework với endpoint "tương thích OpenAI", phải kiểm **đường HTTP thật sự
được gọi**, không chỉ kiểm model có function calling hay không.

### 2. Runner ghi log tham số thô của LLM thay vì tham số đã thực thi

Smoke test kịch bản 5 báo FAIL: `tra_cong_no` nhìn như đã chạy với `ma_can_ho=S2-0805` của căn hộ
khác. Kiểm tra trực tiếp `ToolExecutor` cho thấy **không hề rò rỉ** — executor đã ghi đè thành
S1-1203 và trả đúng dữ liệu của người báo.

Nguyên nhân: `runner.py` ghi `tool_calls` từ `args` mà LLM gửi, trước bước tiêm context.
Không phải lỗ hổng bảo mật, nhưng là lỗi quan sát nghiêm trọng: người audit nhìn log sẽ tưởng
tham số nhạy cảm lọt qua được.

Khắc phục: `ToolContext` mang thêm `trace`, do chính `ToolExecutor` ghi, gồm cả
`args` (đã thực thi) lẫn `args_llm_gui` (LLM xin) và `tham_so_bi_ghi_de`. Mọi nơi báo cáo
tool call đều đọc từ trace này. Kịch bản 5 được siết lại để kiểm cả hai vế — giờ nó chứng minh
được LLM *đã cố* vượt rào và *đã bị chặn*, mạnh hơn bản kiểm cũ.

## Phản hồi nghiệm thu: vị trí tin Lễ tân và luồng chi phí sửa chữa

### Tin của Lễ tân phải ở khung chat, không phải timeline phòng họp

Trước: tin gửi người báo chỉ xuất hiện dưới dạng sự kiện `receptionist_reply` trong timeline
tab Phòng họp; khung chat tab Cư dân không hề cập nhật sau khi phòng họp chạy xong.

Sau: khung chat mở `EventSource` theo ticket vừa tạo (`followTicket`), nên mọi tin của Lễ tân —
kể cả tin cập nhật sau khi quản lý duyệt hành động — hiện ngay tại nơi người báo đang nhìn, kèm
dòng trạng thái "Các bộ phận đang trao đổi nội bộ…" trong lúc chờ. Timeline phòng họp chỉ còn
một dòng đánh dấu "Đã gửi tin cho người báo", vì nội dung đó thuộc về giao diện người dùng.

### Luồng chi phí sửa chữa thiếu Kế toán

Trước: ticket sửa chữa chỉ có Kỹ thuật phát biểu rồi kết thúc. Không ai xác định bên chịu chi phí,
không có báo giá.

Đã sửa, phần lớn bằng **dữ liệu** chứ không phải code — đúng tinh thần platform:

1. `quy_trinh_sua_chua.md`: thêm bảng năm nhóm chịu chi phí (bổ sung nhóm *tài sản cá nhân do cư
   dân tự mua*) và mục 5.1 quy định kỹ thuật **bắt buộc** chuyển kế toán khi chi phí do người báo
   chịu, đồng thời cấm kỹ thuật tự nêu số tiền.
2. `bang_gia_sua_chua.md` (tài liệu mới cho Kế toán): phân nhóm trách nhiệm, đơn giá nhân công,
   đơn giá vật tư, quy trình báo giá hai bước, ngưỡng phê duyệt.
3. Prompt nghiệp vụ của Kỹ thuật và Kế toán viết lại theo quy trình hai bước.

Một thay đổi **có** chạm code, và là chạm đúng chỗ: thêm tool `lap_bao_gia_sua_chua` vào catalog.
Lý do: lần chạy thử đầu tiên Kế toán được giao báo giá nhưng trong tay chỉ có tool phí dịch vụ và
công nợ, nên nó gọi `tra_cong_no` rồi đề xuất giảm 50% phí dịch vụ — hoàn toàn lạc đề. Model sẽ
dùng tool nó có, kể cả khi không liên quan. Tool mới nhận danh mục vật tư và giờ công rồi **tự
cộng** và tra ngưỡng phê duyệt, nên số tiền không do model bịa ra (luật 3 của lớp prompt platform).

### Hai guard mới

- **`normalize_output(self_id=...)`**: bỏ chính agent khỏi `can_them_agent` của nó. Model hay tự
  gọi tên mình, chỉ làm nhiễu quyết định kế tiếp của Điều phối.
- **`ban_giao_chua_duoc_phuc_vu`**: nếu Điều phối định kết thúc trong khi thành viên vừa phát biểu
  còn đề nghị gọi người khác và người đó vẫn chọn được, nhắc lại Điều phối một lần kèm lý do.
  Cùng khuôn mẫu với guard chọn nhầm agent. Prompt Điều phối cũng được siết lại tương ứng.

Kết quả ca "vòi lavabo hết bảo hành", 4 lượt:
Kỹ thuật (phân loại, chuyển kế toán) → Kế toán (BG-DK 650.000đ, xin kỹ thuật xác nhận) →
Kỹ thuật (xác nhận vật tư và giờ công) → Kế toán (BG-CT chốt giá, kiểm ngưỡng lệch 20%).

## Lỗi `run.sh`: "Lỗi phân đoạn (xuất ra core)" khi Ctrl+C

Triệu chứng: `./run.sh` khởi động bình thường, nhưng khi Ctrl+C thì in "Đang dừng..." hàng chục
lần rồi segfault. Ban đầu dễ tưởng là lỗi native của chromadb hay onnxruntime.

Điều tra: chạy riêng từng tiến trình rồi gửi SIGINT và đọc mã thoát thật — MCP server, backend
không MCP, backend có MCP, backend đã nạp chroma: **tất cả đều exit=0**. Không tiến trình Python
nào crash.

Nguyên nhân thật nằm ở chính script:

```bash
cleanup() { echo "Đang dừng..."; kill 0; }   # SAI
trap cleanup EXIT INT TERM
```

`kill 0` bắn SIGTERM vào **toàn bộ process group**, trong đó có chính `run.sh`. Script nhận TERM
→ trap gọi `cleanup` → `kill 0` → lặp vô hạn. Bash tràn stack và **chính bash segfault**, không
phải Python.

Khắc phục:
- `cleanup` có cờ `CLEANING` chống tái nhập, và `trap - EXIT INT TERM` ngay khi vào.
- Giết theo **PID cụ thể** của tiến trình con mà script tự khởi động, không dùng `kill 0`.
- `wait` từng PID để cổng được trả lại trước khi script thoát.
- Thêm kiểm tra cổng `APP_PORT` bị chiếm: báo lỗi rõ ràng và gợi ý `APP_PORT=8002 ./run.sh`,
  thay vì để uvicorn chết lặng lẽ.
- `wait -n` để script thoát ngay khi một trong hai tiến trình chết, thay vì treo.

Lưu ý hành vi: nếu MCP server đã chạy sẵn, `run.sh` dùng lại và **không** giết nó khi thoát —
script chỉ dọn thứ do chính nó khởi động.

---

# Phase 7 — Thư viện tri thức theo domain

## Mô hình

`KnowledgeDoc` chuyển từ "1 tài liệu = 1 agent" sang thư viện dùng chung theo domain:
bỏ `agent_id`, thêm `domain_id`, `title`, `summary` (LLM tự tóm tắt khi upload), `scope`
(`domain`|`restricted`), `version`. Bảng `AgentDoc(agent_id, doc_id)` là liên kết nhiều-nhiều.
Chroma: một collection `knowledge_<domain_id>`, chunk mang `{doc_id, scope, filename,
chunk_index, doc_version}` — không còn `agent_id` trong metadata chunk.

**Đã kiểm tra**: chromadb 1.5.9 hỗ trợ `$or` kết hợp `$in` trong `where` (test trực tiếp bằng
bộ dữ liệu 3 điểm giả). Không cần chạy 2 truy vấn rồi gộp như phương án dự phòng nêu trong đề bài.
Truy xuất của agent: `{"$or": [{"doc_id": {"$in": <doc_ids đã gắn>}}, {"scope": "domain"}]}`.

## Lỗi thật gặp phải

### 1. SQLite không tự thêm cột cho bảng đã tồn tại

`SQLModel.metadata.create_all()` chỉ tạo bảng **chưa có**, không ALTER bảng đã tồn tại. Thêm
`Ticket.is_eval` (cột mới trên bảng cũ) làm mọi truy vấn `Ticket` trên DB thật (đã tạo từ trước)
lỗi `no such column: ticket.is_eval` ngay khi khởi động lại với code mới.

Khắc phục: `backend/app/db.py` có `_patch_missing_columns()` chạy trước `create_all()` — so
cột khai báo trong model với cột thực tế trong bảng đã tồn tại, `ALTER TABLE ... ADD COLUMN`
cho cột nào thiếu, suy ra kiểu và giá trị mặc định từ chính khai báo Pydantic/SQLModel. Đây là
cơ chế chung, không riêng cho `is_eval` — mọi cột thêm sau này trên bảng cũ đều tự vá.

### 2. Bảng `knowledgedoc` đổi hình dạng hoàn toàn — không ALTER được

Xóa `agent_id`, thêm 6 cột. SQLite không hỗ trợ ALTER kiểu này gọn gàng. `scripts/migrate_
knowledge_v2.py` dò schema bằng `PRAGMA table_info`, nếu còn cột `agent_id` (schema cũ) thì:
đọc toàn bộ bản ghi cũ, `DROP TABLE`, gọi lại `init_db()` để tạo bảng mới, rồi **đọc lại nội
dung tài liệu trực tiếp từ file nguồn trên đĩa** (`domains/<domain>/knowledge/<filename>`) thay
vì cố dựng lại từ chunk cũ trong Chroma — đơn giản hơn và cho nội dung giống hệt. Mỗi tên tệp
trùng nhau giữa nhiều agent chỉ tạo **một** `KnowledgeDoc` và ingest **một lần**, các agent cũ
được gắn lại qua `AgentDoc`. Idempotent: chạy lại thấy đã có cột `domain_id` thì bỏ qua.

Đã kiểm chứng trên **bản sao** của `data/demo.db` thật (không đụng vào bản đang chạy cho người
dùng): 4 bản ghi cũ → 4 tài liệu duy nhất, đúng liên kết agent, chạy lại lần hai là no-op.

### 3. Lễ tân trả lời nhanh làm "cướp" mất một số phản ánh cá nhân

Sau khi gắn `bang_phi_dich_vu.md` scope `domain` (theo đúng yêu cầu), câu "Sao tháng này phí
dịch vụ nhà tôi tăng?" — vốn là phản ánh cần Kế toán tra đúng dữ liệu người hỏi — bị Lễ tân xếp
nhầm vào `hoi_thong_tin` và trả lời chung chung từ tài liệu chính sách, không tạo ticket. Phát
hiện qua kịch bản 2 smoke test fail thật (không phải giả định).

Khắc phục: sửa lại tiêu chí phân loại trong `INTAKE_TEMPLATE` — `hoi_thong_tin` chỉ dành cho câu
hỏi *áp dụng như nhau cho mọi người*; bất kỳ câu nào ngụ ý cần tra dữ liệu riêng của người hỏi
("của tôi", "chỗ tôi ở") đều bắt buộc là `phan_anh`, kể cả khi nghe giống câu hỏi. Khi không chắc,
chọn `phan_anh` (an toàn hơn vì vẫn hỏi thêm được, còn trả lời nhầm thì không có đường sửa).

### 4. Tự vi phạm chính nguyên tắc "lõi sạch domain" khi viết ví dụ prompt

Khi viết ví dụ minh họa cho tiêu chí phân loại ở trên, ban đầu viết "CĂN HỘ tôi bị..." — lọt đúng
từ khóa cấm ngay trong file lõi (`receptionist.py`). Kịch bản 6 (đã mở rộng grep thêm
`backend/knowledge/`, thêm từ "Ban quản lý" vào danh sách cấm) bắt được ngay lần chạy đầy đủ đầu
tiên. Sửa bằng cách đổi ví dụ sang từ trung tính ("chỗ tôi ở bị..."). Bài học: chính người viết
core prompt cũng dễ vô tình chèn từ domain khi minh họa bằng ví dụ cụ thể — kịch bản 6 phải luôn
chạy lại sau bất kỳ sửa đổi nào lên `backend/core/` hay `backend/knowledge/`.

## Chưa áp dụng cho server thật đang chạy

Toàn bộ Phase 7 được viết và kiểm chứng trên **server cách ly** (cổng 8010/8111, `data_dev/`
— bản sao của `data/demo.db` và `data/chroma` thật) để không làm gián đoạn phiên demo đang chạy
của người dùng ở cổng 8000/8101. `scripts/migrate_knowledge_v2.py` cần chạy đúng một lần trên
`data/demo.db` thật trước khi khởi động lại backend bằng code mới — việc này cố ý để dành cho
lúc người dùng sẵn sàng chuyển đổi, không tự ý làm giữa phiên của họ.

## Kết quả smoke test (7 kịch bản, 28 kiểm tra)

28/28 PASS trên server cách ly, sau khi sửa hai lỗi thật ở trên. Kịch bản 2 (thắc mắc phí) có
một lần cần chạy lại do model đôi khi trả lời từ RAG mà không gọi tool `tra_phi_dich_vu` để xác
nhận số liệu cá nhân — hành vi ngẫu nhiên đã có từ trước Phase 7, không phải hồi quy mới.

## Chuyển đổi server thật sang Phase 7 — phát hiện thêm một lỗi thật

Khi chuẩn bị migrate `data/demo.db` thật, phát hiện bảng `knowledgedoc` **đã có cả cột cũ
(`agent_id`) lẫn cột mới** (`domain_id`, `title`...), còn bảng `agentdoc` đã tồn tại nhưng
**rỗng**. Tức là ở đâu đó trong quá trình phát triển Phase 7, một lệnh Python đã lỡ gọi
`init_db()` với code mới **nhắm thẳng vào `data/demo.db` thật** (do quên set `DB_PATH`, mặc
định rơi về `./data/demo.db`) — `_patch_missing_columns()` đã ADD COLUMN thật, nhưng không có
migration dữ liệu thật nào chạy theo sau. Không mất dữ liệu (ADD COLUMN chỉ cộng thêm, không
xóa `agent_id` cũ), nhưng làm sai lệch bộ kiểm tra idempotent ban đầu của
`migrate_knowledge_v2.py` (kiểm tra sự có mặt của `domain_id` để quyết định bỏ qua — sai, vì
lúc này `domain_id` đã có mặt nhưng rỗng, script sẽ thoát sớm mà không migrate gì).

**Sửa:** đổi tiêu chí sang kiểm tra `agent_id` **còn tồn tại** hay không — chỉ có DROP + tạo lại
bảng (bước duy nhất xóa hẳn cột này) mới là dấu hiệu đáng tin của "đã migrate xong". Đã kiểm
chứng lại trên bản sao trước khi áp dụng thật.

**Bài học:** mọi script/lệnh một lần chạm tới DB trong lúc phát triển phải luôn tường minh
`DB_PATH`/`CHROMA_PATH`, kể cả các lệnh "chỉ để kiểm tra nhanh". Không có is nào miễn nhiễm.

## Đã áp dụng lên server thật (không còn ở trạng thái "chỉ test cách ly")

- Sao lưu `data/demo.db` và `data/chroma` vào `backups/` trước khi động vào.
- `migrate_knowledge_v2.py` chạy thật: 4 tài liệu cũ → 4 tài liệu thư viện, đúng liên kết theo
  dữ liệu lịch sử (trừ liên kết tới `ky_thuat` — agent này đã bị người dùng xóa qua UI trước đó
  nên không gắn lại được, script báo lỗi rõ ràng thay vì crash).
- `seed.py` chạy lại: khôi phục agent `ky_thuat` (thiếu so với baseline 3 agent seed), sửa scope
  `bang_phi_dich_vu.md`/`noi_quy_an_ninh.md` thành `domain`, gắn `quy_trinh_sua_chua.md` cho cả
  hai agent Kỹ thuật và Kế toán, nạp `faq_chung.md`.
- Agent `cyxanhcnhquan` do người dùng tự tạo qua giao diện (từ phiên demo trước) được giữ
  nguyên, không đụng vào.
- Smoke test 7 kịch bản (28 kiểm tra) chạy PASS 28/28 **trên chính server thật ở cổng 8000**.

---

# Phase 8 — Builder agent

## Thiết kế

`backend/agents/builder.py` là service tầng quản trị, không phải specialist agent: không có
bản ghi `Agent`, Điều phối không bao giờ thấy nó. Builder chỉ đọc — domain pack, metadata tool
(tên/mô tả/scope/cờ duyệt, không schema không code), và `id`/`display_name`/`capability` của
agent khác (không đọc `business_prompt` của họ, không đọc thư viện tài liệu). Output luôn qua
`_validate_and_clean()` trước khi trả về, đảm bảo tools ⊆ catalog, id là slug hợp lệ và không
trùng (tự thêm hậu tố), và **không bao giờ có trường tài liệu** — nếu model trả về `docs`/
`tai_lieu`/`knowledge`/`documents` thì code bỏ và ghi log cảnh báo (đã kiểm bằng test thật).

**Dùng MAF cho tầng quản trị, theo đúng yêu cầu thử nghiệm**: `agent_framework.Agent` +
`OpenAIChatCompletionClient`, ép JSON qua `options={"response_format": {"type": "json_object"}}`.
Đã kiểm chứng hoạt động ổn định với Qwen (test trực tiếp trước khi viết code thật). Có đường lui
về `chat_json(as_dict=True)` nếu MAF lỗi (bọc try/except quanh lệnh gọi MAF), nhưng thực tế
chưa cần dùng tới trong toàn bộ quá trình test.

## Ba lỗi thật gặp khi chạy thử ca cây xanh

### 1. `business_prompt` trả về dạng object thay vì chuỗi markdown

Lần chạy đầu, qwen-flash trả `business_prompt` là một JSON object `{"vai_tro":..., "nhiem_vu":...,
"quy_tac_rieng":...}` thay vì chuỗi `"## Vai trò\n...\n\n## Nhiệm vụ\n..."`. Code cũ chỉ
`str(...)` nên ra `"{'vai_tro': '...', ...}"` — một dict-repr xấu, sai định dạng platform_prompt.py
mong đợi.

Sửa hai lớp: (1) prompt yêu cầu tường minh hơn, có ví dụ định dạng cụ thể ngay trong schema;
(2) `_coerce_business_prompt()` — nếu model vẫn trả dict, tự dựng lại đúng 3 mục markdown từ các
khóa quen thuộc (`vai_tro`/`role`, `nhiem_vu`/`tasks`, `quy_tac_rieng`/`rules`) thay vì phó mặc,
đồng thời ghi cảnh báo để quản lý biết mà xem lại.

### 2. Model mượn tạm tool không liên quan thay vì báo thiếu

Đúng bài học đã cảnh báo trước ("Có tool phù hợp thì model mới làm đúng việc"): lần đầu, với yêu
cầu cây xanh (catalog không có tool cây xanh thật), model chọn `tao_yeu_cau_ve_sinh` và
`lap_bao_gia_sua_chua` — mượn từ vệ sinh và sửa chữa — và để `thieu_tool` **rỗng**, không báo gì.

Sửa: viết lại nguyên tắc 3 trong prompt, cấm rõ ràng việc mượn tool khác việc, yêu cầu để trống
tool còn hơn chọn nhầm. Sau khi sửa, model tự báo đúng 3 tool còn thiếu
(`lap_bao_gia_cay_xanh`, `dieu_nhan_vien_cham_soc_cay`, `tra_hieu_qua_he_thong_tuoi`) — dù vẫn
chọn `tao_yeu_cau_ve_sinh`/`tra_lich_thu_gom_rac` (biện minh được: cành lá sau cắt tỉa tính là
rác cồng kềnh, đúng tinh thần tài liệu `quy_dinh_cay_xanh.md`). Thêm lớp kiểm tra bằng code
`_tool_relevance_check()` — so cosine giữa embedding của `capability` và mô tả từng tool đã chọn,
dưới ngưỡng 0.30 thì cảnh báo — bắt được đúng một tool không liên quan (`tra_thong_tin_cu_dan`)
mà model không tự báo. Đúng nguyên tắc 5: "kiểm tra được bằng code thì để code kiểm tra."

### 3. Bộ lọc chặn vi phạm luật phòng họp tự bắn nhầm câu AN TOÀN

`_sanitize_business_prompt()` dùng regex tìm cụm "tự quyết định" để chặn agent tự ý quyết việc
cần duyệt — nhưng lại xóa nhầm dòng "**Không** tự quyết định chi phí..." (một câu AN TOÀN, phủ
định đúng điều cần cấm) vì regex khớp cả câu bị phủ định.

Sửa: `_is_negated()` — trước khi coi là vi phạm, nhìn ngược 20 ký tự trước cụm khớp tìm từ phủ
định (không/cấm/chưa/đừng/tuyệt đối không). Có phủ định thì bỏ qua (câu đang bảo vệ, không phải
vi phạm). Đã viết unit test riêng cho cả hai chiều (câu an toàn giữ nguyên / câu vi phạm thật vẫn
bị chặn) trước khi chạy lại toàn bộ.

## Kết quả

Chạy thử trọn luồng thật (không phải giả lập): Builder soạn nháp → quản lý tự lưu qua
`POST /api/agents` (qua đúng validate chuẩn) → quản lý tự upload+gắn `quy_dinh_cay_xanh.md` →
sandbox router chọn đúng agent vừa tạo → activate → xóa dọn. Mỗi bước xác nhận bằng gọi API thật,
không mock.

Smoke test 9 kịch bản (40 kiểm tra): **40/40 PASS** trên server cách ly. `AgentDoc` không đổi số
dòng trước/sau khi gọi Builder (xác nhận Builder không đụng vào liên kết tài liệu). Kịch bản 9
(Bảo trì chung) phát hiện chồng lấn "cao" với `ky_thuat` — cả từ LLM tự báo và từ kiểm tra
embedding độc lập của code (0.87, vượt ngưỡng 0.85).

---

# Phase 9 — Evaluator agent

## Thiết kế

`backend/agents/evaluator.py`: sinh case (LLM, không đọc `business_prompt` của agent — chỉ
`capability` + catalog + tóm tắt tài liệu đã gắn + capability agent khác), chạy toàn bộ luồng
thật (Lễ tân → phòng họp → Lễ tân tổng hợp) ở chế độ `is_eval=True`, kiểm tra bằng code (9 tiêu
chí: agent/tool đúng-sai, nguồn RAG, output hợp lệ, không guard nghiêm trọng, context param đúng
ngữ cảnh, không lộ thông tin nội bộ), rồi LLM judge chấm 4 tiêu chí định tính kèm bằng chứng.
`regression()` chạy lại **chỉ phần định tuyến** (rẻ hơn nhiều so với chạy cả phòng họp) trên toàn
bộ case đã duyệt của mọi agent active, so sánh độ chính xác trước/sau khi thêm candidate.

## Cơ chế `sandbox`/`EVAL-` xuyên suốt hệ thống (nguyên tắc 6)

`ToolContext.sandbox` (đã có từ Phase 3 cho `/api/sandbox/agent`) nay được `maf_room.py` và
`room.py` tự động bật khi `ticket.is_eval=True`. Mọi tool ghi (`gui_thong_bao_noi_bo`,
`mien_giam_phi`, `lap_bao_gia_sua_chua`, `tao_bien_ban_an_ninh`, `tao_yeu_cau_ve_sinh`, và hai
tool MCP `tao_phieu_sua_chua`/`dieu_ktv_khan_cap`) nhận thêm tham số **ẩn** `sandbox` — ẩn theo
đúng cơ chế `context_params` sẵn có (không cần khai riêng từng tool trong catalog.yaml, chỉ cần
tool khai báo trường `sandbox` là tự động được `ToolExecutor`/`llm_schema()` xử lý). Khi bật,
tool trả mã `EVAL-...` thay vì mã thật, và **không ghi** vào `MockRecord`/file JSON của MCP.
Ticket đánh giá cũng có mã `EVAL-` ngay từ đầu (thay vì `TK-`), dễ nhận ra ngay trên UI.

## Ba lỗi thật nghiêm trọng phát hiện khi chạy đánh giá thật

### 1. Race condition khi chạy nhiều case song song — lỗi thật, không phải giả định

`EVAL_CONCURRENCY=2` khiến 2 case chạy đồng thời trong `ThreadPoolExecutor`. Lần chạy thật đầu
tiên: 2/5 case lỗi với thông báo khó hiểu `'RustBindingsAPI' object has no attribute 'bindings'`
và một lỗi khác in ra thẳng đường dẫn `chroma` — đây là hai luồng cùng khởi tạo
`chromadb.PersistentClient` lần đầu vào cùng một lúc, phá vỡ trạng thái nội bộ của thư viện.
Sửa: thêm khóa (`threading.Lock`) quanh phần khởi tạo một-lần trong `backend/knowledge/store.py`
(`_get_client()`, `_collection()`) — chỉ khóa lúc khởi tạo, không khóa mỗi lần truy vấn.

### 2. Chính bản vá ở lỗi 1 tự gây deadlock

Ngay sau khi vá lỗi 1, lần chạy tiếp theo **treo vô thời hạn**. Nguyên nhân: dùng
`threading.Lock()` (không tái nhập), trong khi `_collection()` giữ khóa rồi **gọi lại**
`_get_client()` — hàm này cũng cố lấy chính khóa đó, tự khóa chết chính nó ngay ở lượt gọi RAG
đầu tiên sau khi khởi động. Sửa bằng `threading.RLock()`. Đã viết một bài test cách ly rẻ tiền
(8 thread gọi đồng thời `_collection()` lúc nguội) để xác nhận trước khi chạy lại toàn bộ đánh
giá tốn kém. Cùng một lỗi tiềm ẩn được tìm và vá luôn ở `backend/tools/mcp_client.py`'s
`_runner()` (dùng `Lock` thường vì không có gọi lồng, không cần `RLock`).

**Bài học chung:** mọi singleton lazy-init dùng chung giữa các thread phải được khóa đúng cách
*trước khi* đưa engine đánh giá chạy song song vào production — lỗi loại này chỉ lộ ra khi có
tải đồng thời thật, không lộ ra khi test tuần tự (đúng như toàn bộ code trước Phase 9 chỉ chạy
đơn luồng nên không ai từng chạm phải).

### 3. Case viết tay thiếu câu trả lời cho trường bắt buộc "vị trí"

Case eval cho câu hỏi phí dịch vụ không cung cấp `vi_tri` (trường bắt buộc theo `domain.yaml`,
vốn được thiết kế cho phản ánh vật lý, ít phù hợp cho câu hỏi tài chính) — Lễ tân hỏi mãi, hết
`phan_hoi_bo_sung` mà không tạo được ticket. `scripts/smoke_test.py`'s `open_ticket()` đã có sẵn
2 câu trả lời đệm chung chung cho đúng tình huống này; `evaluator._run_ticket_flow()` trước đó
thiếu cơ chế tương tự. Đã thêm cùng 2 câu đệm làm lưới an toàn.

## Phát hiện quan trọng về chi phí (không phải lỗi, nhưng cần biết trước khi demo)

`regression()` lặp qua **toàn bộ case đã duyệt của MỌI agent đang active**, mỗi case tốn
`EVAL_ROUTER_REPEATS(2) × 2 tập thành viên` = 4 lệnh gọi LLM định tuyến. Với 15 case hạt giống,
một lần gọi regression tốn tối thiểu ~60 lệnh gọi `dieu_phoi`; quan sát thực tế một số lệnh mất
tới 80 giây, khiến một lần regression có thể mất 5-15 phút. `builder/auto` gọi cả `run_agent()`
lẫn `regression()` mỗi vòng, tối đa 2 vòng — tổng thời gian một lần chạy tự động có thể vượt
15-20 phút khi bộ case hồi quy đã tích lũy lớn. Đây là đặc tính chi phí thật của thiết kế, không
phải lỗi; ghi vào README phần giới hạn đã biết, cùng hướng giảm tải khả dĩ (giảm
`EVAL_ROUTER_REPEATS`, hoặc lấy mẫu một phần case thay vì toàn bộ) để dành cho "Hướng phát triển".

## Ca phản chứng "Bảo trì chung": phát hiện thật, không ép dữ liệu để ra kết quả mong muốn

Thử ca đối kháng đúng theo mô tả (mô tả năng lực trùng lặp cao với `ky_thuat`, xác nhận trùng
lặp "cao" qua kiểm tra embedding của Builder ở kịch bản 9). Nhưng khi chạy `regression()` thật
— **hai lần độc lập** — Điều phối vẫn nhất quán chọn đúng `ky_thuat` cho toàn bộ 6 case gốc,
không case nào bị "cướp". Đây là quan sát thật, không phải lỗi cơ chế: `regression()` đo hành vi
định tuyến thực tế (khác với kiểm tra tương đồng embedding của Builder — đo ý nghĩa văn bản, không
đo hành vi model), và với router/model hiện tại, việc sao chép gần giống mô tả năng lực không đủ
đánh lừa Điều phối trong trường hợp cụ thể này.

Ban đầu có thử sửa trực tiếp `capability` của agent thử nghiệm để cố tạo ra kết quả "bị cướp" —
đã dừng lại vì đó là ép dữ liệu để khớp kỳ vọng, không phải kiểm thử trung thực. Thay vào đó:
kịch bản 10 được viết lại để kiểm tra đúng cơ chế chốt chặn kích hoạt (activate() tôn trọng kết
quả regression bất kể kết quả là gì) và **báo cáo trung thực** hiện tượng quan sát được, thay vì
khẳng định cứng một kết quả không tái lập ổn định. Cơ chế regression bản thân đã được xác nhận
đúng qua chỉ số hợp lệ (độ chính xác trước/sau theo từng agent, danh sách case bị cướp dạng danh
sách rỗng-hoặc-có-phần-tử) và qua việc `activate()` phản ứng đúng với kết quả `dat` bất kể giá trị.

## Chi phí `regression()` nghiêm trọng hơn ước tính ban đầu — sửa cấu trúc, không chỉ tăng timeout

Ước tính lúc đầu (5-15 phút/lần) chưa đủ: chạy thật `builder/auto` với đúng 15 case gốc (5
case/agent × 3 agent) **vượt 30 phút vẫn không xong** — client timeout, nhưng server vẫn tiếp
tục chạy nền (do `asyncio.to_thread` không hủy khi client ngắt kết nối), lãng phí tài nguyên.
Đo trực tiếp một lần gọi `regression()` độc lập: nhiều lệnh gọi `dieu_phoi` cá nhân mất tới 60-80
giây, khiến 15 case × 4 lệnh gọi (2 lần lặp × 2 tập thành viên) = 60 lệnh gọi cộng dồn rất lớn.

**Không chỉ tăng timeout — sửa đúng gốc:** thêm `EVAL_REGRESSION_MAX_CASES_PER_AGENT` (mặc định
3), giới hạn số case *lấy mẫu* mỗi agent trong một lần `regression()`, ưu tiên case viết tay
(`source=manual`, đáng tin nhất) trước, case tự sinh chỉ lấp phần còn thiếu. Đây là chặn trần chi
phí **có chủ đích**, không phải chắp vá: nếu không giới hạn, chi phí một lần regression sẽ tăng
vô hạn theo thời gian khi domain tích lũy thêm case — càng dùng platform lâu, activate() càng
chậm dần, một cách âm thầm nguy hiểm hơn nhiều so với chậm ngay từ đầu.

Đo lại sau khi vá: regression với 9 case (3×3, đã giới hạn) mất **2 phút 34 giây** — giảm mạnh so
với >30 phút không xong của bản chưa giới hạn. Vẫn chậm (do độ trễ LLM cá nhân, không phải do
thiết kế), nhưng nay có **trần chi phí rõ ràng**, không phụ thuộc domain đã hoạt động bao lâu.

**Đã cân nhắc nhưng không làm**: hạ `EVAL_ROUTER_REPEATS` mặc định từ 2 xuống 1 (cũng giảm chi
phí một nửa) — không đổi, vì đây là giá trị đề bài quy định tường minh; ưu tiên thêm biến giới hạn
case mới thay vì âm thầm hạ độ lặp lại đã được chỉ định.

## HITL: tool cần duyệt phải dừng cả phòng họp, không chỉ dừng chính nó

Bản trước hiểu `requires_approval` theo nghĩa hẹp nhất: không thực thi, tạo một `Action` trạng thái
`cho_duyet`, trả về cho agent một dòng "đã gửi quản lý duyệt" rồi **họp tiếp ngay**. Hệ quả là chốt
chặn duyệt đúng về mặt bảo mật nhưng vô nghĩa về mặt nghiệp vụ: agent viết kết luận quanh một kết
quả tool nó chưa hề nhận, Điều phối kết thúc phiên, Lễ tân trả lời người báo — rồi quản lý mới bấm
Duyệt, tool chạy một mình, và tin cập nhật phải gửi thành một lượt riêng vá vào sau. Người xem demo
nhìn thấy đúng thứ khó chấp nhận nhất: **quyết định của con người không kịp ảnh hưởng tới cuộc họp**.

Nay lượt gọi tool **chặn tại chỗ** trong `ToolExecutor` cho tới khi có quyết định
(`backend/tools/approval_gate.py`). Duyệt xong, chính phòng họp chạy tool và họp tiếp với dữ liệu
thật; từ chối thì agent phải tìm phương án khác ngay trong phiên.

**Vì sao chốt bằng `threading.Event` chứ không bằng asyncio:** phòng họp chạy trong luồng nền
(`asyncio.to_thread` → `anyio.run`), còn endpoint duyệt chạy trên event loop của API. Hai thế giới
khác nhau, nên điểm gặp phải là primitive của threading. Đổi lại, endpoint duyệt vẫn trả lời được
trong lúc phòng họp đứng im — nút Duyệt không bao giờ bị chính việc chờ của nó làm treo.

**Chống chạy hai lần:** `decide()` (bên duyệt) và `abandon()` (bên phòng họp, lúc hết hạn) dùng
chung một lock và cùng thao tác trên một registry. Quyết định về đúng khoảnh khắc hết hạn thì ai
lấy được lock trước sẽ sở hữu hành động: hoặc phòng họp nhận quyết định và chạy tool, hoặc
`decide()` thấy không còn ai chờ và endpoint tự chạy như đường cũ. Không có kịch bản cả hai cùng
chạy, cũng không có kịch bản quyết định bị rơi.

**Chờ có hạn — cố ý.** Chờ vô hạn là cách chắc chắn nhất để một demo bị treo vĩnh viễn (không ai
trực 24/7). `HITL_APPROVAL_TIMEOUT` mặc định 600s: hết hạn thì phát `het_han_cho_duyet` và họp
tiếp, hành động **vẫn** ở `cho_duyet` để duyệt muộn vẫn thực thi được — đúng hành vi cũ, nay là
đường lùi chứ không còn là đường chính.

**Đã cân nhắc nhưng không làm:** (a) lưu trạng thái chờ xuống DB rồi cho phòng họp *hồi sinh* sau
restart — đúng hướng cho production, nhưng cần checkpoint được cả hội thoại GroupChat, quá xa phạm
vi PoC; (b) để endpoint duyệt tự chạy tool rồi "tiêm" kết quả vào phiên đang chạy — vẫn là hai
luồng tranh nhau một hành động, phức tạp hơn mà không an toàn hơn; (c) gửi tin Lễ tân ngay khi
duyệt trong lúc phòng họp còn chạy — bỏ, vì người báo sẽ nhận hai tin nói về cùng một việc; cuối
phiên tổng hợp một thể.

**Kiểm chứng (không cần LLM):** chạy `execute()` trong một luồng như phòng họp thật, quyết định từ
luồng khác — duyệt (tool chạy, agent nhận `ket_qua` thật, ticket quay lại `dang_xu_ly`), từ chối
(tool không chạy), hết hạn 1s (họp tiếp, duyệt muộn vẫn chạy), tắt cờ HITL (đúng hành vi cũ),
sandbox (không bao giờ chặn). Thêm một lượt qua chính endpoint HTTP bằng `TestClient`: `GET
/api/actions` báo `phong_hop_dang_cho`, timeline có `room_waiting` → `action_executed` →
`room_resumed`, và duyệt lần hai bị chặn 400.

## Lễ tân hỏi những thứ hệ thống đã biết — sửa ở dữ liệu vào prompt, không chỉ ở câu chữ prompt

Trace thật (`data/trace/2026-09-23.jsonl`) cho ba lỗi riêng biệt, không phải một:

1. **Câu hỏi rỗng.** Model trả về `"tra_loi": "…vui lòng cung cấp thêm một số thông tin:"` —
   kết thúc bằng dấu hai chấm, không một câu hỏi nào — kèm `truong_da_co: {}` dù người báo vừa nói
   rõ việc ("điều hòa phòng tôi bị hỏng"). Người báo không biết phải trả lời gì.
2. **Hỏi lại thứ đã biết.** Lễ tân hỏi "vị trí cụ thể của máy giặt trong căn hộ (tầng, phòng, khu
   vực trong phòng)". Mã căn hộ hệ thống đã có từ `resident_id`; chỗ đứng của một thiết bị gắn cố
   định thì không đổi cách xử lý.
3. **Hỏi vòng vo không dừng.** Chạy lại bằng model thật: lượt 2 người báo trả lời đúng câu được
   hỏi, model vẫn lặp nguyên câu hỏi cũ và `mo_ta` vẫn rỗng — ticket không bao giờ mở.

**Gốc của (2) không nằm ở câu chữ prompt mà ở dữ liệu vào prompt:** `intake()` nhận `requester_id`
và `subject_id` như tham số hàm nhưng **không hề đưa chúng vào prompt**. Lễ tân thật sự không biết
người báo là ai, nên hỏi là hợp lý. Nay tầng API truyền `requester_profile` và lõi render thành
khối "THÔNG TIN NGƯỜI BÁO — hệ thống đã biết sẵn, TUYỆT ĐỐI KHÔNG hỏi lại". Cố tình nhận một dict
tự do do tầng API đưa xuống, chứ không để lõi tự đọc `cu_dan.json`: lõi không được biết tên tệp hay
tên trường của một domain cụ thể. Chỉ gửi những trường cần cho tiếp nhận — email và ngày vào ở
không giúp gì nên không đẩy sang LLM.

**Gốc của (3) là trạng thái bị mất giữa các lượt.** Lịch sử hội thoại chỉ lưu câu chữ hiển thị, nên
`truong_da_co` của lượt trước không có trong context: mỗi lượt model phải trích lại mọi trường từ
văn bản thô, và nó rơi trường. Nay trạng thái tiếp nhận lưu ở `ChatMessage.data` (cột JSON mới,
`_patch_missing_columns()` tự thêm vào DB cũ — đã kiểm trên bản sao `demo.db` 679 dòng), bơm lại
vào prompt mỗi lượt, và code **cộng dồn** `{**đã_có, **model_trả_về}` thay vì tin vào lượt cuối.
`_collected()` còn lùi qua những lượt không mang trạng thái, vì một câu trả lời nhanh về thông tin
chung xen vào giữa từng xóa sạch những gì đã thu thập.

**Ba guard trong code, không phải ba dòng prompt** (prompt cũng đã siết, nhưng model nhỏ vẫn trượt):
bù câu hỏi khi tin nhắn bế tắc, thay bằng câu chốt khi đã đủ trường mà vẫn xin thêm, và mở phản ánh
sau `INTAKE_MAX_TURNS` lượt. Guard bù câu hỏi **cố tình hẹp**: chỉ chữa khi tin nhắn cụt ở dấu hai
chấm hoặc không có dấu hiệu nào là đang hỏi — một câu nhờ ở thể mệnh lệnh ("Vui lòng mô tả giúp
tình trạng…") là câu hỏi hợp lệ, chèn thêm vào chỉ thành lải nhải. Bản đầu tiên của guard này bắt
theo "không có dấu ?" nên sửa cả những câu vốn đã đúng và sinh ra dấu chấm kép; đã thu hẹp lại.

**Nhãn trường trong `domain.yaml` cũng là một phần của lỗi.** "Vị trí cụ thể (trong căn hộ hay khu
vực chung, ở đâu)" chính là thứ mời model đi hỏi chỗ đứng của thiết bị. Sửa thành "Sự cố ở trong căn
hộ của người báo, hay ở khu vực chung (nếu khu vực chung thì chỗ nào)" — phần từ ngữ đặc thù khách
hàng nằm đúng chỗ của nó, trong domain pack.

**Đo lại bằng model thật sau khi sửa:** "điều hòa phòng tôi bị hỏng" → mở ticket ngay lượt đầu với
`mo_ta` và `vi_tri` tự suy ra, không hỏi câu nào. "Rác tồn đọng ở hành lang tầng 12" →
`vi_tri: "hành lang tầng 12"`, cũng một lượt. Câu hỏi thông tin chung vẫn đi đường trả lời nhanh
kèm `nguon: faq_chung.md`. Thêm 38 phép kiểm ngoại tuyến (stub LLM bằng đúng payload đã gây lỗi)
cho cả năm nhánh: câu hỏi rỗng, không hỏi lại thứ đã biết, guard không đụng vào câu hỏi hợp lệ,
cộng dồn trường khi model quên, và trần số lượt.

## Duyệt nhiều bên: "ai duyệt" là thuộc tính của tool, không phải luật trong code

Bản HITL đầu tiên chỉ có MỘT người duyệt — quản lý — nên `requires_approval: true` là đủ. Quy
trình thật thì không phải vậy: cư dân chốt phương án, BQL duyệt điều đơn vị, đơn vị xác nhận tiếp
nhận, đơn vị báo xong, cư dân nghiệm thu, rồi mới đóng phòng. Năm bước, ba bên khác nhau bấm nút.

**Câu hỏi "chỉ cần sửa tool thôi đúng không" — gần đúng, nhưng thiếu một mảnh.** Sửa tool là đủ để
có *chuỗi hành động*, vì cơ chế chặn-và-chờ đã có sẵn từ bản HITL. Nhưng "ai được bấm duyệt" thì
không thể nằm trong tool: hàng đợi là của platform, không phải của tool. Nên chỗ cần đổi là
**một trường khai báo** — `approval_role` trong `catalog.yaml` — cộng với việc `ToolExecutor` đọc
trường đó và ghi vào `Action.approver_role`. Sau đó mọi thứ còn lại đúng là "chỉ sửa tool": thêm
một bên duyệt mới không phải sửa một dòng nào trong `backend/core/`.

**Vì sao không hard-code ba vai trò trong lõi:** danh sách vai trò khai ở `approval_roles` trong
`domain.yaml` kèm nhãn hiển thị và nơi hiện (`console` hay `resident`). Lõi chỉ định tuyến theo id
chuỗi. Một khách hàng khác có "ban quản trị chung cư", "tổ trưởng dân phố" thì sửa domain pack,
không sửa code.

**Quy trình cũng là dữ liệu.** `quy_trinh_xac_nhan` khai năm bước, mỗi bước khớp bằng một DANH
SÁCH tool (`dieu_ktv_khan_cap` / `dieu_to_an_ninh` / `dieu_to_ve_sinh` cùng là bước "BQL duyệt
điều đơn vị"). Thêm nhà thầu thang máy = thêm tool của họ vào đúng bước. `backend/core/workflow.py`
tính trạng thái bằng một luật duy nhất: *bước xong khi có một hành động `da_thuc_hien` của một
trong các tool thuộc bước đó trên cùng phản ánh*.

**Chặn đóng phòng, nhưng chỉ khi quy trình đã bắt đầu.** Guard `quy_trinh_xac_nhan_chua_xong` nhắc
Điều phối một lần kèm tên bước còn thiếu rồi mới cho kết thúc — không lặp vô hạn, vì một phòng họp
không thể tự làm thay việc của người duyệt. Điều kiện "đã bắt đầu" là bắt buộc: nếu không, mọi
phản ánh chỉ hỏi thông tin (thắc mắc phí, hỏi nội quy) sẽ không bao giờ đóng được.

**Ba MCP server thay vì một.** `an_ninh` (:8102) và `ve_sinh` (:8103) dựng cùng khuôn với
`ky_thuat` (:8101) — mỗi bên một tiến trình, một kho bản ghi riêng, và **không bên nào biết gì về
việc duyệt**: chúng chỉ thực thi khi được gọi. Đó là điểm cần chứng minh — chốt chặn nằm ở tầng
platform, không phụ thuộc thiện chí của hệ thống bên thứ ba.

**Một lỗi do chính bộ test này lộ ra:** `cu_dan_xac_nhan_hoan_thanh` đặt phản ánh sang `hoan_tat`,
nhưng `_wait_for_manager` sau khi duyệt xong lại đặt vé về `dang_xu_ly` — nghiệm thu xong vé vẫn
"đang xử lý". Sửa bằng `only_if="cho_duyet"`: lúc họp tiếp chỉ khôi phục trạng thái nếu vé vẫn
đang ở đúng trạng thái mà lúc dừng đã đặt, không ghi đè thứ tool vừa đặt.

**Kiểm chứng (không cần LLM):** chạy đủ năm bước qua `ToolExecutor` thật với ba MCP server thật —
mỗi bước dừng đúng hàng đợi (`cu_dan`/`bql`/`don_vi`), duyệt xong tool chạy thật (mã `DKC-`,
`KTX-`…), bước sau mới mở ra; một lần từ chối ở bước 4 để chắc bước không bị tính là xong; hết
bước 5 thì vé sang `hoan_tat` và guard hết chặn; vé chỉ hỏi thông tin không bị chặn. Giao diện
kiểm bằng Chrome headless: dải 5 bước, hai hàng đợi tách đúng vai trò, badge riêng từng hàng đợi,
thẻ "Đồng ý / Chưa đồng ý" hiện trong khung chat cư dân, cột duyệt của tool catalog hiện tên vai trò.

## Vòng lặp "hỏi lại người báo": sửa ở chỗ mất trí nhớ, không sửa bằng cách đoán câu hỏi giống nhau

Vé thật TK-3656061B (2026-09-23) chạy ba vòng liền y như nhau: phòng họp mở → Kỹ thuật phát biểu
**đúng một lượt** → hỏi người báo một câu → phòng đóng (`so_luot: 1`) → Lễ tân đi hỏi → người báo
trả lời → phòng mở lại → hỏi tiếp. Người báo trả lời ba lần, vé vẫn đứng ở `cho_cu_dan`.

Đọc dữ liệu vé thì thấy hai nguyên nhân tách bạch:

1. **Câu trả lời không gắn với câu hỏi.** `add_followup` cất câu trả lời vào `thong_tin_bo_sung`
   dưới dạng danh sách văn bản rời. Lượt sau agent nhìn vào TICKET thấy `["máy tự mua, không,
   không"]` mà không biết đó là trả lời cho câu nào — nên nó hỏi tiếp cho chắc.
2. **Không có gì đếm số vòng.** Một vé bật qua lại `cho_cu_dan` bao nhiêu lần cũng được.

**Sửa (1) bằng cách ghép cặp:** phòng họp ghim câu hỏi lên ticket lúc đóng (`cau_hoi_dang_cho`),
`add_followup` ghép nó với câu trả lời thành `hoi_dap: [{hoi, dap}]`, và `format_ticket` in riêng
thành khối "ĐÃ HỎI NGƯỜI BÁO VÀ ĐÃ CÓ CÂU TRẢ LỜI (TUYỆT ĐỐI KHÔNG hỏi lại những ý này)". Agent
đọc thẳng câu trả lời trong ngữ cảnh của chính câu hỏi nó từng đặt.

**Sửa (2) bằng trần cứng:** `MAX_FOLLOWUP_ROUNDS` (mặc định 2). Quá trần thì guard
`khong_hoi_lai_nguoi_bao` **xóa** `can_hoi_them_nguoi_bao` khỏi output của agent — không chỉ cảnh
báo, vì để nguyên thì Lễ tân vẫn đem câu hỏi đó đi hỏi. Phòng họp buộc phải kết luận với thông tin
đang có; thiếu gì thì nêu giả định, đó vẫn tốt hơn bắt người báo trả lời vòng thứ tư.

**Đã thử và BỎ: so khớp "câu hỏi trùng ý" bằng từ khóa.** Ý tưởng là chặn ngay khi agent hỏi lại
cùng một ý dù diễn đạt khác. Đo trên đúng ba câu hỏi của vé lỗi (bỏ dấu, bỏ hư từ, so tập từ):
cặp **trùng ý** đạt overlap 0.50, trong khi một cặp **khác ý** lại đạt 0.75. Không có ngưỡng nào
tách được hai nhóm — hạ ngưỡng để bắt cặp trùng ý thì chặn nhầm câu hỏi chính đáng. Giữ lại đúng
phần chắc chắn: `is_repeat()` ngưỡng 0.8, chỉ bắt câu lặp gần như NGUYÊN VĂN. Phần "trùng ý" giao
cho hai cơ chế không phải đoán: trần số vòng, và việc bơm thẳng câu trả lời vào TICKET.

**Kiểm chứng (không LLM):** ghim → ghép cặp → hàng chờ được xóa; hỏi lặp gần y hệt bị chặn kèm
nguyên văn câu trả lời cũ; câu hỏi khác ý KHÔNG bị chặn nhầm; hết vòng 2 thì mọi câu hỏi bị chặn;
khối ĐÃ HỎI hiện đúng trong prompt và không in thô khóa nội bộ; người báo tự nhắn thêm lúc không
có câu hỏi nào đang chờ vẫn được lưu, ghi rõ là tự bổ sung; vé khác không bị ảnh hưởng.
