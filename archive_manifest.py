"""Read-only archive manifests (RAR sets and ISO images) for the dupe checker.

A scene release that is still packed (``*.rar`` volumes or one ``*.iso``) hides
its real file list from qBittorrent: the torrent only lists the volumes.  The
matcher then compared the size of the packing, not of the content, and could
call a disc that Aither already has "not found".  This module asks 7-Zip for the
member list (``7z l -slt``, nothing is extracted or written) and substitutes it
for the volumes, so the normal manifest comparison sees the true content.

Everything is best effort and conservative: on any doubt (missing volume,
encrypted archive, huge or malformed listing, timeout, symlink, path escape) the
original qBittorrent file list is returned untouched.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

MAX_LISTING_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_ARCHIVES = 8

_PART_RAR_RE = re.compile(r"(?i)^(?P<stem>.+)\.part0*(?P<number>\d+)\.rar$")
_PLAIN_RAR_RE = re.compile(r"(?i)^(?P<stem>.+)\.rar$")
_OLD_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.[r-z]\d{2,3}$")
_SEPARATOR_RE = re.compile(r"^\s*-{5,}\s*$")


@dataclass(frozen=True)
class ListedEntry:
    path: str
    size: int


Lister = Callable[[Path], "tuple[ListedEntry, ...] | None"]


def _safe_member_path(raw: str) -> str | None:
    text = raw.replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part in {"", "..", "."} for part in pure.parts):
        return None
    return pure.as_posix()


def parse_slt_listing(text: str) -> tuple[ListedEntry, ...] | None:
    """Parse ``7z l -slt`` output into file members (directories are dropped).

    Returns ``None`` when the listing cannot be trusted: no members, an
    encrypted member, a member without a size, or an unsafe path.
    """

    lines = str(text).splitlines()
    separator = next(
        (index for index, line in enumerate(lines) if _SEPARATOR_RE.match(line)),
        None,
    )
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in lines[separator + 1 :] if separator is not None else lines:
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, sep, value = line.partition(" = ")
        if not sep:
            key, sep, value = line.partition("=")
        if sep:
            current[key.strip().casefold()] = value.strip()
    if current:
        records.append(current)

    entries: list[ListedEntry] = []
    for record in records:
        raw_path = record.get("path")
        if raw_path is None:
            continue
        if record.get("folder") == "+" or record.get("attributes", "").startswith("D"):
            continue
        if record.get("encrypted") == "+":
            return None
        if not raw_path:
            return None
        path = _safe_member_path(raw_path)
        size_text = record.get("size", "")
        if path is None or not size_text.isdigit():
            return None
        entries.append(ListedEntry(path, int(size_text)))
        if len(entries) > MAX_ENTRIES:
            return None
    return tuple(entries) if entries else None


def list_archive_members(
    archive: Path,
    *,
    seven_zip: str = "7z",
    timeout: float = 60.0,
    max_bytes: int = MAX_LISTING_BYTES,
) -> tuple[ListedEntry, ...] | None:
    """Run a bounded, read-only 7-Zip listing and parse it."""

    try:
        with tempfile.TemporaryFile() as listing:
            result = subprocess.run(
                [seven_zip, "l", "-slt", "--", str(archive)],
                stdin=subprocess.DEVNULL,
                stdout=listing,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                return None
            listing.seek(0)
            raw = listing.read(max_bytes + 1)
    except (OSError, subprocess.SubprocessError):
        return None
    if len(raw) > max_bytes:
        return None
    return parse_slt_listing(raw.decode("utf-8", errors="replace"))


@dataclass(frozen=True)
class _ArchiveGroup:
    first_volume: str
    volumes: frozenset[str]
    kind: str  # "rar" | "iso"


def _norm(name: str) -> str:
    return re.sub(r"/+", "/", name.replace("\\", "/")).strip("/")


def find_archive_groups(
    files: Sequence[Mapping[str, Any]],
) -> list[_ArchiveGroup] | None:
    """Group qB file names into RAR sets and ISO images.

    ``None`` means an incomplete or ambiguous set (for example ``.r00`` without
    a first ``.rar``), in which case nothing may be expanded.
    """

    names = [_norm(str(item.get("name") or "")) for item in files]
    groups: list[_ArchiveGroup] = []
    rar_sets: dict[tuple[str, str, bool], dict[str, Any]] = {}
    for name in names:
        if not name:
            continue
        pure = PurePosixPath(name)
        directory = pure.parent.as_posix()
        base = pure.name
        lowered = base.casefold()
        if lowered.endswith(".iso"):
            groups.append(_ArchiveGroup(name, frozenset({name}), "iso"))
            continue
        part = _PART_RAR_RE.match(base)
        if part:
            key = (directory, part.group("stem").casefold(), True)
            entry = rar_sets.setdefault(key, {"volumes": set(), "first": None})
            entry["volumes"].add(name)
            number = int(part.group("number"))
            if entry["first"] is None or number < entry["first"][0]:
                entry["first"] = (number, name)
            continue
        plain = _PLAIN_RAR_RE.match(base)
        if plain:
            key = (directory, plain.group("stem").casefold(), False)
            entry = rar_sets.setdefault(key, {"volumes": set(), "first": None})
            entry["volumes"].add(name)
            entry["first"] = (0, name)
            continue
        old = _OLD_VOLUME_RE.match(base)
        if old:
            key = (directory, old.group("stem").casefold(), False)
            entry = rar_sets.setdefault(key, {"volumes": set(), "first": None})
            entry["volumes"].add(name)
    for entry in rar_sets.values():
        if entry["first"] is None:
            return None
        groups.append(
            _ArchiveGroup(entry["first"][1], frozenset(entry["volumes"]), "rar")
        )
    return groups


def _resolve_archive(root: Path, relative: str) -> Path | None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        return None
    candidate = root.joinpath(*pure.parts)
    if candidate.is_symlink():
        return None
    try:
        target = candidate.resolve(strict=True)
    except OSError:
        return None
    if (root != target and root not in target.parents) or not target.is_file():
        return None
    return target


def expand_archive_manifest(
    torrent: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    *,
    lister: Lister | None = None,
    seven_zip: str | None = None,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """Return ``(files, note)`` with archive volumes replaced by their members."""

    original = list(files)
    note: dict[str, Any] = {"expanded": False, "reason": "no_archive"}
    groups = find_archive_groups(original)
    if groups is None:
        note["reason"] = "incomplete_volume_set"
        return original, note
    if not groups:
        return original, note
    if len(groups) > MAX_ARCHIVES:
        note["reason"] = "too_many_archives"
        return original, note
    try:
        root = Path(str(torrent.get("save_path") or "")).resolve(strict=True)
    except OSError:
        note["reason"] = "save_path_unavailable"
        return original, note
    if not root.is_dir():
        note["reason"] = "save_path_unavailable"
        return original, note

    if lister is None:
        executable = seven_zip or os.environ.get("AITHER_7Z", "7z")

        def lister(path: Path) -> "tuple[ListedEntry, ...] | None":
            return list_archive_members(path, seven_zip=executable)

    replaced: set[str] = set()
    additions: list[dict[str, Any]] = []
    total_entries = 0
    for group in groups:
        archive = _resolve_archive(root, group.first_volume)
        if archive is None:
            note["reason"] = "archive_path_unsafe"
            return original, note
        members = lister(archive)
        if not members:
            note["reason"] = "listing_failed"
            return original, note
        total_entries += len(members)
        if total_entries > MAX_ENTRIES:
            note["reason"] = "too_many_members"
            return original, note
        parent = PurePosixPath(group.first_volume).parent
        for member in members:
            additions.append(
                {
                    "name": (parent / member.path).as_posix()
                    if parent.as_posix() != "."
                    else member.path,
                    "size": member.size,
                    "progress": 1.0,
                    "archive_member": True,
                }
            )
        replaced.update(group.volumes)

    kept = [
        item for item in original if _norm(str(item.get("name") or "")) not in replaced
    ]
    note.update(
        expanded=True,
        reason="listed",
        archives=len(groups),
        entries=total_entries,
    )
    return kept + additions, note
