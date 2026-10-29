
<!-- POWERCONTEXT-GUIDANCE:START -->
# Scope and Memory

## Read context

Form one short search query from the topic, entity or exact identifier supplied by the current question.
Preserve its language and technical terms; do not append guessed synonyms, translations or project names.
Leave answer-format and fallback instructions out of the query, while preserving domain qualifiers, negations,
versions and exact values. If the topic is unclear, ask for clarification instead of guessing a query.

A successful empty search means this query found no matching Memory, not that no Memory exists.
It does not authorize broader queries, another Scope, an inventory, local memory files or another retrieval channel.
Make additional searches only when the current user request authorizes them. Report failed, denied, unavailable
and unknown operations separately from a successful empty result.


- Use `pc_search` with a focused query, `mode: "auto"`, and no more than eight results.
- Use `pc_memory_list` for an explicitly requested inventory of active entries in the current Scope.
- Use `pc_memory_get` with the exact Atomic Memory artifact returned by search or list.
- Follow `next_cursor` for later inventory pages; a single page is not the whole inventory.
- Use `pc_prepare_context` when one bounded, query-specific context value is more useful than raw search hits.

## Write only on request

- Call `pc_remember` only when the user explicitly asks to persist a concise decision, constraint, current state,
  task outcome, next step, or agent note.
- For `pc_memory_revise`, read the current Atomic Memory and copy its exact artifact and real content ETag from `pc_memory_get` into `if_match`.
- For `pc_memory_retire`, use its exact current artifact and `state_version` from search, list or `pc_memory_state`. This sets recoverable `forgotten` state and preserves history.
- An exact read of an older revision does not supply a current write ETag.
- Never submit secrets or credentials.
- Pi asks for confirmation before an explicit durable mutation and refuses it without an interactive UI.

Automatic hooks attempt bounded context and Source capture; neither substitutes for an explicit Memory save.
<!-- POWERCONTEXT-GUIDANCE:END -->
