# Ba agent: prompt, model, memory, tool, RAG

Quy định chung: **prompt** = giá trị cố định dev nhập từ file; **context** = giá trị động lúc chạy (request, kết quả agent trước,
kết quả tool). Code không viết sẵn câu hướng dẫn cho model.

| Agent | Việc | Output (`schemas/output.py`) |
| --- | --- | --- |
| 1 `agent_1_classification` | Tách, phân loại điều khoản, trích fact theo từng chunk | `ClassificationResult` |
| 2 `agent_2_risk_scoring` | Phán quyết R01–R20, findings, 20 check | `RiskScoringResult` |
| 3 `agent_3_recommendation` | Đề xuất cho mọi finding, đề xuất tổng thể | `RecommendationResult` |

## 1. Prompt

Mỗi agent ở `agents/<agent>/prompts/`. Loader: `agents/hermes/config_loader.py`.

| File | Bắt buộc | Vai trò |
| --- | --- | --- |
| `soul.md` | Có | Vai trò và cách làm việc |
| `core.md` | Có | Cách xử lý hợp đồng và ràng buộc output |
| `external.md` | Có | Khi nào dùng tool và context bổ sung |
| `rules/**` (`.md/.json/.txt`) | Không | File luật phải đối chiếu, nạp đệ quy |
| `examples/*.md` | Không | Mỗi file một ví dụ |
| `fix_format.md` | Khi có schema | Yêu cầu sửa output; code điền `{problems}` |
| `reevaluate.md` | Agent 2/3 | Yêu cầu đánh giá lại; code điền `{gaps}` |

Thứ tự gửi model: soul → core → external → `# Rules` → `# Output schema` → `# Examples` → bài học đã duyệt → memory ngắn hạn → request.
File bắt buộc rỗng → `ConfigurationError`, không gọi model. `plan.md` chỉ là tài liệu, không nạp.

- Nội dung lấy từ gói ContractGuard: Agent 1 có 10 hợp đồng mẫu; Agent 2 có `rules/risk_policy.json` (R01–R20, M01–M03) và 3 báo cáo mẫu.
- Prompt song ngữ Việt/Anh: giải thích theo `request.output_language`; trích dẫn, ID, enum giữ nguyên văn.
- **Danh mục chờ đối tác**: `rules/BR-RISK-006/`, `rules/fact_keys/` (Agent 1), `rules/BR-RISK-007/` (Agent 2). Khi chưa có:
  `clause_type=C99`, `fact_values={}`, cả 20 check là `not_applicable` (nghĩa là *chưa kiểm*). Không tự đặt nghĩa cho mã.

## 2. Model

Ba agent dùng Gemini qua REST (`integrations/external/gemini_client.py`, httpx, không SDK).

| Agent | Model | `reasoning_effort` | `max_completion_tokens` |
| --- | --- | --- | --- |
| 1 | `gemini-3.5-flash-lite` | medium | 32.000 |
| 2 | `gemini-3.5-flash-lite` | high | 64.000 |
| 3 | `gemini-3.5-flash-lite` | medium | 32.000 |

- Chọn model: `AGENT_n_MODEL` → `GEMINI_MODEL` → `agents/<agent>/config.yaml`. `MODEL_PROVIDER=gemini|fixture` (fixture chỉ khi `APP_ENV=local`).
- Key chỉ từ `GEMINI_API_KEY` trong root `.env`, không qua Secrets Manager, không ghi vào file tracked.
- Token thinking tính vào `max_completion_tokens`; câu trả lời bị cắt làm job lỗi. `temperature`/`top_p` để trống.
- Gemini 3 bắt buộc gửi lại `thoughtSignature` của mỗi lời gọi tool; client và `agents/base.py` giữ nó.
- Lỗi 429/5xx/mạng: thử lại 5 lần rồi `TransientModelError`. 400/401/403 hoặc `finishReason` khác `STOP` → `ModelResponseError`, không thử lại.

## 3. Luồng LangGraph

`orchestration/workflow.py`: `classify_chunk` (lặp) → `aggregate` → `evaluate` → `recommend`.

- **Agent 1**: từng chunk tuần tự, khôi phục memory từ chunk liền trước cùng hợp đồng. Một dòng cache = `{scope, result, memory}`;
  sai scope thì dừng trước khi gọi model. Đủ chunk → tổng hợp, lưu, đọc lại, rồi mới xóa memory Agent 1.
- **Agent 2** nhận `context.workflow.classification`; **Agent 3** nhận `context.workflow.risk_scoring`. Cả hai có `job_id` thật.
- Bàn giao quá ngân sách → bản tóm tắt kèm `ids` (mọi clause_id/finding_id). Chưa có cách tải lại bản đầy đủ.
- Kết quả cuối (`orchestration/extraction.py`) dựng và kiểm trước khi cache `complete`: trích dẫn phải nằm trong điều khoản, trang
  kẹp trong trang của chunk, finding hết bằng chứng bị bỏ, verdict trích sai không được tính điểm.
- **Điểm do code tính** (`orchestration/scoring.py`, công thức 4.5b). Domain: phap_ly→legal, tai_chinh→financial, van_hanh→operational,
  tuan_thu→compliance, ben_thu_ba→third_party, R17→data_privacy, R01/R02→legal; cybersecurity không có rule.

**Cache và resume** (`orchestration/cache.py`, `checkpoint.py`): khóa = job_id + runtime_id + contract_id + contract_version. Đổi
input, model, prompt hay cấu hình thì không dùng lại cache. Chạy lại bắt đầu từ chunk đầu tiên chưa có kết quả.

| Tầng retry | Số lần | Điều kiện |
| --- | --- | --- |
| Gemini client | 5 | 429, 5xx, lỗi mạng |
| HTTP giữa service | 3 | 429, 5xx, lỗi mạng |
| Job nội bộ | 3 | `RETRYABLE_JOB_ERRORS` |
| Node LangGraph | 3, chờ 5→60 giây | Lỗi tạm thời (`retry_policy.py`) |
| Message SQS | 3 | Mọi lỗi trừ lỗi đầu vào |

Lỗi validate, cấu hình, output model và `JobCancelled` không thử lại trong LangGraph.

## 4. Vòng lặp agent (`agents/base.py`)

1. Gọi model; model đòi tool thì chạy tool rồi gọi lại, tối đa 6 vòng. Hết lượt: yêu cầu trả lời với dữ liệu đã có.
2. Kiểm output bằng Pydantic + `check_output`; sai thì gửi lại lỗi, tối đa 2 lần.
   - Agent 1: điều khoản nguyên văn, trích dẫn và trang đúng chunk. Agent 2: clause_id có thật. Agent 3: đủ đề xuất cho mọi finding.
3. `evidence_gaps` không rỗng → tìm thêm và đánh giá lại, tối đa 2 lần. Hết lượt thì giữ gap, không bịa.

Lỗi tool trả về cho model như dữ liệu, không làm hỏng job. Kết quả tool quá 20.000 ký tự được rút ngắn mà vẫn là JSON hợp lệ. Ngân
sách token kiểm trước mỗi lần gọi; `guard()` dừng ngay khi job bị hủy.

## 5. Memory

Mỗi agent có memory riêng, không dùng chung.

| Loại | Phạm vi | Lưu ở | Xóa |
| --- | --- | --- | --- |
| Ngắn hạn | 1 hợp đồng, 1 agent | Snapshot ContextEngine trong cache LangGraph | Agent 1 sau tổng hợp; Agent 2/3 sau khi lưu kết quả cuối |
| Dài hạn | Nhiều hợp đồng, 1 agent | `/data/long_term_memory/<agent_id>.sqlite3` | Người duyệt |

- Ngân sách ở `configs/memory.yaml` (Agent 1: 300k, Agent 2/3: 900k, đơn vị byte ước lượng). Vượt thì tóm tắt bằng
  `memory/short_term_summary.md`; bản gốc vẫn ở cache.
- Dài hạn: sau mỗi job, `reflect` đề xuất bài học ở trạng thái `pending`. Prompt `long_term_reflection.md` đang trống nên bước này bị bỏ
  qua. Chỉ bài `approved` vào prompt (≤ 30 bài, 8.000 ký tự). Reflection lỗi không làm hỏng job.

```sh
docker compose exec agent-2 python -m agents.memory.long_term list --status pending
docker compose exec agent-2 python -m agents.memory.long_term approve 3 4
docker compose exec agent-2 python -m agents.memory.long_term add "Bài học do người viết"
```

## 6. Tool qua MCP

Agent 2/3 gọi tool qua service `mcp` (`mcp_server/`, SDK `mcp`, streamable HTTP, `MCP_API_TOKEN`). Connector chỉ mở khi được gọi.
Agent 1 không có tool. Ưu tiên kho luật trước, web sau.

| Tool | Nguồn | Ghi chú |
| --- | --- | --- |
| `legal_search` (Agent 3: `legal_reference`) | RAG | Bằng chứng pháp luật có hiệu lực, mục 7 |
| `qdrant_search`, `qdrant_collection_info` | Qdrant | Mặc định `current` + `in_force`; tự embed câu hỏi |
| `supabase_select`, `supabase_list_tables` | Supabase | Chỉ GET |
| `postgres_query` | PostgreSQL | Tùy chọn (`POSTGRES_DSN`); chỉ đọc, 5 giây, ≤ 100 dòng |
| `s3_list_objects`, `s3_get_object` | S3 kho luật | File nhị phân trả `binary=true`, không OCR |
| `web_search` | Tavily | Chỉ gửi câu hỏi ngắn, không gửi nội dung hợp đồng |
| `partner_document` | S3 ContractGuard | Chỉ Agent 3; đọc tài liệu gốc, ≤ 15.000 ký tự/lần (`offset`) |

- Mọi tool **chỉ đọc**, chặn ghi ngay trong code (hook botocore/httpx) ngoài quyền IAM.
- `partner_document` chỉ nhận key `quarantine/` hoặc `clean/` có trong `results/<job_id>/request.json` của chính job; PDF/DOCX/ảnh
  trích qua input-processing. IAM đối tác chỉ cho đọc `quarantine/*`.
- Lỗi cấu hình trả thông điệp rõ; lỗi khác chỉ trả tên lớp, không lộ nội dung hay credential.
- Kho luật dùng role EC2; key Supabase/Qdrant/Tavily chỉ cấp cho `mcp`.

## 7. RAG pháp luật

`rag/` chạy trong service `mcp`; agent không import.

1. `metadata_filter`: mặc định bản hiện hành của luật còn hiệu lực; `as_of=YYYY-MM-DD` lấy bản có hiệu lực ngày đó.
2. `hybrid_search`: vector `intfloat/multilingual-e5-large` (1024 chiều) + từ khóa BM25, trộn bằng RRF trong một lệnh Qdrant.
3. Gộp các phần của cùng Điều thành nguyên văn một bằng chứng.
4. `reranker`: `jinaai/jina-reranker-v2-base-multilingual` (CPU, `RAG_RERANK_MODEL` rỗng = tắt).
5. `evidence_formatter`: `evidence_id` (`số hiệu#Điều@ngày`), `citation`, nguyên văn (≤ 6.000 ký tự), hiệu lực, nguồn, cảnh báo.

Tham số: `query`, `limit` (≤ 20), `symbol` + `article_no`, `doc_class`, `as_of`, `include_inactive`. `found=0` nghĩa là kho không có,
lúc đó mới dùng `web_search`. Model đọc offline từ `models/`; RAM `mcp` khoảng 3,5 GB. Đổi tokenizer hoặc embedding phải index lại.
