# Tài liệu mô tả chức năng các folder – Contract Risk Management AI

Dưới đây là bản phân tích chi tiết 17 thư mục chính và các file bên trong theo cấu trúc dự án bạn đã xây dựng.

Mục tiêu là làm rõ:

- Mỗi folder chịu trách nhiệm xử lý công việc gì.
- Mỗi file đóng vai trò gì trong hệ thống.
- Input và output của từng module.
- Module nào kết nối với Hermes, Bedrock, S3, Qdrant, PostgreSQL và SQS.
- Phân biệt code nghiệp vụ AI, code backend và code triển khai hạ tầng.

## 1. Tổng quan các folder chính

| Folder                | Chức năng chính                            | Thành phần sử dụng   |
| --------------------- | ------------------------------------------ | -------------------- |
| `third_party/`        | Chứa source code Hermes gốc                | 3 AI Agents          |
| `apps/`               | Xây dựng Backend API                       | FastAPI              |
| `input_processing/`   | Tiền xử lý hợp đồng                        | S3, Worker, Agent 1  |
| `agents/`             | Định nghĩa và thực thi 3 AI Agents         | Hermes, Bedrock      |
| `hermes_integration/` | Kết nối Hermes với model và tools          | Hermes, Bedrock      |
| `orchestration/`      | Điều phối workflow giữa các Agent          | LangGraph            |
| `workers/`            | Xử lý job bất đồng bộ                      | SQS, Orchestration   |
| `data_pipeline/`      | Thu thập và cập nhật dữ liệu bên ngoài     | API, S3, Qdrant      |
| `rag/`                | Truy xuất kiến thức phục vụ Agent          | Qdrant, Embedding    |
| `integrations/`       | Kết nối dịch vụ bên ngoài                  | AWS, Database, MCP   |
| `database/`           | Định nghĩa và truy vấn dữ liệu có cấu trúc | PostgreSQL           |
| `infrastructure/`     | Cấu hình triển khai hệ thống               | Docker, EC2, AWS     |
| `monitoring/`         | Theo dõi logs, metrics và traces           | CloudWatch, Langfuse |
| `tests/`              | Kiểm thử hệ thống                          | Pytest, evaluation   |
| `configs/`            | Cấu hình chung                             | Toàn bộ hệ thống     |
| `scripts/`            | Các script hỗ trợ vận hành                 | Developer, DevOps    |

Cấu trúc bạn gửi có 16 thư mục cấp cao nhất. Bên trong chúng có nhiều thư mục con đảm nhiệm các chức năng riêng.

## 2. `third_party/` – Source code Hermes

```
third_party/
└── hermes-agent/
    └── ...             # Source code Hermes gốc
```

Chức năng: Chứa mã nguồn Hermes Agent được lấy từ repository chính thức.

Đây là nền tảng runtime để xây dựng và thực thi AI Agent, không phải nơi chứa logic phân tích hợp đồng của team.

Các trách nhiệm chính của Hermes gồm:

- Quản lý quá trình tương tác giữa Agent và LLM.
- Xử lý tool calling.
- Quản lý lịch sử hội thoại và context theo cơ chế runtime.
- Thực hiện các vòng suy luận và gọi công cụ.
- Cung cấp cơ chế cấu hình model, tools và memory tùy phiên bản.

Input: Cấu hình Agent, prompt, messages, tools và yêu cầu thực thi.

Output: Phản hồi model, kết quả tool calling và kết quả thực thi Agent.

Kết nối: `hermes_integration/` → `third_party/hermes-agent/`.

Không nên chỉnh sửa trực tiếp source Hermes gốc nếu có thể thực hiện bằng adapter hoặc extension.

## 3. `apps/api/` – FastAPI Backend

Đây là tầng tiếp nhận và phản hồi yêu cầu từ frontend.

### `middleware/` – Xử lý request trước khi vào API

| File                | Chức năng                           |
| ------------------- | ----------------------------------- |
| `authentication.py` | Xác thực token, xác định người dùng |
| `authorization.py`  | Kiểm tra quyền truy cập hợp đồng    |
| `rate_limit.py`     | Giới hạn số request theo user/IP    |

### `routes/` – Định nghĩa các API endpoints

| File           | Chức năng                                           |
| -------------- | --------------------------------------------------- |
| `contracts.py` | Upload, lấy danh sách, cập nhật và quản lý hợp đồng |
| `analysis.py`  | Tiếp nhận yêu cầu phân tích hợp đồng                |
| `jobs.py`      | Truy vấn trạng thái job                             |
| `results.py`   | Trả về kết quả phân tích và đề xuất                 |

### `schemas/` – Định nghĩa cấu trúc request/response

| File          | Chức năng                      |
| ------------- | ------------------------------ |
| `contract.py` | Schema thông tin hợp đồng      |
| `job.py`      | Schema job và trạng thái xử lý |
| `response.py` | Chuẩn hóa dữ liệu API trả về   |

### `services/` – Xử lý nghiệp vụ Backend

| File                | Chức năng                                     |
| ------------------- | --------------------------------------------- |
| `upload_service.py` | Upload hợp đồng lên S3 hoặc cấp presigned URL |
| `job_service.py`    | Tạo job, lưu PostgreSQL và gửi SQS            |
| `result_service.py` | Truy vấn kết quả phân tích                    |

`main.py` khởi tạo FastAPI, đăng ký routes và middleware. `dependencies.py` cung cấp các dependency dùng chung như database session và xác thực.

Input: HTTP request từ frontend thông qua API Gateway.

Output: JSON response, `job_id`, trạng thái xử lý hoặc kết quả phân tích.

Ví dụ:

```
Frontend
   ↓
API Gateway
   ↓
FastAPI
   ├── Authentication
   ├── Authorization
   ├── Validation
   ├── PostgreSQL: Create Job
   └── SQS: Enqueue Job
           ↓
       Worker
```

FastAPI không nên chờ toàn bộ ba Agent chạy xong trong một HTTP request dài.

## 4. `input_processing/` – Tiền xử lý hợp đồng

Đây là tầng chuyển file hợp đồng thành dữ liệu có cấu trúc để Agent 1 xử lý.

| File                            | Chức năng                                    |
| ------------------------------- | -------------------------------------------- |
| `pipeline.py`                   | Điều phối toàn bộ quá trình tiền xử lý       |
| `file_retrieval.py`             | Tải file hợp đồng từ S3                      |
| `text_extraction.py`            | Trích xuất văn bản PDF/DOCX; gọi OCR khi cần |
| `text_cleaning.py`              | Làm sạch ký tự lỗi, chuẩn hóa văn bản        |
| `structure_preservation.py`     | Giữ tiêu đề, số điều khoản, bảng và vị trí   |
| `chunking.py`                   | Chia văn bản thành những chunk phù hợp       |
| `token_counter.py`              | Ước lượng token để kiểm soát giới hạn model  |
| `schemas/processed_contract.py` | Định nghĩa cấu trúc dữ liệu sau tiền xử lý   |

Input: File PDF, DOCX hoặc text từ S3.

Output: JSON gồm các chunk, metadata, vị trí điều khoản và nội dung văn bản.

Ví dụ:

```
{
  "contract_id": "contract_001",
  "chunks": [
    {
      "chunk_id": "001",
      "text": "Payment shall be made within 30 days...",
      "page": 2,
      "token_count": 120
    }
  ]
}
```

Lưu ý: Agent 1 nhận văn bản đã chia chunk, không nhất thiết nhận token ID. `token_counter.py` chủ yếu giúp kiểm soát kích thước input.

## 5. `agents/` – Ba AI Agents

Đây là nơi chứa logic nghiệp vụ AI chính của hệ thống.

Agent 1

`agent_1_classification/`

Phân loại hợp đồng thành các nhóm điều khoản, trích xuất thông tin quan trọng và giữ ngữ cảnh khi xử lý nhiều chunk.

Agent 2

`agent_2_risk_scoring/`

Đánh giá rủi ro của từng điều khoản bằng cách kết hợp nội dung hợp đồng, dữ liệu pháp luật, thị trường và doanh nghiệp.

Agent 3

`agent_3_recommendation/`

Đưa ra phương án giảm thiểu rủi ro, giải thích và đề xuất chỉnh sửa điều khoản.

### Các file chung trong mỗi Agent

| File/Folder         | Chức năng                                      |
| ------------------- | ---------------------------------------------- |
| `Dockerfile`        | Đóng gói Agent thành Docker image              |
| `main.py`           | Entrypoint khởi chạy service Agent             |
| `agent.py`          | Khởi tạo Agent, gọi Hermes và xử lý kết quả    |
| `config.yaml`       | Model, temperature, tools và giới hạn thực thi |
| `soul.md`           | Vai trò, nguyên tắc và giới hạn hành vi        |
| `plan.md`           | Quy trình xử lý nghiệp vụ                      |
| `prompts/`          | Prompt chuyên biệt                             |
| `context/`          | Xây dựng và quản lý context                    |
| `tools/`            | Công cụ nghiệp vụ Agent có thể gọi             |
| `schemas/output.py` | Cấu trúc JSON đầu ra                           |

Các file Markdown là quy ước cấu hình của dự án. Hermes chỉ sử dụng chúng nếu lớp tích hợp chủ động nạp nội dung.

### Agent 1 – Classification

| File                   | Chức năng                                       |
| ---------------------- | ----------------------------------------------- |
| `context_builder.py`   | Ghép chunk hiện tại với ngữ cảnh cần thiết      |
| `chunk_memory.py`      | Quản lý thông tin quan trọng từ chunk trước     |
| `context_compactor.py` | Rút gọn ngữ cảnh để tránh vượt giới hạn token   |
| `classify_clause.py`   | Phân loại điều khoản                            |
| `extract_key_terms.py` | Trích xuất nghĩa vụ, thời hạn, số tiền, chủ thể |

Output: Danh sách điều khoản đã phân loại, thông tin quan trọng và vị trí nguồn.

### Agent 2 – Risk Scoring

| File                  | Chức năng                                |
| --------------------- | ---------------------------------------- |
| `context_builder.py`  | Kết hợp điều khoản và bằng chứng         |
| `evidence_manager.py` | Kiểm tra, sắp xếp và quản lý bằng chứng  |
| `qdrant_search.py`    | Gọi module RAG tìm dữ liệu               |
| `legal_search.py`     | Tìm kiếm quy định pháp luật              |
| `market_search.py`    | Tìm kiếm thông tin thị trường            |
| `risk_calculator.py`  | Tính điểm theo tiêu chí định nghĩa trước |

Output: Điểm rủi ro, mức độ rủi ro, giải thích và bằng chứng tham chiếu.

### Agent 3 – Recommendation

| File                       | Chức năng                                 |
| -------------------------- | ----------------------------------------- |
| `context_builder.py`       | Tổng hợp điều khoản, rủi ro và bằng chứng |
| `recommendation_memory.py` | Quản lý ngữ cảnh đề xuất trong một job    |
| `qdrant_search.py`         | Tìm điều khoản tham khảo                  |
| `legal_reference.py`       | Truy xuất căn cứ pháp lý                  |
| `clause_rewriter.py`       | Đề xuất nội dung điều khoản thay thế      |

Output: Các đề xuất cải thiện và nội dung chỉnh sửa có giải thích.

## 6. `hermes_integration/` – Kết nối Hermes và Bedrock

Folder này là lớp trung gian giữa source Hermes gốc và hệ thống AI của team.

| File                  | Chức năng                               |
| --------------------- | --------------------------------------- |
| `runtime.py`          | Khởi tạo runtime Hermes                 |
| `agent_factory.py`    | Tạo Agent theo cấu hình                 |
| `model_adapter.py`    | Chuẩn hóa giao diện gọi model           |
| `bedrock_provider.py` | Chuyển đổi request/response cho Bedrock |
| `tool_registry.py`    | Đăng ký các tools được phép gọi         |
| `memory_adapter.py`   | Kết nối cơ chế memory/context           |
| `config_loader.py`    | Nạp cấu hình Agent và model             |

Luồng thiết kế:

```
agents/agent_2_risk_scoring/agent.py
                 ↓
hermes_integration/runtime.py
                 ↓
third_party/hermes-agent/
                 ↓
hermes_integration/model_adapter.py
                 ↓
hermes_integration/bedrock_provider.py
                 ↓
integrations/aws/bedrock_client.py
                 ↓
             AWS Bedrock
```

Đây là luồng tích hợp dự kiến. Khi triển khai thực tế, cần kiểm tra giao diện model provider của phiên bản Hermes để tránh tạo adapter không tương thích hoặc trùng với chức năng Hermes đã hỗ trợ.

## 7. `orchestration/` – Điều phối workflow

Orchestration quản lý toàn bộ quá trình thực thi Agent 1 → Agent 2 → Agent 3.

| File                   | Chức năng                               |
| ---------------------- | --------------------------------------- |
| `workflow.py`          | Định nghĩa graph và thứ tự xử lý        |
| `pipeline.py`          | Khởi chạy workflow cho một job          |
| `state.py`             | Định nghĩa trạng thái chung của job     |
| `context_transfer.py`  | Chuẩn hóa dữ liệu truyền giữa các Agent |
| `result_aggregator.py` | Tổng hợp kết quả cuối cùng              |
| `retry_policy.py`      | Quy tắc retry, backoff và xử lý lỗi     |

Ví dụ workflow:

```
START
  ↓
Input Processing
  ↓
Agent 1: Classification
  ↓
Agent 2: Risk Evaluation
  ├── Thiếu dữ liệu → RAG/Search → Đánh giá lại
  ↓
Agent 3: Recommendation
  ↓
Result Aggregator
  ↓
Save Results
  ↓
END
```

Nếu sử dụng LangGraph, `workflow.py` sẽ định nghĩa các node và edge. `state.py` định nghĩa dữ liệu dùng chung giữa các bước.

Lưu ý với kiến trúc ba container: Orchestration cần gọi Agent thông qua giao diện service hoặc queue nội bộ; không thể import trực tiếp tiến trình Agent đang chạy ở container khác.

## 8. `workers/` – Xử lý job bất đồng bộ

Worker nhận job từ Amazon SQS và khởi động quá trình xử lý.

| File                    | Chức năng                                      |
| ----------------------- | ---------------------------------------------- |
| `contract_worker.py`    | Nhận và xử lý job phân tích hợp đồng           |
| `ingestion_worker.py`   | Xử lý job cập nhật dữ liệu tham chiếu          |
| `job_dispatcher.py`     | Phân loại và chuyển job đến pipeline tương ứng |
| `visibility_manager.py` | Gia hạn visibility timeout khi xử lý lâu       |

Input: SQS message chứa `job_id`, `contract_id` và loại tác vụ.

Output: Trạng thái job, kết quả xử lý và xác nhận hoàn thành message.

Ví dụ:

```
SQS
 ↓
contract_worker.py
 ↓
job_dispatcher.py
 ↓
orchestration/pipeline.py
 ↓
Agent 1 → Agent 2 → Agent 3
 ↓
PostgreSQL
```

Worker phải xử lý trường hợp SQS gửi trùng message và chỉ xóa message sau khi đã lưu kết quả thành công.

## 9. `data_pipeline/` – Thu thập và cập nhật dữ liệu bên ngoài

Đây là pipeline phục vụ việc cập nhật kiến thức pháp luật, thị trường và doanh nghiệp.

Khác với `input_processing/`, folder này không xử lý hợp đồng người dùng mà xử lý nguồn dữ liệu tham chiếu.

### `sources/` – Thu thập dữ liệu

| File                | Chức năng                              |
| ------------------- | -------------------------------------- |
| `legal_source.py`   | Lấy dữ liệu pháp luật                  |
| `market_source.py`  | Lấy thông tin thị trường               |
| `company_source.py` | Lấy dữ liệu doanh nghiệp               |
| `web_source.py`     | Thu thập dữ liệu website được cho phép |

### `processing/` – Làm sạch và xác minh dữ liệu

| File              | Chức năng                     |
| ----------------- | ----------------------------- |
| `extractor.py`    | Trích xuất nội dung           |
| `normalizer.py`   | Chuẩn hóa định dạng           |
| `validator.py`    | Kiểm tra tính hợp lệ          |
| `deduplicator.py` | Phát hiện dữ liệu trùng       |
| `versioning.py`   | Quản lý phiên bản và thay đổi |

### `router/` – Phân loại và định tuyến dữ liệu

| File                   | Chức năng                             |
| ---------------------- | ------------------------------------- |
| `data_router.py`       | Quyết định dữ liệu cần chuyển đến đâu |
| `source_classifier.py` | Phân loại nguồn và nội dung           |
| `collection_mapper.py` | Ánh xạ dữ liệu vào collection Qdrant  |

### `ingestion/` – Đưa dữ liệu vào kho

| File                 | Chức năng                         |
| -------------------- | --------------------------------- |
| `chunker.py`         | Chia tài liệu tham chiếu          |
| `embedding.py`       | Tạo vector embedding              |
| `qdrant_writer.py`   | Ghi vector và metadata vào Qdrant |
| `postgres_writer.py` | Ghi dữ liệu có cấu trúc           |
| `s3_archiver.py`     | Lưu tài liệu nguồn trên S3        |

Luồng:

```
Legal / Market / Company Sources
               ↓
           Collector
               ↓
        Processing
               ↓
          Data Router
               ↓
     ┌─────────┼──────────┐
     ↓         ↓          ↓
 PostgreSQL  Qdrant      S3
```

Dữ liệu cần có provenance, ngày thu thập, ngày hiệu lực và version để tránh Agent sử dụng thông tin đã lỗi thời.

## 10. `rag/` – Truy xuất kiến thức

RAG phục vụ Agent 2 và Agent 3 khi cần tìm thông tin từ Qdrant.

| File                    | Chức năng                                   |
| ----------------------- | ------------------------------------------- |
| `retriever.py`          | Điều phối truy xuất                         |
| `hybrid_search.py`      | Kết hợp tìm kiếm vector và từ khóa          |
| `metadata_filter.py`    | Lọc theo loại tài liệu, quốc gia, thời gian |
| `reranker.py`           | Xếp hạng lại kết quả                        |
| `evidence_formatter.py` | Chuẩn hóa bằng chứng cho Agent              |

Input: Truy vấn của Agent, điều khoản, metadata và tiêu chí tìm kiếm.

Output: Danh sách đoạn văn bản liên quan, nguồn trích dẫn và metadata.

```
Agent 2
   ↓
qdrant_search.py
   ↓
rag/retriever.py
   ↓
Qdrant
   ↓
Evidence
   ↓
Agent 2 + Bedrock
```

`rag/` chứa logic truy xuất; Qdrant mới là nơi lưu trữ vector.

## 11. `integrations/` – Kết nối dịch vụ

Folder này chứa các client kết nối tới dịch vụ ngoài hệ thống.

### `aws/`

| File                | Kết nối             |
| ------------------- | ------------------- |
| `bedrock_client.py` | AWS Bedrock Runtime |
| `s3_client.py`      | Amazon S3           |
| `sqs_client.py`     | Amazon SQS          |
| `lambda_client.py`  | AWS Lambda          |

### `database/`

| File                 | Kết nối    |
| -------------------- | ---------- |
| `postgres_client.py` | PostgreSQL |
| `qdrant_client.py`   | Qdrant     |
| `redis_client.py`    | Redis      |

### `external/`

| File                  | Kết nối                    |
| --------------------- | -------------------------- |
| `search_client.py`    | Công cụ tìm kiếm bên ngoài |
| `mcp_client.py`       | MCP Server                 |
| `legal_api_client.py` | API dữ liệu pháp luật      |

Nguyên tắc: Folder `integrations/` chỉ nên quản lý kết nối, xác thực, retry kỹ thuật và giao tiếp API. Không nên chứa logic chấm điểm rủi ro hoặc phân loại điều khoản.

## 12. `database/` – PostgreSQL Models và Repositories

Folder này quản lý dữ liệu có cấu trúc và truy vấn database.

### `models/`

| File                  | Bảng dữ liệu               |
| --------------------- | -------------------------- |
| `users.py`            | Thông tin người dùng       |
| `contracts.py`        | Hợp đồng và phiên bản      |
| `jobs.py`             | Trạng thái xử lý           |
| `analysis_results.py` | Điểm rủi ro và kết quả     |
| `legal_documents.py`  | Metadata văn bản pháp luật |
| `source_versions.py`  | Phiên bản nguồn dữ liệu    |

### `repositories/`

Chứa code truy vấn database như tạo job, cập nhật trạng thái, lấy hợp đồng và lưu kết quả.

### `migrations/`

Chứa các bản thay đổi cấu trúc database, thường quản lý bằng Alembic.

Phân biệt:

- `integrations/database/postgres_client.py`: Tạo kết nối PostgreSQL.
- `database/models/`: Định nghĩa bảng.
- `database/repositories/`: Thực hiện truy vấn nghiệp vụ.

## 13. `infrastructure/` – Triển khai AWS và Docker

Folder này quản lý cấu hình triển khai, không chứa logic phân tích AI.

| Folder             | Chức năng                    |
| ------------------ | ---------------------------- |
| `lambda/`          | Source code AWS Lambda       |
| `docker/`          | Dockerfile và Docker Compose |
| `aws/iam/`         | IAM policies và roles        |
| `aws/sqs/`         | Cấu hình queue và DLQ        |
| `aws/s3/`          | Cấu hình bucket              |
| `aws/api_gateway/` | Cấu hình API Gateway         |
| `ec2/`             | Script cài đặt và deploy EC2 |

Các Dockerfile trong `agents/` phục vụ từng Agent, còn Dockerfile trong `infrastructure/docker/` phục vụ API và Worker.

Nếu chuyển sang ECS/Fargate, nên bổ sung:

```
infrastructure/
├── ecs/
│   ├── cluster/
│   ├── task_definitions/
│   ├── services/
│   └── autoscaling/
└── ecr/
```

ECR lưu Docker image; ECS quản lý task/service; Fargate cung cấp môi trường chạy container.

## 14. `monitoring/` – Giám sát hệ thống

| File          | Chức năng                                          |
| ------------- | -------------------------------------------------- |
| `logging.py`  | Ghi logs có cấu trúc                               |
| `tracing.py`  | Theo dõi luồng request/job qua các service         |
| `metrics.py`  | Thu thập thời gian xử lý, lỗi, token và throughput |
| `langfuse.py` | Tích hợp Langfuse để theo dõi LLM                  |

Monitoring giúp trả lời:

- Job nào bị lỗi?
- Agent nào xử lý chậm?
- Bedrock sử dụng bao nhiêu token?
- Worker có đang bị quá tải?
- Qdrant truy xuất mất bao lâu?

Nên dùng `job_id` và `trace_id` xuyên suốt pipeline để liên kết logs và traces.

## 15. `tests/` – Kiểm thử

| Folder              | Nội dung kiểm thử                                   |
| ------------------- | --------------------------------------------------- |
| `unit/`             | Kiểm thử từng hàm/module độc lập                    |
| `integration/`      | Kiểm thử kết nối Bedrock, S3, Qdrant, SQS           |
| `agent_evaluation/` | Đánh giá chất lượng phân loại, chấm điểm và đề xuất |
| `load/`             | Kiểm thử nhiều request/job đồng thời                |

Ví dụ:

- Agent 1 phân loại đúng bao nhiêu phần trăm điều khoản?
- Agent 2 có đưa ra điểm rủi ro nhất quán với bộ dữ liệu đánh giá không?
- Agent 3 có đề xuất dựa trên bằng chứng hợp lệ không?
- Hệ thống xử lý 100 job với 5 worker như thế nào?

Đối với Agent 2, không nên chỉ đánh giá bằng accuracy chung. Cần kiểm tra từng tiêu chí rủi ro, mức độ nhất quán, khả năng truy xuất nguồn và các trường hợp đánh giá sai nghiêm trọng.

## 16. `configs/` – Cấu hình hệ thống

| File             | Nội dung                             |
| ---------------- | ------------------------------------ |
| `app.yaml`       | Cấu hình API, worker, timeout        |
| `aws.yaml`       | Region, tên bucket, queue và service |
| `models.yaml`    | Model ID và thông số cho từng Agent  |
| `databases.yaml` | Cấu hình PostgreSQL, Qdrant, Redis   |

Ví dụ:

```
agents:
  agent_1:
    model: ${AGENT_1_MODEL_ID}
    max_tokens: 4096

  agent_2:
    model: ${AGENT_2_MODEL_ID}
    max_tokens: 4096

  agent_3:
    model: ${AGENT_3_MODEL_ID}
    max_tokens: 4096
```

Các giá trị `${...}` cần được `config_loader.py` hoặc hệ thống cấu hình thay thế bằng biến môi trường.

Không lưu AWS access key, database password hay API key trực tiếp trong các file YAML được commit lên Git.

## 17. `scripts/` – Công cụ hỗ trợ developer

| File                  | Chức năng                                         |
| --------------------- | ------------------------------------------------- |
| `init_postgres.py`    | Khởi tạo dữ liệu hoặc thiết lập PostgreSQL        |
| `init_qdrant.py`      | Tạo collections và indexes                        |
| `ingest_knowledge.py` | Chạy thử pipeline đưa dữ liệu vào RAG             |
| `run_local.py`        | Khởi chạy các thành phần phục vụ phát triển Local |

Các script này hỗ trợ setup, kiểm thử và vận hành thủ công. Những tác vụ chạy liên tục nên được quản lý bằng Worker, ECS Service hoặc scheduler phù hợp.

## 18. Luồng phụ thuộc giữa các folder

Để team dễ phát triển, nên thống nhất chiều phụ thuộc như sau:

`apps/api/`

Tiếp nhận request, tạo job

`workers/`

Nhận job và khởi động xử lý

`orchestration/`

Quản lý workflow và state

`input_processing/`

`agents/`

`hermes_integration/`

Hermes + Bedrock

`rag/`

Qdrant Retrieval

`integrations/` + `database/`

AWS, PostgreSQL, Qdrant, S3

`data_pipeline/` là luồng riêng để cập nhật kiến thức vào kho dữ liệu. `monitoring/` và `configs/` là các thành phần dùng chung, không phải bước xử lý tuần tự.

## 19. Phân chia công việc theo nhóm phát triển

| Nhóm phụ trách     | Folder chính                          |
| ------------------ | ------------------------------------- |
| Backend            | `apps/`, `database/`                  |
| AI Agent           | `agents/`, `hermes_integration/`      |
| AI Workflow        | `orchestration/`, `workers/`          |
| Data Engineering   | `input_processing/`, `data_pipeline/` |
| RAG / Retrieval    | `rag/`, Qdrant integration            |
| DevOps / Cloud     | `infrastructure/`, `monitoring/`      |
| QA / AI Evaluation | `tests/`                              |

### Một số điểm nên bổ sung vào cấu trúc hiện tại

1. `shared/` hoặc `common/`: Chứa schema chung giữa API, Worker và ba Agent; đặc biệt quan trọng khi các Agent chạy trong container riêng.
2. `orchestration/checkpoint.py`: Lưu trạng thái để có thể tiếp tục từ Agent 2 nếu Agent 1 đã hoàn thành.
3. `infrastructure/ecs/`: Nếu chuyển từ EC2 + Docker sang ECS/Fargate.
4. `data_pipeline/scheduler/`: Lập lịch cập nhật dữ liệu pháp luật, thị trường và doanh nghiệp.
5. `monitoring/alerts.py`: Thiết lập cảnh báo khi job thất bại, SQS tồn đọng hoặc Bedrock bị throttling.

Tổng kết: Với kiến trúc hiện tại, `agents/` và `hermes_integration/` là phần lõi AI; `orchestration/` và `workers/` quản lý quá trình thực thi; `rag/` và `data_pipeline/` cung cấp dữ liệu tham chiếu; còn `apps/`, `database/`, `integrations/` và `infrastructure/` giúp toàn bộ hệ thống hoạt động và triển khai trên AWS.

Cách phân chia này cũng giúp các thành viên phát triển độc lập từng module mà không phải chỉnh sửa trực tiếp source Hermes gốc.