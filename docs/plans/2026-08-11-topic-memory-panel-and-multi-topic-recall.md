# Topic Memory Panel and Multi-Topic Recall Implementation Plan

> **For Hermes:** Implement this plan only through the user's OMP workflow. Use strict TDD, one focused commit per task, independent verification after each work package, and cloud-only live Hermes E2E.

**Goal:** Deliver first-class multi-topic recall and an authenticated MM-style Topic Memory Panel for topic discovery, question history, recall inspection, canonical memory maintenance, versioning and recycle-bin operations.

**Architecture:** Keep canonical topic files and per-topic QA caches unchanged. Add a durable query-history ledger, a multi-topic orchestrator that performs independent per-topic recalls before a labeled synthesis, lifecycle-aware topic storage, and a dependency-light Python Panel server serving JSON APIs plus static HTML/CSS/JS assets. Preserve the existing single-topic Provider contract and static dashboard export.

**Tech Stack:** Python 3.11+, standard library HTTP server, file-backed JSON/JSONL/Markdown storage, existing Reader/OpenAI-compatible client, vanilla HTML/CSS/JavaScript and SVG charts, pytest, OMP, Azure China isolated Hermes profile.

**Authoritative design:** `docs/superpowers/specs/2026-08-11-topic-memory-panel-and-multi-topic-recall-design.md`

---

## Work-package boundaries

```text
WP1  query ledger + metrics
  -> WP2 multi-topic core + Provider contract
  -> WP3 topic lifecycle and safe write services
  -> WP4 Panel read/auth API
  -> WP5 MM-style Panel UI
  -> WP6 Panel write/recycle API + UI
  -> WP7 build, isolated cloud deploy, joined E2E
```

- WP1–WP6 modify only the repository and run local deterministic tests.
- WP7 deploys only after all local gates pass.
- Local active Hermes is never installed, activated, restarted or used for live E2E.
- Real 51-topic production data is read-only during automated acceptance; mutation E2E uses a disposable cloned profile/data root.
- No graph, Dreaming, token administration, model routing, embeddings, rerank administration or arbitrary shell endpoint is included.

---

### Task 1: Add a durable query-history ledger

**Objective:** Persist every real single- or multi-topic question independently from disposable QA cache files.

**Files:**
- Create: `src/hermes_topic_memory/query_history.py`
- Create: `tests/test_query_history.py`
- Modify: `src/hermes_topic_memory/__init__.py`

**Step 1: Write failing record round-trip tests**

Add tests covering:

```python
def test_query_record_round_trip_preserves_topic_versions_and_statuses(tmp_path):
    store = QueryHistoryStore(tmp_path)
    record = QueryRecord.create(
        question="compare alpha and beta",
        selected_topics=(
            TopicQueryResult("alpha", 2, "answered", "A", "cache", "exact", 3),
            TopicQueryResult("beta", 4, "no_fact", None, "reader", None, 9),
        ),
        final_answer="A only",
        selection_reasons={"alpha": "named", "beta": "named"},
        source="panel",
        session_ref="session-hash",
        total_latency_ms=14,
    )
    saved = store.append(record)
    assert store.get(saved.query_id) == saved
    assert store.list(topic_id="beta") == (saved,)
```

Also prove:

- UTC timestamps select the correct `YYYY-MM.jsonl` file;
- records are append-only, `0600`, fsynced and locked;
- pagination is newest-first and stable;
- invalid topic IDs, empty questions, duplicate topic IDs and body-sized answers fail;
- raw session IDs are hashed before persistence;
- malformed JSONL reports file and line without leaking neighboring content.

**Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_query_history.py -q
```

Expected: collection/import failure because `query_history.py` does not exist.

**Step 3: Implement immutable records and store**

Implement:

```python
@dataclass(frozen=True, slots=True)
class TopicQueryResult:
    topic_id: str
    topic_version: int
    status: Literal["answered", "no_fact", "error"]
    answer: str | None
    source: Literal["cache", "reader", "failure"]
    match_kind: str | None
    latency_ms: int

@dataclass(frozen=True, slots=True)
class QueryRecord:
    query_id: str
    question: str
    created_at: str
    selected_topics: tuple[TopicQueryResult, ...]
    final_answer: str | None
    selection_reasons: dict[str, str]
    source: Literal["hermes", "panel", "cli"]
    session_ref: str | None
    total_latency_ms: int
```

`QueryHistoryStore.append()` must lock one month file, append one compact JSON line, fsync the file and directory, and update no secondary mutable index. Pagination may scan monthly files backwards because the corpus is bounded.

**Step 4: Run tests to verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_query_history.py -q
```

Expected: all query-history tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/query_history.py src/hermes_topic_memory/__init__.py tests/test_query_history.py
git commit -m "feat: add durable topic query history"
```

---

### Task 2: Add query-ledger metrics and topic summaries

**Objective:** Produce the exact counts and time-series required by the Panel home page without reading canonical bodies into responses.

**Files:**
- Create: `src/hermes_topic_memory/catalog.py`
- Create: `tests/test_catalog.py`
- Modify: `src/hermes_topic_memory/dashboard.py`
- Modify: `tests/test_dashboard.py`

**Step 1: Write failing catalog tests**

Fixture: three active topics, five query records, two current cache entries and one stale cache entry. Lifecycle-aware active/archived counts are added in Task 6 after the lifecycle state exists.

Assertions:

```python
snapshot = TopicCatalog(data_root).snapshot(now=fixed_beijing_time)
assert snapshot.active_topic_count == 3
assert snapshot.total_question_count == 5
assert snapshot.active_cache_count == 2
assert snapshot.questions_last_30_days == 4
assert snapshot.topic("alpha").question_count == 3
assert snapshot.topic("alpha").active_cache_count == 1
assert len(snapshot.daily_questions) == 30
```

Prove Beijing calendar-day grouping, zero-filled days, stale-version cache exclusion, and no topic body in `to_public_dict()`.

**Step 2: Run focused RED test**

```bash
.venv/bin/python -m pytest tests/test_catalog.py tests/test_dashboard.py -q
```

Expected: missing `TopicCatalog` and new fields.

**Step 3: Implement catalog snapshot**

Build catalog data from:

- registry + metadata;
- current version list;
- `QuestionCache.records(topic_id)` filtered to current version;
- `QueryHistoryStore` records;
- lifecycle state.

Do not load `memory.md` for the home-page public snapshot except when size/fingerprint metadata is already insufficient; body content must never enter returned JSON.

Keep `generate_dashboard()` as offline export, but source its counts from the shared catalog so static and server views do not drift.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_catalog.py tests/test_dashboard.py -q
```

Expected: catalog and dashboard tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/catalog.py src/hermes_topic_memory/dashboard.py tests/test_catalog.py tests/test_dashboard.py
git commit -m "feat: add topic catalog metrics"
```

---

### Task 3: Implement the multi-topic recall core

**Objective:** Recall 1–8 topics independently and synthesize only their labeled capsules.

**Files:**
- Create: `src/hermes_topic_memory/multi_recall.py`
- Create: `tests/test_multi_recall.py`
- Modify: `src/hermes_topic_memory/engine.py`
- Modify: `tests/test_engine.py`

**Step 1: Write failing isolation tests**

Use a recording fake Reader and topic bodies containing deliberate cross-topic distractors.

Required cases:

```python
result = engine.recall_multi(
    question="What do alpha and beta say?",
    selections=(
        TopicSelection("alpha", "named in question"),
        TopicSelection("beta", "named in question"),
    ),
)
assert [item.topic_id for item in result.topic_results] == ["alpha", "beta"]
assert recording_reader.topic_ids == ["alpha", "beta"]
assert "ALPHA_BODY" not in recording_synthesizer.last_prompt
assert "BETA_BODY" not in recording_synthesizer.last_prompt
assert "answer from alpha" in recording_synthesizer.last_prompt
assert "answer from beta" in recording_synthesizer.last_prompt
```

Also prove:

- 0 and 9 topics reject before Reader calls;
- duplicate aliases resolve once while preserving first order;
- one `no_fact` result does not block another answer;
- one operational error is disclosed in partial synthesis;
- all failures produce no synthesis;
- mixed per-topic cache hit and Reader miss works;
- exact temporal anchoring remains per-topic;
- a selected topic version change changes the synthesis fingerprint;
- final answer is 1–200 characters and corrected once like single recall.

**Step 2: Run RED test**

```bash
.venv/bin/python -m pytest tests/test_multi_recall.py tests/test_engine.py -q
```

Expected: missing multi-recall classes/methods.

**Step 3: Implement core types and engine**

Core public types:

```python
@dataclass(frozen=True, slots=True)
class TopicSelection:
    topic_id: str
    reason: str

@dataclass(frozen=True, slots=True)
class MultiRecallResult:
    question: str
    topic_results: tuple[RecallResult, ...]
    final_answer: str | None
    synthesis_source: str
    scope_fingerprint: str
    failure: RecallFailure | None = None
```

Reuse `RecallEngine.recall()` for each topic. Add a `CapsuleSynthesizer` protocol so tests do not need network calls. The OpenAI-compatible implementation receives only labeled capsules and explicit status values.

Write one `QueryRecord` only after execution completes; include failed/no-fact per-topic statuses. The ledger write failure must be surfaced as an operational error rather than silently losing history.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_multi_recall.py tests/test_engine.py tests/test_cache.py -q
```

Expected: all focused tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/multi_recall.py src/hermes_topic_memory/engine.py tests/test_multi_recall.py tests/test_engine.py
git commit -m "feat: add isolated multi-topic recall"
```

---

### Task 4: Add deterministic multi-topic routing signals

**Objective:** Distinguish “several valid topics were mentioned” from unsafe alias ambiguity and tell the Agent to use the multi-topic tool.

**Files:**
- Modify: `src/hermes_topic_memory/router.py`
- Modify: `tests/test_router.py`

**Step 1: Write failing router tests**

Add:

```python
assert registry.mentioned_topics("compare Mafu and AI tools") == ("dm-mafu", "ai")
with pytest.raises(MultiTopicRequiredError) as exc:
    registry.mentioned_topic("compare Mafu and AI tools")
assert exc.value.topic_ids == ("dm-mafu", "ai")
```

Retain hard failures for one alias mapping to multiple topics. Prove longest-name matching, stable mention order, duplicate alias collapse, unknown text omission and no implicit all-topic result.

**Step 2: Run RED test**

```bash
.venv/bin/python -m pytest tests/test_router.py -q
```

Expected: missing `mentioned_topics`/`MultiTopicRequiredError`.

**Step 3: Implement router distinction**

- `mentioned_topics(query) -> tuple[str, ...]` returns unique exact topic matches in textual order.
- `mentioned_topic(query)` remains compatible for zero/one matches and raises `MultiTopicRequiredError` for several valid topics.
- `AmbiguousRegistryError` remains reserved for conflicting aliases/display names.

**Step 4: Run GREEN test**

```bash
.venv/bin/python -m pytest tests/test_router.py -q
```

Expected: all router tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/router.py tests/test_router.py
git commit -m "feat: identify multi-topic queries"
```

---

### Task 5: Expose multi-topic recall through Hermes Provider and CLI

**Objective:** Let the Agent choose a topic list and receive verbatim, source-labeled multi-topic output.

**Files:**
- Modify: `plugin/hermes_topic_memory/__init__.py`
- Modify: `tests/test_provider.py`
- Modify: `src/hermes_topic_memory/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `plugin/hermes_topic_memory/README.md`

**Step 1: Write failing Provider contract tests**

Schema requirement:

```json
{
  "name": "topic_memory_recall_multi",
  "parameters": {
    "required": ["query", "topics"],
    "properties": {
      "query": {"type": "string"},
      "topics": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "items": {
          "type": "object",
          "required": ["topic", "reason"],
          "properties": {
            "topic": {"type": "string"},
            "reason": {"type": "string"}
          }
        }
      }
    }
  }
}
```

Tests must prove:

- system prompt tells Agent to choose all required topics, not auto-include global;
- explicit multi-tool topics are resolved before Reader calls;
- multi-topic query sent to single tool returns `multi_topic_required` with candidates;
- output contains `verbatim_answer`, `response_policy`, `query_id`, selected topics, per-topic versions/status/source and no body;
- one-topic multi call works;
- unknown/ambiguous topic fails before any Reader call;
- session current topic is not replaced by an unordered “multi current topic” state;
- shutdown flushes query records.

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_provider.py tests/test_cli.py -q
```

Expected: missing schema/handler/CLI options.

**Step 3: Implement Provider and CLI surface**

- add `MULTI_RECALL_SCHEMA`;
- initialize multi-engine/query ledger beside the single engine;
- add handler branch and safe error mapping;
- update single-handler routing error for `multi_topic_required`;
- add CLI `recall-multi --topic TOPIC --reason REASON ... QUERY`;
- preserve exact raw question and verbatim final relay contract.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_provider.py tests/test_cli.py tests/test_multi_recall.py -q
```

Expected: focused Provider/CLI tests pass.

**Step 5: Commit**

```bash
git add plugin/hermes_topic_memory src/hermes_topic_memory/cli.py tests/test_provider.py tests/test_cli.py README.md
git commit -m "feat: expose multi-topic recall"
```

---

### Task 6: Add topic lifecycle, optimistic edits and recycle bin

**Objective:** Support Panel create/edit/archive/trash/restore/permanent-delete operations without unsafe direct filesystem manipulation.

**Files:**
- Modify: `src/hermes_topic_memory/models.py`
- Modify: `src/hermes_topic_memory/store.py`
- Create: `src/hermes_topic_memory/topic_admin.py`
- Modify: `src/hermes_topic_memory/router.py`
- Modify: `src/hermes_topic_memory/catalog.py`
- Modify: `src/hermes_topic_memory/validation.py`
- Create: `tests/test_topic_admin.py`
- Modify: `tests/test_store.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_catalog.py`
- Modify: `tests/test_fixtures.py`

**Step 1: Write failing lifecycle tests**

Required state sequence:

```python
created = admin.create_topic("e2e-topic", "E2E Topic", (), "# Body\n")
archived = admin.archive("e2e-topic", expected_version=1)
restored = admin.restore("e2e-topic")
trashed = admin.trash("e2e-topic", expected_version=1)
restored_again = admin.restore_from_trash("e2e-topic")
admin.trash("e2e-topic", expected_version=1)
admin.permanent_delete("e2e-topic", confirmation="E2E Topic")
assert not (data_root / "topics/e2e-topic").exists()
```

Also prove:

- duplicate ID/display/alias create is atomic and leaves no orphan directory;
- active routing excludes trashed, and implicit routing excludes archived;
- explicit archived recall can be separately allowed/read-only;
- expected version/fingerprint mismatch returns `concurrent_update` without writing;
- full edit saves prior version and invalidates only that topic's cache;
- permanent delete with wrong display confirmation does nothing;
- deletion audit is fsynced before removal;
- delete cannot escape `topics/` through crafted IDs;
- registry and metadata state transitions are recoverable after injected failures.

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_topic_admin.py tests/test_store.py tests/test_router.py tests/test_fixtures.py -q
```

Expected: lifecycle APIs and metadata state do not exist.

**Step 3: Implement lifecycle service**

Use `TopicAdminService` as the only mutation boundary for Panel and CLI. It coordinates registry and topic metadata under a data-root-level lock. Trash storage:

```text
data/recycle-bin/<topic_id>/
  topic/                 # complete original topic directory
  tombstone.json         # prior registry item, state, timestamps, display confirmation
```

Do not physically remove cache/query history during trash. Permanent delete removes topic/cache/proposals/evidence/recycle files but retains a minimal operation audit with no body.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_topic_admin.py tests/test_store.py tests/test_router.py tests/test_fixtures.py -q
```

Expected: all lifecycle tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/models.py src/hermes_topic_memory/store.py src/hermes_topic_memory/topic_admin.py src/hermes_topic_memory/router.py src/hermes_topic_memory/catalog.py src/hermes_topic_memory/validation.py tests
git commit -m "feat: add safe topic lifecycle operations"
```

---

### Task 7: Build testable Panel read/auth application

**Objective:** Expose authenticated JSON read APIs without coupling route logic to the HTTP server.

**Files:**
- Create: `src/hermes_topic_memory/panel_app.py`
- Create: `src/hermes_topic_memory/panel_server.py`
- Create: `tests/test_panel_app.py`
- Create: `tests/test_panel_server.py`

**Step 1: Write failing application tests**

Construct `PanelApplication(data_root, reader, auth_token)` and call `dispatch()` directly.

Cover:

- `GET /api/overview` contains KPI/chart/catalog data and no body;
- `GET /api/topics` filters/sorts/paginates;
- `GET /api/topics/<id>` returns metadata but not body by default;
- authenticated `GET /api/topics/<id>/body` returns body and fingerprint;
- questions filter by topic, status, source, date and multi/single;
- versions list and version body read;
- recycle-bin list;
- missing/bad authorization is 401 with no data;
- unknown path control returns 404;
- traversal/encoded traversal is rejected;
- malformed JSON is 400;
- no API response exposes API keys, file-system absolute paths or neighboring topic body.

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_panel_app.py tests/test_panel_server.py -q
```

Expected: modules do not exist.

**Step 3: Implement application and HTTP adapter**

Use small response types:

```python
@dataclass(frozen=True, slots=True)
class PanelResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
```

`PanelApplication` owns routing and serialization. `TopicMemoryPanelHandler` only translates HTTP request/response details. Auth uses constant-time comparison against a token read from environment/config at startup. Default bind is `127.0.0.1`.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_panel_app.py tests/test_panel_server.py -q
```

Expected: read/auth HTTP tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/panel_app.py src/hermes_topic_memory/panel_server.py tests/test_panel_app.py tests/test_panel_server.py
git commit -m "feat: add authenticated topic panel API"
```

---

### Task 8: Add Panel recall endpoints

**Objective:** Run Agent-planned or manual-scope multi-topic recall from the Panel and persist query history.

**Files:**
- Modify: `src/hermes_topic_memory/panel_app.py`
- Create: `src/hermes_topic_memory/topic_planner.py`
- Create: `tests/test_topic_planner.py`
- Modify: `tests/test_panel_app.py`

**Step 1: Write failing planner/API tests**

Planner returns strict JSON topic selections from registry metadata only; it never receives bodies.

Prove:

- valid planner output resolves to known unique topics;
- planner cannot select more than 8;
- unknown/duplicate output fails rather than being silently dropped;
- manual override bypasses planner but remains labeled `manual`;
- `/api/recall/run` returns plan, per-topic results, final answer, latency and query ID;
- `/api/recall/rerun/<query_id>` uses original question with current topic versions and creates a new query ID;
- failed recall records its real failure status;
- Panel source is recorded as `panel`.

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_topic_planner.py tests/test_panel_app.py -q
```

Expected: planner/recall routes absent.

**Step 3: Implement bounded planner and endpoints**

Planner prompt inputs:

- raw question;
- topic IDs, display names and aliases only;
- rule: select minimum sufficient topic set, maximum 8, no default global.

Planner output:

```json
{"topics":[{"topic":"dm-mafu","reason":"explicitly named"}]}
```

Do not merge plan and recall into one untestable model call.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_topic_planner.py tests/test_panel_app.py tests/test_multi_recall.py -q
```

Expected: planner and Panel recall tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/topic_planner.py src/hermes_topic_memory/panel_app.py tests/test_topic_planner.py tests/test_panel_app.py
git commit -m "feat: add panel multi-topic recall API"
```

---

### Task 9: Build the MM-style Panel shell and home page

**Objective:** Deliver the approved dark MM-style home dashboard using real APIs and charts.

**Files:**
- Create: `src/hermes_topic_memory/panel_assets/index.html`
- Create: `src/hermes_topic_memory/panel_assets/styles.css`
- Create: `src/hermes_topic_memory/panel_assets/app.js`
- Modify: `src/hermes_topic_memory/panel_server.py`
- Create: `tests/test_panel_assets.py`

**Step 1: Write failing asset/DOM contract tests**

Verify:

- all assets are package resources and served with correct MIME types;
- no external CDN dependency is required;
- home has KPI strip, 30-day chart, topic distribution, recent activity and topic catalog anchors;
- topic rows expose name, question count, cache count, version, update time and status;
- search/filter/sort controls have accessible labels;
- chart has an accessible summary/table fallback;
- no private body placeholder is embedded in initial HTML;
- reduced-motion and mobile breakpoints exist;
- design tokens match MM baseline (`#0d0d0d`, dark panels, orange accent, mono stack).

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_panel_assets.py -q
```

Expected: assets absent.

**Step 3: Implement shell and home UI**

Reuse MM visual language, not its MM-specific business tabs. Top navigation:

```text
首页 | 多主题召回 | 最近提问 | 回收站
```

Use vanilla SVG for the 30-day chart and distribution. Fetch `/api/overview` once; do not fan out one request per topic. Render explicit loading, empty and error states.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_panel_assets.py tests/test_panel_server.py -q
```

Expected: asset and server tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/panel_assets src/hermes_topic_memory/panel_server.py tests/test_panel_assets.py
git commit -m "feat: add MM-style topic panel home"
```

---

### Task 10: Build topic detail, recent questions and recall lab UI

**Objective:** Expose the core read/recall journeys in the Panel.

**Files:**
- Modify: `src/hermes_topic_memory/panel_assets/index.html`
- Modify: `src/hermes_topic_memory/panel_assets/styles.css`
- Modify: `src/hermes_topic_memory/panel_assets/app.js`
- Modify: `tests/test_panel_assets.py`

**Step 1: Add failing DOM/state tests**

Require UI states for:

- integrated topic detail with overview/questions/ask/body/versions/settings;
- paginated question history with other participating topic chips;
- body fetched only when authenticated detail tab opens;
- recall lab plan, per-topic cards, no-fact/error labels and final synthesis;
- manual topic override visibly labeled test-only;
- rerun action preserves old and new query IDs;
- recent-questions filters and pagination;
- keyboard navigation and focus return after dialogs;
- empty/error/loading states.

**Step 2: Run RED test**

```bash
.venv/bin/python -m pytest tests/test_panel_assets.py -q
```

Expected: new anchors/state handlers absent.

**Step 3: Implement UI**

Keep one SPA state object and one `api()` wrapper. Do not add a framework/build chain. Escape all server strings before DOM insertion; prefer `textContent` over `innerHTML` for private content.

**Step 4: Run GREEN test**

```bash
.venv/bin/python -m pytest tests/test_panel_assets.py -q
```

Expected: all UI contract tests pass.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/panel_assets tests/test_panel_assets.py
git commit -m "feat: add panel recall and topic views"
```

---

### Task 11: Add Panel write APIs and maintenance UI

**Objective:** Connect create/edit/version/archive/recycle operations to authenticated Panel workflows.

**Files:**
- Modify: `src/hermes_topic_memory/panel_app.py`
- Modify: `src/hermes_topic_memory/panel_assets/index.html`
- Modify: `src/hermes_topic_memory/panel_assets/styles.css`
- Modify: `src/hermes_topic_memory/panel_assets/app.js`
- Modify: `tests/test_panel_app.py`
- Modify: `tests/test_panel_assets.py`

**Step 1: Write failing joined API tests**

Exercise exact sequences against a disposable data root:

```text
create -> read -> append -> read v2 -> replace -> read v3
-> rollback v1 -> read v4 -> archive -> restore
-> trash -> list recycle -> restore -> trash
-> wrong delete confirmation (no-op) -> exact confirmation -> deleted
```

At every step assert:

- registry/topic metadata coherence;
- version/fingerprint precondition;
- other-topic hashes unchanged;
- cache invalidation limited to target topic;
- audit entry created;
- body never returned in list/overview APIs.

UI tests require diff preview, explicit destructive labels, exact-name confirmation and disabled submit until confirmation matches.

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_panel_app.py tests/test_panel_assets.py tests/test_topic_admin.py -q
```

Expected: write routes/UI actions absent.

**Step 3: Implement write routes and UI**

Map API errors to stable codes:

```text
invalid_request, topic_exists, topic_not_found, alias_conflict,
concurrent_update, append_duplicate, invalid_state,
confirmation_mismatch, operation_failed
```

Never expose Python exception text directly to the browser.

**Step 4: Run GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_panel_app.py tests/test_panel_assets.py tests/test_topic_admin.py -q
```

Expected: maintenance journey passes.

**Step 5: Commit**

```bash
git add src/hermes_topic_memory/panel_app.py src/hermes_topic_memory/panel_assets tests/test_panel_app.py tests/test_panel_assets.py
git commit -m "feat: add panel topic maintenance"
```

---

### Task 12: Add CLI serve command, package assets and operations docs

**Objective:** Make Panel startup/deployment explicit and reproducible without modifying Hermes configuration.

**Files:**
- Modify: `src/hermes_topic_memory/cli.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_install.py`
- Modify: `README.md`
- Modify: `plugin/hermes_topic_memory/README.md`
- Modify: `.hermes/memory.md`
- Modify: `.hermes/skills/hermes-topic-memory/SKILL.md`

**Step 1: Write failing CLI/package tests**

Command:

```bash
hermes-topic-memory --data-root /path panel \
  --host 127.0.0.1 --port 8765 --auth-token-env TOPIC_PANEL_TOKEN
```

Tests prove:

- non-loopback host without auth fails;
- token value is not accepted on command line;
- missing token env fails before bind;
- package wheel contains HTML/CSS/JS assets;
- installer remains copy-only and does not start Panel/restart Hermes;
- legacy `dashboard --output` remains available.

**Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_install.py -q
```

Expected: `panel` command/assets packaging absent.

**Step 3: Implement command and docs**

Add package data configuration and startup logging that reports only host/port/data-root topic count, never auth token or private paths beyond what the operator explicitly supplied.

**Step 4: Run GREEN tests and wheel smoke**

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_install.py -q
rm -rf dist build
.venv/bin/python -m build
python3 -m venv /tmp/htm-panel-wheel
/tmp/htm-panel-wheel/bin/pip install --no-deps dist/*.whl
/tmp/htm-panel-wheel/bin/hermes-topic-memory --help
```

Expected: tests pass, wheel installs, help lists `panel` and `recall-multi`.

**Step 5: Commit**

```bash
git add pyproject.toml src/hermes_topic_memory/cli.py tests/test_cli.py tests/test_install.py README.md plugin/hermes_topic_memory/README.md .hermes
git commit -m "docs: package and operate topic memory panel"
```

---

### Task 13: Run full local verification and self-review

**Objective:** Prove the complete repository is clean before any cloud deployment.

**Files:**
- Review all changed files
- No new production files unless a defect is found

**Step 1: Run full deterministic suite**

```bash
git diff --check
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

Expected: zero diff-check errors, all tests pass, sdist and wheel build.

**Step 2: Run known-bad controls**

- invalid auth token returns 401;
- nonexistent route returns 404, proving 401 is not a global false positive;
- malformed topic alias produces a deterministic failure;
- deliberately duplicated append leaves version unchanged;
- two concurrent edits with the same expected version yield one success and one `concurrent_update`;
- intentionally mixed topic fixture cannot leak a distractor into the other topic result.

**Step 3: Review the actual diff**

```bash
git status --short
git log --oneline --decorate -20
git diff 8cbda3f..HEAD --stat
git diff 8cbda3f..HEAD -- src plugin tests README.md pyproject.toml
```

Check:

- no private corpus or secrets;
- no generated query history/cache;
- no absolute private path in production defaults;
- no MM-only feature residue;
- no body in overview/list payloads;
- every new state field clears or transitions on all exit paths;
- Panel server cannot run arbitrary commands.

**Step 4: Commit review fixes only if needed**

Use a focused `fix:` commit; rerun the full suite and build after any fix.

---

### Task 14: Deploy to an isolated cloud profile and run joined E2E

**Objective:** Verify the exact user journeys on cloud Hermes while proving production data and code remain controlled.

**Environment:**
- Azure China VM: `mindmemos-brain2`
- Repository runtime: `/home/claw/Projects/hermes-topic-memory-real`
- Production profile: `/home/claw/.hermes-topic-memory-real`
- Disposable acceptance profile: `/home/claw/.hermes-topic-memory-panel-e2e`

**Step 1: Capture production baselines before deployment**

Record without outputting private content:

- deployed commit;
- Provider status;
- topic count;
- deterministic production data-root tree hash;
- current code tree hash;
- current service states.

**Step 2: Deploy code with rollback copy**

Use a Git archive with SHA-256 verification. Back up code and plugin directories. Do not copy private corpus into Git artifacts. Restart only the isolated/new Panel service and the intended cloud topic-memory process if required.

**Step 3: Create disposable profile**

Clone config/plugin/data into the E2E home. Use a dedicated Panel port and an ephemeral auth token supplied through environment. Do not expose the token in command output.

**Step 4: Joined multi-topic Hermes E2E**

Run through real cloud Hermes/Reader:

1. single exact-time Mafu recall;
2. single AI topic recall;
3. a question requiring Mafu + AI, verifying both topic IDs and labeled synthesis;
4. a question requiring general + project, verifying Agent-selected scope;
5. unknown topic safe failure;
6. one topic returns no fact while another answers;
7. same-session A -> B -> A;
8. repeat one multi-topic question to prove per-topic cache hits and query history.

Inspect Provider tool traces to prove `topic_memory_recall_multi` was selected for multi-topic questions. Final replies must preserve Reader dates and source labels.

**Step 5: Joined Panel browser/API E2E**

Against the disposable profile:

```text
login/auth -> home -> topic catalog -> topic detail
-> multi-topic recall lab -> query history -> rerun
-> create topic -> append -> full edit -> diff -> rollback
-> archive -> restore -> trash -> restore -> trash -> permanent delete
```

Verify API status, persisted files/versions/audit and browser UI after refresh. Capture desktop and mobile screenshots of home, topic detail and recall lab. Check browser console/network errors.

**Step 6: Production safety proof and cleanup**

- stop/remove disposable Panel/profile;
- recompute production data tree hash and require exact match;
- verify production topic count and active Provider;
- verify no E2E topic/query marker in production;
- keep only non-sensitive acceptance summary outside the data root;
- if deployment of code was accepted, production code hash should equal local HEAD; otherwise restore backup.

**Step 7: Final acceptance report**

Report separately:

```text
Multi-topic Hermes path: pass/fail
Panel read path: pass/fail
Panel write path (disposable): pass/fail
Production data unchanged: exact hash match yes/no
Known unsupported/untested: explicit list
```

Do not call the product complete if the outer Agent still uses the single-topic tool for a real multi-topic question, even if direct Provider tests pass.

---

## Final stop condition

Implementation is complete only when:

1. all local tests/build pass;
2. the cloud Provider actually selects and executes multi-topic recall;
3. the Panel displays real topic/question counts and joined recall evidence;
4. the full maintenance journey passes on disposable data;
5. production data hash is unchanged by automated E2E;
6. the deployed code provenance matches the committed source;
7. no private body, token or query corpus entered Git.
