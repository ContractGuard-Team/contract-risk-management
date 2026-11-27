# Data pipeline pháp luật

Cung cấp kho luật cho Agent 2/3. Chạy **ngoài Docker**: hai service systemd trên EC2, một venv riêng, dùng chung root `.env`. Không liên
quan worker hợp đồng.

## 1. Luồng

```text
EventBridge Scheduler ({"mode":"incremental|full"}) → SQS contract-data-update-queue
  └─> contract-data-collector   (CHỈ nó đọc SQS; 1 message = 1 lượt thu thập)
        web → văn bản mới/đổi → file gốc
          ├─ nhánh 1 → S3: chỉ file gốc
          └─ nhánh 2 → processor, cùng bytes trong RAM → Supabase + Qdrant
  contract-data-processor       (retry job lỗi: tải lại TỪ WEB; cập nhật hiệu lực mỗi ngày)
```

S3 chỉ chứa file gốc, không JSON; không lưu file local. Message SQS chỉ xóa khi cả lượt thành công. Lịch đề xuất: incremental
hằng ngày, full hằng tuần; DLQ sau 3 lần; visibility ≥ 900 giây.

## 2. Nguồn

`vanban.chinhphu.vn`, hai danh sách: `typegroupid=1` (Hiến pháp) và `typegroupid=3` (Luật/Bộ luật). Chỉ nhận Hiến pháp, Luật, Bộ luật và
luật sửa đổi; bỏ Pháp lệnh, Nghị quyết, Lệnh.

- Trang ASP.NET: chuyển trang bằng postback `Page$N`; thứ tự không ổn định nên khử trùng theo `docid`, đối soát tổng và vét theo năm.
- Server không có ETag/Last-Modified: phát hiện thay đổi bằng SHA-256.
- Nhận mọi định dạng đính kèm; `.doc/.rtf/.odt` chuyển PDF bằng LibreOffice; phần lớn PDF là bản scan, OCR Tesseract `vie` 300 DPI.
- Cách nhau ≥ 1 giây mỗi request, tối đa 4 văn bản xử lý cùng lúc (`DOCUMENTS_IN_FLIGHT`).

## 3. Xử lý

Trích xuất/OCR → làm sạch → tách Điều → validate → router → ghi. Không đạt validate (số hiệu không khớp, thiếu Điều, chữ lỗi) → `needs_review`,
không ghi Qdrant.

| Trường hợp | Xử lý |
| --- | --- |
| Văn bản mới | Thêm văn bản, Điều, vector |
| Cùng số hiệu, file đổi | Lưu và index bản mới xong mới thay bản cũ; lỗi thì bản cũ giữ nguyên |
| Luật mới thay luật cũ | Bản cũ `replaced`, ghi `replaced_on` |
| Hiến pháp mới | Bản mới nhất `in_force`, bản cũ `replaced` (ký hiệu `HP-<năm>`) |
| Luật sửa đổi | Chỉ tự áp dụng chỉ dẫn chắc chắn (replace/insert/repeal); mơ hồ → `needs_review` + cờ `has_pending_amendment` |
| Sửa đổi có hiệu lực tương lai | `scheduled`, áp dụng khi tới ngày |
| Sửa đổi đến trước luật gốc | `unresolved`, tự giải quyết khi có luật gốc |

- Áp dụng sửa đổi lỗi giữa chừng thì hoàn tác; Điều không bao giờ mất bản hiện hành.
- Retry: bytes tải lại phải khớp hash đã lưu; khác → `failed` (`SourceChanged`), lượt sau lưu thành bản mới. Tối đa 3 lần.
- Mỗi Điều lưu `legal_references` (luật được viện dẫn có số hiệu).

## 4. Lưu trữ

**S3**: `legal/<khóa>/<năm>/<số>-<năm>-<khóa>/source/v{n}_{sha8}_{file}`; Hiến pháp: `legal/hien-phap/<năm>/HP-<năm>/source/`. Không ghi đè.

**Supabase** (6 bảng, RLS bật; chạy `data_pipeline/migrations/001` rồi `002` trong SQL Editor):

| Bảng | Nội dung |
| --- | --- |
| `legal_documents` | Mỗi văn bản một dòng: số hiệu, loại, hiệu lực, trạng thái |
| `legal_document_versions` | Lịch sử file: hash, S3 key, URL nguồn, cảnh báo |
| `legal_articles` | Điều theo thời gian: `valid_from`, `valid_to`, `amended_by` |
| `legal_relations` | Sửa đổi, thay thế, bãi bỏ |
| `ingest_jobs` | Trạng thái xử lý và retry |
| `crawl_runs` | Đối soát mỗi lượt |

**Qdrant** `legal_documents`: dense 1024 chiều cosine + sparse `keywords` (IDF) + 10 payload index. Một point = một Điều (hoặc phần của
Điều dài, ≤ 1.500 ký tự). Point id tất định theo số hiệu + Điều + ngày + phần. `ensure_indexes()` tạo collection nếu chưa có.

## 5. Vận hành

```sh
sudo sh infrastructure/ec2/data_pipeline_setup.sh
sudo systemctl start contract-data-processor contract-data-collector
journalctl -u contract-data-collector -f
sudo -u contract-data env PYTHONPATH=$PWD /opt/contract-risk/data-pipeline/.venv/bin/python -m data_pipeline collect --limit 1
```

Lệnh: `collector | processor | collect | process | refresh`. Secret Qdrant/Supabase đọc từ Secrets Manager; IAM ở
`infrastructure/aws/data-pipeline-policy.json`. Model embedding đọc offline từ `models/` (khoảng 2,2 GB RAM mỗi tiến trình). Sự kiện gửi
monitoring qua `DATA_MONITORING_URL`; mỗi lượt là một `trace_id`.

## 6. Giới hạn

- Đã chạy thật với Supabase và Qdrant Cloud trên vài văn bản rồi xóa dữ liệu thử; **chưa chạy thật** S3, OCR trong luồng này và EC2.
- Lần full đầu OCR khoảng 650 PDF, ước tính 10–20 giờ CPU.
- Hiệu lực khác nhau theo từng khoản trong một luật dùng chung một ngày. Bản mới của cả văn bản làm mất vector bản cũ.
- Các dòng thử cũ có `s3_keys` trỏ file local phải xóa trước khi chạy trên EC2, nếu còn.
