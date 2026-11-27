# Vận hành: env, EC2, monitoring, kiểm thử

## 1. Env và secret

`.env` ở root là file duy nhất cho Docker và data pipeline; bị Git bỏ qua. `.env.example` là mẫu cùng bố cục 9 phần.
`scripts/init_env.py` tạo `.env` mới với token ngẫu nhiên (mặc định local + fixture), không ghi đè file đã có.

- **Secret** nằm trong AWS Secrets Manager `contract-risk/prod/api-keys` (JSON 10 khóa), không đặt trong `.env`. Riêng `GEMINI_API_KEY` ở `.env`.
- Service khởi động → khóa nào rỗng thì hỏi Config bằng `CONFIG_TOKEN_*` riêng → Config kiểm `config/policy.json` → Secrets Manager. Giá
  trị chỉ ở RAM tiến trình. Giá trị có sẵn trong env không bị ghi đè. Đổi secret phải restart service.
- 401 sai token, 403 không được cấp, 404 chưa đặt `API_KEYS_SECRET_ID`, 503 lỗi AWS.
- EC2 dùng IAM instance role, không đặt access key. Chạy local cũng cần credential đọc được secret.

| Service | Khóa được cấp |
| --- | --- |
| config, worker, input-processing, langgraph, agent-1 | INTERNAL_API_TOKEN |
| agent-2, agent-3 | INTERNAL_API_TOKEN, MCP_API_TOKEN |
| monitoring | INTERNAL_API_TOKEN, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY |
| mcp | INTERNAL_API_TOKEN, MCP_API_TOKEN, TAVILY_API_KEY, QDRANT_URL, QDRANT_API_KEY, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY |
| data pipeline | QDRANT_URL, QDRANT_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY, INTERNAL_API_TOKEN |

Các key từng lộ trong chat (Qdrant, Supabase, Tavily, Langfuse) nên được thay; Qdrant cho MCP nên dùng key chỉ đọc.

## 2. Triển khai EC2

EC2 có sẵn Docker và IAM role; image build ngay trên máy, không cần ECR. Gồm 9 service Docker và 2 service systemd. Lệnh chạy từ root
repository; ví dụ dùng Ubuntu, Python 3.12.

### 2.1 Đưa source lên GitHub

Trên máy phát triển, kiểm tra thay đổi trước khi commit:

```sh
git status --short
git diff --check
git diff --stat
git ls-files .env models .venv312 .cache
```

Lệnh cuối phải không liệt kê dữ liệu local. `.env`, `models/`, `.venv312/`, `.cache/`, SQLite và bytecode đã được ignore. Nếu chúng đã bị track từ trước, xử lý khỏi danh sách commit trước khi push. Giữ đủ `agents/hermes/hermes_agent/`, prompt, config, dependency locks và migration SQL.

Sau khi xem lại các thay đổi cần đưa lên:

```sh
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Prepare contract risk services and EC2 deployment docs"
git push origin HEAD
```

Nếu repository chưa gắn GitHub, thêm remote bằng `git remote add origin <GITHUB_REPOSITORY_URL>` trước khi push. Không đưa PAT/API key vào URL hoặc file tracked. Các lệnh commit/push ở đây là hướng dẫn để người triển khai chủ động chạy.

### 2.2 Clone vào EC2

Kiểm tra công cụ:

```sh
docker version
docker compose version
python3.12 --version
git --version
```

Compose phải ≥ 2.24 theo `infrastructure/ec2/setup.sh`. Nếu thiếu Git/Python trên Ubuntu 24.04, cài `git`, `python3.12`, `python3.12-venv` bằng apt. Lệnh Docker bên dưới giả định tài khoản SSH đã có quyền chạy Docker.

Dùng thư mục dưới `/opt` vì systemd data pipeline có `ProtectHome=true`, không đọc checkout trong `/home`:

```sh
sudo install -d -m 755 -o "$(id -un)" -g "$(id -gn)" /opt/contract-risk-management
git clone <GITHUB_REPOSITORY_URL> /opt/contract-risk-management
cd /opt/contract-risk-management
git rev-parse --short HEAD
```

Thay `<GITHUB_REPOSITORY_URL>` bằng URL thật; với repo private dùng SSH key/deploy key hoặc cơ chế xác thực GitHub của máy. Nếu repo đã clone, dùng quy trình cập nhật ở mục 2.9, không clone đè lên dữ liệu đang dùng.

### 2.3 Chuẩn bị `.env` riêng trên EC2

Chỉ chạy khi chưa có `.env`:

```sh
umask 077
python3.12 scripts/init_env.py
chmod 600 .env
nano .env
```

`init_env.py` sinh tám `CONFIG_TOKEN_*` khác nhau và **mặc định local/fixture**. Đổi các giá trị sau trước khi triển khai:

```dotenv
APP_ENV=production
MODEL_PROVIDER=gemini
AWS_REGION=ap-southeast-1
API_KEYS_SECRET_ID=contract-risk/prod/api-keys
WORKER_ENABLED=false
LANGFUSE_ENABLED=true
WORKER_DYNAMODB_TABLE=contract-risk-jobs
WORKER_DYNAMODB_EVENTS_TABLE=contract-risk-job-events
HF_HUB_OFFLINE=1
```

Tạm để worker idle nhằm kiểm tra các service trước khi nhận job; mục 2.7 sẽ bật. Đặt `IMAGE_TAG` theo commit vừa clone, ví dụ `release-<short-commit>`, và giữ `COMPOSE_PROJECT_NAME=contract-risk` ổn định để dùng lại named volumes.

Đối chiếu từng giá trị trong `.env.example` với hạ tầng thực tế:

| Nhóm | Biến cần đúng |
| --- | --- |
| Queue hợp đồng | `WORKER_SQS_QUEUE_URL`, `WORKER_RESULTS_QUEUE_URL` |
| S3 hợp đồng | `WORKER_INPUT_BUCKET`, `WORKER_OUTPUT_BUCKET`, `WORKER_OUTPUT_PREFIX` |
| DynamoDB | `WORKER_DYNAMODB_TABLE`, `WORKER_DYNAMODB_EVENTS_TABLE` |
| Cancel (tùy chọn) | `WORKER_CANCEL_QUEUE_URL` — xem [workers.md](workers.md) |
| MCP/RAG | `MCP_S3_BUCKETS`, `QDRANT_COLLECTION`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `RAG_RERANK_MODEL` |
| Monitoring | `MONITORING_HOST_PORT`, `DATA_MONITORING_URL`, `LANGFUSE_ENABLED`, `LANGFUSE_BASE_URL` |
| Data pipeline | `DATA_SQS_QUEUE_URL`, `DATA_S3_BUCKET`, `DATA_S3_PREFIX`, các biến phần 9 của `.env.example` |

Template đang trỏ tới queue/bucket ContractGuard của đối tác. `WORKER_RESULTS_QUEUE_URL` có giá trị thì worker dùng giao thức ContractGuard (`analysis_requested` + S3 `request.json`, `ping/pong`). Để trống biến này chỉ khi chủ động dùng message nội bộ như mô tả trong [workers.md](workers.md).

Trên EC2 để trống `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_ENDPOINT_URL`. SDK lấy credential của instance role. Không chép credential local sang EC2.

Model mặc định đã có trong `agents/<agent>/config.yaml`: cả 3 agent `gemini-3.5-flash-lite`. Key Gemini đặt ở `GEMINI_API_KEY` trong `.env` (không ghi trong code). Có thể điền `AGENT_1_MODEL`/`AGENT_2_MODEL`/`AGENT_3_MODEL` để chọn model khác; `GEMINI_MODEL` ghi đè mặc định chung. Prompt nghiệp vụ và `configs/memory.yaml` đã có nội dung; cần rà lại theo workload khi đổi model.

### 2.4 Secret và quyền dịch vụ

Config service lấy JSON từ `API_KEYS_SECRET_ID`. Secret hiện dùng các tên khóa sau; không đưa giá trị vào `.env`:

```text
INTERNAL_API_TOKEN       MCP_API_TOKEN
TAVILY_API_KEY           QDRANT_URL           QDRANT_API_KEY
SUPABASE_URL             SUPABASE_PUBLISHABLE_KEY
SUPABASE_SECRET_KEY      LANGFUSE_PUBLIC_KEY  LANGFUSE_SECRET_KEY
```

Hai token nghiệp vụ cần giá trị ngẫu nhiên đủ dài; `INTERNAL_API_TOKEN` tối thiểu 32 ký tự. `CONFIG_TOKEN_*` vẫn nằm trong `.env` vì chúng dùng để xác thực lần lấy secret đầu tiên. Langfuse có thể tắt bằng `LANGFUSE_ENABLED=false`. Alias và allowlist: mục 1.

IAM role và quyền cross-account cần phù hợp các thao tác code thực sự gọi:

| Dịch vụ | Quyền sử dụng |
| --- | --- |
| Secrets Manager | Đọc JSON secret; thêm `kms:Decrypt` nếu secret dùng KMS key riêng |
| SQS requests | Nhận message, gia hạn visibility, xóa sau khi hoàn tất |
| SQS results | Gửi progress/completed/failed/pong |
| S3 hợp đồng | Đọc request/source; ghi clean/result; xóa source trong quarantine sau hoàn tất |
| SQS cancel (nếu dùng) | Nhận và xóa message |
| DynamoDB jobs/events | Đọc và ghi trạng thái/lease; đọc, thêm, thay log kết cục |
| S3 tham chiếu qua MCP | List/Get; connector này chỉ đọc |
| Data pipeline | Nhận/xóa/gia hạn queue cập nhật; ghi file gốc vào bucket tham chiếu |

Hai bảng DynamoDB phải có schema:

| Bảng | Partition key | Sort key | Cách lưu |
| --- | --- | --- | --- |
| `contract-risk-jobs` | `job_id` String | Không có | Một dòng mỗi hợp đồng: đang ở đâu, trạng thái gì |
| `contract-risk-job-events` | `job_id` String | `event_id` String | Một log kết cục mỗi job: completed, failed hoặc cancelled |

IAM policy tham khảo nằm trong `infrastructure/aws/`; chúng không thay toàn bộ quyền worker/cross-account của môi trường. `preflight.sh` kiểm STS và list bucket tham chiếu từ container, không chứng minh mọi quyền S3/SQS/DynamoDB hay quyền gọi Gemini đã đúng.

Container bridge cần truy cập được IMDS của instance role; script preflight nhắc kiểm tra IMDSv2 hop limit ≥ 2. Cần đường ra AWS APIs, Gemini API (generativelanguage.googleapis.com), Qdrant, Supabase, Tavily, website pháp luật và Langfuse nếu bật. Không cần mở cổng service nội bộ ra Internet; monitoring chỉ bind loopback.

### 2.5 Đưa model embedding/reranker lên EC2

`models/` **không đi theo Git clone**. MCP mount nó vào `/models:ro`; runtime mặc định offline. Cần có cache trước khi chạy setup/deploy.

Chọn một trong hai cách:

**Tải trên EC2 một lần** (cần Internet và dung lượng cho cache; script hiện ghi khoảng 3,3 GB cho bộ model mặc định):

```sh
python3.12 -m venv .venv312
.venv312/bin/python -m pip install -r infrastructure/requirements/mcp.txt
.venv312/bin/python scripts/download_models.py
```

**Hoặc copy cache đã tải từ máy phát triển**. Chạy `python scripts/download_models.py` trên máy đó trước; script chuẩn hóa metadata đường dẫn cho Linux. Ví dụ từ PowerShell tại root project:

```powershell
scp -i "C:\path\ec2-key.pem" -r .\models ubuntu@<EC2_HOST>:/opt/contract-risk-management/
```

Trên EC2, đảm bảo UID 10001 trong container và user data pipeline đọc được cache:

```sh
chmod -R a+rX models
du -sh models
```

Không đặt secret hoặc dữ liệu hợp đồng vào thư mục model. `setup.sh` chỉ kiểm tra thư mục có nội dung; kiểm model thật nằm ở bước nghiệm thu RAG.

### 2.6 Build và khởi động 9 service

```sh
sh infrastructure/ec2/setup.sh
sh infrastructure/ec2/deploy.sh
docker compose ps
docker compose logs --tail 100 config input-processing agent-1 agent-2 agent-3 langgraph mcp monitoring worker
```

`deploy.sh` thực hiện setup → build → preflight AWS (production) → `up -d --wait --wait-timeout 180`. Với `WORKER_ENABLED=false`, worker sống ở chế độ idle và chưa đọc queue. Nếu lệnh timeout, xem health/log từng service; không bật worker để bỏ qua lỗi readiness.

Kiểm tra bổ sung:

```sh
docker compose exec agent-1 python -m common.service_health
docker compose exec agent-2 python -m common.service_health
docker compose exec agent-3 python -m common.service_health
docker compose exec langgraph python -m common.service_health
docker compose exec mcp python -m common.service_health
docker compose exec worker python -m workers --check-config
```

Health/readiness không gọi một lượt phân tích thật và không bảo đảm kho luật có dữ liệu. Kiểm tra quyền model, RAG và kết quả nghiệp vụ bằng job thử được phép ở mục tiếp theo.

### 2.7 Bật worker và nghiệm thu một job

Khi các service healthy, sửa `.env` thành `WORKER_ENABLED=true` rồi recreate worker để nhận biến mới:

```sh
nano .env
docker compose up -d --no-deps --wait worker
docker compose logs -f --tail 100 worker langgraph
```

Việc bật worker bắt đầu nhận những message đang có trong requests queue. Dùng một hợp đồng thử đã thống nhất với bên gửi để kiểm tra:

1. Backend gửi `ping` và nhận `pong` qua results queue nếu dùng ContractGuard.
2. Gửi yêu cầu phân tích đúng schema; worker đọc `request.json`/source từ S3.
3. Trạng thái đi qua processing, Agent 1, Agent 2, Agent 3 và saving_result; cùng `job_id`/runtime xuất hiện trong monitoring.
4. S3 có `extraction.json` đúng `result_key` và bản clean đúng request. `AnalysisResult` có output tổng hợp, findings, recommendations và scoring theo schema.
5. DynamoDB jobs phản ánh trạng thái cuối; events có đúng một log `completed` của job. Langfuse có trace của job (`docker compose exec monitoring python -m monitoring.langfuse_exporter` nếu không thấy). Backend nhận `completed`; worker mới ACK/xóa message requests sau các bước lưu/hoàn tất.
6. Thử lỗi đầu vào có kiểm soát để kiểm thông báo `failed`/DLQ; không đưa dữ liệu nhạy cảm vào log hoặc commit.

Xem [workers.md](workers.md) và [architecture.md](architecture.md) về schema, retry và giới hạn. Chỉ chạy các bài Moto/fixture trong môi trường thử riêng; không chạy `container_smoke.py` vào stack production.

### 2.8 Khởi chạy data pipeline pháp luật trên cùng EC2

Bước này cần nếu muốn máy tự thu thập/cập nhật kho luật. Pipeline nằm ngoài Docker và sử dụng queue khác worker hợp đồng.

1. Xác nhận các biến data pipeline trong `.env`, secret Supabase/Qdrant và quyền ghi kho dữ liệu.
2. Với database mới, chạy lần lượt `data_pipeline/migrations/001_legal_schema.sql` và `002_versions_without_s3_json.sql` bằng SQL Editor của Supabase. Với database đã dùng, đối chiếu migration đã áp dụng trước khi chạy; không reset schema hoặc dữ liệu.
3. Cài dependencies/venv/systemd units:

```sh
sudo sh infrastructure/ec2/data_pipeline_setup.sh
sudo -u contract-data test -r /opt/contract-risk-management/.env
sudo -u contract-data test -r /opt/contract-risk-management/models
sudo systemctl start contract-data-processor contract-data-collector
sudo systemctl status contract-data-collector contract-data-processor --no-pager
sudo journalctl -u contract-data-collector -u contract-data-processor -n 100 --no-pager
```

Script cài Tesseract tiếng Việt và LibreOffice cho `.doc/.rtf`, tạo user `contract-data`, venv `/opt/contract-risk/data-pipeline/.venv`, enable hai service. Script đổi `.env` sang group `contract-data`, mode `640` để user service đọc được. Không đổi lại mode `600` nếu vẫn chạy pipeline dưới user này.

Collector đợi SQS do EventBridge Scheduler gửi; processor retry ingest jobs và refresh hiệu lực. Lần thu thập đầu có thể kéo dài. Qdrant writer tạo collection/index khi chưa có; collection có sẵn phải đúng cấu hình embedding 1024 chiều và sparse vector của project. Không xóa collection để xử lý lỗi cấu hình một cách tự động.

Nếu cần chạy thủ công một lượt nhỏ trước khi bật lịch, dừng collector để tránh hai lượt chạy cùng lúc rồi chạy:

```sh
sudo systemctl stop contract-data-collector
sudo -u contract-data env PYTHONPATH="$PWD" \
  /opt/contract-risk/data-pipeline/.venv/bin/python -m data_pipeline collect --mode incremental --limit 1
sudo systemctl start contract-data-collector
```

Lệnh này thu thập và **ghi thật** S3/Supabase/Qdrant. Sau đó kiểm số văn bản, ingest jobs, vector và tìm kiếm MCP/RAG. Chi tiết: [data_pipeline.md](data_pipeline.md).

### 2.9 Cập nhật, dừng và rollback

Để cập nhật đồng bộ source/image, chờ các job hiện tại hoàn tất và bố trí khoảng dừng nhận job. Ghi lại commit/tag image cũ; backup named volumes và dữ liệu ngoài trước thay đổi có ảnh hưởng schema/cache. Không dùng `down -v` hoặc `docker system prune --volumes` khi cần giữ dữ liệu.

```sh
docker compose stop worker
# Nếu đã cài data pipeline trên máy:
sudo systemctl stop contract-data-collector contract-data-processor
git pull --ff-only
git rev-parse --short HEAD
nano .env  # đối chiếu .env.example, đổi IMAGE_TAG sang tag mới
sh infrastructure/ec2/deploy.sh
```

Sau cập nhật code/dependency của data pipeline, chạy lại `data_pipeline_setup.sh` rồi start hai service. Script không tự áp dụng SQL migration. Nếu thay model embedding/reranker, chuẩn bị cache tương ứng trước deploy và lập kế hoạch index lại khi cần.

Rollback image Docker đã có sẵn:

```sh
sh infrastructure/ec2/rollback.sh <PREVIOUS_IMAGE_TAG>
```

Script chỉ đổi tag image cho lượt Compose đó; không rollback schema/volumes/source/systemd. Cập nhật `IMAGE_TAG` trong `.env` về tag đã chọn để lần `up` sau giữ đúng bản. Nếu cần rollback cả pipeline, dùng commit và dependency tương ứng, kiểm khả năng tương thích dữ liệu trước.

Các lệnh vận hành thường dùng:

```sh
docker compose logs --tail 100 worker monitoring
docker compose down                    # Giữ named volumes; systemd vẫn chạy
sudo systemctl stop contract-data-collector contract-data-processor
docker compose up -d --wait             # Khởi động lại Docker stack
sudo systemctl start contract-data-processor contract-data-collector
```

Đổi `.env` dùng `up -d` để recreate. Khi chỉ đổi giá trị secret trong Secrets Manager, restart Config rồi các client liên quan để chúng nạp lại; giữ nguyên token bootstrap trừ khi chủ động xoay đồng bộ.

### 2.10 Chẩn đoán lỗi

| Biểu hiện | Kiểm tra |
| --- | --- |
| Setup báo thiếu model | `models/` không đi theo Git; tải/copy theo mục 2.5, kiểm quyền đọc |
| Config/client lỗi 401/403 | Tám `CONFIG_TOKEN_*`, service identity và `config/policy.json` |
| Config lỗi AWS/secret | Region, secret ID, quyền GetSecretValue/KMS, instance role và IMDS; không in secret để debug |
| Preflight STS thất bại | IAM role, IMDS hop limit, route/egress của container, bỏ AWS_ENDPOINT_URL giả lập |
| Worker healthy nhưng không nhận job | `WORKER_ENABLED`, queue URL, message protocol, quyền SQS; worker idle là trạng thái hợp lệ |
| Gemini từ chối/thiếu model | Key `GEMINI_API_KEY` trong `.env`, model trong env/config, quota/rate limit của key; readiness chưa kiểm gọi model thật |
| RAG trả rỗng | Kho luật chưa nạp, filter hiệu lực, collection/schema vector, cache embedding/reranker |
| Systemd không đọc source/.env | Checkout trong `/opt`, quyền traverse/read của `contract-data`, `.env` group/mode; `ProtectHome=true` |
| Thay env nhưng chưa có tác dụng | Recreate service bằng Compose; restart systemd nếu thay cấu hình pipeline |

## 3. Monitoring và Langfuse

Mọi service gọi `monitoring.client.emit` → `POST /v1/events` + JSON ra stdout → SQLite → Langfuse (OTLP, không SDK). Chỉ gửi ID, stage,
trạng thái, thời gian, token, model; **không gửi nội dung hợp đồng**. Monitoring hỏng không ảnh hưởng job và không ghi DynamoDB.

| Xem gì | Ở đâu (cần token, trừ `/readyz`) |
| --- | --- |
| Độ trễ, lỗi, token, job lỗi | `/v1/metrics?window_minutes=60` |
| Timeline một job | `/v1/traces/{trace_id}` |
| Cảnh báo | `/v1/alerts` (`job_failed`, `model_throttled`, `slow_stage`, `data_*`), tùy chọn `ALERT_WEBHOOK_URL` |
| Tình trạng Langfuse | `/readyz`: `langfuse_active`, `langfuse_error`, `langfuse_pending` |

SQS tồn đọng đo bằng CloudWatch alarm. Log: `LOG_FORMAT=auto|json|text`; service hợp đồng không in nội dung exception. Xóa sự kiện cũ sau
`MONITORING_RETENTION_DAYS`, trừ hàng chưa gửi Langfuse. Observation tên `service.stage`, Session = `job_id`, Environment = `APP_ENV`.

**Không thấy log trên Langfuse:**

```bash
docker compose exec monitoring python -m monitoring.langfuse_exporter
```

| `error` | Cách sửa |
| --- | --- |
| `LANGFUSE_ENABLED is false` | `.env`: `LANGFUSE_ENABLED=true`, rồi `docker compose up -d monitoring` |
| `Empty LANGFUSE_...` | Thêm key vào Secrets Manager |
| `HTTP 401/403` | Key sai, hoặc `LANGFUSE_BASE_URL` khác vùng của key (EU cloud, US us.cloud, JP jp.cloud) |
| `cannot reach` | EC2 không ra được Internet tới Langfuse |
| `langfuse_ok: true` | Đã nhận; chọn đúng project và Environment trên dashboard |

Event bị từ chối được bỏ qua, không chặn event sau; lỗi key/mạng thì giữ lại và gửi bù. Org Langfuse mới chỉ đọc được bằng API v2
(`/api/public/v2/observations`). Chưa xác nhận trên EC2 của người dùng.

## 4. Kiểm thử

```sh
python -B -m unittest discover -s tests/unit -p "test_*.py"
```

Windows: `.venv312/Scripts/python.exe`. Unit test dùng Moto, model và client giả; không cần credential. Danh sách notebook:
[tests/README.md](../tests/README.md).

Smoke container (stack thử riêng, `APP_ENV=local`, `MODEL_PROVIDER=fixture`, không chạy vào production):

```sh
docker build --target test -t contract-risk/test:local -f infrastructure/docker/Dockerfile .
docker run --rm --network contract-risk_backend --env-file .env contract-risk/test:local
```

Đối chiếu dữ liệu từng điểm bàn giao: thêm `SMOKE_AUDIT_DIR=/audit`, `SMOKE_INPUT_KIND=txt|png` và mount volume chỉ đọc
`contract-risk_<service>-data` vào `/audit/<service>` (`tests/integration/handoff_audit.py`).

Đã kiểm: unit test; E2E bằng tiến trình thật với AWS/model/Langfuse giả lập (gồm cancel). **Chưa kiểm**: image build từ code mới nhất, AWS và
EC2 thật, hợp đồng thật với Gemini, độ chính xác OCR và pháp lý.
