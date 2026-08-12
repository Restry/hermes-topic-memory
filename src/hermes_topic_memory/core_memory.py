from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

CORE_ID = "core"
CORE_DISPLAY_NAME = "Core / WhoAmI"
CORE_MAX_BODY_CHARS = 4_000
CORE_ROLLBACK_CONFIRMATION = CORE_DISPLAY_NAME
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_VERSION_FILE = re.compile(r"^([1-9][0-9]*)\.md$")


class CoreMemoryError(RuntimeError):
    """Stable Core mutation/read failure suitable for sanitized API mapping."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CoreMemoryNotFoundError(CoreMemoryError):
    def __init__(self) -> None:
        super().__init__("core_memory_not_found", "Core memory is not initialized")


class CoreMemoryExistsError(CoreMemoryError):
    def __init__(self) -> None:
        super().__init__("core_memory_exists", "Core memory is already initialized")


class CoreMemoryCorruptionError(CoreMemoryError):
    def __init__(self, message: str = "Core memory state is corrupt") -> None:
        super().__init__("operation_failed", message)


class ConcurrentCoreMemoryUpdateError(CoreMemoryError):
    def __init__(self) -> None:
        super().__init__("concurrent_update", "Core memory changed concurrently")


@dataclass(frozen=True, slots=True)
class CoreMemoryMetadata:
    memory_id: str
    display_name: str
    version: int
    content_fingerprint: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_fingerprint": self.content_fingerprint,
            "created_at": self.created_at,
            "display_name": self.display_name,
            "memory_id": self.memory_id,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CoreMemoryMetadata:
        expected_fields = {
            "content_fingerprint",
            "created_at",
            "display_name",
            "memory_id",
            "updated_at",
            "version",
        }
        if set(value) != expected_fields:
            raise ValueError("invalid Core metadata fields")
        version = value["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("invalid Core version")
        fingerprint = value["content_fingerprint"]
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("invalid Core fingerprint")
        created_at = value["created_at"]
        updated_at = value["updated_at"]
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("invalid Core creation timestamp")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("invalid Core update timestamp")
        memory_id = value["memory_id"]
        display_name = value["display_name"]
        if memory_id != CORE_ID or display_name != CORE_DISPLAY_NAME:
            raise ValueError("invalid Core identity")
        return cls(
            memory_id=memory_id,
            display_name=display_name,
            version=version,
            content_fingerprint=fingerprint,
            created_at=created_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True, slots=True)
class CoreMemory:
    metadata: CoreMemoryMetadata
    body: str


class CoreMemoryStore:
    """Low-level canonical Core files; callers serialize access and transactions."""

    def __init__(self, data_root: str | Path) -> None:
        root_input = Path(data_root).expanduser()
        if root_input.is_symlink():
            raise ValueError("data root must not be a symlink")
        self.root = root_input.resolve(strict=False)
        self.core_dir = self.root / "core-memory"
        self.memory_path = self.core_dir / "memory.md"
        self.metadata_path = self.core_dir / "metadata.json"
        self.versions_dir = self.core_dir / "versions"

    def read_optional_unlocked(self) -> CoreMemory | None:
        if self.core_dir.is_symlink():
            raise CoreMemoryCorruptionError("Core memory directory must not be a symlink")
        memory_exists = self.memory_path.exists() or self.memory_path.is_symlink()
        metadata_exists = self.metadata_path.exists() or self.metadata_path.is_symlink()
        if not memory_exists and not metadata_exists:
            return None
        if not memory_exists or not metadata_exists:
            raise CoreMemoryCorruptionError("Core canonical files are incomplete")
        if (
            self.memory_path.is_symlink()
            or self.metadata_path.is_symlink()
            or not self.memory_path.is_file()
            or not self.metadata_path.is_file()
        ):
            raise CoreMemoryCorruptionError("Core canonical files are invalid")
        try:
            payload = self.memory_path.read_bytes()
            body = payload.decode("utf-8", errors="strict")
            _validate_body(body)
            metadata_value = json.loads(
                self.metadata_path.read_bytes().decode("utf-8", errors="strict")
            )
            if not isinstance(metadata_value, dict):
                raise TypeError("Core metadata root must be an object")
            metadata = CoreMemoryMetadata.from_dict(metadata_value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise CoreMemoryCorruptionError() from error
        if metadata.content_fingerprint != _fingerprint(body):
            raise CoreMemoryCorruptionError("Core metadata does not match canonical body")
        return CoreMemory(metadata=metadata, body=body)

    def initialize_unlocked(self, body: str) -> CoreMemory:
        _validate_body(body)
        if self.read_optional_unlocked() is not None:
            raise CoreMemoryExistsError()
        now = _utc_now()
        metadata = CoreMemoryMetadata(
            memory_id=CORE_ID,
            display_name=CORE_DISPLAY_NAME,
            version=1,
            content_fingerprint=_fingerprint(body),
            created_at=now,
            updated_at=now,
        )
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.memory_path, body.encode("utf-8"))
        self._write_metadata(metadata)
        return CoreMemory(metadata=metadata, body=body)

    def replace_unlocked(
        self,
        body: str,
        *,
        expected_version: int,
        expected_fingerprint: str,
    ) -> CoreMemory:
        _validate_body(body)
        _validate_preconditions(expected_version, expected_fingerprint)
        current = self.read_optional_unlocked()
        if current is None:
            raise CoreMemoryNotFoundError()
        if (
            current.metadata.version != expected_version
            or current.metadata.content_fingerprint != expected_fingerprint
        ):
            raise ConcurrentCoreMemoryUpdateError()
        if current.body == body:
            raise ValueError("replacement body must differ from current body")
        self._write_snapshot(current.metadata.version, current.body)
        metadata = replace(
            current.metadata,
            version=current.metadata.version + 1,
            content_fingerprint=_fingerprint(body),
            updated_at=_utc_now(),
        )
        _atomic_write(self.memory_path, body.encode("utf-8"))
        self._write_metadata(metadata)
        return CoreMemory(metadata=metadata, body=body)

    def read_version_unlocked(self, version: int) -> str:
        _validate_version(version)
        current = self.read_optional_unlocked()
        if current is None:
            raise CoreMemoryNotFoundError()
        if version == current.metadata.version:
            return current.body
        path = self.version_path(version)
        if path.is_symlink() or not path.is_file():
            raise CoreMemoryNotFoundError()
        try:
            body = path.read_bytes().decode("utf-8", errors="strict")
            _validate_body(body)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise CoreMemoryCorruptionError("Core version body is invalid") from error
        return body

    def list_versions_unlocked(self) -> tuple[int, ...]:
        current = self.read_optional_unlocked()
        if current is None:
            raise CoreMemoryNotFoundError()
        if self.versions_dir.is_symlink():
            raise CoreMemoryCorruptionError("Core versions directory must not be a symlink")
        historical: set[int] = set()
        if self.versions_dir.is_dir():
            for path in self.versions_dir.iterdir():
                match = _VERSION_FILE.fullmatch(path.name)
                if match is not None:
                    if path.is_symlink() or not path.is_file():
                        raise CoreMemoryCorruptionError("Core version file is invalid")
                    historical.add(int(match.group(1)))
        expected_historical = set(range(1, current.metadata.version))
        if historical != expected_historical:
            raise CoreMemoryCorruptionError("Core version history is incomplete")
        return tuple(range(1, current.metadata.version + 1))

    def version_path(self, version: int) -> Path:
        _validate_version(version)
        return self.versions_dir / f"{version}.md"

    def _write_snapshot(self, version: int, body: str) -> None:
        path = self.version_path(version)
        payload = body.encode("utf-8")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise CoreMemoryCorruptionError("Core version snapshot conflicts with canonical body")
            return
        _atomic_write(path, payload)

    def _write_metadata(self, metadata: CoreMemoryMetadata) -> None:
        payload = json.dumps(
            metadata.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        _atomic_write(self.metadata_path, payload)


class CoreMemoryService:
    """Serialize manual Core reads/writes, history, audit, and crash recovery."""

    def __init__(self, data_root: str | Path) -> None:
        self.store = CoreMemoryStore(data_root)
        self.root = self.store.root
        self._lock_path = self.root / ".core-memory.lock"
        self._transaction_path = self.root / ".core-memory-transaction"
        self._audit_path = self.root / "operation-audit" / "core-memory.jsonl"

    def get_optional(self) -> CoreMemory | None:
        core_dir = self.store.core_dir
        has_pending = self._transaction_path.exists() or self._transaction_path.is_symlink()
        if (
            not has_pending
            and not core_dir.is_symlink()
            and not self.store.memory_path.exists()
            and not self.store.memory_path.is_symlink()
            and not self.store.metadata_path.exists()
            and not self.store.metadata_path.is_symlink()
        ):
            return None
        with self._locked():
            return self.store.read_optional_unlocked()

    def get(self) -> CoreMemory:
        current = self.get_optional()
        if current is None:
            raise CoreMemoryNotFoundError()
        return current

    def initialize(self, body: str) -> CoreMemory:
        _validate_body(body)
        with self._locked():
            if self.store.read_optional_unlocked() is not None:
                raise CoreMemoryExistsError()
            with self._transaction((self.store.core_dir, self._audit_path)):
                initialized = self.store.initialize_unlocked(body)
                self._append_audit(
                    "initialize",
                    version=initialized.metadata.version,
                    previous_version=None,
                    target_version=None,
                )
            return initialized

    def replace(
        self,
        body: str,
        *,
        expected_version: int,
        expected_fingerprint: str,
    ) -> CoreMemory:
        _validate_body(body)
        _validate_preconditions(expected_version, expected_fingerprint)
        with self._locked():
            current = self.store.read_optional_unlocked()
            if current is None:
                raise CoreMemoryNotFoundError()
            snapshot_path = self.store.version_path(current.metadata.version)
            paths = (
                self.store.memory_path,
                self.store.metadata_path,
                snapshot_path,
                self._audit_path,
            )
            with self._transaction(paths):
                updated = self.store.replace_unlocked(
                    body,
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
                self._append_audit(
                    "replace",
                    version=updated.metadata.version,
                    previous_version=current.metadata.version,
                    target_version=None,
                )
            return updated

    def rollback(
        self,
        target_version: int,
        *,
        expected_version: int,
        expected_fingerprint: str,
        confirmation: str,
    ) -> CoreMemory:
        _validate_version(target_version)
        _validate_preconditions(expected_version, expected_fingerprint)
        if confirmation != CORE_ROLLBACK_CONFIRMATION:
            raise CoreMemoryError(
                "confirmation_mismatch", "Core rollback confirmation does not match"
            )
        with self._locked():
            current = self.store.read_optional_unlocked()
            if current is None:
                raise CoreMemoryNotFoundError()
            if (
                current.metadata.version != expected_version
                or current.metadata.content_fingerprint != expected_fingerprint
            ):
                raise ConcurrentCoreMemoryUpdateError()
            if target_version >= current.metadata.version:
                raise ValueError("rollback target must be historical")
            historical_body = self.store.read_version_unlocked(target_version)
            snapshot_path = self.store.version_path(current.metadata.version)
            paths = (
                self.store.memory_path,
                self.store.metadata_path,
                snapshot_path,
                self._audit_path,
            )
            with self._transaction(paths):
                updated = self.store.replace_unlocked(
                    historical_body,
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
                self._append_audit(
                    "rollback",
                    version=updated.metadata.version,
                    previous_version=current.metadata.version,
                    target_version=target_version,
                )
            return updated

    def list_versions(self) -> tuple[int, ...]:
        with self._locked():
            return self.store.list_versions_unlocked()

    def read_version(self, version: int) -> str:
        _validate_version(version)
        with self._locked():
            return self.store.read_version_unlocked(version)

    def version_snapshot(self) -> tuple[CoreMemory, tuple[tuple[int, str], ...]]:
        with self._locked():
            current = self.store.read_optional_unlocked()
            if current is None:
                raise CoreMemoryNotFoundError()
            versions = self.store.list_versions_unlocked()
            bodies = tuple(
                (version, self.store.read_version_unlocked(version))
                for version in versions
            )
            return current, bodies

    def read_version_snapshot(self, version: int) -> tuple[CoreMemory, str]:
        _validate_version(version)
        with self._locked():
            current = self.store.read_optional_unlocked()
            if current is None:
                raise CoreMemoryNotFoundError()
            return current, self.store.read_version_unlocked(version)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._recover_pending_transaction()
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _transaction(self, paths: tuple[Path, ...]) -> Iterator[None]:
        if self._transaction_path.exists() or self._transaction_path.is_symlink():
            raise CoreMemoryError("operation_failed", "Pending Core transaction was not recovered")
        backup_root = self._transaction_path / "backup"
        entries: list[dict[str, Any]] = []
        try:
            backup_root.mkdir(parents=True)
            for index, path in enumerate(paths):
                relative = self._relative_path(path)
                existed = path.exists() or path.is_symlink()
                backup_name = str(index)
                if existed:
                    _copy_path(path, backup_root / backup_name)
                entries.append(
                    {
                        "backup": backup_name,
                        "existed": existed,
                        "path": relative.as_posix(),
                    }
                )
            self._fsync_snapshot_tree()
            _atomic_json_write(
                self._transaction_path / "manifest.json", {"entries": entries}
            )
            _fsync_directory(self._transaction_path)
        except BaseException:
            _remove_path(self._transaction_path)
            if self.root.is_dir():
                _fsync_directory(self.root)
            raise

        try:
            yield
        except BaseException:
            self._restore_transaction(entries)
            raise
        else:
            _remove_path(self._transaction_path)
            _fsync_directory(self.root)

    def _recover_pending_transaction(self) -> None:
        if not (self._transaction_path.exists() or self._transaction_path.is_symlink()):
            return
        manifest_path = self._transaction_path / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            _remove_path(self._transaction_path)
            _fsync_directory(self.root)
            return
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = value["entries"]
            if not isinstance(entries, list):
                raise TypeError("transaction entries must be a list")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CoreMemoryError(
                "operation_failed", "Pending Core transaction is corrupt"
            ) from error
        self._restore_transaction(entries)

    def _restore_transaction(self, entries: list[dict[str, Any]]) -> None:
        backup_root = self._transaction_path / "backup"
        restored = False
        try:
            for entry in reversed(entries):
                if not isinstance(entry, dict):
                    raise CoreMemoryError(
                        "operation_failed", "Pending Core transaction entry is invalid"
                    )
                relative_text = entry.get("path")
                backup_name = entry.get("backup")
                existed = entry.get("existed")
                if (
                    not isinstance(relative_text, str)
                    or not isinstance(backup_name, str)
                    or not isinstance(existed, bool)
                ):
                    raise CoreMemoryError(
                        "operation_failed", "Pending Core transaction entry is invalid"
                    )
                target = self.root / relative_text
                self._relative_path(target)
                _remove_path(target)
                if existed:
                    backup = backup_root / backup_name
                    if not (backup.exists() or backup.is_symlink()):
                        raise CoreMemoryError(
                            "operation_failed", "Pending Core transaction backup is missing"
                        )
                    _copy_path(backup, target)
                    _fsync_directory(target.parent)
            restored = True
        finally:
            if restored:
                _remove_path(self._transaction_path)
                _fsync_directory(self.root)

    def _append_audit(
        self,
        action: str,
        *,
        version: int,
        previous_version: int | None,
        target_version: int | None,
    ) -> None:
        record: dict[str, Any] = {
            "action": action,
            "memory_id": CORE_ID,
            "recorded_at": _utc_now(),
            "version": version,
        }
        if previous_version is not None:
            record["previous_version"] = previous_version
        if target_version is not None:
            record["target_version"] = target_version
        payload = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self._audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("incomplete Core operation audit write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self._audit_path.parent)

    def _relative_path(self, path: Path) -> Path:
        try:
            return path.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Core transaction path escapes data root") from error

    def _fsync_snapshot_tree(self) -> None:
        directories = [
            path
            for path in self._transaction_path.rglob("*")
            if path.is_dir() and not path.is_symlink()
        ]
        for directory in reversed(directories):
            _fsync_directory(directory)
        _fsync_directory(self._transaction_path)


def _validate_body(body: str) -> None:
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Core body must be non-empty text")
    if len(body) > CORE_MAX_BODY_CHARS:
        raise ValueError(f"Core body must not exceed {CORE_MAX_BODY_CHARS} characters")


def _validate_version(version: int) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Core version must be a positive integer")


def _validate_preconditions(expected_version: int, expected_fingerprint: str) -> None:
    _validate_version(expected_version)
    if (
        not isinstance(expected_fingerprint, str)
        or not _FINGERPRINT.fullmatch(expected_fingerprint)
    ):
        raise ValueError("Expected Core fingerprint must be lowercase sha256")


def _fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    _atomic_write(path, payload)


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


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=True)
    else:
        shutil.copy2(source, destination)




def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
