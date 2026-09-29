# GOV-C2-086 — FacilityInspectionIntakeAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: GOV

## Overview

FacilityInspectionIntakeAgent turns the unstructured damage reports that reach a municipal
disaster-response headquarters in the hours after an earthquake, typhoon or flood — phone
transcripts, field memos, written reports, photo captions — into a per-facility damage record, and
hands the headquarters a CSV shortlist of public facilities to inspect.

It suits work where the reports have already arrived and now need sorting: a headquarters officer
triaging a burst of incoming reports, a facilities department deciding where to send inspection
teams, or an officer recording an adjudication so that later processing reflects it.

The division of labour between the language model and the deterministic layer is the point of the
design. The model does two things only: it reads a report into candidate observations, and it
writes the prose findings. Everything that decides what is published — and everything a reader
would treat as a fact — is computed:

- **Urgency is derived deterministically** from the municipality-approved rules in
  `config/config.yaml`. The rules are evaluated top-down and the first match wins, so rule order is
  part of the approved policy; the rule that fired is reported as `applied_rule_id`, and a run with
  no matching rule reports the reserved value `U_DEFAULT`. There is no built-in rule set — a
  deployment that omits `urgency_rules` or `default_urgency` fails at start-up rather than inventing
  an urgency. **The model never authors urgency.**
- **Facility resolution is grounded, and an uncertain match is never written.** An exact registry
  name or alias match resolves without calling the model at all. Otherwise a deterministic 2-gram
  candidate set is built from the approved facility registry and the model only *scores* it; the
  winning candidate is then checked for the registry name appearing literally inside the quoted
  span. Low confidence, too small a margin between the top two candidates, or an ungrounded span
  sends the report to a review queue with a reason code — it is never written to the facility record.
- **The model does not author provenance.** A claim it returns carries a `field_path` and an
  `observation_id` and nothing else. The `quoted_span`, `source_report_id` and `kind` published
  alongside it are read back from the persisted observation, and the numbers in the prose are
  re-extracted by the template and matched against that observation's deterministic values. A
  sentence whose figures are not supported that way is removed from the finding and recorded in
  `unresolved`.
- **Contradictory reports are not auto-resolved.** In the first hours of a disaster a later report
  is not necessarily the correct one, so both observations are kept, a conflict entry is recorded,
  the facility is flagged with `conflict_flag`, and a human adjudicates through the `feedback` mode.
  Every adjudication is appended to an audit trail.
- **An empty result is not a quiet success.** "No damage" and "nothing could be processed" must not
  look alike on a disaster desk, so an ingest that wrote and queued nothing, a facility list that
  came back empty, and a facility list with no prose at all each raise an explicit
  `degradation_reason` while `status` stays `success`.

The agent does not declare buildings safe, does not issue evacuation instructions, does not set the
final inspection priority, does not optimise inspection routes, and does not analyse images — it
reads text. Those decisions stay with qualified people.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to your
own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- an approved facility registry (facility id, formal name, aliases, importance) provisioned into the
  repository, and the municipality-approved urgency rules in `config/config.yaml`. The agent has no
  write path to the registry at all — the repository port exposes `load_registry` only — because one
  forged alias row would misattribute every subsequent report;
- a `BaseLLM` implementation through `config["llm"]`. It is **optional**: without one the agent still
  runs, falling back to literal registry-name matching for extraction and returning empty prose, and
  records `llm_unavailable` in `degradation_reason`. The bundled standalone adapter builds an
  Anthropic client when an `ANTHROPIC_API_KEY` resolves through the secrets provider, and runs
  key-less otherwise;
- caller authentication in front of the entry adapter. Every request must carry a verified caller
  identity, and the two write modes additionally require a verified *middleware* identity — see
  *Input* below.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `urgency_rules` (**required**) | Municipality-approved urgency rules plus `default_urgency`. Evaluated top-down, first match wins. Absent → start-up fails |
| `resolution_confidence_threshold` (`0.85`) | Below this, the report goes to the review queue instead of the facility record |
| `resolution_margin` (`0.15`) | Top-vs-runner-up gap below this is treated as undecidable |
| `candidate_top_k` (`10`) / `candidate_min_bigram_hits` (`2`) | Deterministic candidate-set size and the absolute minimum 2-gram hits to enter it |
| `min_quoted_span_chars` (`8`) / `max_quoted_span_chars` (`200`) | Provenance span bounds. The lower bound stops a 1-character span matching vacuously; the upper bound stops a whole report body riding out as a "span" |
| `max_report_chars` (`8000`) / `max_records_per_request` (`100`) | Per-report length and per-request record count |
| `max_note_chars` (`500`) | Length of a human-authored adjudication note in `feedback` mode |
| `max_replacement_char_ratio` / `max_control_char_ratio` / `min_printable_ratio` | Text-quality gate thresholds applied to every incoming report |
| `payload_ttl_seconds` (`300`) | How long a report body stays in the request-scoped payload store. It is not a retention setting — the body is never persisted |
| `business_timezone` (`Asia/Tokyo`) | Date-boundary interpretation and display only |
| `repository_path` (`./data/gov_c2_086.sqlite3`) | File-backed SQLite store for facility status, review queue, conflicts and the audit trail |
| `assertive_phrase_action` (`downgrade`) | The only accepted value; assertive safety statements are downgraded, never silently allowed |
| `memory_enabled` (`false`) / `hitl.enabled` (`false`) | No checkpointer is attached and the graph never suspends |

The template does not fetch reports, parse PDF, Office or image files, perform OCR, schedule or
route inspections, or evaluate citizen emergency calls. Input is text.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent runs on the platform runtime. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. The Anthropic client used by the standalone adapter comes from the framework's `anthropic` extra |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework package must be installed — `src/api/server.py` imports `framework.*` and `shared.*`
at module load, so without it the server does not start at all. With the framework present, the
standalone server does start without a platform connection: it loads `config/config.yaml`, opens the
file-backed SQLite repository, and compiles the graph. Two things then differ from a platform
deployment, and both are deliberate:

- **No caller is admitted by default.** With neither upstream authentication middleware nor
  `INVOKE_AUTH_TOKEN` set, every `/invoke` request is rejected with **403** before the graph runs.
  A bearer token matching `INVOKE_AUTH_TOKEN` promotes the caller to verified-external, but only for
  the read-only `invoke` mode; `ingest` and `feedback` still require a verified middleware identity.
- **No language model is configured** unless an `ANTHROPIC_API_KEY` resolves through the secrets
  provider. The agent answers anyway, in its degraded mode, and says so in `degradation_reason`.

`/health` is unauthenticated and returns `{"status": "ok", "agent": "FacilityInspectionIntakeAgent"}`.
The test suite runs without a platform connection and without a language model.

## Input

`POST /invoke` takes a JSON body whose `mode` selects one of three mutually exclusive operations.
The body itself is the request — there is no wrapper field — and every key is allowlisted per mode,
so an unknown key is rejected rather than ignored.

| Mode | Body | Authentication |
|---|---|---|
| `ingest` | `mode`, `record_kind` (only `damage_report`), `records[]` of `{report_id, text, reported_at, channel}` | Verified middleware identity only |
| `invoke` | `mode`, optional `as_of`, optional `facility_ids[]` | Middleware identity, a valid `INVOKE_AUTH_TOKEN` bearer, or the token-gated staging default |
| `feedback` | `mode`, `decisions[]` of `{queue_id\|conflict_id, action, facility_id?, keep_observation_ids?, note?}` | Verified middleware identity only |

`channel` is one of `phone_transcript`, `field_memo`, `written_report`, `photo_caption`.
`reported_at` and `as_of` must be ISO-8601 timestamps carrying a UTC offset. A `feedback` `action`
is one of `accept_candidate`, `reject_all`, `assign_facility`, `resolve_conflict`.

Every request is partitioned by a `disaster_event_id` scope key. The caller never supplies it in the
body — naming it there is rejected as an unknown key. It comes from verified request state, or, for
a token-authenticated read, from the `STG_DEFAULT_DISASTER_EVENT_ID` environment default. A request
with no resolvable scope is rejected with 403 rather than being answered across events.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{"mode": "invoke", "facility_ids": ["FAC-A"]}'
```

Report bodies never enter the graph state. The entry adapter puts each body into a scope-bound,
short-lived payload store and passes the graph a reference; a raw string handed straight to the
graph is refused with `E_DIRECT_INVOKE_FORBIDDEN` before any node runs.

Input fails closed with stable error codes: `E_MODE_REQUIRED` / `E_MODE_UNKNOWN`, `E_UNKNOWN_KEY`,
`E_TYPE`, `E_RECORD_KIND_REQUIRED` / `E_RECORD_KIND_UNKNOWN`, `E_RECORDS_REQUIRED` /
`E_RECORDS_TOO_MANY`, `E_REPORT_TOO_LONG`, `E_TIMESTAMP_FORMAT`, `E_ENUM_UNKNOWN`,
`E_DECISION_TARGET` / `E_DECISION_FIELD`, `E_NOTE_TOO_LONG`. Inside the graph a report can also be
rejected as `E_BINARY_INPUT`, `E_TEXT_QUALITY` or `E_INJECTION_SUSPECTED` — per report, so one bad
row does not fail the whole request.

## Output

The standard AgentCore envelope is preserved: `output`, `status`, `trace_id`, `correlation_id`,
`node_history` and a normalised `error_log` (category and exception type only — never a raw provider
message).

`output` always carries `mode`, `generated_at`, `advisory_notice`, `degradation_reason` and
`rejected`, plus the fields for the mode:

| Mode | Additional fields |
|---|---|
| `ingest` | `ingest_summary` (`written_ids`, `queued_ids`, `rejected_count`), `review_queue_delta[]` |
| `invoke` | `facilities[]`, `csv_document`, `unresolved[]` |
| `feedback` | `review_queue_delta[]`, `applied_count`, `rejected_decisions[]` |

```json
{
  "mode": "invoke",
  "generated_at": "2026-08-20T00:00:00Z",
  "advisory_notice": "緊急度は自治体承認ルールによる候補値です。人間の承認前に運用指示として使用できません。",
  "degradation_reason": [],
  "rejected": [],
  "facilities": [
    {
      "facility_id": "FAC-A",
      "facility_name": "中央第一小学校",
      "urgency": "high",
      "applied_rule_id": "U3",
      "conflict_flag": false,
      "needs_confirmation": false,
      "damage_summary": "体育館で床上30cmの浸水を確認。",
      "required_actions": ["体育館の浸水箇所を点検する。"],
      "source_report_ids": ["REP-1"],
      "confidence": 1.0,
      "claims": [
        {
          "field_path": "damage_summary",
          "observation_id": "68fa8599fc9d40df",
          "source_report_id": "REP-1",
          "quoted_span": "中央第一小の体育館で床上30cmの浸水を確認しました。",
          "kind": "observation"
        }
      ]
    }
  ],
  "csv_document": "facility_id,facility_name,urgency,applied_rule_id,...",
  "unresolved": []
}
```

`facilities[]` is sorted by urgency then facility id. `csv_document` is rendered from the same
guarded values as the JSON — never from the raw draft — so the two cannot diverge, and it carries
the advisory notice on every row. Cells beginning with `=`, `+`, `-`, `@` or a control character are
prefixed with an apostrophe so a spreadsheet does not execute them.

`degradation_reason` is a sorted, allowlisted set of codes: `llm_unavailable`,
`llm_contract_violation`, `facility_registry_empty`, `partial_ingest`, `facility_result_empty`,
`brief_prose_empty`. `unresolved[]` and `review_queue_delta[]` carry a `reason_code` for every item
held back — `R_LOW_CONFIDENCE`, `R_AMBIGUOUS_CANDIDATES`, `R_UNGROUNDED_CANDIDATE`, `R_CONFLICT`,
`R_LLM_UNAVAILABLE`, `R_LLM_CONTRACT`, `E_CLAIM_UNVERIFIED`, `E_SPAN_TOO_SHORT`, `E_SPAN_TOO_LONG`,
`E_PII_OUTPUT`.

Values above are illustrative; real output depends on the reports and registry supplied.

## Security and Limitations

Every node requires verified-external trust, so an unverified caller is stopped before any node body
runs, and authentication completes before anything is written to the payload store. The two write
modes are additionally restricted to a verified middleware principal, so a standalone token or a
staging default cannot ingest reports or adjudicate the review queue.

Report text is normalised, quality-gated and **masked for personal information before it reaches the
language model**, not after. Content that reads as an instruction to the model is isolated per
report. On the way out, the generated prose is re-scanned: a sentence carrying personal information
is removed from the finding rather than published, the CSV is scanned as well, and the output gate
verifies that nothing remains — it verifies, it does not move values, so the JSON and the CSV stay
in step.

Assertive safety statements ("no inspection required", an all-clear) are **downgraded, not blocked**.
Blocking would drop the whole finding and flatten every report to the same tone; instead the sentence
is marked for confirmation and the facility carries `needs_confirmation: true`.

Known limits, recorded rather than hidden:

- **Urgency is a candidate value.** It cannot be used as an operational instruction before human
  approval, and every row of the output says so.
- **Personal-name detection in Japanese is incomplete.** The framework detector covers Latin-script
  names and labelled Kanji/Katakana names; unlabelled names, Hiragana-only values and other label
  forms are not detected. The template does not paper over this with a hand-rolled dictionary —
  it mitigates structurally instead: report originals are never persisted, and no mode returns them.
- **Report originals are not retained.** A body lives only in the request-scoped payload store; what
  persists is the masked quoted span and a digest for re-verification. "Show me last month's full
  report text" is out of reach by design — the original stays in the reporting system.
- **A quoted span proves provenance, not correctness.** It shows the finding is anchored to text a
  report actually contained; it does not establish that the assessment is right.
- **An unprovisioned registry degrades to nothing resolved.** Every report lands in the review queue
  with `facility_registry_empty` raised — visibly, not as a silent zero-result success.
- **The interim persistence backend is file-backed SQLite**, assuming a single writing process. The
  repository port is the swap point for a production store.

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be made against the primary reports by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), inner custom-topology graph over three modes
- Generation mode: `llm`, degrading deterministically when no client is injected
- HITL: not enabled — human adjudication is an ordinary asynchronous `feedback` request, not a graph suspend
- Persistence: file-backed SQLite for facility status, review queue, conflicts and the audit trail; no checkpointer
- External API clients: an Anthropic client is constructed by the standalone adapter when a key is available; otherwise none

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection and without a language model, using fakes. Running the agent
itself needs more: `uvicorn src.api.server:app` will start, but with no authentication configured
every request is refused with 403, and with no `ANTHROPIC_API_KEY` the agent runs in its degraded,
prose-free mode. Set `INVOKE_AUTH_TOKEN` and `STG_DEFAULT_DISASTER_EVENT_ID` for a read-only local
try, or front it with real authentication middleware for anything else.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the entity-resolution and provenance
contracts, the persistence governance rules and the security design. `docs/03_test_spec.md` records
the test specification and the verification results, including live-model runs and the defects they
found.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
