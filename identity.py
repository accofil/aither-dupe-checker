"""Safe, side-effect-controlled local identity helpers.

This module deliberately does not know how to call Aither.  It turns qBittorrent
metadata and a bounded selection of local text files into small, secret-free
signals, and offers an optional exact IMDb suggestion lookup.  Callers should
keep an ambiguous result unresolved instead of guessing an identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import threading
from typing import Any, Iterable, Literal, Mapping, Sequence
import unicodedata
from urllib.parse import quote, urlsplit

import requests


ManifestStatus = Literal["MEDIA", "NOT_MEDIA", "UNKNOWN"]

_AITHER_DOMAIN = "aither.cc"
_URL_RE = re.compile(r"(?i)https?://[^\s<>\"']+")
_IMDB_RE = re.compile(r"(?i)(?<![a-z0-9])tt(\d{5,10})(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_RAR_PART_RE = re.compile(r"(?i)\.(?:part\d+\.rar|r\d{2,3})$")

_VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".asf",
        ".avi",
        ".divx",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ogm",
        ".ogv",
        ".rmvb",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)
_OPAQUE_MEDIA_EXTENSIONS = frozenset(
    {".iso", ".img", ".rar", ".7z", ".zip", ".001"}
)
_NON_VIDEO_PAYLOAD_EXTENSIONS = frozenset(
    {
        ".apk",
        ".appimage",
        ".deb",
        ".dll",
        ".dmg",
        ".exe",
        ".flac",
        ".m4a",
        ".msi",
        ".mp3",
        ".ogg",
        ".pkg",
        ".rpm",
        ".wav",
    }
)
_STRONG_NON_VIDEO_EXTENSIONS = frozenset(
    {".apk", ".appimage", ".deb", ".dmg", ".exe", ".msi", ".pkg", ".rpm"}
)
_ISO_NON_VIDEO_EXTENSIONS = frozenset(
    {
        ".apk",
        ".appimage",
        ".assets",
        ".bin",
        ".bundle",
        ".cab",
        ".deb",
        ".dll",
        ".dmg",
        ".exe",
        ".flac",
        ".m4a",
        ".mp3",
        ".msi",
        ".ogg",
        ".pak",
        ".pkg",
        ".rpm",
        ".unity3d",
        ".vpk",
        ".wad",
        ".wav",
    }
)
_AUXILIARY_EXTENSIONS = frozenset(
    {
        ".cue",
        ".jpg",
        ".jpeg",
        ".nfo",
        ".png",
        ".sfv",
        ".srt",
        ".sub",
        ".txt",
        ".xml",
    }
)
_SCANNABLE_EXTENSIONS = frozenset({".nfo", ".txt", ".xml"})
_PARENTHETICAL_NOISE = frozenset(
    {
        "3d",
        "4k",
        "bluray",
        "blu ray",
        "complete",
        "criterion",
        "directors cut",
        "extended",
        "extended cut",
        "remastered",
        "theatrical",
        "theatrical cut",
        "uhd",
        "unrated",
    }
)


@dataclass(frozen=True)
class MetadataEvidence:
    """Bounded local metadata findings without paths or file contents.

    ``imdb_id`` is populated only when all findings agree.  Keeping all unique
    candidates in ``imdb_ids`` lets a caller explain ambiguity without ever
    selecting the first occurrence by accident.
    """

    imdb_id: str | None
    imdb_ids: tuple[str, ...]
    bdmv_titles: tuple[str, ...]
    scanned_files: int
    scanned_bytes: int


@dataclass(frozen=True)
class ContainerMetadataEvidence:
    """Small, convergent identity signals read from Matroska format tags."""

    imdb_id: str | None
    imdb_ids: tuple[str, ...]
    tmdb_id: int | None
    tmdb_ids: tuple[int, ...]
    tmdb_refs: tuple[tuple[str | None, int], ...]
    titles: tuple[str, ...]
    series_titles: tuple[str, ...]
    probed_files: int


@dataclass(frozen=True)
class TitleVariant:
    title: str
    year: int | None


@dataclass(frozen=True)
class IMDbIdentity:
    imdb_id: str
    title: str
    year: int
    media_type: Literal["movie", "tv"]


class IMDbSuggestionError(RuntimeError):
    """An IMDb suggestion request failed without exposing its request URL."""


def _candidate_urls(value: Any) -> Iterable[str]:
    if not isinstance(value, str):
        return ()
    stripped = value.strip()
    found = _URL_RE.findall(stripped)
    if found:
        return tuple(found)
    if stripped.startswith("//"):
        return ("https:" + stripped,)
    return ()


def _is_aither_url(value: Any) -> bool:
    for candidate in _candidate_urls(value):
        try:
            hostname = (urlsplit(candidate).hostname or "").rstrip(".").casefold()
            hostname = hostname.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            continue
        if hostname == _AITHER_DOMAIN or hostname.endswith("." + _AITHER_DOMAIN):
            return True
    return False


def detect_source_aither(
    torrent: Mapping[str, Any],
    trackers: Sequence[Mapping[str, Any] | str] = (),
) -> bool:
    """Return only whether qB metadata identifies Aither as the source.

    qBittorrent's ``tracker`` and ``comment`` values may contain passkeys or
    other private URL data.  This function parses them in memory and exposes
    only a boolean.  Host matching is boundary-aware, so ``aither.cc.evil`` and
    ``not-aither.cc`` never count.
    """

    if _is_aither_url(torrent.get("tracker")) or _is_aither_url(
        torrent.get("comment")
    ):
        return True
    for tracker in trackers:
        value = tracker.get("url") if isinstance(tracker, Mapping) else tracker
        if _is_aither_url(value):
            return True
    return False


def _safe_manifest_name(item: Mapping[str, Any]) -> PurePosixPath | None:
    raw = str(item.get("name") or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        return None
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part == ".." for part in pure.parts):
        return None
    return pure


def classify_manifest(files: Sequence[Mapping[str, Any]]) -> ManifestStatus:
    """Classify a qB file manifest conservatively for a movie/TV pipeline.

    Archives and optical-disc images stay eligible because their contents are
    opaque until the later preparation phase.  Unknown payloads produce
    ``UNKNOWN``; ``NOT_MEDIA`` requires positive non-video evidence or a
    manifest consisting solely of sidecar files.
    """

    valid: list[PurePosixPath] = []
    for item in files:
        pure = _safe_manifest_name(item)
        if pure is not None:
            valid.append(pure)
    if not valid:
        return "UNKNOWN"

    has_unknown_payload = False
    has_non_video_payload = False
    has_strong_non_video_payload = False
    for pure in valid:
        parts = tuple(part.casefold() for part in pure.parts)
        suffix = Path(pure.name).suffix.casefold()
        lower_name = pure.name.casefold()
        if "bdmv" in parts or "video_ts" in parts:
            return "MEDIA"
        if suffix in _VIDEO_EXTENSIONS or suffix in _OPAQUE_MEDIA_EXTENSIONS:
            return "MEDIA"
        if _RAR_PART_RE.search(lower_name):
            return "MEDIA"
        if suffix in _NON_VIDEO_PAYLOAD_EXTENSIONS:
            has_non_video_payload = True
            has_strong_non_video_payload |= suffix in _STRONG_NON_VIDEO_EXTENSIONS
        elif suffix not in _AUXILIARY_EXTENSIONS:
            has_unknown_payload = True

    # Installer/application payload is positive evidence even when its data is
    # carried in otherwise opaque .bin/.data companions.  A direct video, disc,
    # or archive signal above always takes precedence.
    if has_strong_non_video_payload:
        return "NOT_MEDIA"
    if has_unknown_payload:
        return "UNKNOWN"
    if has_non_video_payload or valid:
        return "NOT_MEDIA"
    return "UNKNOWN"


def manifest_is_not_media(files: Sequence[Mapping[str, Any]]) -> bool:
    return classify_manifest(files) == "NOT_MEDIA"


def _slt_member_paths(text: str) -> tuple[str, ...]:
    """Return member Path values, excluding 7z's archive-summary record."""

    lines = str(text).splitlines()
    separator = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"\s*-{5,}\s*", line)
        ),
        None,
    )
    # Normal `7z l -slt` output puts the archive metadata before a dashed
    # separator and member records after it.  Accept record-only fixtures too.
    member_lines = lines[separator + 1 :] if separator is not None else lines
    paths: list[str] = []
    for line in member_lines:
        match = re.match(r"(?i)^\s*Path\s*=\s*(.*?)\s*$", line)
        if match is not None and match.group(1):
            paths.append(match.group(1))
    return tuple(paths)


def classify_iso_listing(text: str) -> ManifestStatus:
    """Classify already captured `7z l -slt` ISO member output.

    This pure helper never opens an image or launches a subprocess.  A BDMV or
    VIDEO_TS tree, or a direct video file, is conclusive media evidence.  In
    their absence, executable/game-asset/binary/audio payload is conclusive
    non-video evidence.  Empty, malformed, and otherwise opaque listings stay
    ``UNKNOWN`` rather than being guessed.
    """

    paths = _slt_member_paths(text)
    if not paths:
        return "UNKNOWN"

    has_non_video_payload = False
    for raw in paths:
        normalized = raw.replace("\\", "/").strip(" /")
        if not normalized:
            continue
        pure = PurePosixPath(normalized)
        parts = tuple(part.casefold() for part in pure.parts)
        suffix = Path(pure.name).suffix.casefold()
        if "bdmv" in parts or "video_ts" in parts or suffix in _VIDEO_EXTENSIONS:
            return "MEDIA"
        if suffix in _ISO_NON_VIDEO_EXTENSIONS:
            has_non_video_payload = True

    return "NOT_MEDIA" if has_non_video_payload else "UNKNOWN"


def normalize_imdb_id(value: Any) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?i)(?:tt)?(\d{5,10})", str(value).strip())
    return f"tt{match.group(1)}" if match else None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_read(path: Path, maximum: int) -> bytes | None:
    """Read a regular, non-symlink file with a hard byte ceiling."""

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > maximum:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            return None
        return data
    except OSError:
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _decode_metadata(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass
    declaration = re.search(
        br"(?i)<\?xml[^>]*encoding\s*=\s*['\"]([a-z0-9._-]+)['\"]", data[:256]
    )
    if declaration:
        try:
            return data.decode(declaration.group(1).decode("ascii"))
        except (LookupError, UnicodeError):
            pass
    return data.decode("utf-8", errors="replace")


def _bdmv_meta_titles(text: str) -> Iterable[str]:
    pattern = re.compile(
        r"(?is)<(?:[a-z_][\w.-]*:)?name\b[^>]*>(.*?)"
        r"</(?:[a-z_][\w.-]*:)?name\s*>"
    )
    for match in pattern.finditer(text):
        value = re.sub(r"(?s)<[^>]+>", " ", match.group(1))
        value = html.unescape(value).replace("\x00", " ")
        value = re.sub(r"\s+", " ", value).strip()
        if 1 < len(value) <= 300:
            yield value


def _is_bdmv_meta_xml(pure: PurePosixPath) -> bool:
    parts = tuple(part.casefold() for part in pure.parts)
    for index in range(len(parts) - 1):
        if parts[index : index + 2] == ("bdmv", "meta"):
            return pure.suffix.casefold() == ".xml"
    return False


def scan_local_metadata(
    torrent: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    *,
    max_file_bytes: int = 2 * 1024 * 1024,
    max_total_bytes: int = 4 * 1024 * 1024,
    max_files: int = 64,
) -> MetadataEvidence:
    """Scan small NFO/XML/TXT files listed by qB, within ``save_path`` only.

    Both qB's reported size and the live regular-file size must fit the limits.
    Absolute paths, ``..`` paths, symlinks, and paths resolving outside the
    save root are ignored.  At most ``max_files`` and ``max_total_bytes`` are
    read, making filesystem I/O explicit and bounded.
    """

    if max_file_bytes <= 0 or max_total_bytes <= 0 or max_files <= 0:
        raise ValueError("metadata scan limits must be positive")
    imdb_ids: set[str] = set()
    name_match = _IMDB_RE.search(str(torrent.get("name") or ""))
    if name_match:
        imdb_ids.add(f"tt{name_match.group(1)}")
    raw_root = str(torrent.get("save_path") or "").strip()
    unresolved_root = Path(raw_root)
    try:
        if not raw_root or not unresolved_root.is_absolute():
            raise OSError("unsafe save root")
        root = unresolved_root.resolve(strict=True)
        if root == Path(root.anchor):
            raise OSError("overly broad save root")
    except OSError:
        sorted_ids = tuple(sorted(imdb_ids))
        return MetadataEvidence(
            sorted_ids[0] if len(sorted_ids) == 1 else None,
            sorted_ids,
            (),
            0,
            0,
        )
    if not root.is_dir():
        sorted_ids = tuple(sorted(imdb_ids))
        return MetadataEvidence(
            sorted_ids[0] if len(sorted_ids) == 1 else None,
            sorted_ids,
            (),
            0,
            0,
        )

    titles: set[str] = set()
    scanned_files = 0
    scanned_bytes = 0

    ordered: list[tuple[str, Mapping[str, Any], PurePosixPath]] = []
    for item in files:
        pure = _safe_manifest_name(item)
        if pure is None or pure.suffix.casefold() not in _SCANNABLE_EXTENSIONS:
            continue
        ordered.append((pure.as_posix().casefold(), item, pure))

    seen_names: set[str] = set()
    for _, item, pure in sorted(ordered, key=lambda row: row[0]):
        normalized_name = pure.as_posix().casefold()
        if normalized_name in seen_names or scanned_files >= max_files:
            continue
        seen_names.add(normalized_name)
        try:
            reported_size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            continue
        remaining = max_total_bytes - scanned_bytes
        ceiling = min(max_file_bytes, remaining)
        if reported_size <= 0 or reported_size > ceiling or ceiling <= 0:
            continue

        unresolved = root.joinpath(*pure.parts)
        try:
            if unresolved.is_symlink():
                continue
            candidate = unresolved.resolve(strict=True)
        except OSError:
            continue
        if not _is_within(candidate, root) or candidate.is_symlink():
            continue
        data = _bounded_read(candidate, ceiling)
        if data is None:
            continue

        scanned_files += 1
        scanned_bytes += len(data)
        text = _decode_metadata(data)
        imdb_ids.update(f"tt{value}" for value in _IMDB_RE.findall(text))
        if _is_bdmv_meta_xml(pure):
            titles.update(_bdmv_meta_titles(text))

    sorted_ids = tuple(sorted(imdb_ids))
    return MetadataEvidence(
        imdb_id=sorted_ids[0] if len(sorted_ids) == 1 else None,
        imdb_ids=sorted_ids,
        bdmv_titles=tuple(sorted(titles, key=lambda value: value.casefold())),
        scanned_files=scanned_files,
        scanned_bytes=scanned_bytes,
    )


_CONTAINER_TITLE_TAGS = frozenset(
    {"title", "movie", "show", "show_name", "series", "series_name"}
)
_CONTAINER_IMDB_TAGS = frozenset(
    {"imdb", "imdb_id", "imdbid", "imdb_url", "imdb_link", "uniqueid_imdb"}
)
_IMDB_URL_VALUE_RE = re.compile(
    r"(?i)(?<![a-z0-9.-])(?:https?://)?(?:(?:www|m)\.)?"
    r"imdb\.com/title/(tt\d{5,10})(?!\d)"
)
_TMDB_URL_RE = re.compile(
    r"(?i)(?<![a-z0-9.-])(?:https?://)?(?:www\.)?"
    r"themoviedb\.org/(movie|tv)/(\d{1,10})(?!\d)"
)
_TMDB_TAG_RE = re.compile(
    r"(?i)(?:(movie|tv)\s*[/#:=-]\s*)?(\d{1,10})(?!\d)"
)


def _empty_container_metadata() -> ContainerMetadataEvidence:
    return ContainerMetadataEvidence(None, (), None, (), (), (), (), 0)


def _clean_container_title(value: str) -> str | None:
    cleaned = html.unescape(str(value)).replace("\x00", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not 1 < len(cleaned) <= 500:
        return None
    # Episode tags often contain `Series S01E02` (sometimes followed by the
    # episode title).  The catalogue lookup needs the series identity.
    series = re.sub(
        r"(?i)\s+S\d{1,2}E\d{1,3}(?:\s*[-E]\s*\d{1,3})?(?:\s.*)?$",
        "",
        cleaned,
    ).strip(" ._-")
    return series if canonical_title(series) else None


def _parse_ffprobe_container_metadata(raw: bytes | str) -> ContainerMetadataEvidence:
    """Parse bounded ffprobe JSON without trusting tag keys or value types."""

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_container_metadata()
    if not isinstance(payload, Mapping):
        return _empty_container_metadata()
    format_row = payload.get("format")
    tags = format_row.get("tags") if isinstance(format_row, Mapping) else None
    if not isinstance(tags, Mapping):
        return _empty_container_metadata()

    imdb_ids: set[str] = set()
    tmdb_refs: set[tuple[str | None, int]] = set()
    titles: set[str] = set()
    series_titles: set[str] = set()
    for raw_key, raw_value in tags.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = re.sub(r"[^a-z0-9]+", "_", raw_key.casefold()).strip("_")
        value = raw_value.strip()
        if not value or "\x00" in value or len(value) > 4096:
            continue
        # A bare ``tt...`` sequence is authoritative only in a dedicated IMDb
        # tag.  Arbitrary COMMENT/ENCODER text may mention unrelated IDs.  A
        # full imdb.com/title URL remains explicit enough in any safe tag.
        imdb_ids.update(
            match.group(1).casefold()
            for match in _IMDB_URL_VALUE_RE.finditer(value)
        )
        if key in _CONTAINER_IMDB_TAGS:
            imdb_ids.update(f"tt{digits}" for digits in _IMDB_RE.findall(value))
        if key in _CONTAINER_TITLE_TAGS:
            title = _clean_container_title(value)
            if title is not None:
                titles.add(title)
                if key in {"show", "show_name", "series", "series_name"} or re.search(
                    r"(?i)(?<![A-Z0-9])S\d{1,2}E\d{1,3}(?!\d)", value
                ):
                    series_titles.add(title)

        for match in _TMDB_URL_RE.finditer(value):
            tmdb_refs.add((match.group(1).casefold(), int(match.group(2))))
        if "tmdb" in key:
            for tagged in _TMDB_TAG_RE.finditer(value):
                kind = tagged.group(1)
                tmdb_refs.add((kind.casefold() if kind else None, int(tagged.group(2))))

    sorted_imdb = tuple(sorted(imdb_ids))
    sorted_refs = tuple(sorted(tmdb_refs, key=lambda item: (item[1], item[0] or "")))
    sorted_tmdb = tuple(sorted({item[1] for item in sorted_refs}))
    explicit_types = {item[0] for item in sorted_refs if item[0] is not None}
    unique_tmdb = len(sorted_tmdb) == 1 and len(explicit_types) <= 1
    return ContainerMetadataEvidence(
        imdb_id=sorted_imdb[0] if len(sorted_imdb) == 1 else None,
        imdb_ids=sorted_imdb,
        tmdb_id=sorted_tmdb[0] if unique_tmdb else None,
        tmdb_ids=sorted_tmdb,
        tmdb_refs=sorted_refs,
        titles=tuple(sorted(titles, key=lambda item: item.casefold())),
        series_titles=tuple(
            sorted(series_titles, key=lambda item: item.casefold())
        ),
        probed_files=1,
    )


def _probe_matroska_path(
    path: Path,
    *,
    expected_root: Path,
    expected_size: int,
    executable: Path,
    timeout: float,
    max_output_bytes: int,
) -> ContainerMetadataEvidence:
    """Probe an already validated path through a non-following file handle."""

    if os.name != "posix" or timeout <= 0 or max_output_bytes <= 0:
        return _empty_container_metadata()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return _empty_container_metadata()
    try:
        info = os.fstat(descriptor)
        proc_path = Path(f"/proc/self/fd/{descriptor}")
        try:
            opened_target = proc_path.resolve(strict=True)
        except OSError:
            return _empty_container_metadata()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size != expected_size
            or not proc_path.exists()
            or not _is_within(opened_target, expected_root)
            or opened_target != path
        ):
            return _empty_container_metadata()
        try:
            process = subprocess.Popen(
                [
                    str(executable),
                    "-v",
                    "error",
                    "-show_entries",
                    "format_tags",
                    "-of",
                    "json",
                    str(proc_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(descriptor,),
            )
        except OSError:
            return _empty_container_metadata()
        timed_out = threading.Event()

        def kill_process() -> None:
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                # The main thread may have reaped the child between poll/kill.
                pass

        def reap_process() -> None:
            kill_process()
            try:
                process.wait()
            except (OSError, subprocess.SubprocessError):
                pass

        def kill_on_timeout() -> None:
            if process.poll() is None:
                timed_out.set()
                kill_process()

        timer = threading.Timer(timeout, kill_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None:
                reap_process()
                return _empty_container_metadata()
            stdout = process.stdout.read(max_output_bytes + 1)
            if len(stdout) > max_output_bytes and process.poll() is None:
                kill_process()
            return_code = process.wait()
        except (OSError, subprocess.SubprocessError):
            reap_process()
            return _empty_container_metadata()
        finally:
            timer.cancel()
            if process.stdout is not None:
                process.stdout.close()
        if timed_out.is_set() or return_code != 0 or len(stdout) > max_output_bytes:
            return _empty_container_metadata()
        return _parse_ffprobe_container_metadata(stdout)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def scan_matroska_metadata(
    torrent: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    *,
    max_files: int = 8,
    timeout: float = 8.0,
    max_output_bytes: int = 64 * 1024,
    ffprobe_path: Path = Path("/usr/bin/ffprobe"),
) -> ContainerMetadataEvidence:
    """Lazily read bounded Matroska format tags from qB-owned regular files."""

    if max_files <= 0 or timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("container metadata limits must be positive")
    raw_root = str(torrent.get("save_path") or "").strip()
    try:
        root = Path(raw_root).resolve(strict=True)
        executable = Path(ffprobe_path).resolve(strict=True)
    except OSError:
        return _empty_container_metadata()
    if (
        not raw_root
        or not Path(raw_root).is_absolute()
        or root == Path(root.anchor)
        or not root.is_dir()
        or not Path(ffprobe_path).is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        return _empty_container_metadata()

    ordered: list[tuple[str, Mapping[str, Any], PurePosixPath]] = []
    for item in files:
        pure = _safe_manifest_name(item)
        if pure is not None and pure.suffix.casefold() == ".mkv":
            ordered.append((pure.as_posix().casefold(), item, pure))

    eligible_names = {pure.as_posix() for _, _, pure in ordered}
    # Never accept a positive identity from a partial sample: an unprobed
    # later file could carry a conflicting catalogue tag.  The bounded cap is
    # a safety limit, not permission to trust the first N files.
    if len(eligible_names) > max_files:
        return _empty_container_metadata()

    imdb_ids: set[str] = set()
    tmdb_refs: set[tuple[str | None, int]] = set()
    titles: set[str] = set()
    series_titles: set[str] = set()
    probed_files = 0
    attempted_files = 0
    seen_names: set[str] = set()
    for _, item, pure in sorted(ordered, key=lambda row: (row[0], row[2].as_posix())):
        exact_name = pure.as_posix()
        if exact_name in seen_names or attempted_files >= max_files:
            continue
        seen_names.add(exact_name)
        raw_size = item.get("size")
        if isinstance(raw_size, bool):
            continue
        try:
            expected_size = int(raw_size)
        except (TypeError, ValueError, OverflowError):
            continue
        if expected_size <= 0:
            continue

        unresolved = root.joinpath(*pure.parts)
        try:
            if unresolved.is_symlink():
                continue
            candidate = unresolved.resolve(strict=True)
        except OSError:
            continue
        if (
            candidate != unresolved
            or not _is_within(candidate, root)
            or candidate.is_symlink()
        ):
            continue
        attempted_files += 1
        evidence = _probe_matroska_path(
            candidate,
            expected_root=root,
            expected_size=expected_size,
            executable=executable,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        if evidence.probed_files != 1:
            continue
        probed_files += 1
        imdb_ids.update(evidence.imdb_ids)
        tmdb_refs.update(evidence.tmdb_refs)
        titles.update(evidence.titles)
        series_titles.update(evidence.series_titles)

    if attempted_files != len(eligible_names) or probed_files != len(eligible_names):
        return _empty_container_metadata()

    sorted_imdb = tuple(sorted(imdb_ids))
    sorted_refs = tuple(sorted(tmdb_refs, key=lambda item: (item[1], item[0] or "")))
    sorted_tmdb = tuple(sorted({item[1] for item in sorted_refs}))
    explicit_types = {item[0] for item in sorted_refs if item[0] is not None}
    unique_tmdb = len(sorted_tmdb) == 1 and len(explicit_types) <= 1
    return ContainerMetadataEvidence(
        imdb_id=sorted_imdb[0] if len(sorted_imdb) == 1 else None,
        imdb_ids=sorted_imdb,
        tmdb_id=sorted_tmdb[0] if unique_tmdb else None,
        tmdb_ids=sorted_tmdb,
        tmdb_refs=sorted_refs,
        titles=tuple(sorted(titles, key=lambda item: item.casefold())),
        series_titles=tuple(
            sorted(series_titles, key=lambda item: item.casefold())
        ),
        probed_files=probed_files,
    )


def canonical_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value)).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _clean_title(value: str) -> str:
    value = str(value).replace("_", " ").replace(".", " ")
    value = re.sub(r"\s+", " ", value).strip(" -+&\t\r\n")
    return value


def _title_and_year(value: str, fallback_year: int | None) -> TitleVariant | None:
    cleaned = _clean_title(value)
    year_match = None
    explicit_years = list(
        re.finditer(r"[\[(]\s*((?:18|19|20)\d{2})\s*[\])]", cleaned)
    )
    if explicit_years:
        year_match = explicit_years[-1]
    else:
        terminal = re.search(r"(?:^|\s)((?:18|19|20)\d{2})\s*$", cleaned)
        # A purely numeric title such as 1917 or 1670 is not its release year.
        if terminal is not None and canonical_title(cleaned[: terminal.start()]):
            year_match = terminal
    found_year = int(year_match.group(1)) if year_match is not None else fallback_year
    if year_match is not None:
        cleaned = (cleaned[: year_match.start()] + " " + cleaned[year_match.end() :])
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    if not canonical_title(cleaned):
        return None
    return TitleVariant(cleaned, found_year)


def title_alias_variants(title: str, year: int | None = None) -> tuple[TitleVariant, ...]:
    """Generate deterministic exact-query variants without fuzzy guessing.

    Supported structures are bilingual ``Title - Alias`` names,
    parenthetical aliases, and ``&``/``+`` multi-features where every segment
    carries its own year.  A structurally explicit label emits only its real
    component titles; otherwise the complete cleaned title is the fallback.
    """

    raw = _clean_title(title)
    if not raw:
        return ()
    variants: list[TitleVariant] = []

    multi_parts = re.split(r"\s+(?:&|\+)\s+", raw)
    parsed_multi = [_title_and_year(part, None) for part in multi_parts]
    if len(multi_parts) > 1 and all(
        item is not None and item.year is not None for item in parsed_multi
    ):
        # A multi-feature is not one long IMDb title.  Preserve each work and
        # its own year; do not emit the joined release label as a false query.
        return _unique_variants(item for item in parsed_multi if item is not None)

    bilingual_parts = re.split(r"\s+-\s+", raw)
    if len(bilingual_parts) == 2:
        aliases = tuple(
            item
            for item in (
                _title_and_year(part, year) for part in bilingual_parts
            )
            if item is not None
        )
        if len(aliases) == len(bilingual_parts):
            # The joined bilingual label is likewise not a real catalog title.
            return _unique_variants(aliases)

    parenthetical_values = re.findall(r"\(([^()]*)\)", raw)
    if parenthetical_values:
        whole_with_year = _title_and_year(raw, year)
        effective_year = whole_with_year.year if whole_with_year is not None else year
        base = re.sub(r"\([^()]*\)", " ", raw)
        base_variant = _title_and_year(base, effective_year)
        if base_variant is not None:
            variants.append(base_variant)
        for value in parenthetical_values:
            if re.fullmatch(r"\s*(?:18|19|20)\d{2}\s*", value):
                continue
            inner = _title_and_year(value, effective_year)
            if inner is None:
                continue
            inner_canonical = canonical_title(inner.title)
            inner_canonical = re.sub(r"^(?:aka|also known as)\s+", "", inner_canonical)
            if inner_canonical in _PARENTHETICAL_NOISE:
                continue
            display = re.sub(r"(?i)^\s*(?:aka|also known as)\s*[:\-]?\s*", "", inner.title)
            if canonical_title(display):
                variants.append(TitleVariant(display, inner.year))

    if not variants:
        whole = _title_and_year(raw, year)
        if whole is not None:
            variants.append(whole)
    return _unique_variants(variants)


def _unique_variants(variants: Iterable[TitleVariant]) -> tuple[TitleVariant, ...]:
    unique: list[TitleVariant] = []
    seen: set[tuple[str, int | None]] = set()
    for variant in variants:
        key = (canonical_title(variant.title), variant.year)
        if key not in seen:
            seen.add(key)
            unique.append(variant)
    return tuple(unique)


_TECHNICAL_TAIL_RE = re.compile(
    r"(?ix)(?:^|\s)(?:"
    r"(?:4k|(?:4320|2160|1080|720|576|540|480)[pi])"
    r"|s\d{1,2}(?:e\d{1,3}(?:[-e]\d{1,3})?)?"
    r"|(?:web(?:[ .-]?dl)?|webrip|hdtv|pdtv|dsr)"
    r"|(?:uhd|blu[ .-]?ray|bluray|bd(?:25|50|66|100)?|dvd(?:5|9)?)"
    r"|(?:remux|x26[45]|h[ .-]?26[45]|avc|hevc|vc[ .-]?1|mpeg[ .-]?2)"
    r"|(?:complete|dual|multi|remaster(?:ed)?|hdr10\+?|hdr|dolby[ .-]?vision)"
    r")(?=\s|$)"
)


def _release_identity_head(name: str) -> str:
    cleaned = _clean_title(name)
    if not cleaned:
        return ""
    # Technical words inside a genuine title should not win over a later,
    # explicit release year.  Start tail detection after the last year token.
    year_matches = list(_YEAR_RE.finditer(cleaned))
    search_from = year_matches[-1].end() if year_matches else 0
    tail = _TECHNICAL_TAIL_RE.search(cleaned, search_from)
    if tail is not None:
        cleaned = cleaned[: tail.start()].strip(" -")
    return cleaned


def _scene_alias_variants(
    name: str, fallback_year: int | None
) -> tuple[TitleVariant, ...]:
    """Split only the explicit scene ``.-.`` alias separator.

    A generic display-title dash can be part of a real catalogue title.  The
    dot-delimited release separator is much stronger structure and safely
    covers two to four localized/market labels, including the three-name
    Starfight release.
    """

    parts = re.split(r"\.\-\.", str(name))
    if not 2 <= len(parts) <= 4:
        return ()
    parts[-1] = _release_identity_head(parts[-1])
    parsed = tuple(_title_and_year(part, fallback_year) for part in parts)
    if any(item is None for item in parsed):
        return ()
    return _unique_variants(item for item in parsed if item is not None)


def _aka_alias_variants(
    name: str, fallback_year: int | None
) -> tuple[TitleVariant, ...]:
    """Split only a standalone AKA marker in the non-technical title head."""

    head = _release_identity_head(name)
    if not head:
        return ()
    parts = re.split(r"(?i)\s+(?:aka|also\s+known\s+as)\s+", head)
    if not 2 <= len(parts) <= 3:
        return ()
    parsed = tuple(_title_and_year(part, fallback_year) for part in parts)
    usable = tuple(item for item in parsed if item is not None)
    if not usable:
        return ()
    return _unique_variants(usable)


def release_identity_variants(
    name: str,
    parsed_title: str,
    year: int | None,
    bdmv_titles: Sequence[str] = (),
) -> tuple[TitleVariant, ...]:
    """Recover catalog-title variants before a scene parser loses structure.

    The original release label is cut at its first technical-tail token only
    after the last explicit year.  This preserves dotted ``.-.`` bilingual
    names and per-feature parenthesized years.  BDMV META titles and the normal
    parser title are deterministic fallbacks; all variants are deduplicated by
    exact canonical title and year.
    """

    explicit_aliases = _scene_alias_variants(name, year)
    if explicit_aliases:
        return explicit_aliases
    aka_aliases = _aka_alias_variants(name, year)
    if aka_aliases:
        return aka_aliases

    variants: list[TitleVariant] = []
    head = _release_identity_head(name)
    if head:
        original_variants = title_alias_variants(head, year)
        # Two or more variants from the untouched label mean an explicit
        # alias/multi-feature structure.  A lossy parser fallback must not add
        # a fabricated joined title to that set.
        if len(original_variants) > 1:
            return _unique_variants(original_variants)
        variants.extend(original_variants)
    for title in bdmv_titles:
        structured = _aka_alias_variants(str(title), year)
        variants.extend(structured or title_alias_variants(str(title), year))
    if parsed_title:
        variants.extend(title_alias_variants(parsed_title, year))
    return _unique_variants(variants)


def _imdb_year(item: Mapping[str, Any]) -> int | None:
    raw = item.get("y")
    if isinstance(raw, int) and 1800 <= raw <= 2099:
        return raw
    if isinstance(raw, str):
        match = _YEAR_RE.search(raw)
        if match:
            return int(match.group(1))
    span = item.get("yr")
    if isinstance(span, str):
        match = _YEAR_RE.search(span)
        if match:
            return int(match.group(1))
    return None


def _imdb_media_type(item: Mapping[str, Any]) -> Literal["movie", "tv"] | None:
    qid = str(item.get("qid") or "").casefold()
    label = str(item.get("q") or "").casefold()
    if qid in {"movie", "tvmovie", "video", "short"} or label in {
        "feature",
        "short",
        "tv movie",
        "video",
    }:
        return "movie"
    if qid in {"tvseries", "tvminiseries"} or label in {
        "tv mini-series",
        "tv miniseries",
        "tv series",
    }:
        return "tv"
    return None


def _imdb_titles(item: Mapping[str, Any]) -> Iterable[str]:
    primary = item.get("l")
    if isinstance(primary, str):
        yield primary
    akas = item.get("akas")
    if isinstance(akas, Sequence) and not isinstance(akas, (str, bytes)):
        for value in akas:
            if isinstance(value, str):
                yield value


class IMDbSuggestionClient:
    """Strict client for IMDb's public suggestion endpoint.

    The endpoint is used solely to recover a title ID.  No fuzzy score, result
    rank, or popularity is allowed to override an exact canonical title, exact
    year, and matching movie/TV family.  Multiple surviving IDs are ambiguous.
    """

    _BASE = "https://v3.sg.media-imdb.com/suggestion/x"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    def search_exact(
        self,
        title: str,
        year: int | None,
        media_type: Literal["movie", "tv"],
    ) -> IMDbIdentity | None:
        query = _clean_title(title)
        canonical_query = canonical_title(query)
        if not canonical_query or year is None:
            return None
        if media_type not in {"movie", "tv"}:
            raise ValueError("media_type must be movie or tv")
        try:
            exact_year = int(year)
        except (TypeError, ValueError):
            return None
        if exact_year < 1800 or exact_year > 2099 or len(query) > 300:
            return None

        url = f"{self._BASE}/{quote(query, safe='')}.json"
        try:
            response = self.session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            # Requests exceptions commonly include the fully prepared URL.
            raise IMDbSuggestionError("IMDb suggestion network error") from None
        if response.status_code != 200:
            raise IMDbSuggestionError(
                f"IMDb suggestion HTTP error: {int(response.status_code)}"
            )
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise IMDbSuggestionError("IMDb suggestion returned invalid JSON") from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("d", []), list):
            raise IMDbSuggestionError("IMDb suggestion returned an invalid payload")

        matches: dict[str, IMDbIdentity] = {}
        for raw in payload.get("d", [])[:100]:
            if not isinstance(raw, Mapping):
                continue
            imdb_id = normalize_imdb_id(raw.get("id"))
            remote_year = _imdb_year(raw)
            remote_type = _imdb_media_type(raw)
            if imdb_id is None or remote_year != exact_year or remote_type != media_type:
                continue
            if canonical_query not in {
                canonical_title(candidate) for candidate in _imdb_titles(raw)
            }:
                continue
            remote_title = next(iter(_imdb_titles(raw)), query)
            matches[imdb_id] = IMDbIdentity(
                imdb_id=imdb_id,
                title=remote_title,
                year=remote_year,
                media_type=remote_type,
            )
        if len(matches) != 1:
            return None
        return next(iter(matches.values()))


__all__ = [
    "IMDbIdentity",
    "IMDbSuggestionClient",
    "IMDbSuggestionError",
    "ManifestStatus",
    "MetadataEvidence",
    "ContainerMetadataEvidence",
    "TitleVariant",
    "canonical_title",
    "classify_iso_listing",
    "classify_manifest",
    "detect_source_aither",
    "manifest_is_not_media",
    "normalize_imdb_id",
    "release_identity_variants",
    "scan_local_metadata",
    "scan_matroska_metadata",
    "title_alias_variants",
]
