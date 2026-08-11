# Hermes Topic Memory repository context

- Canonical memory is versioned Markdown partitioned by topic; raw completed-turn evidence is the only update source.
- Routing is strict: explicit topic/alias, registered chat ID, configured default, registry default, then `global`. Never infer cross-topic recall.
- QA cache entries are disposable, version-bound, and must never become canonical evidence.
- Reader answers are at most 200 characters; one correction is allowed, then recall fails closed.
- Provider storage must use runtime `hermes_home` or an explicit configured data root. Never hardcode or mutate the active local Hermes profile.
- Local work is limited to repository tests, static checks, package builds, temporary package-only venv installs, and installer tests against ordinary temporary directories.
- Live installation, activation, Gateway restart, and E2E are deferred to Azure China VM `mindmemos-brain2` in `RG-MINDMEMOS-LAB2` with backup and rollback.
- Secrets stay in environment variables and never appear in config readback, tool output, logs, fixtures, or Git.
