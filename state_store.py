"""Durable release state, rate limiting, and single-process locking.

The module deliberately does not log state records or HTTP arguments.  Release
records may contain paths and tracker metadata, while HTTP arguments may contain
credentials; callers should decide what is safe to present to the user.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, MutableMapping, TypeVar

try:  # pragma: no cover - exercised by the POSIX-only lock test
    import fcntl
except ImportError:  # Windows development hosts can still run state/rate tests.
    fcntl = None  # type: ignore[assignment]


STATE_VERSION = 2

_SENSITIVE_QUERY_FIELDS = frozenset(
    {
        "api_key",
        "api_token",
        "authorization",
        "authkey",
        "cookie",
        "cookies",
        "details_link",
        "download_link",
        "magnet",
        "magnet_link",
        "passkey",
        "password",
        "token",
    }
)

# ERROR is terminal on purpose: normal runs must never spend another API call on
# a release that was already attempted.  A deliberate state edit/migration is
# required if an operator wants to retry it.
TERMINAL_STATUSES = frozenset(
    {
        "API_ERROR",
        "AITHER_ATTEMPTED",
        "BLACKLISTED",
        "DUPLICATE",
        "ERROR",
        "FOUND",
        "LEGACY_SEEN",
        "MANUAL",
        "MISSING",
        "NOT_FOUND",
        "NOT_MEDIA",
        "RECHECK_REQUIRED",
        "UNRESOLVED",
    }
)


class StateError(RuntimeError):
    """Base class for durable-state failures."""


class StateFormatError(StateError):
    """Raised when a state file cannot be safely interpreted."""


class AlreadyRunningError(RuntimeError):
    """Raised when another process owns the single-process lock."""


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "releases": {},
        "rate_limits": {},
        "queries": {},
    }


def _canonical_query_value(value: Any) -> Any:
    """Return a stable JSON value suitable for query-key construction."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, Mapping):
        return {
            str(key).strip().casefold(): _canonical_query_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_query_value(item) for item in value]
    raise ValueError("Query-key components must be JSON-compatible values")


def _canonical_season(season: Any) -> Any:
    if season is None or isinstance(season, (bool, int, float)):
        return season
    if isinstance(season, str):
        normalized = season.strip().casefold()
        if normalized.startswith("s") and normalized[1:].isdigit():
            return int(normalized[1:])
        if normalized.isdigit():
            return int(normalized)
        return normalized
    return _canonical_query_value(season)


def build_query_key(
    identity: Any,
    category: Any,
    season: Any = None,
    **_release_specific: Any,
) -> str:
    """Build a deterministic cache key for an Aither identity query.

    Deliberately only identity, category, and season participate.  Extra
    release-specific keyword arguments (for example episode, type, or
    resolution) are accepted and ignored so episodes and technical variants
    share the same cached tracker response.
    """

    canonical = {
        "category": _canonical_query_value(category),
        "identity": _canonical_query_value(identity),
        "season": _canonical_season(season),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"q2:{hashlib.sha256(encoded).hexdigest()}"


def _sanitize_query_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StateFormatError("Query record keys must be strings")
            if key.strip().casefold() in _SENSITIVE_QUERY_FIELDS:
                continue
            sanitized[key] = _sanitize_query_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_query_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_query_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return copy.deepcopy(value)
    raise StateFormatError("Query records must contain only JSON-compatible values")


def _normalize_query_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise StateFormatError("A query record must be a JSON object")
    sanitized = _sanitize_query_value(record)
    assert isinstance(sanitized, dict)
    return sanitized


def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise StateFormatError("A release record must be a JSON object")

    normalized = copy.deepcopy(dict(record))
    status = normalized.get("status")
    if not isinstance(status, str) or not status.strip():
        raise StateFormatError("A release record must contain a non-empty status")

    status = status.strip().upper()
    if status not in TERMINAL_STATUSES:
        raise StateFormatError(f"Unsupported release status: {status}")
    normalized["status"] = status
    # Release states are deliberately terminal.  AITHER_ATTEMPTED and
    # RECHECK_REQUIRED have explicit, no-repeat recovery paths in the checker;
    # a persisted ``terminal: false`` must never make an already-decided or
    # unknown release silently searchable again.
    normalized["terminal"] = True

    processing = normalized.get("processing")
    if processing is not None and not isinstance(processing, Mapping):
        raise StateFormatError("The processing field must be a JSON object")
    if processing is not None:
        normalized["processing"] = copy.deepcopy(dict(processing))

    return normalized


def _normalize_state(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        releases: dict[str, dict[str, Any]] = {}
        for key in raw:
            if not isinstance(key, str) or not key:
                raise StateFormatError("Legacy state entries must be non-empty strings")
            releases[key] = {
                "status": "LEGACY_SEEN",
                "terminal": True,
                "candidate": None,
                "processing": {"status": "UNKNOWN"},
            }
        state = _empty_state()
        state["releases"] = releases
        return state

    if not isinstance(raw, Mapping):
        raise StateFormatError("State must be a legacy list or a versioned JSON object")

    version = raw.get("version")
    if version not in {1, STATE_VERSION}:
        raise StateFormatError("Unsupported state version")

    releases = raw.get("releases", {})
    rate_limits = raw.get("rate_limits", {})
    queries = raw.get("queries", {})
    if not isinstance(releases, Mapping):
        raise StateFormatError("The releases field must be a JSON object")
    if not isinstance(rate_limits, Mapping):
        raise StateFormatError("The rate_limits field must be a JSON object")
    if not isinstance(queries, Mapping):
        raise StateFormatError("The queries field must be a JSON object")

    normalized = copy.deepcopy(dict(raw))
    normalized["version"] = STATE_VERSION
    normalized["releases"] = {}
    for key, record in releases.items():
        if not isinstance(key, str) or not key:
            raise StateFormatError("Release keys must be non-empty strings")
        normalized["releases"][key] = _normalize_record(record)
    normalized["rate_limits"] = copy.deepcopy(dict(rate_limits))
    normalized["queries"] = {}
    for key, record in queries.items():
        if not isinstance(key, str) or not key:
            raise StateFormatError("Query keys must be non-empty strings")
        normalized["queries"][key] = _normalize_query_record(record)
    return normalized


MutationResult = TypeVar("MutationResult")


class StateStore:
    """In-memory view backed by an atomically replaced, mode-0600 JSON file.

    Mutating public methods save immediately.  ``save()`` remains available for
    explicit persistence after a caller intentionally edits a copied state and
    records it through a future extension of this API.
    """

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)
        self._mutex = threading.RLock()
        self._state, needs_migration = self._load()
        if needs_migration:
            self._save_unlocked()

    def _load(self) -> tuple[dict[str, Any], bool]:
        if not self.path.exists():
            return _empty_state(), False
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as error:
            raise StateFormatError(
                f"State file is not valid JSON: {self.path}"
            ) from error
        except OSError as error:
            raise StateError(f"State file cannot be read: {self.path}") from error
        return _normalize_state(raw), bool(
            isinstance(raw, list)
            or (isinstance(raw, Mapping) and raw.get("version") != STATE_VERSION)
        )

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            return copy.deepcopy(self._state)

    def get_release(self, key: str) -> dict[str, Any] | None:
        with self._mutex:
            record = self._state["releases"].get(key)
            return copy.deepcopy(record) if record is not None else None

    def is_terminal(self, key: str) -> bool:
        record = self.get_release(key)
        return bool(record and record.get("terminal"))

    def get_query(self, key: str) -> dict[str, Any] | None:
        """Return an isolated copy of a cached, sanitized query record."""

        with self._mutex:
            record = self._state["queries"].get(key)
            return copy.deepcopy(record) if record is not None else None

    def record_query(
        self,
        key: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically store caller-provided query facts after sanitization."""

        if not isinstance(key, str) or not key:
            raise ValueError("Query key must be a non-empty string")
        normalized = _normalize_query_record(record)

        def apply(state: MutableMapping[str, Any]) -> dict[str, Any]:
            state["queries"][key] = copy.deepcopy(normalized)
            return copy.deepcopy(normalized)

        return self._mutate_and_save(apply)

    def record_release(
        self,
        key: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically store status, candidate, and related release metadata."""

        if not isinstance(key, str) or not key:
            raise ValueError("Release key must be a non-empty string")
        normalized = _normalize_record(record)

        def apply(state: MutableMapping[str, Any]) -> dict[str, Any]:
            state["releases"][key] = copy.deepcopy(normalized)
            return copy.deepcopy(normalized)

        return self._mutate_and_save(apply)

    def terminal_records(
        self,
        status: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        wanted = status.strip().upper() if status is not None else None
        with self._mutex:
            result = {
                key: copy.deepcopy(record)
                for key, record in self._state["releases"].items()
                if record.get("terminal")
                and (wanted is None or record.get("status") == wanted)
            }
        return dict(sorted(result.items()))

    def bind_release_hashes(
        self,
        bindings: Mapping[str, tuple[str, ...]],
    ) -> int:
        """Bind multiple existing release records in one atomic state write.

        The caller owns infohash syntax validation.  This method deliberately
        accepts only immutable tuples of non-empty strings, so malformed
        external state cannot be smuggled into the no-repeat index.
        """

        normalized: dict[str, tuple[str, ...]] = {}
        for key, hashes in bindings.items():
            if not isinstance(key, str) or not key:
                raise ValueError("Release hash binding keys must be non-empty strings")
            if (
                not isinstance(hashes, tuple)
                or not hashes
                or any(not isinstance(value, str) or not value for value in hashes)
            ):
                raise ValueError("Release hash bindings must be non-empty string tuples")
            normalized[key] = tuple(hashes)

        if not normalized:
            return 0

        def apply(state: MutableMapping[str, Any]) -> int:
            for key, hashes in normalized.items():
                try:
                    record = state["releases"][key]
                except KeyError as error:
                    raise KeyError(f"Unknown release key: {key}") from error
                record["torrent_hashes"] = list(hashes)
            return len(normalized)

        return self._mutate_and_save(apply)

    def update_processing(
        self,
        key: str,
        processing: Mapping[str, Any] | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """Merge processing data without replacing the persisted candidate."""

        if processing is not None and not isinstance(processing, Mapping):
            raise ValueError("Processing update must be a mapping")
        merged_changes = copy.deepcopy(dict(processing or {}))
        merged_changes.update(copy.deepcopy(changes))

        def apply(state: MutableMapping[str, Any]) -> dict[str, Any]:
            try:
                record = state["releases"][key]
            except KeyError as error:
                raise KeyError(f"Unknown release key: {key}") from error
            current = record.get("processing")
            if current is None:
                current = {}
                record["processing"] = current
            if not isinstance(current, MutableMapping):
                raise StateFormatError("The processing field must be a JSON object")
            current.update(merged_changes)
            return copy.deepcopy(record)

        return self._mutate_and_save(apply)

    def rate_snapshot(self, name: str = "aither") -> dict[str, Any]:
        with self._mutex:
            bucket = self._state["rate_limits"].get(name, {})
            return copy.deepcopy(dict(bucket))

    def _mutate_rate(
        self,
        name: str,
        mutator: Callable[[MutableMapping[str, Any]], MutationResult],
    ) -> MutationResult:
        def apply(state: MutableMapping[str, Any]) -> MutationResult:
            bucket = state["rate_limits"].setdefault(name, {})
            if not isinstance(bucket, MutableMapping):
                raise StateFormatError("A rate-limit bucket must be a JSON object")
            return mutator(bucket)

        return self._mutate_and_save(apply)

    def _mutate_and_save(
        self,
        mutator: Callable[[MutableMapping[str, Any]], MutationResult],
    ) -> MutationResult:
        with self._mutex:
            previous = copy.deepcopy(self._state)
            try:
                result = mutator(self._state)
                self._save_unlocked()
            except BaseException:
                self._state = previous
                raise
            return result

    def save(self) -> None:
        with self._mutex:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        try:
            payload = json.dumps(
                self._state,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
        except (TypeError, ValueError) as error:
            raise StateFormatError("State contains a non-JSON value") from error

        parent = self.path.parent
        if not parent.is_dir():
            raise StateError(f"State directory does not exist: {parent}")

        descriptor: int | None = None
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=parent,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
            self._fsync_parent(parent)
        except OSError as error:
            raise StateError(f"State file cannot be saved: {self.path}") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _fsync_parent(parent: Path) -> None:
        if os.name != "posix":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class AttemptReservation:
    identifier: str
    number: int
    post_delay: float


CallResult = TypeVar("CallResult")


class AitherRateGate:
    """Persistently rate-limit actual Aither HTTP attempts.

    Callers must wrap the HTTP operation itself with ``call()`` or ``attempt()``.
    The reservation is saved before control reaches the HTTP code, so exceptions
    and HTTP 429 responses count.  A conservative in-flight deadline plus crash
    recovery prevents a restart from skipping the required delay.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        name: str = "aither",
        batch_size: int = 10,
        normal_delay: float = 5.0,
        batch_delay: float = 30.0,
        request_timeout: float = 30.0,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if min(normal_delay, batch_delay, request_timeout) < 0:
            raise ValueError("Rate-limit delays cannot be negative")
        self.store = store
        self.name = name
        self.batch_size = batch_size
        self.normal_delay = float(normal_delay)
        self.batch_delay = float(batch_delay)
        self.request_timeout = float(request_timeout)
        self.clock = clock
        self.sleeper = sleeper

    def call(
        self,
        operation: Callable[..., CallResult],
        *args: Any,
        **kwargs: Any,
    ) -> CallResult:
        reservation = self._begin_attempt()
        try:
            return operation(*args, **kwargs)
        finally:
            self._finish_attempt(reservation)

    run = call

    @contextmanager
    def attempt(self) -> Iterator[AttemptReservation]:
        reservation = self._begin_attempt()
        try:
            yield reservation
        finally:
            self._finish_attempt(reservation)

    @property
    def attempts_total(self) -> int:
        return int(self.store.rate_snapshot(self.name).get("attempts_total", 0))

    def _recover_interrupted_attempt(self) -> None:
        snapshot = self.store.rate_snapshot(self.name)
        in_flight = snapshot.get("in_flight")
        if not isinstance(in_flight, Mapping):
            return
        identifier = in_flight.get("identifier")
        post_delay = max(0.0, float(in_flight.get("post_delay", 0.0)))
        now = float(self.clock())

        def recover(bucket: MutableMapping[str, Any]) -> None:
            current = bucket.get("in_flight")
            if not isinstance(current, Mapping) or current.get("identifier") != identifier:
                return
            bucket["next_allowed_at"] = max(
                float(bucket.get("next_allowed_at", 0.0)),
                now + post_delay,
            )
            bucket["in_flight"] = None
            bucket["last_interrupted_at"] = now

        self.store._mutate_rate(self.name, recover)

    def _wait_for_previous_attempt(self) -> None:
        snapshot = self.store.rate_snapshot(self.name)
        deadline = float(snapshot.get("next_allowed_at", 0.0))
        remaining = deadline - float(self.clock())
        if remaining > 0:
            self.sleeper(remaining)

    def _begin_attempt(self) -> AttemptReservation:
        self._recover_interrupted_attempt()
        self._wait_for_previous_attempt()
        observed_now = float(self.clock())
        previous_deadline = float(
            self.store.rate_snapshot(self.name).get("next_allowed_at", 0.0)
        )
        effective_now = max(observed_now, previous_deadline)
        identifier = uuid.uuid4().hex

        def reserve(bucket: MutableMapping[str, Any]) -> AttemptReservation:
            total = int(bucket.get("attempts_total", 0)) + 1
            since_pause = int(bucket.get("attempts_since_pause", 0)) + 1
            if since_pause >= self.batch_size:
                post_delay = self.batch_delay
                since_pause = 0
            else:
                post_delay = self.normal_delay

            reservation = AttemptReservation(identifier, total, post_delay)
            bucket.update(
                {
                    "attempts_total": total,
                    "attempts_since_pause": since_pause,
                    "last_attempt_started_at": effective_now,
                    "next_allowed_at": (
                        effective_now + self.request_timeout + post_delay
                    ),
                    "in_flight": {
                        "identifier": identifier,
                        "number": total,
                        "started_at": effective_now,
                        "post_delay": post_delay,
                    },
                }
            )
            return reservation

        return self.store._mutate_rate(self.name, reserve)

    def _finish_attempt(self, reservation: AttemptReservation) -> None:
        finished_at = float(self.clock())

        def finish(bucket: MutableMapping[str, Any]) -> None:
            current = bucket.get("in_flight")
            if not isinstance(current, Mapping) or current.get("identifier") != reservation.identifier:
                raise StateError("Rate-limit reservation does not match active attempt")
            bucket["in_flight"] = None
            bucket["last_attempt_finished_at"] = finished_at
            bucket["next_allowed_at"] = finished_at + reservation.post_delay

        self.store._mutate_rate(self.name, finish)
        remaining = (
            float(self.store.rate_snapshot(self.name).get("next_allowed_at", 0.0))
            - float(self.clock())
        )
        if remaining > 0:
            self.sleeper(remaining)


PersistentAitherRateGate = AitherRateGate


class SingleProcessLock:
    """Non-blocking POSIX flock protected against a symlink lock target."""

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)
        self._handle: Any = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if fcntl is None:
            raise RuntimeError("Single-process locking requires POSIX fcntl")
        if self.acquired:
            return True

        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="ascii")
            descriptor = -1
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False
            self._handle = handle
            return True
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def release(self) -> None:
        if self._handle is None:
            return
        assert fcntl is not None
        handle, self._handle = self._handle, None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "SingleProcessLock":
        if not self.acquire():
            raise AlreadyRunningError("Another aither process is already running")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def acquire_single_process_lock(
    path: os.PathLike[str] | str,
) -> SingleProcessLock | None:
    lock = SingleProcessLock(path)
    return lock if lock.acquire() else None
