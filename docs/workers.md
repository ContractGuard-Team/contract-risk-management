# Worker và giao thức ContractGuard

## 1. Cách hoạt động

`python -m workers` mở supervisor và các tiến trình con:

| Tiến trình | Việc |
| --- | --- |
| intake | SQS → DynamoDB lease → tải S3 → input-processing → LangGraph → trả lời → xóa message. Xử lý một hợp đồng mỗi lúc |
| progress | Nhận sự kiện từ intake, ghi S3 rồi DynamoDB |
| chat (khi có `WORKER_CHAT_QUEUE_URL`) | Trả lời chatbot, tách riêng để câu hỏi không chờ sau một phân tích dài |

- Thứ tự bắt buộc: S3 → DynamoDB `completed` → gửi `completed` → xóa quarantine → xóa message SQS.
- Heartbeat gia hạn lease và visibility mỗi 30 giây; hết lease thì worker khác nhận lại. `runtime_id` giữ nguyên qua retry.
- `WORKER_ENABLED=false`: chế độ idle, không đọc SQS. SIGTERM: job đang dở giữ message để chạy lại.
- Worker không gọi agent trực tiếp; không đọc Langfuse để hoàn tất job.

## 2. Giao thức ContractGuard

Bật bằng `WORKER_RESULTS_QUEUE_URL` (`workers/partner.py`). Để trống thì nhận message nội bộ: `job_id`, `contract_id`,
`contract_version`, `source_file_key`, `bucket`.

| Tài nguyên (699061129385, ap-southeast-1) | Biến |
| --- | --- |
| `contractguard-dev-analysis-requests` | `WORKER_SQS_QUEUE_URL` |
| `contractguard-dev-analysis-results` | `WORKER_RESULTS_QUEUE_URL` |
| `contractguard-dev-chat-requests` | `WORKER_CHAT_QUEUE_URL` |
| `contractguard-dev-documents-699061129385` | `WORKER_INPUT_BUCKET` = `WORKER_OUTPUT_BUCKET` (bắt buộc bằng nhau) |

1. `ping` → `pong`, xóa message.
2. `analysis_requested` → đọc `results/<job_id>/request.json` (`AnalysisRequest`). Mọi S3 key lấy từ request, worker không tự ghép;
   `result_key` phải là `results/<job_id>/extraction.json`. Toàn bộ request vào `context.request`.
3. Gửi `progress` khi đổi bước: downloading/dispatching/processing → `reading_file`, agent_1 → `splitting_clauses`, agent_2/3 →
   `evaluating`, saving_result → `scoring`. Lặp lại bước hiện tại mỗi 5 phút (backend đánh lỗi sau 20 phút im lặng).
4. Input-processing nhận file → ghi bản sạch ra `clean_file_key`. Xong → `extraction.json` → `completed` → xóa `quarantine/`.
5. Message trùng sau khi xong → gửi lại `completed`, không chạy lại.

Kiểm đầu vào: `limits.max_bytes` chỉ siết thêm; quá `limits.max_pages` → `ERR-UPL-004`; loại file thật khác `declared_file_type` → lỗi đầu
vào. File hỏng hoặc không khớp đuôi file bị input-processing từ chối, hiện được thử đủ 3 lần rồi báo `ERR-AI-002`.

Mọi message gửi ra qua `check_outbound` (= `OutboundMessage`); sai schema thì không gửi. `AnalysisResult` được LangGraph dựng và kiểm,
worker chỉ ghi. File TXT chỉ có ở chế độ nội bộ.

**Chatbot** (`workers/chat.py`, `agents/chat.py`): `chat_requested` → đọc `ChatRequest` → `POST agent-3/v1/chat` → `chat_answered`. Câu hỏi
và lịch sử là dữ liệu, không phải chỉ dẫn. Backend chờ 3 phút nên lỗi được trả `failed` ngay, không thử lại.

## 3. Hai bảng DynamoDB (`ContractJobStore`)

Khi có `WORKER_DYNAMODB_EVENTS_TABLE` (Compose mặc định `contract-risk-job-events`), worker dùng
`workers/runtime/contract_job_store.py`. Chỉ worker ghi hai bảng này; monitoring không còn ghi vào DynamoDB.

**jobs** — mỗi hợp đồng một dòng, khóa `job_id = contract:<contract_id>`, trả lời "hợp đồng đang ở đâu":

| `location` | Khi nào |
| --- | --- |
| `worker` | vừa nhận, đang tải file, đang lưu kết quả, đã xong |
| `input_processing` | đang tách/OCR |
| `langgraph` | đã bàn giao cho LangGraph, chưa vào agent |
| `agent_1` / `agent_2` / `agent_3` | đang ở agent đó (lấy từ polling LangGraph, agent chạy quá nhanh có thể không kịp hiện) |

`status`: `queued`, `running`, `retrying` (lần chạy lỗi, SQS sẽ gửi lại), `completed`, `failed`, `cancelled`. Mỗi lần chuyển
bước, `status/location/stage/progress/details` bị ghi đè toàn bộ nên log cũ mất. Xong việc thì dòng giữ nguyên cho tới khi có
job khác cùng `contract_id` (khác `active_job_id`): lúc đó cả dòng được thay mới.

**events** — khóa `job_id` + `event_id="outcome"`, tối đa một log cho mỗi job, chỉ ghi khi: `completed` (kèm `output`),
`failed` (lỗi đầu vào, hoặc hết số lần thử; kèm `location`, `error_type`, `reason`, `attempts`, `output`), `cancelled` (kèm
`location`). Đang chạy hoặc đang thử lại thì không ghi gì. Job `failed` mà cùng `job_id` được gửi lại và chạy xong thì log
được thay bằng kết cục mới. `completed` và `cancelled` không ghi đè nhau (điều kiện ngay trên dòng log).

Thứ tự lưu giữ nguyên: S3 → một transaction (dòng jobs + log outcome) → trả lời queue kết quả → xóa message SQS.
Dòng cũ trong bảng events (`registered`, `trace`, `worker:*`, `monitoring:*`) không còn được đọc, trừ biên nhận `completed` cũ;
xóa tay nếu muốn bảng gọn.

## 4. Cancel, lỗi và output lỗi

Bản gửi ContractGuard: [partner_cancel_and_failure.md](partner_cancel_and_failure.md); sửa giao thức thì sửa cả hai.

**Cancel** — `ContractJobStore.cancel(job_id)`, gọi từ `workers/cancel.py` theo hai đường:

| Đường | Cách gọi |
| --- | --- |
| SQS (cho nút cancel của ContractGuard) | `WORKER_CANCEL_QUEUE_URL`, message `{"message_version":"1","event_type":"cancel_requested","job_id":"..."}` |
| HTTP nội bộ | `POST worker:8000/v1/jobs/<job_id>/cancel`, `Authorization: Bearer INTERNAL_API_TOKEN` → `{"status": "cancelled" \| "completed" \| "failed"}` |

Phải là queue riêng: queue yêu cầu chỉ được đọc giữa hai hợp đồng nên message cancel sẽ tới quá trễ. Role worker cần
`sqs:ReceiveMessage` + `sqs:DeleteMessage` trên queue cancel. Tên message và mã `ERR-AI-003` là tạm, chờ đối tác chốt.

Diễn biến: log `cancelled` được ghi ngay → worker kiểm mỗi giây, dừng → gọi `POST /v1/jobs/<key>/cancel` của service đang chạy,
LangGraph tự hủy tiếp job ở agent → runner bỏ lại lời gọi model/OCR đang treo (kết quả bị vứt, dừng ở `guard` kế tiếp, không
gọi model thêm) và nhận việc mới ngay → queue kết quả nhận `failed` + `ERR-AI-003` (giao thức chưa có event `cancelled`) →
xóa message → hợp đồng kế tiếp. Cancel trước khi message tới: message tới là bị trả lời và xóa, không chạy. Đã xong: không đổi gì.
File `quarantine/` giữ nguyên khi cancel hoặc lỗi.

**Lỗi** — lỗi đầu vào dừng ngay; lỗi khác để SQS gửi lại, lần nhận thứ 3 vẫn lỗi thì dừng (`MAX_RECEIVES`), áp dụng cả message
nội bộ. Khi dừng: ghi output lỗi ra S3 (`result_key`; message nội bộ: prefix kết quả) → log `failed` → message `failed`
(`failure_reason`, `error_code`) lên queue kết quả → xóa message. Output lỗi = `error_result()` trong
`common/schemas/business/outbound.py`: đủ mọi khóa của `AnalysisResult`, mục không có là `null`, `document_complete=false`.
Backend chỉ đọc `result_key` sau `completed` nên file này không bị hiểu là kết quả. Request sai tới mức không đọc được
`result_key` thì chỉ có log và message `failed`.
