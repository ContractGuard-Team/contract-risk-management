# RAG pháp luật cho Agent 2/3 (2026-11-14)

`rag/` là logic truy xuất; Qdrant (`legal_documents`) là kho; dữ liệu do `data_pipeline/` ghi. RAG chạy **trong service
`mcp`** (image `contract-risk/mcp:local` COPY `rag/`), vì model embedding/rerank và credential Qdrant chỉ nằm ở đó.
Agent không import `rag/`: gọi tool MCP `legal_search`.

```text
Agent 2: tools/legal_search.py ─┐                      (handler bind lúc khởi tạo, MCP_URL)
Agent 3: tools/legal_reference.py ┴─> MCP legal_search ─> rag/retriever.py (LegalRetriever)
   1. metadata_filter.py  tiêu chí → Qdrant filter (mặc định current=true + status=in_force)
   2. hybrid_search.py    1 lệnh query_points: prefetch dense (e5, cosine) + sparse "keywords" (BM25, IDF) → RRF
   3. retriever._articles gộp các phần (part) của cùng Điều/phiên bản thành nguyên văn Điều, 1 bằng chứng/Điều
   4. reranker.py         cross-encoder jina-reranker-v2-base-multilingual (fastembed ONNX, CPU, không key)
   5. evidence_formatter  citation, evidence_id, hiệu lực, nguồn, cảnh báo
```

## Tool `legal_search`

| Tham số | Ý nghĩa |
| --- | --- |
| `query` | câu hỏi pháp lý (≤1000 ký tự). Bỏ trống khi tra cứu đúng Điều |
| `limit` | số Điều trả về (≤20, mặc định 5) |
| `symbol` + `article_no` | lọc/tra cứu theo số hiệu (`91/2015/QH13`, `HP-2013`) và số Điều (`418`, `5a`) |
| `doc_class` | `constitution`, `code`, `law`, `amendment` (nhiều giá trị cách nhau dấu phẩy) |
| `as_of` | `YYYY-MM-DD`: nội dung **đang có hiệu lực vào ngày đó** (vd ngày ký hợp đồng), gồm bản đã bị sửa đổi/luật đã bị thay thế sau này |
| `include_inactive` | thêm luật đã bị thay thế/chưa có hiệu lực (bản hiện hành của chúng) |

Kết quả: `{query, filters, reranked, found, evidence: [...]}`. Mỗi bằng chứng: `evidence_id` (`số hiệu#Điều@valid_from`),
`citation` ("Điều 418 Bộ luật Dân sự số 91/2015/QH13"), `text` (nguyên Điều, cắt ở 6000 ký tự + `truncated`), `heading`,
`chapter`, `status`, `valid_from`/`valid_to`/`replaced_on`/`effective_on`, `current`, `version`, `amended_by`,
`has_pending_amendment`, `legal_references`, `page_from`, `source_url`, `s3_prefix` (file gốc trên S3: `s3_list_objects`),
`score` (rerank), `fusion_rank`, `warnings` (luật hết hiệu lực, bản lịch sử, sửa đổi chưa áp dụng, bị cắt), `retrieved_at`.
`found=0` = kho không có văn bản phù hợp → agent mới chuyển sang `web_search` (đúng thứ tự trong plan Agent 2/3).

## Hybrid: dense + keyword

- Dense: `intfloat/multilingual-e5-large` (1024 chiều) — hiểu diễn đạt khác nhau.
- Sparse: `integrations/external/embedding_client.py:sparse_vector` — âm tiết tiếng Việt + bigram ("hợp đồng"), số hiệu
  luật giữ nguyên 1 token ("91/2015/qh13"), index = crc32, giá trị tf bão hòa (BM25 k1=1.2); Qdrant nhân IDF
  (`modifier=idf`). Không lưu thống kê corpus, không model. Pipeline đưa thêm dòng "Điều N <tên luật> <số hiệu>" vào
  vector từ khóa để câu hỏi chứa số Điều/số hiệu khớp chính xác.
- Pipeline và RAG dùng chung một hàm → token khớp nhau. Đổi tokenizer = phải index lại.

## Lọc theo thời gian

Payload mỗi point: `valid_from` (ngày Điều/phiên bản bắt đầu áp dụng), `valid_to` (ngày bị sửa đổi — processor ghi khi
retire), `replaced_on` (ngày luật thay thế có hiệu lực — processor ghi khi đánh dấu `replaced`). `as_of=D`:
`valid_from ≤ D` và không (`valid_to ≤ D`) và không (`replaced_on ≤ D`). Các trường có payload index DATETIME
(strict mode của Qdrant Cloud chặn lọc trên trường chưa index). Bản mới của cả văn bản (cùng số hiệu, file đổi) xóa
vector cũ → không tra lịch sử theo version file.

## Collection

`QdrantWriter.ensure_indexes()` tạo collection nếu chưa có: vector dense không tên 1024/Cosine + sparse `keywords`
(IDF) + 10 payload index. Collection cũ không có sparse → báo lỗi, phải tạo lại khi rỗng. Đã tạo lại Qdrant Cloud
`legal_documents` ngày 2026-11-14 (0 điểm trước khi xóa, giữ strict mode).

## Cấu hình (service mcp)

| Biến | Mặc định | Ghi chú |
| --- | --- | --- |
| `RAG_RERANK_MODEL` | `jinaai/jina-reranker-v2-base-multilingual` | ~1,1 GB, tải sẵn vào `<repo>/models` (`scripts/download_models.py`), mount read-only `/models`, `HF_HUB_OFFLINE=1`. Đặt rỗng để bỏ rerank (giữ thứ tự RRF) trên máy ít RAM. Thay thế nhẹ hơn: `BAAI/bge-reranker-v2-m3-int8` (0,57 GB) |
| `EMBEDDING_MODEL`/`EMBEDDING_CACHE_DIR` | e5-large, `/models` | dùng chung 1 instance cho `qdrant_search` và `legal_search` |
| `QDRANT_URL`/`QDRANT_API_KEY`/`QDRANT_COLLECTION` | | `/readyz` báo `legal_search` theo `QDRANT_URL` |

RAM service mcp khi đã nạp cả hai model: ~3,5 GB. Lần gọi đầu chờ tải/nạp model.

## Kiểm chứng

- `tests/unit/test_rag.py` (8 test, Qdrant in-memory, ghi bằng `QdrantWriter` thật, model giả): hybrid tìm theo từ khóa,
  mặc định chỉ bản hiện hành của luật còn hiệu lực, `as_of` trả đúng bản theo ngày (trước luật mới, trước/sau sửa đổi),
  ghép 2 phần thành nguyên Điều, `include_inactive`, `doc_class`, tra cứu theo số hiệu + Điều, rerank đổi thứ tự, validate.
- Chạy thật (máy local, model thật, Qdrant Cloud thật): pipeline đưa luật 144/2025/QH15 (4 Điều, 33 point dense+sparse)
  vào collection mới; `legal_search`:
  - "quy hoạch đô thị và nông thôn được sửa đổi như thế nào" → Điều 1 (điểm rerank 1,37) — Điều chứa các nội dung sửa đổi;
    lần gọi đầu 51 s (tải + nạp 2 model), các lần sau 0,3–2,4 s.
  - "hiệu lực thi hành của luật" → Điều 3 đứng đầu (rerank 0,65), đúng Điều về hiệu lực.
  - tra cứu `symbol=144/2025/QH15, article_no=1` → đúng Điều, 0,4 s; Điều dài cắt ở 6000 ký tự có cảnh báo.
  - `as_of=2020-01-01` → `found=0` (luật có hiệu lực 2026-01-01), đúng.
  Sau đó đã xóa dữ liệu thử (Supabase 6 bảng = 0, Qdrant 0 point, collection + index giữ) và model tải về.
- Chưa kiểm qua MCP từ container agent (theo yêu cầu); image mcp đã build lại có `rag/`, `/readyz` báo `legal_search: true`.
- Giới hạn: ngưỡng điểm rerank không lọc bằng chứng (agent tự đánh giá); Điều 1 của luật sửa đổi rất dài, chỉ 2000 ký tự đầu
  vào rerank; chưa có rerank/lọc cho `market_search` (chưa có dữ liệu thị trường).
