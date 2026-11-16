# Hướng dẫn triển khai Contract Risk Management AI

## Tài liệu tham chiếu

- Đọc `docs/folder-responsibilities.md` trước khi triển khai hoặc thay đổi kiến trúc. Đây là bản mô tả chức năng folder/file do người dùng cung cấp, được lưu nguyên văn.
- Dùng tài liệu đó làm định hướng trách nhiệm, input/output và chiều phụ thuộc của module. Đây là thiết kế mục tiêu, không phải xác nhận các chức năng đã được triển khai.
- Từ 2026-11-06, backend API và database ở root đã bỏ; worker SQS hai tiến trình được triển khai theo yêu cầu mới nhất. Đọc `docs/architecture.md` để đối chiếu thay đổi; giữ nguyên bản tham chiếu gốc.
- Yêu cầu mới của người dùng được ưu tiên. Kiểm tra code thực tế trước khi kết luận một chức năng đã có; khi thay đổi phân công module, cập nhật tài liệu liên quan.

## Trách nhiệm các folder

| Folder | Phạm vi |
| --- | --- |
| `agents/hermes/hermes_agent/` | Source Hermes gốc (chuyển từ third_party/ ngày 2026-11-07). Ưu tiên adapter/extension, tránh sửa trực tiếp source upstream. |
| `input_processing/` | Nhận bytes hợp đồng đã tải từ worker hoặc text; kiểm tra file, PDF/DOCX/OCR, làm sạch, bảo toàn cấu trúc và vị trí nguồn, chia chunk, đếm token. |
| `agents/` | Logic AI: Agent 1 phân loại/trích xuất; Agent 2 đánh giá và chấm điểm rủi ro có bằng chứng; Agent 3 đề xuất giảm thiểu/chỉnh sửa điều khoản. |
| `agents/hermes/` | Adapter runtime, model provider, cấu hình, tools và memory giữa ứng dụng với Hermes/Bedrock. Kiểm tra giao diện phiên bản Hermes thực tế trước khi nối runtime. |
| `orchestration/` | Workflow, state, chuyển context, gọi agent, retry nghiệp vụ, tổng hợp kết quả và checkpoint khi triển khai. |
| `workers/` | SQS/S3 intake, HTTP processing → LangGraph, runtime hai tiến trình, DynamoDB/S3-before-ACK. Docker/env/dependencies quản lý tập trung tại root/infrastructure theo plan_work.txt. |
| `data_pipeline/` | Thu thập dữ liệu tham chiếu luật/thị trường/doanh nghiệp; chuẩn hóa, kiểm chứng, loại trùng, quản lý phiên bản, định tuyến và ghi PostgreSQL/Qdrant/S3. |
| `mcp_server/` | MCP gateway (image `mcp`) cho Agent 2/3: nhận tool call, mở connector theo tool được gọi; connector nằm ở `integrations/`. Xem `docs/mcp.md`. |
| `rag/` | Logic truy xuất, lọc metadata, hybrid search, rerank và định dạng bằng chứng cho Agent 2/3. Qdrant là kho vector. |
| `integrations/` | Client của agent/dữ liệu tham chiếu: Bedrock/database/API/search/MCP; client SQS/S3/DynamoDB riêng của worker ở workers/connectors. Không chứa nghiệp vụ phân loại/chấm điểm. |
| `common/` | Schema/settings/ports; HTTP auth/client, bounded JSON, durable job queue/lease dùng chung giữa service. |
| `config/` | Config Service có token/allowlist riêng từng service, Secrets Manager/SSM và runtime secret bootstrap. |
| `infrastructure/` | Một Dockerfile nhiều target cho 8 image, dependency locks, script EC2. Compose duy nhất docker-compose.yml ở root. |
| `monitoring/` | HTTP collector, stdout JSON, SQLite/outbox, metrics, observer lọc nội dung và exporter Langfuse OTLP tùy chọn; liên kết job_id/runtime_id/trace_id/request_id. |
| `tests/` | Unit, integration, end-to-end, tải và đánh giá chất lượng agent, gồm tính nhất quán và truy xuất bằng chứng. |
| `configs/` | Cấu hình ứng dụng, AWS, model, agent, database, logging; không lưu secret. Nội suy biến môi trường cần được triển khai rõ ràng. |
| `scripts/` | Công cụ setup, khởi tạo dữ liệu, ingestion thủ công và chạy local. |
| `docs/` | Tài liệu kiến trúc, giao diện container, kết nối AWS, triển khai và kiểm thử. |

## Ranh giới và luồng thiết kế

- Luồng hiện tại theo plan_work.txt: worker → input-processing HTTP → ContractInput JSON → worker → LangGraph HTTP → agent-1/2/3 HTTP → worker → S3/DynamoDB/ACK. Không import để gọi chéo container. Langfuse chỉ telemetry tùy chọn, không phải kênh hoàn tất job. Các mục ngày trước bên dưới là lịch sử.
- Luồng kiến thức riêng: sources → processing → router → ingestion → PostgreSQL/Qdrant/S3. Không gộp xử lý hợp đồng người dùng vào `data_pipeline/`.
- Agent gọi tool nghiệp vụ; tool tìm kiếm dùng `rag/`; client dịch vụ nằm ở `integrations/`. Logic workflow thuộc `orchestration/`, không đặt trong client AWS.
- Ba agent chạy container riêng phải giao tiếp qua service hoặc queue; import object Python chỉ phù hợp khi chạy cùng tiến trình.
- Bảo toàn nguồn trích dẫn, vị trí điều khoản và metadata của bằng chứng. Dữ liệu tham chiếu cần provenance, thời điểm thu thập, ngày hiệu lực và phiên bản.
- Prompt chỉ có tác dụng khi code chủ động nạp (`load_prompts`); không giả định Hermes tự đọc. Cấu trúc hiện tại: `docs/agent_core.md` (mục cuối file này).

## Đối chiếu tài liệu với repo tại thời điểm ghi nhớ (2026-11-03)

- Repo có 18 folder cấp cao nhất ngoài `.git`, gồm `common/` và `docs/`; con số 16/17 trong bản mô tả không phải số lượng chuẩn để kiểm tra repo.
- Dockerfile ba agent nằm tại `infrastructure/docker/`; Compose nằm ở root (`compose.yaml`, `compose.ec2.yaml`). Dockerfile API đã bỏ; Dockerfile.contract_worker được triển khai lại cho yêu cầu worker mới. Compose root chạy worker; compose.ec2.yaml còn là khung.
- `infrastructure/aws/`, `infrastructure/ecs/`, `infrastructure/ecr/`, `data_pipeline/scheduler/`, `orchestration/checkpoint.py` và `monitoring/alerts.py` là các phần dự kiến/chưa có. ECS/Fargate là tùy chọn khi thay đổi nền tảng triển khai, không phải yêu cầu đã chốt.
- LangGraph đã được triển khai trong `orchestration/workflow.py`; xem `docs/agent_workflow.md`. Agent 1 xử lý các chunk tuần tự, cache đầy đủ trước khi tổng hợp và chuyển context sang Agent 2; Agent 2 chuyển output vào context Agent 3.
- Base code hiện có: pipeline cùng tiến trình gọi ba agent, cấu hình riêng từng agent, nạp các Markdown bắt buộc, ghép context, model provider và bộ chuyển đổi Bedrock từ Hermes. Tool có giao diện/callback đăng ký tường minh; memory lưu theo instance của job. Chưa có vòng lặp runtime tự gọi tool, retrieval hoặc schema nghiệp vụ. Prompt, API/model config và chính sách riêng vẫn để TODO theo yêu cầu người dùng.
- Memory workflow dùng extension ContextEngine của Hermes, riêng từng agent/job, snapshot lưu cùng result trong SQLite. Không dùng memory cá nhân toàn cục của Hermes. Giữ nguyên raw results khi nén context; chưa được tự viết prompt hay luật nghiệp vụ thay người dùng.
- Người dùng đã yêu cầu viết plan cho Agent 1: đọc `agents/agent_1_classification/plan.md`. Vòng đời mục tiêu là context → xử lý theo prompt → output/cache → tổng hợp đủ chunk → giải phóng memory Agent 1, giữ kết quả để Agent 2 sử dụng. Plan đã viết; cleanup snapshot/checkpoint chưa được triển khai bằng code, không được báo là đã tự động hoạt động.
- Plan Agent 2/3 đã viết trong `agents/agent_2_risk_scoring/plan.md` và `agents/agent_3_recommendation/plan.md`, cùng cấu trúc vòng đời Agent 1. Ưu tiên truy xuất database qua MCP; chỉ chuyển sang tool search web qua MCP khi bằng chứng chưa đủ. Giữ nguồn, điểm thiếu, cache kết quả/bằng chứng và memory theo hợp đồng; dọn memory sau khi kết quả đã lưu và bàn giao được. Đây là quy trình mục tiêu: MCP client, vòng lặp tool, kiểm tra bằng chứng/schema và cleanup chưa được triển khai; binding/endpoint/credential và luật nghiệp vụ vẫn để TODO.
- Source Hermes yêu cầu Python >=3.11,<3.15; workspace kiểm thử bằng `.venv312` (Python 3.12). Unit test dùng `unittest` trong `tests/unit/`; các notebook vẫn trống. Xem `docs/testing.md`.

## Cập nhật tiền xử lý (2026-11-04)

- `input_processing/` đã xử lý text hoặc file local/S3: TXT/PDF/DOCX/ảnh, OCR, validation/limits, normalize có nguồn, cấu trúc và chunking; xem `docs/input_processing.md`. Tesseract và gói ngôn ngữ là dependency hệ điều hành; chưa có trên workspace kiểm thử.
- `ContractRiskPipeline.run_preprocessing_job` nối kết quả sang `ContractInput` rồi workflow hiện có. Metadata nguồn được giữ qua Agent 1/handoff; không sửa prompt/luật nghiệp vụ.
- Token mặc định là UTF-8 byte estimate; counter cho text khác chữ ký counter messages của workflow. Không tự tải tokenizer hoặc cho rằng estimate là usage model.
- Source offsets là ký tự trong text đã extract/cleaned text, mapping cấp dòng; PDF/ảnh biết trang vật lý, DOCX dùng element ID và page=null. Không tuyên bố là tọa độ chính xác trong PDF.
- `build_chunk_request` nối manifest vào context Agent 1; `workflow.source_metadata` giữ nguồn từng chunk, `preprocessing` giữ thống kê/warnings/file của toàn tài liệu. Hai key này được dành riêng.
- 76/76 unit test đạt trên Python 3.12.16; notebook unit tiền xử lý và `tests/integration/test_file_to_agent1_context.ipynb` có nội dung. Model/S3/OCR engine giả và tracing tắt; PDF/DOCX/render dùng thư viện thật. Không suy ra OCR/AWS thật đã được xác minh.

## Thu gọn cấu trúc (2026-11-06)

- Đã bỏ `apps/` (folder app người dùng nhắc tới), `database/` ở root và các placeholder SQS/Redis/Lambda. Các worker SQS, port job queue, Dockerfile API/worker và notebook API/worker rỗng cũng đã bỏ.
- Yêu cầu worker mới bên dưới thay thế phần xóa worker/SQS; không tự tạo lại API, database root, Redis/Lambda. Giữ `integrations/database/` cho PostgreSQL/Qdrant, luồng dữ liệu tham chiếu, S3/Bedrock và cache SQLite của orchestration.

## Worker hai tiến trình (yêu cầu tiếp theo ngày 2026-11-06)

- `workers/main.py` tạo intake/progress bằng spawn và supervisor. Intake nhận SQS, đăng ký runtime/lease DynamoDB, tải S3, xử lý file, gọi graph hiện có. Progress ghi đè stage/status vào DynamoDB, lưu output Agent 3 lên S3 trước completed/ACK. Chỉ xóa SQS sau ACK cuối.
- `input_processing/file_retrieval.py` đã bỏ. Loader nằm ở `workers/contract_source.py`; `process_job` và `run_preprocessing_job` nhận `file=RetrievedFile`, không còn `retriever=`/`source` bên trong preprocessing. CLI vẫn nhận source và tự tải trước.
- `monitoring/worker_observer.py` chuyển observation trực tiếp qua IPC tới progress worker, không phụ thuộc telemetry cloud. Không gửi raw contract/output vào DynamoDB progress tự động.
- Runtime UUID4 khi thiếu, lưu theo job_id và dùng lại khi SQS redeliver. Lease/sequence ngăn owner/event cũ ghi đè. Cache SQLite lưu trên volume để retry, không đảm bảo exactly-once model call giữa các host.
- Dockerfile worker và compose root có cấu hình chạy; chưa build/run Docker hoặc gọi AWS thật. Không provision SQS/S3/DynamoDB/IAM. Các TODO prompt/model/memory cũ giữ nguyên.
- Test mới dùng Moto, model giả, LangGraph thật; có spawn progress process và notebook worker. Xem `docs/workers.md` và `docs/testing.md` cho giới hạn kiểm chứng.

## Gỡ telemetry khi chuẩn bị EC2 (2026-11-06)

- Runtime không còn SDK/exporter telemetry, cấu hình credential hay smoke script gửi trace ra ngoài. Không tự khôi phục từ tài liệu lịch sử.
- Giao diện `monitoring/observer.py` chỉ là callback nội bộ; worker dùng `WorkerObserver(events)` để giữ tiến độ DynamoDB/S3 và ACK SQS. Không thêm dependency cloud để theo dõi stage.
- Bản `docs/folder-responsibilities.md` và source Hermes gốc giữ nguyên để tham chiếu; plugin catalog trong upstream không đồng nghĩa plugin được cài/khởi động trong project.

## Ngắt kết nối để chuẩn bị container (2026-11-07)

- Mục tiến độ các ngày trước là lịch sử. Hiện worker không gọi/import input_processing, agents, orchestration; preprocessing không gọi agent/worker, orchestration không gọi preprocessing. File DTO thụ động ở common/schemas/files.py.
- Đã bỏ to_contract_input và ContractRiskPipeline.run_preprocessing_job. Giữ run_contract/run, node/cạnh LangGraph, context/handoff/memory/cache giữa ba agent.
- IntakeWorker.handle chỉ tải file, ghi waiting/awaiting_dispatch, trả bytes trong RAM; không completed/DeleteMessage cho job mới. serve báo IntegrationNotConfigured trước polling. Compose restart=no; chưa có transport liên container hay bàn giao bền vững.
- ProgressWorker giữ xử lý event/output tường minh, S3 trước DynamoDB completed/ACK. WorkerObserver chưa tự gắn vào graph; không khẳng định có callback agent → worker đang chạy.
- CLI preprocessing chỉ local/text; không nối worker/S3. Notebook file-processing đã đổi tên; các test component chạy độc lập, test LangGraph vẫn kiểm tra đầy đủ ba agent.
- Sau khi tách kết nối: 85/85 test và ba notebook đạt trên Python 3.12.16; dependencies tương thích, không còn SDK/exporter tracing trong .venv312. Không suy ra container/AWS thật đã được kiểm chứng.

## Lịch sử: gom worker thành một image và đọc Langfuse API (2026-11-07)

- Mục này thay thế mô tả worker tải rồi dừng/polling tắt ở mục lịch sử trên. Worker đã có HTTP client gọi service preprocessing và đọc Langfuse API theo trace_id như lựa chọn của người dùng; không khôi phục Python import giữa các thành phần.
- Toàn bộ source/dependency/entrypoint/Dockerfile/Compose worker nằm trong workers/. Root Compose chỉ include workers/compose.yaml. Client SQS/S3/DynamoDB đã chuyển từ integrations/aws sang workers/connectors; runtime chuyển sang workers/runtime.
- Image chỉ copy workers/, không phụ thuộc common/integrations/monitoring/input_processing/agents/Hermes. DTO bytes riêng của worker không phải module preprocessing được import qua container.
- Intake polling sau khi validate đủ cấu hình: nhận SQS, giữ runtime/lease DynamoDB, tải S3, bind hash/trace, HTTP multipart với Idempotency-Key ổn định rồi đợi ACK cuối. Progress đọc observations v2 theo trace_id; chỉ worker_event có identity khớp được cập nhật progress. Agent 3 output phải lưu S3 trước completed DynamoDB/ACK/DeleteMessage.
- HTTP server bọc input_processing, bàn giao bền vững/idempotency bên nhận và producer phát worker_event vào Langfuse chưa triển khai trong repo. Không tự nối preprocessing → Agent 1; không sửa graph/handoff/model/prompt giữa ba agent. Chỉ có API key/trace generation thông thường chưa đủ hoàn tất job.
- Không cài SDK/exporter Langfuse. Worker chỉ gửi GET qua httpx với project API keys; cấu hình riêng ở workers/.env.example, không đọc root .env. Xem workers/README.md cho giao thức và cách build/run.
- Image contract-risk-worker:local đã build ở workspace. Xem docs/testing.md cho kết quả unit/notebook/container và các giới hạn; chưa provision hoặc deploy AWS/EC2, chưa kiểm chứng endpoint thật.
- Kết quả sau đóng gói: 94/94 unit test, 3/3 notebook đạt. Container Linux/amd64 chạy UID 10001, hai tiến trình spawn và stop SIGTERM exit 0; Compose root/folder đều hợp lệ. Không có kết nối cloud thật trong smoke test.

## Thực hiện plan_work.txt: multi-container (2026-11-07, cập nhật mới nhất)

- Yêu cầu này thay thế thiết kế Docker worker độc lập và trạng thái ngắt kết nối trước đó. Đọc plan_work_note.md để tiếp tục theo checkpoint thực tế, docs/container_interfaces.md cho giao thức.
- Một docker-compose.yml ở root quản lý 8 service/image: worker, input-processing, agent-1/2/3, langgraph, monitoring, config. Dockerfile target và dependency locks nằm trong infrastructure/. Không có Compose/env/requirements riêng ở workers/ nữa.
- Root .env ignored, host-only; scripts/init_env.py tạo token ngẫu nhiên và local fixture mode nếu file chưa tồn tại. Compose inject đúng biến service; Config broker có token riêng từng principal và policy allowlist. Không log/bake secrets; provider AWS dùng IAM/credential chain.
- common/service_jobs.py: HTTP jobs có auth, idempotency, SQLite volume, lease/heartbeat, progress/result và phục hồi claim sau crash. Single-host storage, không cam kết exactly-once model call hoặc checkpoint đa host.
- input_processing/service.py trả ContractInput giữ provenance và context.preprocessing; nghiệp vụ preprocessing không đổi. Worker gửi manifest sang graph; không gọi agent trực tiếp.
- agents/service.py đóng gói BaseAgent/Hermes, RPC trả AgentResult + memory snapshot, kiểm tra revision. Graph dùng RemoteAgent/RemoteSummarizer; node/cạnh/handoff/memory/cache cũ giữ nguyên. HTTP JSON không thay thế schema nghiệp vụ còn TODO.
- MODEL_PROVIDER=fixture chỉ hợp lệ khi APP_ENV=local; instructions fixture nằm /tmp, không sửa prompt/plan nguồn. Production cần Bedrock model/region, instructions và memory budgets hiện còn TODO. Không tự viết luật chấm điểm.
- Monitoring nhận telemetry HTTP + stdout JSON, lọc nội dung/secret, lưu SQLite/outbox. Export OTLP Langfuse khi enabled và đủ key; thiếu key vẫn chạy nội bộ. Không triển khai Langfuse Server; worker không cần Langfuse để hoàn tất job.
- Worker vẫn giữ hai process và AWS lease/hash/order/S3-before-completed-before-ACK/DeleteMessage. WORKER_ENABLED=false là idle rõ ràng; entrypoint chạy mới luôn dùng processing + LangGraph. Reader/multipart legacy chỉ còn cho caller library/test cũ.
- Không thay Hermes upstream, không tạo apps/database root/Redis/Lambda hoặc tài nguyên AWS thật. Xem docs/testing.md và báo cáo plan_work để phân biệt nghiệm thu local với production.
- Nghiệm thu cuối: 106/106 unit test, 4/4 notebook, 8/8 image build và service healthy; E2E AWS Moto/model fixture đạt cả khi Agent 2 scale=2. OCR Tesseract trong image, volume persistence, UID/dependencies và shell EC2 đã kiểm tra. Đã trả Agent 2 về một replica, worker local idle. Báo cáo tại docs/plan_work_report.md; lần sau đọc checkpoint đầu plan_work_note.md, không làm lại các mục local đã hoàn tất.

## Nối Langfuse cloud vào monitoring (2026-11-07, mới nhất)

- Đọc `docs/langfuse_monitoring.md` trước khi làm tiếp monitoring/Langfuse. Root .env đã bật LANGFUSE_ENABLED với key project (EU cloud); không ghi key vào file tracked.
- Monitoring export OTLP đã xác minh với Langfuse thật: 222 event lên cloud, trace đọc lại qua API v2. Org mới không dùng được API legacy /api/public/traces.
- Observation name là `service.stage`, có `langfuse.environment`=APP_ENV và model name. Không cài SDK, không gửi nội dung hợp đồng.
- Monitoring → worker forwarding (MONITORING_WORKER_ENABLED=true) đã bật; tiến độ DynamoDB của worker vẫn lấy chính từ polling LangGraph.
- Skill chính thức Langfuse cài tại `.claude/skills/langfuse/`. 113/113 unit test đạt.

## Luồng Agent 1 theo chunk (2026-11-07, mới nhất)

- Đọc `docs/agent_1_flow.md` trước khi sửa Agent 1/LangGraph. Mỗi row cache chunk = {scope, result, memory}; memory chỉ khôi phục từ chunk N−1 cùng contract/version, sai scope thì dừng trước khi gọi model.
- Aggregate kiểm đủ chunk + scope, lưu và đọc lại bản tổng hợp, rồi `ContractCache.release_memory` xóa memory Agent 1 (giữ output) trước khi chuyển context sang Agent 2.
- 115/115 unit test; E2E TXT 3 chunk có audit exit 0. Chưa dọn checkpoint toàn job hay memory Agent 2/3.

## MCP tool gateway cho Agent 2/3 (2026-11-07, mới nhất)

- Đọc `docs/mcp.md` trước khi sửa tool bên ngoài. Service thứ 9 `mcp` (image riêng, `mcp_server/`) dùng SDK `mcp` 1.30, streamable HTTP, token riêng `MCP_API_TOKEN` chỉ cấp cho agent-2/3.
- 4 tool: `web_search` chạy thật (nay là Tavily, xem mục cuối), `qdrant_search`/`postgres_query`/`s3_get_object` là base code chờ `QDRANT_URL`/`POSTGRES_DSN`/`MCP_S3_BUCKETS`; connector mở lazy khi agent gọi.
- Agent 2/3 bind handler MCP vào ToolRegistry khi có `MCP_URL`; chưa có vòng lặp model tự gọi tool. Mục "MCP client chưa triển khai" ở trên là lịch sử.
- 123/123 unit test, 9/9 service healthy, agent-2/3 gọi web search thật qua MCP, E2E TXT exit 0.

## Hermes nằm trong agents/ (2026-11-07, mới nhất)

- Đã bỏ `third_party/` và `hermes_integration/` ở root. Source Hermes upstream đầy đủ (22.016 file) ở `agents/hermes/hermes_agent/`; code tích hợp ở `agents/hermes/*.py`, import `agents.hermes.*`. Các mục lịch sử nhắc `third_party/hermes-agent` hoặc `hermes_integration` nay hiểu là hai vị trí này.
- `agents/hermes/runtime.py` thêm `agents/hermes/hermes_agent` vào sys.path để nạp `agent.context_engine`/`agent.bedrock_adapter`; vẫn ưu tiên adapter, tránh sửa source upstream.
- Image agent-1/2/3 và langgraph chỉ COPY `agents/` (đã gồm Hermes). pyproject loại `agents.hermes.hermes_agent*` khỏi package.
- 123/123 unit test, 9/9 service healthy, E2E TXT exit 0. Không còn đường dẫn third_party trong code/Dockerfile.

## Hoàn thiện orchestration + kiểm tra EC2 (2026-11-07, mới nhất)

- `orchestration/retry_policy.py`: retry node agent (3 lần, backoff 5→60s) chỉ cho lỗi tạm thời; Bedrock throttling/outage → `TransientModelError` (integrations/aws/bedrock_client.py), JobStore cho submit lại theo `RETRYABLE_JOB_ERRORS`. Lỗi validate/cấu hình/output không retry.
- `orchestration/checkpoint.py`: resume từ chunk đầu chưa có trong cache (Agent 1 đủ → vào thẳng aggregate/Agent 2); xóa thread InMemorySaver sau mỗi lần chạy; xóa memory Agent 2/3 sau khi lưu `complete`. Đã xóa `orchestration/agent_client.py` rỗng (RemoteAgent ở remote_agent.py).
- Chưa làm nhánh "Agent 2 thiếu dữ liệu → RAG/Search → đánh giá lại" (cần vòng lặp tool + luật nghiệp vụ của người dùng) và retention cache.
- EC2: đọc `docs/ec2_readiness.md`. `infrastructure/ec2/setup.sh` kiểm Compose ≥2.24 và .env production; `preflight.sh` gọi STS từ container (IAM role, IMDS hop limit ≥2, egress). 129/129 unit test, E2E TXT + PNG exit 0.

## egg-info (2026-11-07)

- `setup.cfg` ở root chỉ có `[egg_info] egg_base = infrastructure/requirements`: `pip/uv pip install -e .` sinh `contract_risk_management.egg-info` (metadata tự sinh, gitignored) trong `infrastructure/requirements/`, không ở root. Không sửa tay thư mục này; cấu hình thật là `pyproject.toml`. pyproject đã thêm `mcp_server*`.

## Hoàn thiện monitoring (2026-11-07, mới nhất)

- Đọc `docs/monitoring.md` trước khi sửa `monitoring/`. Đã điền `logging.py` (log JSON mọi service), `metrics.py` (`/v1/metrics?window_minutes`: p95/lỗi/token/job lỗi), `tracing.py` (`/v1/traces/{trace_id}`), thêm `alerts.py` (job_failed/model_throttled/slow_stage, `/v1/alerts`, webhook https tùy chọn) và retention `MONITORING_RETENTION_DAYS`.
- `emit()` không còn gửi HTTP đồng bộ (trước đây collector sập làm chậm 2s/event): hàng đợi RAM + thread nền. Langfuse: mốc tiến độ là `event`, lỗi `level=ERROR`.
- Dockerfile sắp lại: dependency trước, code sau (stage `shared`); code thuộc root. Sửa code không cài lại pip.
- SQS backlog dùng CloudWatch alarm, không đo trong collector. 138/138 unit test, 9/9 healthy, E2E TXT + PNG exit 0.

## S3 tham chiếu cho MCP (2026-11-12, mới nhất)

- Bucket `contract-risk-database-unique-333596350642-ap-southeast-1-an` (ap-southeast-1) nối vào MCP qua `mcp_server/s3_reader.py` (trong image mcp): tool `s3_list_objects` + `s3_get_object`, CHỈ ĐỌC (hook botocore chặn mọi thao tác ghi + IAM policy `infrastructure/aws/mcp-s3-readonly-policy.json`). Xem mục S3 trong `docs/mcp.md`.
- Project chạy trên EC2: credential chỉ từ instance role (không đặt key trong .env). `preflight.sh` kiểm tra mcp list được bucket. Local không có credential nên chưa đọc bucket thật. 138/138 unit test, 9/9 healthy.

## Qdrant Cloud cho MCP (2026-11-12, mới nhất)

- `mcp_server/qdrant_reader.py` (image mcp, thư viện `qdrant-client==1.20.0` trong mcp.txt): tool `qdrant_search` + `qdrant_collection_info`, chỉ đọc. Env `.env`: QDRANT_URL/QDRANT_API_KEY/QDRANT_COLLECTION=legal_documents (3072-d cosine, index `contract`). Đã bỏ connector REST cũ ở integrations/database.
- Đã xác minh thật agent-2/3 → MCP → Qdrant Cloud (collection rỗng). Key hiện là quyền manage và đã lộ trong chat: nên thay bằng key read-only. Xem mục Qdrant trong `docs/mcp.md`.

## Supabase cho MCP (2026-11-12, mới nhất)

- `mcp_server/supabase_reader.py` (image mcp, `supabase==2.34.1` trong mcp.txt): tool `supabase_select` + `supabase_list_tables`, CHỈ ĐỌC (hook httpx chặn mọi request khác GET; PostgREST GET = READ ONLY). Env SUPABASE_URL/SECRET_KEY/PUBLISHABLE_KEY/TABLES chỉ cấp cho mcp. Xem mục Supabase trong `docs/mcp.md`.
- Schema public hiện 0 bảng. Secret key bỏ qua RLS và đã lộ trong chat: nên xoay key. 139/139 unit test, 9/9 healthy.

## Data pipeline pháp luật (2026-11-14, mới nhất)

- Đọc mục 11 của `docs/data_pipeline_design.md` trước khi sửa `data_pipeline/`. Đã có code: 2 service systemd trên EC2, venv riêng, không Docker.
- SQS `contract-data-update-queue` (EventBridge Scheduler) CHỈ nối vào collector (`python -m data_pipeline collector`): mỗi message = 1 lượt vào vanban.chinhphu.vn. Không liên quan service Docker `workers/`.
- Luồng HAI NHÁNH (mới nhất): web → collector → S3 (CHỈ file gốc) và web → collector → processor (bytes trong RAM, không qua S3) → Supabase + Qdrant. Không ghi JSON lên S3, không lưu local. Service processor chỉ retry job lỗi (tải lại từ web) + refresh hằng ngày. Cần chạy migration `002_versions_without_s3_json.sql`. Xem mục cuối `docs/data_pipeline_design.md`.
- Embedding KHÔNG dùng API key: `intfloat/multilingual-e5-large` (1024 chiều, fastembed CPU) dùng chung ở `integrations/external/embedding_client.py` cho pipeline và MCP `qdrant_search(query=...)`; collection Qdrant đã tạo lại 1024 chiều. (Thay quyết định OpenAI trước đó.) Mặc định đã chốt: bỏ payload `contract`, migration SQL dán tay, sửa đổi mơ hồ → needs_review, incremental hằng ngày + full hằng tuần.
- Nhận mọi định dạng đính kèm (.doc/.rtf chuyển PDF bằng LibreOffice). Đã chạy thử vào Supabase thật: 10 văn bản (dòng thử có s3_keys local, phải xóa trước khi chạy EC2). Thu thập 2 danh sách: typegroupid=1 (Hiến pháp, ký hiệu HP-<năm>, bản mới nhất thay bản cũ) + typegroupid=3 (Luật/Bộ luật). Mỗi Điều có `legal_references`. 154/154 unit test; liệt kê thật 870/870; 2 PDF scan thật OCR → 66 Điều. Migration 001+002 đã chạy. Chạy thật luồng hai nhánh (2026-11-14): web → collector → processor → Supabase + Qdrant Cloud với 144/2025/QH15 (PDF có text, S3 dùng Moto vì local không có credential) → job done, 4 Điều, 33 vector, 60 quan hệ unresolved (chưa có luật gốc), tìm kiếm trả đúng; sau đó đã xóa sạch (6 bảng 0, Qdrant 0) và xóa model. Chưa chạy thật: S3, OCR trong luồng mới, EC2.

## Web search MCP = Tavily (2026-11-14, mới nhất)

- Đã xóa web search ddgs (`integrations/external/search_client.py`, dependency `ddgs`, env WEB_SEARCH_*). `web_search` của MCP nay dùng Tavily: `mcp_server/tavily_search.py` trong image mcp, `tavily-python==0.8.5`, env `TAVILY_API_KEY`/`TAVILY_SEARCH_DEPTH`/`TAVILY_COUNTRY` chỉ cấp cho mcp; key ở root `.env` (gitignored), không ghi vào file tracked. Xem mục cuối `docs/mcp.md`.
- Đã xác minh thật agent-2/3 → MCP → Tavily; 154/154 unit test, 9/9 healthy. Key đã lộ trong chat: nên xoay key. Chưa cài Tavily CLI/skills toàn máy theo SKILL.md.

## RAG pháp luật (2026-11-14, mới nhất)

- Đọc `docs/rag.md` trước khi sửa `rag/`. RAG chạy trong service mcp (image COPY `rag/`), tool MCP `legal_search`; Agent 2 `legal_search` và Agent 3 `legal_reference` (alias `MCP_ALIASES`) bind tới nó. Agent không import `rag/`.
- Luồng: metadata_filter (mặc định current + in_force; `as_of` theo valid_from/valid_to/replaced_on) → hybrid_search (dense e5 + sparse `keywords` BM25/IDF, RRF, 1 lệnh Qdrant) → gộp part thành nguyên Điều → reranker cross-encoder `jinaai/jina-reranker-v2-base-multilingual` (fastembed, `RAG_RERANK_MODEL`, rỗng = tắt) → evidence_formatter (citation, evidence_id, hiệu lực, nguồn S3/web, warnings).
- `sparse_vector`/`shared()` ở `integrations/external/embedding_client.py` dùng chung cho pipeline và RAG (đổi tokenizer = index lại). Pipeline ghi dense + sparse, `valid_to` khi sửa đổi, `replaced_on` khi bị thay thế; `QdrantWriter.ensure_indexes()` tạo collection + 10 index. Qdrant Cloud `legal_documents` đã tạo lại (rỗng) với sparse vector, giữ strict mode.
- 162/162 unit test (8 test `tests/unit/test_rag.py`). Chạy thật local với model thật + Qdrant Cloud: kết quả đúng (xem `docs/rag.md`), đã xóa dữ liệu thử và model. Chưa kiểm qua MCP từ container agent theo yêu cầu người dùng.

## Model agent = OpenAI, đã gỡ Bedrock (2026-11-14, mới nhất)

- Đã xóa `integrations/aws/` (BedrockClient), `agents/hermes/bedrock_provider.py`, `agents/hermes/model_adapter.py`, `load_bedrock_adapter`, `BedrockSettings`, biến BEDROCK_MODEL_ID/AGENT_n_MODEL_ID/AGENT_n_REGION và AWS_* của agent-1/2/3 trong compose. Mọi mục lịch sử nhắc Bedrock/HermesBedrockProvider nay hiểu là OpenAI. Source Hermes upstream (`agent/bedrock_adapter.py`) giữ nguyên, không còn được nạp.
- Provider: `integrations/external/openai_client.py:OpenAIChatClient` (SDK `openai==2.54.0` trong agents.in), Chat Completions, tin nhắn chat giữ nguyên; `finish_reason` phải là `stop`; RateLimit (trừ `insufficient_quota`)/5xx/kết nối → `TransientModelError`. Settings `common/settings.py:OpenAISettings(model, api_key)`; api_key không vào repr/revision (`public()`).
- Env: `MODEL_PROVIDER=openai|fixture`, `OPENAI_API_KEY` (ĐIỀN SAU, root `.env` hoặc Config binding), `OPENAI_MODEL`, `AGENT_n_MODEL`. `config.yaml` agent: `model` + inference `max_completion_tokens/temperature/top_p/stop`. Usage: `prompt_tokens/completion_tokens` → `monitoring/usage.py:model_usage`.
- 162/162 unit test; image agent-1/2/3 + langgraph build lại, 9/9 healthy (fixture). Chế độ openai chưa có key → readiness `TODO: configure OpenAI OPENAI_MODEL, OPENAI_API_KEY`. Chưa gọi OpenAI thật (chưa có key/model).


## Core agent: memory long/short term + prompt 3 loại (2026-11-14, mới nhất)

- Đọc `docs/agent_core.md` trước khi sửa prompt/memory agent. Prompt mỗi agent: `prompts/core.md` (xử lý trung tâm, bắt buộc), `prompts/external.md` (khi nào dùng service ngoài/context bổ sung, bắt buộc), `prompts/examples/*.md` (tùy chọn). TẤT CẢ đang để trống theo yêu cầu — không tự viết nội dung. Đã xóa system.md/classify|evaluate|recommend.md/soul.md/constraints.md/context/instructions.md; `plan.md` chỉ là tài liệu, không nạp. Các mục cũ nhắc các file đó là lịch sử.
- Memory RIÊNG TỪNG AGENT, không dùng chung (yêu cầu người dùng): prompt memory ở `agents/<agent>/memory/short_term_summary.md` + `long_term_reflection.md` (đã bỏ `agents/shared/prompts/`); long term là file riêng `<LONG_TERM_MEMORY_DIR>/<agent_id>.sqlite3` (`LONG_TERM_MEMORY_DIR=/data/long_term_memory`, volume agent-n-data); short term là snapshot ContextEngine riêng của agent theo hợp đồng.
- Short term = `agents/memory/short_term.py` (xóa sau khi lưu kết quả). Long term = `agents/memory/long_term.py`: sau mỗi job pipeline gọi `reflect` của từng agent (prompt của agent đó, trống → skipped) → bài `pending`; người duyệt bằng `python -m agents.memory.long_term list|approve|reject|add` trong container agent; chỉ `approved` vào prompt (≤30 bài/8000 ký tự). Reflection lỗi không làm hỏng job.
- 170/170 unit test; 9/9 healthy sau build lại agent/langgraph.

## Một file env + AWS Secrets Manager (2026-11-14, mới nhất)

- Đọc mục "AWS credential và secret access" trong `docs/environment.md` trước khi sửa env/secret. `.env.example` (tracked) và `.env` (gitignored) cùng bố cục 9 mục, mỗi key một lần; giữ `.env.example` vì `.env` không commit được. Đã gộp env data pipeline vào root `.env` (bỏ `data_pipeline/.env.example`, `/etc/contract-risk/data-pipeline.env`) và bỏ 9 biến `*_SECRET_BINDINGS`.
- Secret `contract-risk/prod/api-keys` (JSON 11 key: INTERNAL_API_TOKEN, MCP_API_TOKEN, OPENAI_API_KEY, TAVILY_API_KEY, QDRANT_URL, QDRANT_API_KEY, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY), `API_KEYS_SECRET_ID` trong `.env`. `config/policy.json` cấp mỗi service đúng key nó dùng; compose `CONFIG_SECRET_BINDINGS` = danh sách đó (test kiểm khớp). Bootstrap chỉ lấy key RỖNG (giá trị `.env` thắng, không ghi đè); Config tự đọc INTERNAL_API_TOKEN; data pipeline đọc thẳng 4 key Qdrant/Supabase. Không có API_KEYS_SECRET_ID → 404 → chạy bằng `.env`.
- Local `.env` để `API_KEYS_SECRET_ID` rỗng (máy không có credential AWS). IAM: `infrastructure/aws/config-secrets-policy.json` + `data-pipeline-policy.json`. Chưa gọi Secrets Manager thật (Moto). 176/176 unit test; 9/9 healthy; mỗi container chỉ có key của mình.
- (cập nhật cùng ngày) Theo yêu cầu người dùng, 11 key trên ĐÃ XÓA khỏi `.env`, `.env.example` và docker-compose: chỉ lấy qua Config/Secrets Manager. Không ghi lại chúng vào file env. Local `.env` có `API_KEYS_SECRET_ID=contract-risk/prod/api-keys`; máy local hiện không có credential AWS nên stack local không khởi động lại được cho tới khi có credential đọc secret (container đang chạy vẫn giữ env cũ).


## Monitoring data pipeline + log terminal (2026-11-14, mới nhất)

- Đọc mục cuối `docs/monitoring.md`. Pipeline (systemd trên host) gửi sự kiện `data-collector`/`data-processor` tới monitoring qua `127.0.0.1:${MONITORING_HOST_PORT:-8099}` (compose `ports` loopback-only) với `DATA_MONITORING_URL` + INTERNAL_API_TOKEN từ Secrets Manager. Mỗi lượt = 1 trace_id; field mới `document`, `count`; alert `data_run_*`, `data_document_failed`, `data_sqs_retry`.
- Worker KHÔNG đổi: sự kiện pipeline không job-scoped nên không forward sang worker/DynamoDB.
- `monitoring/logging.py`: `LOG_FORMAT=auto|json|text` (terminal → dòng đọc được); lỗi có `error_type`, `error_at`, `stack`; chỉ pipeline (`error_details=True`) in message + traceback. `data_pipeline/telemetry.py:report()` = log + emit (echo=False). `python -m data_pipeline ...` lỗi → log + exit 1.
- 181/181 unit test. Chưa build/restart container monitoring: máy local không có credential AWS nên service không lấy được key sau khi đã xóa key khỏi `.env`.

## Model tải sẵn (2026-11-14, mới nhất)

- `python scripts/download_models.py` tải EMBEDDING_MODEL (e5-large) + RAG_RERANK_MODEL (jina v2) vào `<repo>/models` (gitignored + dockerignored, copy kèm repo lên EC2). mcp mount `./models:/models:ro` (bỏ volume `mcp-models`), pipeline `EMBEDDING_CACHE_DIR=models` (tương đối theo repo); cả hai `HF_HUB_OFFLINE=1` → không tải lúc chạy. `setup.sh` dừng nếu thiếu `models/`. Tesseract vie đã nằm trong image input-processing.


## Worker ↔ ContractGuard (2026-11-15, mới nhất)

- Đọc mục cuối `docs/workers.md`. `WORKER_RESULTS_QUEUE_URL` bật `workers/partner.py` (giao thức đối tác §1–4: ping/pong, request.json, progress/completed/failed lên queue kết quả, clean_file_key, result_key, xóa quarantine, xóa message cuối). Kiến trúc worker KHÔNG đổi; định dạng message cũ vẫn chạy khi biến rỗng. Không tự ghép key S3.
- `.env` mục 6 đã điền queue/bucket của tài khoản 699061129385; IAM chéo tài khoản do người dùng đã cấu hình (role ContractRisk-EC2-Role). DynamoDB jobs/events vẫn ở tài khoản AI.
- Tạm: `extraction.json` bố cục tạm chờ schema `extraction.v2`; error_code ERR-AI-001/002 và step = tên stage chờ danh mục của đối tác. 186/186 unit test; chưa chạy AWS thật.

## Tên mục thống nhất theo schema worker nhận (2026-11-16, mới nhất)

- Mọi thành phần dùng tên mục giống ContractGuard: `version` → **`contract_version`** (ContractInput, Job/chunk của input-processing, LangGraph scope/cache key/kết quả, context Agent 1, QueueJob, CLI `--contract-version`). Message nội bộ cũ của worker dùng `source_file_key` (+ `bucket`, mặc định WORKER_INPUT_BUCKET); đã bỏ `version`, `source`, `s3_bucket`, `s3_key`, `s3_version_id`, không nhận tên cũ. Adapter ContractGuard tạo cùng thân message này.
- Schema tham chiếu: `docs/schemas/worker_sqs_input.schema.json` (`x-field-names` liệt kê tên chuẩn). Cache LangGraph/agent cũ không dùng lại được (fingerprint đổi). 194/194 unit test.

## Kiểm tra đồng nhất schema giữa container (2026-11-16, mới nhất)

- Quét tĩnh: không còn `version`/`s3_key`/`s3_bucket`/`s3_version_id` của hợp đồng trong code. `version` ở `rag/` và `data_pipeline/` là phiên bản văn bản luật (domain khác), `version_id` ở `contract_source.py` là S3 VersionId.
- Đã build lại 9 image và chạy thật giữa các container (stack riêng, Secrets Manager giả, Moto, model fixture): smoke TXT + PNG (message nội bộ) và luồng ContractGuard (ping/pong, request.json → extraction.json + completed, clean copy, xóa quarantine) đều đạt.
- Lỗi tìm thấy và đã sửa: span `<agent>.reflect` (long-term memory sau Agent 3) làm tiến độ báo lùi `agent_3 → agent_1` lên DynamoDB/queue ContractGuard; `monitoring/service_observer.py` bỏ qua span reflect. Smoke test đặt `WORKER_RESULTS_QUEUE_URL=""`. 195/195 unit test.
- Stack chính local vẫn chạy image cũ (không thể recreate khi chưa có credential đọc Secrets Manager). Còn chờ đối tác: extraction.v2, analysis-request.v1, queue-messages.v1, danh mục step/error_code, tên khóa `limits`.

## Output nghiệp vụ vs ContractGuard (2026-11-16, kiểm tra, chưa sửa code)

- Hiện tại: AgentResult chỉ có `agent_id/text/usage/stop_reason` (schema nghiệp vụ TODO); `workflow_service.execute` chỉ lấy Agent 3 nên `extraction.json` (`workers/partner.py:extraction`) bỏ mất output Agent 1/2, lại ghi nhãn `"schema": "extraction.v2"` dù chưa khớp → backend sẽ từ chối cả kết quả (job failed).
- Gói `ContractGuard_4_2-4_5_Package` có 3 schema theo ĐIỀU KHOẢN: `extraction_output` (Agent trích xuất: contract_id, clause_id, category 11 giá trị, facts[]), `risk_verdict` (verify_clause: verdicts R01–R20 có evidence_quote/rationale ≤300), `risk_score` (score_contract: công thức 4.5b tất định từ `risk/risk_policy.json`). Không có schema cho Agent 3 (đề xuất).
- Khác biệt kiến trúc: ContractGuard xử lý theo clause (clause_id + category + clause_text, candidate_hits từ bộ quét luật); project chia chunk theo token. Quote phải là chuỗi con liên tục của clause_text (sau chuẩn hóa) — cần validator code trước khi ghi.
- Chưa có trong tay: `contracts/extraction.v2.schema.json`, `contracts/examples/extraction.v2.example.json`, `docs/06-ai-integration-points.md` §3.1 (khối `scoring`, kiểm trích dẫn). Pattern `^HD-[0-9]{3}$` trong gói là ID bộ mẫu, không suy ra cho job thật.

## Prompt 3 agent + vòng tool + partner_document (2026-11-17, mới nhất)

- Đọc `docs/agent_core.md` (mục Prompt và Vòng lặp gọi tool). Prompt ĐÃ ĐIỀN theo yêu cầu người dùng (thay quyết định "để trống" cũ): `prompts/soul.md` (mới, bắt buộc) + `core.md` + `external.md` cho 3 agent; nguồn từ `ContractGuard_4_2-4_5_Package.zip` (Extraction → Agent 1, Risk → Agent 2, nguyên văn sau phần "Áp dụng trong hệ thống"); Agent 3 viết mới. Schema output người dùng tự thiết kế sau: prompt chỉ ràng buộc "theo schema trong source code"; đặt file `agents/<agent>/schemas/output.schema.json` thì loader tự nạp.
- Agent 1 `examples/` = 10 hợp đồng mẫu rút gọn (chỉ cách tách). Agent 2 `prompts/rules/risk_policy.json` = file luật (đối chiếu sau khi chấm điểm để xác định loại rủi ro); `examples/` = 3 báo cáo từ `risk_rule_report.json` (file đó là kết quả chấm 100 hợp đồng mẫu, không phải danh mục luật).
- `agents/base.py:run`: vòng function calling với tool MCP (spec từ `list_tools`, ≤6 vòng, lỗi tool trả cho model) + đánh giá lại khi output JSON có `evidence_gaps` không rỗng (≤2 lần). Schema của Agent 2/3 phải có khóa gốc `evidence_gaps`. Đây là nhánh "thiếu dữ liệu → RAG/Search → đánh giá lại" trước đây còn thiếu.
- MCP tool thứ 10 `partner_document` (chỉ Agent 3): đọc tài liệu gốc trong bucket ContractGuard (`PARTNER_S3_BUCKET=${WORKER_INPUT_BUCKET}`), chỉ `quarantine/`/`clean/`, chỉ đọc; PDF/DOCX/ảnh trích qua service input-processing. Worker thêm `context.source_document`. IAM đối tác chỉ cho GetObject `quarantine/*`.
- 201/201 unit test; 9/9 image build lại; stack thử: smoke TXT đạt, `partner_document` → input-processing (DOCX) đạt, agent-3 lấy 10 spec tool từ MCP. Chưa gọi OpenAI thật (vòng tool chỉ kiểm bằng client giả), chưa đọc bucket đối tác thật. System prompt A1 ≈ 60 KB, A2 ≈ 55 KB: `configs/memory.yaml` (TODO) phải tính.
