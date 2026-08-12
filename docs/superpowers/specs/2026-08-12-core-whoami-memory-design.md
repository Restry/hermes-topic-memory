# Core / WhoAmI Memory Design

## Product model

Runtime context is composed in this order:

1. Core / WhoAmI remembered background;
2. routed single- or multi-topic directive;
3. current session context managed by Hermes.

Core has stable identity `core` and display name `Core / WhoAmI`. It is an always-on, small, manually maintained layer. It is not a registered topic, is never routed, and cannot be archived, trashed, deleted, or changed by evidence/updater hooks.

## Persistence and safety

The canonical data root contains:

```text
core-memory/memory.md
core-memory/metadata.json
core-memory/versions/<version>.md
operation-audit/core-memory.jsonl
```

`metadata.json` records identity, display name, monotonically increasing version, SHA-256 content fingerprint, creation time, and update time. Bodies are strict UTF-8, nonblank, and limited to 4,000 Unicode code points at both write and read boundaries. Absence of canonical Core files is valid.

Initialization creates version 1. Replace and rollback preserve the prior canonical body as an immutable numbered snapshot and create a new current version. Both expected version and expected fingerprint are mandatory. Rollback only accepts a historical version and the exact confirmation `Core / WhoAmI`.

Writes use fsynced temporary files and atomic replacement. A data-root lock serializes Core operations. A durable transaction manifest snapshots every affected canonical, version, and audit path; exceptions or a pending transaction restore the prior state. Sanitized audit receipts contain operation, identity, versions, target version when relevant, and timestamp—never body text or local paths.

## Provider injection

Every initialized provider reads Core from its configured `data_root` on each `prefetch()`. Missing Core preserves the previous result exactly. Present Core is returned by itself when no safe topic resolves, or followed by the existing single/multi-topic directive when routing succeeds. Topic cache, follow-up routing, multi-topic recall, and query-history behavior are otherwise unchanged.

The plugin wrapper labels Core as remembered background rather than current-user instructions. It includes version and fingerprint but no path. Body markup is escaped before interpolation so remembered text cannot close or create wrapper tags. Corrupt reads fail open: only the exception class is logged, and topic recall continues without Core.

## Authenticated Panel API

All routes inherit Bearer authentication, strict JSON objects, unknown-field rejection, path validation, and sanitized errors.

Read routes:

- `GET /api/core-memory` — presence and metadata only;
- `GET /api/core-memory/body` — current body, version, and fingerprint;
- `GET /api/core-memory/versions?page=&page_size=` — paginated version metadata;
- `GET /api/core-memory/versions/{version}/body` — one version body.

Write routes:

- `POST /api/core-memory/initialize` — `body`, only while absent;
- `POST /api/core-memory/replace` — `body`, `expected_version`, `expected_fingerprint`;
- `POST /api/core-memory/rollback` — `target_version`, both concurrency fields, and `confirmation`.

`/api/overview` exposes Core metadata as the separate top-level `core_memory` object. Core never enters topic catalogs, topic KPIs, topic distribution, query summaries, or lifecycle routes. Body text appears only on authenticated body endpoints.

## Delivery boundary

WP1 implements storage, runtime injection, authenticated API, audit/concurrency safety, and tests. WP2 Panel UI is pending; WP1 does not modify Panel HTML, CSS, or JavaScript and does not seed live Core content.
