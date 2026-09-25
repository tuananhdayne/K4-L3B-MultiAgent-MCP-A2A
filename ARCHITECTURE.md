# L3B Architecture Record

## 1. System overview

`day09 run` loads the 100 L3B cases and opens one authenticated MCP session. For each case, `solve_case` creates a fresh `Investigator` and follows this observable flow:

```text
case_received (CLI)
  -> entity agent (customer history, order resolution)
  -> order / shipment / payment agents (scoped MCP evidence)
  -> policy agent (MCP policy, decision and refund)
  -> verifier agent (field consistency and trace)
  -> case_finalized (CLI)
```

The coordinator runs the actors within one async Python state machine. A2A handoffs are recorded as `task_assigned` and `handoff` trace events. The implementation uses deterministic rules and **no language model**. Thus no model above the competition's 10B parameter limit is invoked. A future LLM addition must document its exact model ID and public parameter count and keep schema/evidence verification deterministic.

## 2. Actor ownership and permissions

| Actor | Input | Tools | Output |
| --- | --- | --- | --- |
| `entity-agent` | Case candidates, customer hint | `get_customer_history`, `get_order` | Selected/rejected order IDs and confidence |
| `order-agent` | Resolved order ID | `get_order_items`, `get_product_context` | Item, seller, product context |
| `shipment-agent` | Resolved order ID | `get_shipment_summary` | Delivery verdict and late seller IDs |
| `payment-agent` | Resolved order ID | `get_payment_timeline`, conditional `get_refund_timeline` | Capture/refund totals and payment verdict |
| `policy-agent` | Case policy version and specialist findings | `get_policy` | Primary issue, parties, actions and capped refund |
| `verifier-agent` | Case output and evidence ledger | No MCP tools | Schema and consistency gate before finalization |

Only the coordinator passes the input `case_id` to `EvidenceGateway.call`. The gateway validates every successful response against `mcp-evidence-response-v1.schema.json`. All evidence references originate from `evidence_ref` in those responses. The per-case ledger and cache are discarded after the case.

## 3. Entity resolution and A2A protocol

Candidate order IDs are deduplicated from `customer_request.claimed_order_id` and `candidate_order_ids`. Customer history is used first when the input has a customer hint; an order returned by that history is preferred. The selected candidate is verified by `get_order`, and remaining candidates are recorded as rejected. If the first candidate fails, other candidates are tried. If none yields an order, the output uses `not_found` and `insufficient_evidence` rather than inventing an entity. When multiple records reuse an order ID, the workflow picks the latest purchase timestamp not later than the case's `opened_at`, then scopes item, shipment, payment and refund events to that snapshot's time window.

Handoffs are correlated by `case_id`; actors do not send free-form hidden reasoning to trace. Entity calls run in order; independent specialist and policy calls run concurrently. The per-case cache prevents duplicate calls with the same tool and arguments. The workflow has no cross-case evidence reuse.

## 4. Evidence and conflict lifecycle

Every successful MCP result used by the workflow emits `tool_result_consumed` with its original ref and tool name. The output includes refs for the order, entity, item, policy and the specialist sources relevant to the chosen issue. Claim assessments cite the output refs. The gateway's audit is the authority for provenance; the trace does not fabricate calls or refs.

An issue named in the complaint is selected when MCP evidence confirms it. If no named issue is confirmed, an observed issue can override the complaint. `get_policy` supplies allowed case status, action, responsible parties and policy refund amount. Recommended refund is capped by captured minus already refunded value. If the latest raw order or shipment record differs from the case's selected temporal snapshot, `data_conflicts` records both sources and selects the case-scoped customer record. If the customer/order link conflicts, the conflict remains unresolved and confidence is reduced.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace/result |
| --- | ---: | --- | --- |
| MCP transport failure or timeout | One tool retry and up to two retries for the affected eight-case block | Discard the block's temporary trace before retry | Only completed blocks enter the submission trace |
| Candidate not found | Try each listed candidate once | `entity_resolution.status=not_found` | Entity handoff with `entity_not_found` |
| Source conflict | No extra query by default | Record conflict and lower confidence | `data_conflicts` |
| Invalid specialist data | No extra query by default | Conservative verdict and amount | Output schema validation by CLI |

Refund timeline is queried only for refund/cancellation/unavailability complaints; shipment summary is queried only for delivery complaints. Product context is not queried because no released issue type uses product detail in its decision. Each tool/argument pair is cached within the case. Eight cases share an MCP session; a transport failure retries only that block. A new `day09 run` starts from an empty outputs/trace directory. `day09 run --resume` keeps only completed eight-case blocks, each with a finalized trace.

## 6. Verification invariants

The CLI validates every output with `l3b-output-v2.schema.json` and checks its `case_id` before writing. `day09 validate` requires exactly the IDs in `case-set.json` and validates each trace event. The packager creates a manifest from the actual case-set version, enforces size limits, scans outputs/trace for Team API Key patterns, and creates only `manifest.json`, `trace.jsonl`, and `outputs/*.json` in the ZIP.

The workflow keeps refund lines equal to the recommended refund, limits refund to available captured funds, and calculates confidence from entity match quality, required-source coverage, direct issue evidence, timeline consistency, and the amount of distinct evidence references. Resolved snapshot differences receive a small penalty; unresolved conflicts receive a larger penalty. Confidence is capped below `1.0` and recorded in `[0,1]`. The workflow also records receive/assign/consume/handoff/policy/verify/finalize lifecycle events. These checks establish format and local consistency. MCP server audit and the competition scorer decide provenance and semantic correctness after submission.

## 7. Reproducibility and run commands

Python 3.11 or newer is required. Dependencies are declared in `pyproject.toml`; this checkout was run with Python 3.13.5. No randomness or LLM is used by the workflow. Cases run in input order; independent tools within a case run concurrently. Keep `.env` private and untracked.

```text
python -m pip install -e ".[dev]"
day09 validate-inputs
day09 mcp-tools
pytest -q
day09 run
day09 validate
day09 package --output dist/submission.zip
```
