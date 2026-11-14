# Kiểm thử hệ thống multi-container

Kết quả ngày 2026-11-07 sau khi thực hiện `plan_work.txt`: **106/106 unit test,
4/4 notebook, 8/8 image runtime build thành công và 8/8 service healthy**. Pipeline
xuyên container đạt cả khi Agent 2 có hai replica. AWS dùng Moto và model dùng
fixture tường minh; đây là nghiệm thu tích hợp local, không phải nghiệm thu AI/AWS thật.

## Chạy unit test

Tại root repository, Python >=3.11,<3.15; workspace dùng Python 3.12.16:

```sh
python -m pip install -e ".[test]"
python -B -m unittest discover -s tests/unit -p "test_*.py" -v
```

Windows dùng `.venv312/Scripts/python.exe`. Trong sandbox có thể cần quyền truy cập
thư mục tạm cho Python/notebook. Test không yêu cầu tài khoản hoặc credential AWS thật.

| Bộ test | Phạm vi |
| --- | --- |
| test_agent_base.py | BaseAgent, client OpenAI giả (request Chat Completions), context, prompt/config validation, tools, memory riêng từng job |
| test_contract_workflow.py | LangGraph và Hermes ContextEngine thật, 40 chunk, tổng hợp/handoff Agent 1 → 2 → 3, budget/summary, cache/resume, thay input/prompt và cô lập hợp đồng |
| test_input_processing.py | Validation, normalize, cấu trúc, chunk/budget/overlap, provenance, manifest và CLI |
| test_file_processing.py | PDF/DOCX/TXT/ảnh, bảng/numbering/note, render, metadata, S3 version/stream, giới hạn và lỗi OCR |
| test_observer.py | Callback/usage nội bộ, lọc raw content, không báo completed sai khi lỗi |
| test_workers.py | Moto SQS/S3/DynamoDB, runtime/lease/heartbeat/hash, S3-before-completed/ACK/DeleteMessage, retry, timeout, negative ACK; progress process spawn thật |
| test_worker_connectors.py | Giao thức multipart và Langfuse reader cũ được giữ cho caller library; auth, phân trang, identity, sequence, giới hạn response/lỗi |
| test_component_boundaries.py | Worker không import agent/preprocessing/orchestration; dùng common/config/monitoring để gọi API nội bộ |
| test_service_architecture.py | 12 test mới: HTTP auth/identity, provenance, job idempotency/lease/recovery/fencing/retry, agent RPC/memory/revision, fixture chỉ local, Secrets Manager/SSM, phân quyền config và runtime bootstrap, telemetry redaction/dedup, Langfuse OTLP tùy chọn |

Entrypoint worker mới không dùng reader Langfuse để hoàn tất job. Các test reader
cũ là kiểm tra tương thích, không mô tả đường chạy Compose hiện tại.

## Notebook Jupyter

Bốn notebook đã chạy bằng kernel `.venv312`, không cell lỗi:

- `tests/unit/test_input_processing.ipynb`: xử lý text và assertion.
- `tests/integration/test_file_processing.ipynb`: DOCX/ảnh mẫu, JSON/chunk/provenance.
- `tests/integration/test_worker_job_lifecycle.ipynb`: vòng đời worker, AWS giả lập,
  lỗi ghi kết quả, IPC và progress process.
- `tests/integration/test_container_services.ipynb`: kiểm tra giao thức service,
  secret/auth, memory và recovery bằng bộ test offline.

Source notebook giữ output trống; bản có kết quả nằm trong
`.cache/executed-notebooks/`, không commit. Notebook service không tự khởi động Docker;
kiểm thử xuyên container thực tế chạy bằng script dưới đây. Các notebook agent riêng
còn là khung; logic ba agent được kiểm tra trong unit test và E2E.

## Docker và E2E

```sh
python scripts/init_env.py  # Chỉ tạo nếu chưa có .env, không ghi đè cấu hình hiện tại.
docker compose config --quiet
docker compose build
docker compose up -d --wait
docker build --target test -t contract-risk/test:local -f infrastructure/docker/Dockerfile .
docker run --rm --network contract-risk_backend --env-file .env contract-risk/test:local
```

Đổi network nếu đổi COMPOSE_PROJECT_NAME. E2E yêu cầu APP_ENV=local,
MODEL_PROVIDER=fixture và Langfuse tắt. Script `tests/integration/container_smoke.py`
khởi động Moto HTTP trong test container, tạo SQS/S3/DynamoDB giả, chạy entrypoint
worker với supervisor/intake/progress thật, gọi các service Compose qua HTTP/DNS.
Không cần Docker socket trong container.

Đã xác minh:

- Tám service ready; API yêu cầu token, config từ chối alias không được cấp quyền.
- SQS → tải S3 → input-processing → LangGraph → Agent 1/2/3 → worker.
- Tự tạo runtime_id, ghi output Agent 3 lên S3 và trạng thái DynamoDB trước ACK/xóa SQS.
- Job gửi lại dùng cùng runtime/result; telemetry nối được job_id/trace_id/request_id
  và có token usage của fixture; không xuất raw contract trong events.
- Collector ghi nhận đủ tám service; chạy được khi không có Langfuse API key.
- Worker hai process dừng bằng SIGTERM, exit code 0.
- Scale riêng Agent 2 lên hai replica healthy; E2E tiếp tục đạt; đã trả về một replica.
- Workflow result và cache/memory còn sau khi recreate container. Recovery lease và
  chặn owner cũ được kiểm tra bằng unit test; chưa chaos-test mọi thời điểm crash.
- Tesseract thật trong image input-processing có eng/vie; mẫu ảnh chữ tiếng Anh được
  extract/chunk qua pipeline với vie+eng. Không dùng kết quả này để suy ra độ chính xác
  OCR cho hợp đồng scan tiếng Việt thực tế.
- Tám image Linux/amd64 chạy UID 10001, không có .env dưới /app; các giá trị token
  runtime sinh bởi init_env không có trong image metadata. `pip check` đạt cho bốn
  bộ dependency khác nhau: worker, processing, agents, langgraph.
- Script EC2 được chuyển sang LF, có .gitattributes bảo toàn LF và qua `sh -n` trên Linux.

Evidence local: `.cache/image-manifest.json`, `.cache/final-e2e.log`,
`.cache/final-e2e-exit.txt` (0), `.cache/executed-notebooks/`. Không commit cache.

## Giới hạn nghiệm thu

Chưa gọi AWS/OpenAI/Langfuse thật, chưa provision tài nguyên hoặc deploy EC2.
Prompt/model settings/memory budgets và schema/luật nghiệp vụ còn TODO theo thiết kế
cũ; fixture không phân tích hoặc chấm điểm hợp đồng thật. HTTP JSON giữ AgentResult
hiện có, không tự tạo quy tắc AI mới.

SQLite volumes và scale được kiểm tra trên một Docker host; không hỗ trợ chia sẻ
SQLite qua NFS/multi-host, không cam kết exactly-once model call. Secret được lấy lúc
startup và cache ngắn phía broker; rotation cần restart client. Telemetry không chặn
job khi collector mất kết nối; stdout là fallback, chưa có durable outbox ở client.

Xem `docs/plan_work_report.md` cho danh sách bàn giao và `docs/deployment_ec2.md` cho
các bước đưa cấu hình/model/IAM thật vào môi trường đích.

## Kiểm chứng năm điểm bàn giao (2026-11-07)

Lượt kiểm tra bổ sung chạy lại 44 unit test thuộc test_contract_workflow,
test_service_architecture và test_file_processing, đều đạt. Hai E2E mới xác minh
đủ năm điểm bàn giao bằng TXT tiếng Việt 3 chunk và PNG qua Tesseract thật 1 chunk.
Audit so sánh bytes tải từ S3, manifest, metadata, memory/message model, aggregate
Agent 1, context Agent 2/3 và JSON cuối worker lưu S3. AWS/model vẫn là Moto/fixture.

`tests/integration/handoff_audit.py` chỉ đọc records của job kiểm thử vừa tạo từ
SQLite volume; không bổ sung debug API hay log raw contract. Cách chạy bằng PowerShell
tại root với Compose đang healthy và `.env` local fixture:

```powershell
docker build --target test -t contract-risk/test:local -f infrastructure/docker/Dockerfile .
$handoffMounts = @()
foreach ($service in @('input-processing', 'langgraph', 'agent-1', 'agent-2', 'agent-3')) {
    $handoffMounts += '--mount'
    $handoffMounts += "type=volume,source=contract-risk_${service}-data,target=/audit/${service},readonly"
}
foreach ($kind in @('txt', 'png')) {
    docker run --rm --pull never --network contract-risk_backend --env-file .env `
        -e SMOKE_AUDIT_DIR=/audit -e "SMOKE_INPUT_KIND=$kind" `
        @handoffMounts contract-risk/test:local
    if ($LASTEXITCODE -ne 0) { throw "Handoff verification failed: $kind" }
}
```

Nếu đổi COMPOSE_PROJECT_NAME, đổi network và tiền tố volume tương ứng; các volume
phải thuộc Compose hiện có. Chạy smoke không có SMOKE_AUDIT_DIR vẫn kiểm tra luồng
chính, nhưng không đọc/đối chiếu nội dung tại từng điểm bàn giao.

Evidence local: `.cache/handoff-text.log`, `.cache/handoff-ocr.log` và các file exit
code cùng tên. Xem mục 10 của [báo cáo luồng](current_connections_report.md) để biết
đường truyền chính xác và giới hạn của lần kiểm tra.
