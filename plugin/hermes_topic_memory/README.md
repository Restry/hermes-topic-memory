# Hermes Topic Memory Provider

Standalone Hermes memory-provider entrypoint for the `hermes-topic-memory` Python package.

## Prerequisites

1. Install the repository package in the Python environment used by the target Hermes profile.
2. Create the target profile's `topic-memory/config.json` and data root; `hermes-topic-memory --data-root ...` manages canonical data.
3. Copy this source directory to `$TARGET_HERMES_HOME/plugins/hermes_topic_memory` with `hermes-topic-memory-install --source ./plugin/hermes_topic_memory --target "$TARGET_HERMES_HOME/plugins/hermes_topic_memory"`.
4. Explicitly activate `memory.provider: hermes_topic_memory` only after reviewing the target profile and taking a backup.

The installer copies files only. It never edits Hermes configuration, writes `.env`, activates a provider, or restarts a Gateway. Secrets belong in the target process environment; use `HERMES_TOPIC_MEMORY_API_KEY`, never `config.json`.

## Session routing and interactive updates

A successful recall selected by an explicit topic or a unique topic name in the query binds that topic only to the current session. Later explicit/query selection replaces the binding; otherwise follow-up evidence sync and updates reuse it. Unknown and ambiguous topic references still fail closed.

`topic_memory_update` accepts externally verified `evidence_ids` as before. During an active primary user turn, callers may omit them: the provider validates topic, body, and reason, persists the stored raw user message as topic/session-scoped evidence, then applies the replacement. Successful responses contain versions, `evidence_ids`, and `evidence_source`, never canonical or evidence bodies.
