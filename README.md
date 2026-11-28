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
  api-keys.json
  .api-keys.lock
  activity-log/<YYYY-MM>.jsonl
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
hermes-topic-memory --data-root /path/to/data recall-multi \
  'Compare the general policy with Project Alpha.' \
  --topic general --reason 'Policy context is required' \
  --topic project-alpha --reason 'The project is named'
```

`recall-multi` requires one to eight ordered `--topic`/`--reason` pairs. It resolves every ID, alias, or full display name before reading, recalls each resolved topic/version independently, synthesizes only labeled capsules, and emits structured JSON with the `query_id`, scope fingerprint, per-topic statuses/sources, and final answer. No general topic or all-topic search is added implicitly.

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

`initialize()` records the runtime chat ID (`chat_id`, then `user_id`/`user_id_alt`) for that session. Single-topic routing priority is an explicit tool topic (ID, alias, or full display name), a unique exact topic name mentioned in the current query, the session's current topic, configured chat ID, configured default, registry default, then `global`. A successful explicit/query single recall binds its resolved topic only to that session; a later explicit/query mention replaces it. Several valid names return `multi_topic_required` with ordered candidate IDs instead of selecting one. Unknown or conflicting names fail closed; routing never searches topic bodies or another topic implicitly.

The Provider exposes:

- `topic_memory_recall` — synchronous recall from one resolved topic; successful results keep `answer` and expose the identical `verbatim_answer` with `response_policy: "copy_verbatim_answer_exactly"`, which must be relayed without changing any dates, times, or years;
- `topic_memory_recall_multi` — ordered Agent-selected `{topic, reason}` entries for one to eight topics; each topic is recalled independently, then only labeled capsules are synthesized. Results expose `query_id`, versions/statuses/sources, and a verbatim answer whose dates, times, and source labels must not be changed. Choose the minimum sufficient set and never auto-include `general`;
- `topic_memory_update` — bounded `append_text` for additive changes without exposing or reconstructing the canonical body, or `body` for intentional full replacement; both modes use supplied verified raw evidence IDs or immediate evidence captured from the active stored user turn;
- `topic_memory_topics` — discovery-only topic metadata and current versions, never bodies or config.

A current query naming several valid topics is not prefetched as one arbitrary topic. Prefetch returns empty so the Agent can call `topic_memory_recall_multi`; multi-topic session scope is retained separately and never overwrites the single current-topic binding with an arbitrary member.

`queue_prefetch()` and `sync_turn()` submit background work and return immediately. `prefetch()` only consumes a completed result keyed to the same session and query. `sync_turn()` honors a new unique topic mention, otherwise writes to that session's current topic. Session-end, pre-compression, configured batch, and shutdown boundaries flush evidence and run the updater. `topic_memory_update` requires exactly one of `body` or `append_text`; append mode preserves the complete current body as a byte-identical prefix and normalizes only the new Markdown boundary. Successful explicit updates return `update_mode` (`append` or `replace`), versions, `evidence_ids`, and `evidence_source` (`supplied` or `current_turn`) without returning bodies. Expected rejections use stable safe codes: `invalid_update_arguments`, `current_turn_evidence_unavailable`, `topic_ambiguous`, `topic_unresolved`, `body_unchanged`, `append_duplicate`, `evidence_not_found`, or `evidence_topic_mismatch`; unexpected failures remain redacted as `operation_failed`.

## HTTP access and Studio keys

MCP (`POST /mcp`), REST (`/api/v1/*`), and Studio (`/studio`) share the same authenticated service. With the Reader and `HERMES_TOPIC_MEMORY_PANEL_TOKEN` configured in the process environment:

```bash
TOPIC_MEMORY_UI_MODE=studio hermes-topic-memory --data-root /path/to/data serve \
  --bind 127.0.0.1 --port 8080
```

The environment token remains the bootstrap administrator; WeChat SSO sessions are also administrators. Studio's **接入与密钥** tab issues a separate key per agent/user and generates copyable client configurations. Administrators can create topics inline, edit or roll back bodies, manage bindings, archive/restore topics, and move topics to the recycle bin. The fifth **Core 与回收站** tab initializes/edits shared Core / WhoAmI, browses and rolls back its versions, and restores or permanently deletes recycled topics.

| Role | MCP / REST access | Panel access |
| --- | --- | --- |
| `read` | `whoami`, `list_topics`, `recall` | Read-only views and recall plan/run/rerun |
| `write` | Read access plus `remember`, `bind_topic`, `dream` | Read-only views and recall; no administrative mutations |
| `admin` | All tools | Topic, Core, recycle-bin, and key administration |

Keys use `htm_` plus 32 URL-safe random characters. Creation reveals the plaintext only once; copy it into the client's secret environment before dismissing the reveal. Studio never saves the newly revealed secret in browser storage. Snippets use the current origin and revealed key, then return to environment references after dismissal/logout. Non-admin users still get the integration generator.

`data_root/api-keys.json` stores only SHA-256 digests and 12-character prefixes, protected by stable-file exclusive `flock`, atomic replacement, and `0o600` permissions. Keys support optional expiry, idempotent revocation, and last-use persistence throttled to 60 seconds. Revoked/expired keys cannot authenticate.

Omit the topic allowlist for all topics, or supply non-empty topic IDs. IDs, aliases, and bound contexts are checked before effects; automatic recall sees only allowed candidates. Scoped topic lists, overview counts, histories, reports, and activity pagination cannot expose another topic. Mixed-topic history is hidden unless every selected topic is allowed. Core / WhoAmI remains shared. Scoped administrators cannot create broader-scope keys.

Studio administrative controls are hidden for read/write users; both can read Core, and write users retain Remember/Dream. Topic and Core edits/rollbacks use version/fingerprint snapshots read when editing or confirming, so concurrent writes reject without discarding the draft. Confirmations stay inline. Recycled entries use their listed `trashed_at` as the concurrency guard, and permanent deletion requires the exact display name. Restore archived topics before editing, rolling back, or adding bindings.

`TopicCatalog` shares bounded, thread-safe snapshots by resolved data root and Beijing date, with isolated topic-scope views. Each read checks stat signatures for the registry, topic bodies/metadata/version-directory membership, and query-history/QA-cache/cache files; changes rebuild before returning. Directory validation still runs on cache hits, and corruption is not cached. Concurrent writes retry until the before/after signature converges rather than serving a stale snapshot.

Administrative endpoints:

- `GET /api/keys` → `{"items": [...]}`: public metadata and active/revoked/expired status only.
- `POST /api/keys` with `name`, `role`, optional non-empty `allowed_topics`, optional positive integer `expires_in_days` → `201 {"key": {...}, "secret": "htm_..."}`.
- `POST /api/keys/{key_id}/revoke` → `200 {"key": {...}}`; repeated revocation preserves its timestamp; a missing key returns `404`.
- `GET /api/auth/me` includes authenticated `actor`, `role`, `key_id`; the environment-token response retains `mode="token"`, managed keys use `mode="api_key"`.
- `POST /api/v1/topics/bind` accepts optional `expected_version` (positive integer) and `expected_fingerprint` (lowercase SHA-256) together. Studio sends both from a current topic snapshot; stale values return `409 concurrent_update`. Existing callers may omit the pair. Bindings do not generate a canonical body version.

Role denials return `403 forbidden`; out-of-scope targets return `403 topic_forbidden` (MCP returns a tool error). Ledger events identify the authenticated actor/key, including key changes and denials, without tokens. A valid Bearer credential takes precedence over SSO; otherwise the independent existing SSO-session fallback is preserved.

See [the integration guide](llms.txt) for exact client formats. Hermes/OMP expand `${TOPIC_MEMORY_TOKEN}`; OpenCode uses `{env:TOPIC_MEMORY_TOKEN}`; Codex uses `bearer_token_env_var = "TOPIC_MEMORY_TOKEN"`. Merge snippets into existing settings and keep secrets out of Git.

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
