# Contract Risk Management — base code 3 agent

Luồng đã nối: `ClassificationAgent → RiskScoringAgent → RecommendationAgent`.
Mỗi agent gọi `HermesBedrockProvider → HermesModelAdapter → bộ chuyển đổi tin nhắn
trong third_party/hermes-agent → BedrockClient → boto3 bedrock-runtime.converse`.

Đây là nền gọi model bằng văn bản. Chưa khởi động vòng lặp `AIAgent` của Hermes,
chưa tự gọi tool, chưa có HTTP service/container, database, RAG hoặc luật chấm điểm.
Các tool có giao diện và handler có thể đăng ký; chưa đăng ký sẽ báo TODO rõ ràng.
Không sửa source Hermes gốc.

## Cài đặt và kiểm thử

Python >= 3.10, chạy tại root của source checkout:

```sh
python -m pip install -e .
python -B -m unittest discover -s tests/unit -p "test_*.py" -v
```

Giữ thư mục `third_party/hermes-agent` cạnh `hermes_integration/`. Phiên bản này
chạy từ source checkout/editable install; wheel riêng không đóng gói source Hermes.
Import hoặc tạo agent không gọi mạng, không tìm credential và không tạo AWS client.
Unit test dùng Bedrock giả và prompt tạm, không gọi AWS thật.

## Các phần cố ý để trống

- `.env.example`: region, model/inference-profile ID và thông tin xác thực AWS.
  Export vào môi trường chạy; code không tự đọc `.env`. Boto3 dùng credential chain,
  gồm AWS profile, environment hoặc IAM role; không điền secret vào source/YAML.
- Mỗi agent: `config.yaml`, `prompts/system.md`, prompt tác vụ (`classify.md`,
  `evaluate.md`, `recommend.md`), `soul.md`, `plan.md`, `constraints.md`,
  `context/instructions.md`. Các Markdown chỉ có comment TODO và phải điền trước khi gọi model.
- Input/output nghiệp vụ, công thức điểm, retrieval, API ngoài, kiểm chứng bằng chứng
  và chính sách rút gọn context: có điểm mở rộng, chưa tự đặt quy tắc.

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

## Gọi nối tiếp ba agent

```python
from orchestration.pipeline import ContractRiskPipeline

results = ContractRiskPipeline().run("Nội dung hợp đồng", context={})
```

Pipeline chạy cùng tiến trình, truyền kết quả trước qua `previous_results`, giữ
nguyên text/usage/stop_reason. Dừng ngay khi một bước lỗi; chưa retry workflow hay
checkpoint. Chưa parse JSON nghiệp vụ hoặc tạo điểm rủi ro giả.

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

Không có lưu hội thoại chung giữa các lần chạy. `compact_context` và
`EvidenceManager.validate` yêu cầu callback triển khai chính sách riêng;
chưa cấu hình sẽ báo `IntegrationNotConfigured`.

Tham khảo phân công module trong [docs/folder-responsibilities.md](docs/folder-responsibilities.md).
