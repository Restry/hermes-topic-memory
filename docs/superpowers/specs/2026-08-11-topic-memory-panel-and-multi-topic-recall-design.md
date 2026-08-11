# Topic Memory Panel and Multi-Topic Recall Design

**Date:** 2026-08-11  
**Status:** Approved direction; implementation details delegated to Hermes  
**Repository:** `/Users/leway/Projects/hermes-topic-memory`

## 1. Product decision

Hermes Topic Memory is not only a file archive and not a generic memory database. Its core product is:

1. keep one canonical, versioned memory document per topic;
2. let an Agent decide which one or more topics a question requires;
3. read every selected topic independently without leaking facts across topic boundaries;
4. synthesize a final answer with visible source attribution;
5. make topics, historical questions, canonical memory, versions and maintenance operations manageable in one Panel.

Topic creation does not need to be exposed as a chat tool in the first release. CLI and Panel creation are sufficient. Multi-topic recall is a first-class requirement.

## 2. Chosen product shape

Three product shapes were considered:

- **Metadata dashboard only:** small and safe, but cannot support recall inspection or memory maintenance.
- **Topic Memory control center:** combines topic management, recall testing, question history and version maintenance.
- **Full MindMemOS clone:** adds token management, Agent credentials, model routing, graph and Dreaming; this exceeds the project's core business.

The chosen shape is the **Topic Memory control center**. It reuses the visual language and chart style of the existing MM Panel, but only includes capabilities backed by this project's topic-memory model.

## 3. User journeys

### 3.1 Browse and understand the memory estate

The user opens the home page and immediately sees:

- all topics;
- total and per-topic historical question counts;
- active QA cache counts;
- topic version, update time and state;
- recent question activity and distribution across topics.

Clicking a topic opens its integrated detail page.

### 3.2 Ask across multiple topics

The user asks a question in Hermes or in the Panel recall lab. The Agent decides whether the question needs one topic or several topics. For example, a project question may require both a project topic and a general topic, but the general topic is not included by default.

The system shows:

- which topics the Agent selected and why;
- the independently produced answer/capsule from each topic;
- which topics had no supporting fact;
- the final synthesized answer;
- the topic versions and cache status used for that answer.

### 3.3 Maintain a topic

From a topic detail page, the user can:

- create a topic;
- inspect and search the canonical body;
- append a small change;
- intentionally replace/edit the complete body;
- inspect prior versions and diffs;
- roll back by creating a new version;
- archive and restore a topic;
- move a topic to the recycle bin;
- permanently delete it after a second explicit confirmation.

## 4. Domain model

### 4.1 Canonical topic memory

The existing topic structure remains authoritative:

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

A topic retains a stable `topic_id`, display name, aliases, source metadata, state, current version and content fingerprint.

New lifecycle state:

```text
active -> archived -> trashed -> permanently deleted
```

`archived` topics remain searchable only when explicitly selected. `trashed` topics are excluded from recall and remain restorable. Permanent deletion is available only from the recycle bin.

### 4.2 Query history is separate from QA cache

The QA cache is disposable and version-bound, so it cannot serve as the durable source for historical question counts. Add a separate append-only query ledger:

```text
data/query-history/YYYY-MM.jsonl
```

Each query record contains only the minimum operational data needed by the Panel and audit:

- `query_id`;
- original question;
- timestamp;
- selected topic IDs and versions;
- per-topic status (`answered`, `no_fact`, `error`) and capsule;
- per-topic cache hit/miss and match type;
- final synthesized answer;
- selection reason supplied by the Agent;
- total/per-stage latency;
- source (`hermes`, `panel`, `cli`);
- session identifier in a non-secret, non-content form.

The ledger never becomes evidence for canonical memory updates. Deleting a QA cache entry does not delete query history. Topic updates invalidate cache usability by version but preserve historical query records.

Home-page “question count” is calculated from query history. “Active cache count” is shown separately.

## 5. Multi-topic recall

### 5.1 Agent-owned topic selection

No topic is permanently paired with a general topic. The Agent decides the required scope for every question.

The Provider offers two paths:

- `topic_memory_recall`: existing strict single-topic recall;
- `topic_memory_recall_multi`: new explicit multi-topic recall.

The multi-topic tool accepts:

- the original question copied verbatim;
- an ordered list of 1–8 topic names/IDs selected by the Agent;
- a short selection reason for each topic.

One topic is allowed so the Agent does not need separate orchestration logic, but the existing single-topic tool remains for compatibility and lower overhead.

### 5.2 Isolation-preserving execution

For each selected topic, the Provider:

1. resolves the topic name deterministically;
2. captures the current topic version;
3. performs cache lookup only inside that topic/version;
4. asks the Reader using only that topic's canonical body;
5. returns a labeled capsule or `no_fact` result.

No Reader request receives two full topic bodies. This preserves stable per-topic prefixes and prevents one topic from contaminating another.

After all topic results are available, a synthesis step receives only:

- the original question;
- labeled topic names and versions;
- the short per-topic capsules;
- explicit `no_fact` markers.

It does not receive canonical bodies. The synthesis must cite topic labels inline when claims come from different topics.

### 5.3 Routing behavior

- A query that names several topics is no longer treated as a generic ambiguity.
- If the Agent calls the single-topic tool for a clearly multi-topic question, the Provider returns `multi_topic_required` with the resolved candidate topic IDs; it does not silently choose one.
- Unknown or genuinely ambiguous aliases fail before any Reader call.
- Duplicate topic aliases collapse to one topic.
- A topic returning `no_fact` does not block successful results from other selected topics.
- A topic operational error is labeled separately; partial synthesis may proceed only if at least one topic succeeded and the final answer discloses the partial failure.
- An empty selected-topic list is invalid; there is no hidden all-topic search.

### 5.4 Caching

Existing per-topic caches remain independent. A multi-topic question may therefore mix hits and misses safely.

The final synthesis may be cached using a scope fingerprint derived from:

```text
normalized question + ordered(topic_id, topic_version, capsule hash) + prompt version
```

Any selected topic version change invalidates the synthesis fingerprint automatically without removing historical query records.

## 6. Panel information architecture

The Panel is server-backed rather than a generated static HTML file. It follows the existing MM Panel deployment shape: one Python service serving JSON APIs and a dependency-light single-page interface.

Top-level navigation:

1. **首页**
2. **多主题召回**
3. **最近提问**
4. **回收站**

Topic details are reached from the home page rather than occupying a permanent top-level tab.

### 6.1 Home page

KPI strip:

- active topic count;
- total historical questions;
- active QA cache count;
- questions in the last 30 days;
- archived/trashed counts shown as secondary status, not primary KPIs.

Charts and activity:

- 30-day question activity chart;
- topic distribution by historical question count;
- recent query activity list;
- Provider health and current deployed data version in a small integration strip.

Topic catalog:

- search by display name, topic ID and alias;
- filter by active/archived;
- sort by question count, update time, version and title;
- each row/card shows topic name, question count, active cache count, current version, updated time and status;
- create-topic action is visible from this catalog;
- clicking the topic enters the integrated detail page.

The home page does not render private canonical body text.

### 6.2 Topic detail page

Sections/tabs inside one integrated page:

- **概览:** metadata, aliases, current version, size, question/cache counts, recent activity;
- **提问记录:** paginated query history involving this topic, cache hit/miss, other topics involved, per-topic result and final answer;
- **继续提问:** ask with this topic preselected while still allowing the Agent to add other required topics;
- **记忆正文:** authenticated read, search, append and full edit with version precondition;
- **版本:** version timeline, metadata, diff, rollback;
- **设置:** display name, aliases, archive, move to recycle bin.

### 6.3 Multi-topic recall lab

The recall lab is a first-release core feature.

It presents:

1. question input;
2. Agent-selected topic chips and selection reasons;
3. per-topic execution cards showing topic/version, hit/miss, status and capsule;
4. final synthesized answer with topic attribution;
5. latency breakdown and query ID;
6. a manual topic override for testing, clearly marked as test-only rather than normal Agent behavior.

A user can reopen any historical query in the recall lab and rerun it against current topic versions. The old and new runs remain distinct query records.

### 6.4 Recent questions

Global paginated view with filters for:

- topic;
- single-topic vs multi-topic;
- cache hit/miss;
- answered/no-fact/error;
- date range;
- source (`hermes`, `panel`, `cli`).

### 6.5 Recycle bin

- restore topic;
- show deletion timestamp and prior state;
- export/download before permanent deletion;
- permanent delete requires typing the exact topic display name and a second confirmation;
- deletion writes a non-sensitive audit event before removal.

## 7. Write operations and safety

All writes reuse the existing lock, fsync, atomic replacement, evidence, version and audit mechanisms.

Additional requirements:

- create validates unique topic ID, display name and aliases before any files are written;
- edits require the current version/fingerprint to prevent overwriting a concurrent change;
- append remains bounded and duplicate-aware;
- full edit displays a diff before save;
- rollback creates a new version rather than rewinding metadata invisibly;
- archive/recycle operations are atomic registry + metadata transitions;
- permanent deletion is never exposed as a one-click action;
- no Panel API accepts arbitrary file paths or shell commands.

## 8. Panel API surface

Read APIs:

```text
GET  /api/overview
GET  /api/topics
GET  /api/topics/<topic_id>
GET  /api/topics/<topic_id>/questions
GET  /api/topics/<topic_id>/versions
GET  /api/questions
GET  /api/recycle-bin
```

Recall APIs:

```text
POST /api/recall/plan
POST /api/recall/run
POST /api/recall/rerun/<query_id>
```

Write APIs:

```text
POST   /api/topics
POST   /api/topics/<topic_id>/append
PUT    /api/topics/<topic_id>/body
POST   /api/topics/<topic_id>/rollback
POST   /api/topics/<topic_id>/archive
POST   /api/topics/<topic_id>/restore
DELETE /api/topics/<topic_id>              # move to recycle bin
DELETE /api/recycle-bin/<topic_id>         # confirmed permanent delete
```

All errors are structured and safe. Private body content is returned only by authenticated topic-detail endpoints.

## 9. Visual design baseline

Use the MM Panel visual language directly:

- near-black background and dark panels;
- monochrome/JetBrains Mono typography;
- orange-red accent;
- square borders and compact dashboard density;
- KPI cards with a left accent rule;
- SVG charts with subtle grid lines, orange bars/lines and restrained glow;
- green/warn/red status dots;
- responsive one-column collapse on mobile;
- reduced-motion support;
- no new design system and no unrelated decorative illustration.

The existing MM Panel's home charts, activity list, distribution cards and navigation are the visual reference. The Topic Memory Panel must still use its own product labels and must not display unsupported MM capabilities.

## 10. Authentication and privacy

- bind to loopback by default; non-loopback deployment requires authentication;
- server reads secrets from environment/runtime files only and never sends them to the browser;
- topic bodies and question answers are private and require authenticated endpoints;
- home-page metadata is still treated as private in deployed environments;
- no CDN/runtime third-party scripts are required for core operation;
- logs redact body text, credentials and full private answers by default;
- every write operation records actor/source, topic, version transition, timestamp and operation type.

## 11. Explicit non-goals for the first release

Do not port these MM Panel capabilities unless a separate product need appears:

- API token and Agent credential management;
- model/endpoint routing configuration;
- Neo4j graph views;
- Dreaming;
- MindMemOS lineage/provenance concepts;
- MM-specific memory types/statuses;
- embedding, rerank or vector-store administration;
- arbitrary shell/terminal access;
- global hidden search across every topic.

## 12. Compatibility

- existing single-topic CLI and Provider calls continue to work;
- current data roots migrate forward without rewriting canonical `memory.md` files;
- the existing static metadata dashboard command may remain as an offline/export view, but it is not the primary Panel;
- private real corpus, query history and generated Panel data remain Git-ignored;
- local Hermes is not activated or modified; live Provider/Panel E2E remains cloud-only.

## 13. Acceptance criteria

### Multi-topic recall

- Agent can select 1–8 topics;
- general + project topic recall works without fixed pairing;
- every topic is read independently;
- source labels and topic versions are preserved;
- one topic cannot leak a fact into another topic's result;
- unknown aliases and accidental single-tool multi-topic calls fail safely and specifically;
- same query supports mixed cache hits/misses;
- final answer changes when a participating topic version changes;
- query ledger records the real topic set and result.

### Panel

- home page lists all real topics and correct historical question counts;
- charts use real query-ledger data and Beijing time;
- topic detail provides all approved sections;
- multi-topic recall lab exposes planning, per-topic execution and synthesis;
- create, append, full edit, rollback, archive, recycle, restore and confirmed permanent delete work on an isolated data root;
- concurrency mismatch cannot overwrite a newer version;
- pagination works for questions and versions;
- desktop and mobile layouts have no horizontal overflow;
- authenticated body access and unauthenticated rejection are verified;
- no private body is embedded in static home-page HTML;
- cloud E2E leaves the production corpus unchanged unless the user explicitly performs a write.

## 14. Implementation order

1. durable query ledger and read APIs;
2. multi-topic Provider tool and isolated synthesis;
3. Panel server and MM-style shell;
4. home page and topic catalog;
5. topic detail and historical questions;
6. recall lab;
7. create/edit/version/archive/recycle operations;
8. authentication, audit and cloud E2E.
