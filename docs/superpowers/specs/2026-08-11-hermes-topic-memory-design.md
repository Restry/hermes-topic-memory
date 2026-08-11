# Hermes Topic Memory Design

**Date:** 2026-08-11

## Goal

Build a standalone, file-based long-term memory system for Hermes. Canonical knowledge is separated by topic, stored as versioned plain text, read by an independent long-context Reader model, and returned to Hermes as a capsule of at most 200 characters. No embedding, vector database, graph database, or reranker participates in canonical recall.

## Architecture

```text
Hermes chat/session context
  -> topic router (chat_id, explicit alias, configured default)
  -> topic memory.md + metadata version
  -> exact/Jaccard QA cache lookup bound to topic version
  -> independent Reader request with byte-stable topic prefix
  -> <=200 character capsule returned by Provider.prefetch()
```

Completed turns are persisted first as raw evidence. At session end or a bounded batch threshold, an updater classifies the session as `no_change` or `topic_update`. Only verified conversation evidence may update canonical topic text. QA cache content can never update canonical memory.

## Project boundary

New repository: `/Users/leway/Projects/hermes-topic-memory`

The existing `topic-memory-lab`, Mem0, and MindMemOS projects are read-only references and are not modified. The local active Hermes provider is never installed, switched, restarted, or used for live E2E. Local work is limited to repository unit/static tests. Provider installation, activation, Gateway restart, and live E2E run only on the Azure China MindMemOS test VM `mindmemos-brain2` in resource group `RG-MINDMEMOS-LAB2`.

## Data model

```text
data/
  topic-registry.json
  topics/<topic_id>/
    memory.md
    metadata.json
    evidence.jsonl
    versions/<version>-memory.md
  qa-cache/<topic_id>.jsonl
  update-proposals/<topic_id>.jsonl
```

`metadata.json` includes `topic_id`, display name, aliases, integer version, content fingerprint, created/updated time, and source policy. Updates use lock + temporary file + fsync + atomic rename, save the prior body in `versions/`, increment the version, and invalidate only stale QA records for that topic.

## Topic routing

Priority:

1. explicit `topic`/alias in a recall tool call;
2. current Hermes `chat_id` registered as a topic;
3. configured session/default topic;
4. `global` only for stable cross-project preferences.

No implicit cross-topic recall. Explicit `scope=all` is a separate deep-recall path and must label each source topic.

## Reader and prompt caching

The Reader uses an OpenAI-compatible endpoint configured outside Git. Each request preserves an identical prefix for a topic version:

1. stable system instruction;
2. exact topic metadata and complete `memory.md` bytes;
3. only then the new question.

The system records provider-reported cached tokens when available. A topic update necessarily creates a new prefix/version. Reader output is corrected once if over 200 characters; a second violation is a structured failure, not silent truncation.

## QA cache

- exact normalized-question hash first;
- deterministic token/shingle Jaccard second;
- no embeddings;
- record question, answer, topic ID, topic version, model, prompt version, created time, and similarity;
- ignore records whose topic version differs from the current version;
- cache is disposable and never evidence for topic updates.

## Update pipeline

`sync_turn()` appends raw evidence and queues the turn. `on_session_end()` or a bounded batch invokes an updater:

- `no_change`: canonical text unchanged; successful answers may remain in QA cache;
- `topic_update`: updater returns a complete proposed body plus evidence IDs and reason;
- deterministic validation rejects missing evidence, empty/oversized body, topic mismatch, and unchanged replacements;
- accepted update is atomic, versioned, and limited to the current topic.

Initial MVP allows automatic application only when deterministic validation passes. Every applied update is auditable and reversible.

## Hermes Provider

Standalone plugin implements:

- `initialize()` — profile-safe paths and runtime topic context;
- `system_prompt_block()` — static usage rules only;
- `prefetch()` / `queue_prefetch()` — cached background recall;
- `sync_turn()` — append evidence and queue updates;
- `on_session_end()` / `on_pre_compress()` — bounded consolidation;
- `on_session_switch()` — reset per-session buffers;
- tools: `topic_memory_recall`, `topic_memory_update`, `topic_memory_topics`.

The plugin does not modify Hermes core.

The repository source remains `plugin/hermes_topic_memory`. Hermes user-provider discovery requires installation at `$TARGET_HERMES_HOME/plugins/hermes_topic_memory`, not under the bundled-provider-only `plugins/memory/` hierarchy.

## Fixtures

Four sanitized topics: `gsk-d360`, `mindmemos`, `growlog`, and `global`. Each contains current state, historical state, a superseding update, overlapping terminology, an expected-empty case, and cross-topic distractors. Fixtures include questions and expected topic/answer constraints, not private user memory.

## Acceptance

- deterministic tests for routing, isolation, cache matching, per-topic invalidation, atomic updates, rollback, and answer limits;
- local contract/unit tests may import the Hermes ABC and exercise real discovery against a temporary `HERMES_HOME`, but must not install or activate the provider in local Hermes;
- plugin install/readback uses `$TARGET_HERMES_HOME/plugins/hermes_topic_memory`; Provider lifecycle, activation, Gateway restart, and live E2E run on the Azure China test VM only;
- mock Reader end-to-end tests plus at most four real Reader calls after deterministic gates pass;
- no secret, private memory, runtime cache, or generated data committed;
- repository clean and committed with README and project-local `.hermes` context.
