# Luồng Agent 1 theo chunk — ghi chú bàn giao (2026-11-07)

Đọc file này trước khi sửa luồng Agent 1. Plan nghiệp vụ: `agents/agent_1_classification/plan.md`.
Prompt/schema/luật phân loại vẫn TODO theo người dùng; lần này chỉ đổi runtime/orchestration.

## Luồng hiện tại

```text
LangGraph (orchestration/workflow.py)
  classify_chunk(i) lặp tuần tự i = 0..N-1
    ├─ cache hit (cùng scope) → bỏ qua, không gọi model
    ├─ i = 0 → memory rỗng (contract mới không mang context cũ)
    ├─ i > 0 → khôi phục memory từ row cache của chunk i-1, bắt buộc scope khớp
    │          contract_id/contract_version/chunk_id/chunk_index; sai/thiếu → ValueError, không gọi model
    ├─ RPC agent-1 với memory snapshot → AgentResult + memory mới
    └─ ghi MỘT row: {scope, result, memory}  (atomic, resume không thấy result thiếu memory)
  aggregate
    ├─ đủ N chunk, scope từng chunk đúng contract → bản tổng hợp theo thứ tự
    ├─ cache.put("aggregate") + đọc lại so khớp (xác nhận đã xử lý xong contract)
    └─ cache.release_memory(key, "classification") → xóa snapshot Agent 1, giữ result + scope
  evaluate → Agent 2 nhận context.workflow.classification = bản tổng hợp
```

- Memory Agent 1 tồn tại suốt các chunk của **một** contract (key cache = job_id + contract_id + contract_version)
  và chỉ bị xóa sau khi bản tổng hợp đã lưu và đọc lại được. Lỗi giữa chừng → memory các chunk
  đã xử lý vẫn còn để resume (test `test_failure_resume_uses_durable_results_and_memory`).
- Agent container không giữ memory giữa các job: mỗi RPC dựng ContextEngine từ snapshot gửi kèm.
- Memory quá ngân sách → engine tóm tắt turn cũ (summary), bản đầy đủ vẫn trong cache.
- `release_memory` idempotent. Resume sau khi đã release: các chunk là cache hit nên không cần memory.

## File đã sửa

| File | Thay đổi |
| --- | --- |
| `orchestration/workflow.py` | scope trong row cache; kiểm scope khi cache hit và khi khôi phục memory; aggregate đọc lại rồi release memory |
| `orchestration/result_aggregator.py` | `chunk_scope()`; aggregate từ chối chunk sai scope |
| `orchestration/cache.py` | `ContractCache.release_memory(key, stage)` |
| `tests/unit/test_contract_workflow.py` | scope + memory released sau khi xong; memory còn khi chưa xong; từ chối memory contract khác; release idempotent |
| `tests/integration/handoff_audit.py` | Kiểm cache LangGraph thật: đủ chunk, đúng contract, không còn memory |
| `tests/integration/container_smoke.py` | Thêm repo root vào sys.path (probe import `workers`); collector cô lập tắt Langfuse; chấp nhận exit -15 của uvicorn ≥0.29 sau shutdown sạch; bỏ giả định Langfuse luôn tắt |

## Kiểm chứng

- 115/115 unit test (`python -B -m unittest discover -s tests/unit -p "test_*.py"` trong `.venv312`).
- 8 image rebuild, 8/8 healthy. E2E TXT 3 chunk có audit (Moto + model fixture) exit 0:
  cả 5 điểm bàn giao + "Agent 1 memory released", forwarding monitoring → worker, rerun, ACK, SIGTERM.
  Log `.cache/handoff-text.log`, exit `.cache/handoff-text-exit.txt`. Chưa chạy lại ca PNG/OCR.

## Lưu ý

- Row cache cũ (trước thay đổi) không có `scope`: job đang dở với cache cũ sẽ dừng với ValueError
  thay vì nối memory sai. Chạy lại bằng job_id mới. Không có job thật nào bị ảnh hưởng tại thời điểm sửa.
- Chưa dọn checkpoint toàn job, chưa dọn memory Agent 2/3 (plan Agent 2/3 vẫn là mục tiêu).
- E2E dùng `--env-file .env` nên event của job smoke được export lên Langfuse (environment=local)
  và forward vào inbox của worker Compose đang idle (inbox không được drain khi idle; khi bật worker
  thật các event này bị đánh dấu rejected vì job không đăng ký trên DynamoDB thật).
