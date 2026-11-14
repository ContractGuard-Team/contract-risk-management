# Contract Risk Management — base code 3 agent

Hệ thống hiện có 8 service Docker: worker, input-processing, agent-1/2/3,
langgraph, monitoring và config. Một `docker-compose.yml` ở root quản lý network,
volumes, healthcheck, restart và logging. Worker gọi preprocessing rồi LangGraph qua
HTTP; LangGraph điều phối ba agent. Xem [kiến trúc](docs/architecture.md).

Chạy local lần đầu: `python scripts/init_env.py`, `docker compose build`,
`docker compose up -d --wait`. File `.env` hiện tại đã được tạo ở chế độ local fixture,
worker idle để không gọi AWS thật. Hướng dẫn đầy đủ: [triển khai](docs/deployment_ec2.md)
và [môi trường/secret](docs/environment.md). Nhật ký thực hiện: [plan_work_note.md](plan_work_note.md).

Luồng đã nối: `ClassificationAgent → RiskScoringAgent → RecommendationAgent`.
Mỗi agent gọi `OpenAIChatClient` (integrations/external/openai_client.py) → OpenAI Chat Completions
(SDK `openai`); tin nhắn đã ở dạng chat OpenAI nên không cần bộ chuyển đổi. Bedrock đã bị gỡ (2026-11-14).

Đây là nền gọi model bằng văn bản. Chưa khởi động vòng lặp `AIAgent` của Hermes,
chưa tự gọi tool, chưa có PostgreSQL/RAG hoặc luật chấm điểm nghiệp vụ. HTTP service nội bộ đã có cho container.
Đã có LangGraph, memory qua ContextEngine Hermes, cache SQLite theo job và callback tiến độ nội bộ.
Tiền xử lý độc lập: normalize → nhận diện cấu trúc → chunk có nguồn.
HTTP adapter trả ContractInput cho worker gửi sang workflow. Xem [đánh giá code và cách dùng](docs/input_processing.md).
Các tool có giao diện và handler có thể đăng ký; chưa đăng ký sẽ báo TODO rõ ràng.
Không sửa source Hermes gốc.

## Cài đặt và kiểm thử

Python >= 3.11 và < 3.15 (theo source Hermes), chạy tại root của source checkout:

```sh
python -m pip install -e ".[test]"
python -B -m unittest discover -s tests/unit -p "test_*.py" -v
```

Source Hermes nằm trong `agents/hermes/hermes_agent`, code tích hợp ở `agents/hermes/`. Phiên bản này
chạy từ source checkout/editable install; wheel riêng không đóng gói source Hermes.
Import hoặc tạo agent không gọi mạng, không tìm credential và không tạo AWS client.
Unit test dùng client OpenAI giả và prompt tạm, không gọi OpenAI thật.
Môi trường đã chuẩn bị trong workspace này là `.venv312` (Python 3.12).
Trên Windows có thể chạy `.\.venv312\Scripts\python.exe` thay cho `python`.

## Các phần cố ý để trống

- `.env.example`: `OPENAI_API_KEY` (điền sau), `OPENAI_MODEL` và `AGENT_n_MODEL` tùy chọn. Key chỉ ở root `.env`
  (gitignored) hoặc Config Service (`AGENT_n_SECRET_BINDINGS`); không điền secret vào source/YAML.
- Mỗi agent: `config.yaml` và 3 loại prompt (để trống, điền sau; xem [docs/agent_core.md](docs/agent_core.md)):
  `prompts/core.md` (xử lý trung tâm), `prompts/external.md` (dùng service ngoài/context bổ sung),
  `prompts/examples/*.md` (ví dụ). core/external phải điền trước khi gọi model.
  `plan.md` của cả ba agent đã có quy trình; Agent 2/3 ưu tiên database qua MCP,
  chỉ search web qua MCP khi bằng chứng chưa đủ. MCP và cleanup trong plan chưa có code thực thi.
- `configs/memory.yaml`: ngân sách input và summary cho từng model.
- Memory 2 loại, riêng từng agent: short term (`agents/<agent>/memory/short_term_summary.md`, tóm tắt khi vượt
  ngân sách, xóa sau mỗi hợp đồng) và long term (`agents/<agent>/memory/long_term_reflection.md`, bài học chờ người
  duyệt, file SQLite riêng `/data/long_term_memory/<agent_id>.sqlite3`). Các prompt này để trống.
- Input/output nghiệp vụ, công thức điểm, retrieval, API ngoài và kiểm chứng bằng chứng:
  có điểm mở rộng, chưa tự đặt quy tắc.

Thứ tự chọn model OpenAI: `settings` truyền trực tiếp → `AGENT_1/2/3_MODEL` → `OPENAI_MODEL` → `model` trong
`config.yaml`. Key chỉ lấy từ `OPENAI_API_KEY`. inference_config: `max_completion_tokens`, `temperature`, `top_p`, `stop`.
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
Tiền xử lý chạy riêng qua `process_job(job, file=...)` hoặc CLI, trả JSON/chunk.
Gọi Python trực tiếp vẫn nhận `ContractInput`; trong Compose, preprocessing service
trả manifest này cho worker gửi tới LangGraph service. Xem [memory, cache và phục hồi](docs/agent_workflow.md).

## Nối một tool sau này

```python
from agents.agent_2_risk_scoring.main import create_agent
from agents.hermes.tool_registry import ToolRegistry

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
from agents.hermes.memory_adapter import memory_context

memory = ChunkMemory()
memory.put("chunk-1", {"text": "Nội dung được giữ lại"})
context = memory_context(memory)
```

Các helper ở ví dụ trên vẫn dùng cho dữ liệu tường minh. Workflow mới tự tạo
`ContractContextEngine` riêng cho từng agent/job và lưu snapshot trong cache.
Không dùng MEMORY.md/USER.md toàn cục của Hermes.
`compact_context` và `EvidenceManager.validate` vẫn là hook cho quy tắc riêng;
việc nén memory trong workflow đi qua `agents/hermes/summarizer.py`.

Tham khảo phân công module trong [docs/folder-responsibilities.md](docs/folder-responsibilities.md).

## Chạy hệ thống qua Docker Compose

```sh
# Chỉ chạy init_env.py nếu chưa có .env; script không ghi đè cấu hình đang có.
python scripts/init_env.py
docker compose build
docker compose up -d --wait
docker compose ps
docker compose logs --tail 100 worker langgraph monitoring
```

8 image dùng các target riêng trong `infrastructure/docker/Dockerfile`; dependency
khóa tại `infrastructure/requirements/`. Không có Compose hoặc .env riêng trong workers/.
Không public port host; các service gọi nhau theo tên trên network backend.

Mode local dùng fixture model tường minh và WORKER_ENABLED=false; health worker cho
biết đang idle. Để chạy hợp đồng thật, chuyển APP_ENV=production, MODEL_PROVIDER=openai, OPENAI_API_KEY,
điền cấu hình/prompt/memory và AWS rồi bật WORKER_ENABLED=true. Fixture không thể chạy
trong production. Các API liên container dùng token; Config Service có token/allowlist
riêng từng service. Langfuse có thể tắt hoặc chưa có key mà pipeline vẫn chạy.

Kiểm thử xuyên container (AWS và model giả lập, HTTP/LangGraph/Hermes thật):

```sh
docker build --target test -t contract-risk/test:local -f infrastructure/docker/Dockerfile .
docker run --rm --network contract-risk_backend --env-file .env contract-risk/test:local
```

Nếu đổi COMPOSE_PROJECT_NAME, đổi network tương ứng. Image test không nằm trong 8
service vận hành. Xem [giao thức](docs/container_interfaces.md), [kiểm thử](docs/testing.md),
[triển khai EC2](docs/deployment_ec2.md), [báo cáo bàn giao](docs/plan_work_report.md)
và [nhật ký tiến độ](plan_work_note.md). Xem [báo cáo tình trạng các luồng kết nối](docs/current_connections_report.md)
để biết thành phần nào đã nối, nhánh nào đang tắt và phần nào còn là khung.
