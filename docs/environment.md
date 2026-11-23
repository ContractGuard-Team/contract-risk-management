# Environment và secret runtime

`.env` tại root là file host, ignored bởi Git và Docker. `scripts/init_env.py` tạo
file mới với token ngẫu nhiên và local fixture mode; không in secret, không ghi đè file
đã có. `.env.example` là mẫu production, không chứa credential thật.

Compose dùng env_file cho Config broker và environment mapping tường minh cho từng
service còn lại. Chỉ Config nhận toàn bộ map token principal; mỗi client chỉ nhận
CONFIG_TOKEN riêng. INTERNAL_API_TOKEN dùng cho API nghiệp vụ nội bộ và tách biệt với
config token. Không có Docker socket; monitoring bind loopback `127.0.0.1:8099` mặc định để data pipeline trên host gửi event.

## Local và production

Local đã tạo: APP_ENV=local, MODEL_PROVIDER=fixture, WORKER_ENABLED=false.
Ba agent chạy fixture rõ ràng để kiểm tra transport; không tạo phân tích pháp lý thật.
Worker idle có health `mode=idle`, không gọi SQS. Không âm thầm fallback từ Gemini
sang fixture khi lỗi. Fixture bị từ chối nếu APP_ENV khác local.

Production: APP_ENV=production, MODEL_PROVIDER=gemini. Key Gemini chỉ đặt GEMINI_API_KEY trong root .env (gitignored),
KHÔNG qua Secrets Manager và không ghi trong code. Model mặc định trong config.yaml
từng agent (`gemini-3.5-flash-lite`); GEMINI_MODEL hoặc AGENT_1/2/3_MODEL dùng để ghi đè.
Prompt nghiệp vụ, configs/memory.yaml và short_term_summary đã có nội dung (2026-11-18).
Prompt long_term_reflection còn trống nên tự đề xuất bài học đang được bỏ qua. Readiness kiểm cấu hình
bắt buộc, không kiểm gọi Gemini thật. Xem [hướng dẫn EC2](ec2_setup_guide.md).

Bật WORKER_ENABLED=true sau khi đặt SQS URL, DynamoDB table, input/output S3 bucket.
URLs nội bộ do Compose cấp theo service name; không dùng localhost để gọi container khác.
AWS_ENDPOINT_URL để trống trên AWS thật; chỉ dùng cho bài kiểm thử emulator tường minh.
HTTP_TIMEOUT_SECONDS mặc định 120, JOB_TIMEOUT_SECONDS 3600; worker vẫn có heartbeat/
visibility/lease và giới hạn thời gian SQS riêng. Đồng bộ timeout với workload thực tế.

## AWS credential và secret access (2026-11-14, mới nhất)

Ưu tiên EC2 IAM instance role và SDK credential provider chain. Không điền access key tĩnh vào .env.
Container phải tới được IMDS (hop limit ≥ 2). Compose không provision role hoặc sửa metadata options của EC2.

**Một file env:** `.env.example` (tracked, mẫu) và `.env` (gitignored, giá trị thật) cùng bố cục 9 mục, mỗi key một lần:
1 Runtime · 2 AWS · 3 Secrets · 4 Config tokens · 5 Agents · 6 Worker · 7 MCP/RAG · 8 Monitoring · 9 Data pipeline.
Data pipeline (EC2 systemd) đọc chính root `.env` (đã bỏ `data_pipeline/.env.example` và `/etc/contract-risk/data-pipeline.env`).
Vẫn giữ `.env.example` vì `.env` không được commit: máy mới (EC2) cần mẫu để tạo `.env`, `scripts/init_env.py` đọc nó.

**Secrets Manager `contract-risk/prod/api-keys`** — JSON 10 key: INTERNAL_API_TOKEN, MCP_API_TOKEN,
TAVILY_API_KEY, QDRANT_URL, QDRANT_API_KEY, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY,
LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY. `.env`: `API_KEYS_SECRET_ID=contract-risk/prod/api-keys`.

```text
service start -> config.client.bootstrap -> key nào RỖNG trong env -> GET http://config:8000/v1/secrets/<KEY>
  (Bearer CONFIG_TOKEN riêng) -> Config service kiểm config/policy.json -> Secrets Manager (instance role,
  1 GetSecretValue/30 s cho cả JSON) -> giá trị vào biến môi trường của tiến trình (không ghi file, không log)
Config service tự đọc INTERNAL_API_TOKEN thẳng từ Secrets Manager; data pipeline đọc thẳng
  (config.secrets.read_secret_json) cho QDRANT_URL/QDRANT_API_KEY/SUPABASE_URL/SUPABASE_SECRET_KEY.
```

| Service | Key được cấp (config/policy.json = CONFIG_SECRET_BINDINGS trong compose) |
| --- | --- |
| config, worker, input-processing, langgraph | INTERNAL_API_TOKEN |
| agent-1 | INTERNAL_API_TOKEN (GEMINI_API_KEY lấy từ .env qua Compose, không qua Config) |
| agent-2, agent-3 | INTERNAL_API_TOKEN, MCP_API_TOKEN |
| monitoring | INTERNAL_API_TOKEN, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY |
| mcp | INTERNAL_API_TOKEN, MCP_API_TOKEN, TAVILY_API_KEY, QDRANT_URL, QDRANT_API_KEY, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY |
| data pipeline (systemd) | QDRANT_URL, QDRANT_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY, INTERNAL_API_TOKEN (gửi monitoring) |

**11 key này KHÔNG có trong `.env`/`.env.example`/docker-compose** (đã xóa để tránh lộ): service chỉ nhận chúng từ Config
lúc khởi động, giữ trong RAM của tiến trình. `setup.sh` từ chối `.env` còn chứa key nào trong 11 key. Hệ quả: chạy được
(kể cả local) cần credential AWS đọc được secret — EC2 dùng instance role; máy dev phải cấp credential tạm qua mục 2.
Thiếu `API_KEYS_SECRET_ID` → Config trả 404 → service thiếu INTERNAL_API_TOKEN sẽ dừng. 401 thiếu/sai token, 403 alias
không được cấp, 503 lỗi AWS (fail fast). Code vẫn tôn trọng giá trị đặt sẵn trong môi trường tiến trình (test/CLI), không
ghi đè. Đổi secret cần restart service; không hot reload.

`CONFIG_TOKEN_*` (mục 4) là token khởi động riêng từng service để hỏi Config; nằm ở `.env` của host, không đưa vào
Secrets Manager (phải có trước khi hỏi). IAM: gắn `infrastructure/aws/config-secrets-policy.json` (Config service) và
`data-pipeline-policy.json` (đã thêm `secretsmanager:GetSecretValue` trên `contract-risk/prod/api-keys-*`) vào instance role;
secret mã hóa bằng KMS key riêng cần thêm `kms:Decrypt`.

## Langfuse

LANGFUSE_ENABLED=false mặc định. Khi bật và đủ LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL,
monitoring export OTLP HTTP JSON. Thiếu keys vẫn lưu telemetry nội bộ và báo
langfuse_active=false. Không cài Langfuse Server hoặc SDK trong agent.

Theo [Langfuse OTLP](https://langfuse.com/integrations/native/opentelemetry), endpoint
/api/public/otel/v1/traces dùng Basic auth và header x-langfuse-ingestion-version: 4.
Không dùng API ingestion legacy. Đã xác minh với Langfuse cloud thật ngày 2026-11-07;
xem [langfuse_monitoring.md](langfuse_monitoring.md). Secret provider dùng API chính thức
[Secrets Manager](https://docs.aws.amazon.com/boto3/latest/reference/services/secretsmanager/client/get_secret_value.html)
và [SSM](https://docs.aws.amazon.com/boto3/latest/reference/services/ssm/client/get_parameter.html).
