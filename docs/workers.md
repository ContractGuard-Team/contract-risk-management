# Worker hai tiến trình trong multi-container

Theo plan_work.txt, Dockerfile/Compose/dependency/môi trường worker riêng đã bỏ.
Root docker-compose.yml quản lý worker cùng bảy service còn lại. Source AWS connectors,
JobStore DynamoDB, lease/heartbeat, S3 attempt output key và persistence-before-ACK giữ nguyên.

Intake: SQS → tải S3 → processing HTTP → ContractInput → LangGraph HTTP → kết quả.
Progress: sự kiện IPC → ghi đè tiến độ DynamoDB → lưu Agent 3 output S3 → completed/ACK.
Worker không gọi trực tiếp hoặc điều phối agent; LangGraph đảm nhiệm thứ tự ba stage.

WORKER_ENABLED=false là idle local, không ReceiveMessage. Khi true supervisor spawn hai
process và readiness kiểm tra cả hai còn sống. SIGTERM chờ grace rồi đóng các process;
job chưa hoàn tất giữ message, lease hết hạn cho retry. Chỉ DeleteMessage sau output
được lưu và xác nhận lại bằng consistent read DynamoDB.

Message SQS (định dạng nội bộ) giữ job_id/contract_id/contract_version, source_file_key (+ bucket, mặc định
WORKER_INPUT_BUCKET); runtime_id/runtime-id tùy chọn. Tên mục trùng với ContractGuard (2026-11-16); tên cũ version,
s3_bucket/s3_key/s3_version_id, source đã bỏ. Schema: `common/schemas/worker/sqs_input.schema.json` (mục cuối). Không tạo AWS resource trong code vận hành.
Config AWS quyền/region, token và internal URL xem [environment](environment.md).

Nhánh đọc Langfuse cũ còn để tương thích caller/test library; không được dùng trong
entrypoint Compose. Monitoring mới nhận telemetry riêng, không phải kênh hoàn tất job.
Chi tiết JSON và durable HTTP jobs: [container_interfaces.md](container_interfaces.md).

## Hai bảng DynamoDB (ContractJobStore, viết lại 2026-11-24)

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

## Cancel, lỗi và output lỗi (2026-11-24)

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

## Kết nối ContractGuard (2026-11-15, mới nhất)

Theo `07-worker-aws-integration.md` §1–4 của đối tác. Bật bằng `WORKER_RESULTS_QUEUE_URL`; kiến trúc worker giữ nguyên
(2 tiến trình, lease/progress DynamoDB trong tài khoản AI, input-processing → LangGraph → 3 agent, kết quả S3 trước
completed trước ACK). Code thêm: `workers/partner.py` + móc nhỏ trong intake/progress/heartbeat; định dạng message nội bộ cũ
vẫn chạy khi biến này rỗng.

| Tài nguyên (tài khoản 699061129385, ap-southeast-1) | `.env` mục 6 |
| --- | --- |
| queue việc `contractguard-dev-analysis-requests` | `WORKER_SQS_QUEUE_URL` |
| queue kết quả `contractguard-dev-analysis-results` | `WORKER_RESULTS_QUEUE_URL` |
| bucket `contractguard-dev-documents-699061129385` | `WORKER_INPUT_BUCKET` = `WORKER_OUTPUT_BUCKET` (bắt buộc bằng nhau) |

Vòng xử lý:
1. `ping` → gửi `pong` (cùng `job_id`) rồi xóa message.
2. `analysis_requested` → đọc `request_key`, phải đúng `results/<job_id>/request.json` của chính message (và
   `request.json.job_id`, nếu có, phải trùng). Key lấy từ request, kiểm tra tiền tố (`quarantine/`, `clean/`, `results/`),
   không tự ghép; `result_key` phải đúng `results/<job_id>/extraction.json` (khớp IAM của đối tác) — sai thì báo lỗi
   đầu vào trước khi chạy agent. `output_language` (mã ngôn ngữ) và `limits` được đưa vào `context` để agent đọc;
   worker tự áp `limits.max_file_bytes` (chỉ siết thêm, ≤ 20 MB) và `limits.max_pages` (theo `page_count` của
   input-processing). Tên 2 khóa limits là giả định, chờ `analysis-request.v1`. `contract_id`/`contract_version` lấy từ `clean/{workspace}/{contract}/v{n}/…`
   (hoặc trường `contract_id`/`contract_version` nếu có); `output_language`, `limits`, các key lưu ở `metadata.contractguard`.
3. Mỗi bước gửi `progress` (`step` = tên stage của worker: downloading, dispatching, processing, agent_1/2/3,
   saving_result); heartbeat lặp lại bước hiện tại mỗi 5 phút để backend không đánh lỗi sau 20 phút; visibility vẫn gia hạn 30 s/lần.
4. Sau khi input-processing nhận file: ghi bản đã kiểm tra ra `clean_file_key`. Kết quả ghi ra `result_key`
   (có `document.clean_file_key`) bởi tiến trình progress, rồi DynamoDB completed → gửi `completed` → xóa file
   `quarantine/` → xóa message cuối cùng.
5. Lỗi đầu vào (`is_input_error`: request/key/định dạng/kích thước sai, `request.json` hỏng, `job_id` trùng với request khác)
   → `failed` + `ERR-AI-001`, xóa message ngay. Lỗi khác, kể cả ValueError nội bộ (LangGraph/Agent 3 trả kết quả không
   hợp lệ) → giữ message cho SQS gửi lại; tới lần nhận thứ 3 → `failed` + `ERR-AI-002`, xóa message.
   Job đã `completed` trong DynamoDB thì **không bao giờ** gửi `failed`: giữ message để lần giao lại gửi lại `completed`.
   Xóa file `quarantine/` lỗi chỉ ghi log, không chặn ACK. `failure_reason` không chứa nội dung hợp đồng (lỗi service: `Remote job failed: <loại lỗi>`).
6. Message trùng sau khi đã xong → gửi lại `completed`, xóa quarantine (idempotent), không chạy lại 3 agent.

Mọi message gửi lên queue kết quả qua `check_outbound` (đúng bộ trường §3 theo `event_type`, chuỗi không rỗng,
`result_key` đúng mẫu, `failure_reason` ≤ 2000, `step` thuộc 4 bước, `error_code` dạng ERR-XXX-000); sai thì không gửi.
Cấu trúc vào/ra chi tiết: mục cuối "Cấu trúc vào/ra của worker".

Còn phụ thuộc đối tác: danh mục ERR-* đầy đủ (đang dùng ERR-AI-001/002 tạm, ERR-UPL-004 của đối tác), danh mục
BR-RISK-006/007 và fact_key (xem `docs/agent_core.md`). "Làm sạch" hiện = chép bytes đã được input-processing chấp nhận, giữ ContentType của file upload.
Kiểm chứng: `tests/unit/test_worker_partner.py` (Moto: ping/pong, trọn vòng, giao trùng, lỗi đầu vào, retry tới lần 3,
keepalive) + 19 test worker cũ; chưa chạy với AWS thật / LocalStack của đối tác.

## Schema nghiệp vụ vào/ra với ContractGuard (2026-11-17)

Schema RIÊNG của project, viết bằng Pydantic trong `common/schemas/business/` (image nào cũng có, không thêm thư viện),
dựng từ các mục dữ liệu trong message/file đối tác gửi — không chép file schema của họ.

| File | Model | Vai trò |
| --- | --- | --- |
| `inbound.py` (NHẬN) | `InboundMessage` = `Ping` / `AnalysisRequested`; `AnalysisRequest` (+ `ContractContext`, `Limits`) | Mọi mục ContractGuard gửi đều có trường; trường lạ được GIỮ (`extra="allow"`) để không mất dữ liệu |
| `outbound.py` (TRẢ) | `OutboundMessage` = `Progress` / `Completed` / `Failed` / `Pong`; `AnalysisResult` (+ Document, Clause, Location, Region, Finding, Scoring, DomainScore, Trace, Embeddings) | Mọi mục ContractGuard nhận đều có trường; khóa lạ bị cấm; trường tùy chọn không có giá trị thì bỏ (`to_json`) |
| `shared.py` | Evidence, Reference, Fact, Check, ContractMetadata, InjectionWarning, kiểu Id/ClauseType/CheckCode/Domain/RiskLevel, `CHECK_CODES` | Khối dùng chung cho cả hai chiều và output 3 agent |

Luồng: `PartnerProtocol.read` kiểm message bằng `InboundMessage`, đọc request.json bằng `AnalysisRequest` → QueueJob với
`context = {"request": <request.json đúng như đối tác gửi>}` → input-processing → LangGraph → 3 agent đọc
`context.request` (output_language, bối cảnh người dùng xác nhận, key tài liệu gốc…) → `orchestration/extraction.py`
dựng `AnalysisResult` (tenant_id, output_language, clean_file_key lấy từ `context.request`) → worker ghi `result_key`.
Message ra luôn qua `check_outbound` (= `OutboundMessage`, đúng bộ khóa sẽ gửi).

- Bước tiến độ: downloading/dispatching/processing → `reading_file`, agent_1 → `splitting_clauses`, agent_2/agent_3 →
  `evaluating`, saving_result → `scoring`; chỉ gửi khi đổi bước.
- `limits.max_bytes` siết giới hạn tải; `limits.max_pages` vượt → `failed` ERR-UPL-004; loại file thật khác
  `declared_file_type` → ERR-AI-001; `result_key` phải là `results/<job_id>/extraction.json` (IAM đối tác);
  `source_file_key` ở `quarantine/` hoặc `clean/` (chỉ xóa khi ở quarantine/).
- Trích dẫn/trang được code kiểm trước khi ghi (quote không nằm trong điều khoản bị bỏ, finding hết bằng chứng bị bỏ,
  trang sai được đặt về trang đầu điều khoản).
- Đã đối chiếu ngoài repo với các file gốc của đối tác (Downloads): kết quả + 8 loại message 0 lỗi; request đủ mọi trường
  tùy chọn và 2 loại message được model nhận lại nguyên vẹn. `extraction.schema.json` (v1) và `job-result.schema.json`
  là định dạng nội bộ backend, không đi qua worker. `embeddings` có trong model nhưng chưa sinh (Phase 4).
- File TXT chỉ có ở chế độ nội bộ (ContractGuard không có `txt`): kết quả cùng bố cục nhưng không ép qua `AnalysisResult`.

