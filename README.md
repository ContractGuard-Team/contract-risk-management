# Contract Risk Management — base code 3 agent

Luồng đã nối: `ClassificationAgent → RiskScoringAgent → RecommendationAgent`.
Mỗi agent gọi `HermesBedrockProvider → HermesModelAdapter → bộ chuyển đổi tin nhắn
trong third_party/hermes-agent → BedrockClient → boto3 bedrock-runtime.converse`.

Đây là nền gọi model bằng văn bản. Chưa khởi động vòng lặp `AIAgent` của Hermes,
chưa tự gọi tool, chưa có HTTP service/container, PostgreSQL, RAG hoặc luật chấm điểm.
Đã có LangGraph, memory qua ContextEngine Hermes, cache SQLite theo job và Langfuse.
Các tool có giao diện và handler có thể đăng ký; chưa đăng ký sẽ báo TODO rõ ràng.
Không sửa source Hermes gốc.

## Cài đặt và kiểm thử

Python >= 3.11 và < 3.15 (theo source Hermes), chạy tại root của source checkout:

```sh
python -m pip install -e .
python -B -m unittest discover -s tests/unit -p "test_*.py" -v
```

Giữ thư mục `third_party/hermes-agent` cạnh `hermes_integration/`. Phiên bản này
chạy từ source checkout/editable install; wheel riêng không đóng gói source Hermes.
Import hoặc tạo agent không gọi mạng, không tìm credential và không tạo AWS client.
Unit test dùng Bedrock giả và prompt tạm, không gọi AWS thật.
Môi trường đã chuẩn bị trong workspace này là `.venv312` (Python 3.12).
Trên Windows có thể chạy `.\.venv312\Scripts\python.exe` thay cho `python`.

## Các phần cố ý để trống

- `.env.example`: region, model/inference-profile ID và thông tin xác thực AWS.
  Export biến AWS/Bedrock vào môi trường chạy. Boto3 dùng credential chain,
  gồm AWS profile, environment hoặc IAM role; không điền secret vào source/YAML.
- Mỗi agent: `config.yaml`, `prompts/system.md`, prompt tác vụ (`classify.md`,
  `evaluate.md`, `recommend.md`), `soul.md`, `constraints.md`,
  `context/instructions.md`. Các prompt/instructions này vẫn để TODO và phải điền trước khi gọi model.
  `plan.md` của cả ba agent đã có quy trình; Agent 2/3 ưu tiên database qua MCP,
  chỉ search web qua MCP khi bằng chứng chưa đủ. MCP và cleanup trong plan chưa có code thực thi.
- `configs/memory.yaml`: ngân sách input và summary cho từng model.
- `agents/shared/prompts/compact_memory.md`: prompt tóm tắt memory/handoff.
  Bước tóm tắt đã có code, chỉ gọi khi cần; prompt nghiệp vụ vẫn để TODO.
- Langfuse đọc riêng các biến `LANGFUSE_*` từ `.env` tại root; biến môi trường được ưu tiên.
  `.env.example` để trống credential, `.env` không được commit. Workspace này đã cấu hình
  kết nối và bật tracing; nội dung hợp đồng mặc định không được gửi.
- Input/output nghiệp vụ, công thức điểm, retrieval, API ngoài và kiểm chứng bằng chứng:
  có điểm mở rộng, chưa tự đặt quy tắc.

Thứ tự cấu hình Bedrock: `settings` truyền trực tiếp → biến môi trường của agent
(`AGENT_1_*`, `AGENT_2_*`, `AGENT_3_*`) → biến môi trường chung → `config.yaml`.
Các giá trị inference để trống sẽ không được gửi. Chỉ điền tham số model hỗ trợ.

## Gọi riêng một agent

Sau khi điền cấu hình và các file Markdown bắt buộc:

```python
from agents.agent_1_classification.main import create_agent
from agents.agent_1_classification.schemas.input import ClassificationInput

agent = create_agent()
result = agent.classify(ClassificationInput(content="Nội dung hợp đồng", context={}))
print(result.text)
```

Agent 2 dùng `RiskScoringInput` và `.evaluate(request)`; Agent 3 dùng
`RecommendationInput` và `.recommend(request)`. Cả ba hỗ trợ `.run(request)`.
`main.py` cung cấp hàm Python `create_agent()`/`run()`; chưa khởi động server.

## Phân tích hợp đồng nhiều chunk qua LangGraph

Điền các cấu hình/prompt ở trên trước khi gọi thật:

```python
from orchestration.pipeline import ContractRiskPipeline
from common.schemas.contract import ContractInput, ContractChunk

contract = ContractInput(
    contract_id="contract-001",
    version="v1",
    chunks=[
        ContractChunk("chunk-0", 0, "Nội dung phần 1", {"page": 1}),
        ContractChunk("chunk-1", 1, "Nội dung phần 2", {"page": 2}),
    ],
    expected_chunk_count=2,
    context={},
)
pipeline = ContractRiskPipeline()
try:
    result = pipeline.run_contract(contract, job_id="analysis-001")
finally:
    pipeline.cache.close()
```

Luồng: Agent 1 lặp từng chunk → cache kết quả/memory → kiểm tra đủ chunk → tổng hợp
→ context Agent 2 → context Agent 3. Graph chạy cùng tiến trình.

Agent 1 trả bản tổng hợp có danh sách `chunks`; Agent 2/3 giữ text/usage/stop_reason.
Handoff nằm trong `request.context["workflow"]`, không nhân đôi trong `previous_results`.
Cache mặc định là `.cache/contracts.sqlite3`. Chạy lại cùng job/input/config sẽ dùng
kết quả đã lưu; input hoặc prompt thay đổi cần job ID mới.

`.run(text, context)` vẫn dùng được như trường hợp một chunk, tạo job ID mới mỗi lần.
Xem [chi tiết memory, cache, Langfuse và phục hồi](docs/agent_workflow.md).
Xem [cấu hình và kiểm tra trace Langfuse](docs/langfuse.md). Pipeline tự mở trace khi
`run_contract()` chạy, ghi từng agent/lần gọi model và flush sau khi thành công hoặc lỗi.
Gọi agent riêng cần truyền `observer`; ví dụ trong tài liệu Langfuse.

## Nối một tool sau này

```python
from agents.agent_2_risk_scoring.main import create_agent
from hermes_integration.tool_registry import ToolRegistry

def configured_risk_handler(payload: dict) -> dict:
    # TODO: Thay bằng luật chấm điểm đã được xác định.
    raise NotImplementedError("TODO: implement risk rules")

tools = ToolRegistry()
tools.register("risk_calculator", configured_risk_handler)
agent = create_agent(tools=tools)
# Gọi tường minh sau khi đã triển khai handler:
# result = agent.run_tool("risk_calculator", payload)
```

Tên tool nằm trong `tool_functions` của từng agent. Các hàm tool cũng có thể được
gọi trực tiếp với `handler=...`. Model chưa được cấp tool schema để tự chọn/gọi tool.

## Context và memory

`build_context()` sao chép input, context và kết quả trước; không truy xuất ngầm.
`ChunkMemory`, `EvidenceManager`, `RecommendationMemory` hỗ trợ `put`, `snapshot`,
`clear`; tạo instance riêng cho từng job. Truyền snapshot tường minh vào context:

```python
from agents.agent_1_classification.context.chunk_memory import ChunkMemory
from hermes_integration.memory_adapter import memory_context

memory = ChunkMemory()
memory.put("chunk-1", {"text": "Nội dung được giữ lại"})
context = memory_context(memory)
```

Các helper ở ví dụ trên vẫn dùng cho dữ liệu tường minh. Workflow mới tự tạo
`ContractContextEngine` riêng cho từng agent/job và lưu snapshot trong cache.
Không dùng MEMORY.md/USER.md toàn cục của Hermes.
`compact_context` và `EvidenceManager.validate` vẫn là hook cho quy tắc riêng;
việc nén memory trong workflow đi qua `hermes_integration/summarizer.py`.

Tham khảo phân công module trong [docs/folder-responsibilities.md](docs/folder-responsibilities.md).
