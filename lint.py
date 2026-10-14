"""Pre-upload lint for releases the checker would hand to the uploader.

Aither staff reject some releases that are not duplicates.  The one observed so
far (2026-10-13, an HDH 2160p remux) was rejected with "redundant audio": the
file carried three English tracks (TrueHD Atmos 7.1, Dolby Digital 5.1 and
DTS-HD MA 5.1).  This module reads the track list with MediaInfo (read-only)
and warns about the same pattern before the upload is attempted.

The findings are warnings only: they never change a decision or the READY list.
The rule below is inferred from that rejection, not from the tracker's wording.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

VIDEO_SUFFIXES = (".mkv", ".mp4")
_EXTRA_TRACK_RE = re.compile(
    r"(?i)commentary|audio[ ._-]?description|descriptive|isolated|"
    r"music[ ._-]?(?:and|&)[ ._-]?effects|\bm\s?&\s?e\b|\bad\b"
)
# MediaInfo ServiceKind: C = commentary, VI = visually impaired (audio description)
_EXTRA_SERVICE_KINDS = {"C", "VI"}

MediaInfoRunner = Callable[[Path], "Mapping[str, Any] | None"]


@dataclass(frozen=True)
class LintFinding:
    code: str
    message: str


def run_mediainfo(path: Path, *, timeout: float = 60.0) -> Mapping[str, Any] | None:
    try:
        result = subprocess.run(
            ["mediainfo", "--Output=JSON", "--", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _tracks(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    media = payload.get("media")
    tracks = media.get("track") if isinstance(media, Mapping) else None
    return [t for t in tracks if isinstance(t, Mapping)] if isinstance(tracks, list) else []


def _language(track: Mapping[str, Any]) -> str:
    value = str(track.get("Language") or "").strip().casefold()
    return value[:2] if value else "und"


def _describe(track: Mapping[str, Any]) -> str:
    name = str(track.get("Format_Commercial_IfAny") or track.get("Format") or "?")
    name = (
        name.replace("Dolby TrueHD with Dolby Atmos", "TrueHD Atmos")
        .replace("Dolby Digital Plus", "DD+")
        .replace("Dolby Digital", "DD")
        .replace("Dolby TrueHD", "TrueHD")
        .replace("DTS-HD Master Audio", "DTS-HD MA")
    )
    channels = str(track.get("Channels") or "").strip()
    return f"{name} {channels}ch" if channels.isdigit() else name


_COMPAT_RE = re.compile(r"(?i)compatib")
_GENERIC_TITLE_WORDS = frozenset(
    """english french german spanish italian japanese chinese korean russian
    portuguese dutch swedish danish norwegian finnish polish czech hungarian
    turkish arabic hebrew hindi thai greek dolby digital plus truehd atmos dts
    hd master audio ma x es core lossless lossy flac pcm lpcm aac ac eac ddp dd
    mlp surround stereo mono mix channel channels ch track main primary default
    sound""".split()
)
_LOSSLESS_FORMATS = {"MLP FBA", "MLP", "FLAC", "PCM", "ALAC"}


def _is_lossless(track: Mapping[str, Any]) -> bool:
    if str(track.get("Compression_Mode") or "").casefold() == "lossless":
        return True
    if str(track.get("Format") or "") in _LOSSLESS_FORMATS:
        return True
    extra = " ".join(
        str(track.get(key) or "") for key in ("Format_AdditionalFeatures", "Format_Commercial_IfAny")
    )
    return "XLL" in extra or "Master Audio" in extra or "TrueHD" in extra


def _meaningful_title(title: str) -> bool:
    """A title that says something beyond language, codec or channel layout."""

    words = re.findall(r"[^\W\d_]+", title.casefold())
    return any(len(word) > 2 and word not in _GENERIC_TITLE_WORDS for word in words)


def redundant_audio(payload: Mapping[str, Any]) -> list[str]:
    """Same-language audio that looks redundant, e.g. ``en: TrueHD + DD (compat)``.

    Commentaries and audio descriptions are ignored.  A lossy track with a
    real title (an extra such as a reading or interview) is ignored.  A track
    titled "Compatibility Track" next to a main track is redundant.  Several
    main tracks are redundant unless every one carries its own meaningful
    title (alternate mixes such as "Original Mono" and "5.1 Surround Remix").
    """

    mains: dict[str, list[Mapping[str, Any]]] = {}
    compat: dict[str, list[Mapping[str, Any]]] = {}
    for track in _tracks(payload):
        if track.get("@type") != "Audio":
            continue
        title = str(track.get("Title") or "")
        if _EXTRA_TRACK_RE.search(title) or str(track.get("ServiceKind") or "") in _EXTRA_SERVICE_KINDS:
            continue
        language = _language(track)
        if _COMPAT_RE.search(title):
            compat.setdefault(language, []).append(track)
        elif _is_lossless(track) or not _meaningful_title(title):
            mains.setdefault(language, []).append(track)

    findings: list[str] = []
    for language in sorted(set(mains) | set(compat)):
        main_tracks = mains.get(language, [])
        compat_tracks = compat.get(language, [])
        titles = [str(t.get("Title") or "").strip().casefold() for t in main_tracks]
        alternate_mixes = (
            len(main_tracks) > 1
            and all(_meaningful_title(t) for t in titles)
            and len(set(titles)) == len(titles)
        )
        multiple_mains = len(main_tracks) > 1 and not alternate_mixes
        has_compat = bool(compat_tracks) and bool(main_tracks)
        if not (multiple_mains or has_compat):
            continue
        names = [_describe(t) for t in [*main_tracks, *compat_tracks]]
        note = " (compatibility track)" if has_compat else ""
        findings.append(f"{language}: " + " + ".join(names) + note)
    return findings


def _video_file(path: Path) -> Path | None:
    if path.is_file():
        return path if path.suffix.casefold() in VIDEO_SUFFIXES else None
    if not path.is_dir():
        return None
    children = {child.name.casefold() for child in path.iterdir() if child.is_dir()}
    if children & {"bdmv", "video_ts"}:
        return None
    videos = [
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES
        and "sample" not in {part.casefold() for part in item.parts}
    ]
    return max(videos, key=lambda item: item.stat().st_size, default=None)


def lint_release(
    path: Path,
    release_type: str | None,
    *,
    runner: MediaInfoRunner = run_mediainfo,
) -> list[LintFinding]:
    """Findings for one READY path.  Only remuxes are checked for redundant audio."""

    if str(release_type or "").casefold() != "remux":
        return []
    video = _video_file(path)
    if video is None:
        return []
    payload = runner(video)
    if payload is None:
        return []
    findings: list[LintFinding] = []
    redundant = redundant_audio(payload)
    if redundant:
        findings.append(
            LintFinding(
                "REDUNDANT_AUDIO",
                "több hangsáv azonos nyelven (Aither-elutasítás: redundant audio): "
                + "; ".join(redundant),
            )
        )
    return findings


def lint_many(
    items: Sequence[tuple[str, Path, str | None]],
    *,
    runner: MediaInfoRunner = run_mediainfo,
) -> dict[str, list[LintFinding]]:
    """``items``: (display name, path, release type) -> findings by name."""

    result: dict[str, list[LintFinding]] = {}
    for name, path, release_type in items:
        findings = lint_release(path, release_type, runner=runner)
        if findings:
            result[name] = findings
    return result
