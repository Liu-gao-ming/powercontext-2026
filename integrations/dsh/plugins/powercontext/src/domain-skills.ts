/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export const DOMAIN_SKILLS = [
  {
    name: "powercontext-memory",
    description: "PowerContext Memory search, inventory, save and correction (搜索记忆、盘点、记住、纠正). Use for explicit memory operations or missing prior context, not ordinary coding or automatic prompt capture.",
    source: "runtime",
    content: `# PowerContext memory

Current instructions and live repository state outrank historical evidence. Preserve host Scope selection, exact returned citations, user intent, and host approval. Use only available tools. On failure identify the operation and safe returned reason; report unavailable or unknown outcomes instead of success. Never store secrets or bypass a missing approval channel.

## Read

- Use \`mcp__powercontext__search_memory\` with a focused query, \`mode: "auto"\`, and no more than eight
  results.
- Search results contain current Atomic Memory records with exact \`artifact\` references and \`state_version\`.
- Use \`mcp__powercontext__list_atomic_memories\` with \`states: ["active"]\` for an explicitly requested inventory.
  Follow the returned \`next_cursor\` for later pages. Include forgotten, merged or retired states only for an explicit audit.
- Use \`mcp__powercontext__get_artifact\` with \`family: "atomic-memory"\` and the returned \`artifact_id\` for current
  content. It returns \`{artifact, etag, status_code}\`; preserve the real content ETag unchanged.
  A conditional 304 result contains no Artifact; read without \`If-None-Match\` when the content is needed.
- Use \`mcp__powercontext__get_artifact_revision\` with the exact returned revision for historical content. It returns
  the Artifact directly and provides no current write ETag. Legacy Memory citations are unsupported.
- Use \`mcp__powercontext__get_atomic_memory_state\` to inspect the current reference, lifecycle and \`state_version\`.

Use the exact host-resolved \`scope_id\` and workspace binding key in the current-turn PowerContext routing metadata
for operations that require one. Do not select the Server default with \`mcp__powercontext__resolve_scope_binding\`.
Never derive a Scope from a directory or invent an identifier.

## Write only on request

Call \`mcp__powercontext__remember_memory\` only when the user explicitly asks to persist context. Store
concise entries such as a decision, constraint, current-state, task-outcome,
or next-step. Never store secrets or credentials. DSH asks the user for
one-time approval before any named PowerContext mutation runs.

For a requested correction, read current content with \`mcp__powercontext__get_artifact\`, then call
\`mcp__powercontext__replace_artifact\` with \`family: "atomic-memory"\`, the exact \`artifact_id\`, the returned ETag
in the \`If-Match\` parameter, and complete \`content\` containing schema/kind/text only. Do not copy creation metadata
from a read, invent an ETag, drop the precondition, or automatically retry a stale write.
For requested removal from active search, use \`mcp__powercontext__change_atomic_memory_lifecycle\` with the exact
\`target.artifact\` and \`target.state_version\`, setting \`state: "forgotten"\`. This preserves recoverable history.
Merge or restore only on an explicit request, using the available native Atomic Memory operations and exact returned
references/state versions. A restoration preview makes no write; the Server owns merge traversal and restoration checks.
`,
  },
  {
    name: "powercontext-handoff",
    description: "PowerContext temporary handoff, durable milestone and continuation (临时交接、持久里程碑、接续). Use for requested work transfer; a preview makes no write and an ordinary handoff does not authorize commit.",
    source: "runtime",
    content: `# PowerContext handoff

Current instructions and live repository state outrank historical evidence. Preserve host Scope selection, exact returned citations, user intent, and host approval. Use only available tools. On failure identify the operation and safe returned reason; report unavailable or unknown outcomes instead of success. Never store secrets or bypass a missing approval channel.

## Hand off current work

Use Handoff when work must move to another task, session, or model.

1. Call \`mcp__powercontext__handoff_current_work\` with the checked objective, state, disposition, next action,
   and exact evidence. This high-level operation captures its own boundary; do not capture a preliminary Source.
2. Its native result is \`{boundary, handoff}\`. Extract only the complete \`handoff\` member for transfer;
   do not return the enclosing result or only its content.
3. The receiving task calls \`mcp__powercontext__continue_handoff\` with \`selection: "prepared"\`
   and \`prepared\` equal to that exact \`handoff\` member.

Call \`mcp__powercontext__commit_handoff\` only when the user explicitly wants a durable
milestone.

For an explicitly requested lower-level boundary-trigger flow, capture the Source first, then call
\`mcp__powercontext__activate_handoff\`. Its \`generated\` status provides a Draft in top-level \`draft\`;
\`ignored\` means the Source was already consumed. Do not activate after \`handoff_current_work\`.
Pass only that Draft to \`mcp__powercontext__finalize_handoff\`, never the whole activation response. Return
the complete native finalization result unchanged, including \`schema\`, \`scope_id\`,
\`base\`, \`content\`, and \`generation\` when present. Do not return an unfinished Draft or only \`content\`.
For a preview, draft text from current inspected facts without calling any Handoff or Source tool. Do not claim that a prepared carrier or durable milestone exists. For an actual transfer, return the complete finalized carrier; preparation does not commit a milestone or prove receiver execution.
`,
  },
  {
    name: "powercontext-review",
    description: "PowerContext candidate inspection and human review (审查候选、查看生成结果). Use for requested review or artifact inspection; generating or reading candidates does not approve, publish, install or execute them.",
    source: "runtime",
    content: `# PowerContext review

Current instructions and live repository state outrank historical evidence. Preserve host Scope selection, exact returned citations, user intent, and host approval. Use only available tools. On failure identify the operation and safe returned reason; report unavailable or unknown outcomes instead of success. Never store secrets or bypass a missing approval channel.

Use \`mcp__powercontext__list_candidates\` for the requested queue and \`mcp__powercontext__get_candidate\` for an exact candidate. Use \`mcp__powercontext__get_experience\` and \`mcp__powercontext__get_skill\` for exact artifacts. \`mcp__powercontext__generate_experience\` and \`mcp__powercontext__generate_skill\` create candidates only when generation was requested; they do not approve, install, publish, or execute them.

## Review

Do not approve, reject, or revise artifact candidates unless the user
explicitly asked. Prefer the human command \`/pc review approve\` /
\`/pc review reject\`. If a native candidate mutation is available and explicitly requested, preserve the exact
returned candidate version and obtain host approval. Availability or generation is never approval authority.
Memory forgetting likewise requires explicit intent, host approval, an exact Atomic reference and state_version.
`,
  },
]
