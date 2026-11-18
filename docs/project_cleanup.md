# Rà soát file rỗng và dọn project — 2026-11-18

Đã đối chiếu file rỗng, notebook không có cell, Python chỉ có docstring, nơi import/tham chiếu và luồng triển khai thực tế. Không xóa dựa riêng vào kích thước file. Source Hermes upstream, môi trường Python, model, `.env` và dữ liệu job/cache được giữ nguyên.

## Các file đã bỏ

**28 file** không có chức năng thực thi hoặc nội dung: 21 file 0 byte và 7 notebook không có cell.

| File/nhóm file | Vai trò dự kiến | Lý do bỏ / nơi thay thế |
| --- | --- | --- |
| `configs/agents.yaml`, `app.yaml`, `aws.yaml`, `databases.yaml`, `logging.yaml`, `models.yaml` | Config tập trung từ thiết kế ban đầu | Không có nội dung hay code nạp; config thật ở `.env`, Compose, `config/policy.json`, `agents/<agent>/config.yaml`; giữ `configs/memory.yaml` |
| `scripts/ingest_knowledge.py` | Ingestion thủ công | Không có code; dùng `python -m data_pipeline collect/process/refresh` |
| `scripts/init_postgres.py` | Khởi tạo database | Không có code; schema pháp luật có SQL migrations trong `data_pipeline/migrations/` |
| `scripts/init_qdrant.py` | Khởi tạo collection | Không có code; dùng `QdrantWriter.ensure_indexes()` trong pipeline |
| `scripts/run_local.py` | Chạy local | Không có code; dùng root Compose và entrypoint từng module |
| `common/ports/job_repository.py`, `vector_store.py` | Port lưu job/vector dự kiến | Không định nghĩa interface, không có import; job storage và connector hiện tại có implementation riêng |
| `common/schemas/analysis.py` | Schema phân tích dự kiến | Không có model; schema thật ở `common/schemas/business/` và schema từng agent |
| `data_pipeline/processing/versioning.py` | Quản lý phiên bản | Không có code; vai trò đang nằm ở deduplicator, processor và store |
| `docs/aws_connections.md` | Tài liệu AWS dự kiến | Trống; kết nối và triển khai được mô tả ở workers/environment/EC2 guide |
| `agents/agent_3_recommendation/prompts/examples/example_1.md` | Ví dụ Agent 3 tùy chọn | Trống, loader bỏ qua; xóa không thay nội dung prompt nhưng danh sách file dùng tính execution revision thay đổi. Hoàn tất job đang chạy trước khi deploy. Khi có ví dụ thật có thể tạo lại `examples/*.md` |
| `tests/fixtures/{agent_inputs,contracts,expected_outputs,knowledge}/.gitkeep`, `tests/outputs/.gitkeep` | Giữ folder test dự kiến | Không có dữ liệu hoặc nơi dùng; giữ fixture pháp luật thực tế. Test khác sinh dữ liệu trong thư mục tạm; output local vào `.cache/` |
| `tests/integration/test_data_pipeline.ipynb`, `test_hermes_integration.ipynb`, `test_knowledge_retrieval.ipynb`, `test_multi_agent_workflow.ipynb` | Notebook tích hợp dự kiến | Không có cell; logic tương ứng có unit test và notebook workflow tổng thể |
| `tests/unit/test_input_validation.ipynb`, `test_orchestration.ipynb` | Notebook unit dự kiến | Không có cell; đã có notebook processing/tổng thể và unit test Python |
| `tests/load/test_concurrent_jobs.ipynb` | Test tải dự kiến | Không có cell hoặc bài đo tải; bỏ để không hiểu nhầm project đã có load test |

Đã bỏ thêm **7 thư mục rỗng** phát sinh từ các file trên và **28 thư mục `__pycache__`** của code ứng dụng. Bytecode có thể tự sinh lại; `.gitignore`/`.dockerignore` loại thêm cache pytest/ruff/Jupyter. Không xóa `.cache` vì có thể chứa job, kết quả kiểm thử và dữ liệu cần tiếp tục.

## File rỗng cần giữ

| File/nhóm | Lý do |
| --- | --- |
| 31 file `__init__.py` rỗng của ứng dụng | Marker package Python/setuptools; không phải nghiệp vụ chưa triển khai |
| Ba `agents/<agent>/memory/long_term_reflection.md` | Hook prompt đã được runtime/test tham chiếu; để trống thì reflection bỏ qua. Chờ nội dung do người dùng/dev quyết định |
| `agents/agent_1_classification/prompts/rules/BR-RISK-006/.gitkeep` | Giữ vị trí danh mục phân loại chờ đối tác |
| `agents/agent_1_classification/prompts/rules/fact_keys/.gitkeep` | Giữ vị trí danh mục fact key chờ đối tác |
| `agents/agent_2_risk_scoring/prompts/rules/BR-RISK-007/.gitkeep` | Giữ vị trí danh mục rủi ro chờ đối tác |
| File rỗng trong `agents/hermes/hermes_agent/` | Source upstream có package marker, fixture và placeholder riêng; không thay đổi thư viện gốc để làm sạch ứng dụng |
| `infrastructure/requirements/contract_risk_management.egg-info/dependency_links.txt` | Metadata sinh từ editable install, hiện chỉ whitespace; không sửa tay metadata package |

Sau cleanup còn **37 file 0 byte thuộc ứng dụng** có lý do ở bảng trên. Không có Python stub chỉ chứa docstring được phát hiện ngoài upstream.

## File được hoàn thiện / tài liệu được đồng bộ

- Viết lại `README.md` theo runtime hiện tại, thêm `docs/ec2_setup_guide.md`; `docs/deployment_ec2.md` dẫn về hướng dẫn mới để tránh hai quy trình khác nhau.
- Đồng bộ kiến trúc, environment, testing; đánh dấu báo cáo EC2 cũ là lịch sử. Giữ nguyên bản tham chiếu `docs/folder-responsibilities.md`.
- Điền `tests/README.md` làm mục lục kiểm thử. Ba notebook agent và notebook tổng thể trước đây trống nay gọi unittest có sẵn; không thêm luật nghiệp vụ hoặc test chất lượng model giả định.
- Sửa notebook service tìm root bằng `pyproject.toml` thay vì `plan_work.txt` đã không còn.
- Không thay luồng nghiệp vụ hoặc cấu hình `.env`; các thay đổi source có sẵn trước cleanup được giữ nguyên.

## Kiểm chứng

- Parse AST **217 file Python** ứng dụng: không lỗi cú pháp.
- **8 notebook** hợp lệ theo nbformat, có cell, code parse được, output tracked trống.
- Thực thi các code cell của **4 notebook vừa hoàn thiện** bằng Python 3.12: đạt **58 lượt test** (7 + 8 + 6 + 37; có test dùng chung được chạy lại). Đây không phải kiểm bằng Jupyter UI/kernel hoặc full test suite.
- `docker compose config --quiet`: đạt; không build/recreate service trong đợt cleanup.
- Discover **248 test**: không có lỗi import sau khi bỏ module rỗng; không đồng nghĩa đã chạy toàn bộ test.
- Đã sửa liên kết cũ trỏ source Bedrock/plan_work_note không còn tồn tại; giữ báo cáo đó dưới nhãn lịch sử. Liên kết nội bộ của README, AGENTS, tests/README và docs được kiểm tra; `git diff --check` đạt.
- Không triển khai EC2, không gọi AWS/OpenAI thật, không kết luận lại chất lượng model hoặc kết quả nghiệm thu container từ các test này.

Để tiếp tục: làm theo `ec2_setup_guide.md` trên máy đích, kiểm quyền model/AWS và một job thử; bổ sung danh mục/reflection khi có nội dung được duyệt. Không cần tạo lại các placeholder đã bỏ.
