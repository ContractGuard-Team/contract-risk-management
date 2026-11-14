# Monitoring — ghi chú bàn giao (2026-11-07)

Đọc file này trước khi sửa `monitoring/`. Langfuse: [langfuse_monitoring.md](langfuse_monitoring.md).

## Thành phần

| File | Vai trò |
| --- | --- |
| `client.py` | `emit()` in JSON ra stdout rồi đẩy vào **hàng đợi trong RAM** (1000, thread nền gửi `POST /v1/events`). Không bao giờ chặn luồng xử lý hợp đồng khi collector chậm/sập; đầy hàng đợi → `dropped_queue_full`. `flush()` khi tắt process |
| `service.py` | Collector: sanitize allowlist → SQLite `/data/events.sqlite3` (outbox Langfuse + worker), đánh giá alert, retention |
| `metrics.py` | `GET /v1/metrics?window_minutes=60`: theo `service.stage` count/completed/failed/error_rate/avg/p95/max ms; token theo agent/model; lỗi theo loại; job hoàn tất/lỗi; 50 job lỗi gần nhất |
| `tracing.py` | `GET /v1/traces/{trace_id}`: timeline một job qua các service (thứ tự, at_ms, duration, status, lỗi đầu tiên, wall time) |
| `alerts.py` | Luật: `job_failed` (1 alert/job dù lỗi lan qua agent → langgraph → worker), `model_throttled` (≥3 `TransientModelError`/10 phút), `slow_stage` (≥15 phút). Lưu bảng `alerts`, in log JSON `{"alert":...}`, gửi `ALERT_WEBHOOK_URL` (https, payload `{"text","alert"}` hợp Slack). `GET /v1/alerts` |
| `logging.py` | Log JSON một dòng cho mọi service (`serve()` và worker): timestamp, level, service, logger, message, error_type. Không in traceback/thông điệp exception (có thể chứa nội dung hợp đồng); tắt INFO của httpx/botocore/urllib3/mcp |
| `langfuse_exporter.py` | OTLP → Langfuse: `running/started` = `event`, `completed/failed` = `span`, có token = `generation`; lỗi `level=ERROR` + `status_message` |
| `service_observer.py`, `worker_observer.py`, `observer.py`, `usage.py` | Như trước: span agent/graph → event; tiến độ IPC của worker; usage OpenAI (`model_usage`) |

Mọi endpoint trừ `/healthz`, `/readyz` cần `Authorization: Bearer INTERNAL_API_TOKEN`; không publish port.

## Trả lời các câu hỏi thiết kế

- Job nào lỗi? `/v1/metrics` → `failed_jobs`; `/v1/alerts`; `/v1/traces/{trace_id}` → `first_failure`.
- Agent nào chậm? `/v1/metrics` → `stages["agent-2.job"].p95_ms`...; alert `slow_stage`.
- Model (OpenAI) dùng bao nhiêu token? `/v1/metrics` → `tokens["agent-1/<model>"]`; Langfuse generation có usage/model.
- Worker quá tải? `jobs` + `stages["worker.*"]` ở đây; **SQS tồn đọng đo bằng CloudWatch alarm** trên
  `ApproximateAgeOfOldestMessage` / `ApproximateNumberOfMessagesVisible` (collector không gọi SQS).
- Qdrant/tool mất bao lâu? `stages["mcp.tool.qdrant_search"]` (avg/p95/max).

Cách xem trong máy chủ (không mở port):

```bash
docker compose exec monitoring python -c "import os,json,urllib.request as u;print(json.dumps(json.load(u.urlopen(u.Request('http://127.0.0.1:8000/v1/metrics?window_minutes=60',headers={'Authorization':'Bearer '+os.environ['INTERNAL_API_TOKEN']}))),indent=1))"
```

## Cấu hình (.env)

`LOG_LEVEL=INFO`, `MONITORING_RETENTION_DAYS=30` (xóa event/alert cũ mỗi giờ, không xóa hàng chưa
export Langfuse/worker hoặc alert chưa gửi webhook), `ALERT_WEBHOOK_URL=`, `ALERT_SLOW_STAGE_MS=900000`,
`ALERT_THROTTLE_COUNT=3`, `ALERT_THROTTLE_WINDOW_MINUTES=10`, `MONITORING_QUEUE_SIZE=1000`.

## Dockerfile (thay đổi kèm theo)

Lần build đầu sau khi sửa code monitoring bị lỗi `No matching distribution found for langchain-core`
(PyPI không phản hồi) vì stage `base` COPY code trước `pip install` của các stage con: mọi thay đổi code
cài lại toàn bộ dependency. Đã sắp lại: dependency trước, code (`shared` stage: common/config/monitoring)
sau. Kiểm chứng: sửa monitoring → layer pip `CACHED`, build agent-2 + mcp 40 giây; monitoring 14 giây.
Code trong image giờ thuộc root (user `app` không sửa được).

## Đã kiểm chứng

- 138/138 unit test (9 test mới `tests/unit/test_monitoring.py`: emit không chặn khi collector treo,
  metrics, timeline, alert dedupe/throttle/slow/replay, retention, endpoint HTTP, webhook https, log JSON
  không lộ exception, loại observation Langfuse).
- 9/9 image build mới, 9/9 healthy; E2E TXT + PNG/OCR exit 0 (12 PASS mỗi ca).
- Trong container: log JSON ở mọi service; metrics có p95 theo agent và token theo model; timeline job mới
  nhất `completed` qua 6 service; Langfuse cloud nhận 19 EVENT / 11 SPAN / 3 GENERATION, outbox 0.

## Chưa làm

- Hàng đợi emit nằm trong RAM: process chết đột ngột thì event còn trong hàng đợi chỉ có ở stdout.
- Cây span Langfuse vẫn phẳng (chưa lan truyền parent span id qua HTTP).
- Chưa có dashboard web; chưa thử webhook thật (chưa có URL); chưa tạo CloudWatch alarm SQS.
- Metrics tính bằng Python trên 50.000 event mới nhất; cần cột có index nếu lưu lượng lớn.

## Data pipeline trong monitoring + log terminal (2026-11-14, mới nhất)

**Pipeline → monitoring.** Hai service systemd chạy trên host gửi sự kiện tới container monitoring qua cổng loopback
`127.0.0.1:${MONITORING_HOST_PORT:-8099}` (không mở ra ngoài): `.env` mục 9 `DATA_MONITORING_URL=http://127.0.0.1:8099`,
token `INTERNAL_API_TOKEN` đọc từ Secrets Manager. Service: `data-collector`, `data-processor` (thêm vào `SERVICES`).

| stage | status | Ý nghĩa |
| --- | --- | --- |
| `run` | started / completed / incomplete / failed | một lượt thu thập (1 message SQS hoặc lệnh `collect`), `count` = số văn bản mới+đổi |
| `listing` | completed | số văn bản/trang đã liệt kê (`count`), thiếu so với tổng trên web |
| `document` | collected / no_file / failed | từng văn bản: file gốc lên S3, thiếu đính kèm, lỗi tải/parse (`document` = số hiệu) |
| `process` | done / needs_review / retry / failed | xử lý → Supabase + Qdrant; `failed` = bỏ cuộc sau 3 lần |
| `sqs_message` | received / deleted / kept | message EventBridge; `kept` = lượt lỗi, SQS sẽ gửi lại |
| `refresh`, `retry_cycle`, `requeue`, `cycle`, `service` | … | vòng lặp processor (hiệu lực hằng ngày, retry, job kẹt, lỗi vòng) |

- Mỗi lượt chạy là **một trace_id**: `GET /v1/traces/<trace_id>` (hoặc Langfuse nếu bật) cho timeline cả lượt;
  `/v1/metrics` có `data-collector.process` (p95, error_rate…), `/v1/events?trace_id=`.
- Cảnh báo mới (`/v1/alerts`, webhook nếu có): `data_run_incomplete`, `data_run_failed`, `data_document_failed`,
  `data_sqs_retry`. `slow_stage` chỉ áp dụng cho `process` của pipeline (cả lượt full chạy nhiều giờ là bình thường).
- **Worker giữ nguyên:** sự kiện pipeline không có `job_id/runtime_id/...` nên `job_scoped()` = false → không bao giờ
  được chuyển sang worker/DynamoDB. Luồng monitoring → worker của hợp đồng không đổi.
- Monitoring không chạy (hoặc `DATA_MONITORING_URL` rỗng): pipeline vẫn chạy, log terminal/journald đầy đủ.

**Log trên terminal (mọi service, `monitoring/logging.py`).** `LOG_FORMAT=auto` (mặc định): chạy trong terminal → dòng
dễ đọc `HH:MM:SS LEVEL service logger: message`; systemd/Docker → JSON 1 dòng. Lỗi luôn có `error_type`, `error_at`
(file:dòng trong hàm) và `stack` (các frame code). Service hợp đồng KHÔNG in nội dung thông báo lỗi (có thể chứa
text hợp đồng); data pipeline (dữ liệu luật công khai) in đầy đủ thông báo + traceback. Pipeline log mỗi bước dạng
`[stage status] nội dung (document=… count=… duration_ms=…)`; lệnh lỗi in 1 dòng lỗi + traceback và exit 1.

```bash
sudo -u contract-data env PYTHONPATH=$PWD /opt/contract-risk/data-pipeline/.venv/bin/python -m data_pipeline collect --limit 1
journalctl -u contract-data-collector -f        # service: JSON, hoặc LOG_FORMAT=text trong unit để đọc trực tiếp
docker compose logs -f monitoring               # sự kiện/alert đã nhận
```
