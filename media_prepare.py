"""Safe, interactive preparation of Aither upload candidates.

The duplicate checker owns the single versioned JSON state.  This module never
creates another candidate registry: its state helpers return modified copies of
the caller's state, while :class:`MediaPreparer` performs only an explicitly
reviewed filesystem transaction.  A ready TXT is a derived output, not state.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class MediaPrepareError(RuntimeError):
    """Base class for preparation failures."""


class UnsafePathError(MediaPrepareError):
    """An input, archive member, or destination crossed a safety boundary."""


class ArchiveError(MediaPrepareError):
    """An archive was incomplete, corrupt, unsafe, or could not be extracted."""


class OwnershipError(MediaPrepareError):
    """The exact qBittorrent owners could not be proven."""


class TransactionError(MediaPrepareError):
    """A transaction could not be committed or rolled back safely."""


class Decision(str, Enum):
    YES = "yes"
    NO = "no"
    SEPARATE = "separate"


class ProcessingStatus(str, Enum):
    READY = "READY"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    STALE = "STALE"


@dataclass(frozen=True)
class Candidate:
    """All qB data needed to prepare one physical release.

    ``hashes`` must already contain every exact duplicate qB owner.  Partial or
    ancestor/descendant overlaps are intentionally rejected by the caller's
    owner validator.
    """

    key: str
    name: str
    hashes: tuple[str, ...]
    save_path: Path
    content_path: Path
    file_names: tuple[str, ...]
    running_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key or not self.name:
            raise ValueError("Candidate key and name must be non-empty")
        hashes = tuple(sorted({str(item) for item in self.hashes if str(item)}))
        if not hashes:
            raise ValueError("Candidate must contain at least one torrent hash")
        files = tuple(dict.fromkeys(str(item) for item in self.file_names if str(item)))
        if not files:
            raise ValueError("Candidate must contain the qBittorrent file list")
        running = tuple(sorted({str(item) for item in self.running_hashes if str(item)}))
        if not set(running).issubset(hashes):
            raise ValueError("running_hashes must be a subset of hashes")
        object.__setattr__(self, "hashes", hashes)
        object.__setattr__(self, "file_names", files)
        object.__setattr__(self, "running_hashes", running)
        object.__setattr__(self, "save_path", Path(self.save_path))
        object.__setattr__(self, "content_path", Path(self.content_path))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "hashes": list(self.hashes),
            "save_path": str(self.save_path),
            "content_path": str(self.content_path),
            "file_names": list(self.file_names),
            "running_hashes": list(self.running_hashes),
        }

    @classmethod
    def from_mapping(
        cls, key: str, value: Mapping[str, Any]
    ) -> "Candidate":
        """Read both the canonical record and common qB-shaped variants."""

        raw: Mapping[str, Any] = value
        nested = raw.get("candidate")
        if isinstance(nested, Mapping):
            raw = nested
        torrent = raw.get("torrent")
        if isinstance(torrent, Mapping):
            merged = dict(torrent)
            merged.update(raw)
            raw = merged

        hash_values = raw.get("hashes")
        if isinstance(hash_values, str):
            hashes = (hash_values,)
        elif isinstance(hash_values, Sequence):
            hashes = tuple(str(item) for item in hash_values)
        else:
            one_hash = raw.get("hash") or raw.get("torrent_hash")
            hashes = (str(one_hash),) if one_hash else ()

        raw_files = raw.get("file_names", raw.get("files", ()))
        files: list[str] = []
        if isinstance(raw_files, Sequence) and not isinstance(raw_files, (str, bytes)):
            for item in raw_files:
                if isinstance(item, Mapping):
                    name = item.get("name")
                    if name is not None:
                        files.append(str(name))
                else:
                    files.append(str(item))

        running_values = raw.get("running_hashes", ())
        if isinstance(running_values, str):
            running = (running_values,)
        elif isinstance(running_values, Sequence):
            running = tuple(str(item) for item in running_values)
        else:
            running = ()

        name = str(raw.get("name") or raw.get("release_name") or "")
        save_path = raw.get("save_path")
        content_path = raw.get("content_path") or save_path
        if not name or save_path is None or content_path is None:
            raise ValueError(f"Incomplete media candidate record: {key}")
        return cls(
            key=str(raw.get("key") or key),
            name=name,
            hashes=hashes,
            save_path=Path(str(save_path)),
            content_path=Path(str(content_path)),
            file_names=tuple(files),
            running_hashes=running,
        )


@dataclass(frozen=True)
class ReviewBatch:
    decisions: tuple[tuple[str, Decision], ...]
    cancelled: bool = False

    def as_dict(self) -> dict[str, Decision]:
        return dict(self.decisions)


@dataclass(frozen=True)
class ProcessingResult:
    key: str
    status: ProcessingStatus
    mode: str
    ready_path: Path | None = None
    message: str = ""

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "mode": self.mode,
        }
        if self.ready_path is not None:
            result["ready_path"] = str(self.ready_path)
        if self.message:
            result["message"] = self.message
        return result


@dataclass(frozen=True)
class ReviewSummary:
    cancelled: bool
    reviewed: int
    ready: int
    skipped: int
    errors: int
    results: tuple[ProcessingResult, ...] = ()


@dataclass(frozen=True)
class ArchiveMember:
    relative_path: Path
    is_directory: bool
    size: int = 0

    def __post_init__(self) -> None:
        relative = safe_relative_path(str(self.relative_path), "archive member")
        if self.size < 0:
            raise ArchiveError("Archive member size cannot be negative")
        object.__setattr__(self, "relative_path", relative)


@dataclass(frozen=True)
class RarSet:
    first_volume: Path
    volumes: tuple[Path, ...]


@dataclass(frozen=True)
class MediaDetection:
    rar_sets: tuple[RarSet, ...]
    iso_files: tuple[Path, ...]
    direct_files: tuple[Path, ...]

    @property
    def wrapped(self) -> bool:
        return bool(self.rar_sets or self.iso_files)


@dataclass(frozen=True)
class SourceLayout:
    source_root: Path
    save_path: Path
    release_root: Path | None
    relative_root: Path
    owned_files: tuple[tuple[Path, Path], ...]
    flat: bool

    def relative_to_release(self, qbit_relative: Path) -> Path:
        if self.flat:
            if qbit_relative.parent != Path("."):
                raise UnsafePathError("Flat release unexpectedly contains a directory")
            return Path(qbit_relative.name)
        return qbit_relative.relative_to(self.relative_root)


class Extractor(Protocol):
    def inspect(self, source: Path) -> tuple[ArchiveMember, ...]: ...

    def extract(self, source: Path, destination: Path) -> None: ...


class QbitClientInterface(Protocol):
    def stop(self, hashes: Sequence[str]) -> None: ...

    def start(self, hashes: Sequence[str]) -> None: ...

    def remove_list_only(self, hashes: Sequence[str]) -> None: ...


class ProcessingStateStore(Protocol):
    def update_processing(
        self, key: str, processing: Mapping[str, Any] | None = None, **changes: Any
    ) -> Mapping[str, Any]: ...


OwnerValidator = Callable[[Candidate, tuple[Path, ...]], Iterable[str]]
SaveState = Callable[[Mapping[str, Any]], None]


def _state_copy(state: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(state))
    releases = result.setdefault("releases", {})
    if not isinstance(releases, dict):
        raise ValueError("state['releases'] must be a mapping")
    return result


def upsert_candidate_record(
    state: Mapping[str, Any],
    candidate: Candidate,
    *,
    terminal_status: str = "MISSING",
) -> dict[str, Any]:
    """Return a state copy with candidate data in its terminal release record."""

    result = _state_copy(state)
    releases = result["releases"]
    current = copy.deepcopy(dict(releases.get(candidate.key, {})))
    current.setdefault("status", terminal_status.upper())
    current.setdefault("terminal", True)
    current["candidate"] = candidate.to_mapping()
    processing = current.setdefault("processing", {"status": "PENDING_REVIEW"})
    if not isinstance(processing, dict):
        raise ValueError("release processing data must be a mapping")
    releases[candidate.key] = current
    return result


def load_candidates_from_state(
    state: Mapping[str, Any],
    *,
    statuses: Iterable[str] = ("MISSING", "NOT_FOUND"),
    include_finished: bool = False,
) -> tuple[Candidate, ...]:
    """Load candidates from the caller-owned state without mutating it."""

    wanted = {str(item).upper() for item in statuses}
    releases = state.get("releases", {})
    if not isinstance(releases, Mapping):
        raise ValueError("state['releases'] must be a mapping")
    result: list[Candidate] = []
    for key, record in releases.items():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("status", "")).upper() not in wanted:
            continue
        processing = record.get("processing")
        processing_status = (
            str(processing.get("status", ""))
            if isinstance(processing, Mapping)
            else ""
        ).upper()
        if not include_finished and processing_status in {
            ProcessingStatus.READY.value,
            ProcessingStatus.SKIPPED.value,
            ProcessingStatus.STALE.value,
        }:
            continue
        candidate_data = record.get("candidate")
        if isinstance(candidate_data, Mapping):
            result.append(Candidate.from_mapping(str(key), candidate_data))
    return tuple(sorted(result, key=lambda item: (item.name.casefold(), item.key)))


def apply_review_decisions(
    state: Mapping[str, Any], batch: ReviewBatch
) -> dict[str, Any]:
    """Purely attach a completed first-phase review to release records."""

    result = _state_copy(state)
    if batch.cancelled:
        return result
    for key, decision in batch.decisions:
        try:
            record = result["releases"][key]
        except KeyError as error:
            raise KeyError(f"Unknown reviewed release key: {key}") from error
        processing = record.setdefault("processing", {})
        if not isinstance(processing, dict):
            raise ValueError("release processing data must be a mapping")
        processing["decision"] = decision.value
        processing["status"] = "REVIEWED"
    return result


def apply_processing_result(
    state: Mapping[str, Any], result_record: ProcessingResult
) -> dict[str, Any]:
    """Purely merge one transaction result without replacing candidate data."""

    result = _state_copy(state)
    try:
        record = result["releases"][result_record.key]
    except KeyError as error:
        raise KeyError(f"Unknown processed release key: {result_record.key}") from error
    processing = record.setdefault("processing", {})
    if not isinstance(processing, dict):
        raise ValueError("release processing data must be a mapping")
    decision = processing.get("decision")
    processing.clear()
    if decision is not None:
        processing["decision"] = decision
    processing.update(result_record.to_mapping())
    return result


def ready_entries_from_state(state: Mapping[str, Any]) -> tuple[tuple[str, Path], ...]:
    releases = state.get("releases", {})
    if not isinstance(releases, Mapping):
        return ()
    entries: list[tuple[str, Path]] = []
    for key, record in releases.items():
        if not isinstance(record, Mapping):
            continue
        processing = record.get("processing")
        if not isinstance(processing, Mapping):
            continue
        if str(processing.get("status", "")).upper() != ProcessingStatus.READY.value:
            continue
        raw_paths: list[Any] = []
        if processing.get("ready_path"):
            raw_paths.append(processing["ready_path"])
        configured_paths = processing.get("ready_paths")
        if isinstance(configured_paths, Sequence) and not isinstance(
            configured_paths, (str, bytes)
        ):
            raw_paths.extend(configured_paths)
        # The checker's plain candidates are READY immediately and use the
        # plural field.  Fall back to their content path for older READY state.
        if not raw_paths:
            candidate = record.get("candidate")
            if isinstance(candidate, Mapping) and candidate.get("content_path"):
                raw_paths.append(candidate["content_path"])
        for raw_path in raw_paths:
            if raw_path:
                entries.append((str(key), Path(str(raw_path))))
    unique = {(key, str(path)): (key, path) for key, path in entries}
    return tuple(
        sorted(unique.values(), key=lambda item: (str(item[1]).casefold(), item[0]))
    )


def render_ready_txt(state: Mapping[str, Any]) -> str:
    """Render only committed payload paths; this text is derived output."""

    lines = [str(path) for _, path in ready_entries_from_state(state)]
    return "" if not lines else "\n".join(lines) + "\n"


def write_ready_txt(path: Path | str, state: Mapping[str, Any]) -> None:
    """Atomically publish the derived ready list."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(render_ready_txt(state))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def collect_decisions(
    candidates: Sequence[Candidate],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> ReviewBatch:
    """Collect every decision before any transaction is allowed to start.

    ``q`` cancels the whole batch.  This is deliberately not a prompt-and-act
    loop: callers must first receive a non-cancelled batch and only then execute
    it with :func:`execute_review_batch`.
    """

    decisions: list[tuple[str, Decision]] = []
    total = len(candidates)
    aliases = {
        "i": Decision.YES,
        "igen": Decision.YES,
        "y": Decision.YES,
        "yes": Decision.YES,
        "n": Decision.NO,
        "nem": Decision.NO,
        "no": Decision.NO,
        "k": Decision.SEPARATE,
        "kulon": Decision.SEPARATE,
        "külön": Decision.SEPARATE,
        "separate": Decision.SEPARATE,
    }
    for index, candidate in enumerate(candidates, 1):
        output_fn(f"[{index}/{total}] {candidate.name}")
        output_fn(f"Forrás: {candidate.content_path}")
        while True:
            try:
                answer = input_fn("Művelet [i]gen/[n]em/[k]ülön/[q]kilépés: ")
            except (EOFError, KeyboardInterrupt):
                return ReviewBatch(tuple(decisions), cancelled=True)
            normalized = str(answer).strip().casefold()
            if normalized in {"q", "quit", "kilepes", "kilépés"}:
                return ReviewBatch(tuple(decisions), cancelled=True)
            decision = aliases.get(normalized)
            if decision is not None:
                decisions.append((candidate.key, decision))
                break
            output_fn("Érvénytelen válasz; válassz: i, n, k vagy q.")
    return ReviewBatch(tuple(decisions), cancelled=False)


def confirm_review_batch(
    candidates: Sequence[Candidate],
    batch: ReviewBatch,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> bool:
    """Show the complete plan and require one final explicit confirmation."""

    if batch.cancelled:
        return False
    candidate_by_key = {candidate.key: candidate for candidate in candidates}
    output_fn("=== Feldolgozási összesítő ===")
    for key, decision in batch.decisions:
        candidate = candidate_by_key.get(key)
        name = candidate.name if candidate is not None else key
        output_fn(f"{decision.value:>8}  {name}")
    try:
        answer = input_fn("A teljes terv végrehajtása? [igen/nem]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return str(answer).strip().casefold() in {"i", "igen", "y", "yes"}


def safe_relative_path(raw: str, label: str = "path") -> Path:
    """Convert an untrusted qB/archive path to a safe platform Path."""

    value = str(raw)
    if not value or "\x00" in value or any(ord(char) < 32 for char in value):
        raise UnsafePathError(f"Unsafe {label}: {raw!r}")
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or not posix.parts
        or ".." in posix.parts
    ):
        raise UnsafePathError(f"Unsafe {label}: {raw!r}")
    return Path(*posix.parts)


_PART_RAR_RE = re.compile(r"^(?P<prefix>.+)\.part(?P<number>\d+)\.rar$", re.I)
_OLD_VOLUME_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<letter>[r-z])(?P<number>\d{2,3})$", re.I
)
_RARISH_RE = re.compile(r"(?:\.rar|\.part\d+\.rar|\.[r-z]\d{2,3})$", re.I)


def detect_media(file_names: Iterable[str]) -> MediaDetection:
    """Recognize complete RAR sets and ISO images from qB file names."""

    paths = tuple(dict.fromkeys(safe_relative_path(name, "qB file path") for name in file_names))
    by_parent: dict[Path, list[Path]] = {}
    for path in paths:
        if _RARISH_RE.search(path.name):
            by_parent.setdefault(path.parent, []).append(path)

    sets: list[RarSet] = []
    claimed: set[Path] = set()
    for parent, rar_paths in by_parent.items():
        modern: dict[str, list[tuple[int, Path]]] = {}
        normal: dict[str, list[Path]] = {}
        old: dict[str, list[Path]] = {}
        for path in rar_paths:
            part = _PART_RAR_RE.match(path.name)
            if part:
                modern.setdefault(part.group("prefix").casefold(), []).append(
                    (int(part.group("number")), path)
                )
                continue
            continuation = _OLD_VOLUME_RE.match(path.name)
            if continuation:
                old.setdefault(continuation.group("stem").casefold(), []).append(path)
                continue
            if path.suffix.casefold() == ".rar":
                normal.setdefault(path.stem.casefold(), []).append(path)

        for numbered in modern.values():
            numbers = [number for number, _ in numbered]
            if len(numbers) != len(set(numbers)) or min(numbers, default=0) != 1:
                raise ArchiveError("Ambiguous or missing first multipart RAR volume")
            if sorted(numbers) != list(range(1, max(numbers) + 1)):
                raise ArchiveError("Multipart RAR volumes are not contiguous")
            ordered = tuple(path for _, path in sorted(numbered))
            first = next(path for number, path in numbered if number == 1)
            sets.append(RarSet(first, ordered))
            claimed.update(ordered)

        for stem, firsts in normal.items():
            if len(firsts) != 1:
                raise ArchiveError("Ambiguous first RAR volume")
            continuations = sorted(old.get(stem, ()), key=_old_volume_sort_key)
            volumes = (firsts[0], *continuations)
            sets.append(RarSet(firsts[0], volumes))
            claimed.update(volumes)

    all_rarish = {path for path in paths if _RARISH_RE.search(path.name)}
    orphaned = all_rarish.difference(claimed)
    if orphaned:
        raise ArchiveError(
            "Orphaned or unsupported RAR volume: "
            + ", ".join(str(path) for path in sorted(orphaned))
        )

    iso_files = tuple(
        sorted(path for path in paths if path.suffix.casefold() == ".iso")
    )
    wrapped = all_rarish.union(iso_files)
    direct = tuple(path for path in paths if path not in wrapped)
    return MediaDetection(
        rar_sets=tuple(sorted(sets, key=lambda item: str(item.first_volume).casefold())),
        iso_files=iso_files,
        direct_files=direct,
    )


def _old_volume_sort_key(path: Path) -> tuple[int, int, str]:
    match = _OLD_VOLUME_RE.match(path.name)
    if not match:
        return (99, 0, path.name.casefold())
    letter = ord(match.group("letter").casefold()) - ord("r")
    return (letter, int(match.group("number")), path.name.casefold())


class _CommandExtractor:
    def __init__(self, executable: str | Path, timeout: int = 24 * 60 * 60):
        self.executable = str(executable)
        self.timeout = timeout

    def _run(self, arguments: Sequence[str]) -> str:
        environment = dict(os.environ)
        environment["LC_ALL"] = "C"
        environment["LANG"] = "C"
        completed = subprocess.run(
            [self.executable, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
            env=environment,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-20:])
            raise ArchiveError(
                f"Extractor failed (exit={completed.returncode}):\n{tail}"
            )
        return completed.stdout


class UnrarExtractor(_CommandExtractor):
    """CRC-test, inspect, and extract a RAR set with upstream unrar."""

    def inspect(self, source: Path) -> tuple[ArchiveMember, ...]:
        self._run(["t", "-y", "-c-", "-p-", str(source)])
        output = self._run(["lt", "-c-", "-p-", str(source)])
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            match = re.match(r"^\s*([^:]+):\s?(.*)$", line)
            if not match:
                continue
            key, value = match.group(1).strip(), match.group(2)
            if key == "Name" and current:
                records.append(current)
                current = {}
            if key in {"Name", "Type", "Size", "Target"}:
                current[key] = value
        if current:
            records.append(current)
        members: list[ArchiveMember] = []
        for record in records:
            if "Name" not in record or "Type" not in record:
                continue
            entry_type = record["Type"].casefold()
            if "link" in entry_type or "Target" in record:
                raise ArchiveError("RAR contains a link")
            try:
                size = int(record.get("Size", "0"))
            except ValueError as error:
                raise ArchiveError("Invalid size in RAR member list") from error
            members.append(
                ArchiveMember(
                    safe_relative_path(record["Name"], "RAR member"),
                    entry_type == "directory",
                    size,
                )
            )
        return _validate_member_manifest(members, source)

    def extract(self, source: Path, destination: Path) -> None:
        self._run(
            ["x", "-y", "-o+", "-c-", "-p-", str(source), f"{destination}{os.sep}"]
        )


class SevenZipExtractor(_CommandExtractor):
    """Rootless ISO/UDF reader and extractor."""

    def inspect(self, source: Path) -> tuple[ArchiveMember, ...]:
        self._run(["t", "-y", "-bb0", "--", str(source)])
        output = self._run(["l", "-slt", "--", str(source)])
        after_separator = False
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if line.startswith("----------"):
                after_separator = True
                current = {}
                continue
            if not after_separator:
                continue
            if not line.strip():
                if current:
                    records.append(current)
                    current = {}
                continue
            if " = " in line:
                key, value = line.split(" = ", 1)
                current[key] = value
        if current:
            records.append(current)

        members: list[ArchiveMember] = []
        for record in records:
            raw_path = record.get("Path")
            if not raw_path:
                continue
            attributes = record.get("Attributes", "")
            if (
                any("symbolic link" in key.casefold() for key in record)
                or record.get("Symbolic Link")
                or attributes.startswith("L")
            ):
                raise ArchiveError("ISO contains a symbolic link")
            is_directory = attributes.startswith("D") or record.get("Folder") == "+"
            try:
                size = int(record.get("Size") or "0")
            except ValueError as error:
                raise ArchiveError("Invalid size in ISO member list") from error
            members.append(
                ArchiveMember(
                    safe_relative_path(raw_path, "ISO member"), is_directory, size
                )
            )
        return _validate_member_manifest(members, source)

    def extract(self, source: Path, destination: Path) -> None:
        self._run(
            ["x", "-y", "-aoa", "-bb0", f"-o{destination}", "--", str(source)]
        )


def _validate_member_manifest(
    members: Sequence[ArchiveMember], source: Path
) -> tuple[ArchiveMember, ...]:
    if not members:
        raise ArchiveError(f"Archive has no listable payload: {source}")
    kinds: dict[Path, bool] = {}
    casefolded: dict[str, Path] = {}
    for member in members:
        previous_kind = kinds.get(member.relative_path)
        if previous_kind is not None and previous_kind != member.is_directory:
            raise ArchiveError("Archive path is both a file and a directory")
        kinds[member.relative_path] = member.is_directory
        folded = str(member.relative_path).casefold()
        previous = casefolded.get(folded)
        if previous is not None and previous != member.relative_path:
            raise ArchiveError("Archive contains case-colliding paths")
        casefolded[folded] = member.relative_path
    files = {path for path, is_directory in kinds.items() if not is_directory}
    for file_path in files:
        if any(parent in files for parent in file_path.parents):
            raise ArchiveError("Archive contains nested file/directory conflicts")
    return tuple(members)


class QbitApi:
    """Small adapter for the checker's authenticated requests-like session.

    The deletion method intentionally exposes no ``delete_files`` argument.
    Its only possible request payload is the list-only invariant.
    """

    def __init__(self, session: Any, base_url: str, timeout: int = 30):
        self.session = session
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout

    def _post(self, endpoint: str, hashes: Sequence[str], **extra: str) -> None:
        response = self.session.post(
            f"{self.base_url}/api/v2/{endpoint}",
            data={"hashes": "|".join(hashes), **extra},
            timeout=self.timeout,
        )
        if getattr(response, "status_code", None) != 200:
            raise TransactionError(
                f"qBittorrent {endpoint} failed: HTTP "
                f"{getattr(response, 'status_code', 'unknown')}"
            )

    def stop(self, hashes: Sequence[str]) -> None:
        self._post("torrents/stop", hashes)

    def start(self, hashes: Sequence[str]) -> None:
        self._post("torrents/start", hashes)

    def remove_list_only(self, hashes: Sequence[str]) -> None:
        self._post("torrents/delete", hashes, deleteFiles="false")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_chain(path: Path, root: Path) -> None:
    path_abs = _absolute_without_resolving(path)
    root_abs = _absolute_without_resolving(root)
    if not _is_within(path_abs, root_abs):
        raise UnsafePathError(f"Path escapes source root: {path}")
    current = root_abs
    if current.is_symlink():
        raise UnsafePathError(f"Source root is a symlink: {root}")
    for part in path_abs.relative_to(root_abs).parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise UnsafePathError(f"Symlink in path chain: {current}")


def _existing_regular_file(path: Path, root: Path) -> Path:
    _assert_no_symlink_chain(path, root)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UnsafePathError(f"qB file is missing: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafePathError(f"qB path is not a regular file: {path}")
    resolved = path.resolve(strict=True)
    if not _is_within(resolved, root):
        raise UnsafePathError(f"qB file escapes source root: {path}")
    return resolved


def _validate_directory_tree(root: Path) -> None:
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise UnsafePathError(f"Release root is not a real directory: {root}")
    device = metadata.st_dev
    for current_name, directories, files in os.walk(root, followlinks=False):
        current = Path(current_name)
        for name in [*directories, *files]:
            path = current / name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode):
                raise UnsafePathError(f"Symlink in release tree: {path}")
            if item.st_dev != device:
                raise UnsafePathError(f"Nested filesystem in release tree: {path}")
            if name in directories and not stat.S_ISDIR(item.st_mode):
                raise UnsafePathError(f"Special directory entry: {path}")
            if name in files and not stat.S_ISREG(item.st_mode):
                raise UnsafePathError(f"Special file in release tree: {path}")


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, int, int], ...]:
    if root.is_file():
        item = root.lstat()
        return ((root.name, item.st_size, item.st_mtime_ns, item.st_ino),)
    snapshot: list[tuple[str, int, int, int]] = []
    for current_name, directories, files in os.walk(root, followlinks=False):
        current = Path(current_name)
        for name in sorted([*directories, *files]):
            path = current / name
            item = path.lstat()
            snapshot.append(
                (
                    str(path.relative_to(root)),
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_ino,
                )
            )
    return tuple(snapshot)


def _validate_extracted_tree(
    root: Path, members: Sequence[ArchiveMember]
) -> None:
    expected_files = {
        member.relative_path: member.size
        for member in members
        if not member.is_directory
    }
    expected_directories = {
        member.relative_path for member in members if member.is_directory
    }
    for path in [*expected_files, *expected_directories]:
        expected_directories.update(path.parents)
    expected_directories.discard(Path("."))

    actual_files: dict[Path, int] = {}
    actual_directories: set[Path] = set()
    for current_name, directories, files in os.walk(root, followlinks=False):
        current = Path(current_name)
        for name in directories:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ArchiveError(f"Unsafe extracted directory: {path}")
            actual_directories.add(path.relative_to(root))
        for name in files:
            path = current / name
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise ArchiveError(f"Unsafe extracted file: {path}")
            actual_files[path.relative_to(root)] = metadata.st_size

    if actual_files != expected_files:
        raise ArchiveError("Extracted files or sizes differ from archive manifest")
    if not actual_directories.issubset(expected_directories):
        raise ArchiveError("Extractor produced an unexpected directory")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files_identical(first: Path, second: Path) -> bool:
    return first.stat().st_size == second.stat().st_size and _sha256(first) == _sha256(second)


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise UnsafePathError(f"Symlink destination: {destination}")
    for current_name, directories, files in os.walk(source, followlinks=False):
        current = Path(current_name)
        relative = current.relative_to(source)
        target_directory = destination / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        if target_directory.is_symlink():
            raise UnsafePathError(f"Symlink destination: {target_directory}")
        for name in directories:
            item = current / name
            if item.is_symlink() or not item.is_dir():
                raise ArchiveError(f"Unsafe staged directory: {item}")
            target = target_directory / name
            if target.exists() and (target.is_symlink() or not target.is_dir()):
                raise ArchiveError(f"File/directory collision: {target}")
            target.mkdir(exist_ok=True)
        for name in files:
            item = current / name
            metadata = item.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise ArchiveError(f"Unsafe staged file: {item}")
            target = target_directory / name
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise ArchiveError(f"File collision: {target}")
                if not _files_identical(item, target):
                    raise ArchiveError(f"Different payloads collide at: {target}")
                continue
            with item.open("rb") as source_handle, target.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, 8 * 1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.chmod(target, 0o644)


def _trees_identical(first: Path, second: Path) -> bool:
    def files(root: Path) -> dict[Path, Path]:
        result: dict[Path, Path] = {}
        for current_name, directories, file_names in os.walk(root, followlinks=False):
            current = Path(current_name)
            if any((current / name).is_symlink() for name in [*directories, *file_names]):
                return {}
            for name in file_names:
                path = current / name
                if not path.is_file():
                    return {}
                result[path.relative_to(root)] = path
        return result

    left, right = files(first), files(second)
    return bool(left) and left.keys() == right.keys() and all(
        _files_identical(path, right[relative]) for relative, path in left.items()
    )


class MediaPreparer:
    """Prepare one reviewed candidate using recoverable filesystem swaps."""

    def __init__(
        self,
        *,
        source_roots: Sequence[Path | str],
        up_root: Path | str,
        qbit: QbitClientInterface,
        owner_validator: OwnerValidator,
        rar_extractor: Extractor | None = None,
        iso_extractor: Extractor | None = None,
        min_free_bytes: int = 1024**3,
        max_nested_isos: int = 64,
    ) -> None:
        if not source_roots:
            raise ValueError("At least one source root is required")
        roots: list[Path] = []
        for raw_root in source_roots:
            root = Path(raw_root).resolve(strict=True)
            if not root.is_dir() or root.is_symlink():
                raise UnsafePathError(f"Invalid source root: {raw_root}")
            roots.append(root)
        self.source_roots = tuple(roots)
        self.up_root = _absolute_without_resolving(Path(up_root))
        self.qbit = qbit
        self.owner_validator = owner_validator
        self.rar_extractor = rar_extractor
        self.iso_extractor = iso_extractor
        self.min_free_bytes = int(min_free_bytes)
        self.max_nested_isos = int(max_nested_isos)
        if self.min_free_bytes < 0 or self.max_nested_isos < 1:
            raise ValueError("Invalid safety limit")
        if not any(_is_within(self.up_root, root) for root in self.source_roots):
            raise UnsafePathError("up_root must be beneath an allowed source root")

    def prepare(self, candidate: Candidate, decision: Decision) -> ProcessingResult:
        if decision is Decision.NO:
            return ProcessingResult(
                candidate.key, ProcessingStatus.SKIPPED, Decision.NO.value
            )
        layout = self._resolve_layout(candidate)
        detection = detect_media(candidate.file_names)
        if decision is Decision.SEPARATE:
            ready = self._prepare_separate(candidate, layout, detection)
        elif decision is Decision.YES:
            ready = self._prepare_in_place(candidate, layout, detection)
        else:  # pragma: no cover - Enum prevents this through the public API
            raise ValueError(f"Unsupported decision: {decision}")
        return ProcessingResult(
            candidate.key,
            ProcessingStatus.READY,
            decision.value,
            ready_path=ready,
        )

    def _source_root_for(self, path: Path) -> Path:
        absolute = _absolute_without_resolving(path)
        matches = [root for root in self.source_roots if _is_within(absolute, root)]
        if not matches:
            raise UnsafePathError(f"Path is outside allowed source roots: {path}")
        return max(matches, key=lambda item: len(item.parts))

    def _resolve_layout(self, candidate: Candidate) -> SourceLayout:
        save_path_abs = _absolute_without_resolving(candidate.save_path)
        source_root = self._source_root_for(save_path_abs)
        _assert_no_symlink_chain(save_path_abs, source_root)
        save_path = save_path_abs.resolve(strict=True)
        if not save_path.is_dir():
            raise UnsafePathError("qB save_path is not a directory")
        if _is_within(save_path, self.up_root):
            raise UnsafePathError("A release inside up_root cannot be processed")

        owned: list[tuple[Path, Path]] = []
        for raw_name in candidate.file_names:
            relative = safe_relative_path(raw_name, "qB file path")
            absolute = _existing_regular_file(save_path / relative, source_root)
            if _is_within(absolute, self.up_root):
                raise UnsafePathError("A qB file inside up_root cannot be processed")
            owned.append((relative, absolute))

        content_abs = _absolute_without_resolving(candidate.content_path)
        release_root: Path | None = None
        if os.path.lexists(content_abs) and content_abs.is_dir() and not content_abs.is_symlink():
            possible = content_abs.resolve(strict=True)
            if possible != save_path and all(_is_within(path, possible) for _, path in owned):
                release_root = possible
        if release_root is None:
            first_parts = {relative.parts[0] for relative, _ in owned}
            if len(first_parts) == 1:
                possible = save_path / next(iter(first_parts))
                if possible.is_dir() and not possible.is_symlink():
                    possible = possible.resolve(strict=True)
                    if all(_is_within(path, possible) for _, path in owned):
                        release_root = possible

        if release_root is not None:
            if release_root == source_root or release_root == save_path:
                raise UnsafePathError("Refusing to treat a broad root as a release")
            if _is_within(release_root, self.up_root):
                raise UnsafePathError("Release root overlaps up_root")
            _assert_no_symlink_chain(release_root, source_root)
            _validate_directory_tree(release_root)
            relative_root = release_root.relative_to(save_path)
            return SourceLayout(
                source_root,
                save_path,
                release_root,
                relative_root,
                tuple(owned),
                False,
            )

        if any(relative.parent != Path(".") for relative, _ in owned):
            raise UnsafePathError("qB files span multiple release roots")
        return SourceLayout(
            source_root,
            save_path,
            None,
            Path(_flat_container_name(candidate, detect_media(candidate.file_names))),
            tuple(owned),
            True,
        )

    def _validate_owners(
        self,
        candidate: Candidate,
        layout: SourceLayout,
        *,
        destructive: bool = True,
    ) -> None:
        affected_paths: tuple[Path, ...]
        if destructive and layout.release_root is not None:
            # In-place mode swaps the whole directory, not only candidate-owned
            # archive volumes.  Every live qB path physically beneath that root
            # must therefore participate in the exact-owner comparison.
            affected: list[Path] = []
            for current_name, directories, files in os.walk(
                layout.release_root, followlinks=False
            ):
                current = Path(current_name)
                for name in directories:
                    path = current / name
                    if path.is_symlink() or not path.is_dir():
                        raise UnsafePathError(f"Unsafe affected directory: {path}")
                for name in files:
                    path = current / name
                    item = path.lstat()
                    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                        raise UnsafePathError(f"Unsafe affected file: {path}")
                    affected.append(path.resolve(strict=True))
            affected_paths = tuple(affected)
        else:
            affected_paths = tuple(path for _, path in layout.owned_files)

        destructive_reader = getattr(
            self.owner_validator, "destructive_owners", None
        )
        if destructive and callable(destructive_reader):
            owner_values = destructive_reader(candidate, affected_paths)
        else:
            owner_values = self.owner_validator(candidate, affected_paths)
        exact = {str(item) for item in owner_values}
        expected = set(candidate.hashes)
        ownership_is_safe = exact == expected if destructive else expected.issubset(exact)
        if not ownership_is_safe:
            raise OwnershipError(
                f"Exact owners changed: expected={sorted(expected)}, actual={sorted(exact)}"
            )

    def _prepare_payload(
        self,
        candidate: Candidate,
        layout: SourceLayout,
        detection: MediaDetection,
        stage: Path,
    ) -> None:
        stage.mkdir(mode=0o700)
        mapping = {relative: absolute for relative, absolute in layout.owned_files}
        if not detection.wrapped:
            for relative, source in layout.owned_files:
                release_relative = layout.relative_to_release(relative)
                target = stage / release_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as input_handle, target.open("xb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, 8 * 1024 * 1024)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                os.chmod(target, 0o644)
            return

        if detection.rar_sets and self.rar_extractor is None:
            raise ArchiveError("RAR payload found but no RAR extractor is configured")
        if detection.iso_files and self.iso_extractor is None:
            raise ArchiveError("ISO payload found but no ISO extractor is configured")

        for archive_set in detection.rar_sets:
            source = mapping.get(archive_set.first_volume)
            if source is None:
                raise ArchiveError("RAR first volume is not owned by the candidate")
            self._extract_and_merge(
                self.rar_extractor,
                source,
                stage / layout.relative_to_release(archive_set.first_volume).parent,
                stage.parent,
            )

        for iso_relative in detection.iso_files:
            source = mapping.get(iso_relative)
            if source is None:
                raise ArchiveError("ISO is not owned by the candidate")
            self._extract_and_merge(
                self.iso_extractor,
                source,
                stage / layout.relative_to_release(iso_relative).parent,
                stage.parent,
            )

        self._expand_nested_isos(stage)
        leftovers = [
            path
            for path in stage.rglob("*")
            if path.is_file()
            and (_RARISH_RE.search(path.name) or path.suffix.casefold() == ".iso")
        ]
        if leftovers:
            raise ArchiveError(f"Wrapped media remains in payload: {leftovers[0]}")
        _validate_payload_tree(stage)

    def _extract_and_merge(
        self,
        extractor: Extractor | None,
        source: Path,
        destination: Path,
        temporary_parent: Path,
    ) -> None:
        if extractor is None:
            raise ArchiveError("Required extractor is not configured")
        members = extractor.inspect(source)
        expected = sum(member.size for member in members if not member.is_directory)
        free = shutil.disk_usage(temporary_parent).free
        if free - expected < self.min_free_bytes:
            raise ArchiveError(
                f"Insufficient free space: expected={expected}, free={free}, "
                f"reserve={self.min_free_bytes}"
            )
        with tempfile.TemporaryDirectory(
            prefix=".aither-extract-", dir=temporary_parent
        ) as temporary_name:
            extracted = Path(temporary_name) / "payload"
            extracted.mkdir(mode=0o700)
            extractor.extract(source, extracted)
            _validate_extracted_tree(extracted, members)
            _merge_tree(extracted, destination)

    def _expand_nested_isos(self, stage: Path) -> None:
        count = 0
        while True:
            images = sorted(
                path
                for path in stage.rglob("*")
                if path.is_file() and path.suffix.casefold() == ".iso"
            )
            if not images:
                return
            if self.iso_extractor is None:
                raise ArchiveError("RAR produced an ISO but no ISO extractor is configured")
            for image in images:
                count += 1
                if count > self.max_nested_isos:
                    raise ArchiveError("Too many nested ISO images")
                self._extract_and_merge(
                    self.iso_extractor, image, image.parent, stage.parent
                )
                image.unlink()

    def _ensure_up_root(self) -> None:
        parent_root = self._source_root_for(self.up_root)
        _assert_no_symlink_chain(self.up_root.parent, parent_root)
        self.up_root.mkdir(mode=0o750, exist_ok=True)
        _assert_no_symlink_chain(self.up_root, parent_root)
        if not self.up_root.is_dir() or self.up_root.is_symlink():
            raise UnsafePathError("up_root is not a real directory")

    def _prepare_separate(
        self, candidate: Candidate, layout: SourceLayout, detection: MediaDetection
    ) -> Path:
        # Additional overlapping owners do not make a read/copy unsafe.  Every
        # selected hash must still own the input, but separate mode never stops
        # or removes any owner and never mutates the original path.
        self._validate_owners(candidate, layout, destructive=False)
        source_for_snapshot = layout.release_root
        before = (
            _tree_snapshot(source_for_snapshot)
            if source_for_snapshot is not None
            else tuple(
                (str(relative), *_file_snapshot(path))
                for relative, path in layout.owned_files
            )
        )
        self._ensure_up_root()
        destination = self.up_root / layout.relative_root
        _assert_no_symlink_chain(destination.parent, self.up_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_chain(destination.parent, self.up_root)
        stage = destination.parent / f".{destination.name}.aither-stage-{uuid.uuid4().hex}"
        try:
            self._prepare_payload(candidate, layout, detection, stage)
            self._validate_owners(candidate, layout, destructive=False)
            after = (
                _tree_snapshot(source_for_snapshot)
                if source_for_snapshot is not None
                else tuple(
                    (str(relative), *_file_snapshot(path))
                    for relative, path in layout.owned_files
                )
            )
            if before != after:
                raise TransactionError("Source release changed while preparing separate output")
            if destination.exists():
                if destination.is_dir() and _trees_identical(stage, destination):
                    shutil.rmtree(stage)
                    return destination
                raise TransactionError(f"Separate destination already exists: {destination}")
            os.replace(stage, destination)
            return destination
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def _prepare_in_place(
        self, candidate: Candidate, layout: SourceLayout, detection: MediaDetection
    ) -> Path:
        self._validate_owners(candidate, layout)
        if layout.flat:
            return self._prepare_flat_in_place(candidate, layout, detection)
        assert layout.release_root is not None
        target = layout.release_root
        before = _tree_snapshot(target)
        transaction_id = uuid.uuid4().hex
        stage = target.parent / f".{target.name}.aither-stage-{transaction_id}"
        quarantine = target.parent / f".{target.name}.aither-quarantine-{transaction_id}"
        original_moved = False
        installed = False
        stopped = False
        removed = False
        try:
            self._prepare_payload(candidate, layout, detection, stage)
            if before != _tree_snapshot(target):
                raise TransactionError("Source release changed during extraction")
            self._validate_owners(candidate, layout)
            stopped = True
            self.qbit.stop(candidate.hashes)
            # Closing validation after qB stop narrows the remaining TOCTOU
            # window and catches owners added during the stop request itself.
            self._validate_owners(candidate, layout)
            if before != _tree_snapshot(target):
                raise TransactionError("Source release changed after qB stop")
            os.replace(target, quarantine)
            original_moved = True
            os.replace(stage, target)
            installed = True
            self.qbit.remove_list_only(candidate.hashes)
            removed = True
            _safe_delete_generated_tree(quarantine, target.parent, ".aither-quarantine-")
            return target
        except BaseException:
            if not removed:
                try:
                    if original_moved:
                        if installed and target.exists():
                            os.replace(target, stage)
                        if quarantine.exists():
                            os.replace(quarantine, target)
                    if stopped and candidate.running_hashes:
                        self.qbit.start(candidate.running_hashes)
                except Exception as rollback_error:
                    raise TransactionError(
                        f"Transaction rollback failed: {rollback_error}"
                    ) from rollback_error
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if quarantine.exists() and not removed:
                # A successful rollback leaves it empty/nonexistent.  Never
                # delete a quarantine after the qB commit boundary here.
                try:
                    if not any(quarantine.iterdir()):
                        quarantine.rmdir()
                except OSError:
                    pass

    def _prepare_flat_in_place(
        self, candidate: Candidate, layout: SourceLayout, detection: MediaDetection
    ) -> Path:
        target = layout.save_path / layout.relative_root
        if target.exists() or target.is_symlink():
            raise TransactionError(f"Flat payload target already exists: {target}")
        transaction_id = uuid.uuid4().hex
        stage = target.parent / f".{target.name}.aither-stage-{transaction_id}"
        quarantine = target.parent / f".{target.name}.aither-quarantine-{transaction_id}"
        before = tuple(
            (str(relative), *_file_snapshot(path))
            for relative, path in layout.owned_files
        )
        moved: list[tuple[Path, Path]] = []
        stopped = False
        installed = False
        removed = False
        try:
            self._prepare_payload(candidate, layout, detection, stage)
            current = tuple(
                (str(relative), *_file_snapshot(path))
                for relative, path in layout.owned_files
            )
            if current != before:
                raise TransactionError("Flat source release changed during extraction")
            self._validate_owners(candidate, layout)
            stopped = True
            self.qbit.stop(candidate.hashes)
            self._validate_owners(candidate, layout)
            quarantine.mkdir(mode=0o700)
            for relative, source in layout.owned_files:
                destination = quarantine / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                moved.append((source, destination))
            os.replace(stage, target)
            installed = True
            self.qbit.remove_list_only(candidate.hashes)
            removed = True
            _safe_delete_generated_tree(quarantine, target.parent, ".aither-quarantine-")
            return target
        except BaseException:
            if not removed:
                try:
                    if installed and target.exists():
                        os.replace(target, stage)
                    for original, quarantined in reversed(moved):
                        original.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(quarantined, original)
                    if stopped and candidate.running_hashes:
                        self.qbit.start(candidate.running_hashes)
                except Exception as rollback_error:
                    raise TransactionError(
                        f"Flat transaction rollback failed: {rollback_error}"
                    ) from rollback_error
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if quarantine.exists() and not removed:
                shutil.rmtree(quarantine)


def _flat_container_name(candidate: Candidate, detection: MediaDetection) -> str:
    first: Path
    if detection.rar_sets:
        first = detection.rar_sets[0].first_volume
        match = _PART_RAR_RE.match(first.name)
        value = match.group("prefix") if match else first.stem
    elif detection.iso_files:
        value = detection.iso_files[0].stem
    else:
        value = Path(candidate.name).stem
    value = value.strip().strip(".")
    if not value or value in {".", ".."} or any(char in value for char in "/\\"):
        raise UnsafePathError("Cannot derive a safe flat release directory")
    return value


def _file_snapshot(path: Path) -> tuple[int, int, int]:
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode):
        raise UnsafePathError(f"Source is no longer a regular file: {path}")
    return item.st_size, item.st_mtime_ns, item.st_ino


def _validate_payload_tree(root: Path) -> None:
    file_count = 0
    for current_name, directories, files in os.walk(root, followlinks=False):
        current = Path(current_name)
        for name in directories:
            path = current / name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                raise ArchiveError(f"Unsafe payload directory: {path}")
        for name in files:
            file_count += 1
            path = current / name
            item = path.lstat()
            if (
                stat.S_ISLNK(item.st_mode)
                or not stat.S_ISREG(item.st_mode)
                or item.st_nlink != 1
            ):
                raise ArchiveError(f"Unsafe payload file: {path}")
    if file_count == 0:
        raise ArchiveError("Prepared payload is empty")


def _safe_delete_generated_tree(path: Path, parent: Path, marker: str) -> None:
    if path.parent != parent or marker not in path.name or path.is_symlink():
        raise TransactionError(f"Refusing unsafe quarantine deletion: {path}")
    if path.exists():
        shutil.rmtree(path)


def _persist_processing(
    state: Mapping[str, Any],
    key: str,
    *,
    save_callback: SaveState | None,
    state_store: ProcessingStateStore | None,
) -> None:
    if save_callback is not None and state_store is not None:
        raise ValueError("Use either save_callback or state_store, not both")
    processing = state["releases"][key].get("processing", {})
    if state_store is not None:
        state_store.update_processing(key, processing)
    elif save_callback is not None:
        save_callback(copy.deepcopy(dict(state)))


def execute_review_batch(
    state: Mapping[str, Any],
    candidates: Sequence[Candidate],
    batch: ReviewBatch,
    preparer: MediaPreparer,
    *,
    save_callback: SaveState | None = None,
    state_store: ProcessingStateStore | None = None,
) -> tuple[dict[str, Any], tuple[ProcessingResult, ...]]:
    """Execute phase two only after a complete, non-cancelled review.

    The returned state is the caller's next single source of truth.  When a
    ``StateStore`` is supplied only its existing ``processing`` field is
    updated; candidate/search records stay owned by that store.
    """

    if batch.cancelled:
        return _state_copy(state), ()
    candidate_by_key = {candidate.key: candidate for candidate in candidates}
    updated = apply_review_decisions(state, batch)
    for key, _ in batch.decisions:
        _persist_processing(
            updated,
            key,
            save_callback=save_callback,
            state_store=state_store,
        )

    results: list[ProcessingResult] = []
    for key, decision in batch.decisions:
        candidate = candidate_by_key.get(key)
        if candidate is None:
            result = ProcessingResult(
                key,
                ProcessingStatus.ERROR,
                decision.value,
                message="Reviewed candidate is unavailable",
            )
        elif decision is Decision.NO:
            result = ProcessingResult(
                key, ProcessingStatus.SKIPPED, decision.value
            )
        else:
            try:
                result = preparer.prepare(candidate, decision)
            except Exception as error:  # one release must not hide later results
                result = ProcessingResult(
                    key,
                    ProcessingStatus.ERROR,
                    decision.value,
                    message=f"{type(error).__name__}: {error}",
                )
        updated = apply_processing_result(updated, result)
        _persist_processing(
            updated,
            key,
            save_callback=save_callback,
            state_store=state_store,
        )
        results.append(result)
    return updated, tuple(results)


def _object_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _candidate_hash_values(value: Mapping[str, Any]) -> tuple[str, ...]:
    raw: Mapping[str, Any] = value
    nested = raw.get("candidate")
    if isinstance(nested, Mapping):
        raw = nested
    torrent = raw.get("torrent")
    if isinstance(torrent, Mapping):
        merged = dict(torrent)
        merged.update(raw)
        raw = merged
    hashes = raw.get("hashes")
    if isinstance(hashes, str):
        return (hashes,)
    if isinstance(hashes, Sequence):
        return tuple(str(item) for item in hashes if str(item))
    one_hash = raw.get("hash") or raw.get("torrent_hash")
    return (str(one_hash),) if one_hash else ()


def _qbit_file_names(qbit_client: Any, torrent_hash: str) -> tuple[str, ...]:
    raw_files = qbit_client.files(torrent_hash)
    result: list[str] = []
    for item in raw_files:
        if isinstance(item, Mapping):
            name = item.get("name")
        else:
            name = getattr(item, "name", item)
        if name is not None and str(name):
            result.append(str(name))
    return tuple(result)


def _all_qbit_torrents(qbit_client: Any) -> tuple[Any, ...]:
    try:
        values = qbit_client.torrents(filter_name="all", hashes=None)
    except TypeError:
        values = qbit_client.torrents()
    return tuple(values)


def _validated_source_roots(
    source_roots: Sequence[Path | str],
) -> tuple[Path, ...]:
    """Return existing, real configured roots without following root symlinks."""

    roots: list[Path] = []
    for raw_root in source_roots:
        root = _absolute_without_resolving(Path(raw_root))
        try:
            metadata = root.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            continue
        roots.append(root.resolve(strict=True))
    return tuple(dict.fromkeys(roots))


def _validate_plain_ready_path(raw_path: Any, roots: Sequence[Path]) -> Path:
    """Validate one live qB content path before publishing it as upload-ready."""

    if raw_path is None or not str(raw_path).strip():
        raise UnsafePathError("qBittorrent has no current content_path")
    path = _absolute_without_resolving(Path(str(raw_path)))
    matching_roots = [root for root in roots if _is_within(path, root)]
    if not matching_roots:
        raise UnsafePathError("qBittorrent content_path is outside source_roots")
    source_root = max(matching_roots, key=lambda item: len(item.parts))
    _assert_no_symlink_chain(path, source_root)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UnsafePathError("qBittorrent content_path no longer exists") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafePathError("qBittorrent content_path is a symlink")
    resolved = path.resolve(strict=True)
    if not _is_within(resolved, source_root):
        raise UnsafePathError("qBittorrent content_path escapes source_roots")
    if stat.S_ISDIR(metadata.st_mode):
        _validate_directory_tree(resolved)
    elif not stat.S_ISREG(metadata.st_mode):
        raise UnsafePathError("qBittorrent content_path is not a file or directory")
    return resolved


def _validate_plain_ready_records(
    state: Mapping[str, Any],
    qbit_client: Any,
    source_roots: Sequence[Path | str],
    *,
    state_store: ProcessingStateStore | None = None,
) -> dict[str, Any]:
    """Refresh or retire READY ``plain`` entries using current qB/filesystem state.

    Prepared ``yes``/``separate`` records are deliberately outside this check:
    their source torrent may have been removed from qB as part of a successful
    transaction.  A retired plain record keeps ``mode=plain``: normal media
    hydration ignores it, while this validator can restore it if the torrent
    and path later become valid again.
    """

    updated = _state_copy(state)
    releases = updated["releases"]
    roots = _validated_source_roots(source_roots)
    qbit_error = ""
    try:
        torrents = _all_qbit_torrents(qbit_client)
    except Exception as error:
        torrents = ()
        qbit_error = f"qBittorrent validation failed: {type(error).__name__}"
    live_by_hash = {
        str(_object_field(torrent, "hash")): torrent
        for torrent in torrents
        if _object_field(torrent, "hash")
    }

    for key, record in releases.items():
        if not isinstance(record, Mapping):
            continue
        processing = record.get("processing")
        if not isinstance(processing, Mapping):
            continue
        if (
            str(processing.get("status", "")).upper()
            not in {
                ProcessingStatus.READY.value,
                ProcessingStatus.STALE.value,
            }
            or str(processing.get("mode", "")).casefold() != "plain"
        ):
            continue

        candidate = record.get("candidate")
        reason = qbit_error
        current_path: Path | None = None
        if not reason and not isinstance(candidate, Mapping):
            reason = "Plain READY record has no candidate data"
        hashes = _candidate_hash_values(candidate) if isinstance(candidate, Mapping) else ()
        if not reason and not hashes:
            reason = "Plain READY record has no torrent hash"
        live = [live_by_hash[item] for item in dict.fromkeys(hashes) if item in live_by_hash]
        if not reason and len(live) != len(tuple(dict.fromkeys(hashes))):
            reason = "One or more torrents are no longer present in qBittorrent"
        raw_paths = {
            str(_object_field(item, "content_path", "")).strip()
            for item in live
        }
        if not reason and ("" in raw_paths or len(raw_paths) != 1):
            reason = "Live qBittorrent owners disagree on content_path"
        if not reason:
            try:
                current_path = _validate_plain_ready_path(next(iter(raw_paths)), roots)
            except (OSError, UnsafePathError) as error:
                reason = str(error)

        new_processing = copy.deepcopy(dict(processing))
        if reason:
            new_processing.update(
                {
                    "status": ProcessingStatus.STALE.value,
                    "mode": "plain",
                    "ready_path": "",
                    "ready_paths": [],
                    "message": reason,
                }
            )
        else:
            assert current_path is not None
            new_processing.update(
                {
                    "status": ProcessingStatus.READY.value,
                    "mode": "plain",
                    "ready_path": str(current_path),
                    "ready_paths": [],
                    "message": "",
                }
            )
        if new_processing == dict(processing):
            continue
        record["processing"] = new_processing
        # A transient qB API outage removes the entry only from this derived
        # TXT view.  Do not persist STALE in that case, so the next run can
        # validate and restore it automatically when qB is reachable again.
        if state_store is not None and not qbit_error:
            state_store.update_processing(str(key), new_processing)
    return updated


def _hydrate_candidates_from_qbit(
    state: Mapping[str, Any], qbit_client: Any, torrents: Sequence[Any]
) -> tuple[Candidate, ...]:
    """Build runtime candidates while leaving the caller's state untouched."""

    releases = state.get("releases", {})
    if not isinstance(releases, Mapping):
        raise ValueError("state['releases'] must be a mapping")
    live_by_hash = {
        str(_object_field(torrent, "hash")): torrent
        for torrent in torrents
        if _object_field(torrent, "hash")
    }
    completed_statuses = {"MISSING", "NOT_FOUND"}
    candidates: list[Candidate] = []
    for key, record in releases.items():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("status", "")).upper() not in completed_statuses:
            continue
        processing = record.get("processing")
        if isinstance(processing, Mapping):
            processing_status = str(processing.get("status", "")).upper()
            processing_mode = str(processing.get("mode", "")).casefold()
            if processing_status in {
                ProcessingStatus.READY.value,
                ProcessingStatus.SKIPPED.value,
                ProcessingStatus.STALE.value,
            } or processing_mode == "plain":
                continue
        candidate_record = record.get("candidate")
        if not isinstance(candidate_record, Mapping):
            continue
        runtime = copy.deepcopy(dict(candidate_record))
        hashes = _candidate_hash_values(runtime)
        if not hashes:
            raise ValueError(f"Candidate has no torrent hash: {key}")
        live = [live_by_hash[item] for item in hashes if item in live_by_hash]
        if len(live) != len(set(hashes)):
            raise OwnershipError(f"Candidate torrent is no longer in qBittorrent: {key}")
        primary = live[0]
        runtime.setdefault("key", str(key))
        runtime.setdefault("hashes", list(hashes))
        runtime.setdefault("name", str(_object_field(primary, "name", "")))
        runtime.setdefault("save_path", str(_object_field(primary, "save_path", "")))
        runtime.setdefault(
            "content_path",
            str(
                _object_field(primary, "content_path", None)
                or _object_field(primary, "save_path", "")
            ),
        )
        raw_files = runtime.get("file_names", runtime.get("files"))
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)) or not raw_files:
            file_names: list[str] = []
            for torrent_hash in hashes:
                file_names.extend(_qbit_file_names(qbit_client, torrent_hash))
            runtime["file_names"] = list(dict.fromkeys(file_names))
        stopped_states = {"stoppedup", "stoppeddl", "pausedup", "pauseddl"}
        # This must never come from durable candidate state: an operator may
        # have manually stopped/started the torrent since the search run.
        # Queued torrents retain an active intent and must be restarted after a
        # rollback, so queuedUP/queuedDL are intentionally not stopped states.
        runtime["running_hashes"] = [
            str(_object_field(item, "hash"))
            for item in live
            if str(_object_field(item, "state", "")).casefold()
            not in stopped_states
        ]
        candidates.append(Candidate.from_mapping(str(key), runtime))
    return tuple(sorted(candidates, key=lambda item: (item.name.casefold(), item.key)))


class _LiveOwnerValidator:
    """Return owners from a fresh qB torrent snapshot on every validation.

    Immutable torrent hashes may reuse their file manifest while their
    save/content/name fingerprint stays unchanged.  New, removed, renamed, or
    relocated entries are synchronized before each preflight/pre-commit call.
    """

    def __init__(self, qbit_client: Any, torrents: Sequence[Any]) -> None:
        self.qbit_client = qbit_client
        self.torrents = tuple(torrents)
        self._paths_by_hash: dict[str, frozenset[Path]] = {}
        self._fingerprints: dict[str, tuple[str, str, str]] = {}

    @staticmethod
    def _fingerprint(torrent: Any) -> tuple[str, str, str]:
        return (
            str(_object_field(torrent, "save_path", "")),
            str(_object_field(torrent, "content_path", "")),
            str(_object_field(torrent, "name", "")),
        )

    def refresh(
        self,
        *,
        related_paths: Iterable[Path] = (),
        force_hashes: Iterable[str] = (),
    ) -> dict[str, frozenset[Path]]:
        live_torrents = _all_qbit_torrents(self.qbit_client)
        related = tuple(
            _absolute_without_resolving(Path(path)) for path in related_paths
        )
        forced = {str(item) for item in force_hashes}
        result: dict[str, frozenset[Path]] = {}
        fingerprints: dict[str, tuple[str, str, str]] = {}
        for torrent in live_torrents:
            torrent_hash = str(_object_field(torrent, "hash", ""))
            raw_save_path = _object_field(torrent, "save_path")
            if not torrent_hash or not raw_save_path:
                continue
            fingerprint = self._fingerprint(torrent)
            fingerprints[torrent_hash] = fingerprint
            raw_content_path = _object_field(torrent, "content_path")
            content_path = _absolute_without_resolving(
                Path(str(raw_content_path or raw_save_path))
            )
            related_to_affected = any(
                content_path == path
                or content_path in path.parents
                or path in content_path.parents
                for path in related
            )
            if (
                torrent_hash not in forced
                and not related_to_affected
                and self._fingerprints.get(torrent_hash) == fingerprint
                and torrent_hash in self._paths_by_hash
            ):
                result[torrent_hash] = self._paths_by_hash[torrent_hash]
                continue
            save_path = _absolute_without_resolving(Path(str(raw_save_path)))
            mapped: set[Path] = set()
            for raw_name in _qbit_file_names(self.qbit_client, torrent_hash):
                relative = safe_relative_path(raw_name, "live qB file path")
                # Existing paths are resolved to catch shared files reached
                # through different lexical forms; missing paths remain lexical.
                candidate = save_path / relative
                try:
                    mapped.add(candidate.resolve(strict=True))
                except OSError:
                    mapped.add(_absolute_without_resolving(candidate))
            result[torrent_hash] = frozenset(mapped)
        self._paths_by_hash = result
        self._fingerprints = fingerprints
        self.torrents = live_torrents
        return result


    def _build(self) -> dict[str, frozenset[Path]]:
        """Compatibility alias for callers that want an eager fresh snapshot."""

        return self.refresh()

    def __call__(
        self, _candidate: Candidate, selected_paths: tuple[Path, ...]
    ) -> Iterable[str]:
        selected = {path.resolve(strict=True) for path in selected_paths}
        return tuple(
            torrent_hash
            for torrent_hash, paths in self.refresh(
                related_paths=selected,
            ).items()
            if selected.intersection(paths)
        )

    def destructive_owners(
        self, candidate: Candidate, affected_paths: tuple[Path, ...]
    ) -> Iterable[str]:
        """Validate whole-manifest containment before list removal.

        Merely intersecting an affected root is insufficient: removing a hash
        whose manifest also reaches outside that root would leave untracked
        orphan data behind.  Separate mode deliberately uses ``__call__`` and
        does not impose this destructive-only requirement.
        """

        affected = {path.resolve(strict=True) for path in affected_paths}
        manifests = self.refresh(
            related_paths=affected,
            force_hashes=candidate.hashes,
        )
        for torrent_hash in candidate.hashes:
            manifest = manifests.get(torrent_hash)
            if manifest is None:
                raise OwnershipError(
                    f"Expected qB owner disappeared: {torrent_hash}"
                )
            outside = manifest.difference(affected)
            if outside:
                raise OwnershipError(
                    f"qB owner manifest escapes affected release: {torrent_hash}"
                )
        return tuple(
            torrent_hash
            for torrent_hash, paths in manifests.items()
            if affected.intersection(paths)
        )


def review_and_process(
    store: Any,
    qbit_client: Any,
    source_roots: Sequence[Path | str],
    upload_root: Path | str,
    ready_list_file: Path | str,
    *,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    rar_extractor: Extractor | None = None,
    iso_extractor: Extractor | None = None,
    min_free_bytes: int = 1024**3,
) -> ReviewSummary:
    """High-level two-phase entry point for the checker.

    Expected caller interfaces are deliberately the already available ones:

    * ``store.snapshot()`` and ``store.update_processing(key, mapping)``;
    * ``qbit_client.torrents(filter_name='all', hashes=None)``;
    * ``qbit_client.files(hash)``, ``stop``, ``start``, and
      ``remove_list_only``.

    Candidate file lists are refreshed from qB at runtime when absent from the
    terminal state record.  Search status/candidate data remain exclusively in
    ``store``.  ``ready_list_file`` is only a regenerated TXT view.
    """

    state = store.snapshot()
    latest_state = state
    try:
        torrents = _all_qbit_torrents(qbit_client)
        candidates = _hydrate_candidates_from_qbit(state, qbit_client, torrents)
        if not candidates:
            output_func("Nincs feldolgozásra váró release.")
            return ReviewSummary(False, 0, 0, 0, 0, ())

        batch = collect_decisions(
            candidates, input_fn=input_func, output_fn=output_func
        )
        if batch.cancelled or not confirm_review_batch(
            candidates,
            batch,
            input_fn=input_func,
            output_fn=output_func,
        ):
            output_func("A review megszakadt; semmilyen release nem lett módosítva.")
            return ReviewSummary(True, len(batch.decisions), 0, 0, 0, ())

        if rar_extractor is None:
            executable = shutil.which("unrar")
            rar_extractor = UnrarExtractor(executable) if executable else None
        if iso_extractor is None:
            executable = shutil.which("7z") or shutil.which("7zz")
            iso_extractor = SevenZipExtractor(executable) if executable else None

        # Build the first complete live path-owner index after final
        # confirmation.  Each later preflight/pre-commit validation refreshes
        # torrents/info and synchronizes changed/new/removed manifests.
        owner_validator = _LiveOwnerValidator(qbit_client, torrents)
        owner_validator.refresh()
        preparer = MediaPreparer(
            source_roots=source_roots,
            up_root=upload_root,
            qbit=qbit_client,
            owner_validator=owner_validator,
            rar_extractor=rar_extractor,
            iso_extractor=iso_extractor,
            min_free_bytes=min_free_bytes,
        )
        updated, results = execute_review_batch(
            state,
            candidates,
            batch,
            preparer,
            state_store=store,
        )
        latest_state = updated
        ready = sum(result.status is ProcessingStatus.READY for result in results)
        skipped = sum(result.status is ProcessingStatus.SKIPPED for result in results)
        errors = sum(result.status is ProcessingStatus.ERROR for result in results)
        output_func(
            f"Feldolgozás kész: ready={ready}, kihagyva={skipped}, hiba={errors}."
        )
        return ReviewSummary(False, len(results), ready, skipped, errors, results)
    finally:
        # The TXT is a disposable view, so regenerate it even after cancellation
        # or an exception.  Prefer the durable store after any partial result,
        # then validate plain paths against the current qB/filesystem view.
        try:
            latest_state = store.snapshot()
        except Exception:
            pass
        latest_state = _validate_plain_ready_records(
            latest_state,
            qbit_client,
            source_roots,
            state_store=store,
        )
        write_ready_txt(ready_list_file, latest_state)


__all__ = [
    "ArchiveError",
    "ArchiveMember",
    "Candidate",
    "Decision",
    "MediaDetection",
    "MediaPrepareError",
    "MediaPreparer",
    "OwnershipError",
    "ProcessingResult",
    "ProcessingStatus",
    "QbitApi",
    "ReviewBatch",
    "ReviewSummary",
    "SevenZipExtractor",
    "TransactionError",
    "UnsafePathError",
    "UnrarExtractor",
    "apply_processing_result",
    "apply_review_decisions",
    "collect_decisions",
    "confirm_review_batch",
    "detect_media",
    "execute_review_batch",
    "load_candidates_from_state",
    "ready_entries_from_state",
    "render_ready_txt",
    "review_and_process",
    "safe_relative_path",
    "upsert_candidate_record",
    "write_ready_txt",
]
