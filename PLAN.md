# PLAN — L3B Multi-Agent MCP + A2A

> Cập nhật 25/09/2026: ban tổ chức yêu cầu mỗi học viên nộp bài riêng. Phạm Xuân Quý hiện sở hữu và thực hiện toàn bộ các workstream bên dưới; phân công bốn người được giữ lại như lịch sử kế hoạch ban đầu, không còn là dependency để hoàn tất submission.

## 1. Mục tiêu chung

Nhóm xây dựng một Python async state machine gồm coordinator và các specialist. Hệ thống phải xử lý đủ 100 case L3B, lấy evidence qua MCP đúng scope, ghi trace A2A và xuất JSON đúng contract.

Thứ tự ưu tiên khi ra quyết định:

1. Đúng entity/order cần điều tra.
2. Đúng nghiệp vụ và số tiền xử lý.
3. Evidence liên quan, tồn tại trong MCP audit và đúng `case_id`.
4. Các field nhất quán và confidence hợp lý.
5. Trace thể hiện đúng quá trình phối hợp.
6. Giảm số MCP calls thừa.

Không sửa contract công khai, tự tạo hoặc sửa `evidence_ref`, dùng evidence chéo case, đưa API key vào source/output/trace, hoặc đoán dữ liệu khi evidence còn thiếu.

## 2. Model sử dụng

### Model dự phòng đã thẩm định

- Model ID: `Qwen/Qwen3-8B`.
- Tổng số tham số: **8.2B**, đáp ứng hard gate **không quá 10B**.
- Loại: dense instruction model, hỗ trợ đa ngôn ngữ và agent/tool use.
- Nguồn xác minh: [model card chính thức của Qwen](https://huggingface.co/Qwen/Qwen3-8B).
- License: Apache-2.0.

Submission hiện tại dùng deterministic Python state machine và **không gọi model ở runtime**. Nếu cần bật reviewer sau này, chỉ chạy **một model Qwen3-8B dùng chung**; không cần tải bốn model. Quantization chỉ giảm bộ nhớ, không thay đổi số tham số dùng để xét hard gate.

### Cách triển khai đề xuất

- Máy có NVIDIA GPU đủ bộ nhớ: serve bằng vLLM qua OpenAI-compatible API.
- Máy hạn chế VRAM/RAM: dùng bản GGUF Q4_K_M với llama.cpp hoặc Ollama.
- Dùng `temperature=0` hoặc `0.1`, giới hạn output token và yêu cầu JSON theo contract nội bộ.
- Mặc định dùng non-thinking mode để output dễ parse và giảm thời gian. Chỉ policy/verifier được phép chạy thinking mode khi case có conflict hoặc entity ambiguous.
- Model không tự ghi output cuối, không tự tạo evidence và không trực tiếp quyết định số tiền bằng văn bản tự do. Python thực hiện MCP call, phép tính BRL, schema validation và kiểm tra invariant.

Trước batch run chính thức, ghi model ID, revision/quantization, tổng số tham số, runtime và nguồn xác minh vào `ARCHITECTURE.md`. Không đổi sang model khác nếu chưa kiểm tra lại hard gate ≤10B.

## 3. Kiến trúc chung

```text
Case input
   │
   ▼
Coordinator ──► Entity/Customer Agent
   │                    │
   │                    └── resolved order/customer + rejected candidates
   │
   ├──────────► Order/Shipment Agent
   │                    └── order facts + shipment verdict + evidence
   │
   ├──────────► Payment/Refund Agent
   │                    └── payment totals + refund state + evidence
   │
   ▼
Policy/Conflict Resolver
   │
   ▼
Verifier ──► validated L3B output
```

Coordinator sở hữu state của từng case, cache MCP, call budget và trace. Specialist chỉ xử lý domain được giao. Mọi MCP response được validate bởi gateway; evidence chỉ được đưa vào output khi thực sự hỗ trợ một kết luận.

## 4. Phân công bốn thành viên

### 4.1. Phạm Xuân Quý — Orchestration, MCP control và tích hợp

Đây là phần trung tâm của hệ thống.

Đầu việc:

- Thiết kế state machine trong `solve_case()` và thứ tự handoff.
- Định nghĩa contract nội bộ dùng chung cho specialist: input, facts, verdict, conflict, confidence và evidence refs.
- Xây wrapper MCP theo case: đúng `case_id`, cache theo `(tool_name, arguments)`, retry có giới hạn và đếm call.
- Phát `task_assigned`, `handoff` và điều phối `tool_result_consumed`. CLI đã phát `case_received` và `case_finalized`.
- Ghép kết quả specialist thành output L3B cuối, gọi policy/verifier và xử lý lỗi specialist.
- Tích hợp model client dùng chung; khóa model ID để không thể vô tình gọi model >10B.
- Merge code của ba thành viên, chạy batch và xử lý lỗi tích hợp.

File sở hữu:

- `src/student_agent/workflow.py`
- `src/student_agent/orchestrator.py`
- `src/student_agent/agent_contracts.py`
- `src/student_agent/evidence_context.py`
- phần orchestration trong `ARCHITECTURE.md`

Đầu ra bàn giao:

- Một case chạy xuyên suốt từ input đến output.
- Không gọi MCP trùng trong cùng case.
- Trace đúng thứ tự và đúng actor.
- Specialist có thể cắm vào mà không sửa coordinator.

### 4.2. Vũ Minh Điềm — Entity Resolution và Customer Context

Đầu việc:

- Đọc cấu trúc input và xác định tín hiệu dùng để xếp hạng candidate.
- Resolve order/customer; trả `resolved`, `ambiguous` hoặc `not_found`.
- Ghi đầy đủ `resolved_order_ids`, `rejected_candidates`, confidence và lý do dạng mã nội bộ.
- Lấy customer history sau khi có `customer_unique_id`; xác định related orders nhưng không dùng evidence chéo case.
- Đề xuất ngưỡng accept/reject candidate và cách hạ confidence khi tín hiệu mâu thuẫn.
- Viết test cho exact ID, nhiều candidate, không tìm thấy và hai candidate đồng hạng.

File sở hữu:

- `src/student_agent/agents/entity_customer.py`
- `tests/test_entity_customer.py`
- phần entity resolution trong `ARCHITECTURE.md`

Đầu ra bàn giao:

- `EntityCustomerResult` đúng contract nội bộ.
- Danh sách tool cần dùng và quyền tool của actor `entity-customer-agent`.
- Bộ case/test đại diện để Quý tích hợp.

### 4.3. Nguyễn Minh Thịnh — Order, Item và Shipment

Đầu việc:

- Phân tích order, item, product, seller và shipment cho order đã resolve.
- Dựng timeline bằng timestamp có thẩm quyền.
- Phân biệt `on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`, `conflicting` và `insufficient_evidence`.
- Xác định `late_seller_ids`, `timeline_complete` và affected entities.
- Phát hiện source conflict; chuyển cả nguồn và evidence refs cho resolver.
- Viết test cho seller giao trễ, logistics giao trễ, shipment mất/return và timeline thiếu.

File sở hữu:

- `src/student_agent/agents/order_shipment.py`
- `tests/test_order_shipment.py`
- phần order/shipment trong `ARCHITECTURE.md`

Đầu ra bàn giao:

- `OrderShipmentResult` đúng contract nội bộ.
- Timeline chuẩn hóa và evidence mapping cho từng verdict.
- Không kết luận payment/refund hoặc tự chọn policy cuối.

### 4.4. Nguyễn Hoàng Tuyến — Payment, Policy và Verifier

Đầu việc payment/refund:

- Đối chiếu capture, payment reference, refund và trạng thái hoàn tiền.
- Tính `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl` bằng Python/Decimal.
- Phân loại `reconciled`, `capture_mismatch`, `duplicate_capture`, `refund_pending`, `refund_failed`, `refunded` hoặc `insufficient_evidence`.

Đầu việc policy/verifier:

- Ánh xạ facts sang `primary_issue`, case status, ranked causes, responsible parties, refund lines và resolution actions.
- Resolve source conflict theo policy; giữ conflict unresolved khi chưa đủ căn cứ.
- Kiểm tra schema, evidence ownership, entity scope, timeline, tổng tiền, action trùng và logic giữa issue/trách nhiệm/refund.
- Hiệu chuẩn confidence; không trả 1.0 khi evidence thiếu hoặc còn conflict.
- Phát `policy_decided` và `verification_completed` sau khi các bước tương ứng thực sự hoàn tất.
- Viết test cho payment mismatch, duplicate charge, refund pending/failed và các invariant chéo field.

File sở hữu:

- `src/student_agent/agents/payment_refund.py`
- `src/student_agent/policy.py`
- `src/student_agent/verifier.py`
- `tests/test_payment_policy_verifier.py`
- phần policy, conflict và verification trong `ARCHITECTURE.md`

Đầu ra bàn giao:

- `PaymentRefundResult` và hàm build/verify output.
- Tính tiền có quy tắc làm tròn rõ ràng.
- Verifier trả danh sách lỗi cụ thể để coordinator có thể sửa hoặc đánh dấu `needs_investigation`.

## 5. Contract tích hợp giữa các thành viên

Quý tạo contract trước để ba bạn còn lại code song song. Kết quả mỗi specialist tối thiểu có:

```python
@dataclass
class SpecialistResult:
    status: str
    facts: dict[str, Any]
    verdict: str
    confidence: float
    evidence_refs: list[str]
    conflicts: list[dict[str, Any]]
    warnings: list[str]
```

Quy tắc bắt buộc:

- Specialist nhận case context và permissioned evidence client từ coordinator.
- Không specialist nào ghi trực tiếp file output hoặc `trace.jsonl`.
- Evidence client ghi nhận tool name, domain và nguyên bản `evidence_ref`.
- `evidence_refs` phải unique, đúng case và chỉ gồm evidence đã dùng.
- Tiền dùng `Decimal` trong xử lý nội bộ; chỉ chuyển sang JSON number khi tạo output.
- Không để model sinh trực tiếp field ngoài enum hoặc field không có trong schema.

## 6. Tiến độ bốn giờ

### 0–30 phút — Cả nhóm khóa đầu vào

- Cài môi trường, cấu hình `.env`, tải input L3B.
- Chạy `pytest -q`, `day09 validate-inputs`, `day09 mcp-tools`.
- Đọc schema và một số case đại diện.
- Quý tạo contract nội bộ và skeleton module.
- Chốt `Qwen/Qwen3-8B`, runtime và quantization phù hợp máy chạy.

### 30–100 phút — Làm song song

- Quý: coordinator, evidence client, cache, trace và model adapter.
- Điềm: entity/customer.
- Thịnh: order/shipment.
- Tuyến: payment/refund, policy và verifier skeleton.

Mỗi người phải có test đơn vị cho phần mình trước khi giao code.

### 100–145 phút — Tích hợp lần một

- Ghép các module vào `solve_case()`.
- Chạy một case exact ID và một case nhiều candidate.
- Sửa lỗi contract, trace, enum và evidence linkage.
- Hoàn thiện policy/verifier với dữ liệu thật từ specialist.

### 145–190 phút — Batch và sửa case khó

- Chạy `day09 run`, sau đó `day09 validate`.
- Chia danh sách case lỗi thành bốn phần để rà, nhưng lỗi phải được sửa trong module đúng owner.
- Ưu tiên lỗi entity, semantic, evidence/provenance rồi mới tối ưu call count.
- Chạy lại batch sau khi sửa; lưu ý `day09 run` xóa output và trace cũ.

### 190–225 phút — Audit chéo

- Quý kiểm tra orchestration, trace lifecycle và số MCP calls.
- Điềm kiểm tra entity/customer trên mẫu case của ba phần còn lại.
- Thịnh kiểm tra timeline và trách nhiệm seller/logistics.
- Tuyến kiểm tra totals, refund, policy, schema và confidence.
- Cả nhóm cập nhật `ARCHITECTURE.md` theo code thật.

### 225–240 phút — Đóng gói

- Chạy `pytest -q`, `day09 validate`.
- Chạy `day09 package --output dist/submission.zip`.
- Kiểm tra ZIP chỉ có `manifest.json`, `trace.jsonl` và `outputs/` đủ 100 file.
- Kiểm tra lần cuối model runtime là Qwen3-8B 8.2B và không có API key trong artifact.
- Upload lên `/l3b`, chờ scoring và chọn submission final.

## 7. Definition of Done

- Đủ đúng 100 output theo `case-set.json` và tất cả pass L3B schema.
- Mọi evidence ref tồn tại, đúng team/run/case và có `tool_result_consumed` tương ứng.
- Có đủ lifecycle event bắt buộc và thứ tự hợp lý.
- Không có total âm, refund vượt giới hạn hợp lý, action trùng hoặc trách nhiệm mâu thuẫn với issue.
- `day09 validate` và `day09 package` thành công.
- Model duy nhất trong runtime là `Qwen/Qwen3-8B`, tổng 8.2B tham số; mọi thay đổi model đều phải được kiểm tra lại trước khi chạy.
