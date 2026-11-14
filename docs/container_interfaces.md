# Giao diện nội bộ multi-container

Tất cả service nghe cổng 8000 trong network Compose `backend`, không publish port host.
HTTP dùng `Authorization: Bearer INTERNAL_API_TOKEN`, trừ health/readiness. Config
có token riêng từng principal. Docker không mount socket vào bất kỳ service nào.

## Schema và job lifecycle

Processing, ba agent và LangGraph dùng:

- `POST /v1/jobs`, header `Idempotency-Key` 64 hex; trả 202 với row đã lưu SQLite.
- `GET /v1/jobs/{key}` trả queued/running/completed/failed, identity, stage, progress,
  attempts, error_type và result khi hoàn tất. Input không được trả ở endpoint status.
- `/healthz` kiểm tra tiến trình; `/readyz` kiểm tra readiness và trạng thái cấu hình.

Payload luôn có `identity`: job_id, runtime_id, contract_id, trace_id (32 lowercase hex),
request_id. Worker dùng trace_id ổn định làm request_id của yêu cầu nghiệp vụ qua retry;
header X-Request-ID cũng được truyền. Tất cả content dùng JSON UTF-8, tối đa 40 MiB/request.
Mỗi response có Cache-Control: no-store. API lỗi không trả input/secret/exception message.

Job được commit trước ACK nhận; lease/heartbeat SQLite chống nhiều runner cùng claim.
Lease hết hạn cho phép tiếp tục sau crash. Cùng key/input trả kết quả đã lưu; key trùng
nhưng payload khác bị 409. Lỗi transport workflow có thể submit lại tối đa 3 attempt;
lỗi validation/model nghiệp vụ giữ failed và cần sửa cấu hình/input trước job mới.
Client HTTP retry có giới hạn cho 429/5xx/transport, polling trong deadline.

## Worker → Input Processing

Service URL: `http://input-processing:8000`.
Payload gồm identity, job (envelope preprocessing), context và file:
`{filename, data_base64, metadata}`. Bytes giải mã tối đa 20 MiB. Metadata gồm nguồn
S3/key/version/hash; worker không import Python module preprocessing.

Result gồm `contract`, `stats`, `warnings`. ContractInput JSON có contract_id, version,
chunks[{chunk_id,index,text,metadata}], expected_chunk_count và context. Metadata giữ
source spans, trang/element, heading, token estimate, hashes và thông tin file.
Context.preprocessing chứa thống kê, warnings và extraction manifest. Các key reserved
workflow/preprocessing/worker không được client tự đưa vào context đầu vào.

## Worker → LangGraph → Agents

Service graph: `http://langgraph:8000`. Payload: identity + contract nhận từ processing.
Worker chỉ submit/poll workflow, không gọi/điều phối từng agent. Graph giữ nguyên các node
và cạnh hiện có: Agent 1 từng chunk → aggregate đủ chunk → Agent 2 → Agent 3.

Agent URL: `http://agent-1:8000`, tương tự agent-2/3. RPC run gồm identity,
execution_revision, AgentRequest `{content,context,previous_results}`, policy và memory
snapshot. Trả AgentResult `{agent_id,text,usage,stop_reason}` cùng memory snapshot mới.
Đây là JSON transport; schema/luật nghiệp vụ AI vẫn TODO, không tự sinh thay người dùng.

`GET /v1/describe` trả agent_id/mode và hash cấu hình, không trả secret. `/v1/messages`
là API thuần dựng message để graph đo budget handoff bằng cùng instructions của agent.
RPC summarize dùng provider của agent đó; không gọi model từ LangGraph container.
Revision không khớp bị từ chối để tránh dùng cache cho cấu hình đã thay đổi.

LangGraph trả result đầy đủ ba stage. Worker lấy Agent 3 result → IPC progress process
→ S3 output → DynamoDB completed → ACK → DeleteMessage. Thiếu runtime_id tự sinh UUID4,
redelivery dùng lại runtime. Hash input/lease/sequence và S3 attempt key được giữ nguyên.

## Agent 2/3 → MCP

Service `http://mcp:8000/mcp`, MCP streamable HTTP (stateless, JSON response), Bearer `MCP_API_TOKEN`
riêng chỉ agent-2/3 có. Tool: `web_search`, `qdrant_search`, `postgres_query`, `s3_get_object`; connector
mở khi được gọi lần đầu. `/readyz` báo tool nào đã cấu hình. Chi tiết: [mcp.md](mcp.md).

## Monitoring và Config

Monitoring `POST /v1/events` nhận allowlist correlation/status/duration/retry/token usage;
không lưu input/output hợp đồng. GET /v1/events?trace_id=... và /v1/metrics có auth.
Client đẩy HTTP và log JSON stdout; collector hỏng không làm job thất bại. Collector
lưu SQLite/outbox và retry Langfuse độc lập. Langfuse không phải kênh trả kết quả worker.

Config `GET /v1/secrets/{alias}` nhận token riêng của service, ánh xạ principal theo
config/policy.json. Principal chỉ đọc alias được cấp. Client không được chỉ định ARN
bất kỳ. Xem [environment/secret management](environment.md).

## Phạm vi lưu và phục hồi

Volumes riêng cho processing, từng agent, graph/cache, monitoring và worker. SQLite
phù hợp local/EC2 một host; không dùng volume SQLite trên NFS hoặc coi đây là hệ thống
checkpoint đa host. Durable job và full-result/memory cache phục hồi sau crash; graph
traverses lại node nhưng dùng kết quả đã cache. Không đảm bảo exactly-once model call
nếu crash sau model response mà trước commit. Cần chính sách retention/backup volume
khi vận hành với hợp đồng thật; source project không tự xóa dữ liệu.
