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
s3_bucket/s3_key/s3_version_id, source đã bỏ. Schema: `docs/schemas/worker_sqs_input.schema.json`. Không tạo AWS resource trong code vận hành.
Config AWS quyền/region, token và internal URL xem [environment](environment.md).

Nhánh đọc Langfuse cũ còn để tương thích caller/test library; không được dùng trong
entrypoint Compose. Monitoring mới nhận telemetry riêng, không phải kênh hoàn tất job.
Chi tiết JSON và durable HTTP jobs: [container_interfaces.md](container_interfaces.md).

## Hai bảng DynamoDB tiến độ (ContractJobStore)

Khi có `WORKER_DYNAMODB_EVENTS_TABLE` (Compose mặc định `contract-risk-job-events`), worker dùng
`workers/runtime/contract_job_store.py`:

| Bảng | Khóa | Nội dung |
| --- | --- | --- |
| `WORKER_DYNAMODB_TABLE` (jobs) | `job_id = contract:<contract_id>` | Trạng thái hiện tại của hợp đồng: active_job_id, runtime, owner/lease, stage, progress, status, output (bucket/key S3) |
| `WORKER_DYNAMODB_EVENTS_TABLE` (events) | `job_id` + `event_id` | Lịch sử chỉ-thêm: `registered`, `trace`, `worker:<id>` mỗi phase, `monitoring:<id>` từ monitoring, `completed` (biên nhận) |

Luồng output Agent 3: LangGraph result → `WorkflowServiceClient` kiểm tra identity/Agent 3 → intake →
IPC → progress writer ghi S3 trước → một `TransactWriteItems` cập nhật bảng jobs (completed, 100, output)
và thêm `worker:<id>` + `completed` vào bảng events → ACK → intake đọc lại (consistent) → DeleteMessage.
S3 lỗi thì không ghi completed (unit test `test_s3_failure_never_completes_or_appends_completion`).

Mỗi phase (downloading, dispatching, processing, agent_1/2/3, saving_result, completed) là một transaction
hai bảng, có điều kiện owner/runtime/sequence/lease. Stage agent lấy từ việc poll LangGraph nên một stage
rất nhanh có thể không có bản ghi `worker:`; khi `MONITORING_WORKER_ENABLED=true`, event của agent-service
vẫn được ghi `monitoring:<id>` vào bảng events. E2E (Moto) xác minh 2 bảng ngày 2026-11-07.

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
   Xóa file `quarantine/` lỗi chỉ ghi log, không chặn ACK. `failure_reason` không chứa nội dung hợp đồng.
6. Message trùng sau khi đã xong → gửi lại `completed`, xóa quarantine (idempotent), không chạy lại 3 agent.

Mọi message gửi lên queue kết quả qua `check_outbound` (đúng bộ trường §3 theo `event_type`, chuỗi không rỗng,
`result_key` đúng mẫu, `failure_reason` ≤ 300, `step` ≤ 64); sai thì không gửi. Viết tay theo tài liệu, thay bằng
`queue-messages.v1.schema.json` khi có.

Còn phụ thuộc đối tác: file `extraction.json` hiện là bố cục tạm (`workers/partner.py:extraction`) cho tới khi có
`contracts/extraction.v2.schema.json` — backend sẽ từ chối kết quả không qua schema; danh mục `error_code` và giá trị `step`
chưa có (dùng tạm ERR-AI-001/002 và tên stage). "Làm sạch" hiện = chép bytes đã được input-processing chấp nhận, giữ ContentType của file upload.
Kiểm chứng: `tests/unit/test_worker_partner.py` (Moto: ping/pong, trọn vòng, giao trùng, lỗi đầu vào, retry tới lần 3,
keepalive) + 19 test worker cũ; chưa chạy với AWS thật / LocalStack của đối tác.

### Context `source_document` cho Agent 3 (2026-11-17)
Chế độ ContractGuard: `PartnerProtocol.read` thêm `context.source_document = {bucket, source_file_key, clean_file_key}`
(lấy từ `request.json`, không tự ghép). Context đi nguyên qua input-processing → ContractInput → LangGraph → agent; Agent 3
đọc tài liệu gốc bằng tool MCP `partner_document` (xem `docs/mcp.md`). Message nội bộ không có khóa này.
