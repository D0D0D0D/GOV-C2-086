# GOV-C2-086 Test Specification

## Strategy and isolation

- Scope: unit, integration, vendored-part contract, and proof-of-boundary tests.
- Runtime: Python 3.11 / `agenticstar-agentcore[anthropic]==1.0.1`.
- Network: disabled. LLM, repository, and entry identity boundaries use injected fakes or local SQLite.
- Safety method: every security control has an accepted control case and a rejected/adversarial case. Tests do not depend on a blank implementation returning success.
- HITL/checkpoint: `memory_enabled: false`, `hitl.enabled: false`; PB-5 and PB-7 are auto-waived by the approved design. The PB-7 file is kept under its required name.

## Framework compliance

| ID | Contract | Evidence | Result |
|---|---|---|---|
| TC-01 | `State` and `DisasterIntakeWorkflowState` are flat `TypedDict` contracts | `tests/unit/test_framework_compliance.py` | PASS |
| TC-02 | Invalid internal envelope raises `SecurityViolationError` | `test_tc02_security_violation_error_fires_for_unresolvable_envelope` | PASS |
| TC-03 | No credential-like field exists in State | `test_tc03_state_has_no_credential_fields`, PB-2 scan | PASS |
| TC-04 | Nodes do not construct `InvocationContext` directly | AST test; construction exists only in entry adapter | PASS |
| TC-05 | Domain code does not duplicate `node_start/complete/error`; every node has a domain event | TC-05 AST + PB-1 module-binding patch | PASS |
| TC-06 | FunctionNode S-2 final gate cannot be overridden | `test_framework_compliance_tc06_tc07.py` | PASS |
| TC-07 | FunctionNode S-3 final gate cannot be overridden | same | PASS |
| TC-08 | All outer 3 + inner 8 nodes explicitly require `VERIFIED_EXTERNAL`; anonymous caller is denied before `execute()` | parameterized 11-node test + PB-6 | PASS |
| TC-09 | Domain S-2 hooks fail closed | outer envelope resolution/allowlist and GraphNode boundary tests | PASS |
| TC-10 | Domain S-3 hook independently validates shape, claim/numeric bindings, PII, assertion downgrade, and guard context | `test_post_process_guards.py` | PASS |
| TC-11 | Each node module emits a domain-owned event, including failure paths | `test_pb1_domain_audit.py` | PASS |

## Proof of boundary

| ID | Boundary | Evidence | Result |
|---|---|---|---|
| PB-1 | Node → domain audit binding | All 11 node module bindings patched; payload checked for PII non-disclosure | PASS |
| PB-2 | State/serialization | End-to-end ingest output + SQLite records JSON-serialize; raw sentinel and phone absent | PASS |
| PB-3 | External service port | `StatusLoadNode` receives exact frozen scope/ID list through injected repository fake; no network | PASS |
| PB-4 | Import isolation | AST scan: no Level-0 imports | PASS |
| PB-5 | Checkpoint safety | Checkpointing disabled by approved design | AUTO-WAIVED |
| PB-6 | Invoke order | S-1 → node_start → S-2 → execute → S-3 → node_complete for all discovered nodes | PASS |
| PB-7 | GraphInterrupt propagation | Non-HITL approved design; the file is kept under its required name | AUTO-WAIVED |

## Domain contract matrix

| ID | Contract and adversarial pair | Primary evidence | Result |
|---|---|---|---|
| MB-01 | Public envelope allowlist; unknown `scope/payload_ref/resolved_by`, wrong type, or absent mode rejected | server boundary integration + entry-adapter part tests | PASS |
| MB-02 | Internal envelope accepted only with exact keys; missing scope/clock/caller fails closed | `test_domain_contracts.py` | PASS |
| MB-03 | Raw report never enters serialized State/repository; mutation surface is asserted by sentinel search | PB-2 runtime test | PASS |
| MB-04 | Outer `user_input` is a digit-free 32-letter ref; raw direct invoke is rejected before compile | payload-store part tests + direct invoke unit test | PASS |
| MB-05 | Inner GraphNode returns a ref and cached dispatch resolves it | `test_main_node.py`, workflow integration | PASS |
| MB-06 | Resolved envelope is placed in custom typed keys, never `validated_input` | preprocess/unit source contract + PB-2 | PASS |
| MB-07 | Internal envelope rejects undeclared free-text fields | `test_internal_envelope_rejects_free_text_unknown_field` | PASS |
| MB-08 | Valid ref invokes; raw AgentGateway-style text yields `E_DIRECT_INVOKE_FORBIDDEN` with zero nodes | direct invoke test | PASS |
| MB-09 | Standalone default scope is token-gated/read-only; missing scope and standalone writes are denied | adapted entry-adapter part tests + server integration | PASS |
| MB-10 | Representative `_guard_context` passes framework credential scan; missing/tampered context fails | graph integration + post-process guard tests | PASS |
| MB-11 | `mint_reference()` survives scope-bound put/resolve; wrong scope fails | payload-store part + domain unit test | PASS |
| MB-12 | `record_kind=damage_report` accepted; unknown/missing rejected before writes | server validator + integration | PASS |
| MB-13 | Registry is read-only through repository port | repository API surface test | PASS |
| MB-14 | Operation policy permits middleware writes and read-only standalone token; auth denial produces no payload | entry-adapter part + server integration | PASS |
| MB-15 | `disaster_event_id` is required and preserved outer → inner → repository; other event is invisible | repository and workflow tests | PASS |
| MB-16 | Preprocess error runs main passthrough but no inner node; postprocess is absent from history | `test_preprocess_error_skips_inner_nodes...` | PASS |
| MB-17 | Quality → NFKC → PII mask → injection evaluation → LLM; provider mock sees only masked text | `test_pii_masking_happens_before_llm_provider_egress` | PASS |
| MB-18 | Plain text accepted; PDF/ZIP/malformed/quality thresholds rejected | ingest-file-sanitizer part + binary integration | PASS |
| MB-19 | High-confidence injected row is rejected while a good sibling row persists | `test_injection_report_isolated_while_other_row_continues` | PASS |
| MB-20 | Exact alias resolves at confidence 1.0 without LLM call | entity unit test | PASS |
| MB-21 | Unspaced Japanese 2-gram candidate generation returns the intended facility | entity unit test | PASS |
| MB-22 | Resolution is keyed by `extraction_item_id`; wrong-item grounding cannot borrow another facility span | entity grounding tests | PASS |
| MB-23 | Candidate must be in deterministic set and contain that registry name/alias in the same literal span; NaN/out-of-range rejected; short/long spans retain `E_SPAN_TOO_SHORT` / `E_SPAN_TOO_LONG` | entity and span reason-code tests | PASS |
| MB-24 | Duplicate is idempotent; access-only change gets a different ID and conflict; both observations remain | repository + reconcile tests | PASS |
| MB-25 | Latest group, heaviest severity, access OR, config order, default rule, future exclusion | urgency/as-of tests + config tests | PASS |
| MB-26 | `3棟`, full-width digits, adjacent yen/time, kanji numerals parse without `\w` lookaround; 1 億 rejected | numeric extractor parameterized tests | PASS |
| MB-27 | Claim source/observation/span/digest/asserted values match forward; uncited numeric sentence removed reverse; claim span bounds remain distinguishable in `unresolved[]` | post-process claim/span tests | PASS |
| MB-28 | Assertive wording in findings/actions is downgraded once per sentence before CSV | assertive + post-process tests | PASS |
| MB-29 | S-3 recomputes claim/numeric coverage and rejects post-execute tampering | independent tamper test | PASS |
| MB-30 | S-3 hook leaves formatted JSON byte-equivalent and only removes `_guard_context` | hook immutability test | PASS |
| MB-31 | LLM PII sentence moves to unresolved before CSV; neither JSON prose nor CSV contains it | PII placement test | PASS |
| MB-32 | Declared free-text surfaces are recursively scanned; undeclared output key fails shape allowlist | S-3 free-text/shape tests | PASS |
| MB-33 | Same input produces byte-identical RFC 4180 CSV with fixed columns and advisory notice | CSV test | PASS |
| MB-34 | Formula-leading facility/prose cells receive a leading apostrophe | CSV formula test | PASS |
| MB-35 | Feedback resolves only open owned target; actor comes from caller; repeated/invalid target rejected | feedback integration + feedback-intake part tests | PASS |
| MB-36 | Note raw is confined to audit; safe value is NFKC → length → PII mask → injection reject | feedback integration/unit + part tests | PASS |
| MB-37 | File SQLite survives repository re-instantiation | durability test | PASS |
| MB-38 | `apply_ingest/apply_feedback` are atomic; injected audit failure rolls status back; no per-row write API | rollback + API tests | PASS |
| MB-39 | Persistent records with undeclared keys abort the whole write | repository schema test | PASS |
| MB-40 | Repository scope/empty/idempotency/conflict/durability/rollback contract is backend-local | repository contract suite | PASS |
| MB-41 | `llm=None` compiles and degrades; non-BaseLLM string fails startup | config tests + workflow | PASS |
| MB-42 | Error path is composed by `Graph.get_output`; postprocess does not run | error-envelope integration | PASS |
| MB-43 | One `status_load` as-of projection feeds all invoke stages; future observation excluded | as-of unit test | PASS |
| MB-44 | Full-width email is normalized before PII detection | feedback note unit test | PASS |
| MB-45 | Config rejects bool-as-int, range, unknown keys, min/max, unknown rule key, duplicate rule ID | config parameterized tests | PASS |
| MB-46 | Nodes never call wall clock; all generated/persisted timestamps use request clock | source invariant + workflow assertions | PASS |
| MB-47 | Deterministic IDs repeat for identical semantic fields and change with `access_blocked` | ID/repository tests | PASS |
| MB-48 | Valid JSON LLM contract is accepted; non-object/unknown shapes degrade or queue without provider text leakage | llm-draft part + domain tests | PASS |
| MB-49 | No-LLM mode persists deterministic matches, keeps empty summary/action fields, and records `llm_unavailable` | workflow integration | PASS |
| MB-50 | Empty registry queues reports and records `facility_registry_empty` without silent zero-success | integration | PASS |
| MB-51 | Degradation values are allowlisted, de-duplicated, and sorted; unknown value raises | output/config helpers + integration | PASS |
| MB-52 | Error log exposes only allowlisted category/type and de-duplicates; raw provider text is absent | error normalization unit test | PASS |
| MB-53 | `mask_pii` import path is `framework.security.pii_masking` | import smoke via runtime/unit collection | PASS |
| MB-54 | Trust gate covers all 11 nodes | TC-08 | PASS |
| MB-55 | Domain audit covers every node and failure/degrade paths without PII payload | PB-1 | PASS |
| MB-56 | Invoke order calls every discovered node's real S-2/execute/S-3; no-op S-3 mutation violates the shared PB-6 postcondition | PB-6 + mutation-kill test | PASS |
| MB-57 | State is primitive/JSON-safe and raw-free | PB-2 | PASS |
| MB-58 | Checkpointing disabled | PB-5 | AUTO-WAIVED |
| MB-59 | HITL disabled; no interrupt/resume endpoint | PB-7 + server startup test | AUTO-WAIVED |
| MB-60 | Manifest class imports; IDs/config/secret declaration align; no placeholders outside examples | manifest tests | PASS |
| MB-61 | `TestClient` lifespan runs and exposes urgency config, repository, and payload store | startup integration | PASS |
| MB-62 | Real provider ingest→invoke with non-empty generated brief | revised two-key claim contract must be rerun through the live entry path (see *Live provider verification*) | REVALIDATION REQUIRED |
| MB-63 | All three LLM prompts expose their strict output contract: extraction enums/span/index/range, resolution item/candidate allowlist, and brief two-key references | `test_llm_prompt_contracts.py` | PASS |
| MB-64 | Non-empty ingest input with zero written and queued IDs records `partial_ingest`; a successful write does not | workflow integration | PASS |
| MB-65 | Non-empty facility target with zero rendered facilities records `facility_result_empty`; a non-empty result does not | workflow integration | PASS |
| MB-66 | LLM `finding` field paths are mapped once to public `damage_summary`; the prompt vocabulary and S-3 matcher contract are tested together | prompt contract + post-process guard tests | PASS |
| MB-67 | Unverified numeric prose is removed into `unresolved` before CSV while status remains success; post-execute numeric injection is still rejected by S-3 | post-process guard control/mutation pair | PASS |
| MB-68 | Ingest and invoke use distinct session IDs; persisted span/digest provenance remains valid without payload-store access | transient payload lifecycle integration | PASS |
| MB-69 | File repository close/reopen preserves facility status, review queue, and audit record; a new store/session returns the same invoke output | restart integration + SQLite audit inspection | PASS |
| MB-70 | Payload refs resolve only inside their bound request window; another session, TTL expiry, and another scope fail closed | payload lifecycle integration | PASS |
| MB-71 | Persistent documents contain neither the complete report nor `payload_ref`; replay after intake fails because the ref was consumed | non-retention integration | PASS |
| MB-72 | Reintroducing `payload_ref` anywhere in a persistent write batch is rejected as `E_SCHEMA_UNKNOWN_FIELD` | persistent allowlist mutation test | PASS |
| MB-73 | Brief claims containing only `field_path` and `observation_id` are accepted; provenance and kind are derived from the persisted observation | prompt contract + post-process guard tests | PASS |
| MB-74 | A model-supplied `quoted_span` is an extra claim key and produces `E_LLM_CONTRACT` | brief contract failure test | PASS |
| MB-75 | An unknown `observation_id` removes only that claim; prose numeric bindings are extracted by the template and remain independently S-3 checked | post-process guard tests | PASS |
| MB-76 | Two non-empty facilities with no prose record `brief_prose_empty`; one non-empty finding suppresses that degradation | post-process degradation control pair | PASS |

## Execution summary

- Execution date: 2026-08-20 (Asia/Tokyo)
- `pytest tests/ -q`: **435 passed, 3 skipped, 0 failed** (PB-5 ×1 and PB-7 ×2 approved auto-waivers)
- `pytest tests/proof_of_boundary/ -q`: **23 passed, 3 skipped, 0 failed**
- External connections: 0
- Real LLM turn: **revalidation required** for the revised two-key claim contract.

## Live provider verification

Automated suites inject a fake `BaseLLM`, and a fake returns a well-formed object by construction.
That hides any defect where the prompt fails to tell a real model what to produce, and any defect
where the verifier expects something a real model will not reproduce. This template was therefore
driven end to end against a real provider, repeatedly, until it held.

| Item | Value |
|---|---|
| Date | 2026-08-20 |
| How | A real provider client injected as `config["llm"]`, driving `ingest` then `invoke` through the real entry path (an `envelope_ref` minted by the payload store). **`ingest` and `invoke` run under different session ids**, because reusing one fixed session hides every cross-request defect. A generic shared harness cannot drive this template — it passes a raw string to `invoke()`, which is refused with `E_DIRECT_INVOKE_FORBIDDEN` by design. |
| Input | Two free-text damage reports naming facilities by an alias, including a flooding depth, a count of fallen panels, and an access obstruction |
| Result | `ingest` (session A): observations written, one held as `R_UNGROUNDED_CANDIDATE`, none rejected. `invoke` (session B): 2 facilities with non-empty prose, 2 with surviving claims, `unresolved` empty, urgency derived deterministically (`U3` / `U5`), a well-formed CSV, `status: success`. Neither the original report text nor any `payload_ref` appears anywhere in the persisted records. |

**Defects these turns found that the fake could not** (each fixed, each covered by a test that needs
no live provider):

1. The extraction prompt declared only the *key names* of the schema, never the permitted enum
   values, so a real model returned natural-language categories and **every observation was rejected**.
2. An all-rejected ingest and an empty facility list were returned as `success` with an empty
   `degradation_reason` — a reader could not tell "no damage" from "nothing could be processed".
3. `field_path` used `finding` in the model contract and `damage_summary` in the verifier, so every
   figure in `damage_summary` was judged uncited and the whole `invoke` failed.
4. The reverse claim check raised instead of removing the unsupported sentence, so one unsupported
   sentence failed the entire request.
5. **Only visible once ingest and invoke used different sessions**: the verifier required the model's
   `quoted_span` to match the persisted one exactly. A real model summarises and re-punctuates, so
   every claim was dropped and all prose disappeared while `status` stayed `success`.

Finding 5 is the reason the model no longer authors provenance material at all. Three consecutive
live turns failed the same way — the model's output not matching what the verifier expected — and
each earlier fix made the prompt heavier rather than removing the dependency. The model now writes
prose and an `observation_id`; the span, source id and kind are read back from the persisted record,
and asserted values are extracted from the prose by the template. There is no longer any model-authored
value to match against.

## Marketplace verification (AGENTIC STAR, 2026-09-29)

Run on the Marketplace through the entry adapter (`cli.py`), image built from `develop` at `da5664b6`.

| Case | Input | Result |
|---|---|---|
| Read path | `{}` | `status: success`, `mode: invoke`, `facilities: []`, header-only CSV, `degradation_reason: []` — the write modes are not reachable from the Marketplace, so no records exist there |
| Rejected input | a plain-text sentence | `agent failed: ValueError: E_TYPE` delivered to the chat |

Before registration the same image was run locally with the registered Azure values (`gpt-5.4-mini`) in
the runner's order (provision, compile, threaded invoke): identical output, a pasted `{"mode": "ingest"}`
was processed as `invoke`, and a canary placed in the conversation history did not reach the result.
With placeholder (unreachable) model credentials the same call reported `llm_contract_violation`;
an unreachable model should be reported as `llm_unavailable` — recorded as a follow-up.

