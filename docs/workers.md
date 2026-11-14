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

Message SQS giữ job_id/contract_id/version, s3_bucket/s3_key/s3_version_id hoặc source
object; runtime_id/runtime-id tùy chọn. Không tạo AWS resource trong code vận hành.
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
