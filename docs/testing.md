# Kiểm thử ba agent, workflow và memory

Chạy tại root repository bằng Python >=3.11,<3.15 với dependency trong `pyproject.toml`:

```sh
python -B -m unittest discover -s tests/unit -p "test_*.py" -v
```

`tests/unit/test_agent_base.py` kiểm tra luồng ba agent qua bộ chuyển đổi Hermes thật
và Bedrock client giả, chuyển context/kết quả, cấu hình từng agent, prompt chưa điền,
response lỗi/không hoàn chỉnh, dừng workflow khi lỗi, handler tool và memory theo job.
Prompt thử nghiệm được tạo trong thư mục tạm; các file prompt của dự án vẫn là TODO.

Không cần credential, không gọi AWS hoặc API ngoài. Bộ kiểm thử chưa xác minh IAM,
quyền truy cập model, chất lượng phân tích hợp đồng, HTTP/container hoặc hệ thống end-to-end.
Các notebook hiện vẫn là khung để triển khai kiểm thử sau.

`tests/unit/test_contract_workflow.py` chạy LangGraph và ContextEngine Hermes thật:
kiểm tra 40 chunk vượt recursion limit mặc định, context nối tiếp, tổng hợp đủ chunk,
handoff Agent 1 → 2 → 3, giới hạn request, summary nhiều tầng, lỗi/khôi phục từ SQLite,
cache hit và xung đột input/prompt, cô lập hợp đồng, Langfuse lỗi/không cấu hình.
Langfuse SDK thật được kiểm tra với tracing tắt; các kiểm tra event dùng client giả.

Môi trường workspace đã tạo: `.venv312/Scripts/python.exe` (Python 3.12).
Lần xác minh ngày 2026-11-03 đạt 38/38 unit test.
`tests/unit/test_langfuse_config.py` kiểm tra cấu hình `.env`/environment, che key/email
ở lớp export OTel, token usage kể cả response không hoàn chỉnh, và trace/session khi chạy lại.
Unit test không gọi Bedrock hoặc gửi trace tới server thật, kể cả khi `.env` local bật Langfuse.
Trong sandbox Windows hiện tại, Python 3.12 bị từ chối ghi vào temporary directory;
lần kiểm thử xác minh được chạy ngoài sandbox sau khi được cấp quyền.

## Kiểm tra kết nối Langfuse thật

```sh
python -B -m scripts.verify_langfuse
```

Script cần credential Langfuse đã cấu hình; chạy LangGraph, Hermes adapter và memory
với bốn chunk giả lập, prompt tạm và Bedrock client giả. Chỉ gửi dữ liệu giả lập tới
Langfuse, không gọi AWS. Có các lần nén memory để kiểm tra generation tương ứng.
Trace mang tag `synthetic`/`smoke-test`, model `synthetic-bedrock-no-network`, metadata
`usage_is_simulated=true`; số token thử nghiệm không phản ánh chi phí model thật.
Script flush/shutdown và in URL trace; báo cáo lưu trong `.cache/langfuse-smoke-result.json`.
Xem [cách đọc lại và kiểm tra trace](langfuse.md).

Lần smoke test ngày 2026-11-03 đã gửi và đọc lại bằng CLI chính thức: 18 observation,
gồm sáu bước agent (bốn chunk Agent 1, Agent 2, Agent 3), mười generation giả lập
(gồm nén memory), một aggregate và một root. Đã kiểm tra parent/type, input/output,
model/usage, session, tags và environment.
[Trace đã xác minh](https://cloud.langfuse.com/project/cmw0e1gby01m9ad0et1wb1m17/traces/76bfd6795c2a45d6b78e77c787338223).
