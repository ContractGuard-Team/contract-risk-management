# Core agent: memory 2 loại + prompt (2026-11-14, cập nhật 2026-11-17)

Áp dụng cho cả ba agent (`agents/base.py`). `soul.md`, `core.md`, `external.md` rỗng thì agent báo
`ConfigurationError: TODO: fill ...` và không gọi model (readiness not ready).

## Prompt (mỗi agent: `agents/<agent>/prompts/`) — đã điền ngày 2026-11-17

| Loại | File | Bắt buộc | Vai trò |
| --- | --- | --- | --- |
| Soul | `soul.md` | có | vai trò + cách làm việc của agent (A1 phân điều khoản, A2 đánh giá rủi ro, A3 đề xuất sửa đổi) |
| Xử lý hợp đồng (trung tâm) | `core.md` | có | cách xử lý hợp đồng + ràng buộc output theo schema trong source code |
| Prompt service (ngoài luồng) | `external.md` | có | A1: vòng lặp chunk với cache; A2/A3: dùng tool MCP và đánh giá lại khi thiếu bằng chứng |
| File luật | `rules/*.md|*.json` | không | file agent phải đối chiếu; nạp thành system `# Rules: <tên>` |
| Schema output | `../schemas/output.schema.json` | không | **người dùng thiết kế sau**; khi có file thì nạp thành system `# Output schema` |
| Example | `examples/*.md` | không | mỗi file một ví dụ; file rỗng bị bỏ qua; nạp theo thứ tự tên file |
| Yêu cầu sửa output | `fix_format.md` | khi có schema output (cả 3 agent) | gửi lại model khi sai schema/kiểm chéo; code điền `{problems}` |
| Yêu cầu đánh giá lại | `reevaluate.md` | khi có tool (Agent 2/3) | gửi khi còn `evidence_gaps`; code điền `{gaps}` |

Thứ tự message gửi model: system `soul` → `core` → `external` → `# Rules: ...` → `# Output schema` (nếu có) →
`# Examples` (nếu có) → bài học long term đã duyệt (nếu có) → lịch sử short term (ContextEngine) → user (request JSON).

Nguồn nội dung (gói `ContractGuard_4_2-4_5_Package.zip`):
- A1 `core.md` = phần "Áp dụng trong hệ thống" + nguyên văn `prompts/Extraction_Agent/01,02` (heading hạ cấp). Hệ thống
  không có Bước 4.3 nên A1 tự tách điều khoản + gán category; `clause_id` = `<contract_id>-C<4 số>`.
  `examples/` = 10 hợp đồng từ `json/` (HD-006/022/035/036/058/059/093/094/095/097), phủ đủ 11 category, mỗi mục ≤2
  điều khoản rút gọn 220 ký tự; chỉ tham khảo cách tách, không phải định dạng output.
- A2 `core.md` = phần "Áp dụng trong hệ thống" + nguyên văn `prompts/Risk_Agent/01,02`. `rules/risk_policy.json` =
  `risk/risk_policy.json` nguyên bản (danh mục R01–R20, M01–M03): tạo candidate, chấm điểm, rồi đối chiếu để xác định loại
  rủi ro. `examples/` = 3 báo cáo trong `risk/risk_rule_report.json` (HD-094 high, HD-072 medium, HD-002 low; ≤10 hit).
  (`risk_rule_report.json` là kết quả chấm 100 hợp đồng mẫu, không phải danh mục luật — người dùng chọn dùng policy làm file luật.)
- A3 viết mới theo yêu cầu: kết quả Agent 2 + tài liệu gốc S3 đối tác (`partner_document`) + Qdrant/Supabase.
- Dung lượng system prompt: A1 ≈ 60 KB, A2 ≈ 55 KB, A3 ≈ 9 KB → `configs/memory.yaml` (max_input_tokens, còn TODO) phải
  tính phần này.

`plan.md` giữ làm tài liệu quy trình, **không** nạp vào prompt. Loader: `agents/hermes/config_loader.py`
(`load_prompts`, `prompt_files`, `REQUIRED`). Revision thực thi (cache/describe) băm mọi file prompt/rules/schema +
config.yaml + 2 prompt memory.

## Schema nghiệp vụ của 3 agent (2026-11-17)

Pydantic trong `agents/<agent>/schemas/` (dùng lại khối chung `common/schemas/business/shared.py`, nên tên trường trùng
kết quả trả ContractGuard):

| Agent | Đầu vào (`input.py`, kiểm khi build_context) | Đầu ra (`output.py`, `text` của AgentResult) | Đi vào AnalysisResult |
| --- | --- | --- | --- |
| 1 | `ChunkContext`: request + workflow chunk (chunk_id, index, count, source_metadata) | `ClassificationResult` mỗi chunk: chunk_id, language, clauses (ChunkClause: clause_id, continuation, complete, number, heading, text, category 11 loại, clause_type, page_start/end, facts), metadata, fact_values, injection_warnings, corrections | clauses, metadata, facts, injection_warnings, document.language/complete |
| 2 | `RiskScoringContext`: request + workflow.classification | `RiskScoringResult`: clause_verdicts (R01–R20), findings (RiskFinding), checks (đúng 20 mã), evidence_gaps | findings, checks; verdict → scoring 4.5b bằng code |
| 3 | `RecommendationContext`: request + workflow.risk_scoring | `RecommendationResult`: recommendations (1/finding: recommendation, proposed_text, priority, evidence, references), overall_recommendation, evidence_gaps | findings[].recommendation, references, overall_recommendation |

- Agent khai báo `output_model`; `BaseAgent` gửi JSON Schema sinh từ model (`model_json_schema()`) vào system
  `# Output schema`, kiểm câu trả lời bằng model + `check_output` (Agent 1 đúng chunk_id/không trùng mã; Agent 2
  clause_id phải có trong kết quả Agent 1; Agent 3 đủ đề xuất cho mọi finding). Sai → gửi lại lỗi, tối đa
  `MAX_FORMAT_RETRIES=2`, rồi `ModelResponseError`. Revision cache/describe gồm schema của model.
- `context.request` = toàn bộ request.json ContractGuard (không có với job nội bộ). Prompt 3 agent đọc từ đây.
- Điểm KHÔNG do model tính: `orchestration/scoring.py` (4.5b, `risk_policy.json`; ánh xạ domain cố định
  phap_ly→legal, tai_chinh→financial, van_hanh→operational, tuan_thu→compliance, ben_thu_ba→third_party, R17→data_privacy,
  R01/R02→legal; cybersecurity không có rule → not applicable). Khớp báo cáo HD-001 của đối tác (23, medium).
- Danh mục chờ đối tác: Agent 1 `prompts/rules/BR-RISK-006/`, `prompts/rules/fact_keys/`; Agent 2
  `prompts/rules/BR-RISK-007/` (.md/.json/.txt, nạp đệ quy). Chưa có → C99, `fact_values={}`, 20 check `not_applicable`.
- Prompt nén memory `memory/short_term_summary.md` đã điền cho cả 3 agent; `long_term_reflection.md` vẫn trống.
- Fixture (`MODEL_PROVIDER=fixture`) trả output tối thiểu đúng model từng agent → luồng chạy tới AnalysisResult.

## Vòng lặp gọi tool + đánh giá lại khi thiếu bằng chứng (`agents/base.py:run`)

- Tool đưa cho model = các tool MCP đã bind (`bind_mcp_tools`); spec (tên, mô tả, JSON schema tham số) lấy từ
  `list_tools` của MCP gateway ở lần gọi model đầu tiên (`ToolRegistry.spec_loader`), alias đổi tên (`legal_reference`).
  Agent 1 không có tool → gọi model một lần như cũ (không gửi tham số `tools`).
- OpenAI Chat Completions function calling: `finish_reason="tool_calls"` → chạy từng tool (`run_tool`, span `tool.*`) →
  thêm message `tool` → gọi lại. Tối đa `MAX_TOOL_ROUNDS=6` vòng mỗi lượt đánh giá; hết thì gọi với `tool_choice="none"`.
  Lỗi tool (chưa cấu hình, sai tham số, upstream lỗi) trả về cho model dưới dạng `{"error", "message"}`, không làm hỏng job.
  Kết quả tool cắt ở 20.000 ký tự (dùng `offset` của tool để đọc tiếp).
- Đánh giá lại: nếu câu trả lời cuối là JSON có khóa gốc `evidence_gaps` không rỗng → thêm yêu cầu tìm căn cứ cho đúng các
  điểm đó và trả lại toàn bộ kết quả; tối đa `MAX_REEVALUATIONS=2` lần (mỗi lần có lại 6 vòng tool). Hết lượt thì giữ
  nguyên gap (không bịa). **Schema output của Agent 2/3 phải có khóa gốc `evidence_gaps`** để cơ chế này hoạt động.
- Usage cộng dồn mọi lần gọi model; memory short term chỉ ghi câu trả lời cuối (không ghi transcript tool).

## Memory — riêng cho từng agent, không có memory dùng chung

Mỗi agent có thư mục memory riêng (prompt) và kho riêng (dữ liệu):

```text
agents/agent_1_classification/memory/short_term_summary.md     agents/agent_2_risk_scoring/memory/...   agents/agent_3_recommendation/memory/...
agents/agent_1_classification/memory/long_term_reflection.md   (mỗi agent 2 file, đang để trống)
container agent-1: /data/long_term_memory/agent_1_classification.sqlite3   (volume agent-1-data)
container agent-2: /data/long_term_memory/agent_2_risk_scoring.sqlite3     (volume agent-2-data)
container agent-3: /data/long_term_memory/agent_3_recommendation.sqlite3   (volume agent-3-data)
```

| Loại | Phạm vi | Lưu ở | Khi nào xóa | Code |
| --- | --- | --- | --- | --- |
| Short term | 1 lần xử lý 1 hợp đồng, 1 agent | snapshot ContextEngine riêng của agent (row cache theo agent/stage trong cache job của langgraph); agent chỉ nhận snapshot của chính nó | Agent 1 sau aggregate; Agent 2/3 sau khi lưu kết quả cuối; state LangGraph sau mỗi lần chạy. Kết quả giữ, chỉ xóa memory | `agents/memory/short_term.py`, `agents/hermes/context_engine.py`, prompt nén `agents/<agent>/memory/short_term_summary.md` |
| Long term | nhiều hợp đồng, 1 agent | file SQLite riêng `<LONG_TERM_MEMORY_DIR>/<agent_id>.sqlite3` (`LONG_TERM_MEMORY_DIR=/data/long_term_memory` trên volume của agent) | không tự xóa; người duyệt/loại | `agents/memory/long_term.py`, prompt rút kinh nghiệm `agents/<agent>/memory/long_term_reflection.md` |

Agent không đọc được bài học hay short-term memory của agent khác; kể cả khi chạy chung tiến trình (CLI local) mỗi
agent vẫn mở file riêng. Thư mục `agents/shared/prompts/` cũ đã bỏ.

### Vòng đời bài học (long term)

1. Hợp đồng xử lý xong và kết quả cuối đã lưu → `ContractRiskPipeline._learn` gọi `reflect` của từng agent
   (container agent, operation `reflect`). Input cho model: `memory/long_term_reflection.md` của chính agent đó + `{agent_id, contract, output,
   approved_lessons}`. Model trả `{"lessons": [...]}` hoặc `[...]` (≤5 bài, ≤1000 ký tự/bài).
2. Bài học lưu trạng thái `pending` (trùng nội dung với bài đã có — kể cả đã loại — bị bỏ).
3. Người duyệt trong container agent:
   ```sh
   docker compose exec agent-2 python -m agents.memory.long_term list --status pending
   docker compose exec agent-2 python -m agents.memory.long_term approve 3 4
   docker compose exec agent-2 python -m agents.memory.long_term reject 5
   docker compose exec agent-2 python -m agents.memory.long_term add "Bài học do người viết"   # vào thẳng approved
   ```
4. Chỉ bài `approved` vào prompt các lần sau: tối đa 30 bài mới nhất, 8000 ký tự.

An toàn: reflection là best effort — prompt rỗng, agent chưa ready, lỗi model đều chỉ bị ghi `skipped`, không làm hỏng
job đã hoàn tất. Bài học không nằm trong revision (duyệt bài mới không làm job đang chạy thất bại); bài được duyệt giữa
chừng có hiệu lực từ lần gọi model tiếp theo (shortcut). SQLite theo host: mất nếu xóa volume `agent-n-data`; nhiều host
EC2 cần chuyển sang kho dùng chung.

## Kiểm chứng

- `tests/unit/test_agent_memory.py` (7 test): duyệt/loại/trùng, ngân sách prompt, parse output, CLI, examples + bài học là
  system message riêng (pending không lộ), reflection sau job → pending → duyệt → có trong prompt hợp đồng sau, prompt
  rỗng/lỗi không làm hỏng job; mỗi agent 1 file riêng + prompt memory trong thư mục của nó, agent 2 không thấy bài
  của agent 1. Tổng 170/170 unit test.
- Image agent-1/2/3 + langgraph build lại, 9/9 healthy; agent-1 tạo `/data/long_term_memory/agent_1_classification.sqlite3`,
  agent-2 tạo `.../agent_2_risk_scoring.sqlite3` trên volume riêng (rỗng).
