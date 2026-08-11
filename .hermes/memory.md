# Hermes Topic Memory repository context

- Canonical memory is versioned Markdown partitioned by topic; raw completed-turn evidence is the only update source.
- Routing is strict: explicit topic/alias, registered chat ID, configured default, registry default, then `global`. Never infer cross-topic recall.
- QA cache entries are disposable, version-bound, and must never become canonical evidence.
- Reader answers are at most 200 characters; one correction is allowed, then recall fails closed.
- Provider storage must use runtime `hermes_home` or an explicit configured data root. Never hardcode or mutate the active local Hermes profile.
- Local work is limited to repository tests, static checks, package builds, temporary package-only venv installs, and installer tests against ordinary temporary directories.
- The source plugin stays at `plugin/hermes_topic_memory`; user-provider installs target `$TARGET_HERMES_HOME/plugins/hermes_topic_memory`, because `plugins/memory/` is only the bundled-provider source hierarchy.
- Live installation, activation, Gateway restart, and E2E are deferred to Azure China VM `mindmemos-brain2` in `RG-MINDMEMOS-LAB2` with backup and rollback.
- Secrets stay in environment variables and never appear in config readback, tool output, logs, fixtures, or Git.

- Directory imports map every Markdown file to one isolated topic, preserve valid UTF-8 source bytes, retain source provenance metadata, and activate only after staged validation. Imported registries intentionally omit a default unless an operator configures one later.
- Use `hermes-topic-memory --data-root DATA import-markdown --source-dir CORPUS --dry-run` before the real import. Existing targets require `--replace`, which preserves a sibling rollback backup.
- Use `hermes-topic-memory --data-root DATA dashboard --output DASHBOARD.html` for local inspection. The self-contained page is metadata-only and must remain ignored when generated from private data.
- Committed synthetic examples are smoke fixtures only. Long-text evaluation requires a representative, meaningful corpus kept outside Git.