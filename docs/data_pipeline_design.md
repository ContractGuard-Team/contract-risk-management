# Thiết kế Data Recollector + Data Router/Processing (2026-11-13)

Trạng thái: **bản thiết kế, chưa có code**; đã có file môi trường mẫu `data_pipeline/.env.example` (2026-11-13). `data_pipeline/` hiện là 23 file rỗng. Mục 9 liệt kê các quyết định
cần người dùng chốt trước khi triển khai. Mục 2 là số liệu đo thật trên trang nguồn ngày 2026-11-13.

## 1. Phạm vi

Cung cấp dữ kiện pháp luật cho Agent 2/3 (đọc qua MCP: `qdrant_search`, `supabase_select`, `s3_get_object`).
Hai thành phần, chạy **ngoài Docker Compose**, trong venv và file môi trường riêng trên EC2:

1. **Data Recollector**: EventBridge (lịch) → SQS → vào web → phát hiện văn bản mới/thay đổi → tải PDF →
   (a) lưu S3, (b) đẩy sang Processing + Validation.
2. **Data Router + Processing**: nhận văn bản → trích xuất/OCR → tiền xử lý → kiểm tra đã có chưa → lưu mới,
   thay thế, hoặc áp dụng sửa đổi/bổ sung → ghi Supabase (cấu trúc) + Qdrant (vector).

Chỉ thu thập: **Hiến pháp, Luật, Bộ luật, Luật/Bộ luật sửa đổi, bổ sung**. Bỏ qua Pháp lệnh, Nghị quyết, Lệnh.

## 2. Khảo sát trang nguồn (đã đo)

`https://vanban.chinhphu.vn/he-thong-van-ban?classid=1&mode=1&typegroupid=3`

| Điểm | Thực tế |
| --- | --- |
| Công nghệ | ASP.NET WebForms, lưới `ctrl_191017_163$grvDocument`; robots.txt `Allow: /` |
| Quy mô | 865 văn bản, 50 dòng/trang → 18 trang. Dropdown 100/200/500 có nhưng postback đổi cỡ trang **không có tác dụng** khi thử |
| Chuyển trang | Không có URL `?page=`. Phải POST lại form với `__VIEWSTATE` (85 KB), `__EVENTVALIDATION`, `__EVENTTARGET=…grvDocument`, `__EVENTARGUMENT=Page$N`. Đã thử: trang 2 trả "51 - 100 \| 865" |
| Thứ tự không ổn định | Trang 2 lặp lại 15/50 văn bản của trang 1 (cùng ngày ban hành) → **bắt buộc khử trùng theo `docid`** và đối soát với tổng "\| 865"; có thể sót văn bản nếu chỉ đi một lượt |
| Mỗi dòng | Số ký hiệu (`24/2026/QH16`), ngày ban hành, trích yếu, link chi tiết `?pageid=27160&docid=…`, link PDF trực tiếp `datafiles.chinhphu.vn/...pdf` |
| Trang chi tiết | Số ký hiệu, ngày ban hành, **ngày có hiệu lực**, **Loại văn bản**, cơ quan ban hành, người ký, trích yếu, file đính kèm |
| File | 100% PDF trong 234 văn bản đã quét; 220 văn bản có 1 file, 13 có 2, 1 có 3 |
| Nội dung PDF | 2/3 mẫu là **bản scan không có lớp text** (cần OCR), 1/3 có text (bản Công báo) |
| Phát hiện thay đổi | Server **không trả ETag/Last-Modified** → chỉ còn cách so hash nội dung |
| Loại lẫn trong nhóm 3 | Luật 147, Luật sửa đổi 76, có cả Pháp lệnh và văn bản UBTVQH → phải lọc theo "Loại văn bản" + tiêu đề |

## 3. Kiến trúc

```text
EventBridge Scheduler (cron)
   └─> SQS contract-data-update-queue  {"source":"vanban.chinhphu.vn","mode":"incremental|full"}
          └─> [EC2, systemd] data_pipeline.collector   (httpx + asyncio, BeautifulSoup + lxml)
                 1. duyệt danh sách: GET trang 1 → POST Page$2..N, khử trùng docid, đối soát tổng
                 2. lọc loại văn bản; so với sổ trạng thái (Supabase) → mới / đổi / không đổi
                 3. GET trang chi tiết (metadata) + tải PDF (giới hạn tốc độ, retry)
                 4. hash SHA-256 → nhánh 1: S3 (CHỈ bản gốc)
                 5. ghi `ingest_jobs` → nhánh 2: cùng bytes trong RAM chuyển thẳng sang processor (không qua S3)
          └─> [cùng tiến trình collector] data_pipeline.processor  (Data Router + Processing)
                 6. trích xuất text PDF; không có text → OCR Tesseract vie
                 7. chuẩn hóa, tách cấu trúc Chương/Mục/Điều/Khoản/Điểm, validate
                 8. router: văn bản gốc mới | bản cập nhật cùng số | văn bản thay thế | văn bản sửa đổi
                 9. ghi Supabase (văn bản, phiên bản, điều khoản, quan hệ) → embed → ghi Qdrant
                10. đánh dấu job done; chỉ xóa SQS message sau khi cả lượt thu thập đã ghi sổ
```

- (Cập nhật 2026-11-14, xem mục cuối) Collector gọi processor trực tiếp bằng bytes trong RAM; `ingest_jobs` chỉ còn là
  sổ trạng thái + hàng đợi retry. Job lỗi được service processor tải lại **từ web** (không đọc S3).
- Idempotent ở mọi bước: khóa tự nhiên là `docid` + `sha256` của PDF. SQS gửi trùng hay chạy lại đều không tạo bản ghi đôi.
- Không đi qua MCP: MCP là cổng **chỉ đọc** của agent. Pipeline dùng credential ghi riêng.

## 4. Data Recollector

**Kích hoạt.** EventBridge Scheduler → SQS. Đề xuất: `incremental` hằng ngày (chỉ đi các trang đầu cho tới khi gặp
một trang toàn văn bản đã biết và không đổi), `full` hằng tuần (đi hết 18 trang, đối soát tổng, phát hiện văn bản bị
sửa file). Worker long-poll SQS (20s), gia hạn visibility khi đang chạy, DLQ sau 3 lần lỗi.

**Phân trang** (`sources/legal_source.py`):

```text
state = GET list → parse hidden inputs + select
loop page = 1..:
    rows = parse(grid)                    # docid, số ký hiệu, ngày, trích yếu, link PDF
    seen |= {docid}
    nếu không có link Page$(page+1) và không có "..." → dừng
    state = POST form(state, __EVENTTARGET=grid, __EVENTARGUMENT=Page$(page+1))
kết thúc: nếu len(seen) < tổng trên trang ("| 865") → chạy thêm lượt lọc theo từng năm (drdDocYear) để vét phần sót
```

Mỗi request cách nhau ≥1s, một kết nối, User-Agent định danh, retry 3 lần có backoff; `asyncio` chỉ dùng để tải
song song trang chi tiết/PDF (tối đa 3 luồng), không song song hóa phân trang (VIEWSTATE phụ thuộc trang trước).

**Lọc loại** (`router/source_classifier.py`): nhận nếu "Loại văn bản" ∈ {Hiến pháp, Luật, Bộ luật} **và** số ký hiệu
kết thúc `/QH<n>` (hoặc Hiến pháp). Phân lớp từ tiêu đề đã chuẩn hóa (không phân biệt hoa thường):
`constitution` | `code` (Bộ luật) | `law` | `amendment` (bắt đầu bằng "Luật/Bộ luật sửa đổi, bổ sung" hoặc
"Sửa đổi, bổ sung một số điều của Luật…"). Mọi thứ khác ghi `skipped` kèm lý do.

**Phát hiện thay đổi** (`processing/deduplicator.py`, `versioning.py`):

| Tình huống | Dấu hiệu | Hành động |
| --- | --- | --- |
| Văn bản mới | `docid` chưa có trong sổ | tải, version 1 |
| Không đổi | `docid` có, metadata dòng giống | bỏ qua (lượt `incremental` không tải lại PDF) |
| Đổi metadata | số ký hiệu/ngày/trích yếu/tên file khác | tải lại, so hash |
| Đổi file | lượt `full`: tải lại, `sha256` khác | version mới, version cũ `superseded` |
| Trùng nội dung | `sha256` đã có ở docid khác | không lưu lần hai, ghi liên kết |

**S3 – bản gốc, sắp theo mã số luật.** Bucket `contract-risk-database-unique-333596350642-ap-southeast-1-an`:

```text
legal/
  {khoa}/{nam}/{so:03d}-{nam}-{khoa}/          vd  legal/QH15/2024/042-2024-QH15/
      source/v{n}_{sha8}_{ten-file-goc}.{ext}  bản gốc (mọi định dạng), không bao giờ ghi đè — S3 CHỈ chứa các file này
  hien-phap/{nam}/…                            Hiến pháp không có số ký hiệu dạng số
(metadata, text, cảnh báo, nhật ký lượt chạy nằm ở Supabase; vector ở Qdrant)
```

Số được đệm 3 chữ số để liệt kê S3 theo prefix ra đúng thứ tự mã số luật. Bật S3 Versioning cho bucket.

## 5. Data Router + Processing

**Trích xuất.** Dùng lại thư viện `input_processing` (đã có PDF text + render + Tesseract vie, làm sạch, giữ vị trí
trang) bằng import trong venv của pipeline; cần cài `tesseract-ocr` + `tesseract-ocr-vie` trên host EC2. Trang không có
text → OCR 300 DPI. Ghi `extraction_method` (text|ocr) và độ tin cậy OCR theo trang.

**Tiền xử lý.** Bỏ header/footer Công báo, số trang, dấu "signed"; chuẩn Unicode NFC; nối dòng gãy. Tách cấu trúc bằng
regex pháp lý Việt Nam: `Phần`, `Chương`, `Mục`, `Điều N.` (đơn vị chính), `khoản`, `điểm`.

**Validation** (`processing/validator.py`) – không đạt thì job `needs_review`, không ghi Qdrant:
số ký hiệu trong text khớp metadata; có ≥1 `Điều`; số Điều tăng liên tục (thiếu Điều = OCR hỏng); tỉ lệ ký tự tiếng
Việt hợp lệ; độ tin cậy OCR ≥ ngưỡng; ngày ban hành ≤ ngày hiệu lực.

**Router – quy tắc dữ liệu.**

| Trường hợp | Nhận biết | Xử lý |
| --- | --- | --- |
| A. Văn bản mới | số ký hiệu chưa có | thêm văn bản + điều khoản + vector |
| B. Cập nhật cùng văn bản | cùng số ký hiệu, hash khác | version mới thay hoàn toàn: xóa vector cũ của văn bản đó, ghi vector mới; version cũ giữ ở Supabase/S3 với `superseded` |
| C. Luật mới thay luật cũ | điều khoản thi hành ghi "Luật … số …/…/QH… hết hiệu lực kể từ ngày Luật này có hiệu lực" | văn bản cũ `status=replaced`, `replaced_by`; đến ngày hiệu lực thì gỡ vector cũ khỏi tìm kiếm mặc định |
| D. Luật sửa đổi, bổ sung | lớp `amendment` | xem dưới |

**D – sửa đổi, bổ sung.** Luật sửa đổi là danh sách chỉ dẫn, mỗi chỉ dẫn nhắm vào một Điều/khoản của luật gốc:

1. Lưu luật sửa đổi như một văn bản riêng (có vector riêng, truy xuất được).
2. Xác định luật gốc từ tiêu đề và từ từng "Điều N. Sửa đổi, bổ sung một số điều của Luật X" (một luật sửa nhiều luật).
3. Phân tích từng chỉ dẫn thành `{luật gốc, điều, khoản, điểm, thao tác, nội dung mới}` với thao tác:
   `replace` ("Sửa đổi, bổ sung Điều 5 như sau: «…»"), `insert` ("Bổ sung Điều 5a vào sau Điều 5"), `repeal` ("Bãi bỏ khoản 3 Điều 7"), `rephrase` ("Thay cụm từ … bằng cụm từ …").
4. Áp dụng vào bản **hợp nhất** của luật gốc ở mức Điều: tạo phiên bản điều khoản mới (`valid_from` = ngày hiệu lực của
   luật sửa đổi), phiên bản cũ đóng lại (`valid_to`). Vector của Điều cũ được thay bằng vector của Điều đã hợp nhất,
   payload ghi `amended_by`.
5. Chỉ tự áp dụng khi chỉ dẫn khớp chắc chắn (`replace` nguyên Điều/khoản có nội dung trong ngoặc kép, `insert`, `repeal`
   có địa chỉ rõ). `rephrase` và chỉ dẫn mơ hồ → **không tự sửa**, ghi `legal_relations.status=needs_review`, Điều gốc
   gắn cờ `has_pending_amendment` để agent biết phải đọc thêm luật sửa đổi.

**Hiệu lực.** `status` ∈ `pending` (chưa đến ngày hiệu lực) | `in_force` | `replaced` | `expired`. Một job hằng ngày
(cùng lịch EventBridge) chuyển `pending → in_force` và áp dụng các sửa đổi đến hạn. Agent mặc định chỉ tìm `in_force`.

## 6. Cấu trúc lưu trữ

**Supabase (Postgres) – nguồn sự thật có cấu trúc:**

| Bảng | Khóa / cột chính | Mục đích |
| --- | --- | --- |
| `legal_documents` | `id`, `symbol` (unique), `docid`, `doc_class`, `title`, `issuer`, `issued_on`, `effective_on`, `status`, `replaced_by`, `current_version`, `source_url` | mỗi văn bản một dòng |
| `legal_document_versions` | `document_id`, `version`, `sha256` (unique), `s3_key`, `extraction_method`, `collected_at`, `state` | lịch sử file |
| `legal_articles` | `document_id`, `article_no`, `clause_no`, `heading`, `text`, `valid_from`, `valid_to`, `amended_by`, `has_pending_amendment`, `page_from` | bản hợp nhất theo Điều, có phiên bản theo thời gian |
| `legal_relations` | `source_document_id`, `target_document_id`, `target_article`, `relation` (amends/replaces/repeals), `operation`, `effective_on`, `status` | sửa đổi, thay thế |
| `ingest_jobs` | `docid`, `sha256`, `state` (pending/processing/done/needs_review/failed), `attempts`, `error_type` | hàng đợi collector → processor |
| `crawl_runs` | `run_id`, `mode`, `pages`, `seen`, `new`, `changed`, `skipped`, `total_on_site`, `finished_at` | đối soát mỗi lượt |

Index: `legal_documents(symbol)`, `(status, effective_on)`, `legal_articles(document_id, article_no, valid_to)`.
Bật RLS, chỉ cấp `select` cho role đọc. Tạo bảng bằng file migration SQL (PostgREST không tạo được bảng).

**Qdrant `legal_documents`** (đã có: 3072 chiều, Cosine, strict mode, index `contract`):

- 1 point = 1 Điều (Điều dài thì tách theo khoản, kèm tiêu đề Điều ở đầu mỗi đoạn). `id = uuid5(symbol, article_no, part, valid_from)`.
- Payload: `document_id`, `symbol`, `doc_class`, `title`, `article_no`, `clause_no`, `heading`, `text`, `status`,
  `effective_on`, `valid_from`, `amended_by`, `s3_key`, `page_from`, `source_url`, `version`.
- Vì strict mode từ chối lọc trên trường chưa index, cần tạo thêm payload index: `symbol`, `doc_class`, `status`
  (keyword), `article_no` (integer), `effective_on` (datetime).
- Truy xuất nhanh nhất cho agent: vector search lọc `status=in_force` → lấy `symbol` + `article_no` → đọc nguyên văn/lịch
  sử ở Supabase → cần bản gốc thì `s3_key`.

## 7. Môi trường chạy riêng trên EC2 (không dùng Docker)

```text
(đã gộp) cấu hình nằm ở mục 9 của root .env / .env.example; secret lấy từ Secrets Manager (2026-11-14)
infrastructure/requirements/data_pipeline.in/.txt    httpx, beautifulsoup4, lxml, boto3, supabase, qdrant-client, pypdf, pypdfium2, Pillow, openai
infrastructure/ec2/data_pipeline_setup.sh            tạo venv /opt/contract-risk/data-pipeline/.venv, cài tesseract-ocr-vie, cài lock
infrastructure/ec2/contract-data-pipeline.service    systemd: EnvironmentFile=…, ExecStart=…python -m data_pipeline run, Restart=on-failure
infrastructure/aws/data-pipeline-policy.json         sqs Receive/Delete/ChangeVisibility trên queue; s3 Put/Get/List trên prefix legal/
```

Biến môi trường: `DATA_SQS_QUEUE_URL=https://sqs.ap-southeast-1.amazonaws.com/333596350642/contract-data-update-queue`,
`AWS_REGION=ap-southeast-1`, `DATA_S3_BUCKET`, `DATA_S3_PREFIX=legal`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` (ghi),
`QDRANT_URL`, `QDRANT_API_KEY` (ghi), `QDRANT_COLLECTION=legal_documents`, `EMBEDDING_PROVIDER=openai`,
`EMBEDDING_MODEL=text-embedding-3-large`, `EMBEDDING_DIMENSIONS=3072`, `OPENAI_API_KEY`, `SOURCE_LIST_URL`,
`CRAWL_DELAY_SECONDS=1`, `CRAWL_CONCURRENCY=3`, `OCR_LANG=vie`, `LOG_LEVEL`. AWS dùng instance role, không đặt access key.

## 8. Ánh xạ vào file sẵn có và thứ tự triển khai

| Bước | File | Kết quả kiểm chứng |
| --- | --- | --- |
| 1 | `sources/legal_source.py`, `router/source_classifier.py` | test với HTML đã lưu: phân trang, khử trùng docid, lọc loại; chạy thật đối soát 865 |
| 2 | `ingestion/s3_archiver.py`, `processing/deduplicator.py`, `versioning.py`, migration SQL | test Moto: key theo mã số, không ghi đè, hash trùng không lưu lại |
| 3 | `pipeline.py` (SQS worker), env/requirements/systemd | chạy `collect` thật một lượt incremental |
| 4 | `processing/extractor.py`, `normalizer.py`, `validator.py`, `ingestion/chunker.py` | 3 PDF mẫu (2 scan, 1 text) tách đúng số Điều |
| 5 | `router/data_router.py`, `collection_mapper.py`, `ingestion/postgres_writer.py`, `embedding.py`, `qdrant_writer.py` | trường hợp A/B/C trên dữ liệu thật |
| 6 | sửa đổi/bổ sung (mục 5-D) | bộ test chỉ dẫn replace/insert/repeal + hàng đợi needs_review |
| 7 | MCP: tool tìm kiếm bằng text (embed câu hỏi) | agent-2/3 tìm được Điều luật qua MCP |

`data_pipeline/sources/` chỉ còn `legal_source.py` (đã bỏ market/company/web_source rỗng ngày 2026-11-14); `rag/` không thuộc phạm vi lần này.

## 9. Quyết định cần chốt trước khi viết code

1. ~~Model embedding 3072 chiều~~ **Đã chốt 2026-11-13: OpenAI `text-embedding-3-large`** (mặc định 3072 chiều = kích thước
   collection). Dùng SDK `openai` trong venv pipeline, khóa `OPENAI_API_KEY` trong file môi trường của pipeline. Câu hỏi của
   agent sau này (MCP tìm bằng text) phải embed bằng đúng model này.
2. **Payload `contract`** (index tenant sẵn có trong collection) dùng để chứa gì với văn bản luật? Nếu không có ý định
   riêng, đề xuất không dùng và lọc theo `symbol`/`doc_class`/`status`.
3. **Tạo bảng Supabase**: pipeline cần chạy migration SQL một lần (dán vào SQL Editor, hoặc cấp chuỗi kết nối Postgres).
4. **Quyền ghi**: pipeline dùng key Qdrant quyền ghi và secret key Supabase riêng; MCP chuyển sang key chỉ đọc.
5. **Sửa đổi mơ hồ**: đồng ý cơ chế `needs_review` (không tự sửa luật gốc khi không chắc) hay muốn dùng LLM để
   hợp nhất tự động, chấp nhận rủi ro sai nội dung luật?
6. **Lịch**: incremental hằng ngày + full hằng tuần có phù hợp không; ai tạo rule EventBridge và DLQ.
7. **Phạm vi lịch sử**: trang hiện có 865 văn bản từ 2016; lần đầu chạy `full` sẽ OCR khoảng vài trăm PDF scan
   (ước lượng hàng giờ CPU trên EC2 nhỏ).

## 10. Rủi ro

- Trang đổi giao diện/ID control → phân trang hỏng. Giảm thiểu: tìm control theo hậu tố `grvDocument`, cảnh báo khi
  số dòng = 0 hoặc tổng lệch.
- OCR tiếng Việt sai dấu trên bản scan chất lượng thấp → validation chặn, vào `needs_review`.
- Thứ tự danh sách không ổn định → luôn đối soát với tổng và vét theo năm.
- Hợp nhất sửa đổi sai làm agent trích luật sai → chỉ tự áp dụng mẫu chắc chắn, luôn giữ `amended_by` và bản gốc.
- Văn bản bị gỡ khỏi trang: không tự xóa dữ liệu; ghi `missing_since` để người xem xét.

## 11. Trạng thái triển khai (2026-11-14) — ĐÃ CÓ CODE

Trạng thái đầu file ("chưa có code") là lịch sử. Hai tiến trình, hai service systemd, một venv, không Docker:

```text
EventBridge Scheduler --(theo lịch, {"mode":"incremental|full"})--> SQS contract-data-update-queue
  └─> contract-data-collector  = python -m data_pipeline collector   (data_pipeline/collector.py, CHỈ NÓ đọc SQS)
        mỗi message = 1 lượt: web → văn bản mới/đổi → file gốc
          ├─ nhánh 1 → S3 legal/... (chỉ bản gốc)
          └─ nhánh 2 → processor trong RAM: OCR → tách Điều → validate → quy tắc → Supabase + Qdrant
        → ghi crawl_runs → xóa message
  contract-data-processor     = python -m data_pipeline processor   (retry job lỗi: tải lại TỪ WEB; refresh hiệu lực mỗi ngày)
```

| File | Vai trò |
| --- | --- |
| `sources/legal_source.py` | parse danh sách/chi tiết, postback `Page$N`, khử trùng docid, vét theo năm |
| `router/source_classifier.py` | giữ Hiến pháp/Luật/Bộ luật/sửa đổi |
| `processing/deduplicator.py` | SHA-256, quyết định new/unchanged/recheck (vai trò của `versioning.py`, file đó để trống) |
| `ingestion/s3_archiver.py` | chỉ bản gốc: `legal/QH15/2024/042-2024-QH15/source/v{n}_{sha8}_{file}` |
| `ingestion/postgres_writer.py` + `migrations/001_legal_schema.sql` | Supabase (6 bảng, RLS bật) |
| `processing/extractor.py`, `normalizer.py`, `ingestion/chunker.py`, `processing/validator.py` | OCR (input_processing, Tesseract vie), làm sạch, tách Điều chịu lỗi OCR, kiểm tra |
| `router/data_router.py`, `processor.py` | quy tắc: bản mới thay bản cũ; luật thay thế; sửa đổi theo Điều (replace/insert/repeal chắc chắn tự áp dụng, còn lại needs_review + cờ `has_pending_amendment`); sửa đổi tương lai `scheduled`; sửa đổi đến trước luật gốc `unresolved` rồi tự giải quyết lại |
| `ingestion/embedding.py`, `router/collection_mapper.py`, `ingestion/qdrant_writer.py` | OpenAI text-embedding-3-large; 1 point/Điều (Điều dài chia theo khoản); payload index symbol/doc_class/status/article_no/current/effective_on. Agent lọc `current=true` + `status=in_force` |
| `pipeline.py`, `__main__.py` | nạp root `.env` (DATA_PIPELINE_ENV_FILE) + secret rỗng từ Secrets Manager, lệnh `collector|processor|collect|process|refresh` |
| `infrastructure/requirements/data_pipeline.in/.txt`, `infrastructure/ec2/data_pipeline_setup.sh`, `contract-data-collector.service`, `contract-data-processor.service`, `infrastructure/aws/data-pipeline-policy.json` | venv riêng, Tesseract vie, systemd, IAM |

**Đã kiểm chứng:** 148/148 unit test (9 test mới `tests/unit/test_data_pipeline.py`: HTML thật, phân trang/khử trùng,
lưu S3 không ghi đè, chạy lặp idempotent, các quy tắc, sửa đổi tương lai + refresh, sửa đổi đến trước luật gốc,
consumer SQS chỉ xóa message khi thành công). Chạy thật: liệt kê **865/865** văn bản (missing 0, 68 giây, 40 trang
gồm vét năm), 646 qua lọc tên; 2 PDF scan thật qua OCR: luật 18/2026/QH16 → 66 Điều/72 vector, luật sửa đổi
24/2026/QH16 → 3 Điều, chỉ dẫn "bổ sung khoản 2a" tự áp dụng được, đích 143/2025/QH15 lấy từ lời dẫn.

**Chưa chạy thật:** ghi Supabase (chưa chạy migration), ghi Qdrant Cloud, gọi OpenAI (chưa có key), SQS/S3 thật.

**Triển khai trên EC2:**
1. Supabase SQL Editor: chạy `data_pipeline/migrations/001_legal_schema.sql`.
2. Gắn `infrastructure/aws/data-pipeline-policy.json` vào instance role (cùng role với Docker: quyền ghi `legal/` nên
   chỉ dùng cho pipeline; MCP vẫn chặn ghi trong code).
3. `sudo sh infrastructure/ec2/data_pipeline_setup.sh` (Ubuntu khuyến nghị: có sẵn `tesseract-ocr-vie`).
4. Root `.env` (cp .env.example .env): secret để trống — QDRANT_*/SUPABASE_* đọc từ Secrets Manager `contract-risk/prod/api-keys`.
5. Lần đầu: `sudo -u contract-data ... python -m data_pipeline collect --mode full` hoặc gửi `{"mode":"full"}` vào SQS;
   rồi `systemctl start contract-data-processor contract-data-collector`.
6. EventBridge Scheduler → SQS: incremental hằng ngày `{"mode":"incremental"}`, full hằng tuần `{"mode":"full"}`;
   đặt redrive policy (DLQ, maxReceiveCount 3) và visibility timeout ≥ 900 s.

**Giới hạn:** một luật sửa đổi bị tải lại phiên bản mới sẽ ghi quan hệ lần nữa (không khử trùng quan hệ); hiệu lực
khác nhau theo từng khoản trong cùng luật (vd khoản 1 Điều 1 hiệu lực sớm hơn) dùng một ngày hiệu lực chung của văn bản;
lần full đầu OCR ~650 PDF (ước tính 10–20 giờ CPU ở 300 DPI).

### Bổ sung sau khi đối chiếu yêu cầu (2026-11-14)

- **Hiến pháp không nằm trong `typegroupid=3`** (0/865). Hiến pháp ở `typegroupid=1` (5 văn bản 1946–2013). Collector đọc
  `SOURCE_LIST_URLS` = cả hai danh sách; thật: 870/870, missing 0. Chỉ Hiến pháp 2013 có PDF; 4 bản cũ không có file
  (đếm `no_file`). Ký hiệu Hiến pháp ("." / "68/LCT/HĐNN8") được đổi thành `HP-<năm>`; S3 `legal/hien-phap/2013/HP-2013/`.
- **Hiến pháp mới thay bản cũ**: sau khi xử lý một Hiến pháp, bản ban hành mới nhất giữ `in_force`, các bản cũ hơn `replaced`
  (văn bản Hiến pháp không dùng câu "… hết hiệu lực" như luật).
- **Legal references**: mỗi Điều lưu `legal_references` (số hiệu luật được viện dẫn "… Luật X số N/YYYY/QHk …") trong Supabase
  và payload Qdrant (có index keyword → lọc "các Điều viện dẫn luật X"). Viện dẫn chỉ ghi tên không kèm số chưa được ánh xạ.
- Trang không có RSS/API: chỉ HTML. "Bộ luật sửa đổi" trên trang mang tiêu đề "Luật sửa đổi, bổ sung … của Bộ luật …"
  (nằm trong 172 luật sửa đổi), router nhận đích `Bộ luật`.
- Nghị quyết sửa đổi Hiến pháp (vd 203/2025/QH15) là loại Nghị quyết nên không thu thập theo phạm vi yêu cầu.
- 150/150 unit test (11 test pipeline).

### Nhận mọi định dạng đính kèm + chạy thử vào Supabase thật (2026-11-14)

- **Mọi file văn bản đính kèm đều được nhận** (theo yêu cầu): .pdf, .doc, .docx, .rtf, .odt… Chỉ từ chối file rỗng hoặc
  trang HTML lỗi. Bản gốc lưu đúng định dạng (ContentType theo đuôi file). Processor đọc PDF/DOCX/TXT/ảnh bằng
  input_processing; .doc/.rtf/.odt… được LibreOffice headless chuyển sang PDF trước (`processing/extractor.py`,
  `SOFFICE_PATH` tùy chọn). `data_pipeline_setup.sh` cài `libreoffice-writer-nogui`. Hiến pháp 1946–1992 chỉ có .doc/.rtf;
  file cũ có thể dùng font TCVN3/VNI (trước Unicode) → validator sẽ chặn nếu chữ lỗi (needs_review).
- **Chạy thử thật (máy local, không có quyền S3):** migration đã chạy trên Supabase; `DATA_ARCHIVE_DIR` (LocalArchiver,
  cùng key như S3) + `collect --limit N`. 3 lượt: 10 văn bản vào Supabase (5 Hiến pháp HP-1946…HP-2013, 5 luật sửa đổi
  20–24/2026/QH16), 10 version, 10 ingest_jobs pending, 3 crawl_runs; lượt lặp lại nhận 6 văn bản `unchanged`, không bản trùng.
- **Trước khi chạy thật trên EC2:** các dòng thử này có `s3_keys` trỏ tới file local, chưa có trên S3. Phải xóa chúng
  (6 bảng) rồi để collector trên EC2 thu thập lại; nếu không, lượt EC2 sẽ coi chúng `unchanged` và không upload lên S3.

### Embedding không dùng API key (2026-11-14, thay quyết định OpenAI ở mục 9.1)

- Model mã nguồn mở **`intfloat/multilingual-e5-large`** (1024 chiều) chạy CPU qua `fastembed`/ONNX, không cần key, văn bản
  không rời máy. Code dùng chung: `integrations/external/embedding_client.py` (`LocalEmbedder`, tự thêm tiền tố
  `passage: `/`query: ` mà e5 yêu cầu). Đã bỏ `data_pipeline/ingestion/embedding.py` (OpenAI) và `openai` khỏi lock.
- Collection Qdrant `legal_documents` đã tạo lại **1024 chiều** (trước 3072, đang rỗng: script kiểm 0 điểm rồi mới xóa) và có
  sẵn payload index symbol/doc_class/status/article_no/current/effective_on/legal_references.
- e5 chỉ đọc 512 token; văn bản luật tiếng Việt ~3,6 ký tự/token → `embedding_parts` cắt tối đa 1500 ký tự (≤ ~420 token).
- MCP `qdrant_search` nhận `query` dạng text và tự embed bằng cùng model; mặc định lọc `current=true` + `status=in_force`
  (`query_filter={}` để tìm toàn bộ). Image mcp 705 MB + volume `mcp-models` (model tải lần đầu vào `/models`,
  `HF_HOME=/models/huggingface` vì home chỉ đọc). Pipeline EC2: `EMBEDDING_CACHE_DIR`/`HF_HOME` dưới
  `/var/lib/contract-risk/models` (systemd `StateDirectory`).
- Đã đo trên máy local (không qua container): 66 Điều thật → 194 đoạn, 0,38 s/đoạn; truy vấn tiếng Việt trả đúng Điều liên quan.
  Lần full đầu (~650 văn bản) ước tính vài giờ CPU cho embedding, cộng thời gian OCR. RAM: model ~2,2 GB mỗi tiến trình
  (processor và mcp mỗi bên một bản). Theo yêu cầu, chưa chạy thử truy vấn qua model trong container mcp.

### Luồng hai nhánh, không lưu local (2026-11-14, mới nhất — thay các mô tả S3 JSON/LocalArchiver ở trên)

```text
web (file gốc) → collector ─┬─> S3: CHỈ file gốc (legal/<khóa>/<năm>/<số>/source/v{n}_{sha8}_{file})
                            └─> processor (bytes trong RAM, cùng tiến trình) → Supabase + Qdrant
```

- Đã bỏ: `metadata.json`, `text/v{n}.json`, `_runs/*.json` trên S3; `LocalArchiver`/`DATA_ARCHIVE_DIR`; processor đọc S3.
- Collector tải file 1 lần, ghi S3 xong thì đưa **cùng bytes** cho `processor.process_files` (`asyncio.to_thread`, mỗi lúc
  1 văn bản vì OCR nặng CPU). Job tạo ở trạng thái `processing` → `done`/`needs_review`; lỗi → `pending` (tối đa 3 lần).
- Service `contract-data-processor` chỉ còn: retry job `pending` (tải lại từ `source_urls` trên web, không đọc S3), trả job
  `processing` quá 3 giờ (tiến trình chết) về `pending`, refresh hiệu lực hằng ngày, giải quyết quan hệ `unresolved`.
- Thông tin trước đây ghi JSON lên S3 nay ở Supabase: `legal_document_versions.source_urls/preamble/warnings/validation_errors`
  (**chạy `data_pipeline/migrations/002_versions_without_s3_json.sql` trong SQL Editor**), nhật ký lượt chạy ở `crawl_runs`.
- Không ghi file local; chỉ có file tạm OCR/LibreOffice (tự xóa) và cache model embedding (bắt buộc để chạy model).
- Systemd: collector giờ cũng nạp model (StateDirectory models, TimeoutStopSec 300, Nice 10). RAM: collector và processor
  mỗi tiến trình nạp model ~2,2 GB khi khởi động.
- 154/154 unit test (test mới: xử lý lỗi → retry tải lại từ web, `get_object` S3 bị cấm trong test).
- **Chạy thật luồng mới (2026-11-14):** 1 văn bản thật 144/2025/QH15 (luật sửa đổi, PDF có lớp text vì máy local không có
  Tesseract) → collector → processor trong RAM → Supabase + Qdrant Cloud thật; nhánh S3 chạy trên Moto (local không có
  credential). Kết quả: job `done` ở lần 1, version `processed` (method text, 0 lỗi validate, preamble 466 ký tự,
  `source_urls` đúng), 4 Điều, 33 vector 1024 chiều, 60 quan hệ `amends` `unresolved` (luật gốc chưa có trong DB — đúng quy tắc),
  tìm kiếm "phạm vi điều chỉnh của luật" trả Điều 1/Điều 4 (score 0,83/0,82). Đã xóa sạch dữ liệu thử và model.

### Một file env + Secrets Manager (2026-11-14, mới nhất)

- Đã bỏ `data_pipeline/.env.example` và `/etc/contract-risk/data-pipeline.env`. Pipeline đọc root `.env` (mục 9 + key
  dùng chung AWS_REGION, QDRANT_*, SUPABASE_*, EMBEDDING_*, LOG_LEVEL). systemd: `DATA_PIPELINE_ENV_FILE=<repo>/.env`;
  `data_pipeline_setup.sh` cho group service user đọc file (640).
- `QDRANT_URL`, `QDRANT_API_KEY`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` rỗng trong `.env` → đọc từ Secrets Manager
  `API_KEYS_SECRET_ID` (`config.secrets.read_secret_json`, instance role; quyền trong `data-pipeline-policy.json`).
  Xem docs/environment.md.

## Sửa lỗi luồng ghi (2026-11-17)

- Collector: version đã lưu nhưng thiếu ingest job (dừng giữa `add_version` và `add_job`) → lần quét sau thấy cùng hash vẫn
  gọi `add_job` (idempotent) nên job được tạo lại.
- Processor retry: bytes tải lại phải khớp `file_hashes` của version; khác → job `failed` (`SourceChanged`), lần crawl sau
  lưu file mới thành version mới.
- `apply_amendment`: lỗi ở bất kỳ bước nào sau khi đóng Điều cũ → hoàn tác (đóng dòng mới với valid_to=valid_from, xóa point
  mới, mở lại dòng/point cũ theo point id tất định) rồi ném lỗi; Điều không bao giờ mất bản hiện hành, retry chạy lại từ
  cùng trạng thái. Hoàn tác cũng lỗi → log ERROR để người kiểm tra.
