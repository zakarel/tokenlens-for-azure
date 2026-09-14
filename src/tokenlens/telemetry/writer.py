"""Shared rotating JSONL writer for canonical telemetry.

The writer is the only component that touches disk. It is deliberately small,
synchronous, and loud about failure: instrumentation must not break a model call
because telemetry cannot be written, but a failure must never look like success.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import TelemetryConfig
from .privacy import assert_contentless
from .schema import CanonicalRecord, record_json

ErrorCallback = Callable[[BaseException, str], None]

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_MAX_BUFFERED_LINES = 512


@lru_cache(maxsize=8)
def _FILE_PATTERN(prefix: str) -> re.Pattern[str]:
    """Match only TokenLens's own dated output files."""
    return re.compile(rf"^{re.escape(prefix)}-\d{{4}}-\d{{2}}-\d{{2}}(\.\d{{3}})?\.jsonl$")


class TelemetryWriteError(OSError):
    """Raised in strict mode when a telemetry line cannot be persisted."""


class TelemetryWriter:
    """Append canonical records to rotating, user-private JSONL files."""

    def __init__(
        self,
        config: TelemetryConfig | None = None,
        *,
        on_error: ErrorCallback | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or TelemetryConfig.from_env()
        self._on_error = on_error
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._dropped = 0
        self._written = 0
        self._skipped_duplicates = 0
        self._last_error: str | None = None
        self._warned = 0
        self._retention_day: date | None = None
        self._known_ids: set[str] | None = None

    # -- introspection -------------------------------------------------
    @property
    def dropped_events(self) -> int:
        return self._dropped

    @property
    def written_events(self) -> int:
        return self._written

    @property
    def skipped_duplicates(self) -> int:
        """Records already present in the output directory that were not rewritten."""
        return self._skipped_duplicates

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def current_path(self, when: datetime | None = None) -> Path:
        stamp = (when or self._clock()).astimezone(UTC)
        return self.config.output_dir / f"{self.config.file_prefix}-{stamp:%Y-%m-%d}.jsonl"

    def output_files(self) -> list[Path]:
        """Every TokenLens-owned JSONL file in the configured output directory."""
        root = self.config.output_dir
        if not root.is_dir():
            return []
        return sorted(
            entry
            for entry in root.iterdir()
            if entry.is_file() and not entry.is_symlink() and _FILE_PATTERN(self.config.file_prefix).match(entry.name)
        )

    def known_event_ids(self, *, refresh: bool = False) -> set[str]:
        """Event identifiers already persisted in the output directory.

        Collection and import are idempotent because canonical records carry a
        deterministic ``event_id``. Re-running the same window therefore appends
        nothing instead of duplicating buckets or spans.
        """
        if self._known_ids is not None and not refresh:
            return self._known_ids
        known: set[str] = set()
        for path in self.output_files():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event_id = payload.get("event_id") if isinstance(payload, dict) else None
                        if isinstance(event_id, str):
                            known.add(event_id)
            except OSError as exc:
                self._fail(exc, "dedupe-scan-failed")
        self._known_ids = known
        return known

    # -- writing -------------------------------------------------------
    def write(self, record: CanonicalRecord, *, skip_existing: bool = False) -> bool:
        """Write one canonical record. Returns ``True`` when it reached disk.

        With ``skip_existing`` the record is compared against the identifiers
        already persisted in the output directory, so re-importing or
        re-collecting the same window is a no-op rather than a duplicate.
        """
        if skip_existing and record.event_id in self.known_event_ids():
            self._skipped_duplicates += 1
            return False
        payload = assert_contentless(record_json(record))
        written = self.write_payload(payload, when=record.timestamp)
        if written and self._known_ids is not None:
            self._known_ids.add(record.event_id)
        return written

    def write_all(self, records: Iterable[CanonicalRecord], *, skip_existing: bool = True) -> int:
        """Write a batch idempotently. Returns the number of new records persisted."""
        return sum(1 for record in records if self.write(record, skip_existing=skip_existing))

    def write_payload(self, payload: dict[str, Any], *, when: datetime | None = None) -> bool:
        if not self.config.enabled:
            return False
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        if len(line) > _MAX_BUFFERED_LINES * 1024:
            self._fail(ValueError("telemetry record exceeds the bounded line size"), "oversized-record")
            return False
        with self._lock:
            try:
                path = self._prepare(when)
                # Retention runs before the append so the file this record lands
                # in can never be pruned by the same call that persisted it.
                self._apply_retention(keep=path)
                self._append(path, line)
            except OSError as exc:
                self._fail(exc, "write-failed")
                return False
            self._written += 1
            return True

    def flush(self) -> None:
        """No-op retained as an explicit host-application hook.

        Each line is appended with ``os.write`` on an ``O_APPEND`` descriptor and
        the descriptor is closed immediately, so there is never buffered state.
        """
        return None

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "TelemetryWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------
    def _prepare(self, when: datetime | None) -> Path:
        directory = self.config.output_dir
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        path = self.current_path(when)
        return self._rotate_for_size(path)

    def _rotate_for_size(self, path: Path) -> Path:
        limit = int(self.config.max_mb * 1024 * 1024)
        if limit <= 0 or not path.exists() or path.stat().st_size < limit:
            return path
        for part in range(1, 1000):
            candidate = path.with_name(f"{path.stem}.{part:03d}{path.suffix}")
            if not candidate.exists() or candidate.stat().st_size < limit:
                return candidate
        raise OSError("telemetry rotation exhausted 999 same-day parts")

    def _append(self, path: Path, line: str) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
        try:
            # One write syscall per line keeps concurrent appends atomic on
            # POSIX for lines under PIPE_BUF-sized kernel buffers, and keeps the
            # file valid JSONL even when several processes share it.
            os.write(descriptor, line.encode("utf-8"))
        finally:
            os.close(descriptor)

    def _apply_retention(self, *, keep: Path | None = None) -> None:
        """Prune expired local files by modification time.

        Retention measures how long a file has existed locally, not the
        timestamps of the records inside it. Backfilled telemetry — an imported
        OpenTelemetry export or an Azure Monitor window covering older days —
        writes to a date-named file whose contents predate the retention window,
        and pruning by that in-record date would delete data the same call just
        reported as persisted. Modification time keeps retention a statement
        about local storage age, which is what a retention policy protects.
        """
        days = self.config.retention_days
        now = self._clock().astimezone(UTC)
        if days <= 0 or self._retention_day == now.date():
            return
        self._retention_day = now.date()
        cutoff = (now - timedelta(days=days)).timestamp()
        root = self.config.output_dir
        pattern = _FILE_PATTERN(self.config.file_prefix)
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            self._fail(exc, "retention-scan-failed")
            return
        keep_name = keep.name if keep is not None else None
        for entry in entries:
            if not pattern.match(entry.name) or entry.name == keep_name:
                continue
            # Retention only ever removes TokenLens's own dated files inside the
            # configured root. Symlinks and directories are left untouched.
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except OSError as exc:
                self._fail(exc, "retention-scan-failed")
                continue
            try:
                entry.unlink()
            except OSError as exc:
                self._fail(exc, "retention-delete-failed")
            else:
                if self._known_ids is not None:
                    # The cached identifier index no longer reflects disk.
                    self._known_ids = None

    def _fail(self, exc: BaseException, reason: str) -> None:
        self._dropped += 1
        self._last_error = reason
        if self._on_error is not None:
            self._on_error(exc, reason)
        if self.config.strict:
            raise TelemetryWriteError(f"telemetry {reason}") from exc
        # Rate-limit console warnings so a failing disk cannot flood an
        # application's logs, but never stay completely silent.
        if self._warned < 3:
            self._warned += 1
            import warnings

            warnings.warn(
                f"TokenLens telemetry {reason}; {self._dropped} event(s) dropped",
                RuntimeWarning,
                stacklevel=3,
            )
