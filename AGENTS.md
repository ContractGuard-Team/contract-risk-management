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
- LangGraph được nêu trong thiết kế; code hiện chưa tích hợp. Không coi tên công nghệ trong bảng mô tả là dependency đã cài hoặc chức năng đã chạy.
- Base code hiện có: pipeline cùng tiến trình gọi ba agent, cấu hình riêng từng agent, nạp các Markdown bắt buộc, ghép context, model provider và bộ chuyển đổi Bedrock từ Hermes. Tool có giao diện/callback đăng ký tường minh; memory lưu theo instance của job. Chưa có vòng lặp runtime tự gọi tool, retrieval hoặc schema nghiệp vụ. Prompt, API/model config và chính sách riêng vẫn để TODO theo yêu cầu người dùng.
- Unit test hiện dùng `unittest` trong `tests/unit/test_agent_base.py`; các notebook test còn trống. Xem `docs/testing.md`. Chỉ báo cáo các kiểm chứng thực sự đã chạy, không suy ra hệ thống end-to-end hoạt động từ test mock.
