# Tracing Langfuse

Tích hợp sử dụng [skill chính thức của Langfuse](https://github.com/langfuse/skills/tree/main/skills/langfuse)
và [hướng dẫn tracing](https://langfuse.com/docs/observability/best-practices), với Python SDK `>=5.1,<6`.
Ứng dụng gọi Bedrock trực tiếp qua Hermes provider nên instrument ở biên workflow,
agent và provider call. Không thêm LangChain callback ghi lặp generations hoặc tự thu toàn bộ graph state.

## Cấu hình và thời điểm gọi

`monitoring/config.py` đọc riêng biến `LANGFUSE_*` từ `.env` ở root project.
Biến môi trường được ưu tiên; bộ đọc không thay đổi `os.environ` hoặc tự tìm `.env`
ở thư mục cha. AWS/Bedrock vẫn cấu hình qua settings/environment riêng.

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=<điền riêng>
LANGFUSE_SECRET_KEY=<điền riêng>
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_CAPTURE_CONTENT=false
LANGFUSE_ENVIRONMENT=development
LANGFUSE_RELEASE=
```

Credential local đã được đặt trong `.env`, bị loại khỏi Git và Docker build context.
Template `.env.example` không chứa key. Khi deploy, truyền credential qua môi trường
hoặc secret manager của nền tảng; đặt environment/release tương ứng.

`ContractRiskPipeline` tạo observer khi khởi tạo. `run_contract()` mở trace trước khi
đọc cache/chạy graph và flush trong `finally`, kể cả khi lỗi. Một lần chạy lại tạo trace
mới; session ID là hash của job ID + contract ID + version, để xem lịch sử trong Sessions.
Cache hit toàn bộ chỉ có root trace, không tạo generation giả hoặc tính lại token.
Đóng `observer.shutdown()` khi tiến trình kết thúc, không đóng sau từng job trong worker.

Khi gọi agent riêng, inject observer vào `.run()`:

```python
from monitoring.langfuse import LangfuseObserver
from common.schemas.agent_messages import AgentRequest

observer = LangfuseObserver.from_env()
try:
    # agent đã được cấu hình Bedrock, prompt và instructions.
    with observer.attributes(session_id="job-id", tags=["contract-risk-management"]):
        with observer.span("classify-contract", kind="agent"):
            result = agent.run(AgentRequest("Nội dung hợp đồng"), observer=observer)
finally:
    observer.shutdown()
```

## Dữ liệu quan sát

```text
contract.workflow [chain]
├─ agent_1_classification.chunk [agent] × số chunk
│  ├─ memory.summarize [generation, khi cần]
│  └─ agent_1_classification [generation, nếu cache miss]
├─ aggregate_classification [span]
├─ agent_2_risk_scoring.stage [agent]
│  ├─ memory.summarize [generation, khi cần]
│  └─ agent_2_risk_scoring [generation, nếu cache miss]
└─ agent_3_recommendation.stage [agent]
   ├─ memory.summarize [generation, khi cần]
   └─ agent_3_recommendation [generation, nếu cache miss]
```

Mỗi call model ghi model ID, tham số inference, thời gian và token usage Bedrock trả về,
kể cả response bị từ chối vì không hoàn chỉnh. Không tự tạo usage khi provider không trả số liệu.
Cache-read/cache-write token được ghi riêng. Langfuse có thể tính chi phí nếu model ID khớp
bảng giá; inference-profile/model tùy chỉnh cần cấu hình giá phía Langfuse.
`run_tool(..., observer=observer)` ghi kiểu `tool` dưới span đang hoạt động. Chưa có vòng lặp
tự chọn tool, user ID hoặc scoring chất lượng vì các phần đó chưa được triển khai.

Mặc định chỉ ghi thống kê input/output (số chunk, số ký tự, trạng thái, cache hit),
metadata định danh job/contract/chunk và token usage. Không gửi văn bản hợp đồng,
prompt hoặc snapshot memory. Bật `LANGFUSE_CAPTURE_CONTENT=true` mới gửi messages
thực tế và output model/tool; lịch sử/summary đã đưa vào messages cũng xuất hiện.
Root và các bước workflow vẫn chỉ ghi thống kê để tránh lặp payload lớn.

Lớp export che các trường credential đã biết, giá trị key Langfuse và email. Đây không
phải bộ nhận diện mọi loại PII; cần mở rộng theo schema dữ liệu trước khi bật full capture
cho dữ liệu nhạy cảm. Lỗi chỉ ghi tên loại exception. Lỗi telemetry không thay thế lỗi
nghiệp vụ hoặc kích hoạt gọi lại model; cấu hình thiếu key/URL khi bật tracing báo lỗi rõ ràng.

## Xác minh

Unit test dùng observer giả hoặc SDK tắt tracing. Chạy `python -B -m scripts.verify_langfuse`
để gửi một trace giả lập qua luồng thật tới Langfuse; không cần cấu hình Bedrock/prompt nghiệp vụ.
Full capture chỉ bật cho fixture này, không sửa cấu hình ứng dụng. URL và trace ID lưu
tại `.cache/langfuse-smoke-result.json`.

Có thể mở URL đó hoặc dùng CLI chính thức (cần Node/npm và credential trong môi trường):

```sh
npx --yes langfuse-cli api observations list --trace-id <trace-id> \
  --fields core,basic,io,model,usage,metadata,trace_context,metrics --limit 100 --json
```

Kiểm tra đủ bốn chunk, agent types/parent spans, các lần nén memory, model và usage,
session/tags, input/output ở root và từng bước. Trong UI, dùng Traces để xem lần chạy,
Sessions để xem các lần tiếp tục cùng job và tag `synthetic` để tách smoke test.
Trace giả lập đã được gửi và đọc lại từ Cloud; kiểm tra này không xác minh quyền gọi
Bedrock hoặc chất lượng phân tích hợp đồng.
