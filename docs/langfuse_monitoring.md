# Langfuse + Monitoring — ghi chú bàn giao (2026-11-07)

Đọc file này trước khi làm tiếp phần giám sát/Langfuse. Không ghi key vào đây hay bất kỳ file tracked nào.

## Trạng thái hiện tại (đã kiểm chứng)

- Root `.env` (gitignored) có `LANGFUSE_ENABLED=true`, `LANGFUSE_BASE_URL=https://cloud.langfuse.com`
  (EU) và cặp project key. Key đã kiểm tra bằng `GET /api/public/projects` → 200.
- Đã rebuild/recreate 8 service bằng `docker compose up -d --build --wait`; 8/8 healthy.
  Monitoring `/readyz`: `langfuse_active=true`, `worker_forwarding=false`.
- Outbox monitoring đã đẩy hết 222/222 event cũ (5 trace E2E fixture) lên Langfuse, 0 retry.
  Đã đọc lại trace `2ea72ee4c8924885fc7495795c877f38` qua API v2: 39 observation,
  đủ phase worker → input-processing → langgraph → agent-1/2/3 → saving_result/completed,
  generation agent có usage, `environment=local`, `sessionId=job_id`.
- 113/113 unit test đạt (`python -B -m unittest discover -s tests/unit -p "test_*.py"`).

## Luồng dữ liệu

```text
worker / input-processing / langgraph / agent-1..3 / config
  → monitoring.client.emit (HTTP POST /v1/events + stdout JSON, chỉ field allowlist)
  → monitoring/service.py: sanitize → SQLite /data/events.sqlite3 (outbox)
  → monitoring/langfuse_exporter.py: OTLP HTTP/JSON → {BASE_URL}/api/public/otel/v1/traces
     (Basic auth, header x-langfuse-ingestion-version: 4)
```

Không cài Langfuse SDK, không gửi nội dung hợp đồng/prompt/output — chỉ ID, stage, status,
thời gian, retry, token usage, model. Langfuse không nằm trên đường hoàn tất job.

## Thay đổi lần này

| File | Thay đổi |
| --- | --- |
| `monitoring/client.py` | Thêm `model` vào allowlist FIELDS |
| `monitoring/service_observer.py` | Generation span gửi kèm `model` khi provider có `model_id` |
| `monitoring/langfuse_exporter.py` | Tên observation `service.stage` (vd `agent-1.job`); thêm `langfuse.environment` từ `APP_ENV`; thêm `langfuse.observation.model.name` |
| `tests/unit/test_service_architecture.py` | Assert tên span, environment, model, type generation |
| `.claude/skills/langfuse/` | Skill chính thức `langfuse/skills` (MIT, commit 6fb2baf), xem `SOURCE.txt` |

Tên observation giờ là API ổn định cho dashboard/evaluator: đừng đổi tùy tiện.
`APP_ENV=local` → trace fixture tách khỏi `production` trong Langfuse (lọc theo Environment).

## Worker ↔ Monitoring: tiến độ từng phase

1. **Worker → monitoring: đã nối.** `workers/runtime/intake_worker.py::_run_pipeline` gọi
   `emit("worker", stage, ...)` tại processing, từng stage LangGraph (agent_1/2/3 lấy từ
   polling), saving_result, completed và `pipeline failed`. Hai mốc `downloading` (5%) và
   `dispatching` (10%) chỉ đi IPC → DynamoDB, **không** gửi monitoring.
2. **Tiến độ DynamoDB không phụ thuộc monitoring.** Intake poll `GET /v1/jobs/{key}` của
   LangGraph → IPC → progress process → DynamoDB. Monitoring/Langfuse hỏng không ảnh hưởng.
3. **Monitoring → worker (chiều ngược): ĐÃ BẬT 2026-11-07** (`MONITORING_WORKER_ENABLED=true`
   trong `.env`, monitoring `/readyz` báo `worker_forwarding=true`, worker ingress trả 400 cho
   event rỗng = route hoạt động; backlog forward = 0). Chưa có job thật đi qua đường này. `monitoring/worker_exporter.py`
   forward event đủ 5 ID (job/runtime/contract/trace/request) tới `POST worker:8000/v1/monitoring-events`
   → `workers/runtime/monitoring_inbox.py` (SQLite) → progress process →
   `ContractJobStore.observe` ghi bảng `WORKER_DYNAMODB_EVENTS_TABLE` và chiếu stage/progress
   (`progress_view`). Bật bằng `MONITORING_WORKER_ENABLED=true` trong `.env` rồi recreate
   monitoring. Code này viết sau `docs/current_connections_report.md` nên báo cáo đó chưa mô tả.
   Hiện worker idle nên bật cũng chưa có job để forward.

## Lưu ý khi làm tiếp

- Org Langfuse này tạo sau 2026-09-16: **API legacy `/api/public/traces` bị chặn**
  (`LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION`, toàn bộ v3 sunset 2026-11-16). Đọc dữ liệu bằng
  `GET /api/public/v2/observations?fields=core,basic,usage&traceId=<id>&fromStartTime=<iso>`.
  `workers/connectors/langfuse.py` (reader cũ, không dùng trong Compose) cần kiểm tra lại endpoint
  nếu muốn khôi phục.
- Khoảng trống so với https://langfuse.com/docs/observability/best-practices (chưa sửa):
  - Cây span phẳng: mỗi event là root observation; cần lan truyền parent span id qua HTTP
    (worker → langgraph → agent) để lồng `agent`/`generation` đúng cấp.
  - Event `running`/`started` và `completed` cùng stage thành 2 observation (vd `agent-1.job` ×2) → nhiễu.
  - Không có trace input/output (cố ý vì dữ liệu hợp đồng nhạy cảm) — chỉ thêm khi có chính sách mask.
  - Chưa có tool span (agent chưa có vòng lặp tool), chưa capture thinking, chưa có tags/userId.
  - Model name của event cũ trống; fixture provider dùng model `transport-fixture`. OpenAI thật sẽ có tên model.
- Skill `.claude/skills/langfuse` yêu cầu đọc docs mới nhất trước khi code Langfuse
  (`https://langfuse.com/llms.txt`, thêm `.md` vào URL trang docs).
- Secret key đã từng được dán vào chat: nên tạo key mới trong Langfuse Settings → API Keys,
  thay trong `.env`, rồi `docker compose up -d monitoring`.

## Lệnh hữu ích

```bash
docker compose exec -T monitoring python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read().decode())"
```

```bash
docker compose exec -T monitoring python -c "import sqlite3;db=sqlite3.connect('file:/data/events.sqlite3?mode=ro',uri=True);print(db.execute('select count(*) from events where exported=0').fetchone())"
```
