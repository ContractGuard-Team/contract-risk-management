# Plan Agent 1 — xử lý trọn hợp đồng theo từng chunk

## Mục tiêu và phạm vi

Nhận input từ context, xử lý theo prompt đã cấu hình, trả output để runtime ghi
vào cache. Duy trì ngữ cảnh giữa các chunk thuộc cùng một hợp đồng. Sau khi đã
xử lý đủ chunk và lưu bản tổng hợp thành công, giải phóng ngữ cảnh của Agent 1
để bắt đầu hợp đồng tiếp theo với memory rỗng.

Trong plan này, kết quả Agent 1 xuất ra được gọi là **output**. Output tổng hợp
sẽ trở thành input trong context của Agent 2.

Model thực hiện phân tích và trả kết quả. Việc đọc/ghi cache, kiểm tra tiến độ,
khôi phục và xóa memory thuộc runtime/orchestration; không tuyên bố đã thực hiện
các thao tác này chỉ bằng nội dung trả lời của model.

## 1. Nhận input từ context

- Nhận nội dung chunk hiện tại qua `AgentRequest.content`.
- Đọc `context.workflow`: `contract_id`, `version`, `chunk_id`, `chunk_index`,
  `chunk_count` và `source_metadata`.
- Sử dụng lịch sử hoặc summary do memory engine cung cấp để nối ngữ cảnh với
  những chunk trước trong cùng hợp đồng.
- Chunk đầu tiên của một hợp đồng mới bắt đầu bằng memory rỗng. Nếu tiếp tục job
  bị gián đoạn, runtime chỉ khôi phục snapshot thuộc đúng job, hợp đồng và phiên bản.
- Runtime kiểm tra số lượng chunk, thứ tự và ID; không tiếp tục khi thiếu chunk,
  trùng ID hoặc không xác định được hợp đồng mà chunk thuộc về.

## 2. Xử lý chunk theo prompt

- Thực hiện tác vụ được định nghĩa trong `prompts/system.md` và `prompts/classify.md`,
  cùng các ràng buộc, soul và context instructions đã cấu hình.
- Dùng ngữ cảnh trước đó để hiểu nội dung tiếp nối và các tham chiếu giữa những
  phần của cùng hợp đồng. Phân biệt nội dung thực sự có trong chunk với thông tin
  được mang theo từ memory.
- Trả kết quả cho chunk hiện tại theo schema được cấu hình; giữ liên kết với
  chunk và vị trí nguồn thông qua metadata do runtime quản lý.
- Khi một tham chiếu chưa đủ dữ liệu để giải quyết, giữ lại điểm còn thiếu trong
  ngữ cảnh theo quy tắc prompt; không tự suy đoán nội dung của chunk chưa được đọc.
- Nếu context quá dài, runtime dùng bước tóm tắt đã cấu hình. Không âm thầm bỏ
  chunk hoặc cắt nội dung chunk hiện tại để đánh dấu xử lý thành công.

## 3. Xuất output và lưu cache

- Model trả output của chunk hiện tại cho runtime.
- Runtime kiểm tra kết quả không rỗng, không bị cắt giữa chừng và đáp ứng schema
  khi schema nghiệp vụ được triển khai.
- Cập nhật ngữ cảnh với input/output của lượt vừa xử lý để phục vụ chunk kế tiếp.
- Lưu output nguyên bản và snapshot memory sau lượt xử lý vào cache của đúng
  job/hợp đồng/phiên bản/chunk. Chỉ chuyển sang chunk tiếp theo sau khi lưu thành công.
- Nếu chunk đã có kết quả hợp lệ trong cache của cùng lần phân tích, dùng lại
  kết quả và snapshot tương ứng thay vì gọi model lặp lại.

## 4. Lặp cho đến khi xử lý đủ hợp đồng

- Khôi phục ngữ cảnh từ chunk trước đã lưu thành công và nhận chunk kế tiếp.
- Lặp lại bước nhận context → xử lý theo prompt → xuất output → lưu cache.
- Trong khi còn chunk chưa xử lý, giữ memory cần thiết cho việc tiếp nối và
  phục hồi. Không xóa memory sau mỗi chunk.
- Kết quả đầy đủ luôn nằm trong cache; summary chỉ là ngữ cảnh làm việc gọn hơn,
  không thay thế hoặc xóa các output nguyên bản.

## 5. Tổng hợp và chuẩn bị context cho Agent 2

- Chỉ tổng hợp khi mọi chunk trong manifest đều có output lưu thành công.
- Tổng hợp theo thứ tự chunk, bảo toàn ID, metadata nguồn và kết quả từng chunk.
- Lưu bản tổng hợp vào cache và xác nhận Agent 2 có thể nhận dữ liệu từ đó.
- Đưa bản tổng hợp vào `context.workflow.classification` của Agent 2. Nếu cần
  tóm tắt để vừa ngân sách của Agent 2, vẫn giữ bản đầy đủ trong cache.

## 6. Hoàn tất Agent 1 và giải phóng ngữ cảnh

Điều kiện dọn memory: đã đủ output của tất cả chunk **và** bản tổng hợp đã được
lưu thành công, có thể đọc lại để chuyển cho Agent 2. Chỉ thấy chunk cuối chưa đủ
để coi hợp đồng đã hoàn tất.

Runtime thực hiện các việc sau theo phạm vi Agent 1 của đúng job/hợp đồng:

1. Xóa lịch sử turn, rolling summary và trạng thái token của memory đang làm việc.
2. Gỡ các tham chiếu tới context tạm không còn cần thiết để giải phóng RAM.
3. Dọn snapshot memory của Agent 1 trong cache khi không còn cần khôi phục giai đoạn
   phân loại; giữ nguyên output từng chunk, metadata nguồn và bản tổng hợp.
4. Không xóa memory của Agent 2/3 hoặc checkpoint workflow đang cần để chạy các
   bước tiếp theo. Việc dọn checkpoint toàn job diễn ra theo vòng đời của toàn workflow.
5. Khởi tạo memory mới, rỗng cho hợp đồng tiếp theo; không mang thông tin hợp đồng
   trước sang hợp đồng mới.

Thao tác dọn memory phải có thể gọi lại an toàn. Nếu Agent 2 lỗi sau đó, dùng bản
tổng hợp đã lưu; không cần chạy lại Agent 1 hoặc phục hồi lịch sử chunk đã dọn.

## Xử lý gián đoạn

- Model lỗi, output không hợp lệ hoặc ghi cache thất bại: không đánh dấu chunk hoàn tất.
- Hợp đồng chưa tổng hợp thành công: giữ kết quả đã lưu và snapshot cần thiết để
  tiếp tục từ phần chưa hoàn thành.
- Dọn memory thất bại: giữ nguyên kết quả đã lưu, báo lỗi thao tác dọn và cho phép
  thực hiện lại; không gọi lại model chỉ để dọn memory.

<!-- Trạng thái triển khai: plan đã xác định quy trình. Runtime hiện có xử lý chunk,
cache output/snapshot và handoff; chưa có thao tác dọn snapshot Agent 1 sau khi tổng
hợp hay dọn checkpoint toàn job. Cần triển khai các thao tác dọn bằng code riêng;
việc nạp plan này vào prompt không tự tạo ra hành vi xóa memory. -->
