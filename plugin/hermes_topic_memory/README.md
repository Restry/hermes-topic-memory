# Hermes Topic Memory Provider

Standalone Hermes memory-provider entrypoint for the `hermes-topic-memory` Python package.

## Prerequisites

1. Install the repository package in the Python environment used by the target Hermes profile.
2. Create the target profile's `topic-memory/config.json` and data root; `hermes-topic-memory --data-root ...` manages canonical data.
3. Copy this source directory to `$TARGET_HERMES_HOME/plugins/hermes_topic_memory` with `hermes-topic-memory-install --source ./plugin/hermes_topic_memory --target "$TARGET_HERMES_HOME/plugins/hermes_topic_memory"`.
4. Explicitly activate `memory.provider: hermes_topic_memory` only after reviewing the target profile and taking a backup.

The installer copies files only. It never edits Hermes configuration, writes `.env`, activates a provider, or restarts a Gateway. Secrets belong in the target process environment; use `HERMES_TOPIC_MEMORY_API_KEY`, never `config.json`.

## Session routing and interactive updates

A successful single-topic recall selected by an explicit topic or a unique topic name in the query binds that topic only to the current session. Later explicit/query selection replaces the binding; otherwise follow-up evidence sync and updates reuse it. Several valid names return `multi_topic_required` rather than selecting one; unknown and conflicting references fail closed.

`topic_memory_recall_multi` accepts the verbatim query plus one to eight ordered `{topic, reason}` selections. The Agent chooses the minimum sufficient set and never auto-includes `general`. Every selection is resolved before reading; topics are recalled independently and only labeled capsules reach synthesis. The result includes `query_id`, topic versions/statuses/sources and `verbatim_answer`; relay it without changing dates, times, or source labels. A multi-topic scope is not collapsed into one session topic, and current-turn prefetch returns empty for multi-topic questions.

`topic_memory_update` accepts externally verified `evidence_ids` as before. During an active primary user turn, callers may omit them so the provider persists the stored raw user message as topic/session-scoped evidence. Exactly one content mode is allowed: `append_text` contains only the bounded, minimal user-requested Markdown addition, while `body` remains available for an intentional complete replacement. In append mode the provider reads canonical memory internally, preserves it as a byte-identical prefix, adds a deterministic Markdown newline boundary, and rejects empty, oversized, or duplicate additions before writing evidence or a new version. Agents must not call recall, expose, summarize, or reconstruct canonical memory to perform an additive update. Successful responses contain `update_mode`, versions, `evidence_ids`, and `evidence_source`, never canonical or evidence bodies.
