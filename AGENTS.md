# Hướng dẫn triển khai Contract Risk Management AI

## Tài liệu tham chiếu

- Đọc `docs/folder-responsibilities.md` trước khi triển khai hoặc thay đổi kiến trúc. Đây là bản mô tả chức năng folder/file do người dùng cung cấp, được lưu nguyên văn.
- Dùng tài liệu đó làm định hướng trách nhiệm, input/output và chiều phụ thuộc của module. Đây là thiết kế mục tiêu, không phải xác nhận các chức năng đã được triển khai.
- Yêu cầu mới của người dùng được ưu tiên. Kiểm tra code thực tế trước khi kết luận một chức năng đã có; khi thay đổi phân công module, cập nhật tài liệu liên quan.

## Trách nhiệm các folder

| Folder | Phạm vi |
| --- | --- |
| `third_party/hermes-agent/` | Source Hermes gốc. Ưu tiên adapter/extension, tránh sửa trực tiếp source upstream. |
| `apps/api/` | FastAPI, xác thực, phân quyền, validation HTTP, upload, tạo job và truy vấn trạng thái/kết quả. Không chờ cả workflow AI trong request dài. |
| `input_processing/` | Xử lý hợp đồng người dùng: lấy S3, kiểm tra file, PDF/DOCX/OCR, làm sạch, bảo toàn cấu trúc và vị trí nguồn, chia chunk, đếm token. |
| `agents/` | Logic AI: Agent 1 phân loại/trích xuất; Agent 2 đánh giá và chấm điểm rủi ro có bằng chứng; Agent 3 đề xuất giảm thiểu/chỉnh sửa điều khoản. |
| `hermes_integration/` | Adapter runtime, model provider, cấu hình, tools và memory giữa ứng dụng với Hermes/Bedrock. Kiểm tra giao diện phiên bản Hermes thực tế trước khi nối runtime. |
| `orchestration/` | Workflow, state, chuyển context, gọi agent, retry nghiệp vụ, tổng hợp kết quả và checkpoint khi triển khai. |
| `workers/` | Nhận SQS, dispatch job, gia hạn visibility, xử lý message trùng; chỉ xóa message sau khi lưu kết quả thành công. |
| `data_pipeline/` | Thu thập dữ liệu tham chiếu luật/thị trường/doanh nghiệp; chuẩn hóa, kiểm chứng, loại trùng, quản lý phiên bản, định tuyến và ghi PostgreSQL/Qdrant/S3. |
| `rag/` | Logic truy xuất, lọc metadata, hybrid search, rerank và định dạng bằng chứng cho Agent 2/3. Qdrant là kho vector. |
| `integrations/` | Client AWS/database/API/search/MCP, kết nối, xác thực và retry kỹ thuật; không chứa nghiệp vụ phân loại hoặc chấm điểm. |
| `database/` | Session, models, repositories và migrations PostgreSQL; truy vấn nghiệp vụ tách khỏi client kết nối. |
| `common/` | Settings, exceptions, ports và schema dùng chung giữa API, worker và agent; tránh sao chép các giao diện chung. |
| `infrastructure/` | Docker, EC2 và cấu hình hạ tầng AWS; không chứa logic phân tích hợp đồng. |
| `monitoring/` | Logs, metrics, traces, tích hợp Langfuse và cảnh báo khi triển khai; liên kết luồng bằng `job_id`/`trace_id`. |
| `tests/` | Unit, integration, end-to-end, tải và đánh giá chất lượng agent, gồm tính nhất quán và truy xuất bằng chứng. |
| `configs/` | Cấu hình ứng dụng, AWS, model, agent, database, logging; không lưu secret. Nội suy biến môi trường cần được triển khai rõ ràng. |
| `scripts/` | Công cụ setup, khởi tạo dữ liệu, ingestion thủ công và chạy local; tác vụ liên tục thuộc worker/scheduler. |
| `docs/` | Tài liệu kiến trúc, giao diện container, kết nối AWS, triển khai và kiểm thử. |

## Ranh giới và luồng thiết kế

- Luồng hợp đồng: API → lưu job/gửi SQS → worker → orchestration → input processing → Agent 1 → Agent 2 → Agent 3 → tổng hợp/lưu kết quả → API truy vấn kết quả.
- Luồng kiến thức riêng: sources → processing → router → ingestion → PostgreSQL/Qdrant/S3. Không gộp xử lý hợp đồng người dùng vào `data_pipeline/`.
- Agent gọi tool nghiệp vụ; tool tìm kiếm dùng `rag/`; client dịch vụ nằm ở `integrations/`. Logic workflow không đặt trong HTTP routes hoặc client AWS.
- Ba agent chạy container riêng phải giao tiếp qua service hoặc queue; import object Python chỉ phù hợp khi chạy cùng tiến trình.
- Bảo toàn nguồn trích dẫn, vị trí điều khoản và metadata của bằng chứng. Dữ liệu tham chiếu cần provenance, thời điểm thu thập, ngày hiệu lực và phiên bản.
- Các file prompt, `soul.md`, `plan.md`, constraints và context instructions chỉ có tác dụng khi code chủ động nạp; không giả định Hermes tự đọc chúng.

## Đối chiếu tài liệu với repo tại thời điểm ghi nhớ (2026-11-03)

- Repo có 18 folder cấp cao nhất ngoài `.git`, gồm `common/` và `docs/`; con số 16/17 trong bản mô tả không phải số lượng chuẩn để kiểm tra repo.
- Dockerfile của cả API, worker và ba agent hiện nằm tại `infrastructure/docker/`; Compose nằm ở root (`compose.yaml`, `compose.ec2.yaml`). Không tự di chuyển hoặc tạo bản trùng theo vị trí minh họa trong tài liệu.
- `infrastructure/lambda/`, `infrastructure/aws/`, `infrastructure/ecs/`, `infrastructure/ecr/`, `data_pipeline/scheduler/`, `orchestration/checkpoint.py` và `monitoring/alerts.py` là các phần dự kiến/chưa có. ECS/Fargate là tùy chọn khi thay đổi nền tảng triển khai, không phải yêu cầu đã chốt.
- LangGraph đã được triển khai trong `orchestration/workflow.py`; xem `docs/agent_workflow.md`. Agent 1 xử lý các chunk tuần tự, cache đầy đủ trước khi tổng hợp và chuyển context sang Agent 2; Agent 2 chuyển output vào context Agent 3.
- Base code hiện có: pipeline cùng tiến trình gọi ba agent, cấu hình riêng từng agent, nạp các Markdown bắt buộc, ghép context, model provider và bộ chuyển đổi Bedrock từ Hermes. Tool có giao diện/callback đăng ký tường minh; memory lưu theo instance của job. Chưa có vòng lặp runtime tự gọi tool, retrieval hoặc schema nghiệp vụ. Prompt, API/model config và chính sách riêng vẫn để TODO theo yêu cầu người dùng.
- Memory workflow dùng extension ContextEngine của Hermes, riêng từng agent/job, snapshot lưu cùng result trong SQLite. Không dùng memory cá nhân toàn cục của Hermes. Giữ nguyên raw results khi nén context; chưa được tự viết prompt hay luật nghiệp vụ thay người dùng.
- Langfuse dùng SDK 5 (`monitoring/`), đọc biến `LANGFUSE_*` từ `.env` tại root với environment override. File mẫu để trống credential và tắt tracing; `.env` local đã cấu hình và bật. Mỗi lần chạy có trace mới, session theo job/contract/version. Mặc định chỉ gửi metadata/usage/thống kê; không gửi nội dung hợp đồng. Không đưa key vào source hoặc log. Xem `docs/langfuse.md`. Prompt summary và ngân sách theo model vẫn là TODO.
- Người dùng đã yêu cầu viết plan cho Agent 1: đọc `agents/agent_1_classification/plan.md`. Vòng đời mục tiêu là context → xử lý theo prompt → output/cache → tổng hợp đủ chunk → giải phóng memory Agent 1, giữ kết quả để Agent 2 sử dụng. Plan đã viết; cleanup snapshot/checkpoint chưa được triển khai bằng code, không được báo là đã tự động hoạt động.
- Plan Agent 2/3 đã viết trong `agents/agent_2_risk_scoring/plan.md` và `agents/agent_3_recommendation/plan.md`, cùng cấu trúc vòng đời Agent 1. Ưu tiên truy xuất database qua MCP; chỉ chuyển sang tool search web qua MCP khi bằng chứng chưa đủ. Giữ nguồn, điểm thiếu, cache kết quả/bằng chứng và memory theo hợp đồng; dọn memory sau khi kết quả đã lưu và bàn giao được. Đây là quy trình mục tiêu: MCP client, vòng lặp tool, kiểm tra bằng chứng/schema và cleanup chưa được triển khai; binding/endpoint/credential và luật nghiệp vụ vẫn để TODO.
- Source Hermes yêu cầu Python >=3.11,<3.15; workspace kiểm thử bằng `.venv312` (Python 3.12). Unit test dùng `unittest` trong `tests/unit/`; các notebook vẫn trống. Xem `docs/testing.md`. Smoke script `scripts/verify_langfuse.py` gửi dữ liệu giả lập tới Langfuse thật; không suy ra AWS thật hoạt động từ test này.
