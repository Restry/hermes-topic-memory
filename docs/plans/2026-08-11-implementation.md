# Hermes Topic Memory Implementation Plan

> **For Hermes:** Execute with OMP in the approved repository, using test-first vertical slices and independent acceptance afterward.

**Goal:** Deliver a standalone plain-text topic memory engine and installable Hermes Memory Provider with sanitized topic fixtures.

**Architecture:** Canonical knowledge lives in versioned per-topic Markdown. A provider-internal long-context Reader uses a byte-stable prompt prefix and emits a <=200-character capsule. Exact/Jaccard QA cache is version-bound and disposable; completed-turn evidence drives bounded canonical updates.

**Tech Stack:** Python 3.11+, standard library first, pytest, Hermes MemoryProvider plugin contract, OpenAI-compatible Reader API.

---

### Task 1: Package and file-store foundation

**Files:** `pyproject.toml`, `src/hermes_topic_memory/models.py`, `src/hermes_topic_memory/store.py`, `tests/test_store.py`

1. Write failing tests for topic creation, readback, content fingerprint, version history, atomic replacement, and rollback.
2. Run the targeted tests and confirm failures are due to missing implementation.
3. Implement minimal dataclasses and file store using locks, fsync, and atomic rename.
4. Run targeted and full tests.
5. Commit `feat: add versioned topic text store`.

### Task 2: Registry and strict topic routing

**Files:** `src/hermes_topic_memory/router.py`, `tests/test_router.py`

1. Write failing tests for chat ID, alias, explicit topic, global fallback, ambiguity, and no implicit cross-topic lookup.
2. Verify RED.
3. Implement registry validation and deterministic routing.
4. Verify GREEN and full suite.
5. Commit `feat: add strict topic routing`.

### Task 3: Version-bound QA cache

**Files:** `src/hermes_topic_memory/cache.py`, `tests/test_cache.py`

1. Write failing tests for normalized exact hits, Jaccard hits, threshold rejection, topic isolation, and version invalidation.
2. Verify RED.
3. Implement append-only JSONL records and in-memory index; no embeddings.
4. Verify GREEN and full suite.
5. Commit `feat: add versioned question cache`.

### Task 4: Stable-prefix Reader

**Files:** `src/hermes_topic_memory/reader.py`, `src/hermes_topic_memory/engine.py`, `tests/test_reader.py`, `tests/test_engine.py`

1. Write failing tests that the stable prefix is byte-identical within a topic version, question is appended after the prefix, answer length is <=200, and overlength correction fails closed after one retry.
2. Verify RED.
3. Implement an injectable Reader protocol, OpenAI-compatible client, and recall engine.
4. Record usage/cached token metadata without requiring providers to return it.
5. Verify GREEN and full suite.
6. Commit `feat: add cached-prefix topic reader`.

### Task 5: Evidence and automatic update pipeline

**Files:** `src/hermes_topic_memory/evidence.py`, `src/hermes_topic_memory/updater.py`, `tests/test_updates.py`

1. Write failing tests for raw evidence persistence, `no_change`, validated topic update, topic mismatch rejection, no QA-to-canonical path, version bump, and rollback.
2. Verify RED.
3. Implement evidence ledger, updater protocol, deterministic validation, and atomic application.
4. Verify GREEN and full suite.
5. Commit `feat: add evidence-backed topic updates`.

### Task 6: Hermes Memory Provider plugin

**Files:** `plugin/hermes_topic_memory/__init__.py`, `plugin/plugin.yaml`, `src/hermes_topic_memory/install.py`, `tests/test_provider.py`, `tests/test_install.py`

1. Write failing tests against the real `agent.memory_provider.MemoryProvider` contract.
2. Test initialize, static prompt, prefetched capsule, background queue, turn sync, session end, pre-compress, session switch, tool schemas, and profile-safe paths.
3. Verify RED.
4. Implement plugin without changing Hermes core.
5. Unit-test installer path selection and source/runtime hashing without installing into local Hermes. Real installation is deferred to cloud acceptance.
6. Verify GREEN and full suite.
7. Commit `feat: add Hermes topic memory provider`.

### Task 7: Sanitized isolated topic datasets

**Files:** `examples/data/topic-registry.json`, `examples/data/topics/**`, `examples/evaluation/questions.jsonl`, `tests/test_fixtures.py`

1. Write failing fixture validation tests for four topics and required scenario coverage.
2. Add GSK/D360, MindMemOS, GrowLog, and global Markdown memories with overlapping distractors and superseding facts.
3. Add expected-answer, expected-empty, and cross-topic leakage questions.
4. Verify fixtures and full suite.
5. Commit `test: add isolated topic memory corpus`.

### Task 8: CLI, docs, and project context

**Files:** `src/hermes_topic_memory/cli.py`, `README.md`, `config.example.json`, `.hermes/memory.md`, `.hermes/skills/hermes-topic-memory/SKILL.md`, `.gitignore`

1. Add CLI tests for init, topics, recall, update, rollback, and validate.
2. Implement minimal CLI and installation instructions.
3. Document secret handling and isolated-profile activation; do not configure the active profile.
4. Verify CLI smoke path from a clean temporary directory.
5. Commit `docs: add operations and Hermes integration guide`.

### Task 9: Independent acceptance

1. Resolve the real project Python executable and run the full suite.
2. Run package build and install into a fresh venv.
3. Package the repository and transfer it to Azure China VM `mindmemos-brain2` (`RG-MINDMEMOS-LAB2`). Back up its Hermes config/provider state before changes.
4. On that VM only: install into an isolated cloud test profile, run Provider lifecycle E2E with mock Reader, activate the Provider, restart the test Gateway, and verify logs/health.
5. After deterministic gates pass, run at most four real Reader calls across the four topics; record cached tokens when exposed and check answer/isolation constraints.
6. Prove topic isolation, version-bound cache invalidation, completed-turn evidence persistence, and restart persistence on the cloud VM. Restore the previous cloud Provider/config after acceptance unless the user explicitly asks to keep it active.
7. Inspect Git diff/status for secrets, private data, runtime artifacts, and out-of-scope changes.
8. Commit any bounded fixes, tag `v0.1.0`, and leave the working tree clean.
