# Hermes Topic Memory

Standalone, versioned plain-text topic memory with strict topic isolation, a provider-internal OpenAI-compatible Reader, evidence-backed canonical updates, and an installable Hermes `MemoryProvider` plugin.

## Safety boundary

Repository commands never install, activate, configure, or restart local Hermes. The installer only copies an explicitly selected plugin source directory to an explicitly selected target directory. Live Provider installation, activation, Gateway restart, and E2E belong only on the Azure China test VM `mindmemos-brain2` in `RG-MINDMEMOS-LAB2` after backing up that VM's Hermes profile.

## Install the Python package

```bash
python -m venv /tmp/hermes-topic-memory-venv
/tmp/hermes-topic-memory-venv/bin/python -m pip install .
```

This installs the data CLI and pure filesystem installer. It does not touch a Hermes profile.

## Data layout and CLI

Every command requires an explicit data root:

```text
data/
  topic-registry.json
  topics/<topic_id>/memory.md
  topics/<topic_id>/metadata.json
  topics/<topic_id>/evidence.jsonl
  topics/<topic_id>/versions/<version>-memory.md
  qa-cache/<topic_id>.jsonl
  update-proposals/<topic_id>.jsonl
```

Create and inspect a topic:

```bash
hermes-topic-memory --data-root /path/to/data init global \
  --display-name Global --body-file /path/to/global.md --default
hermes-topic-memory --data-root /path/to/data topics
hermes-topic-memory --data-root /path/to/data validate
```

Import a real Markdown corpus recursively. Each `.md` or `.markdown` file becomes exactly one topic; source text is stored byte-for-byte as UTF-8, and the registry has no implicit default topic:

```bash
hermes-topic-memory --data-root /path/to/imported-data import-markdown \
  --source-dir /path/to/markdown-corpus --dry-run
hermes-topic-memory --data-root /path/to/imported-data import-markdown \
  --source-dir /path/to/markdown-corpus
```

The dry-run writes nothing and emits aggregate JSON. Import rejects invalid UTF-8 and symlinks. Activation uses a staged directory rename; an existing target is refused unless `--replace` is supplied, in which case the prior data root is retained beside it as a rollback backup.

Import derives exact aliases from the human-facing filename, including a decorated title and parenthetical alternate name. Only aliases unique across the imported registry are retained; collisions remain unresolved rather than selecting a topic.

Generate a portable, metadata-only HTML inspection page:

```bash
hermes-topic-memory --data-root /path/to/imported-data dashboard \
  --output /path/to/topic-memory-dashboard.html
```

The HTML has inline CSS and JavaScript, remains readable without JavaScript, and includes aggregate sizes, version/cache counts, a size distribution, search, sorting, and native expandable metadata. It never embeds topic body content or previews. Keep dashboards generated from private corpora outside Git.

Reader-backed recall uses environment configuration; the API key is optional and is never written to the data root:

```bash
export HERMES_TOPIC_MEMORY_READER_ENDPOINT=https://reader.example.invalid/v1
export HERMES_TOPIC_MEMORY_READER_MODEL=reader-model
export HERMES_TOPIC_MEMORY_API_KEY='set-outside-git'
hermes-topic-memory --data-root /path/to/data recall 'What is current?' --topic global
```

Canonical replacements require completed-turn evidence IDs:

```bash
hermes-topic-memory --data-root /path/to/data update global \
  --body-file /path/to/replacement.md \
  --evidence-id EVIDENCE_ID \
  --reason 'Verified completed turn supersedes the old fact.'
hermes-topic-memory --data-root /path/to/data rollback global 1
```

## Provider configuration

The Provider reads `$HERMES_HOME/topic-memory/config.json`. Copy `config.example.json` there only on the intended isolated target profile, then adjust non-secret paths and Reader settings. Relative paths resolve under the supplied runtime `hermes_home`; absolute paths are treated as explicit external data roots. Credentials belong only in `HERMES_TOPIC_MEMORY_API_KEY`.

`initialize()` records the runtime chat ID (`chat_id`, then `user_id`/`user_id_alt`) for that session. Routing priority is an explicit tool topic (ID, alias, or full display name), a unique exact topic name mentioned in the current recall query, configured chat ID, configured default, registry default, then `global`. Unknown or ambiguous named-topic queries fail closed; routing never searches topic bodies or another topic implicitly.

The Provider exposes:

- `topic_memory_recall` — synchronous recall from one resolved topic; successful results keep `answer` and expose the identical `verbatim_answer` with `response_policy: "copy_verbatim_answer_exactly"`, which must be relayed without changing any dates, times, or years;
- `topic_memory_update` — full canonical replacement requiring verified evidence IDs;
- `topic_memory_topics` — discovery-only topic metadata and current versions, never bodies or config.

`queue_prefetch()` and `sync_turn()` submit background work and return immediately. `prefetch()` only consumes a completed result keyed to the same session and query. Session-end, pre-compression, configured batch, and shutdown boundaries flush evidence and run the updater.

## Copy-only installer

From a repository checkout, preview the exact source and target:

```bash
hermes-topic-memory-install \
  --source ./plugin/hermes_topic_memory \
  --target "$TARGET_HERMES_HOME/plugins/hermes_topic_memory" \
  --dry-run
```

Install after reviewing the dry-run:

```bash
hermes-topic-memory-install \
  --source ./plugin/hermes_topic_memory \
  --target "$TARGET_HERMES_HOME/plugins/hermes_topic_memory"
```

Hermes discovers user memory providers only at `$HERMES_HOME/plugins/<provider-name>`. The installer hashes the source and staged/runtime copies, backs up an existing target beside it, rejects symlinks and overlapping source/target trees, and leaves `config.yaml`, `.env`, Provider activation, and Gateway state unchanged.

On the Azure test VM only, the later operator must separately:

1. back up the target Hermes profile and current Provider state;
2. install this Python package into the target Hermes Python environment;
3. run installer dry-run, then copy `plugin/hermes_topic_memory` to `$TARGET_HERMES_HOME/plugins/hermes_topic_memory`;
4. save non-secret Provider config in the isolated target `HERMES_HOME` and secrets in its process environment;
5. explicitly set `memory.provider: hermes_topic_memory`;
6. restart the test Gateway and run lifecycle/isolation/persistence E2E;
7. restore the prior Provider/config unless retention is explicitly approved.

## Local verification

Allowed local checks are repository-only:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
```

The committed example topics are sanitized smoke-test fixtures, not a long-text quality benchmark. Meaningful evaluation requires a representative corpus with realistic document sizes and structure; keep any private evaluation corpus, imported data root, dashboard, and reports in an ignored local directory.

Design: `docs/superpowers/specs/2026-08-11-hermes-topic-memory-design.md`

Implementation plan: `docs/plans/2026-08-11-implementation.md`
