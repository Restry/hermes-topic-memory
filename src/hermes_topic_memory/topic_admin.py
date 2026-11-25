from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .evidence import EvidenceLedger
from .models import TopicMemory, TopicMetadata, TopicState
from .operations import apply_evidence_update as run_evidence_update
from .router import (
    TopicConfig,
    TopicRegistry,
    normalize_repo_url,
    normalize_workspace_path,
)
from .store import ConcurrentTopicUpdateError, FileTopicStore, TopicNotFoundError
from .updater import UpdateResult

_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FailureInjector = Callable[[str], None]


class TopicAdminError(RuntimeError):
    """Stable mutation failure suitable for mapping to a Panel or CLI error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TrashedTopic:
    topic_id: str
    display_name: str
    state: TopicState
    trashed_at: str


@dataclass(frozen=True, slots=True)
class PermanentDeleteResult:
    status: str
    topic_id: str
    deleted_at: str


class TopicAdminService:
    """Coordinate all interactive topic mutations under one data-root lock."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        root_input = Path(data_root).expanduser()
        if root_input.is_symlink():
            raise ValueError("data root must not be a symlink")
        self.root = root_input.resolve(strict=False)
        self.store = FileTopicStore(self.root)
        self._failure_injector = failure_injector
        self._registry_path = self.root / "topic-registry.json"
        self._transaction_path = self.root / ".topic-admin-transaction"
        self._operations_audit_path = self.root / "operation-audit" / "operations.jsonl"

    def create_topic(
        self,
        topic_id: str,
        display_name: str,
        aliases: tuple[str, ...],
        body: str,
        *,
        chat_ids: tuple[str, ...] = (),
        repo_urls: tuple[str, ...] = (),
        workspace_paths: tuple[str, ...] = (),
        make_default: bool = False,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        self._validate_topic_names(display_name, aliases)
        if not isinstance(body, str) or not body:
            raise ValueError("topic body must be non-empty text")
        contexts = self._normalize_contexts(chat_ids, repo_urls, workspace_paths)

        with self._locked():
            registry = self._load_registry()
            self._check_create_uniqueness(registry, topic_id, display_name, aliases)
            item = {
                "topic_id": topic_id,
                "display_name": display_name,
                "aliases": list(aliases),
                **{key: list(values) for key, values in contexts.items()},
                "state": "active",
            }
            candidate = self._copy_json(registry)
            candidate["topics"].append(item)
            if make_default:
                candidate["default_topic"] = topic_id
            self._validate_registry(candidate)

            paths = (
                self._registry_path,
                self.root / "topics" / topic_id,
                self._operations_audit_path,
            )
            with self._transaction(paths):
                created = self.store.create_topic(
                    topic_id,
                    body,
                    display_name=display_name,
                    aliases=aliases,
                )
                self._failpoint("after_topic_write")
                self._write_registry(candidate)
                self._append_operation_audit(
                    "create", topic_id, created.metadata.version, "active"
                )
            return created

    def update_topic(
        self,
        topic_id: str,
        *,
        chat_ids: tuple[str, ...] | None = None,
        repo_urls: tuple[str, ...] | None = None,
        workspace_paths: tuple[str, ...] | None = None,
    ) -> TopicConfig:
        """Replace supplied context bindings while preserving all other fields."""
        self._validate_topic_id(topic_id)
        contexts = self._normalize_contexts(chat_ids, repo_urls, workspace_paths)
        with self._locked():
            return self._update_context_locked(topic_id, contexts, append=False)

    def bind_context(
        self,
        topic_id: str,
        *,
        chat_id: str | None = None,
        repo_url: str | None = None,
        workspace_path: str | None = None,
    ) -> TopicConfig:
        """Add context bindings idempotently under the data-root mutation lock."""
        self._validate_topic_id(topic_id)
        if chat_id is None and repo_url is None and workspace_path is None:
            raise ValueError("at least one context binding is required")
        contexts = self._normalize_contexts(
            (chat_id,) if chat_id is not None else None,
            (repo_url,) if repo_url is not None else None,
            (workspace_path,) if workspace_path is not None else None,
        )
        with self._locked():
            return self._update_context_locked(topic_id, contexts, append=True)

    def _update_context_locked(
        self, topic_id: str, contexts: dict[str, tuple[str, ...]], *, append: bool
    ) -> TopicConfig:
        registry = self._load_registry()
        item = self._registered_item(registry, topic_id)
        self._require_state(item, ("active",))
        current = self.store.get_topic(topic_id)
        candidate = self._copy_json(registry)
        updated = self._registered_item(candidate, topic_id)
        existing = next(
            config for config in TopicRegistry.from_dict(registry).topics
            if config.topic_id == topic_id
        )
        for field, values in contexts.items():
            prior = getattr(existing, field) if append else ()
            updated[field] = list(dict.fromkeys((*prior, *values)))
        self._validate_registry(candidate)
        result = next(
            config for config in TopicRegistry.from_dict(candidate).topics
            if config.topic_id == topic_id
        )
        if candidate == registry:
            return result
        with self._transaction((self._registry_path, self._operations_audit_path)):
            self._write_registry(candidate)
            self._failpoint("after_registry_write")
            self._append_operation_audit(
                "bind_context" if append else "update_context",
                topic_id,
                current.metadata.version,
                current.metadata.state,
            )
        return result

    @staticmethod
    def _normalize_contexts(
        chat_ids: tuple[str, ...] | None,
        repo_urls: tuple[str, ...] | None,
        workspace_paths: tuple[str, ...] | None,
    ) -> dict[str, tuple[str, ...]]:
        contexts: dict[str, tuple[str, ...]] = {}
        for field, values, normalize in (
            ("chat_ids", chat_ids, str.strip),
            ("repo_urls", repo_urls, normalize_repo_url),
            ("workspace_paths", workspace_paths, normalize_workspace_path),
        ):
            if values is None:
                continue
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{field} must be non-empty strings")
            contexts[field] = tuple(dict.fromkeys(normalize(value) for value in values))
        return contexts

    def archive(
        self,
        topic_id: str,
        *,
        expected_version: int,
        expected_fingerprint: str | None = None,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        with self._locked():
            registry = self._load_registry()
            item = self._registered_item(registry, topic_id)
            self._require_state(item, ("active",))
            current = self._current_with_preconditions(
                topic_id, expected_version, expected_fingerprint
            )
            candidate = self._copy_json(registry)
            self._registered_item(candidate, topic_id)["state"] = "archived"
            paths = (
                self._registry_path,
                self._metadata_path(topic_id),
                self._operations_audit_path,
            )
            try:
                with self._transaction(paths):
                    self._write_registry(candidate)
                    archived = self.store.set_topic_state(
                        topic_id,
                        "archived",
                        expected_version=current.metadata.version,
                        expected_state="active",
                    )
                    self._failpoint("after_metadata_write")
                    self._append_operation_audit(
                        "archive", topic_id, archived.metadata.version, "archived"
                    )
            except ConcurrentTopicUpdateError as error:
                raise TopicAdminError(
                    "concurrent_update", "topic changed before archive"
                ) from error
            return archived

    def restore(
        self,
        topic_id: str,
        *,
        expected_version: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        with self._locked():
            registry = self._load_registry()
            item = self._registered_item(registry, topic_id)
            self._require_state(item, ("archived",))
            if expected_version is None and expected_fingerprint is None:
                current = self.store.get_topic(topic_id)
            elif expected_version is None:
                raise ValueError("expected version is required")
            else:
                current = self._current_with_preconditions(
                    topic_id, expected_version, expected_fingerprint
                )
            candidate = self._copy_json(registry)
            self._registered_item(candidate, topic_id)["state"] = "active"
            paths = (
                self._registry_path,
                self._metadata_path(topic_id),
                self._operations_audit_path,
            )
            try:
                with self._transaction(paths):
                    restored = self.store.set_topic_state(
                        topic_id,
                        "active",
                        expected_version=current.metadata.version,
                        expected_state="archived",
                    )
                    self._failpoint("after_metadata_write")
                    self._write_registry(candidate)
                    self._append_operation_audit(
                        "restore", topic_id, restored.metadata.version, "active"
                    )
            except ConcurrentTopicUpdateError as error:
                raise TopicAdminError(
                    "concurrent_update", "topic changed before restore"
                ) from error
            return restored

    def trash(
        self,
        topic_id: str,
        *,
        expected_version: int,
        expected_fingerprint: str | None = None,
    ) -> TrashedTopic:
        self._validate_topic_id(topic_id)
        with self._locked():
            registry = self._load_registry()
            item = self._registered_item(registry, topic_id)
            self._require_state(item, ("active", "archived"))
            current = self._current_with_preconditions(
                topic_id, expected_version, expected_fingerprint
            )
            recycle = self.root / "recycle-bin" / topic_id
            if recycle.exists() or recycle.is_symlink():
                raise TopicAdminError("invalid_state", "topic already exists in recycle bin")

            prior_item = self._copy_json(item)
            prior_state = self._item_state(item)
            candidate = self._copy_json(registry)
            candidate["topics"] = [
                raw for raw in candidate["topics"] if raw.get("topic_id") != topic_id
            ]
            was_default = candidate.get("default_topic") == topic_id
            if was_default:
                candidate.pop("default_topic", None)
            self._validate_registry(candidate)
            trashed_at = _utc_now()
            tombstone = {
                "topic_id": topic_id,
                "display_name": str(item["display_name"]),
                "display_confirmation": str(item["display_name"]),
                "prior_registry_item": prior_item,
                "prior_state": prior_state,
                "created_at": current.metadata.created_at,
                "trashed_at": trashed_at,
                "was_default": was_default,
            }
            topic_dir = self.root / "topics" / topic_id
            paths = (
                self._registry_path,
                topic_dir,
                recycle,
                self._operations_audit_path,
            )
            try:
                with self._transaction(paths):
                    self._write_registry(candidate)
                    self.store.set_topic_state(
                        topic_id,
                        "trashed",
                        expected_version=current.metadata.version,
                        expected_state=prior_state,
                    )
                    self._failpoint("after_metadata_write")
                    recycle.mkdir(parents=True, exist_ok=False)
                    os.replace(topic_dir, recycle / "topic")
                    _fsync_directory(topic_dir.parent)
                    _fsync_directory(recycle)
                    self._atomic_json_write(recycle / "tombstone.json", tombstone)
                    self._failpoint("after_trash_move")
                    self._append_operation_audit(
                        "trash", topic_id, current.metadata.version, "trashed"
                    )
            except ConcurrentTopicUpdateError as error:
                raise TopicAdminError(
                    "concurrent_update", "topic changed before trash"
                ) from error
            return TrashedTopic(
                topic_id=topic_id,
                display_name=str(item["display_name"]),
                state="trashed",
                trashed_at=trashed_at,
            )

    def restore_from_trash(
        self, topic_id: str, *, expected_trashed_at: str | None = None
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        with self._locked():
            registry = self._load_registry()
            if any(raw.get("topic_id") == topic_id for raw in registry["topics"]):
                raise TopicAdminError("topic_exists", "topic is already registered")
            recycle = self.root / "recycle-bin" / topic_id
            recycled_topic = recycle / "topic"
            tombstone = self._read_tombstone(recycle, topic_id)
            if expected_trashed_at is not None and tombstone.get("trashed_at") != expected_trashed_at:
                raise TopicAdminError("concurrent_update", "recycle tombstone changed")
            if not recycled_topic.is_dir() or recycled_topic.is_symlink():
                raise TopicAdminError("topic_not_found", "recycled topic body is missing")
            target = self.root / "topics" / topic_id
            if target.exists() or target.is_symlink():
                raise TopicAdminError("topic_exists", "canonical topic directory already exists")

            prior_item = tombstone.get("prior_registry_item")
            if not isinstance(prior_item, dict) or prior_item.get("topic_id") != topic_id:
                raise TopicAdminError("operation_failed", "invalid recycle tombstone")
            prior_state = tombstone.get("prior_state")
            if prior_state not in ("active", "archived"):
                raise TopicAdminError("operation_failed", "invalid prior topic state")
            restored_item = self._copy_json(prior_item)
            restored_item["state"] = prior_state
            candidate = self._copy_json(registry)
            candidate["topics"].append(restored_item)
            if tombstone.get("was_default") is True and "default_topic" not in candidate:
                candidate["default_topic"] = topic_id
            self._check_create_uniqueness(
                registry,
                topic_id,
                str(restored_item.get("display_name", "")),
                tuple(str(alias) for alias in restored_item.get("aliases", ())),
            )
            self._validate_registry(candidate)

            paths = (
                self._registry_path,
                target,
                recycle,
                self._operations_audit_path,
            )
            with self._transaction(paths):
                metadata_path = recycled_topic / "metadata.json"
                metadata = self._read_metadata_path(metadata_path, topic_id)
                if metadata.state != "trashed":
                    raise TopicAdminError("invalid_state", "recycled metadata is not trashed")
                self._write_metadata_path(
                    metadata_path,
                    replace(metadata, state=prior_state, updated_at=_utc_now()),
                )
                self._failpoint("after_metadata_write")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(recycled_topic, target)
                _fsync_directory(target.parent)
                self._remove_path(recycle)
                _fsync_directory(recycle.parent)
                self._write_registry(candidate)
                self._failpoint("after_restore_move")
                self._append_operation_audit(
                    "restore_from_trash", topic_id, metadata.version, prior_state
                )
            return self.store.get_topic(topic_id)

    def full_edit(
        self,
        topic_id: str,
        body: str,
        *,
        expected_version: int,
        expected_fingerprint: str,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        if not isinstance(body, str) or not body:
            raise ValueError("topic body must be non-empty text")
        with self._locked():
            return self._full_edit_locked(
                self._load_registry(),
                topic_id,
                body,
                expected_version=expected_version,
                expected_fingerprint=expected_fingerprint,
                action="replace",
            )

    def append_text(
        self,
        topic_id: str,
        text: str,
        *,
        expected_version: int,
        expected_fingerprint: str,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("append text must contain non-whitespace text")
        with self._locked():
            registry = self._load_registry()
            item = self._registered_item(registry, topic_id)
            self._require_state(item, ("active",))
            current = self._current_with_preconditions(
                topic_id, expected_version, expected_fingerprint
            )
            if text in current.body:
                raise TopicAdminError("append_duplicate", "append text already exists")
            return self._replace_current_locked(
                current, current.body + text, action="append"
            )

    def evidence_backed_edit(
        self,
        topic_id: str,
        body: str,
        *,
        evidence_ids: tuple[str, ...],
        reason: str,
    ) -> UpdateResult:
        self._validate_topic_id(topic_id)
        with self._locked():
            registry = self._load_registry()
            return run_evidence_update(
                store=self.store,
                ledger=EvidenceLedger(self.root),
                topic_id=topic_id,
                body=body,
                evidence_ids=evidence_ids,
                reason=reason,
                replace_topic=lambda memory, replacement: self._full_edit_locked(
                    registry,
                    topic_id,
                    replacement,
                    expected_version=memory.metadata.version,
                    expected_fingerprint=memory.metadata.content_fingerprint,
                ),
            )

    def rollback(self, topic_id: str, target_version: int) -> TopicMemory:
        self._validate_topic_id(topic_id)
        with self._locked():
            registry = self._load_registry()
            item = self._registered_item(registry, topic_id)
            self._require_state(item, ("active",))
            current = self.store.get_topic(topic_id)
            historical_body = self.store.read_version(topic_id, target_version)
            return self._replace_current_locked(current, historical_body, action="rollback")

    def rollback_if_current(
        self,
        topic_id: str,
        target_version: int,
        *,
        expected_version: int,
        expected_fingerprint: str,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        if not isinstance(target_version, int) or isinstance(target_version, bool):
            raise ValueError("target version must be an integer")
        with self._locked():
            registry = self._load_registry()
            item = self._registered_item(registry, topic_id)
            self._require_state(item, ("active",))
            current = self._current_with_preconditions(
                topic_id, expected_version, expected_fingerprint
            )
            historical_body = self.store.read_version(topic_id, target_version)
            return self._replace_current_locked(current, historical_body, action="rollback")

    def _full_edit_locked(
        self,
        registry: dict[str, Any],
        topic_id: str,
        body: str,
        *,
        expected_version: int,
        expected_fingerprint: str,
        action: str = "replace",
    ) -> TopicMemory:
        item = self._registered_item(registry, topic_id)
        self._require_state(item, ("active",))
        current = self._current_with_preconditions(
            topic_id, expected_version, expected_fingerprint
        )
        return self._replace_current_locked(current, body, action=action)

    def _replace_current_locked(
        self, current: TopicMemory, body: str, *, action: str
    ) -> TopicMemory:
        topic_id = current.metadata.topic_id
        cache_path = self.root / "qa-cache" / f"{topic_id}.jsonl"
        paths = (
            self.root / "topics" / topic_id / "memory.md",
            self._metadata_path(topic_id),
            self.root
            / "topics"
            / topic_id
            / "versions"
            / f"{current.metadata.version}-memory.md",
            cache_path,
            self._operations_audit_path,
        )
        try:
            with self._transaction(paths):
                updated = self.store.update_topic(
                    topic_id,
                    body,
                    expected_version=current.metadata.version,
                    expected_fingerprint=current.metadata.content_fingerprint,
                )
                self._failpoint("after_topic_write")
                if cache_path.exists() or cache_path.is_symlink():
                    cache_path.unlink()
                    _fsync_directory(cache_path.parent)
                self._failpoint("after_cache_invalidation")
                self._append_operation_audit(
                    action,
                    topic_id,
                    updated.metadata.version,
                    updated.metadata.state,
                )
        except ConcurrentTopicUpdateError as error:
            raise TopicAdminError(
                "concurrent_update", "topic changed before full edit"
            ) from error
        return updated

    def permanent_delete(
        self,
        topic_id: str,
        *,
        confirmation: str,
        expected_trashed_at: str | None = None,
    ) -> PermanentDeleteResult:
        self._validate_topic_id(topic_id)
        if not isinstance(confirmation, str):
            raise ValueError("confirmation must be text")
        with self._locked():
            registry = self._load_registry()
            if any(raw.get("topic_id") == topic_id for raw in registry["topics"]):
                raise TopicAdminError(
                    "invalid_state", "permanent deletion requires a recycled topic"
                )
            recycle = self.root / "recycle-bin" / topic_id
            tombstone = self._read_tombstone(recycle, topic_id)
            if expected_trashed_at is not None and tombstone.get("trashed_at") != expected_trashed_at:
                raise TopicAdminError("concurrent_update", "recycle tombstone changed")
            display_name = tombstone.get("display_confirmation")
            if not isinstance(display_name, str):
                raise TopicAdminError("operation_failed", "invalid recycle confirmation")
            if confirmation != display_name:
                raise TopicAdminError(
                    "confirmation_mismatch", "display-name confirmation does not match"
                )
            metadata = self._read_metadata_path(
                recycle / "topic" / "metadata.json", topic_id
            )

            deleted_at = _utc_now()
            assets = (
                self.root / "topics" / topic_id,
                recycle,
                self.root / "qa-cache" / f"{topic_id}.jsonl",
                self.root / "qa-cache" / f".{topic_id}.lock",
                self.root / "update-proposals" / f"{topic_id}.jsonl",
                self.root / "update-proposals" / f".{topic_id}.lock",
                self.root / "update-proposals" / topic_id,
                self.root / "evidence" / f"{topic_id}.jsonl",
                self.root / "evidence" / topic_id,
            )
            deletion_audit_path = self.root / "operation-audit" / "deletions.jsonl"
            with self._transaction(
                (*assets, deletion_audit_path, self._operations_audit_path)
            ):
                self._append_deletion_audit(topic_id, display_name, deleted_at)
                self._append_operation_audit(
                    "permanent_delete", topic_id, metadata.version, "deleted"
                )
                self._failpoint("before_permanent_remove")
                for path in assets:
                    self._remove_path(path)
                for parent in {path.parent for path in assets if path.parent.is_dir()}:
                    _fsync_directory(parent)
                self._failpoint("after_permanent_remove")
            return PermanentDeleteResult(
                status="permanently_deleted", topic_id=topic_id, deleted_at=deleted_at
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".topic-admin.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._recover_pending_transaction()
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _transaction(self, paths: tuple[Path, ...]) -> Iterator[None]:
        if self._transaction_path.exists() or self._transaction_path.is_symlink():
            raise TopicAdminError("operation_failed", "pending transaction was not recovered")
        backup_root = self._transaction_path / "backup"
        entries: list[dict[str, Any]] = []
        try:
            backup_root.mkdir(parents=True)
            for index, path in enumerate(paths):
                relative = self._relative_path(path)
                existed = path.exists() or path.is_symlink()
                backup_name = str(index)
                if existed:
                    self._copy_path(path, backup_root / backup_name)
                entries.append(
                    {"path": relative.as_posix(), "existed": existed, "backup": backup_name}
                )
            self._fsync_snapshot_tree()
            self._atomic_json_write(
                self._transaction_path / "manifest.json", {"entries": entries}
            )
            _fsync_directory(self._transaction_path)
        except BaseException:
            self._remove_path(self._transaction_path)
            if self.root.is_dir():
                _fsync_directory(self.root)
            raise

        try:
            yield
        except BaseException:
            self._restore_transaction(entries)
            raise
        else:
            self._remove_path(self._transaction_path)
            _fsync_directory(self.root)

    def _recover_pending_transaction(self) -> None:
        if not (self._transaction_path.exists() or self._transaction_path.is_symlink()):
            return
        manifest_path = self._transaction_path / "manifest.json"
        if not manifest_path.is_file():
            self._remove_path(self._transaction_path)
            _fsync_directory(self.root)
            return
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = value["entries"]
            if not isinstance(entries, list):
                raise TypeError("transaction entries must be a list")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TopicAdminError(
                "operation_failed", "pending topic transaction is corrupt"
            ) from error
        self._restore_transaction(entries)

    def _restore_transaction(self, entries: list[dict[str, Any]]) -> None:
        backup_root = self._transaction_path / "backup"
        restored = False
        try:
            for entry in reversed(entries):
                relative_text = entry.get("path")
                backup_name = entry.get("backup")
                existed = entry.get("existed")
                if (
                    not isinstance(relative_text, str)
                    or not isinstance(backup_name, str)
                    or not isinstance(existed, bool)
                ):
                    raise TopicAdminError(
                        "operation_failed", "pending transaction entry is invalid"
                    )
                target = self.root / relative_text
                self._relative_path(target)
                self._remove_path(target)
                if existed:
                    backup = backup_root / backup_name
                    if not (backup.exists() or backup.is_symlink()):
                        raise TopicAdminError(
                            "operation_failed", "pending transaction backup is missing"
                        )
                    self._copy_path(backup, target)
                    _fsync_directory(target.parent)
            restored = True
        finally:
            if restored:
                self._remove_path(self._transaction_path)
                if self.root.is_dir():
                    _fsync_directory(self.root)


    def _load_registry(self) -> dict[str, Any]:
        if not self._registry_path.is_file():
            return {"topics": []}
        try:
            value = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TopicAdminError("operation_failed", "topic registry is invalid") from error
        if not isinstance(value, dict) or not isinstance(value.get("topics"), list):
            raise TopicAdminError("operation_failed", "topic registry is invalid")
        self._validate_registry(value)
        return value

    def _write_registry(self, value: dict[str, Any]) -> None:
        self._validate_registry(value)
        self._atomic_json_write(self._registry_path, value)

    @staticmethod
    def _validate_registry(value: dict[str, Any]) -> None:
        try:
            TopicRegistry.from_dict(value)
        except (TypeError, ValueError) as error:
            raise TopicAdminError("alias_conflict", "topic registry would be ambiguous") from error

    def _registered_item(self, registry: dict[str, Any], topic_id: str) -> dict[str, Any]:
        for item in registry["topics"]:
            if isinstance(item, dict) and item.get("topic_id") == topic_id:
                return item
        raise TopicAdminError("topic_not_found", "topic is not registered")

    def _current_with_version(self, topic_id: str, expected_version: int) -> TopicMemory:
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("expected version must be an integer")
        try:
            current = self.store.get_topic(topic_id)
        except TopicNotFoundError as error:
            raise TopicAdminError("topic_not_found", "topic does not exist") from error
        if current.metadata.version != expected_version:
            raise TopicAdminError("concurrent_update", "topic version no longer matches")
        return current

    def _current_with_preconditions(
        self,
        topic_id: str,
        expected_version: int,
        expected_fingerprint: str | None,
    ) -> TopicMemory:
        current = self._current_with_version(topic_id, expected_version)
        if expected_fingerprint is not None:
            if not isinstance(expected_fingerprint, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_fingerprint
            ):
                raise ValueError("expected fingerprint must be lowercase sha256")
            if current.metadata.content_fingerprint != expected_fingerprint:
                raise TopicAdminError(
                    "concurrent_update", "topic fingerprint no longer matches"
                )
        return current

    @staticmethod
    def _require_state(item: dict[str, Any], allowed: tuple[TopicState, ...]) -> None:
        state = TopicAdminService._item_state(item)
        if state not in allowed:
            raise TopicAdminError("invalid_state", "topic lifecycle state does not allow this")

    @staticmethod
    def _item_state(item: dict[str, Any]) -> TopicState:
        state = item.get("state", "active")
        if state not in ("active", "archived", "trashed"):
            raise TopicAdminError("operation_failed", "topic registry state is invalid")
        return state

    def _check_create_uniqueness(
        self,
        registry: dict[str, Any],
        topic_id: str,
        display_name: str,
        aliases: tuple[str, ...],
    ) -> None:
        existing_ids = {
            str(item.get("topic_id", "")).casefold()
            for item in registry["topics"]
            if isinstance(item, dict)
        }
        if topic_id.casefold() in existing_ids:
            raise TopicAdminError("topic_exists", "topic id already exists")
        normalized_incoming = {
            _normalize_name(value) for value in (topic_id, display_name, *aliases)
        }
        existing_names: set[str] = set()
        for item in registry["topics"]:
            if not isinstance(item, dict):
                raise TopicAdminError("operation_failed", "topic registry is invalid")
            names = [
                str(item.get("topic_id", "")),
                str(item.get("display_name", item.get("topic_id", ""))),
            ]
            raw_aliases = item.get("aliases", ())
            if not isinstance(raw_aliases, (list, tuple)):
                raise TopicAdminError("operation_failed", "topic registry is invalid")
            names.extend(str(alias) for alias in raw_aliases)
            existing_names.update(_normalize_name(name) for name in names)
        if existing_names.intersection(normalized_incoming):
            raise TopicAdminError("alias_conflict", "topic name or alias already exists")

    def _read_tombstone(self, recycle: Path, topic_id: str) -> dict[str, Any]:
        path = recycle / "tombstone.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TopicAdminError("topic_not_found", "recycled topic does not exist") from error
        if not isinstance(value, dict) or value.get("topic_id") != topic_id:
            raise TopicAdminError("operation_failed", "invalid recycle tombstone")
        return value

    def _append_operation_audit(
        self, action: str, topic_id: str, version: int, state: str
    ) -> None:
        payload = json.dumps(
            {
                "action": action,
                "recorded_at": _utc_now(),
                "state": state,
                "topic_id": topic_id,
                "version": version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path = self._operations_audit_path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("incomplete operation audit write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(path.parent)

    def _append_deletion_audit(
        self, topic_id: str, display_name: str, deleted_at: str
    ) -> None:
        audit_dir = self.root / "operation-audit"
        path = audit_dir / "deletions.jsonl"
        payload = json.dumps(
            {
                "action": "permanent_delete",
                "deleted_at": deleted_at,
                "display_name": display_name,
                "topic_id": topic_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        audit_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("incomplete deletion audit write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(audit_dir)

    def _read_metadata_path(self, path: Path, topic_id: str) -> TopicMetadata:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError("metadata root must be an object")
            metadata = TopicMetadata.from_dict(value)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TopicAdminError("operation_failed", "topic metadata is invalid") from error
        if metadata.topic_id != topic_id:
            raise TopicAdminError("operation_failed", "topic metadata id does not match")
        return metadata

    def _write_metadata_path(self, path: Path, metadata: TopicMetadata) -> None:
        self._atomic_json_write(path, metadata.to_dict())

    def _metadata_path(self, topic_id: str) -> Path:
        return self.root / "topics" / topic_id / "metadata.json"

    def _failpoint(self, point: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(point)

    def _relative_path(self, path: Path) -> Path:
        try:
            return path.relative_to(self.root)
        except ValueError as error:
            raise ValueError("transaction path escapes data root") from error

    def _fsync_snapshot_tree(self) -> None:
        directories = [
            path
            for path in self._transaction_path.rglob("*")
            if path.is_dir() and not path.is_symlink()
        ]
        for directory in reversed(directories):
            _fsync_directory(directory)
        _fsync_directory(self._transaction_path)

    @staticmethod
    def _copy_path(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        elif source.is_dir():
            shutil.copytree(
                source,
                destination,
                copy_function=_link_or_copy,
                symlinks=True,
            )
        else:
            _link_or_copy(source, destination)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        TopicAdminService._atomic_write(path, payload)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _copy_json(value: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _validate_topic_id(topic_id: str) -> None:
        if not isinstance(topic_id, str) or not _TOPIC_ID.fullmatch(topic_id):
            raise ValueError(f"invalid topic id: {topic_id!r}")

    @staticmethod
    def _validate_topic_names(display_name: str, aliases: tuple[str, ...]) -> None:
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("display name must be non-empty text")
        if not isinstance(aliases, tuple) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise ValueError("aliases must be non-empty strings")


def _normalize_name(value: str) -> str:
    return value.strip().casefold()


def _link_or_copy(source: str | Path, destination: str | Path) -> str:
    try:
        os.link(source, destination)
        return str(destination)
    except OSError:
        return shutil.copy2(source, destination)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
