# Build, run và triển khai EC2

Hướng dẫn triển khai hiện hành đã được gom vào **[ec2_setup_guide.md](ec2_setup_guide.md)**: đưa source lên GitHub, clone EC2, cấu hình Secrets Manager/IAM, model cache, build 9 image, nghiệm thu worker, cài data pipeline systemd, cập nhật và rollback.

Chạy local và dependency Python: [README](../README.md). Kiểm thử offline/Jupyter: [tests/README.md](../tests/README.md). Lịch sử kiểm thử image: [ec2_readiness.md](ec2_readiness.md).

Điểm cần nhớ: `models/` và `.env` không đi theo Git; local fixture vẫn cần credential đọc Secrets Manager; giữ named volumes khi cập nhật; data pipeline không thuộc Compose. Source hiện dùng OpenAI Responses API, prompt và memory budgets đã có cấu hình.
