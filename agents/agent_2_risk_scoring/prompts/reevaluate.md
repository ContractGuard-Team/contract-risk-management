<!-- Gửi cho model khi câu trả lời còn evidence_gaps. {gaps} = các điểm còn thiếu căn cứ, code điền lúc chạy. -->
Kết quả trên còn thiếu bằng chứng cho: {gaps}.
Gọi thêm tool phù hợp để tìm căn cứ cho đúng các điểm này rồi đánh giá lại và trả lại TOÀN BỘ kết quả theo schema.
Nếu vẫn không tìm được nguồn, giữ điểm đó trong evidence_gaps; tuyệt đối không bịa căn cứ.
