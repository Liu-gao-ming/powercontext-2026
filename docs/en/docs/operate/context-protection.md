---
title: Context privacy and reuse
description: Configure final model admission and reuse exact prepared context with current authorization.
---

# Context privacy and reuse

PowerContext can check model requests before transmission and complete raw responses before delivery.
It can also return short-lived reuse metadata for prepared Atomic Memory. Retaining that exact preparation within
one task boundary avoids repeating retrieval and query embedding. The model still receives the retained context;
this does not remove its input tokens or guarantee a provider cache hit.

These capabilities use the existing Runtime, Scope, Artifact, and inference components. They do not require a
separate gateway process or new database tables. SQLite, SeekDB, and OceanBase use the existing authoritative
records. A third-party executor can use the same public HTTP contract.

## Configure a supported target

Privacy configuration belongs to the trusted deployment, under `InferenceConfig.protection`.
Its default mode is `disabled`, preserving existing inference behavior. `shadow` and `advisory` observe already
materialized requests only; they do not inspect responses or stop delivery. They must not be described as preventing disclosure. `enforcing` blocks or withholds when a
required check fails. Representation or context reuse options cannot disable an enforcing check.

For example, construct an inference configuration with a controlled text-only endpoint:

```python
from pydantic import SecretStr

from powercontext.builtin.inference.privacy import InferenceProtectionConfig, UpstreamProfile
from powercontext.builtin.runtime import InferenceConfig

inference = InferenceConfig(
    generation_model="openai:approved-model",
    generation_base_url="https://models.example/v1",
    generation_model_settings={"max_tokens": 1024},
    protection=InferenceProtectionConfig(
        mode="enforcing",
        forbidden_values=(SecretStr("synthetic-secret-for-a-local-test"),),
        targets={
            "coding": UpstreamProfile(
                endpoint="https://models.example/v1/chat/completions",
                model="approved-model",
                protocol="openai-chat",
                context_window_tokens=32768,
                max_output_tokens=1024,
                validated_utf8_token_bound=False,
            )
        },
    ),
)
```

The endpoint, model, and limits are illustrative. This example deliberately withholds until the deployment has
verified that its model's tokenizer and protocol template fit the conservative UTF-8 bound and configured protocol
reserve, then sets `validated_utf8_token_bound=True`. A character-count estimate is not a hard model-window bound.
Generating targets need a declared context window, output limit, and an actual positive output limit in the final
request. A payload that exceeds the bound is withheld.

Server deployments can set the same object as JSON in `POWERCONTEXT_SERVER_INFERENCE_PROTECTION`.
Keep secret values in the deployment's protected configuration. Never put real credentials in a checked-in example.
Exact model aliases returned by a provider must be explicitly listed in `response_models` when they differ from
the requested model.

The supported rules match configured exact values and optional bounded formats: `email`, `private_key`, and
`api_token`. Both raw JSON and decoded text, tool arguments, and numeric spellings are inspected. Duplicate JSON
keys, opaque image/audio/file carriers, unknown request fields, incomplete output, and unsupported targets cannot
receive complete coverage. These rules are not a general PII classifier, semantic DLP system, or tool authorization.

The bounded `openai-responses` profile requires an explicit boolean `store: false`; omitted, null, or enabled
storage is withheld. Responses otherwise stores responses by default; see the provider's
[data-retention contract](https://developers.openai.com/api/docs/guides/conversation-state#data-retention-for-model-responses).
For built-in Pydantic AI Responses models, set `openai_store: false` in the relevant model settings.
Unexpanded `item_reference` inputs, including references whose `type` is omitted or null, are withheld because
their historical content is unavailable for inspection and window accounting. Pass the complete supported
text/tool input instead of a provider-side item reference. Anthropic documents support only inline plain-text
sources; PDF/base64, URL, Files API references, and documents containing images are withheld without a request binding.

Built-in OpenAI/Anthropic clients and configured embedding/rerank paths install the checks at the final HTTP
transport. Native SDK formatting happens first. Enforcing disables SDK transport retries; output-schema retries
remain bounded by the existing stage and every actual attempt is checked. A mandatory privacy failure propagates
through advisory decision fallbacks and cannot become an empty extraction or a consumed Memory HOLD.
If a Memory write gate encounters that refusal, flush fails before publication and keeps its Source window
pending across restart so it can be retried after the policy or model output is corrected.
Custom injected inference objects without a controlled final transport are rejected in enforcing mode.

## Prepare once, validate each use

The default `POST /v1/context/prepare` response remains `schema`, `status`, `content`, and `content_bytes`.
The existing `explain` option still controls optional diagnostics. Add `include_reuse: true` for:

```json
{
  "schema": "powercontext.prepared-context-with-reuse.v1",
  "context": {
    "schema": "powercontext.prepared-context.v1",
    "status": "ready",
    "content": "<complete prepared historical context>",
    "content_bytes": 38
  },
  "reuse": {
    "schema": "powercontext.context-reuse.v1",
    "constraints": {
      "preserve": ["content", "citations", "trust", "order"],
      "refresh_on": ["permission_change", "source_ineligible", "context_boundary"]
    },
    "expires_at": "2026-10-26T12:05:00Z",
    "binding": "<opaque encrypted binding>"
  }
}
```

Placeholder content, byte count, time, and bindings are illustrative. Use the exact returned values. The Python
client returns either `PreparedContext` or `PreparedContextWithReuse`; narrow that union before reading its fields.
In process, use `runtime.context.for_scope(scope_id).prepare(request)`. Its `validate_reuse` operation checks the
retained proof without retrieval or embedding; embedded applications supply their own current authorization context.

The first reuse profile supports a single Scope and selected Atomic Memory only, without context references or code.
It records selected current revisions, lifecycle versions, and complete bounded historical dependencies. Exact
historical ancestors of a merge remain valid evidence when their owning domain permits them. Revised or forgotten
roots, revoked access, changed reference policy, expired bindings, and ineligible Sources stop reuse.
Other selected families are explicitly rejected for this option; ordinary preparation remains supported.

Bindings are authenticated and encrypted, expire after five minutes, and are limited to 64 dependencies and eight
ancestry levels. They do not grant access. Server bindings use the existing signing-secret material with a separate
cryptographic purpose and persistent Server identity; processes sharing that deployment must share the secret.
An embedded Runtime's default key is ephemeral. There is no stored prompt or response cache.

## Check the actual final payload

Call `POST /v1/context/check`, operation `check_context`, at two distinct points:

1. `before_send`: after native provider rendering and cache settings, before writing any model request.
2. `before_release`: after collecting the complete raw response, before exposing content, tool arguments, or errors.

Both use root `scope_id`, current authentication/delegation, and current Scope/resource authorization.
`scope.read` does not grant model spending or tool execution. Request fields cannot turn off deployment policy.

The versioned JSON carrier contains the original JSON text, not a reconstructed object:

```json
{
  "scope_id": "<existing Scope>",
  "phase": "before_send",
  "target_profile": "coding",
  "payload": {
    "schema": "powercontext.model-json-payload.v1",
    "headers": {"content-type": "application/json"},
    "body": "{\"model\":\"approved-model\",\"max_tokens\":1024,\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}"
  },
  "prepared_contexts": []
}
```

For each retained prepared block, include its binding and a `powercontext.model-json-text-location.v1` location:
JSON pointer, `start_byte`, and `end_byte`. Offsets address decoded UTF-8 text with half-open bounds. The whole exact
block must remain in a supported user-message text position, preserving its historical-data trust wrapper and order.
System/developer promotion, changed text, overlapping/reordered locations, and unbound prepared markers are rejected.
A target requiring prepared context cannot silently downgrade to a pure check.

Only `allow` with `coverage.complete=true` permits the corresponding action. `block` and `withhold` return HTTP
200 because a decision was made, but neither permits execution. Invalid carriers/locators return 422; authorization
failure returns 401/403; an oversized payload returns 413. If the required check is unavailable, do not send or release.
The public operation withholds when enforcing is not configured.

An allowed send returns a payload-, target-, policy-, identity-, and Scope-bound permit. Its 60-second send deadline
is separate from its five-minute response-correlation deadline. Pass it as `request_binding` to
`before_release`, with the complete response carrier and `completion: "complete"`.
Current policy, identity, and retained context dependencies are checked again. A release permit cannot authorize a
new request, and correlation never extends the send deadline. These permits do not provide exactly-once execution.

The public carrier currently supports complete JSON. The local protected transport also supports OpenAI Chat SSE
with a single choice and explicit `allow_streaming=true`. Other streaming profiles are withheld. It buffers the
complete stream before release with byte, timeout, and concurrency limits. Do not wrap an SSE
summary in the JSON carrier and call it a complete original response. Strict buffering delays first content.

HTTP over a Unix domain socket and remote HTTP(S) use the same DTOs. UDS does not replace bearer authentication.
The reference `examples/context_protection/external_executor.py` shows how an external executor uses these decisions
to control one exact JSON request and response, including byte limits, cancellation, deadlines, and local binding correlation.
The execution adapter must preserve the approved bytes, honor deadlines, stop on refusal, propagate cancellation,
and account for uncertain/failed sends. No concrete third-party gateway plugin or Rust gateway is installed by this
feature. The executor must retain each call's corresponding request binding locally; stateless Core cannot prove
which approved request an arbitrary external executor actually sent. An opaque upstream gateway limits protection to the observed endpoint; it cannot prove hidden routing,
retention, account/region isolation, or internal retries.

## Measure savings and cache behavior

### Exact input encoding by operation

`InferenceConfig.input_encoding` selects input representation by operation; unspecified operations default to `off`.
The supported operations are `atomic_memory.extract`, `atomic_memory.reconcile`, `experience.generate`, and
`experience.incubate`. Unknown or unintegrated operations fail configuration validation. Experience generation and
incubation are independent. Skill, Profile, Handoff, and Topic Memory are not integrated with this encoding strategy.

```python
InferenceConfig(input_encoding={
    "atomic_memory.extract": "shadow",
    "experience.generate": "shadow",
    "experience.incubate": "off",
})
```

The Server accepts a JSON environment variable:

```bash
POWERCONTEXT_SERVER_INFERENCE_INPUT_ENCODING='{"experience.generate":"shadow"}'
```

The existing `atomic_memory_input_encoding` setting and
`POWERCONTEXT_SERVER_INFERENCE_ATOMIC_MEMORY_INPUT_ENCODING` variable remain the fallback for the two Atomic Memory
operations. Explicit operation settings take precedence, including explicit `off`. Serialized worker configuration
preserves the same selection. Encoding changes neither Sources, Artifacts, PreparedContext, nor embedding inputs,
and adds no auxiliary model calls.

| Mode | Input sent to the model | Local diagnostics |
| --- | --- | --- |
| `off` | Original compact JSON | No candidate construction |
| `shadow` | Original compact JSON | Candidate construction, restoration checks, and cost estimates |
| `active` | Eligible smaller candidate, otherwise original | Selection or fallback reason |

`active` is experimental and needs acceptance for the target model and instruction combination. Exact JSON
reconstruction does not establish equivalent model understanding or cheaper reasoning. Actual usage can increase,
and output-budget exhaustion can prevent delivery. Observe representative inputs in `shadow`, then explicitly run
a real A/B comparison of required facts, citations, failures, and complete input plus output usage before enabling
the strategy for that deployment.
Accept every operation separately; Atomic Memory extraction results do not validate reconciliation or Experience generation.

The first codec, `shared-json-v1`, stores identical long text, metadata, or structural values once and retains
a reference at every occurrence in the ordered original tree. Project metadata often repeats in the body
projection, Source metadata, and multiple Sources. Sharing a value never merges Source identities, times,
journal positions, citations, order, or repeated occurrences. Typed object, array, and reference nodes keep
user-provided `$ref` fields distinct from internal references.

Atomic Memory retains compatible `shared-json-v1`. Experience uses `shared-json-v2`, which also recognizes
canonical JSON embedded in content strings and identical text fragments including their original line terminators.
Repeated content under distinct Source identities can be shared while retaining each identity, fragment order, and
occurrence. Noncanonical JSON strings are never reformatted. Full-envelope savings and reconstruction checks still
decide eligibility; public inputs without suitable repetition retain their original representation.

Every eligible candidate must restore the **exact original stage JSON string**, including key order, Unicode,
code, whitespace inside strings, nulls, booleans, and `1` versus `1.0`. The same estimator counts decoding
instructions, shared values, and references. Selection requires fewer bytes and savings of at least 32 estimated
tokens and at least 5%. Fewer bytes or estimated tokens do not guarantee fewer provider tokens.
Input bytes, depth, nodes, shared entries, and expanded decoding all have limits. Unsupported or oversized inputs
retain their original representation. Custom Artifact Prompts also retain the original field contract.

Atomic Memory stage budgeting and generation use the same representation selection. Experience retains its existing
evidence count/length and request limits; the executor still checks the complete request against the target window.
This configuration does not introduce an Experience-specific stage token budget. Enforced privacy protection checks the
original serialization and strictly decoded content first, including nested JSON objects and arrays inside strings.
Candidate JSON parsing/depth or size limits may cause fallback to the original representation that passed this check.
Detected forbidden content, duplicate JSON keys, and other policy failures reject the invocation; these failures
cannot become representation fallback.
This content check never grants full request admission: final privacy and target-window checks still inspect
the complete model request. Fallback never authorizes an unsafe or over-budget original request.
The codec does not remove successful log entries, truncate code, merge distinct log events by template, or write
encoded input back into authoritative records.

For in-process inspection, wrap a call with
`powercontext.builtin.inference.usage.collect_input_encodings()`.
It collects at most 128 content-free reports: stage, codec, mode, selection/fallback reason, original/candidate/
selected bytes and estimated tokens, estimator version, shared values, and references. Budget probes do not
duplicate reports. `UsageReportingStructuredGenerator` attaches the actual preparation reports to the same call's
`InferenceUsage.input_encodings` and sends them through the existing bound usage reporter. Successful results and
failures carrying known usage retain this association. Estimates remain separate from provider `input_tokens` and
`output_tokens`; never add them together or substitute one for the other. Failures without usage stay unknown and
do not create a zero-request billing record. An original-input privacy rejection precedes encoding and produces
no encoding report. The explicit diagnostic collector still works around the call.
These diagnostics belong to the current process and invocation context; they do not aggregate across workers.
Existing persistent statistics aggregate actual model usage only and do not store encoding details or provide a
complete billing ledger. Evaluate repeated metadata, equal bodies with distinct identities, unique short inputs,
and multilingual code/JSON separately.

### Actual model usage

Native cache options remain in the final provider adapter, including existing Pydantic AI model settings.
Configured TTL/retention values require explicit target approval. Changes to body, target, or relevant protocol
headers need another check. Stable prepared text helps preserve a prefix but cannot guarantee cache hits.
Provider cache-read/write counters describe provider accounting, not physical isolation or latency improvement.

Compare the same successful task with repeated preparation, exact reuse, and an existing once-prepared baseline.
Count retrieval embedding, generation input/output, auxiliary calls, warmups, errors, and retries. Cache-read input
is commonly already included in input tokens; do not add it twice. Missing usage or prices remain unknown.
An output-limit parameter is not proof of a provider's hard billing bound. Preserve actual reported usage even if
it exceeds the requested limit. Report input reduction, complete task tokens/cost, quality, and latency separately. Existing Atomic evidence-body
deduplication and recall-query reuse are baseline behavior, not new savings from this feature.

Use `tests/e2e/rfc2086_real_acceptance.py --help` for the opt-in real-service harness. It uses isolated test
identities and records content-free usage evidence. The normal regression suite also covers actual loopback
HTTP/SSE transmission and independent Server processes over UDS and HTTPS with verified certificates; those do not establish third-party provider compatibility.
