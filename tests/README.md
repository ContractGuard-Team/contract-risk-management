# Kiểm thử

Chạy từ root source checkout với Python 3.12. Cài các dependency lock theo [README](../README.md), sau đó:

```sh
python -B -m unittest discover -s tests/unit -p "test_*.py" -v
```

Unit test dùng model/client giả, Moto, dữ liệu mẫu hoặc thư mục tạm. Không cần credential cloud thật. Bộ test kiểm tra agent/schema, processing, workflow/handoff/cache, worker, monitoring/config, MCP/RAG và data pipeline. Kiểm thử chất lượng model thật hoặc hiệu năng cần dữ liệu và môi trường nghiệm thu riêng.

## Notebook Jupyter

Mở bằng VS Code/Jupyter với kernel `.venv312`. Nếu cần đăng ký kernel:

```sh
python -m ipykernel install --user --name contract-risk --display-name "Contract Risk (Python 3.12)"
```

| Notebook | Mục đích |
| --- | --- |
| [Agent 1](agent_evaluation/test_agent_1_classification.ipynb) | Chunk, nguồn, memory và bàn giao |
| [Agent 2](agent_evaluation/test_agent_2_risk_scoring.ipynb) | Vòng tool, bằng chứng và scoring |
| [Agent 3](agent_evaluation/test_agent_3_recommendation.ipynb) | Recommendation, sửa schema và bàn giao |
| [Tổng thể offline](end_to_end/test_contract_risk_system.ipynb) | Ba agent, cache/resume/retry và AnalysisResult |
| [Input processing](unit/test_input_processing.ipynb) | Xử lý text, chunk và provenance |
| [File processing](integration/test_file_processing.ipynb) | DOCX/ảnh, extraction và metadata |
| [Worker lifecycle](integration/test_worker_job_lifecycle.ipynb) | AWS giả lập, IPC, progress và lưu kết quả |
| [Service interfaces](integration/test_container_services.ipynb) | HTTP/auth, secret, durable queue và recovery |

Notebook từng agent và tổng thể gọi lại unittest, không tạo bộ assertion khác. Tên thư mục `agent_evaluation/` được giữ để dùng tiếp khi có dữ liệu đánh giá; hiện chưa đo độ chính xác pháp lý của model thật. Lưu bản có output vào `.cache/executed-notebooks/`, giữ output notebook tracked trống.

## Kiểm thử tích hợp Docker

[container_smoke.py](integration/container_smoke.py) kiểm tra toàn luồng qua HTTP với Moto/model fixture; [handoff_audit.py](integration/handoff_audit.py) kiểm dữ liệu bàn giao. Làm theo [docs/testing.md](../docs/testing.md) trong stack thử riêng. Không chạy bài smoke vào stack production đang nhận job.

Fixture pháp luật hiện dùng nằm trong `fixtures/data_pipeline/`. Test khác chủ động tạo dữ liệu trong code/thư mục tạm. Các notebook và thư mục fixture chỉ có placeholder đã được loại bỏ; xem [biên bản cleanup](../docs/project_cleanup.md).
