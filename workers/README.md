# Worker trong hệ thống Compose chung

Source worker tiếp tục ở folder này, gồm runtime hai tiến trình và AWS/HTTP connectors.
Dockerfile, dependency locks, Compose và env đã chuyển về root/infrastructure theo
plan_work.txt. Không build folder workers/ độc lập nữa.

- connectors/processing_service.py: gửi JSON/file bytes tới input-processing, đợi ContractInput.
- connectors/workflow_service.py: submit/poll LangGraph, nhận output Agent 3.
- runtime/intake_worker.py: SQS intake/lease/heartbeat; không điều phối từng agent.
- runtime/progress_worker.py: persistence DynamoDB/S3 và ACK nội bộ.
- main.py: supervisor hai process khi WORKER_ENABLED=true; HTTP readiness.

WORKER_ENABLED=false chạy idle rõ ràng để cấu hình local không gọi AWS. Khi bật,
SQS URL/table/buckets/internal URLs/token phải đủ. Root .env và Compose cấp cấu hình;
Config Service có thể cung cấp secret runtime. Thiếu runtime_id sinh UUID4, retry giữ
ID/hash và quyền sở hữu; S3 output lưu trước DynamoDB completed và DeleteMessage.

Langfuse reader/multipart client cũ còn giữ như adapter library để không phá caller cũ;
entrypoint Compose luôn dùng HTTP processing + LangGraph, không cần Langfuse API key.

Xem [worker](../docs/workers.md), [giao thức](../docs/architecture.md) và [vận hành](../docs/operations.md).
