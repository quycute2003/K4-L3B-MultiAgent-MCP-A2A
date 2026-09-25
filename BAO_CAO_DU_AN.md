# Báo cáo dự án — K4 L3B Multi-Agent MCP + A2A

Ngày đánh giá: 25/09/2026

## 1. Tóm tắt

Dự án triển khai agent Python bất đồng bộ để điều tra khiếu nại thương mại điện tử cho bài thi Day09 L3B. Mỗi case được xử lý theo chuỗi: resolve đơn hàng và ngữ cảnh khách hàng, điều tra đơn hàng/vận chuyển và thanh toán/hoàn tiền song song, nạp policy, tạo kết luận, rồi xác minh trước khi xuất JSON.

Phiên bản hiện tại dùng state machine và các quy tắc Python xác định; không gọi mô hình ngôn ngữ ở runtime. Evidence chỉ được lấy qua MCP gateway và mọi evidence dùng cho kết luận được ghi vào trace.

## 2. Kiến trúc và phạm vi đã triển khai

| Thành phần | Trách nhiệm chính |
| --- | --- |
| `workflow.py` / `orchestrator.py` | Điều phối case, tạo task A2A, handoff và chạy các specialist độc lập song song sau entity gate. |
| `agent_contracts.py` | Contract typed cho `AgentTask`, `AgentArtifact` và `EvidenceRecord`. |
| `evidence_context.py` | Broker evidence theo từng case: least privilege, tự gắn `case_id`, cache/in-flight de-duplication, budget, timeout retry và trace consumption. |
| `specialists.py` | Bốn vai trò entity/customer, order/shipment, payment/refund và policy. |
| `domain.py` / `decision.py` | Lọc lifecycle theo thời gian, tính tiền bằng `Decimal`, resolve conflict và dựng L3B output. |
| `verifier.py` | Kiểm tra schema, evidence, entity scope, totals, timeline, actions và confidence trước finalization. |
| `cli.py` | Discovery MCP, retry kết nối theo case, ghi output nguyên tử và `day09 run --resume`. |

Các thay đổi cũng hoàn thiện `ARCHITECTURE.md`, cập nhật hướng dẫn resume trong `README.md`, thêm `PLAN.md` và các unit test cho orchestration/domain/decision.

## 3. Kiểm soát chất lượng và an toàn

- Tool permission được cố định theo actor; specialist không có quyền truy cập raw gateway hoặc ghi trực tiếp output/trace.
- Cache và evidence ledger chỉ sống trong một case, tránh tái sử dụng `evidence_ref` chéo case.
- Số tiền được xử lý bằng `Decimal`, sau đó mới chuyển thành số JSON; verifier kiểm tra tổng refund và các invariant liên quan.
- Runner chỉ giữ output pass schema và trace có `case_finalized`; khi resume, artifact/traces dở dang bị loại.
- API key chỉ đọc từ `.env`; dữ liệu thi, output, trace, `.env` và artifact build bị `.gitignore` loại trừ khỏi Git.

## 4. Kết quả kiểm tra tại thời điểm báo cáo

| Lệnh | Kết quả |
| --- | --- |
| `ruff check src tests` | Đạt. |
| `day09 validate-inputs` | Đạt: `l3b / l3b-competition-v1 / 100 cases`. |
| `pytest -q` | 11 đạt, 1 không đạt trong working copy hiện tại. |

Test không đạt là `test_repository_contains_no_competition_payload`. Nguyên nhân là máy hiện có `case-set.json` và 100 JSON trong `inputs/` để chạy bài thi. Các file này đều bị ignore, không nằm trong commit. Trong một checkout sạch của repository, test release-safety này dự kiến đạt; không nên xoá dữ liệu local chỉ để làm xanh test.

## 5. Rủi ro và bước xác nhận trước khi nộp bài

1. Chưa có bằng chứng batch run với MCP thật trong lần đánh giá này, nên cần cấu hình `.env` hợp lệ rồi chạy `day09 run`, `day09 validate` và `day09 package --output dist/submission.zip`.
2. Chất lượng semantic, hiệu quả call và provenance chỉ được scorer xác nhận sau khi chạy trên evidence MCP thật; unit test hiện chủ yếu kiểm chứng contract, temporal filtering và invariant.
3. Cần kiểm tra thủ công ZIP cuối chỉ có `manifest.json`, `trace.jsonl` và 100 file `outputs/`, đồng thời không có secret.

## 6. Kết luận

Codebase đã chuyển từ starter workflow sang một triển khai multi-agent có contract, phân quyền evidence, trace A2A, verifier và cơ chế phục hồi batch. Chất lượng tĩnh và kiểm tra input hiện đạt. Dự án sẵn sàng cho bước chạy batch MCP thực tế; kết quả submission chỉ nên được xem là hoàn tất sau khi validation và packaging trên môi trường có credential thành công.
