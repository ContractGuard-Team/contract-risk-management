# Build, run, debug và triển khai EC2

## Local

Yêu cầu Docker Engine + Docker Compose v2. Máy nghiệm thu dùng Linux containers,
Python 3.12, image linux/amd64. Các dependency Python đã khóa phiên bản cross-platform;
base Python image được pin digest trong infrastructure/docker/Dockerfile.

```sh
# Nếu chưa có .env; không ghi đè .env đã tồn tại.
python scripts/init_env.py
docker compose config --quiet
docker compose build
docker compose up -d --wait
docker compose ps
```

init_env tạo token ngẫu nhiên, APP_ENV=local, MODEL_PROVIDER=fixture,
WORKER_ENABLED=false. Đây là smoke mode, không phân tích hợp đồng bằng LLM thật.
Không public port; truy vấn/debug từ một container trên network hoặc docker compose exec.

```sh
docker compose logs --tail 100 worker input-processing langgraph monitoring
docker compose exec worker python -m workers --check-config
docker compose exec agent-1 python -m common.service_health
# Dừng ứng dụng nhưng giữ dữ liệu:
docker compose down
```

Không dùng down --volumes hoặc prune nếu cần giữ job/cache/events. Các volume chứa
hợp đồng đã xử lý và kết quả; đặt chính sách backup/retention phù hợp khi vận hành thật.
Root Compose là nguồn cấu hình duy nhất; không cần Compose riêng từng service.

## Kiểm thử xuyên container

```sh
docker build --target test -t contract-risk/test:local -f infrastructure/docker/Dockerfile .
docker run --rm --network contract-risk_backend --env-file .env contract-risk/test:local
```

Đổi network nếu COMPOSE_PROJECT_NAME thay đổi. Test image dùng Moto HTTP server cho
SQS/S3/DynamoDB, worker supervisor spawn thật, HTTP đến preprocessing/LangGraph/3 agent
thật và model fixture. Test xác nhận auth, runtime, S3-before-ACK, duplicate job,
monitoring/correlation/token usage và SIGTERM. Test image không phải service vận hành.

## EC2

1. Chuẩn bị EC2 và Docker Engine/Compose. Image đã nghiệm thu là linux/amd64;
   build lại đúng platform nếu dùng ARM/Graviton. Project không tự provision EC2/IAM.
2. Upload source checkout gồm agents/hermes/hermes_agent, dependency locks và **thư mục `models/`** (model đã tải
   sẵn bằng `python scripts/download_models.py`, ~3,3 GB, gitignored nên phải copy kèm, vd rsync). Không upload
   .venv/.cache/.git, không đưa .env vào image hoặc repository.
3. Tạo root .env riêng trên EC2 từ .env.example, sinh CONFIG_TOKEN_*. Đặt APP_ENV=production, MODEL_PROVIDER=openai,
   OPENAI_MODEL; API key nằm ở Secrets Manager (docs/environment.md). Điền prompt và memory budgets.
4. Gắn IAM instance role phù hợp, cho SDK truy cập credential chain từ container.
   **IMDSv2 hop limit phải ≥ 2** (container chạy bridge network; hop limit 1 chỉ host lấy được
   credential): `aws ec2 modify-instance-metadata-options --instance-id <id> --http-tokens required
   --http-put-response-hop-limit 2`. Egress cần tới api.openai.com, SQS/S3/DynamoDB/STS (VPC endpoint hoặc NAT),
   Langfuse cloud nếu bật, và internet công khai cho `web_search` của MCP. Không hardcode access key.
5. Tạo/cấp sẵn SQS, S3 input/output và DynamoDB table partition key job_id (String),
   không sort key. Bật WORKER_ENABLED=true sau khi đặt queue/table/bucket.
6. Nếu dùng secrets: khai báo alias/policy và *_SECRET_BINDINGS. Secret value chỉ lấy
   từ Secrets Manager/SSM lúc startup. Langfuse có thể tiếp tục tắt khi chưa có key.
7. Chạy `sh infrastructure/ec2/setup.sh`, `sh infrastructure/ec2/deploy.sh`.
   setup kiểm tra Compose ≥ 2.24 và .env production (AWS_REGION, MODEL_PROVIDER=openai, OPENAI_API_KEY). Với
   APP_ENV=production, deploy chạy `preflight.sh`: gọi STS từ container worker để chứng minh IAM role,
   hop limit, region và egress, rồi list bucket tham chiếu từ container mcp (policy
   `infrastructure/aws/mcp-s3-readonly-policy.json` phải gắn vào instance role). Script không SSH hoặc tạo tài nguyên AWS.
8. Kiểm tra readiness, log, queue redrive/DLQ và một job đã biết kết quả trước khi nhận
   traffic thật. Không mở public port cho config/monitoring/agent; nếu cần truy cập từ
   ngoài, đặt gateway có TLS/auth riêng theo phạm vi triển khai.

Các phần AI còn TODO không được bỏ qua bằng fixture trong production. Readiness
không xác nhận quyền IAM/model quality; cần kiểm chứng AWS/OpenAI thật ở môi trường đích.

## Kích thước máy

9 container lúc rảnh dùng ~480 MiB RAM (đo local). Đề xuất tối thiểu 2 vCPU / 4 GiB (vd t3.medium) cho
tải nhẹ, 8 GiB nếu OCR/PDF lớn hoặc scale agent. Disk ≥ 30 GiB gp3: image ~1.5 GB, build cache khi build
trên host có thể ~8 GB, cộng volume SQLite. Xem [ec2_readiness.md](ec2_readiness.md).

## Cập nhật hoặc scale độc lập

```sh
docker compose build agent-2
docker compose up -d --no-deps --wait agent-2
# Scale agent khi cần; Compose không cố định container_name:
docker compose up -d --scale agent-2=2
```

Dữ liệu job của một agent nằm trong volume chung trên một host; SQLite claim/lease
fence các runner dùng volume đó. Không chuyển volume SQLite sang NFS/multi-host.
Giữ cùng revision model/prompt giữa replicas; graph từ chối revision thay đổi giữa job.
Khi thay prompt/model cho job mới, dùng job_id mới, tránh ghi đè kết quả cũ.

Để rollback tag đã build/pull sẵn: `sh infrastructure/ec2/rollback.sh previous-tag`.
Không rollback dữ liệu bằng xóa volume. Compose logging giới hạn 10 MiB × 3 file/container;
collector lưu telemetry trong volume riêng. Secret rotation hiện cần restart client.
