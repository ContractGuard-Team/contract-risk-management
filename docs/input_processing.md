# Tiền xử lý độc lập — 2026-11-07

service.py đã bọc logic tiền xử lý bằng HTTP job service. Worker gửi bytes/base64
và job metadata; service lưu job trước ACK, chạy process_job hiện có rồi trả ContractInput
JSON cho worker gửi sang LangGraph. Không import hoặc gọi agent trực tiếp.

Module Python vẫn nhận text hoặc RetrievedFile và trả output preprocessing; logic
extract/OCR/normalize/structure/chunking giữ nguyên. HTTP adapter giữ đầy đủ provenance,
manifest từng chunk và context.preprocessing. Converter chỉ nằm tại ranh giới service.

```text
Worker HTTP → file bytes → process_job → ContractInput JSON → Worker → LangGraph
```

Docker target input-processing có Tesseract và gói eng/vie. Xem
[interface](container_interfaces.md), [Docker/EC2](deployment_ec2.md).

## Chạy riêng

```python
from pathlib import Path
from input_processing import process_job, RetrievedFile

path = Path("uploads/contract.pdf")
file = RetrievedFile(path.read_bytes(), path.name, {"type": "local"})
result = process_job({
    "job_id": "preprocess-001", "contract_id": "HD-001", "contract_version": "1",
    "extraction": {"ocr": "auto", "languages": "vie+eng"},
    "options": {"max_tokens": 512, "overlap_tokens": 64},
}, file=file)
assert result["status"] == "ok", result.get("error")
# result chỉ là dữ liệu, không tự chạy Agent 1.
```

Ví dụ Python trên dành cho file đã biết kích thước. CLI đọc bounded bytes và hỗ trợ
root giới hạn cho file local; không nhận nguồn S3 hoặc import worker:

```sh
python -m scripts.preprocess_contract --file contract.pdf --local-root uploads --contract-id HD-001 -o result.json
python -m scripts.preprocess_contract text-job.json -o result.json
```

Job text có `contract_id`, `job_id`, `text`, options tùy chọn. Với API Python, dùng
`file=` hoặc `text`, không truyền `source`. CLI hỗ trợ `source.type=local` để nạp file
trước khi gọi module; `source.type=s3` báo lỗi, không có kết nối ngầm.

Output giữ `job_id`, `contract_id`, `contract_version`, `status`, `document`, `source_hash`,
`extraction`, `stats`, `warnings`, `cleaned_text` và `chunks`. Mỗi chunk giữ ID/index,
tổng chunk, text, token_count/method, heading_path, pages, source_spans và metadata file.
Trường `error`/chunks rỗng báo thất bại; không được coi là hợp đồng đã phân tích.

## Trích xuất và OCR

- PDF: `pypdf` lấy text theo layout từng trang; PDFium render trang cần OCR.
  Số trang vật lý được giữ cả khi chỉ có một trang hoặc có trang trắng giữa tài liệu.
  PDF mã hóa hoặc hỏng trả lỗi. Không cố đoán password.
- DOCX: đoạn/bảng theo thứ tự body, ô rỗng, số danh sách decimal/letter/Roman/bullet,
  bảng lồng được làm phẳng có warning; header/footer và footnote/endnote có element ID.
  Header/footer/notes được thêm sau body và phân biệt trong metadata, không tự đoán trang Word.
  Ảnh nhúng đi qua OCR. Tracked changes, altChunk, ảnh ngoài và ảnh trong note báo lỗi
  yêu cầu xử lý tài liệu trước; không tự tải tài nguyên ngoài.
- TXT: UTF-8 (có/không BOM) hoặc UTF-16 có BOM; không tự đoán encoding mất dữ liệu.
- PNG/JPEG/TIFF: kiểm tra format, OCR mỗi frame; TIFF nhiều trang giữ thứ tự.
- `ocr=auto`: dùng text PDF khi có; OCR trang có content nhưng không có text.
  Trang có cả text và ảnh giữ text layer và trả warning để caller cân nhắc `always`.
  `always`: render/OCR mọi trang PDF; DOCX giữ native text và OCR ảnh nhúng.
  `never`: báo lỗi khi gặp phần cần OCR, không bỏ phần đó để báo thành công.
- OCR trống/timeout/thiếu engine trả lỗi toàn job. Chưa có nhận diện chất lượng OCR
  hoặc đảm bảo đúng chữ/đúng bảng trên mọi mẫu hợp đồng; cần kiểm tra corpus thực.

OCR cần **Tesseract executable** và traineddata `vie`, `eng` ngoài dependency Python.
`pip install -e .` cài thư viện Python, không tự cài Tesseract vào hệ điều hành.
Trên image Debian/Ubuntu dành cho worker, cài:

```sh
apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng
```

Trên Windows, cài Tesseract cùng language packs, thêm vào PATH hoặc export
`TESSERACT_CMD` tới executable. Có thể inject `TesseractOCR(executable=..., tessdata_dir=...)`.
Biến này được đọc từ environment, không tự nạp `.env` và không chứa credential.
Xác minh bằng `tesseract --list-langs`, rồi chạy CLI với file scan thật.
Workspace kiểm thử hiện chưa có Tesseract; OCR trong unit test dùng engine/subprocess giả,
nhưng việc tạo/render PDF, mở DOCX và đọc ảnh dùng thư viện thật.

`ProcessingLimits` là cấu hình host: mặc định 20 MiB/file, 200 trang/frame,
2 triệu ký tự, 100 MiB tổng DOCX giải nén, 10.000 ZIP entry, 25 triệu pixel,
OCR 60 giây/trang tại 200 DPI. Không cho job JSON tự nâng các giới hạn này.
Đây là giới hạn kiểm tra của ứng dụng, không thay thế giới hạn RAM/CPU của worker/container.

Metadata `source_spans` có thêm `method` và `element` từ extraction. `raw_start/end`
lúc này trỏ vào **text đã extract**, không phải bytes PDF/DOCX; hash bytes gốc nằm ở
`document.file_sha256`. `source_hash` là hash text đã extract.
Word có `page=null`; PDF/ảnh biết page ngay cả tài liệu một trang. Token budget vẫn
chỉ dành cho chunk; workflow kiểm tra budget của cả prompt/context/memory riêng.

Đối chiếu API với nguồn chính thức:
[pypdf text extraction](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md)
phân biệt text layer và OCR;
[python-docx iteration](https://github.com/python-openxml/python-docx/blob/master/features/blk-iter-inner-content.feature)
giữ thứ tự paragraph/table;
[PDFium render](https://pypdfium2.readthedocs.io/en/stable/python_api.html)
dùng scale từ DPI. Tích hợp đóng rõ ràng page/bitmap sau khi render.

## Kiểm thử

`test_input_processing.py` và `test_file_processing.py` kiểm tra output độc lập;
`tests/integration/test_file_processing.ipynb` tạo DOCX/ảnh và kiểm tra provenance.
Không còn notebook gọi tiền xử lý rồi chuyển context vào Agent 1.
Các test graph dùng ContractInput giả lập trực tiếp, tách khỏi preprocessing.

## Đánh giá file người dùng cung cấp

`contract_preprocessor.py` có ranh giới ba bước hợp lý: cleaning → structure → chunking,
job envelope dễ tích hợp, nhận diện điều/chương/phụ lục tiếng Việt/Anh, overlap và CLI.
File chỉ nhận text đã extract; không có chức năng đọc PDF/DOCX, OCR hoặc tải S3.

Các vấn đề tìm thấy và cách xử lý:

| Vấn đề trong bản gốc | Xử lý trong project |
| --- | --- |
| Chuẩn hóa mọi chữ số về `#` khi tìm header lặp có thể xóa `Điều 1`, `Điều 2`, `Điều 3`; số đứng riêng ở mép trang cũng bị xóa | Chỉ bỏ nhãn `Trang/Page N` tại đầu/cuối trang khi input có `\f`; giữ header khác và số tiền |
| `hợp-\nđồng` thành `hợpđồng` | Không tự bỏ gạch nối hoặc đoán từ; giữ nguyên bằng chứng |
| Bảng TAB bỏ ô rỗng, làm lệch cột | Giữ cả ô đầu/cuối và ô giữa rỗng |
| Bảng chỉ có một hàng quá dài có thể trả về danh sách rỗng | Chia hàng dài và giữ toàn bộ nội dung, kèm cảnh báo về cấu trúc bảng |
| Từ/URL dài không có khoảng trắng và tiêu đề lớn có thể vượt budget | Cắt tiếp theo ký tự; kiểm tra ngân sách trên text chunk cuối cùng; không trả `ok` nếu không vừa |
| Cộng token từng block không tương đương đếm toàn chuỗi | Đếm lại cả separator, heading và overlap trên mỗi candidate |
| Gộp điều nhỏ giữ heading path của điều đầu; phụ lục mất scope khi gặp điều | Không gộp khác điều; giữ scope phụ lục trong heading path |
| Không có provenance sau khi normalize/chunk | Thêm source spans theo dòng, vị trí normalized text, trang nếu biết và hash nguồn |
| Option sai kiểu có thể gây TypeError/AttributeError ngoài error envelope | Validate kiểu, miền giá trị và option lạ trước khi xử lý |
| `tiktoken` tự chọn nếu có, fallback mọi Exception và ước lượng ký tự thiếu minh bạch | Mặc định offline UTF-8 byte estimate có nhãn/cảnh báo; inject counter theo model |
| CLI đóng stdin, bỏ qua `--max-tokens 0`, JSONL lỗi có thể ghi file rỗng | Giữ stdin mở, validate số 0, lỗi JSONL giữ nguyên file output đã có |

SHA-256 thay SHA-1 cho `content_hash`; output có `hash_algorithm` để không hiểu nhầm định dạng.
Lỗi dữ liệu trả `status=error`; lỗi bất ngờ trong callback/programming được đẩy lên caller,
không tuyên bố hàm bắt mọi lỗi. Không sao chép cả script thành một module lớn.
