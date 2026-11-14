# Báo cáo thực hiện plan_work.txt

Ngày nghiệm thu: 2026-11-07 (Asia/Saigon). Đã triển khai lần lượt mục 1–10 và nghiệm
thu tích hợp local. Không thay prompt/luật nghiệp vụ AI hoặc source Hermes upstream.
Không provision AWS hoặc triển khai lên EC2 trong lần làm này.

## Kết quả theo yêu cầu

| Mục | Kết quả |
| --- | --- |
| 1. Dọn Docker cũ | Rà soát trước khi sửa, xóa đúng 11 file cấu hình/khung cũ; giữ worker/AWS/Hermes và dữ liệu Docker |
| 2. Docker Infrastructure | Một docker-compose.yml root, tám target/image riêng, DNS/network, volumes, health/readiness, dependency, logging/restart; UID 10001, filesystem read-only |
| 3. Environment | Root .env host-only/gitignored, mẫu .env.example, init_env không ghi đè; biến được inject theo service, không COPY .env |
| 4. Config | Secrets Manager/SSM qua AWS credential chain; token riêng từng principal, allowlist alias, timeout/retry/cache, bootstrap secret runtime |
| 5. Input Processing | HTTP job bền vững nhận bytes, gọi pipeline hiện có, trả ContractInput/metadata/provenance cho Agent 1 |
| 6. Ba agent/Hermes | Image riêng với Hermes có sẵn; API AgentRequest/AgentResult/memory snapshot, revision check, Bedrock settings/readiness; fixture chỉ local |
| 7. Monitoring | Telemetry HTTP + stdout JSON, collector/metrics/outbox SQLite, correlation/latency/usage, lọc raw content; Langfuse OTLP tùy chọn |
| 8. LangGraph | Workflow/node/handoff/cache hiện có qua RemoteAgent, durable job API và volume; worker không điều phối từng agent |
| 9. Pipeline | Worker → processing → LangGraph → Agent 1/2/3 → worker; giữ AWS lease/runtime/hash và S3-before-completed-before-ACK/DeleteMessage |
| 10. Bàn giao | Unit/notebook/E2E, build và chạy Compose, hướng dẫn local/EC2/debug/rollback, báo cáo này và nhật ký để tiếp tục |

## Các file thay đổi trong lần thực hiện kế hoạch

Worktree đã có nhiều thay đổi staged/unstaged trước khi bắt đầu. Danh sách dưới đây
mô tả phần của kế hoạch này; không gán các thay đổi trước đó thành công việc mới.
Không reset, stage hoặc commit tự động.

| Hành động | File/phạm vi |
| --- | --- |
| Xóa 6 cấu hình worker cũ | workers/Dockerfile, workers/compose.yaml, workers/.dockerignore, workers/.env.example, workers/requirements.in, workers/requirements.txt |
| Xóa 2 Compose cũ | compose.yaml, compose.ec2.yaml |
| Xóa 3 Dockerfile khung | infrastructure/docker/Dockerfile.agent_1_classification, Dockerfile.agent_2_risk_scoring, Dockerfile.agent_3_recommendation |
| Tạo cấu hình tập trung | docker-compose.yml, infrastructure/docker/Dockerfile, infrastructure/requirements/{base,processing,agents,test}.{in,txt}, scripts/init_env.py, root .env local |
| Cập nhật packaging | .dockerignore, .env.example, pyproject.toml; thêm .gitattributes cho shell LF |
| Tạo transport chung | common/service_http.py, service_client.py, service_health.py, service_jobs.py |
| Tạo config broker | config/__init__.py, client.py, secrets.py, service.py, policy.json |
| Tạo service entrypoints | input_processing/service.py, agents/service.py, orchestration/service.py |
| Nối remote workflow | orchestration/remote_agent.py; thêm execution_spec cho remote cache trong orchestration/pipeline.py |
| Nối worker mới | workers/connectors/processing_service.py, workflow_service.py; cập nhật workers/settings.py, main.py, runtime/intake_worker.py |
| Tạo monitoring mới | monitoring/client.py, service.py, service_observer.py, langfuse_exporter.py |
| Kiểm thử | tests/unit/test_service_architecture.py, test_component_boundaries.py, test_workers.py; tests/integration/container_smoke.py, test_container_services.ipynb |
| Script EC2 | infrastructure/ec2/setup.sh, deploy.sh, rollback.sh |
| Tài liệu | README.md, AGENTS.md, workers/README.md; docs/architecture.md, container_interfaces.md, environment.md, deployment_ec2.md, workers.md, input_processing.md, agent_workflow.md, testing.md, plan_work_report.md; plan_work_note.md |

workers/.env không tồn tại lúc bắt đầu. Source AWS connector/runtime cũ được giữ;
reader Langfuse/multipart còn để tương thích library/test, không được main Compose
sử dụng. Không tạo lại apps/database root, Redis hoặc Lambda. File tham chiếu
`docs/folder-responsibilities.md` được giữ nguyên.

## Image đã build

Các image runtime dùng Python 3.12 base pin digest, dependency khóa phiên bản có
platform markers; Linux/amd64. Kích thước dưới đây là tổng layer logic của từng image,
không cộng trực tiếp thành dung lượng đĩa vì các image chia sẻ layer.

| Image | Image ID rút gọn | MiB |
| --- | --- | ---: |
| contract-risk/worker:local | 73b3dcde800d | 251 |
| contract-risk/input-processing:local | 8cd2aff21b16 | 459 |
| contract-risk/agent-1:local | af6cdd899a04 | 687 |
| contract-risk/agent-2:local | 4210987d7893 | 687 |
| contract-risk/agent-3:local | ba74f9b02ae5 | 687 |
| contract-risk/langgraph:local | e92a6d16e69f | 687 |
| contract-risk/monitoring:local | 3f38add2070c | 251 |
| contract-risk/config:local | 5d38c7cce241 | 251 |

Có thêm image `contract-risk/test:local` cho nghiệm thu; không nằm trong tám service
vận hành. Manifest đầy đủ lưu local tại `.cache/image-manifest.json`.

## Kiểm chứng

- **106/106 unit test đạt** trên Python 3.12.16.
- **4/4 notebook đạt**, source giữ output trống.
- **8/8 image build được, 8/8 service healthy** qua Compose.
- E2E chạy HTTP xuyên container với LangGraph/Hermes thật, worker hai process thật,
  AWS Moto và model fixture. Xác minh S3/DynamoDB/ACK, runtime tự tạo, duplicate,
  telemetry correlation/usage và SIGTERM exit 0.
- E2E vẫn đạt khi scale Agent 2 lên hai replica; đã trả về một replica.
- OCR Tesseract eng/vie hoạt động trong processing image với ảnh kiểm thử; cache và
  kết quả workflow còn sau recreate; unit test xác minh lease recovery/fencing.
- UID 10001, không đóng gói .env/token runtime trong các vị trí đã kiểm tra, dependency
  `pip check` đạt; script triển khai kiểm tra bằng shell Linux.

Chi tiết phạm vi, cách chạy và giới hạn: `docs/testing.md`. Log E2E cuối:
`.cache/final-e2e.log`, exit code `.cache/final-e2e-exit.txt` = 0.

## Trạng thái bàn giao và việc tiếp theo

Tám service local đang chạy healthy; worker idle (`WORKER_ENABLED=false`), agent dùng
fixture (`APP_ENV=local`, `MODEL_PROVIDER=fixture`), Langfuse tắt. Không có public port.
.env đã tạo token ngẫu nhiên và được gitignore; không đưa nội dung file vào báo cáo.
Volume chứa kết quả kiểm thử được giữ lại; không xóa tài nguyên Docker có sẵn.

Để vận hành thật, cần cấu hình IAM/region/resources AWS, Bedrock model, instructions
và memory budgets còn TODO của project; sau đó nghiệm thu trên EC2 với job thật.
Langfuse có thể giữ tắt hoặc cấu hình key/endpoint sau. Không sử dụng fixture để xác
nhận chất lượng phân loại/chấm điểm/khuyến nghị. Không mở rộng schema/luật nghiệp vụ
ngoài phạm vi yêu cầu tái cấu trúc này.

SQLite/scale hiện dành cho một host; chưa có kiểm thử multi-host, load dài hạn hoặc
mọi tình huống crash. Không đảm bảo exactly-once model call. Secret rotation cần
restart client; client telemetry fallback stdout khi collector gián đoạn. Các giới
hạn này đã ghi trong tài liệu interface/deployment/testing.

Điểm tiếp tục được duy trì tại `plan_work_note.md`. Không còn hạng mục code/build/test
local nào của mười mục đang dở; bước tiếp theo là cấu hình và xác minh môi trường thật.
