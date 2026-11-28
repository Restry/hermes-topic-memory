from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .evidence import EvidenceLedger, EvidenceRecord
from .reader import Reader, ReaderRequest, ReaderResponse
from .remember import RememberService
from .router import TopicRegistry
from .store import (
    ConcurrentTopicUpdateError,
    FileTopicStore,
    TopicNotFoundError,
    TopicStateError,
)
from .topic_admin import TopicAdminService
from .updater import append_update_audit

_MAX_BODY_CHARS = 200_000
_MAX_SUMMARY_CHARS = 160
_MAX_EVIDENCE_ITEMS = 200
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

DreamStatus = Literal[
    "consolidated",
    "no_change",
    "dry_run",
    "skipped_no_new_evidence",
    "aborted_concurrent_write",
    "error",
]


@dataclass(frozen=True, slots=True)
class DreamReport:
    topic_id: str
    status: DreamStatus
    before_version: int
    after_version: int
    before_chars: int
    after_chars: int
    evidence_count: int
    pruned_count: int
    merged_count: int
    conflicts_resolved: int
    summary: str
    changes: tuple[str, ...]
    diff: str
    dry_run: bool
    last_evidence_id: str | None
    created_at: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["changes"] = list(self.changes)
        return payload


@dataclass(frozen=True, slots=True)
class DreamBatchReport:
    dry_run: bool
    force: bool
    total_topics: int
    consolidated: int
    no_change: int
    skipped: int
    dry_run_count: int
    aborted: int
    errors: int
    reports: tuple[DreamReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "force": self.force,
            "total_topics": self.total_topics,
            "consolidated": self.consolidated,
            "no_change": self.no_change,
            "skipped": self.skipped,
            "dry_run_count": self.dry_run_count,
            "aborted": self.aborted,
            "errors": self.errors,
            "reports": [item.to_dict() for item in self.reports],
        }


class DreamConsolidator:
    """Offline slow-path memory consolidation ("Dreaming") for topic memories.

    Decouples fast daytime appends (`remember`) from periodic offline cleanup:
    1. Orientation: load current canonical `memory.md` and prior dream watermark.
    2. Gather signal: collect only new evidence since the last committed dream.
    3. Consolidate (read-only): merge duplicates, resolve contradictions in favor
       of newer evidence, prune one-off status chatter, normalize relative dates,
       and refresh the one-line catalog summary.
    4. Shadow verify & atomic commit: re-check version under per-topic flock so
       concurrent daytime writes are never clobbered; archive snapshot (`versions/`)
       and bump version to invalidate stale QA cache entries for this topic only.
    """

    def __init__(self, data_root: str | Path, model: Reader) -> None:
        self.data_root = Path(data_root)
        self.model = model
        self.store = FileTopicStore(self.data_root)
        self.admin = TopicAdminService(self.data_root)
        self.remember_service = RememberService(self.data_root)
        self.reports_dir = self.data_root / "dream-reports"

    def dream_topic(
        self,
        topic_id: str,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> DreamReport:
        self.store._validate_topic_id(topic_id)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        memory = self.store.get_topic(topic_id)
        if memory.metadata.state != "active":
            raise TopicStateError(
                f"topic is read-only in state {memory.metadata.state}: {topic_id}"
            )

        before_version = memory.metadata.version
        before_body = memory.body
        before_chars = len(before_body)

        ledger = EvidenceLedger(self.data_root)
        all_records = ledger.records(topic_id)
        latest_evidence_id = all_records[-1].evidence_id if all_records else None

        last_watermark = self._last_committed_watermark(topic_id)
        if not force and last_watermark is not None:
            last_ver, last_ev = last_watermark
            if before_version <= last_ver and latest_evidence_id == last_ev:
                return DreamReport(
                    topic_id=topic_id,
                    status="skipped_no_new_evidence",
                    before_version=before_version,
                    after_version=before_version,
                    before_chars=before_chars,
                    after_chars=before_chars,
                    evidence_count=0,
                    pruned_count=0,
                    merged_count=0,
                    conflicts_resolved=0,
                    summary="",
                    changes=(),
                    diff="",
                    dry_run=dry_run,
                    last_evidence_id=latest_evidence_id,
                    created_at=now,
                )

        new_records = _slice_since_watermark(
            all_records, last_watermark[1] if last_watermark else None
        )
        if len(new_records) > _MAX_EVIDENCE_ITEMS:
            new_records = new_records[-_MAX_EVIDENCE_ITEMS:]

        # Phase 3: read-only LLM consolidation outside the write lock.
        prefix = _build_dream_prompt(topic_id, before_body, new_records, now[:10])
        try:
            response = self.model.read(
                ReaderRequest(
                    prefix=prefix,
                    question="Consolidate this topic memory and return strict JSON only.",
                )
            )
            if not isinstance(response, ReaderResponse):
                raise ValueError("dream model must return ReaderResponse")
            proposal = _parse_dream_output(response.answer)
        except Exception as error:
            report = DreamReport(
                topic_id=topic_id,
                status="error",
                before_version=before_version,
                after_version=before_version,
                before_chars=before_chars,
                after_chars=before_chars,
                evidence_count=len(new_records),
                pruned_count=0,
                merged_count=0,
                conflicts_resolved=0,
                summary="",
                changes=(),
                diff="",
                dry_run=dry_run,
                last_evidence_id=latest_evidence_id,
                created_at=now,
                error=str(error),
            )
            self._append_report(report)
            return report

        action = proposal["action"]
        summary = proposal["summary"]
        new_body = proposal["new_body"]
        changes = tuple(proposal["changes"])
        pruned_count = proposal["pruned_count"]
        merged_count = proposal["merged_count"]
        conflicts_resolved = proposal["conflicts_resolved"]

        effective_body = before_body if action == "no_change" else new_body
        body_changed = effective_body.strip() != before_body.strip()
        diff_text = _unified_diff(topic_id, before_body, effective_body) if body_changed else ""

        if dry_run:
            report = DreamReport(
                topic_id=topic_id,
                status="dry_run" if body_changed else "no_change",
                before_version=before_version,
                after_version=before_version,
                before_chars=before_chars,
                after_chars=len(effective_body),
                evidence_count=len(new_records),
                pruned_count=pruned_count,
                merged_count=merged_count,
                conflicts_resolved=conflicts_resolved,
                summary=summary,
                changes=changes,
                diff=diff_text,
                dry_run=True,
                last_evidence_id=latest_evidence_id,
                created_at=now,
            )
            self._append_report(report)
            return report

        # Phase 4: acquire per-topic write lock and verify optimistic version.
        with self.remember_service._write_lock(topic_id):
            current = self.store.get_topic(topic_id)
            if current.metadata.state != "active":
                raise TopicStateError(
                    f"topic is read-only in state {current.metadata.state}: {topic_id}"
                )
            if current.metadata.version != before_version:
                report = DreamReport(
                    topic_id=topic_id,
                    status="aborted_concurrent_write",
                    before_version=before_version,
                    after_version=current.metadata.version,
                    before_chars=before_chars,
                    after_chars=len(current.body),
                    evidence_count=len(new_records),
                    pruned_count=0,
                    merged_count=0,
                    conflicts_resolved=0,
                    summary="",
                    changes=(),
                    diff="",
                    dry_run=False,
                    last_evidence_id=latest_evidence_id,
                    created_at=now,
                    error="Topic version changed while dream consolidation was running.",
                )
                self._append_report(report)
                return report

            after_version = before_version
            status: DreamStatus = "no_change"
            if body_changed:
                try:
                    updated = self.store.update_topic(
                        topic_id,
                        effective_body,
                        expected_version=before_version,
                    )
                except ConcurrentTopicUpdateError:
                    report = DreamReport(
                        topic_id=topic_id,
                        status="aborted_concurrent_write",
                        before_version=before_version,
                        after_version=self.store.get_topic(topic_id).metadata.version,
                        before_chars=before_chars,
                        after_chars=before_chars,
                        evidence_count=len(new_records),
                        pruned_count=0,
                        merged_count=0,
                        conflicts_resolved=0,
                        summary="",
                        changes=(),
                        diff="",
                        dry_run=False,
                        last_evidence_id=latest_evidence_id,
                        created_at=now,
                        error="Concurrent update detected during atomic commit.",
                    )
                    self._append_report(report)
                    return report
                after_version = updated.metadata.version
                status = "consolidated"
                append_update_audit(
                    self.data_root,
                    topic_id,
                    {
                        "action": "dream_consolidate",
                        "status": "applied",
                        "before_version": before_version,
                        "after_version": after_version,
                        "evidence_ids": [item.evidence_id for item in new_records],
                        "changes": list(changes),
                        "pruned_count": pruned_count,
                        "merged_count": merged_count,
                        "conflicts_resolved": conflicts_resolved,
                        "created_at": now,
                    },
                )

            if summary:
                try:
                    self.admin.update_summary(topic_id, summary)
                except Exception:
                    # Summary refresh must never roll back an already committed body.
                    pass

            report = DreamReport(
                topic_id=topic_id,
                status=status,
                before_version=before_version,
                after_version=after_version,
                before_chars=before_chars,
                after_chars=len(effective_body) if body_changed else before_chars,
                evidence_count=len(new_records),
                pruned_count=pruned_count,
                merged_count=merged_count,
                conflicts_resolved=conflicts_resolved,
                summary=summary,
                changes=changes,
                diff=diff_text,
                dry_run=False,
                last_evidence_id=latest_evidence_id,
                created_at=now,
            )
            self._append_report(report)
            return report

    def dream_all(
        self,
        *,
        dry_run: bool = False,
        force: bool = False,
        allowed_topics: frozenset[str] | None = None,
    ) -> DreamBatchReport:
        registry = TopicRegistry.from_file(self.data_root / "topic-registry.json")
        active_topics = [
            topic for topic in registry.topics
            if topic.state == "active" and (allowed_topics is None or topic.topic_id in allowed_topics)
        ]
        reports: list[DreamReport] = []
        for topic in active_topics:
            try:
                reports.append(
                    self.dream_topic(topic.topic_id, dry_run=dry_run, force=force)
                )
            except (TopicNotFoundError, TopicStateError, Exception) as error:
                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                reports.append(
                    DreamReport(
                        topic_id=topic.topic_id,
                        status="error",
                        before_version=0,
                        after_version=0,
                        before_chars=0,
                        after_chars=0,
                        evidence_count=0,
                        pruned_count=0,
                        merged_count=0,
                        conflicts_resolved=0,
                        summary="",
                        changes=(),
                        diff="",
                        dry_run=dry_run,
                        last_evidence_id=None,
                        created_at=now,
                        error=str(error),
                    )
                )
        return DreamBatchReport(
            dry_run=dry_run,
            force=force,
            total_topics=len(active_topics),
            consolidated=sum(1 for r in reports if r.status == "consolidated"),
            no_change=sum(1 for r in reports if r.status == "no_change"),
            skipped=sum(1 for r in reports if r.status == "skipped_no_new_evidence"),
            dry_run_count=sum(1 for r in reports if r.status == "dry_run"),
            aborted=sum(1 for r in reports if r.status == "aborted_concurrent_write"),
            errors=sum(1 for r in reports if r.status == "error"),
            reports=tuple(reports),
        )

    def _report_path(self, topic_id: str) -> Path:
        return self.reports_dir / f"{topic_id}.jsonl"

    def _append_report(self, report: DreamReport) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self._report_path(report.topic_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")

    def _last_committed_watermark(self, topic_id: str) -> tuple[int, str | None] | None:
        path = self._report_path(topic_id)
        if not path.is_file():
            return None
        watermark: tuple[int, str | None] | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("dry_run") is False and item.get("status") in {
                "consolidated",
                "no_change",
            }:
                after_version = item.get("after_version")
                if isinstance(after_version, int):
                    watermark = (after_version, item.get("last_evidence_id"))
        return watermark


def _slice_since_watermark(
    records: tuple[EvidenceRecord, ...], last_evidence_id: str | None
) -> tuple[EvidenceRecord, ...]:
    if not last_evidence_id:
        return records
    for idx, record in enumerate(records):
        if record.evidence_id == last_evidence_id:
            return records[idx + 1 :]
    return records


def _build_dream_prompt(
    topic_id: str,
    current_body: str,
    evidence: tuple[EvidenceRecord, ...],
    today_iso: str,
) -> str:
    ev_lines: list[str] = []
    for item in evidence:
        text = item.fact or item.user_text or ""
        agent = item.source_agent or "unknown"
        ev_lines.append(
            f"- [{item.created_at} | id={item.evidence_id} | agent={agent}] {text}"
        )
    evidence_block = "\n".join(ev_lines) if ev_lines else "(no new evidence since last dream)"
    return (
        "DREAM MEMORY CONSOLIDATION RULES:\n"
        f"Today's date is {today_iso}. You are performing offline memory consolidation "
        f"for topic '{topic_id}'.\n"
        "1. Merge duplicate or overlapping facts into one concise, well-structured bullet.\n"
        "2. Resolve contradictions in favor of newer dated evidence; remove superseded old claims.\n"
        "3. Prune one-off status chatter, ephemeral build logs, and stale debugging notes.\n"
        "4. Convert relative time expressions ('yesterday', 'last week') into absolute YYYY-MM-DD dates using evidence timestamps.\n"
        "5. Never invent facts not grounded in CURRENT MEMORY or NEW EVIDENCE.\n"
        "6. Produce a crisp one-line catalog summary (<= 160 chars) describing what this topic covers.\n"
        "Return strict JSON ONLY with exactly these keys:\n"
        '{"action":"consolidate"|"no_change","summary":"...","new_body":"...",'
        '"changes":["..."],"pruned_count":0,"merged_count":0,"conflicts_resolved":0}\n\n'
        f"CURRENT MEMORY:\n{current_body}\nEND CURRENT MEMORY\n\n"
        f"NEW EVIDENCE:\n{evidence_block}\nEND NEW EVIDENCE\n\n"
    )


def _parse_dream_output(raw_output: str) -> dict[str, Any]:
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ValueError("dream output must be non-empty JSON")
    cleaned = _CODE_FENCE.sub("", raw_output.strip()).strip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("dream output must be a JSON object")
    action = data.get("action")
    if action not in {"consolidate", "no_change"}:
        raise ValueError("dream output action must be 'consolidate' or 'no_change'")
    new_body = data.get("new_body", "")
    if action == "consolidate":
        if not isinstance(new_body, str) or not new_body.strip():
            raise ValueError("consolidate action requires non-empty new_body")
        if len(new_body) > _MAX_BODY_CHARS:
            raise ValueError("new_body exceeds maximum allowed length")
    elif not isinstance(new_body, str):
        raise ValueError("new_body must be a string")
    summary_raw = data.get("summary", "")
    if not isinstance(summary_raw, str):
        raise ValueError("summary must be a string")
    summary = " ".join(summary_raw.split())[:_MAX_SUMMARY_CHARS]
    changes_raw = data.get("changes", [])
    if not isinstance(changes_raw, list) or not all(isinstance(c, str) for c in changes_raw):
        raise ValueError("changes must be a list of strings")
    for count_key in ("pruned_count", "merged_count", "conflicts_resolved"):
        val = data.get(count_key, 0)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValueError(f"{count_key} must be a non-negative integer")
    return {
        "action": action,
        "summary": summary,
        "new_body": new_body,
        "changes": changes_raw,
        "pruned_count": data.get("pruned_count", 0),
        "merged_count": data.get("merged_count", 0),
        "conflicts_resolved": data.get("conflicts_resolved", 0),
    }


def _unified_diff(topic_id: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{topic_id}/memory.md (before)",
            tofile=f"{topic_id}/memory.md (after)",
        )
    )
