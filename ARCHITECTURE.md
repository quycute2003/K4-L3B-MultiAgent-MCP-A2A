# L3B Architecture Record

Tài liệu mô tả các quyết định có thể kiểm chứng trong source và trace. Không ghi prompt bí mật, chain-of-thought hoặc credential.

## 1. System overview

Hệ thống dùng Python async state machine với entity gate, bounded specialists và evidence ledger tập trung. Luồng nộp bài hiện tại hoàn toàn deterministic: Python kiểm soát MCP, temporal entity resolution, phép tính, policy, schema và invariant. Vì không gọi model sinh nội dung trong runtime, hệ thống không phụ thuộc mạng/model và đương nhiên không vượt hard gate 10B tham số.

```text
Input → Coordinator → Entity/Customer Agent ──┬─→ Order/Shipment Agent ──┐
                         │                     ├─→ Payment/Refund Agent ──┤
                         │                     └─→ Policy Agent ──────────┤
                         │                                                ▼
                         └──────────── Case Evidence Ledger ─────→ Conflict Resolver
                                                                          │
                                                                          ▼
                                                                      Verifier
                                                                          │
                                                        pass / one repair / fallback
                                                                          │
                                                                          ▼
                                                                        Output
```

Entity resolution hoàn tất trước fan-out. Sau khi order được resolve, các nhánh độc lập chạy bằng `asyncio.gather`. Coordinator chỉ finalize khi verifier đã chạy.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | Case input, specialist artifacts | Tạo task, giữ case state, MCP budget, tổng hợp và finalize | Không gọi domain tool trực tiếp | `AgentTask`, output draft |
| `entity-customer-agent` | Claimed order, candidates, customer hint | Resolve/reject candidate và customer context | `get_order`, `get_customer_history` | Entity/customer artifact |
| `order-shipment-agent` | Resolved order ID | Order/item/product/seller facts và shipment verdict | `get_order_items`, `get_shipment_summary`, `get_product_context` | Order/shipment artifact |
| `payment-refund-agent` | Resolved order ID | Reconcile capture/refund, tính tổng BRL | `get_payment_timeline`, `get_refund_timeline` | Payment/refund artifact |
| `policy-agent` | Policy version, normalized facts | Tải policy, resolve source conflict, đề xuất issue/action/refund | `get_policy` | Policy decision artifact |
| `verifier-agent` | Output draft, evidence ledger | Schema và cross-field verification | Không gọi MCP; gửi repair request cho coordinator | Verification report |

Tool permissions được khóa trong `evidence_context.TOOL_PERMISSIONS`. Specialist không nhận raw gateway và không ghi trực tiếp output hoặc trace file.

## 3. Entity resolution và A2A protocol

### Candidate resolution

1. Kiểm tra candidate từ input và ưu tiên `claimed_order_id` nếu hợp lệ.
2. Gọi `get_order` theo thứ tự xếp hạng.
3. Dừng khi có một order authoritative và không có tín hiệu mâu thuẫn.
4. Chỉ gọi candidate tiếp theo khi candidate đầu không tồn tại hoặc còn ambiguous.
5. Nếu không đủ evidence, trả `ambiguous` hoặc `not_found`; không chọn candidate bằng phỏng đoán.

Confidence entity dựa trên độ khớp exact ID, existence trong nguồn authoritative và độ tách biệt với candidate còn lại. Candidate bị loại vẫn được giữ trong `rejected_candidates`.

### In-process A2A

`AgentTask` là message giao việc; `AgentArtifact` là kết quả có cấu trúc. Mọi envelope có `task_id` và `case_id`. Dispatcher chỉ nhận artifact từ đúng actor đã được giao task, không nhận handoff cho task đã đóng.

```text
AgentTask:
  task_id, case_id, sender, recipient, task_type,
  entity_scope, allowed_tools, evidence_budget, payload

AgentArtifact:
  task_id, case_id, producer, status, facts, verdict,
  confidence, evidence_refs, conflicts, warnings
```

`task_assigned` được emit khi dispatcher mở task; `handoff` được emit khi artifact hợp lệ quay về coordinator. Trace chỉ chứa metadata quan sát được, không chứa reasoning riêng của model.

Không có vòng giao việc tự do giữa specialist. Chỉ coordinator tạo task. Repair loop tối đa một vòng và phải chỉ ra invariant hoặc evidence group đang thiếu.

## 4. Evidence và conflict lifecycle

Mọi MCP call đi qua `CaseEvidenceContext`:

1. Kiểm tra actor có quyền gọi tool.
2. Tự gắn `case_id`; caller không được truyền hoặc thay `case_id`.
3. Chuẩn hóa arguments và kiểm tra cache trong case.
4. Kiểm tra call budget trước network call.
5. Gọi gateway; gateway validate MCP evidence envelope.
6. Lưu nguyên `evidence_ref`, domain, tool, arguments, data và warnings.
7. Specialist gọi `consume()` khi evidence thực sự hỗ trợ kết luận.
8. `consume()` emit `tool_result_consumed` tối đa một lần cho mỗi cặp actor/evidence.
9. Output chỉ dùng evidence refs đã consume.

Cache và ledger được tạo mới cho mỗi case. Không cache `get_policy` hoặc bất kỳ evidence nào giữa các case dù arguments giống nhau.

Conflict được chuẩn hóa thành field, source list, selected source và resolution code. Policy evidence quyết định source precedence. Khi policy không đủ để phân xử, `selected_source` là `null`, case có thể chuyển sang `needs_investigation` và confidence bị giới hạn.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 1 retry nếu còn budget | Partial artifact; coordinator đánh giá evidence thiếu | Handoff `partial` hoặc `failed` |
| MCP domain/validation error | 0 | Không biến lỗi thành evidence; fail/partial task | Handoff `failed` |
| Entity not found | 0 sau khi hết candidate hợp lệ | `not_found`, `needs_investigation` | `ENTITY_NOT_FOUND` |
| Entity ambiguous | Tối đa candidate chưa kiểm tra | Không tự chọn; hạ confidence | `ENTITY_AMBIGUOUS` |
| Source conflict | 0 network retry | Dùng precedence từ policy hoặc unresolved conflict | `SOURCE_CONFLICT` |
| Invalid specialist result | 1 bounded repair, không mặc định thêm MCP call | Verifier từ chối finalize hoặc safe fallback | `ARTIFACT_INVALID` |
| Model output invalid | 1 structured-output repair | Rule-based safe fallback | `MODEL_OUTPUT_INVALID` |

Call target hiện tại là một call cho mỗi tool cần thiết và tối đa 11 audited calls/case, gồm một slot dự phòng timeout. Cache key là `(tool_name, normalized_arguments)` trong đúng case. Concurrent request trùng key dùng chung một in-flight task.

Không retry not-found, schema error hoặc policy decision. Không quét rộng customer/order ngoài scope.

## 6. Verification invariants

Verifier phải kiểm tra trước finalize:

- Output pass `l3b-output-v2.schema.json` và không có extra field.
- `case_id` khớp input và mọi evidence thuộc đúng case context.
- Resolved/rejected candidate không trùng và nằm trong input scope.
- Affected entities xuất phát từ resolved order.
- Mọi output evidence ref đã được consume và có trace linkage.
- Claim assessment chỉ trích dẫn evidence hỗ trợ claim đó.
- Shipment timeline có thứ tự thời gian hợp lệ; `timeline_complete` phản ánh field còn thiếu.
- Seller delay và logistics delay phù hợp với handoff limit và shipment events.
- `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl` được tính bằng `Decimal` và không âm.
- Tổng `refund_lines.amount_brl` bằng `recommended_refund_brl` theo quy tắc làm tròn đã chọn.
- Refund/action/case status nhất quán; action không trùng.
- Responsible party phù hợp primary issue và entity evidence.
- Source selection tuân theo policy evidence; unresolved conflict không bị che giấu.
- Confidence nằm trong `[0, 1]`; entity ambiguous, evidence thiếu hoặc conflict unresolved phải hạ/cap confidence.
- Có đủ lifecycle events theo scoring policy và `verification_completed` xảy ra trước `case_finalized`.

Confidence ban đầu được tính từ entity confidence, evidence coverage, source agreement và verifier result; model không tự đặt confidence cuối tùy ý. Công thức/cap sẽ được cố định sau khi specialist implementation hoàn tất.

## 7. Reproducibility

- Python: `>=3.11` theo `pyproject.toml`.
- Runtime model trong submission: không dùng. Specialist agents là các role có typed contract chạy bằng deterministic Python state machine.
- Model hard gate: nếu bật reviewer tùy chọn trong tương lai, model phải có tổng tham số `<=10B`; lựa chọn đã thẩm định là `Qwen/Qwen3-8B` (8.2B). Active parameters hoặc quantization không thay thế tổng tham số.
- MCP concurrency: fan-out theo specialist sau entity gate; duplicate request trong case được coalesce. Mỗi case dùng 7 calls; case refund hoặc cần quy trách nhiệm seller dùng 8 calls. `get_payment_timeline` đã chứa base payments nên không gọi lại `get_order_payments`; `get_sellers` chỉ được gọi khi kết luận cần seller-domain evidence.
- Random seed: không dùng randomness trong coordinator/calculator; model sampling gần deterministic.
- Commands: `pytest -q`, `day09 validate-inputs`, `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`.
- Secrets chỉ đọc từ `.env`; không ghi API key vào source, trace, output hoặc tài liệu.

## 8. Implementation status

Đã hoàn tất typed A2A contracts, case-scoped evidence broker, tool permission matrix, cache/in-flight de-duplication, call budget, bounded timeout retry, trace dispatcher, bốn specialist roles, temporal conflict resolution, policy builder, verifier và tích hợp `solve_case()`. Batch runner hỗ trợ per-case reconnect, atomic trace commit và `day09 run --resume` cho mạng không ổn định.
