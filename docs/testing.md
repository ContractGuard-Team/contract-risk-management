# Kiểm thử base code 3 agent

Chạy tại root repository với dependency trong `pyproject.toml`:

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
