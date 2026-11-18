# Kiểm tra image cho Docker trên EC2 — ghi chú bàn giao (2026-11-07)

> Đây là lịch sử kiểm tra theo từng mốc, không phải chứng nhận checkout hiện tại.
> Ngày 2026-11-18 model/prompt/memory đã có cấu hình; các TODO về chúng bên dưới là trạng thái cũ.
> Các thay đổi runtime mới nhất chưa được nghiệm thu lại bằng container/OpenAI thật.
> Dùng [ec2_setup_guide.md](ec2_setup_guide.md) để triển khai; secret nằm trong Secrets Manager,
> không đặt OPENAI_API_KEY vào `.env` như một số ghi chú lịch sử bên dưới.

Kết luận: **9 image đủ điều kiện kỹ thuật để chạy trên Docker của EC2** (Linux, amd64; arm64/Graviton
xem bảng). **Hệ thống chưa thể nhận job production** cho tới khi điền các cấu hình còn TODO (model,
prompt, memory budget) và hạ tầng AWS (IAM role, IMDS hop limit, SQS/S3/DynamoDB). Đây là cấu hình,
không phải lỗi image: service báo `not_ready` rõ ràng thay vì chạy sai.

## Kết quả kiểm tra image

| Hạng mục | Kết quả |
| --- | --- |
| Kiến trúc | 9/9 `linux/amd64`. Base `python:3.12-slim` pin theo **multi-arch index digest** nên build trên Graviton lấy đúng arm64 |
| Build arm64 (QEMU, cacheonly) | Đạt cho `mcp` (psycopg-binary; lúc đó còn ddgs, nay là tavily-python), `input-processing` (pypdfium2, Tesseract apt) và `langgraph` (= hermes-base: dependency của 3 agent + Hermes). worker/monitoring/config chỉ dùng base nên nằm trong tập đã build. Chưa chạy container arm64 |
| User | 9/9 chạy UID 10001 (`app`), `/app` không ghi được; Compose thêm `read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp` |
| Secret | Không có `.env`/`*.pem`/`*.key` trong image; không tìm thấy giá trị INTERNAL_API_TOKEN, MCP_API_TOKEN, CONFIG token, Langfuse secret trong `/app`; `docker history` không chứa token. Secret chỉ inject lúc chạy |
| Healthcheck | 9/9 có HEALTHCHECK (`common.service_health`); Compose `up --wait` 9/9 healthy |
| Dependency | Lock `uv pip compile --universal` (có platform marker), Tesseract eng/vie cài qua apt trong image processing |
| Script EC2 | LF, `sh -n` đạt; `setup.sh` chạy trong container Linux với Compose 2.33 đạt |
| Chức năng | E2E TXT 3 chunk + PNG OCR (Moto + model fixture) exit 0 trên image hiện tại; 129/129 unit test |
| Tài nguyên | Rảnh ~480 MiB RAM tổng; image 1.5 GB; build cache ~8 GB nếu build trên host |

## Mô phỏng production (APP_ENV=production, MODEL_PROVIDER=openai)

| Service | Kết quả | Cần làm |
| --- | --- | --- |
| agent-1/2/3 | `ConfigurationError: TODO: configure OpenAI OPENAI_MODEL, OPENAI_API_KEY`; khi có model + key → `TODO: fill prompts/core.md` | Đặt `OPENAI_API_KEY` + `OPENAI_MODEL` hoặc `AGENT_n_MODEL`; viết prompts/core.md, external.md, examples/, config.yaml (docs/agent_core.md) |
| langgraph | `Set positive input/summary budgets` | Điền `configs/memory.yaml` |
| worker (WORKER_ENABLED=true) | `WORKER_SQS_QUEUE_URL, WORKER_DYNAMODB_TABLE and WORKER_OUTPUT_BUCKET are required` | Tạo SQS/DynamoDB (jobs + events)/S3 và điền `.env` |
| mcp | ready; web_search (Tavily) cần `TAVILY_API_KEY` | `TAVILY_API_KEY`; tùy chọn `QDRANT_URL`, `POSTGRES_DSN`, `MCP_S3_BUCKETS` |
| config/monitoring/input-processing | ready | — |

## Rủi ro riêng EC2 đã xử lý bằng script

- **IMDSv2 hop limit**: container trong bridge network cần hop limit ≥ 2 để lấy credential của
  instance role. `infrastructure/ec2/preflight.sh` (deploy.sh tự gọi khi APP_ENV=production) gọi STS
  từ container worker; thất bại → dừng deploy với hướng dẫn. Đã thử nhánh thất bại local (NoCredentialsError, exit 1).
- **Compose**: `env_file {path, required}` cần Compose ≥ 2.24; setup.sh kiểm tra phiên bản.
- **.env production**: setup.sh yêu cầu `AWS_REGION`, `MODEL_PROVIDER=openai` và `OPENAI_API_KEY` (hoặc binding Config); fixture bị bootstrap từ chối ngoài local.
- **Egress**: cần api.openai.com, SQS/S3/DynamoDB/STS (NAT hoặc VPC endpoint), Langfuse cloud nếu bật, internet tới api.tavily.com cho web_search.

## Chưa kiểm chứng

- Chưa chạy trên EC2 thật, chưa gọi AWS/OpenAI thật, chưa đo tải thật.
- arm64 chỉ build dưới QEMU; chưa chạy container/E2E trên Graviton.
- Không có registry (ECR) push/pull: deploy.sh build trên chính host EC2.
- Build trên EC2: Dockerfile đã tách dependency/code, sửa code chỉ build lại layer code (xem monitoring.md).
- SQLite volume chỉ cho một host; không backup tự động. Monitoring có retention; cache workflow/job chưa có.

## Kiểm tra image sau đợt Secrets Manager / memory / RAG / OpenAI (2026-11-14, mới nhất)

| Image | Size | Ghi chú |
| --- | --- | --- |
| worker, monitoring, config | ~251 MB | base |
| input-processing | 459 MB | Tesseract eng/osd/vie |
| agent-1/2/3, langgraph | 743 MB | Hermes + openai 2.54 + langgraph |
| mcp | 643 MB | rag/, tavily, qdrant-client, supabase, fastembed (model đọc offline từ `./models` mount `/models:ro`, tải trước bằng `scripts/download_models.py`; `setup.sh` dừng nếu thiếu) |

- Cả 9 build lại từ code hiện tại; chạy UID `app`, có HEALTHCHECK, linux/amd64; `pip check` sạch; import module chính OK.
- Không có `.env` hay chuỗi key (tvly-, sb_secret_, sk-lf-, sk-proj-) trong image; container config không chứa 11 key.
- Chạy thử cả stack trong project Compose riêng với Secrets Manager giả (AWS_ENDPOINT_URL, giá trị dummy): 9/9 healthy;
  config gọi GetSecretValue 2 lần (cache 30 s); monitoring nhận sự kiện của 7 service (INTERNAL_API_TOKEN từ secret khớp);
  agent-2 → MCP xác thực bằng MCP_API_TOKEN từ secret; cổng loopback `127.0.0.1:<MONITORING_HOST_PORT>` trả healthz;
  1 hợp đồng 3 chunk qua langgraph → 3 agent (fixture) `completed`, bước reflect chạy (prompt trống → skipped).
  Sự kiện reflect sau khi xong không làm lùi tiến độ worker (worker chỉ nhận progress tăng, khi job còn running).
- Chưa kiểm: Secrets Manager/IAM thật, arm64 cho image mới, OpenAI thật.
