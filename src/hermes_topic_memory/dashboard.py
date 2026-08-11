from __future__ import annotations

import html
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from .cache import QuestionCache
from .router import TopicRegistry
from .store import FileTopicStore
from .validation import validate_data_root

_PREVIEW_CHARS = 160


@dataclass(frozen=True, slots=True)
class DashboardTopic:
    topic_id: str
    display_name: str
    source_relative_path: str
    chars: int
    bytes: int
    lines: int
    version: int
    version_count: int
    cache_entry_count: int
    updated_at: str
    imported_at: str | None
    source_sha256: str | None
    preview: str | None


@dataclass(frozen=True, slots=True)
class DashboardResult:
    data_root: Path
    output: Path
    topic_count: int
    total_chars: int
    total_bytes: int
    min_chars: int
    max_chars: int
    median_chars: int | float
    version_count: int
    cache_entry_count: int
    output_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "generated",
            "data_root": str(self.data_root),
            "output": str(self.output),
            "topic_count": self.topic_count,
            "total_chars": self.total_chars,
            "total_bytes": self.total_bytes,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "median_chars": self.median_chars,
            "version_count": self.version_count,
            "cache_entry_count": self.cache_entry_count,
            "output_bytes": self.output_bytes,
        }


def generate_dashboard(data_root: str | Path, output: str | Path) -> DashboardResult:
    """Render a portable metadata dashboard without embedding complete topic bodies."""
    root = Path(data_root).expanduser().resolve(strict=True)
    output_path = Path(output).expanduser().resolve(strict=False)
    validate_data_root(root)
    registry = TopicRegistry.from_file(root / "topic-registry.json")
    store = FileTopicStore(root)
    cache = QuestionCache(root)
    topics: list[DashboardTopic] = []
    for config in registry.topics:
        memory = store.get_topic(config.topic_id)
        body_bytes = memory.body.encode("utf-8")
        versions = store.list_versions(config.topic_id)
        cache_entries = cache.records(config.topic_id)
        topics.append(
            DashboardTopic(
                topic_id=config.topic_id,
                display_name=config.display_name,
                source_relative_path=config.source_relative_path
                or memory.metadata.source_relative_path
                or "Not recorded",
                chars=len(memory.body),
                bytes=len(body_bytes),
                lines=len(memory.body.splitlines()),
                version=memory.metadata.version,
                version_count=len(versions),
                cache_entry_count=len(cache_entries),
                updated_at=memory.metadata.updated_at,
                imported_at=memory.metadata.imported_at,
                source_sha256=memory.metadata.source_sha256,
                preview=memory.body[:_PREVIEW_CHARS]
                if len(memory.body) > _PREVIEW_CHARS
                else None,
            )
        )
    if not topics:
        raise ValueError("dashboard requires at least one registered topic")

    lengths = [topic.chars for topic in topics]
    total_chars = sum(lengths)
    total_bytes = sum(topic.bytes for topic in topics)
    version_count = sum(topic.version_count for topic in topics)
    cache_entry_count = sum(topic.cache_entry_count for topic in topics)
    payload = _render_html(
        topics,
        total_chars=total_chars,
        total_bytes=total_bytes,
        version_count=version_count,
        cache_entry_count=cache_entry_count,
    ).encode("utf-8")
    _atomic_write(output_path, payload)
    return DashboardResult(
        data_root=root,
        output=output_path,
        topic_count=len(topics),
        total_chars=total_chars,
        total_bytes=total_bytes,
        min_chars=min(lengths),
        max_chars=max(lengths),
        median_chars=median(lengths),
        version_count=version_count,
        cache_entry_count=cache_entry_count,
        output_bytes=len(payload),
    )


def _render_html(
    topics: list[DashboardTopic],
    *,
    total_chars: int,
    total_bytes: int,
    version_count: int,
    cache_entry_count: int,
) -> str:
    lengths = [topic.chars for topic in topics]
    metrics = (
        ("Topics", len(topics)),
        ("Total characters", total_chars),
        ("Total bytes", total_bytes),
        ("Median characters", median(lengths)),
        ("Minimum characters", min(lengths)),
        ("Maximum characters", max(lengths)),
        ("Versions across library", version_count),
        ("Cache entries", cache_entry_count),
    )
    metric_html = "".join(
        f'<div class="metric"><dt>{html.escape(label)}</dt><dd>{_number(value)}</dd></div>'
        for label, value in metrics
    )
    row_html = "".join(_topic_row(topic) for topic in topics)
    distribution_html = _distribution(lengths)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topic Memory Library</title>
<style>
:root {{ color-scheme: light; --cream: #f7f0e3; --paper: #fffaf0; --ink: #3c2c25; --muted: #725e53; --terracotta: #ad563c; --clay: #d98a68; --line: #d9c6b5; --focus: #6e3526; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--cream); color: var(--ink); font: 16px/1.5 ui-serif, Georgia, serif; }}
main {{ width: min(1180px, calc(100% - 2rem)); margin: 0 auto; padding: 2rem 0 4rem; }}
h1, h2 {{ line-height: 1.15; }}
h1 {{ margin-bottom: .5rem; font-size: clamp(2rem, 5vw, 3.5rem); }}
.subtitle {{ color: var(--muted); max-width: 70ch; }}
.banner {{ margin: 1.5rem 0; padding: 1rem 1.1rem; border: 2px solid var(--terracotta); background: var(--paper); font-weight: 700; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .75rem; margin: 0 0 2rem; }}
.metric {{ min-height: 7rem; padding: 1rem; border: 1px solid var(--line); background: var(--paper); }}
.metric dt {{ color: var(--muted); font-size: .85rem; text-transform: uppercase; letter-spacing: .06em; }}
.metric dd {{ margin: .4rem 0 0; color: var(--terracotta); font-size: clamp(1.4rem, 3vw, 2rem); font-weight: 800; }}
.distribution {{ margin: 0 0 2rem; padding: 1.25rem; border: 1px solid var(--line); background: var(--paper); }}
.distribution ul {{ margin: 0; padding: 0; list-style: none; }}
.distribution li {{ display: grid; grid-template-columns: 9rem 1fr 3rem; gap: .75rem; align-items: center; margin: .6rem 0; }}
.bar-track {{ height: 1rem; border: 1px solid var(--line); background: var(--cream); }}
.bar {{ display: block; height: 100%; background: var(--clay); }}
.controls {{ display: flex; flex-wrap: wrap; gap: .75rem; margin: 0 0 1rem; }}
.controls label {{ display: grid; gap: .25rem; color: var(--muted); font-weight: 700; }}
input, select {{ min-height: 2.75rem; padding: .55rem .7rem; border: 2px solid var(--line); border-radius: 0; background: var(--paper); color: var(--ink); font: inherit; }}
input {{ width: min(32rem, 80vw); }}
:focus-visible {{ outline: 3px solid var(--focus); outline-offset: 3px; }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--line); background: var(--paper); }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: .8rem; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
th {{ position: sticky; top: 0; background: #ecd9c5; white-space: nowrap; }}
tbody tr:last-child td {{ border-bottom: 0; }}
.topic-title {{ font-weight: 800; }}
code {{ color: #6e3526; overflow-wrap: anywhere; }}
details {{ min-width: 13rem; }}
summary {{ color: var(--terracotta); cursor: pointer; font-weight: 800; }}
dl.detail-grid {{ display: grid; grid-template-columns: max-content 1fr; gap: .25rem .75rem; margin: .75rem 0; }}
.detail-grid dt {{ color: var(--muted); }}
.detail-grid dd {{ margin: 0; overflow-wrap: anywhere; }}
.preview {{ max-width: 34rem; padding: .65rem; border-left: 4px solid var(--clay); background: var(--cream); white-space: pre-wrap; overflow-wrap: anywhere; }}
[hidden] {{ display: none !important; }}
.result-count {{ align-self: end; padding: .65rem 0; color: var(--muted); }}
@media (max-width: 780px) {{ .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .distribution li {{ grid-template-columns: 7rem 1fr 2.5rem; }} th, td {{ padding: .65rem; }} }}
@media (max-width: 480px) {{ main {{ width: min(100% - 1rem, 1180px); padding-top: 1rem; }} .metrics {{ grid-template-columns: 1fr; }} .controls {{ display: grid; }} input {{ width: 100%; }} }}
</style>
</head>
<body>
<main>
<header>
<h1>Topic Memory Library</h1>
<p class="subtitle">A direct view of the canonical topic store: real sizes, versions, source paths, and cache counts.</p>
<div class="banner">This dashboard is metadata-only. Source texts remain in the private memory store; only a short excerpt from longer documents may appear below.</div>
</header>
<dl class="metrics" aria-label="Library totals">{metric_html}</dl>
<section class="distribution" aria-labelledby="distribution-title">
<h2 id="distribution-title">Size distribution</h2>
{distribution_html}
</section>
<section aria-labelledby="topics-title">
<h2 id="topics-title">Topics</h2>
<div class="controls">
<label for="topic-filter">Search topics<input id="topic-filter" type="search" placeholder="Title, topic ID, or source path" autocomplete="off"></label>
<label for="topic-sort">Sort topics<select id="topic-sort"><option value="title">Title A–Z</option><option value="largest">Largest first</option><option value="smallest">Smallest first</option></select></label>
<div class="result-count" id="result-count" aria-live="polite">{len(topics)} topics</div>
</div>
<div class="table-wrap">
<table>
<thead><tr><th>Display title</th><th>Topic ID</th><th>Source path</th><th>Characters</th><th>Bytes</th><th>Lines</th><th>Version</th><th>Updated</th><th>Details</th></tr></thead>
<tbody id="topic-rows">{row_html}</tbody>
</table>
</div>
</section>
</main>
<script>
(() => {{
  const filter = document.getElementById('topic-filter');
  const sort = document.getElementById('topic-sort');
  const body = document.getElementById('topic-rows');
  const count = document.getElementById('result-count');
  const rows = Array.from(body.querySelectorAll('tr'));
  function apply() {{
    const query = filter.value.trim().toLocaleLowerCase();
    const ordered = rows.slice().sort((left, right) => {{
      if (sort.value === 'largest') return Number(right.dataset.chars) - Number(left.dataset.chars);
      if (sort.value === 'smallest') return Number(left.dataset.chars) - Number(right.dataset.chars);
      return left.dataset.title.localeCompare(right.dataset.title, undefined, {{ sensitivity: 'base' }});
    }});
    let visible = 0;
    for (const row of ordered) {{
      const matches = !query || row.dataset.search.includes(query);
      row.hidden = !matches;
      if (matches) visible += 1;
      body.appendChild(row);
    }}
    count.textContent = `${{visible}} ${{visible === 1 ? 'topic' : 'topics'}}`;
  }}
  filter.addEventListener('input', apply);
  sort.addEventListener('change', apply);
}})();
</script>
</body>
</html>
"""


def _topic_row(topic: DashboardTopic) -> str:
    escaped_title = html.escape(topic.display_name)
    escaped_id = html.escape(topic.topic_id)
    escaped_path = html.escape(topic.source_relative_path)
    search = " ".join(
        (topic.display_name, topic.topic_id, topic.source_relative_path)
    ).casefold()
    preview = (
        f'<p class="preview">{html.escape(topic.preview)}…</p>'
        if topic.preview is not None
        else '<p class="preview">Preview omitted to avoid embedding the complete short source.</p>'
    )
    source_sha = html.escape(topic.source_sha256 or "Not recorded")
    imported_at = html.escape(topic.imported_at or "Not recorded")
    return f"""<tr data-title="{html.escape(topic.display_name.casefold(), quote=True)}" data-search="{html.escape(search, quote=True)}" data-chars="{topic.chars}">
<td class="topic-title">{escaped_title}</td><td><code>{escaped_id}</code></td><td><code>{escaped_path}</code></td><td>{_number(topic.chars)}</td><td>{_number(topic.bytes)}</td><td>{_number(topic.lines)}</td><td>{topic.version}</td><td><time datetime="{html.escape(topic.updated_at, quote=True)}">{html.escape(topic.updated_at)}</time></td>
<td><details><summary>View metadata</summary><dl class="detail-grid"><dt>Versions</dt><dd>{topic.version_count}</dd><dt>Cache entries</dt><dd>{topic.cache_entry_count}</dd><dt>Imported</dt><dd>{imported_at}</dd><dt>Source SHA-256</dt><dd><code>{source_sha}</code></dd></dl>{preview}</details></td>
</tr>"""


def _distribution(lengths: list[int]) -> str:
    buckets = (
        ("0–999 chars", 0, 1_000),
        ("1,000–4,999", 1_000, 5_000),
        ("5,000–9,999", 5_000, 10_000),
        ("10,000–24,999", 10_000, 25_000),
        ("25,000+", 25_000, None),
    )
    counts = [
        sum(1 for length in lengths if length >= lower and (upper is None or length < upper))
        for _label, lower, upper in buckets
    ]
    maximum = max(counts) or 1
    items = "".join(
        f'<li><span>{html.escape(label)}</span><span class="bar-track"><span class="bar" style="width: {count / maximum * 100:.2f}%"></span></span><strong>{count}</strong></li>'
        for (label, _lower, _upper), count in zip(buckets, counts, strict=True)
    )
    return f'<ul aria-label="Topic character count buckets">{items}</ul>'


def _number(value: int | float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
