# MCP tool gateway cho Agent 2/3 — ghi chú bàn giao (2026-11-07)

Đọc file này trước khi sửa tool bên ngoài của Agent 2/3. Plan nghiệp vụ (DB qua MCP trước, web search khi
thiếu bằng chứng) vẫn ở `agents/agent_2_risk_scoring/plan.md`, `agents/agent_3_recommendation/plan.md`.

## Kiến trúc

```text
agent-2 / agent-3 (BaseAgent.run_tool)
  → ToolRegistry handler (bind_mcp_tools lúc AgentRuntime khởi tạo, khi có MCP_URL)
  → integrations/external/mcp_client.py: MCP streamable HTTP, Bearer MCP_API_TOKEN
  → service `mcp` (mcp_server/service.py, image contract-risk/mcp:local, http://mcp:8000/mcp)
      ToolGateway: agent gọi tool nào thì mới tạo connector tool đó (lazy, cache theo process)
      ├─ web_search      → mcp_server/tavily_search.py (Tavily API, tavily-python, ĐÃ CHẠY THẬT)
      ├─ legal_search    → rag/retriever.py (RAG: hybrid + rerank + evidence, xem docs/rag.md)
      ├─ qdrant_search / qdrant_collection_info → mcp_server/qdrant_reader.py (qdrant-client, ĐÃ NỐI Qdrant Cloud)
      ├─ postgres_query  → integrations/database/postgres_client.py (base code, cần POSTGRES_DSN)
      ├─ supabase_select / supabase_list_tables → mcp_server/supabase_reader.py (supabase-py, CHỈ ĐỌC)
      ├─ s3_list_objects → mcp_server/s3_reader.py (CHỈ ĐỌC, bucket contract-risk-database-unique-…-an)
      └─ s3_get_object   → mcp_server/s3_reader.py (CHỈ ĐỌC, cùng connector)
  → mỗi lần gọi tool: emit("mcp", "tool.<name>", completed|failed, duration) → monitoring → Langfuse
```

- SDK `mcp` 1.30 chính thức: server `FastMCP(stateless_http=True, json_response=True)`, client
  `streamable_http_client` + `ClientSession`. Một kết nối MCP ngắn cho mỗi lần gọi tool.
- Web search không có image/container riêng: chạy trong service `mcp`. Qdrant/PostgreSQL không có
  container trong Compose; chỉ có connector chờ URL/DSN thật.
- Agent 1 không có tool MCP (`/readyz` agent-1: `mcp_tools: []`).
- Chưa có vòng lặp model tự chọn tool: tool được gọi tường minh qua `agent.run_tool(name, payload)`.
  Prompt/luật khi nào gọi tool vẫn TODO theo người dùng.

## Tool và tham số

| Tool | Tham số | Kết quả | Giới hạn |
| --- | --- | --- | --- |
| `web_search` | query (1–400 ký tự), max_results ≤20 | `{results:[{title,url,snippet,score}]}` | Tavily `/search`, `TAVILY_SEARCH_DEPTH` (basic = 1 credit/lần), `TAVILY_COUNTRY` (vietnam); thiếu `TAVILY_API_KEY` → báo lỗi cấu hình |
| `qdrant_search` | query (text, MCP tự embed bằng e5) hoặc vector (1024 số), collection (bỏ trống = QDRANT_COLLECTION), limit ≤50, query_filter (JSON filter Qdrant; mặc định current + in_force) | `{points:[{id,score,payload}]}` | chỉ đọc (query_points); vector tự gửi phải cùng model `intfloat/multilingual-e5-large` |
| `qdrant_collection_info` | collection (bỏ trống được) | `{collection,status,points_count,vectors,payload_indexes}` | chỉ đọc |
| `postgres_query` | sql (1 câu), params | `{rows, truncated}` | transaction read-only, statement_timeout 5s, ≤100 dòng, từ chối `;` giữa câu |
| `s3_list_objects` | prefix, bucket (bỏ trống = bucket duy nhất đã cấu hình), max_keys ≤1000, continuation_token | `{bucket,prefix,objects:[{key,size,last_modified}],truncated,next_token}` | chỉ đọc |
| `s3_get_object` | key, bucket (bỏ trống được) | `{bucket,key,text,truncated,content_type}`; file nhị phân (PDF/DOCX) → `text:null, binary:true` | đọc ≤1 MB, UTF-8 không cắt giữa ký tự |

Lỗi cấu hình/validate trả về agent dạng `McpToolError` có thông điệp rõ (vd `TODO: set QDRANT_URL ...`);
lỗi khác chỉ trả tên lớp lỗi, không trả nội dung/credential.

## Bảo mật

- `MCP_API_TOKEN` riêng, chỉ inject vào `mcp`, `agent-2`, `agent-3`. Token nội bộ (`INTERNAL_API_TOKEN`) bị 401.
- `CONFIG_TOKEN_MCP` + principal `mcp` trong `config/policy.json` (đang rỗng): có thể lấy `POSTGRES_DSN`,
  `QDRANT_API_KEY` từ Secrets Manager/SSM qua `MCP_SECRET_BINDINGS`.
- Log INFO của `httpx`/`urllib3` bị tắt. Truy vấn web gửi ra Tavily: agent chỉ gửi câu hỏi ngắn, không gửi nội dung hợp đồng.
- Khuyến nghị khi nối DB thật: role PostgreSQL chỉ có quyền SELECT trên bảng tham chiếu.
- Container `mcp` read-only, UID 10001, không publish port; cần internet ra ngoài cho web search.

## Đã kiểm chứng (2026-11-07)

- 123/123 unit test (gồm 8 test mới `tests/unit/test_mcp_gateway.py`: server MCP thật qua uvicorn,
  lazy open, token, bind chỉ Agent 2/3, connector Qdrant/PostgreSQL/S3/web search).
- 9/9 service healthy. Từ container thật: agent-2 và agent-3 `run_tool("web_search")` → MCP → kết quả web thật
  (thuvienphapluat.vn, hethongphapluat.com, icontract.com.vn...); `qdrant_search` báo thiếu `QDRANT_URL`;
  worker dùng token nội bộ bị 401; event `tool.web_search` có trong monitoring/Langfuse.
- E2E TXT 3 chunk có audit vẫn exit 0 sau khi đổi image agent.

## Làm tiếp

- Nối thật: đặt `QDRANT_URL`/`QDRANT_API_KEY`, `POSTGRES_DSN`, `MCP_S3_BUCKETS` trong `.env` (hoặc
  binding Config) rồi `docker compose up -d mcp`. Cần pipeline embedding cho Qdrant và schema bảng tham chiếu
  (`data_pipeline/`, `rag/` vẫn rỗng).
- Business tools cũ (`legal_search`, `market_search`, `legal_reference`, `clause_rewriter`, `risk_calculator`)
  vẫn TODO; có thể dựng chúng trên 4 tool MCP khi có luật nghiệp vụ.

## S3 tham chiếu: `contract-risk-database-unique-333596350642-ap-southeast-1-an` (2026-11-12)

- `.env`: `MCP_S3_BUCKETS=contract-risk-database-unique-333596350642-ap-southeast-1-an`, `AWS_REGION=ap-southeast-1`.
  Code ở `mcp_server/s3_reader.py` (đóng gói trong image `mcp`; đã bỏ `integrations/aws/s3_client.py`).
- **Chỉ xem/đọc, hai lớp khóa:**
  1. Trong code: hook botocore `before-call.s3` từ chối mọi thao tác ngoài ListObjectsV2/GetObject/HeadObject
     (PermissionError "read-only") kể cả khi credential có quyền ghi. Unit test Moto: PutObject/DeleteObject bị chặn, bucket không đổi.
  2. IAM: gắn `infrastructure/aws/mcp-s3-readonly-policy.json` (chỉ `s3:ListBucket` trên bucket, `s3:GetObject` trên `bucket/*`).
     Bucket mã hóa SSE-KMS cần thêm `kms:Decrypt` trên key đó.
- **Credential: chỉ dùng EC2 instance role**, không đặt access key trong `.env`. Gắn policy
  `mcp-s3-readonly-policy.json` vào role của instance (cùng các quyền SQS/DynamoDB/S3 của worker),
  IMDSv2 hop limit ≥ 2. Bootstrap xóa biến `AWS_*` rỗng; botocore bỏ qua key rỗng và lấy credential từ IMDS.
  `infrastructure/ec2/preflight.sh` (deploy.sh tự gọi khi APP_ENV=production) list 1 key của bucket từ container
  `mcp`; local không có credential nên báo `NoCredentialsError` (đã kiểm tra).
- Chưa đọc được bucket thật (local không có credential).
- **Luồng agent → S3 (2026-11-14):** data pipeline chỉ ghi file gốc vào `legal/<khóa>/<năm>/<số>-<năm>-<khóa>/source/v{n}_{sha8}_{file}`
  (Hiến pháp: `legal/hien-phap/<năm>/HP-<năm>/source/`). Agent: `qdrant_search` → payload `s3_prefix` →
  `s3_list_objects(prefix=s3_prefix + "/source/")` → key file gốc để trích dẫn. `s3_get_object` trên PDF/DOC trả
  `binary=True, text=None`; nội dung Điều đọc qua `qdrant_search`/`supabase_select` (`legal_articles`), không OCR trong MCP.

## Qdrant Cloud: collection `legal_documents` (2026-11-12)

- `.env` (gitignored): `QDRANT_URL=https://9c0cea78-…us-west-1-0.aws.cloud.qdrant.io`, `QDRANT_API_KEY`,
  `QDRANT_COLLECTION=legal_documents`. Không cần ghi cổng: qdrant-client mặc định 6333.
- Code: `mcp_server/qdrant_reader.py` (trong image mcp) dùng thư viện chính thức `qdrant-client==1.20.0`
  (khóa trong `infrastructure/requirements/mcp.txt`); đã bỏ `integrations/database/qdrant_client.py` (REST tự viết).
  Image mcp tăng 366 → 499 MB (grpcio/numpy/protobuf của qdrant-client).
- Collection: 3072 chiều, Cosine, payload index `contract` (keyword, is_tenant), strict mode (lọc trên trường chưa
  index bị từ chối) — lọc theo `{"must":[{"key":"contract","match":{"value":"..."}}]}`. Hiện 0 điểm.
- Đã xác minh thật: máy local và agent-2/3 → MCP → Qdrant Cloud: info green/3072/Cosine, search trả `[]` (collection rỗng);
  API key không xuất hiện trong log.
- Cần lưu ý: API key đang là quyền **manage** (`access: m` trong JWT) và đã được dán vào chat → nên tạo key
  **read-only** trong Qdrant Cloud cho MCP rồi thay `QDRANT_API_KEY`. Cluster ở us-west-1 còn EC2 ở ap-southeast-1:
  mỗi truy vấn đi xuyên Thái Bình Dương (độ trễ, egress). Chưa có model embedding 3072 chiều trong project
  (data_pipeline/ rag/ còn rỗng); agent phải tự có vector cùng model đã index.

## Supabase `lglozpndorcilcfqtgce` (2026-11-12)

- `.env` (gitignored): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY`, tùy chọn `SUPABASE_TABLES`
  (allowlist bảng). Compose chỉ cấp các biến này cho service `mcp`.
- Code: `mcp_server/supabase_reader.py` (image mcp) dùng thư viện chính thức `supabase==2.34.1` (mcp.txt).
  Tool `supabase_select(table, columns, filters {"col": "eq.value"}, order "col.desc", limit ≤100)` và
  `supabase_list_tables()`.
- **Chỉ đọc:** httpx event hook trong MCP từ chối mọi request khác GET/HEAD trước khi ra mạng (insert/update/delete →
  PermissionError, đã kiểm tra thật); PostgREST chạy GET trong transaction READ ONLY. Không có RPC (POST).
- Key: ưu tiên secret key (backend, liệt kê được schema, **bỏ qua RLS**); thiếu thì dùng publishable key (RLS áp dụng,
  không liệt kê được bảng → trả allowlist). Secret key tự nó có quyền ghi: quyền chỉ đọc nằm ở MCP, nên giữ key chỉ
  trong service mcp. Secret key đã được dán vào chat → nên tạo key mới trong Supabase API Keys và thay `.env`.
- Đã xác minh thật: schema `public` hiện 0 bảng, select bảng không tồn tại → PGRST205; agent-2/3 → MCP gọi được
  (lỗi trả về agent chỉ có tên lớp `APIError`). `@supabase/server`/skill JS và `SUPABASE_JWKS_URL` không dùng
  (MCP là Python, có token riêng).

## Tìm kiếm Qdrant bằng text (2026-11-14)

`qdrant_search(query="...")`: MCP embed câu hỏi bằng `intfloat/multilingual-e5-large` (cùng model pipeline, không API key), collection 1024 chiều. Mặc định lọc `current=true` + `status=in_force`; `query_filter={}` để tìm cả lịch sử. Model tải lần đầu vào volume `mcp-models` (`/models`).

## Web search: Tavily thay ddgs (2026-11-14, mới nhất)

- Đã xóa toàn bộ web search cũ: `integrations/external/search_client.py` (ddgs), `ddgs` khỏi `mcp.in`/`mcp.txt` và
  `.venv312`, biến `WEB_SEARCH_REGION`/`WEB_SEARCH_BACKEND`.
- Code mới nằm trong image mcp: `mcp_server/tavily_search.py` (`TavilySearch`, SDK chính thức `tavily-python==0.8.5`,
  khuyến nghị bởi tavily.com/agent-setup/SKILL.md cho code ứng dụng). Env chỉ inject cho `mcp`: `TAVILY_API_KEY`
  (root `.env`, gitignored), `TAVILY_SEARCH_DEPTH=basic`, `TAVILY_COUNTRY=vietnam`. `/readyz` báo `web_search` theo `TAVILY_API_KEY`.
- Đã kiểm chứng: 154/154 unit test; image mcp build lại, 9/9 healthy, không còn module ddgs trong image; agent-2 và agent-3 →
  MCP → Tavily trả kết quả thật (chinhphu.vn, baochinhphu.vn); log không chứa key.
- Chưa làm các bước cài đặt toàn máy trong SKILL.md (Tavily CLI `curl … | bash`, `npx skills add … --global`, OAuth login):
  đó là công cụ cho coding agent trên máy dev, không cần cho service.
- Key đã dán trong chat: nên tạo key mới trên app.tavily.com và thay trong `.env`.

## Tài liệu gốc S3 đối tác: `partner_document` (2026-11-17, mới nhất)

- Chỉ Agent 3 có tool này (`agents/agent_3_recommendation/agent.py`, `MCP_TOOLS`). Code: `mcp_server/partner_reader.py`
  (`PartnerDocuments`), gateway `FACTORIES["partner_document"]`, readiness theo `PARTNER_S3_BUCKET`.
- Tham số: `key` (bắt buộc, chỉ `quarantine/…` hoặc `clean/…`, cấm `..`/`//`), `offset`, `max_chars` (≤50.000).
  Trả `{bucket, key, content_type, extraction, page_count?, characters, offset, text, next_offset}`.
- Key lấy từ `context.source_document` do worker ghi khi đọc `request.json` (`workers/partner.py`):
  `{bucket, source_file_key, clean_file_key}`. Agent không tự ghép key.
- Bucket: compose đặt `PARTNER_S3_BUCKET=${WORKER_INPUT_BUCKET}` cho mcp. Client S3 chặn mọi thao tác ngoài
  `GetObject/HeadObject/ListObjectsV2` (cùng hook với `s3_reader`). IAM đối tác (07-worker-aws-integration §2) chỉ cho role
  `GetObject` trên `quarantine/*` và `results/*/request.json` — **không** đọc được `clean/*`. Agent 3 chạy trước khi worker
  xóa quarantine (sau completed) nên dùng `source_file_key`.
- Text UTF-8 giải mã trực tiếp; PDF/DOCX/ảnh gửi sang service input-processing (`INPUT_PROCESSING_URL`,
  `INTERNAL_API_TOKEN`) như một job idempotent `partner-doc-<sha256[:32]>` → cùng trích xuất/OCR như luồng hợp đồng,
  ghép chunk theo thứ tự (overlap 0). Cache 8 tài liệu theo (key, ETag) trong tiến trình mcp.
- Kiểm chứng: unit test Moto (phân trang, binary → input-processing, cache, key sai, chặn ghi); chạy thật trong stack thử:
  DOCX qua input-processing container trả đúng nguyên văn; container agent-3 lấy 10 spec tool từ MCP qua mạng
  (có `partner_document`, `legal_reference`). Chưa đọc bucket ContractGuard thật (local không có credential role).

## Ràng buộc partner_document theo job + định danh trong event (2026-11-17)

- Agent service đặt `JOB_CONTEXT` (identity của job + `partner_keys` = source/clean key trong `context.request`) cho mỗi
  lần chạy; `McpToolClient` gửi nó trong header `x-job-context`. Gateway đọc header qua `Context` của FastMCP:
  `partner_document` chỉ đọc key thuộc `partner_keys` (khác → `PermissionError`, kể cả khi IAM cho đọc); mọi event
  `mcp.tool.*` gắn `job_id`/`runtime_id`/`contract_id`/`trace_id`/`request_id`.
- `partner_document` trả tối đa 15.000 ký tự/lần để vừa giới hạn kết quả tool của agent (không bị cắt mất `next_offset`).
