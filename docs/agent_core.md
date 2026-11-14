# Core agent: memory 2 loại + prompt 3 loại (2026-11-14)

Áp dụng cho cả ba agent (`agents/base.py`). Mọi file prompt đang **để trống** để điền sau; `core.md` và `external.md`
rỗng thì agent báo `ConfigurationError: TODO: fill ...` và không gọi model (readiness not ready).

## Prompt (mỗi agent: `agents/<agent>/prompts/`)

| Loại | File | Bắt buộc | Vai trò |
| --- | --- | --- | --- |
| Xử lý trung tâm | `core.md` | có | cách xử lý hợp đồng để ra output đúng nhất |
| Xử lý ngoài luồng | `external.md` | có | khi nào dùng service bên ngoài (tool MCP: `legal_search`, `web_search`, S3/Supabase/Qdrant...) và cách dùng thông tin bổ sung qua context |
| Example | `examples/*.md` | không | mỗi file một ví dụ; file rỗng bị bỏ qua; nạp theo thứ tự tên file |

Thứ tự message gửi model: system `core` → system `external` → system `# Examples` (nếu có) → system bài học long term
đã duyệt (nếu có) → lịch sử short term của hợp đồng này (ContextEngine) → user (request/context JSON).
Đã xóa `prompts/system.md`, prompt tác vụ (`classify/evaluate/recommend.md`), `soul.md`, `constraints.md`,
`context/instructions.md`. `plan.md` giữ làm tài liệu quy trình, **không** nạp vào prompt.
Loader: `agents/hermes/config_loader.py` (`load_prompts`, `prompt_files`). Revision thực thi (cache/describe) băm
mọi file prompt + config.yaml + 2 prompt memory.

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
