# Hermes Topic Memory operations

Use this skill when changing or operating this repository.

## Invariants

1. Read the design and implementation plan before changing Provider behavior.
2. Preserve exact topic isolation and version-bound cache behavior.
3. Persist completed turns as raw evidence before any canonical update.
4. Require evidence IDs for explicit canonical replacements; cache answers are never evidence.
5. Keep `sync_turn` and `queue_prefetch` non-blocking. `prefetch` only returns already-completed, same-session results.
6. Resolve all implicit storage from the runtime `hermes_home`; permit other roots only when explicitly configured.
7. Keep secrets in `HERMES_TOPIC_MEMORY_API_KEY`; never serialize or echo them.

## Local workflow

- Run tests with `.venv/bin/python`.
- Installer tests may target ordinary temporary directories only.
- Keep the source plugin at `plugin/hermes_topic_memory`; temporary and cloud user-provider installs target `$TARGET_HERMES_HOME/plugins/hermes_topic_memory`, never the bundled `plugins/memory/` hierarchy.
- Package installation may target a plain temporary venv only.
- Do not install into any local Hermes profile, edit local Hermes config/plugins, start Hermes, or restart a local Gateway.

## Cloud handoff

Real plugin installation and lifecycle E2E occur later on Azure China VM `mindmemos-brain2` in `RG-MINDMEMOS-LAB2`. Before changes, back up that VM's target Hermes profile and Provider state. Copy `plugin/hermes_topic_memory` to `$TARGET_HERMES_HOME/plugins/hermes_topic_memory`, use installer dry-run, verify hashes, activate explicitly, verify Gateway health and topic isolation, then restore the previous Provider unless retention is approved.
