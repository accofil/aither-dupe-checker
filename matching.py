"""Pure release parsing and Aither duplicate matching helpers.

This module deliberately has no network or filesystem side effects.  The caller is
responsible for resolving an identity and for making the Aither request built by
``build_aither_filter_params``.
"""

from __future__ import annotations

from enum import Enum
import hashlib
import html
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


# Snapshot of Upload-Assistant's AITHER banned-group cache (2026-08-10).
# UNIT3D type IDs: 1 Full Disc, 2 Remux, 3 Encode, 4 WEB-DL,
# 5 WEBRip, 6 HDTV.  Entries with no type IDs retain Aither's conservative
# no-type-given semantics below.
BLACKLIST_ALL = {
    "4k4u", "afm72", "aroma", "bandi", "bitor", "bluespots", "bols", "d3g",
    "depraved", "ember", "fgt", "freethefish", "garshasp", "ghost", "grym",
    "hds", "hi10", "hiqve", "ime", "ion10", "ivy", "judas", "lama",
    "langbard", "lion", "megusta", "monolith", "natty", "nikt0", "oeplus",
    "oft", "osc", "panda", "pirates", "pyc", "qxr", "r00t", "ralphy", "rarbg",
    "retropeeps", "rzerox", "sampa", "sicario", "sicfoi", "silence",
    "skiptt", "sm737", "spdvd", "stuttershit", "swtyblz", "t3nzin", "taoe",
    "tekno3d", "telly", "tgx", "tigole", "tsp", "tspxl", "unknown", "vxt",
    "vyndros", "will1869", "x0r", "yify", "yts", "yts.mx",
}

BLACKLIST_BY_TYPE = {
    "aisha": {"Remux"},
    "beechyboy": {"WEB-DL"},
    "chivaman": {"Encode"},
    "edge2020": {"Remux", "Encode"},
    "evo": {"Encode"},
    "flights": {"Remux"},
    "hdt": {"Remux"},
    "j3rico": {"Encode"},
    "mge": {"WEB-DL"},
    "noxxus": {"Encode"},
    "rcvr": {"Encode", "WEBRip"},
    "sasukeduck": {"WEB-DL"},
    "spirit": {"Encode"},
    "sumvision": {"WEB-DL"},
    "weasley[hone]": {"WEB-DL", "WEBRip"},
    "yawnix": {"Encode"},
}

BLACKLIST_NO_TYPE_GIVEN = {
    "alcaide_kira", "aoc", "ben the men", "colortv", "creative24", "drx",
    "mrcs", "nani", "pandemonium", "phocis", "psa", "seedpool", "spx",
    "syncup", "twa", "vdon", "w32", "wks",
}


def apply_banned_groups(groups: Any) -> None:
    """Replace the static snapshot with a fresh Aither list (in place).

    ``groups`` is a ``banned_groups.BannedGroups``.  The module-level sets are
    mutated rather than rebound so every existing reference keeps working.
    """

    BLACKLIST_ALL.clear()
    BLACKLIST_ALL.update(groups.all_types)
    BLACKLIST_NO_TYPE_GIVEN.clear()
    BLACKLIST_NO_TYPE_GIVEN.update(groups.no_type_given)
    BLACKLIST_BY_TYPE.clear()
    BLACKLIST_BY_TYPE.update({name: set(types) for name, types in groups.by_type.items()})


_ADULT_SITE_PATTERNS = (
    (
        "5KPORN",
        re.compile(
            r"(?<![A-Za-z0-9])5K(?:[ ._-]?PORN)(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
)
_ADULT_XXX_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])XXX(?![A-Za-z0-9])", re.IGNORECASE
)
_SCENE_DATE_RE = re.compile(
    r"(?<!\d)(\d{2})[._-](\d{2})[._-](\d{2})(?!\d)"
)
_CONSOLE_GAME_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:ps[2-5]|psp|psvita|nsw|switch|xbox360|xboxone|"
    r"xbox|wiiu|wii|nds|3ds)-[a-z0-9]+$"
)
_VIDEO_RELEASE_TOKEN_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:\d{3,4}[pi]|2160p|bluray|blu-ray|web-?dl|webrip|"
    r"hdtv|dvdrip|remux|uhd)(?![a-z0-9])"
)
_INDYCAR_RE = re.compile(
    r"(?<![A-Za-z0-9])INDY[ ._-]?CAR(?![A-Za-z0-9])", re.IGNORECASE
)
_SPORT_ROUND_RE = re.compile(
    r"(?<![A-Za-z0-9])ROUND[ ._-]?(?:0?[1-9]|[1-9]\d)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MLS_LEAGUES_CUP_RE = re.compile(
    r"(?<![A-Za-z0-9])MLS[ ._-]+LEAGUES[ ._-]+CUP(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_VERSUS_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])VS(?![A-Za-z0-9])", re.IGNORECASE
)


class TechnicalDecision(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    INDETERMINATE = "INDETERMINATE"


_MEDIA_EXTENSION_RE = re.compile(
    r"\.(?:mkv|mp4|avi|m2ts|mts|ts|wmv|mov|iso)$", re.IGNORECASE
)
_PAYLOAD_EXTENSION_RE = re.compile(
    r"\.(?:"
    r"mkv|mp4|avi|m2ts|mts|ts|wmv|mov|iso|"
    r"mka|m4a|ac3|eac3|dts|dtshd|thd|truehd|flac|aac|wav|mp3|opus|ogg|"
    r"sup|mks|srt|ass|ssa|sub|idx|vtt"
    r")$",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_EPISODE_RE = re.compile(
    r"(?<![A-Z0-9])S(\d{1,2})E(\d{1,3})(?!\d)", re.IGNORECASE
)
_SEASON_RE = re.compile(
    r"(?<![A-Z0-9])S(\d{1,2})(?!\d|E\d)", re.IGNORECASE
)
_TECH_TOKEN_RE = re.compile(
    r"\b(?:"
    r"(?:2160|1080|720|576|480)[pi]|"
    r"UHD|Blu[ .-]?Ray|BDRip|BDMV|REMUX|"
    r"WEB[ .-]?DL|WEBRip|HDTV|"
    r"H[ ._-]?26[45]|x26[45]|HEVC|AVC|VC[ ._-]?1|AV1|MPEG[ ._-]?2|"
    r"DDP|DD\+|DTS|TRUEHD|AAC|FLAC|REPACK|PROPER"
    r")\b",
    re.IGNORECASE,
)
_EPISODE_TITLE_END_RE = re.compile(
    r"(?ix)(?:^|\s)(?:"
    r"(?:19|20)\d{2}|"
    r"(?:2160|1080|720|576|480)[pi]|"
    r"(?:NF|NETFLIX|AMZN|HULU|PCOK|DSNP|DISNEY|MAX|HMAX|ATVP|"
    r"CRAV|CRAVE|STAN|PMTP|ITUNES|MA)|"
    r"(?:UHD|BLU[ .-]?RAY|BDRIP|BDMV|REMUX|WEB[ .-]?DL|WEBRIP|HDTV)|"
    r"(?:H[ ._-]?26[45]|X26[45]|HEVC|AVC|VC[ ._-]?1|AV1|MPEG[ ._-]?2)|"
    r"(?:DDP\d*|DD\+|EAC3|AC3|DTS(?:HD)?|TRUEHD|AAC|FLAC|LPCM)|"
    r"(?:DUAL|MULTI|SUBBED|DUBBED|HDR|HDR10\+?|DV|DOVI|REPACK|PROPER)"
    r")(?=\s|$)"
)


# Disc-region markers are deliberately an allow-list.  Treating every short
# upper-case word as a region turns ordinary title words into false evidence.
_DISC_REGION_ALIASES = {
    "CEE": "CEE",
    "EUR": "EUR",
    "EU": "EUR",
    "USA": "USA",
    "US": "USA",
    "CAN": "CAN",
    "HKG": "HKG",
    "HK": "HKG",
    "GBR": "GBR",
    "UK": "GBR",
    "AUS": "AUS",
    "FRA": "FRA",
    "FR": "FRA",
    "GER": "GER",
    "DEU": "GER",
    "JPN": "JPN",
    "JP": "JPN",
    "ITA": "ITA",
    "ESP": "ESP",
    "NLD": "NLD",
    "POL": "POL",
    "CZE": "CZE",
    "HUN": "HUN",
    "KOR": "KOR",
    "TWN": "TWN",
    "NOR": "NOR",
    "SWE": "SWE",
    "DNK": "DNK",
    "FIN": "FIN",
}

_DISC_EDITION_PATTERNS = {
    "remastered": r"\bremaster(?:ed)?\b",
    "restored": r"\b(?:restored|restoration)\b",
    "criterion": r"\bcriterion\b",
    "steelbook": r"\bsteel[ ._-]?book\b",
    "extended": r"\bextended(?:[ ._-]?(?:cut|edition))?\b",
    "theatrical": r"\btheatrical(?:[ ._-]?(?:cut|edition))?\b",
    "directors-cut": r"\bdirector(?:'|’)s[ ._-]?cut\b|\bdirectors[ ._-]?cut\b",
    "final-cut": r"\bfinal[ ._-]?cut\b",
    "unrated": r"\bunrated\b",
    "uncut": r"\buncut\b",
    "imax": r"\bimax\b",
    "open-matte": r"\bopen[ ._-]?matte\b",
    "3d": r"(?<![a-z0-9])3d(?![a-z0-9])",
    "anniversary": r"\banniversary(?:[ ._-]?edition)?\b",
    "collectors-edition": r"\bcollector(?:'|’)s[ ._-]?edition\b|\bcollectors[ ._-]?edition\b",
    "limited-edition": r"\blimited[ ._-]?edition\b",
    "special-edition": r"\bspecial[ ._-]?edition\b",
    "ultimate-edition": r"\bultimate[ ._-]?edition\b",
}

_DISC_DISTRIBUTOR_PATTERNS = {
    "88-films": r"(?<![a-z0-9])88[ ._-]?films?(?![a-z0-9])",
    "arrow": r"(?<![a-z0-9])arrow(?:[ ._-]?video)?(?![a-z0-9])",
    "bfi": r"(?<![a-z0-9])bfi(?![a-z0-9])",
    "blue-underground": r"(?<![a-z0-9])blue[ ._-]?underground(?![a-z0-9])",
    "capelight": r"(?<![a-z0-9])capelight(?![a-z0-9])",
    "criterion": r"(?<![a-z0-9])criterion(?![a-z0-9])",
    "eureka": r"(?<![a-z0-9])(?:eureka|masters[ ._-]?of[ ._-]?cinema)(?![a-z0-9])",
    "imprint": r"(?<![a-z0-9])imprint(?![a-z0-9])",
    "indicator": r"(?<![a-z0-9])(?:indicator|powerhouse)(?![a-z0-9])",
    "kino-lorber": r"(?<![a-z0-9])kino(?:[ ._-]?lorber)?(?![a-z0-9])",
    "radiance": r"(?<![a-z0-9])radiance(?![a-z0-9])",
    "scream-factory": r"(?<![a-z0-9])scream[ ._-]?factory(?![a-z0-9])",
    "second-sight": r"(?<![a-z0-9])second[ ._-]?sight(?![a-z0-9])",
    "severin": r"(?<![a-z0-9])severin(?![a-z0-9])",
    "shout-factory": r"(?<![a-z0-9])shout[ ._-]?(?:factory|select)(?![a-z0-9])",
    "studiocanal": r"(?<![a-z0-9])studio[ ._-]?canal(?![a-z0-9])",
    "synapse": r"(?<![a-z0-9])synapse(?![a-z0-9])",
    "umbrella": r"(?<![a-z0-9])umbrella(?![a-z0-9])",
    "vinegar-syndrome": r"(?<![a-z0-9])vinegar[ ._-]?syndrome(?![a-z0-9])",
    "warner-archive": r"(?<![a-z0-9])warner[ ._-]?archive(?![a-z0-9])",
}

_DISC_MANIFEST_VERSION = "path-size-v2"
_AITHER_RELEASE_TYPE_BY_ID = {
    1: "Full Disc",
    2: "Remux",
    3: "Encode",
    4: "WEB-DL",
    5: "WEBRip",
    6: "HDTV",
}
_MIB = 1024 * 1024
_MANIFEST_JUNK_NAMES = {
    ".ds_store",
    "desktop.ini",
    "thumbs.db",
}

_ManifestEvidence = tuple[
    str,
    str | None,
    int | None,
    int | None,
    int | None,
    str | None,
]


def _strip_media_extension(name: str) -> str:
    return _MEDIA_EXTENSION_RE.sub("", name.strip())


def norm_name(value: str) -> str:
    """Return the legacy-compatible persistent seen key for a release name."""

    # Keep this extension list byte-for-byte compatible with the original state
    # key algorithm.  Parsing supports more formats, but changing this key would
    # make already-closed releases searchable again.
    normalized = str(value).casefold()
    normalized = re.sub(r"\.(?:mkv|mp4|avi|m2ts|ts)$", "", normalized)
    return re.sub(r"[.\-_\s]+", "", normalized)


def canonical_title(value: str) -> str:
    """Normalize a title without stripping article-like prefixes from words.

    Articles are removed only when they are complete leading words, so ``Angel``
    remains ``angel`` while ``An Angel`` also canonicalizes to ``angel``.
    """

    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ").replace("_", " ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"^(?:the|a|an|le|la|les|el)\s+", "", text)
    return "".join(ch for ch in text if ch.isalnum())


def _clean_release_name(name: str) -> tuple[str, bool]:
    base = os.path.basename(str(name).strip())
    is_iso = base.casefold().endswith(".iso")
    base = _strip_media_extension(base)

    # This replacement must happen before dots become spaces.
    # Widow.s.Bay -> Widow's Bay; Tyler.Perry.s.Show -> Tyler Perry's Show.
    base = re.sub(r"(?<=\w)\.s\.(?=\w)", "'s ", base, flags=re.IGNORECASE)
    base = re.sub(r"[._]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base, is_iso


def _detect_resolution(text: str) -> str | None:
    match = re.search(
        r"(?<!\d)((?:2160|1080|720|576|480)[pi])(?!\d)",
        text,
        re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


def _detect_codec(text: str) -> str | None:
    if re.search(r"(?i)(?<![a-z0-9])(?:h[\s._-]?265|x265|hevc)(?!\d)", text):
        return "H265"
    if re.search(r"(?i)(?<![a-z0-9])(?:h[\s._-]?264|x264|avc)(?!\d)", text):
        return "H264"
    if re.search(r"(?i)(?<![a-z0-9])vc[\s._-]?1(?!\d)", text):
        return "VC1"
    if re.search(r"(?i)(?<![a-z0-9])av1(?!\d)", text):
        return "AV1"
    if re.search(r"(?i)(?<![a-z0-9])mpeg[\s._-]?2(?!\d)", text):
        return "MPEG2"
    return None


def _detect_medium_and_type(
    text: str,
    *,
    resolution: str | None,
    codec: str | None,
    is_iso: bool = False,
) -> tuple[str | None, str | None, str | None]:
    lowered = text.casefold()
    has_bluray = bool(
        re.search(
            r"(?i)\b(?:blu[\s.-]?ray|bdrip|bdremux|bdmv|(?:uhd[\s._-]?)?bd)\b",
            text,
        )
    )
    is_uhd_bluray = has_bluray and (
        bool(re.search(r"(?i)\buhd\b", text)) or resolution == "2160p"
    )

    medium: str | None = None
    if has_bluray:
        medium = "UHD BluRay" if is_uhd_bluray else "BluRay"
    elif re.search(r"(?i)\bweb(?:[\s.-]?dl|rip)?\b", text):
        medium = "WEB"
    elif re.search(r"(?i)\bhdtv\b", text):
        medium = "HDTV"
    elif re.search(r"(?i)\bdvd(?:[ ._-]?[59])?\b", text):
        medium = "DVD"

    native_bluray_codec = has_bluray and not bool(
        re.search(r"(?i)\bbd[ ._-]?rip\b", text)
    ) and bool(
        re.search(r"(?i)(?<![a-z0-9])(?:AVC|HEVC|VC[ ._-]?1|MPEG[ ._-]?2)(?![a-z0-9])", text)
    ) and not bool(re.search(r"(?i)(?<![a-z0-9])x26[45](?![a-z0-9])", text))
    full_disc = is_iso or native_bluray_codec or bool(
        re.search(
            r"(?i)\b(?:bdmv|video[ ._-]?ts|dvd[ ._-]?[59]|full[ ._-]?disc|complete[ ._-]?(?:uhd[ ._-]?)?blu[ ._-]?ray|(?:uhd[ ._-]?)?bd)\b",
            text,
        )
    )

    release_type: str | None
    source: str | None
    if re.search(r"(?i)\bremux\b", text):
        release_type = "Remux"
        source = f"{medium} REMUX" if medium else "Remux"
    elif full_disc:
        release_type = "Full Disc"
        source = medium or "Full Disc"
    elif re.search(r"(?i)\bweb[ ._-]?dl\b", text):
        release_type = "WEB-DL"
        source = "WEB-DL"
    elif re.search(r"(?i)\bwebrip\b", text):
        release_type = "WEBRip"
        source = "WEBRip"
    elif medium == "WEB" and codec in {"H264", "H265"}:
        # Scene names commonly use WEB.H264/H265 without spelling out WEB-DL.
        # Classifying these as generic encodes applies the wrong Aither type
        # filter and creates a false NOT_FOUND result.
        release_type = "WEB-DL"
        source = "WEB-DL"
    elif re.search(r"(?i)\bhdtv\b", text):
        release_type = "HDTV"
        source = "HDTV"
    elif codec is not None:
        release_type = "Encode"
        source = medium
    else:
        release_type = None
        source = medium

    return medium, release_type, source


def _disc_regions(value: Any) -> tuple[str, ...]:
    """Extract only explicit, allow-listed disc-region/country markers."""

    text = str(value or "")
    tokens = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{2,3}(?![A-Za-z0-9])", text)
    regions = {
        normalized
        for token in tokens
        # Region markers in release names are conventionally upper-case.  The
        # case requirement prevents titles such as "Us" and ordinary words
        # such as "Can" from becoming fake region evidence.
        if token == token.upper()
        if (normalized := _DISC_REGION_ALIASES.get(token)) is not None
    }
    return tuple(sorted(regions))


def _disc_editions(value: Any) -> tuple[str, ...]:
    text = str(value or "")
    return tuple(
        sorted(
            key
            for key, pattern in _DISC_EDITION_PATTERNS.items()
            if re.search(pattern, text, re.IGNORECASE)
        )
    )


def _file_name(item: Mapping[str, Any]) -> str:
    return str(item.get("name") or item.get("path") or "")


def _nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _file_size(item: Mapping[str, Any]) -> int | None:
    for key in ("size", "length", "bytes"):
        number = _nonnegative_int(item.get(key))
        if number is not None:
            return number
    return None


def _detect_hdr(text: str) -> tuple[str, ...]:
    """Return deterministic dynamic-range markers carried by a release name."""

    value = str(text or "")
    markers: set[str] = set()
    if re.search(
        r"(?i)(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])|\bdolby[ ._-]?vision\b",
        value,
    ):
        markers.add("DV")
    if re.search(
        r"(?i)(?<![a-z0-9])hdr[ ._-]?10(?:[ ._-]?(?:plus)|\+)(?![a-z0-9])",
        value,
    ):
        markers.add("HDR10+")
    elif re.search(r"(?i)(?<![a-z0-9])hdr[ ._-]?10(?![a-z0-9+])", value):
        markers.add("HDR10")
    if re.search(r"(?i)(?<![a-z0-9])hlg(?![a-z0-9])", value):
        markers.add("HLG")
    if re.search(r"(?i)(?<![a-z0-9])hdr(?![ ._-]?10|[a-z0-9])", value):
        markers.add("HDR")

    # MediaInfo/BDInfo commonly use labels such as ``HDR format`` before a
    # later, more specific ``HDR10+`` value.  Treating that label as an
    # additional bare-HDR variant creates a contradiction inside one report.
    # A bare marker remains useful next to DV when no static-HDR variant is
    # stated explicitly, but it is redundant beside HDR10/HDR10+/HLG.
    if markers.intersection({"HDR10+", "HDR10", "HLG"}):
        markers.discard("HDR")

    order = ("DV", "HDR10+", "HDR10", "HLG", "HDR")
    return tuple(marker for marker in order if marker in markers)


def _manifest_path(name: str, kind: str) -> str | None:
    path = re.sub(r"/+", "/", str(name).replace("\\", "/")).strip("/./")
    parts = [part for part in path.split("/") if part]
    lowered = [part.casefold() for part in parts]

    markers = {
        "BDMV": ("bdmv", "certificate"),
        "VIDEO_TS": ("video_ts", "audio_ts"),
    }.get(kind, ())
    marker_indexes = [lowered.index(marker) for marker in markers if marker in lowered]
    if marker_indexes:
        index = min(marker_indexes)
        disc_parts = lowered[index:]
        if any(
            part in _MANIFEST_JUNK_NAMES
            or part == "__macosx"
            or part.startswith("._")
            for part in disc_parts
        ):
            return None
        return "/".join(lowered[index:])
    if kind == "ISO" and path.casefold().endswith(".iso"):
        # An ISO is one opaque payload.  Its release-specific file name is not
        # part of the byte identity, so normalize it to a stable marker.
        return "disc.iso"
    return None


def _disc_file_groups(
    files: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str, tuple[Mapping[str, Any], ...]], ...]:
    """Group primary disc trees and their authored sibling trees by root."""

    described: list[tuple[Mapping[str, Any], str, list[str]]] = []
    roots: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in files:
        path = re.sub(r"/+", "/", _file_name(item).replace("\\", "/")).strip(
            "/./"
        )
        parts = [part for part in path.split("/") if part]
        lowered = [part.casefold() for part in parts]
        described.append((item, path, lowered))
        for marker, kind in (("bdmv", "BDMV"), ("video_ts", "VIDEO_TS")):
            if marker in lowered:
                index = lowered.index(marker)
                roots.setdefault((kind, "/".join(lowered[:index])), [])
                break
        else:
            if path.casefold().endswith(".iso"):
                roots.setdefault(("ISO", path.casefold()), [])

    for item, path, lowered in described:
        root: tuple[str, str] | None = None
        for marker, kind in (("bdmv", "BDMV"), ("video_ts", "VIDEO_TS")):
            if marker in lowered:
                index = lowered.index(marker)
                root = (kind, "/".join(lowered[:index]))
                break
        if root is None:
            for marker, kind in (("certificate", "BDMV"), ("audio_ts", "VIDEO_TS")):
                if marker in lowered:
                    index = lowered.index(marker)
                    sibling_root = (kind, "/".join(lowered[:index]))
                    if sibling_root in roots:
                        root = sibling_root
                    break
        if root is None and path.casefold().endswith(".iso"):
            root = ("ISO", path.casefold())
        if root in roots:
            roots[root].append(item)

    return tuple(
        (kind, root, tuple(members))
        for (kind, root), members in sorted(roots.items())
        if members
    )


def _manifest_evidence_from_files(
    files: Sequence[Mapping[str, Any]],
) -> tuple[_ManifestEvidence, ...]:
    evidence = []
    for kind, _, members in _disc_file_groups(files):
        fingerprint, count, payload_size, main_size = _manifest_details(members, kind)
        if fingerprint:
            evidence.append(
                (
                    fingerprint,
                    _DISC_MANIFEST_VERSION,
                    count,
                    payload_size,
                    main_size,
                    kind,
                )
            )
    return tuple(evidence)


def _manifest_details(
    files: Sequence[Mapping[str, Any]], kind: str
) -> tuple[str | None, int, int | None, int | None]:
    entries: list[tuple[str, int]] = []
    for item in files:
        name = _file_name(item)
        normalized = _manifest_path(name, kind)
        size = _file_size(item)
        if normalized is not None and size is not None:
            entries.append((normalized, size))

    if not entries:
        return None, 0, None, None

    entries.sort()
    digest = hashlib.sha256()
    digest.update(f"{_DISC_MANIFEST_VERSION}\0{kind}\0".encode("ascii"))
    for path, size in entries:
        digest.update(path.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")

    total_size = sum(size for _, size in entries)
    main_size = max(size for _, size in entries)
    return digest.hexdigest(), len(entries), total_size, main_size


_STREAM_CORE_DIRECTORIES = frozenset({"stream", "playlist", "clipinf"})


def _stream_core_fingerprint(
    files: Sequence[Mapping[str, Any]],
) -> tuple[str, int] | None:
    """Fingerprint a Blu-ray's A/V core: STREAM, PLAYLIST and CLIPINF paths+sizes.

    Two pressings that differ only in Java menus, metadata, certificates or
    the BACKUP mirror share this value.  It is evidence of identical main
    content, never of a duplicate on its own.
    """

    entries: list[tuple[str, int]] = []
    has_stream = False
    for item in files:
        path = _manifest_path(_file_name(item), "BDMV")
        size = _file_size(item)
        if path is None or size is None:
            continue
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "bdmv" or parts[1] not in _STREAM_CORE_DIRECTORIES:
            continue
        has_stream |= parts[1] == "stream"
        entries.append((path, size))
    if not has_stream:
        return None
    entries.sort()
    digest = hashlib.sha256()
    digest.update(b"stream-core-v1\0")
    for path, size in entries:
        digest.update(path.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), len(entries)


def _single_bdmv_stream_core(
    files: Sequence[Mapping[str, Any]],
) -> tuple[str, int] | None:
    groups = [group for group in _disc_file_groups(files) if group[0] == "BDMV"]
    if len(groups) != 1:
        return None
    return _stream_core_fingerprint(groups[0][2])


def _candidate_stream_cores(attrs: Mapping[str, Any]) -> frozenset[str]:
    cores: set[str] = set()
    for kind, _, members in _disc_file_groups(_candidate_file_sequence(attrs)):
        if kind != "BDMV":
            continue
        core = _stream_core_fingerprint(members)
        if core is not None:
            cores.add(core[0])
    return frozenset(cores)


def _is_sample_file(name: str) -> bool:
    """Scene Sample/ folders and *-sample.* clips are not upload payload."""

    path = name.replace("\\", "/").casefold()
    parts = path.split("/")
    if any(part == "sample" or part == "samples" for part in parts[:-1]):
        return True
    return bool(re.search(r"(?:^|[._ -])sample\.[a-z0-9]{2,4}$", parts[-1]))


def enrich_release(
    parsed: Mapping[str, Any],
    name: str,
    torrent: Mapping[str, Any] | None,
    files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Enrich a name parse with qBittorrent's authoritative file manifest.

    A directory named ``BDMV``/``VIDEO_TS`` or an ISO payload is stronger
    evidence than scene-name heuristics.  The returned mapping is a copy; no
    caller-owned object is mutated.
    """

    result = dict(parsed)
    result["name"] = str(name)
    torrent = torrent or {}
    file_names = [_file_name(item) for item in files]
    lowered = [item.replace("\\", "/").casefold() for item in file_names]

    kind: str | None = None
    if any(re.search(r"(?:^|/)bdmv(?:/|$)", item) for item in lowered):
        kind = "BDMV"
    elif any(re.search(r"(?:^|/)video_ts(?:/|$)", item) for item in lowered):
        kind = "VIDEO_TS"
    elif any(item.endswith(".iso") for item in lowered):
        kind = "ISO"

    # Name-only parsing still benefits callers which have not fetched a file
    # list yet.  The manifest, when available, always wins.
    reparsed = parse_release(name)
    for key in ("resolution", "medium", "release_type", "source", "codec"):
        if result.get(key) is None and reparsed.get(key) is not None:
            result[key] = reparsed[key]

    if kind is not None:
        result["release_type"] = "Full Disc"
        result["disc_manifest_kind"] = kind
        if kind == "BDMV":
            if re.search(
                r"(?i)(?:^|[^a-z0-9])uhd(?:$|[^a-z0-9])"
                r"|(?<![A-Za-z0-9])UHD[ ._-]?BD(?![A-Za-z0-9])",
                name,
            ):
                result["medium"] = "UHD BluRay"
                result["resolution"] = result.get("resolution") or "2160p"
            else:
                result["medium"] = "BluRay"
            result["source"] = result["medium"]
        elif kind == "VIDEO_TS":
            result["medium"] = "DVD"
            result["source"] = "DVD"
        else:
            # ISO by itself establishes Full Disc, but not whether the opaque
            # image is Blu-ray or DVD.  Preserve trustworthy name evidence.
            result["source"] = result.get("medium") or "Full Disc"

        manifest_evidence = _manifest_evidence_from_files(files)
        if manifest_evidence:
            result["disc_manifest_evidence"] = tuple(
                {
                    "fingerprint": fingerprint,
                    "version": version,
                    "file_count": count,
                    "payload_size": payload_size,
                    "main_file_size": main_size,
                    "kind": manifest_kind,
                }
                for fingerprint, version, count, payload_size, main_size, manifest_kind in manifest_evidence
            )
            result["disc_file_count"] = sum(
                count or 0 for _, _, count, _, _, _ in manifest_evidence
            )
            result["disc_payload_size"] = sum(
                payload_size or 0 for _, _, _, payload_size, _, _ in manifest_evidence
            )
            result["disc_main_file_size"] = max(
                main_size or 0 for _, _, _, _, main_size, _ in manifest_evidence
            )
            if len(manifest_evidence) == 1:
                fingerprint, version, _, _, _, _ = manifest_evidence[0]
                result["disc_manifest_fingerprint"] = fingerprint
                result["disc_manifest_version"] = version
            else:
                result.pop("disc_manifest_fingerprint", None)
                result.pop("disc_manifest_version", None)
            stream_core = _single_bdmv_stream_core(files) if kind == "BDMV" else None
            if stream_core is not None:
                result["disc_stream_core"] = stream_core[0]
                result["disc_stream_core_count"] = stream_core[1]
            else:
                result.pop("disc_stream_core", None)
                result.pop("disc_stream_core_count", None)

    file_total = sum(
        size for item in files if (size := _file_size(item)) is not None
    )
    media_payload_signature = sorted(
        (
            match.group(0).lstrip(".").casefold(),
            size,
        )
        for item in files
        if not _is_sample_file(_file_name(item))
        if (match := _PAYLOAD_EXTENSION_RE.search(_file_name(item)))
        if (size := _file_size(item)) is not None
    )
    media_sizes = sorted(size for _, size in media_payload_signature)
    if kind is None and media_sizes:
        # Keep the actual upload payload separate from NFO/SFV artwork.  UNIT3D's
        # torrent size includes those sidecars, while a cached candidate may
        # expose only its media file list.  Exact payload identity is therefore
        # stronger and less fragile than comparing aggregate torrent sizes.
        result["media_payload_size"] = sum(media_sizes)
        result["media_main_file_size"] = max(media_sizes)
        result["media_file_count"] = len(media_sizes)
        result["media_file_sizes"] = tuple(sorted(media_sizes))
        result["media_payload_signature"] = tuple(media_payload_signature)
    torrent_total = None
    for key in ("total_size", "size"):
        torrent_total = _nonnegative_int(torrent.get(key))
        if torrent_total is not None:
            break
    if file_total > 0:
        result["disc_total_size"] = file_total
    elif torrent_total and torrent_total > 0:
        result["disc_total_size"] = torrent_total
    if files:
        result["torrent_file_count"] = len(files)

    regions = _disc_regions(name)
    editions = _disc_editions(name)
    if regions:
        result["disc_regions"] = regions
    if editions:
        result["disc_editions"] = editions
    return result


def _select_release_year(text: str) -> re.Match[str] | None:
    matches = list(_YEAR_RE.finditer(text))
    if not matches:
        return None

    # A title made solely from a year (for example "1917") must not become an
    # empty title.  When there are two year-like tokens, the rightmost one is
    # the release year: 1917.2019 and 2001.A.Space.Odyssey.1968.
    if len(matches) == 1 and matches[0].start() == 0:
        return None
    return matches[-1]


def _episode_information(
    text: str,
) -> tuple[int | None, tuple[int, ...], str, int | None, int | None]:
    episode_match = _EPISODE_RE.search(text)
    if episode_match:
        season = int(episode_match.group(1))
        first_episode = int(episode_match.group(2))
        episodes = [first_episode]
        tail = text[episode_match.end():]
        scope_end = episode_match.end()

        # S01E01-E03 is a range; S01E01E03 is an explicit multi-episode set.
        range_match = re.match(r"\s*-\s*E(\d{1,3})(?!\d)", tail, re.IGNORECASE)
        if range_match:
            last_episode = int(range_match.group(1))
            scope_end = episode_match.end() + range_match.end()
            if last_episode >= first_episode:
                episodes.extend(range(first_episode + 1, last_episode + 1))
        else:
            for extra in re.finditer(r"\s*(?:[-.]?\s*E)(\d{1,3})(?!\d)", tail, re.IGNORECASE):
                number = int(extra.group(1))
                if number not in episodes:
                    episodes.append(number)
                scope_end = max(scope_end, episode_match.end() + extra.end())

        scope = "episode" if len(episodes) == 1 else "multi_episode"
        return season, tuple(episodes), scope, episode_match.start(), scope_end

    season_match = _SEASON_RE.search(text)
    if season_match:
        return (
            int(season_match.group(1)),
            (),
            "season_pack",
            season_match.start(),
            season_match.end(),
        )

    return None, (), "movie", None, None


def parse_release(name: str) -> dict[str, Any]:
    """Parse a release name into an identity and technical signature."""

    cleaned, is_iso = _clean_release_name(name)
    resolution = _detect_resolution(cleaned)
    codec = _detect_codec(str(name)) or _detect_codec(cleaned)
    hdr = _detect_hdr(str(name))
    medium, release_type, source = _detect_medium_and_type(
        cleaned,
        resolution=resolution,
        codec=codec,
        is_iso=is_iso,
    )
    season, episodes, scope, scope_start, scope_end = _episode_information(cleaned)
    year_match = _select_release_year(cleaned)

    title_end = len(cleaned)
    if scope_start is not None:
        title_end = min(title_end, scope_start)
    if year_match is not None and year_match.start() < title_end:
        title_end = year_match.start()
    if scope_start is None and year_match is None:
        technical_match = _TECH_TOKEN_RE.search(cleaned)
        if technical_match:
            title_end = technical_match.start()

    title = re.sub(r"\s+", " ", cleaned[:title_end]).strip(" ._-")
    year = int(year_match.group(1)) if year_match is not None else None
    episode = episodes[0] if episodes else None
    episode_title: str | None = None
    if scope == "episode" and scope_end is not None:
        episode_tail = cleaned[scope_end:]
        attached_group = episode_tail.startswith("-")
        technical_match = _EPISODE_TITLE_END_RE.search(episode_tail)
        if technical_match is not None:
            episode_tail = episode_tail[: technical_match.start()]
        candidate_title = re.sub(r"\s+", " ", episode_tail).strip(" ._-")
        if not attached_group and canonical_title(candidate_title):
            episode_title = candidate_title

    return {
        "name": str(name),
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "episodes": episodes,
        "episode_title": episode_title,
        "scope": scope,
        "resolution": resolution,
        "medium": medium,
        "release_type": release_type,
        "source": source,
        "codec": codec,
        "hdr": hdr,
        "media_type": "tv" if scope != "movie" else "movie",
    }


def extract_release_group(name: str) -> str:
    base = _strip_media_extension(os.path.basename(str(name).strip()))

    if "-" in base:
        candidate = base.rsplit("-", 1)[1].strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\[\] ]*", candidate):
            return candidate

    bracketed = re.search(r"\[([^\[\]]+)\]$", base)
    if bracketed:
        return bracketed.group(1).strip()
    return ""


def release_type_for_blacklist(parsed: Mapping[str, Any], name: str = "") -> str:
    release_type = _normalize_release_type(parsed.get("release_type"))
    if release_type:
        return release_type

    source = str(parsed.get("source") or "")
    normalized_source = _normalize_release_type(source)
    if normalized_source:
        return normalized_source

    lowered = name.casefold()
    if "remux" in lowered:
        return "Remux"
    if _detect_codec(name):
        return "Encode"
    return ""


def _has_valid_scene_date(name: str) -> bool:
    for match in _SCENE_DATE_RE.finditer(name):
        year = 2000 + int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        if not 1 <= month <= 12:
            continue
        month_days = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        max_day = month_days[month - 1]
        if month == 2 and (
            year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)
        ):
            max_day = 29
        if 1 <= day <= max_day:
            return True
    return False


def _content_blacklist_rule(name: str) -> str:
    for site, pattern in _ADULT_SITE_PATTERNS:
        if pattern.search(name):
            return f"ADULT_SITE:{site}"

    if _ADULT_XXX_TOKEN_RE.search(name) and _has_valid_scene_date(name):
        return "ADULT_SCENE:XXX_DATE"

    if _INDYCAR_RE.search(name) and _SPORT_ROUND_RE.search(name):
        return "SPORT:INDYCAR_ROUND"

    stem = _strip_media_extension(name)
    if _CONSOLE_GAME_RE.search(stem) and not _VIDEO_RELEASE_TOKEN_RE.search(stem):
        return "GAME:CONSOLE_SCENE"

    if _MLS_LEAGUES_CUP_RE.search(name) and _VERSUS_TOKEN_RE.search(name):
        return "SPORT:MLS_LEAGUES_CUP_VS"

    return ""


def is_blacklisted_release(
    name: str, parsed: Mapping[str, Any] | None = None
) -> tuple[bool, str, str, str]:
    parsed = parsed or parse_release(name)
    group = extract_release_group(name)
    group_key = group.casefold()
    release_type = release_type_for_blacklist(parsed, name)

    content_rule = _content_blacklist_rule(name)
    if content_rule:
        return True, group, content_rule, release_type

    if not group:
        return False, "", "", release_type
    if group_key in BLACKLIST_ALL:
        return True, group, "ALL", release_type
    if group_key in BLACKLIST_NO_TYPE_GIVEN:
        return True, group, "ALL/no-type-given", release_type

    forbidden = BLACKLIST_BY_TYPE.get(group_key)
    if forbidden and release_type in forbidden:
        return True, group, ",".join(sorted(forbidden)), release_type
    return False, group, "", release_type


def _normalize_resolution(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(
        r"(?i)\b((?:2160|1080|720|576|480)[pi])\b", str(value)
    )
    return match.group(1).lower() if match else None


def _normalize_codec(value: Any) -> str | None:
    if value is None:
        return None
    upper = str(value).upper().replace(".", "").replace("-", "")
    aliases = {
        "H265": "H265",
        "X265": "H265",
        "HEVC": "H265",
        "H264": "H264",
        "X264": "H264",
        "AVC": "H264",
        "VC1": "VC1",
        "AV1": "AV1",
        "MPEG2": "MPEG2",
    }
    return aliases.get(upper) or _detect_codec(str(value))


def _normalize_hdr_tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return _detect_hdr(value)
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        text = " ".join(str(item) for item in value)
        return _detect_hdr(text)
    return _detect_hdr(str(value))


def _explicit_sdr(value: Any) -> bool:
    """Return whether *value* explicitly states standard dynamic range.

    An empty parser result means that the release name did not advertise its
    dynamic range; it is not evidence that the payload is SDR.  Keeping that
    distinction is important for byte-identical releases whose uploader only
    added the HDR label on one side.
    """

    return bool(
        re.search(
            r"(?i)(?<![a-z0-9])(?:sdr|no[ ._-]?hdr)(?![a-z0-9])"
            r"|^\s*(?:none|false|0)\s*$",
            str(value or ""),
        )
    )


def _hdr_evidence(
    parsed: Mapping[str, Any], name: str = ""
) -> tuple[tuple[str, ...], bool]:
    technical_name = str(name or parsed.get("name") or "")
    structured = parsed.get("hdr") if "hdr" in parsed else None
    structured_tokens = _normalize_hdr_tokens(structured)
    name_tokens = _detect_hdr(technical_name)
    tokens = structured_tokens or name_tokens
    known = bool(
        tokens
        or _explicit_sdr(structured)
        or _explicit_sdr(technical_name)
    )
    return tokens, known


def _hdr_tokens_compatible(local: tuple[str, ...], remote: tuple[str, ...]) -> bool:
    if local == remote:
        return True

    local_set = set(local)
    remote_set = set(remote)
    # Dolby Vision release names inconsistently spell out their HDR fallback.
    # Likewise, a bare HDR marker is less specific than HDR10/HDR10+.  These
    # omissions may remove a veto, but never prove a match by themselves.
    static_hdr = {"HDR", "HDR10", "HDR10+"}
    if "DV" in local_set and "DV" in remote_set:
        local_fallback = local_set - {"DV"}
        remote_fallback = remote_set - {"DV"}
        if not local_fallback or not remote_fallback:
            return True
        if local_fallback == remote_fallback:
            return True
        if local_fallback == {"HDR"} and remote_fallback <= static_hdr:
            return True
        if remote_fallback == {"HDR"} and local_fallback <= static_hdr:
            return True
        return False
    if local_set == {"HDR"} and remote_set and remote_set <= static_hdr:
        return True
    if remote_set == {"HDR"} and local_set and local_set <= static_hdr:
        return True
    return False


def _merge_remote_hdr_evidence(
    attrs: Mapping[str, Any],
    remote_parsed: Mapping[str, Any],
    remote_name: str,
) -> tuple[tuple[str, ...], bool, bool]:
    """Resolve name/structured/report HDR as ``(tokens, known, conflict)``."""

    name_tokens, name_known = _hdr_evidence(remote_parsed, remote_name)
    attrs_present = "hdr" in attrs and attrs.get("hdr") is not None
    attrs_tokens = _normalize_hdr_tokens(attrs.get("hdr")) if attrs_present else ()
    sources: list[tuple[tuple[str, ...], bool]] = [
        (name_tokens, bool(name_tokens or _explicit_sdr(remote_name))),
        (
            attrs_tokens,
            bool(
                attrs_tokens
                or (attrs_present and _explicit_sdr(attrs.get("hdr")))
            ),
        ),
    ]
    for key in ("media_info", "bd_info"):
        report = str(attrs.get(key) or "")
        tokens = _detect_hdr(report)
        sources.append((tokens, bool(tokens or _explicit_sdr(report))))

    explicit_sources = [tokens for tokens, explicit in sources if explicit]
    for index, first in enumerate(explicit_sources):
        for second in explicit_sources[index + 1 :]:
            if not _hdr_tokens_compatible(first, second):
                return (), True, True

    nonempty = [tokens for tokens in explicit_sources if tokens]
    if nonempty:
        rank = {"HDR": 0, "HDR10": 1, "HDR10+": 2, "HLG": 3, "DV": 4}
        strongest = max(
            nonempty,
            key=lambda tokens: (
                len(tokens),
                sum(rank.get(token, 0) for token in tokens),
            ),
        )
        return strongest, True, False
    return (), bool(name_known or explicit_sources), False


def _audio_evidence(value: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    text = str(value or "")
    formats: set[str] = set()
    token_end = (
        r"(?=(?:1\.0|2\.0|5\.1(?:\.[24])?|7\.1(?:\.[246])?)"
        r"(?:[^0-9]|$)|[^a-z0-9]|$)"
    )
    patterns = (
        ("TRUEHD", r"(?i)(?<![a-z0-9])true[ ._-]?hd" + token_end),
        (
            "DTS-HD",
            r"(?i)(?<![a-z0-9])dts[ ._-]?hd(?:[ ._-]?ma)?" + token_end,
        ),
        ("DDP", r"(?i)(?<![a-z0-9])(?:ddp|dd\+|eac3|e-ac-3)" + token_end),
        # ``DD+`` is Dolby Digital Plus (E-AC-3), not a simultaneous AC-3
        # declaration.  Letting the bare ``DD`` alternative consume the plus
        # sign fabricated a two-format conflict and rejected byte-identical
        # Aither releases named with the common ``DD+ 5.1`` spelling.
        ("AC3", r"(?i)(?<![a-z0-9])(?:ac3|ac-3|dd(?!\+))" + token_end),
        ("AAC", r"(?i)(?<![a-z0-9])aac" + token_end),
        ("FLAC", r"(?i)(?<![a-z0-9])flac" + token_end),
        ("LPCM", r"(?i)(?<![a-z0-9])(?:lpcm|pcm)" + token_end),
        ("OPUS", r"(?i)(?<![a-z0-9])opus" + token_end),
        ("DTS", r"(?i)(?<![a-z0-9])dts(?![ ._-]?hd)" + token_end),
    )
    for label, pattern in patterns:
        if re.search(pattern, text):
            formats.add(label)
    channels = {
        match.group(1)
        for match in re.finditer(
            r"(?<!\d)(1\.0|2\.0|5\.1(?:\.[24])?|7\.1(?:\.[246])?)(?!\d)",
            text,
        )
    }
    return tuple(sorted(formats)), tuple(sorted(channels))


def _report_audio_evidence(value: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract audio evidence without mistaking report version numbers for channels.

    Full MediaInfo text contains unrelated values such as ``mkvmerge v6.2.0``
    and ``libmatroska v1.7.1``.  Scanning the whole report made those look like
    2.0/7.1 channel declarations.  Formats are self-identifying; channel-only
    evidence is accepted only on an explicit audio/channel line.
    """

    formats: set[str] = set()
    channels: set[str] = set()
    in_audio_section = False
    section_header = re.compile(
        r"(?i)^(?:general|video|audio|text|subtitle|menu)(?:\s*#\s*\d+)?$"
    )
    channel_field = re.compile(
        r"(?i)^(?:channel(?:\(s\))?|channel layout|audio channels?)\s*:"
    )
    audio_field = re.compile(r"(?i)^audio(?:\s*#\s*\d+)?\s*:")

    for raw_line in str(value or "").splitlines() or [str(value or "")]:
        line = raw_line.strip()
        if not line:
            continue
        if section_header.fullmatch(line):
            in_audio_section = bool(re.match(r"(?i)^audio", line))
            continue

        line_formats, line_channels = _audio_evidence(line)
        if line_formats:
            formats.update(line_formats)
            channels.update(line_channels)
        elif line_channels and (
            audio_field.match(line)
            or (in_audio_section and channel_field.match(line))
        ):
            channels.update(line_channels)

    return tuple(sorted(formats)), tuple(sorted(channels))


def _release_audio_evidence(
    name: Any, *reports: Any
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Combine release-name audio with independently parsed technical reports."""

    name_formats, name_channels = _audio_evidence(name)
    formats = set(name_formats)
    channels = set(name_channels)
    for report in reports:
        report_formats, report_channels = _report_audio_evidence(report)
        formats.update(report_formats)
        channels.update(report_channels)
    return tuple(sorted(formats)), tuple(sorted(channels))


def _audio_profile_evidence(
    value: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(object_audio, dts_hd_profile)`` material markers."""

    text = str(value or "")
    profile_end = (
        r"(?=(?:1\.0|2\.0|5\.1(?:\.[24])?|7\.1(?:\.[246])?)"
        r"(?:[^0-9]|$)|[^a-z0-9]|$)"
    )
    object_audio: set[str] = set()
    dts_profiles: set[str] = set()
    if re.search(
        r"(?i)(?<![a-z0-9])(?:dolby[ ._-]?)?atmos" + profile_end, text
    ):
        object_audio.add("ATMOS")
    if re.search(r"(?i)(?<![a-z0-9])joc" + profile_end, text):
        # E-AC-3 Joint Object Coding is the bitstream profile used for
        # Dolby Atmos and is materially different from plain DDP/EAC3.
        object_audio.add("ATMOS")
    if re.search(
        r"(?i)(?<![a-z0-9])dts[ ._-]?:?[ ._-]?x" + profile_end, text
    ):
        object_audio.add("DTS:X")
    if re.search(
        r"(?i)(?<![a-z0-9])dts[ ._-]?hd[ ._-]?(?:ma|master[ ._-]?audio)"
        + profile_end,
        text,
    ) or re.search(r"(?im)^\s*format[ ._-]?profile\s*:\s*ma\b", text):
        dts_profiles.add("DTS-HD-MA")
    if re.search(
        r"(?i)(?<![a-z0-9])dts[ ._-]?hd[ ._-]?(?:"
        r"hra|hr|high[ ._-]?(?:res|resolution)(?:[ ._-]?audio)?"
        r")"
        + profile_end,
        text,
    ) or re.search(r"(?im)^\s*format[ ._-]?profile\s*:\s*hra\b", text):
        dts_profiles.add("DTS-HD-HRA")
    return tuple(sorted(object_audio)), tuple(sorted(dts_profiles))


def _release_audio_profile_evidence(
    name: Any, *reports: Any
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    object_audio, dts_profiles = _audio_profile_evidence(name)
    object_set = set(object_audio)
    dts_set = set(dts_profiles)
    for report in reports:
        report_object, report_dts = _audio_profile_evidence(report)
        object_set.update(report_object)
        dts_set.update(report_dts)
    return tuple(sorted(object_set)), tuple(sorted(dts_set))


def _audio_profile_relation(
    local: tuple[tuple[str, ...], tuple[str, ...]],
    remote: tuple[tuple[str, ...], tuple[str, ...]],
    *,
    local_formats: tuple[str, ...] = (),
    remote_formats: tuple[str, ...] = (),
) -> tuple[bool, bool]:
    """Return ``(material_conflict, ambiguity)`` across profile dimensions."""

    ambiguous = False
    relevant_formats = ({"TRUEHD", "DDP", "AC3"}, {"DTS-HD"})
    common_formats = set(local_formats) & set(remote_formats)
    for index, (local_dimension, remote_dimension) in enumerate(zip(local, remote)):
        if local_dimension and remote_dimension:
            if set(local_dimension).isdisjoint(remote_dimension):
                return True, False
            if local_dimension != remote_dimension:
                ambiguous = True
        elif bool(local_dimension) != bool(remote_dimension):
            # A profile present on only one side is meaningful ambiguity only
            # when both releases explicitly identify the compatible base
            # audio family.  A completely audio-less local Full Disc name
            # must not be downgraded merely because the remote BDInfo spells
            # out one of its tracks.
            ambiguous |= bool(
                index == 0 or common_formats & relevant_formats[index]
            )
    return False, ambiguous


def _web_services(value: Any) -> tuple[str, ...]:
    text = str(value or "")
    aliases = (
        ("AMZN", r"(?i)(?<![a-z0-9])(?:amzn|amazon)(?![a-z0-9])"),
        ("NF", r"(?i)(?<![a-z0-9])(?:nf|netflix)(?![a-z0-9])"),
        ("HULU", r"(?i)(?<![a-z0-9])hulu(?![a-z0-9])"),
        ("DSNP", r"(?i)(?<![a-z0-9])(?:dsnp|disney\+?)(?![a-z0-9])"),
        ("ATVP", r"(?i)(?<![a-z0-9])(?:atvp|apple[ ._-]?tv\+?)(?![a-z0-9])"),
        ("MAX", r"(?i)(?<![a-z0-9])(?:max|hmax|hbo[ ._-]?max)(?![a-z0-9])"),
        ("PMTP", r"(?i)(?<![a-z0-9])(?:pmtp|paramount\+?)(?![a-z0-9])"),
        ("PCOK", r"(?i)(?<![a-z0-9])(?:pcok|peacock)(?![a-z0-9])"),
    )
    return tuple(sorted(label for label, pattern in aliases if re.search(pattern, text)))


def _bit_depth_markers(value: Any) -> tuple[str, ...]:
    text = str(value or "")
    markers: set[str] = set()
    if re.search(r"(?i)(?<![a-z0-9])(?:10[ ._-]?bits?|hi10p)(?![a-z0-9])", text):
        markers.add("10bit")
    if re.search(r"(?i)(?<![a-z0-9])8[ ._-]?bits?(?![a-z0-9])", text):
        markers.add("8bit")
    return tuple(sorted(markers))


def _language_markers(value: Any) -> tuple[str, ...]:
    text = str(value or "")
    return tuple(
        label
        for label in ("DUAL", "MULTI")
        if re.search(rf"(?i)(?<![a-z0-9]){label}(?![a-z0-9])", text)
    )


def _non_disc_named_evidence_state(local_name: str, remote_name: str) -> tuple[bool, bool]:
    """Return ``(two_sided_conflict, one_sided_missing)`` for name evidence."""

    local_formats, local_channels = _audio_evidence(local_name)
    remote_formats, remote_channels = _audio_evidence(remote_name)
    pairs = (
        (local_formats, remote_formats),
        (local_channels, remote_channels),
        (_web_services(local_name), _web_services(remote_name)),
        (_bit_depth_markers(local_name), _bit_depth_markers(remote_name)),
        (_language_markers(local_name), _language_markers(remote_name)),
    )
    conflict = any(local and remote and local != remote for local, remote in pairs)
    missing = any(bool(local) != bool(remote) for local, remote in pairs)
    return conflict, missing


def _normalize_release_type(value: Any) -> str | None:
    if value is None:
        return None
    compact = re.sub(r"[\s._-]+", "", str(value)).casefold()
    if "remux" in compact:
        return "Remux"
    if compact in {"encode", "encoded"}:
        return "Encode"
    if compact in {"webdl", "webdownload"}:
        return "WEB-DL"
    if compact == "webrip":
        return "WEBRip"
    if compact == "hdtv":
        return "HDTV"
    if compact in {"fulldisc", "disc", "bdmv"}:
        return "Full Disc"
    return None


def _candidate_attributes(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    attributes = candidate.get("attributes")
    return attributes if isinstance(attributes, Mapping) else candidate


def _first_attribute(attrs: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in attrs and attrs.get(key) is not None:
            return attrs.get(key)
    return None


def _episode_numbers(value: Any) -> tuple[int, ...]:
    if value is None or isinstance(value, bool):
        return ()
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        numbers: set[int] = set()
        for item in value:
            numbers.update(_episode_numbers(item))
        return tuple(sorted(numbers))
    if isinstance(value, (int, float)):
        number = int(value)
        return (number,) if number >= 0 else ()

    text = str(value).strip()
    range_match = re.fullmatch(
        r"(?i)(?:E)?(\d{1,3})\s*[-:]\s*(?:E)?(\d{1,3})", text
    )
    if range_match:
        first, last = map(int, range_match.groups())
        return tuple(range(first, last + 1)) if last >= first else ()
    values = {
        int(match.group(1))
        for match in re.finditer(r"(?i)(?:^|[^0-9])(?:E)?(\d{1,3})(?=$|[^0-9])", text)
    }
    return tuple(sorted(values))


def _merge_remote_episode_evidence(
    attrs: Mapping[str, Any], remote_parsed: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Merge UNIT3D season/episode fields without erasing a name range."""

    resolved = dict(remote_parsed)
    raw_season = _first_attribute(attrs, "season_number", "seasonNumber")
    attrs_season = (
        None
        if raw_season is None or isinstance(raw_season, bool)
        else _nonnegative_int(raw_season)
    )
    attrs_episodes = _episode_numbers(
        _first_attribute(attrs, "episode_number", "episodeNumber")
    )
    name_season = _nonnegative_int(remote_parsed.get("season"))
    name_episodes = tuple(
        number
        for number in (_nonnegative_int(item) for item in remote_parsed.get("episodes") or ())
        if number is not None
    )
    name_scope = remote_parsed.get("scope")

    if attrs_season is not None and name_season is not None and attrs_season != name_season:
        return resolved, True
    if attrs_episodes and name_episodes:
        if not set(attrs_episodes).issubset(name_episodes):
            return resolved, True
    elif attrs_episodes and name_scope == "season_pack":
        return resolved, True

    if name_season is None and attrs_season is not None:
        resolved["season"] = attrs_season
    if not name_episodes and attrs_episodes:
        resolved["episodes"] = attrs_episodes
        resolved["episode"] = attrs_episodes[0]
        resolved["scope"] = "episode" if len(attrs_episodes) == 1 else "multi_episode"
        resolved["media_type"] = "tv"
    elif not name_episodes and name_scope == "movie" and attrs_season is not None:
        resolved["scope"] = "season_pack"
        resolved["media_type"] = "tv"
    return resolved, False


def _compare_known(local: Any, remote: Any) -> tuple[bool, bool]:
    """Return ``(mismatch, unknown)`` for one mandatory field."""

    if local is None or remote is None:
        return False, True
    return local != remote, False


def _mapping_tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = re.split(r"[,;/|]+", value)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = list(value)
    else:
        values = [value]
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def _byte_size(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None

    text = str(value).strip()
    byte_match = re.search(r"(?i)(\d[\d,._ ]*)\s*bytes?\b", text)
    if byte_match:
        try:
            number = int(re.sub(r"[^\d]", "", byte_match.group(1)))
        except ValueError:
            return None
        return number if number > 0 else None
    digits = re.fullmatch(r"[\d,._ ]+", text)
    if digits:
        try:
            number = int(re.sub(r"[^\d]", "", text))
        except ValueError:
            return None
        return number if number > 0 else None

    match = re.search(
        r"(?i)(\d+(?:[.,]\d+)?)\s*(bytes?|[KMGT]i?B)\b",
        text,
    )
    if not match:
        return None
    raw_number = match.group(1)
    unit = match.group(2).casefold()
    if unit.startswith("byte"):
        try:
            return int(raw_number.replace(",", "").replace(".", ""))
        except ValueError:
            return None
    try:
        number = float(raw_number.replace(",", "."))
    except ValueError:
        return None
    powers = {
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
    }
    multiplier = powers.get(unit)
    return int(number * multiplier) if multiplier is not None and number > 0 else None


def _bdinfo_size(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(
        r"(?im)^\s*(?:disc\s+size|size)\s*:\s*([^\r\n]+)", text
    )
    return _byte_size(match.group(1)) if match else None


def normalize_bdinfo_signature(value: Any) -> tuple[str, ...]:
    """Extract a stable, comparison-only signature from a BDInfo report.

    BDInfo appears on tracker APIs as plain text, BBCode, HTML, compact summary
    rows, or full report rows.  Subtitle scan bitrates and DialNorm values vary
    between otherwise equivalent playlist scans, so they are intentionally not
    identity evidence.  Video and audio bitrates remain significant.
    """

    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|tr|pre)\s*>", "\n", text)
    text = re.sub(r"\[[^\]\r\n]*\]", "", text)
    text = re.sub(r"<[^>]*>", "", text)

    signature: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("*• ").strip()
        lowered = " ".join(line.split()).casefold()
        has_labeled_row = bool(
            re.match(r"(?i)^(?:video|audio|subtitle)s?\s*:", line)
        )
        has_presentation_graphics = "presentation graphics" in lowered
        has_summary_row = "kbps" in lowered and bool(
            re.search(r"(?i)\b(?:video|audio)\b", line)
        )
        if not (has_labeled_row or has_presentation_graphics or has_summary_row):
            continue

        # A bare section heading carries no technical evidence.
        if re.fullmatch(r"(?i)(?:video|audio|subtitle)s?\s*:", line):
            continue

        line = re.sub(
            r"(?i)(?:\s*/\s*|\s+)?DN\s*[-−]\s*\d+(?:[.,]\d+)?\s*dB\b",
            "",
            line,
        )
        is_subtitle = has_presentation_graphics or bool(
            re.match(r"(?i)^subtitles?\s*:", line)
        )
        if is_subtitle:
            line = re.sub(r"(?i)\b\d+(?:[.,]\d+)?\s*kbps\b", "", line)

        line = re.sub(r"\(\s*\)", "", line)
        line = re.sub(r"\s*/\s*(?=/|$)", " ", line)
        line = re.sub(r"\s*:\s*", ": ", line)
        line = re.sub(r"\s*/\s*", " / ", line)
        line = " ".join(line.split()).strip(" /-")
        if line:
            signature.append(line.casefold())
    return tuple(signature)


def _candidate_file_sequence(attrs: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = attrs.get("files")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        nested = value.get("data") or value.get("files")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            return [item for item in nested if isinstance(item, Mapping)]
    return []


def _candidate_file_count(attrs: Mapping[str, Any]) -> int | None:
    files = _candidate_file_sequence(attrs)
    if files:
        return len(files)
    value = attrs.get("files")
    if isinstance(value, Mapping):
        for key in ("count", "total"):
            count = _nonnegative_int(value.get(key))
            if count is not None:
                return count
    return _nonnegative_int(value)


def _candidate_manifest_evidence(
    attrs: Mapping[str, Any],
) -> tuple[_ManifestEvidence, ...]:
    """Return every independently authored disc manifest in a candidate.

    Multi-disc UNIT3D torrents commonly contain more than one ``BDMV`` root.
    Hashing their files as one synthetic disc prevents an exact local disc from
    matching the corresponding member of a 2-in-1/3-in-1 torrent.  Group by the
    path leading to each disc root and fingerprint every group independently.
    """

    stored_manifest: _ManifestEvidence | None = None
    for key in (
        "disc_manifest_fingerprint",
        "bdmv_manifest_fingerprint",
    ):
        fingerprint = str(attrs.get(key) or "").strip().casefold()
        if fingerprint:
            version = str(attrs.get("disc_manifest_version") or "").strip() or None
            kind = str(attrs.get("disc_manifest_kind") or "").strip().upper() or None
            stored_manifest = (fingerprint, version, None, None, None, kind)
            break

    computed_manifests = list(
        _manifest_evidence_from_files(_candidate_file_sequence(attrs))
    )
    manifests: list[_ManifestEvidence] = []
    if stored_manifest is not None:
        stored_fingerprint, stored_version, _, _, _, stored_kind = stored_manifest
        duplicates_computed = any(
            fingerprint == stored_fingerprint
            and (
                stored_version is None
                or version is None
                or stored_version == version
            )
            and (stored_kind is None or kind is None or stored_kind == kind)
            for fingerprint, version, _, _, _, kind in computed_manifests
        )
        if not duplicates_computed:
            manifests.append(stored_manifest)

    # Do not deduplicate computed roots.  Two separately authored roots may be
    # byte-for-byte identical; their multiplicity is part of a multi-disc
    # release and is consumed by the Counter-based subset policy.
    manifests.extend(computed_manifests)
    return tuple(manifests)


def _candidate_manifest_storage_conflict(attrs: Mapping[str, Any]) -> bool:
    """Reject a stored manifest that contradicts the candidate's own files."""

    provided = {
        str(attrs.get(key) or "").strip().casefold()
        for key in ("disc_manifest_fingerprint", "bdmv_manifest_fingerprint")
        if str(attrs.get(key) or "").strip()
    }
    if len(provided) > 1:
        return True
    if not provided or not _candidate_file_sequence(attrs):
        return False

    version = str(attrs.get("disc_manifest_version") or "").strip() or None
    # Different manifest algorithms are not directly comparable.  They remain
    # ordinary unknown evidence rather than becoming a fabricated conflict.
    if version not in {None, _DISC_MANIFEST_VERSION}:
        return False

    files_only = dict(attrs)
    for key in (
        "disc_manifest_fingerprint",
        "bdmv_manifest_fingerprint",
        "disc_manifest_version",
    ):
        files_only.pop(key, None)
    computed = {
        fingerprint
        for fingerprint, computed_version, _, _, _, _ in _candidate_manifest_evidence(
            files_only
        )
        if computed_version in {None, version, _DISC_MANIFEST_VERSION}
    }
    return bool(computed) and not provided.issubset(computed)


def _candidate_report_conflict(
    attrs: Mapping[str, Any], remote_parsed: Mapping[str, Any], remote_name: str
) -> bool:
    """Compare independent name/structured/MediaInfo/BDInfo evidence."""

    reports = [
        str(attrs.get(key) or "")
        for key in ("media_info", "bd_info")
        if str(attrs.get(key) or "").strip()
    ]

    def scalar_conflict(values: Iterable[Any]) -> bool:
        known = [value for value in values if value]
        return len(set(known)) > 1

    def disjoint_conflict(values: Iterable[tuple[str, ...]]) -> bool:
        known = [set(value) for value in values if value]
        return any(
            first.isdisjoint(second)
            for index, first in enumerate(known)
            for second in known[index + 1 :]
        )

    structured_audio = " ".join(
        str(attrs.get(key) or "")
        for key in ("audio", "audio_codec", "audio_format", "audio_channels")
    )
    name_audio_formats, name_audio_channels = _audio_evidence(remote_name)
    structured_audio_formats, structured_audio_channels = _audio_evidence(
        structured_audio
    )
    report_audio = [_report_audio_evidence(report) for report in reports]
    name_object_audio, name_dts_profile = _audio_profile_evidence(remote_name)
    structured_object_audio, structured_dts_profile = _audio_profile_evidence(
        structured_audio
    )
    report_profiles = [_audio_profile_evidence(report) for report in reports]

    raw_bit_depth = attrs.get("bit_depth", attrs.get("bitDepth"))
    if isinstance(raw_bit_depth, (int, float)) and int(raw_bit_depth) in {8, 10}:
        structured_bit_depth = (f"{int(raw_bit_depth)}bit",)
    else:
        structured_bit_depth = _bit_depth_markers(raw_bit_depth)

    return bool(
        scalar_conflict(
            (
                _normalize_codec(remote_parsed.get("codec")),
                _normalize_codec(attrs.get("codec")),
                *(_normalize_codec(report) for report in reports),
            )
        )
        or scalar_conflict(
            (
                _normalize_resolution(remote_parsed.get("resolution")),
                _normalize_resolution(attrs.get("resolution")),
                *(_normalize_resolution(report) for report in reports),
            )
        )
        or disjoint_conflict(
            (
                name_audio_formats,
                structured_audio_formats,
                *(formats for formats, _ in report_audio),
            )
        )
        or disjoint_conflict(
            (
                name_audio_channels,
                structured_audio_channels,
                *(channels for _, channels in report_audio),
            )
        )
        or disjoint_conflict(
            (
                name_object_audio,
                structured_object_audio,
                *(object_audio for object_audio, _ in report_profiles),
            )
        )
        or disjoint_conflict(
            (
                name_dts_profile,
                structured_dts_profile,
                *(dts_profile for _, dts_profile in report_profiles),
            )
        )
        or disjoint_conflict(
            (
                _bit_depth_markers(remote_name),
                structured_bit_depth,
                *(_bit_depth_markers(report) for report in reports),
            )
        )
    )


def _candidate_internal_conflict(
    attrs: Mapping[str, Any], remote_parsed: Mapping[str, Any], remote_name: str
) -> bool:
    """Validate independent structured and name evidence before matching."""

    remote_type, type_storage_conflict = _candidate_release_type(attrs)
    name_type = _normalize_release_type(remote_parsed.get("release_type"))
    structured_resolution = _normalize_resolution(attrs.get("resolution"))
    name_resolution = _normalize_resolution(remote_parsed.get("resolution"))
    structured_codec = _normalize_codec(attrs.get("codec"))
    name_codec = _normalize_codec(remote_parsed.get("codec"))
    structured_regions, invalid_regions = _structured_region_evidence(
        attrs.get("disc_regions")
    )
    name_regions = _disc_regions(remote_name)
    structured_editions, invalid_editions = _structured_edition_evidence(
        attrs.get("disc_editions")
    )
    name_editions = _disc_editions(remote_name)
    _, _, hdr_conflict = _merge_remote_hdr_evidence(
        attrs, remote_parsed, remote_name
    )
    _, episode_conflict = _merge_remote_episode_evidence(attrs, remote_parsed)

    explicit_pairs = (
        (remote_type, name_type),
        (structured_resolution, name_resolution),
        (structured_codec, name_codec),
        (structured_regions, name_regions),
        (structured_editions, name_editions),
    )
    return bool(
        type_storage_conflict
        or invalid_regions
        or invalid_editions
        or hdr_conflict
        or episode_conflict
        or _candidate_manifest_storage_conflict(attrs)
        or _candidate_report_conflict(attrs, remote_parsed, remote_name)
        or any(local and remote and local != remote for local, remote in explicit_pairs)
    )


def _candidate_manifests(attrs: Mapping[str, Any]) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        dict.fromkeys(
            (fingerprint, version)
            for fingerprint, version, _, _, _, _ in _candidate_manifest_evidence(attrs)
        )
    )


def _candidate_manifest(attrs: Mapping[str, Any]) -> tuple[str | None, str | None]:
    manifests = _candidate_manifests(attrs)
    return manifests[0] if manifests else (None, None)


def _local_manifest_evidence(
    parsed: Mapping[str, Any],
) -> tuple[_ManifestEvidence, ...]:
    raw = parsed.get("disc_manifest_evidence")
    evidence: list[_ManifestEvidence] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Mapping):
                fingerprint = str(item.get("fingerprint") or "").strip().casefold()
                version = str(item.get("version") or "").strip() or None
                details = (
                    _nonnegative_int(item.get("file_count")),
                    _byte_size(item.get("payload_size")),
                    _byte_size(item.get("main_file_size")),
                )
                kind = str(item.get("kind") or "").strip().upper() or None
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                values = list(item)
                if len(values) < 2:
                    continue
                fingerprint = str(values[0] or "").strip().casefold()
                version = str(values[1] or "").strip() or None
                details = (
                    _nonnegative_int(values[2]) if len(values) > 2 else None,
                    _byte_size(values[3]) if len(values) > 3 else None,
                    _byte_size(values[4]) if len(values) > 4 else None,
                )
                kind = (
                    str(values[5] or "").strip().upper()
                    if len(values) > 5 and values[5]
                    else None
                )
            else:
                continue
            if fingerprint:
                evidence.append((fingerprint, version, *details, kind))
    if evidence:
        return tuple(evidence)

    fingerprint = str(parsed.get("disc_manifest_fingerprint") or "").strip().casefold()
    if not fingerprint:
        return ()
    return (
        (
            fingerprint,
            str(parsed.get("disc_manifest_version") or "").strip() or None,
            _nonnegative_int(parsed.get("disc_file_count")),
            _byte_size(parsed.get("disc_payload_size")),
            _byte_size(parsed.get("disc_main_file_size")),
            str(parsed.get("disc_manifest_kind") or "").strip().upper() or None,
        ),
    )


def _media_file_sizes_from_files(
    files: Sequence[Mapping[str, Any]],
) -> tuple[int, ...]:
    return tuple(
        sorted(
            size
            for item in files
            if _PAYLOAD_EXTENSION_RE.search(_file_name(item))
            if (size := _file_size(item)) is not None
        )
    )


def _media_payload_signature_from_files(
    files: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, int], ...]:
    signature: list[tuple[str, int]] = []
    for item in files:
        match = _PAYLOAD_EXTENSION_RE.search(_file_name(item))
        size = _file_size(item)
        if match and size is not None:
            signature.append((match.group(0).lstrip(".").casefold(), size))
    return tuple(sorted(signature))


def _media_payload_signature(value: Any) -> tuple[tuple[str, int], ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        return ()
    signature: list[tuple[str, int]] = []
    for item in value:
        if isinstance(item, Mapping):
            extension = item.get("extension", item.get("ext", item.get("kind")))
            size = _byte_size(item.get("size"))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            values = list(item)
            if len(values) != 2:
                return ()
            extension, raw_size = values
            size = _byte_size(raw_size)
        else:
            return ()
        normalized_extension = str(extension or "").strip().lstrip(".").casefold()
        if (
            size is None
            or not normalized_extension
            or not _PAYLOAD_EXTENSION_RE.fullmatch(f".{normalized_extension}")
        ):
            return ()
        signature.append((normalized_extension, size))
    return tuple(sorted(signature))


def _media_file_sizes(value: Any) -> tuple[int, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        return ()
    sizes = [_byte_size(item) for item in value]
    if not sizes or any(size is None for size in sizes):
        return ()
    return tuple(sorted(size for size in sizes if size is not None))


def _local_media_file_sizes(parsed: Mapping[str, Any]) -> tuple[int, ...]:
    sizes = _media_file_sizes(parsed.get("media_file_sizes"))
    if sizes:
        return sizes

    # A complete vector for a known single-file release is unambiguous and
    # keeps already-enriched state compatible with the stronger matcher.
    if _nonnegative_int(parsed.get("torrent_file_count")) == 1:
        size = _disc_total_size(parsed)
        return (size,) if size is not None else ()
    return ()


def _local_media_payload_signature(
    parsed: Mapping[str, Any],
) -> tuple[tuple[str, int], ...]:
    signature = _media_payload_signature(parsed.get("media_payload_signature"))
    if signature:
        return signature
    if _nonnegative_int(parsed.get("torrent_file_count")) == 1:
        match = _PAYLOAD_EXTENSION_RE.search(str(parsed.get("name") or ""))
        size = _disc_total_size(parsed)
        if match and size is not None:
            return ((match.group(0).lstrip(".").casefold(), size),)
    return ()


def _remote_media_file_sizes(attrs: Mapping[str, Any]) -> tuple[int, ...]:
    sizes = _media_file_sizes_from_files(_candidate_file_sequence(attrs))
    if sizes:
        return sizes
    if _candidate_file_count(attrs) == 1:
        size = _remote_disc_size(attrs)
        return (size,) if size is not None else ()
    return ()


def _remote_media_payload_signature(
    attrs: Mapping[str, Any],
) -> tuple[tuple[str, int], ...]:
    return _media_payload_signature_from_files(_candidate_file_sequence(attrs))


def _local_media_payload_size(parsed: Mapping[str, Any]) -> int | None:
    sizes = _local_media_file_sizes(parsed)
    if sizes:
        return sum(sizes)
    for key in ("media_payload_size", "media_main_file_size"):
        size = _byte_size(parsed.get(key))
        if size is not None:
            return size
    if _nonnegative_int(parsed.get("torrent_file_count")) == 1:
        return _disc_total_size(parsed)
    return None


def _candidate_release_type(attrs: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Resolve UNIT3D's string/id type pair and report an internal conflict."""

    raw_type = attrs.get("type")
    raw_type_id = attrs.get("type_id", attrs.get("typeId"))
    if isinstance(raw_type, Mapping):
        raw_type_id = raw_type.get("id", raw_type_id)
        raw_type = raw_type.get("name") or raw_type.get("type")

    named_type = _normalize_release_type(raw_type)
    type_id = _nonnegative_int(raw_type_id)
    id_type = _AITHER_RELEASE_TYPE_BY_ID.get(type_id) if type_id is not None else None
    conflict = bool(named_type and id_type and named_type != id_type)
    return named_type or id_type, conflict


def _structured_label_values(
    value: Any, *, mapping_fields: tuple[str, ...]
) -> tuple[tuple[str, ...], bool]:
    """Flatten validated UNIT3D label shapes as ``(values, invalid_shape)``."""

    if value is None:
        return (), False
    if isinstance(value, str):
        return _mapping_tokens(value), False
    if isinstance(value, Mapping):
        selected = [value.get(key) for key in mapping_fields if value.get(key) is not None]
        if not selected:
            return (), True
        flattened: list[str] = []
        invalid = False
        for item in selected:
            tokens, item_invalid = _structured_label_values(
                item, mapping_fields=mapping_fields
            )
            flattened.extend(tokens)
            invalid |= item_invalid
        return tuple(flattened), invalid
    if isinstance(value, Iterable) and not isinstance(value, bytes):
        flattened = []
        invalid = False
        for item in value:
            tokens, item_invalid = _structured_label_values(
                item, mapping_fields=mapping_fields
            )
            flattened.extend(tokens)
            invalid |= item_invalid
        return tuple(flattened), invalid
    return (), True


def _structured_region_evidence(value: Any) -> tuple[tuple[str, ...], bool]:
    raw_tokens, invalid = _structured_label_values(
        value, mapping_fields=("name", "code", "region")
    )
    regions: set[str] = set()
    for token in raw_tokens:
        normalized = _DISC_REGION_ALIASES.get(token.strip().upper())
        if normalized:
            regions.add(normalized)
        else:
            invalid = True
    return tuple(sorted(regions)), invalid


def _structured_region_tokens(value: Any) -> tuple[str, ...]:
    return _structured_region_evidence(value)[0]


def _structured_edition_evidence(value: Any) -> tuple[tuple[str, ...], bool]:
    raw_tokens, invalid = _structured_label_values(
        value, mapping_fields=("name", "code", "edition")
    )
    editions: set[str] = set()
    for token in raw_tokens:
        normalized = re.sub(r"[\s._-]+", "-", token.strip().casefold())
        detected = _disc_editions(token)
        if detected:
            editions.update(detected)
        elif normalized:
            editions.add(normalized)
        else:
            invalid = True
    return tuple(sorted(editions)), invalid


def _structured_edition_tokens(value: Any) -> tuple[str, ...]:
    return _structured_edition_evidence(value)[0]


def _candidate_resolution(
    attrs: Mapping[str, Any], remote_parsed: Mapping[str, Any]
) -> str | None:
    return (
        _normalize_resolution(attrs.get("resolution"))
        or _normalize_resolution(remote_parsed.get("resolution"))
        or _normalize_resolution(attrs.get("media_info"))
        or _normalize_resolution(attrs.get("bd_info"))
    )


def _candidate_codec(
    attrs: Mapping[str, Any], remote_parsed: Mapping[str, Any]
) -> str | None:
    structured = _normalize_codec(attrs.get("codec"))
    parsed = _normalize_codec(remote_parsed.get("codec"))
    if structured or parsed:
        return structured or parsed
    return _detect_codec(
        " ".join(str(attrs.get(key) or "") for key in ("media_info", "bd_info"))
    )


def _remote_media_payload_size(attrs: Mapping[str, Any]) -> int | None:
    sizes = _remote_media_file_sizes(attrs)
    return sum(sizes) if sizes else _remote_disc_size(attrs)


def _release_marker_tail(value: Any) -> str:
    cleaned, _ = _clean_release_name(str(value or ""))
    year = _select_release_year(cleaned)
    episode = _EPISODE_RE.search(cleaned) or _SEASON_RE.search(cleaned)
    technical = _TECH_TOKEN_RE.search(cleaned)
    anchors = [
        match.end()
        for match in (year, episode)
        if match is not None
    ]
    if not anchors and technical is not None:
        anchors.append(technical.start())
    return cleaned[min(anchors):] if anchors else ""


def _revision_markers(value: Any) -> tuple[str, ...]:
    tail = _release_marker_tail(value)
    return tuple(
        sorted(
            {
                re.sub(r"[\s._-]+", ".", match.group(0)).upper()
                for match in re.finditer(
                    r"(?i)(?<![a-z0-9])(?:"
                    r"real[ ._-]+(?:proper|repack|rerip)\d*|"
                    r"proper\d*|repack\d*|rerip\d*"
                    r")(?![a-z0-9])",
                    tail,
                )
            }
        )
    )


def _material_markers(value: Any) -> tuple[str, ...]:
    tail = _release_marker_tail(value)
    return tuple(
        sorted(
            {
                match.group(0).upper()
                for match in re.finditer(
                    r"(?i)(?<![a-z0-9])(?:hybrid|custom)(?![a-z0-9])",
                    tail,
                )
            }
        )
    )


def _disc_capacity_markers(value: Any) -> tuple[str, ...]:
    """Return explicit authored-media capacity labels from a release name."""

    return tuple(
        sorted(
            {
                re.sub(r"[\s._-]+", "", match.group(1)).upper()
                for match in re.finditer(
                    r"(?i)(?<![a-z0-9])"
                    r"(DVD[ ._-]?[59]|BD[ ._-]?(?:25|50|66|100))"
                    r"(?![a-z0-9])",
                    str(value or ""),
                )
            }
        )
    )


def _disc_video_standards(value: Any) -> tuple[str, ...]:
    return tuple(
        standard
        for standard in ("PAL", "NTSC")
        if re.search(
            rf"(?i)(?<![a-z0-9]){standard}(?![a-z0-9])", str(value or "")
        )
    )


def _physical_region_codes(value: Any) -> tuple[str, ...]:
    """Extract contextual Blu-ray/DVD region codes without accepting bare A/B/C."""

    text = str(value or "")
    codes: set[str] = set()
    all_regions = bool(
        re.search(
            r"(?i)(?<![a-z0-9])(?:"
            r"region[ ._-]*free|all[ ._-]*regions?|regions?[ ._-]*all|"
            r"regions?[ ._-]*(?:abc|a[ ._/+,&-]*b[ ._/+,&-]*c)"
            r")(?![a-z0-9])",
            text,
        )
    )
    if all_regions:
        codes.add("REGION-FREE")
    else:
        list_separator = (
            r"(?:[ ._]+and[ ._]+|[ ._]*[/+,&-][ ._]*|[ ._]+)"
        )
        for match in re.finditer(
            r"(?i)(?<![a-z0-9])regions?[ ._-]*"
            r"([abc](?:" + list_separator + r"[abc]){1,2}|[abc]{1,3})"
            r"(?![a-z0-9])",
            text,
        ):
            for region in re.findall(r"(?i)[abc]", match.group(1)):
                codes.add(f"BD-{region.upper()}")
        if {"BD-A", "BD-B", "BD-C"}.issubset(codes):
            codes.difference_update({"BD-A", "BD-B", "BD-C"})
            codes.add("REGION-FREE")
    numeric_separator = (
        r"(?:[ ._]+and[ ._]+|[ ._]*[/+,&-][ ._]*|[ ._]+)"
    )
    for match in re.finditer(
        r"(?i)(?<![a-z0-9])regions?[ ._-]*"
        r"(r?[0-8](?:" + numeric_separator + r"r?[0-8])+)"
        r"(?![a-z0-9])",
        text,
    ):
        for region_number in re.findall(r"(?i)r?([0-8])", match.group(1)):
            codes.add(
                "REGION-FREE"
                if region_number == "0"
                else f"DVD-R{region_number}"
            )
    for match in re.finditer(
        r"(?i)(?<![a-z0-9])(?:region[ ._-]*)?r?([0-8])(?![a-z0-9])",
        text,
    ):
        token = match.group(0).strip()
        # A bare digit is not a region marker.  Require either Rn or REGION n.
        if re.match(r"(?i)^(?:r\d|region[ ._-]*r?\d)$", token):
            region_number = match.group(1)
            codes.add("REGION-FREE" if region_number == "0" else f"DVD-R{region_number}")
    return tuple(sorted(codes))


def _explicit_marker_relation(
    local: tuple[str, ...], remote: tuple[str, ...]
) -> tuple[bool, bool]:
    """Return ``(conflict, ambiguous_overlap)`` for two explicit marker sets."""

    if not local or not remote or local == remote:
        return False, False
    if set(local).isdisjoint(remote):
        return True, False
    return False, True


def _disc_distributors(value: Any) -> tuple[str, ...]:
    tail = _release_marker_tail(value)
    return tuple(
        sorted(
            label
            for label, pattern in _DISC_DISTRIBUTOR_PATTERNS.items()
            if re.search(pattern, tail, re.IGNORECASE)
        )
    )


def _exact_group_payload_match(
    parsed: Mapping[str, Any], attrs: Mapping[str, Any]
) -> bool:
    """Same release group and byte-identical media payload (names may differ)."""

    local_group = extract_release_group(str(parsed.get("name") or "")).casefold()
    remote_group = extract_release_group(str(attrs.get("name") or "")).casefold()
    if not local_group or local_group != remote_group:
        return False
    local_sizes = _local_media_file_sizes(parsed)
    if not local_sizes or local_sizes != _remote_media_file_sizes(attrs):
        return False
    local_signature = _local_media_payload_signature(parsed)
    return bool(
        local_signature and local_signature == _remote_media_payload_signature(attrs)
    )


def web_policy() -> str:
    """AITHER_WEB_POLICY: ``strict`` | ``precedent`` (default) | ``distinct-group``.

    ``strict`` keeps every WEB-DL from another group MANUAL.  ``precedent``
    treats it as a separate release only when the tracker's own listing already
    shows parallel WEB-DLs of the same episode and resolution from different
    groups.  ``distinct-group`` does so unconditionally.
    """

    value = os.environ.get("AITHER_WEB_POLICY", "precedent").strip().casefold()
    value = value.replace("_", "-")
    return value if value in {"strict", "precedent", "distinct-group"} else "precedent"


def _web_parallel_precedent(
    parsed: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> bool:
    """True when the listing shows different groups' WEB-DLs side by side.

    Same resolution and the same single episode (or the same movie), at least
    two release groups.  When the tracker exposes the ``internal`` flag at
    least one of those releases must come from a regular uploader: internal
    groups may bypass the dupe rules, so internal-only pairs prove nothing.
    """

    local_resolution = _normalize_resolution(parsed.get("resolution"))
    if not local_resolution:
        return False
    scopes: dict[tuple[Any, tuple[int, ...]], dict[str, set[Any]]] = {}
    for candidate in candidates:
        attrs = _candidate_attributes(candidate)
        name = str(attrs.get("name") or "")
        if not name:
            continue
        raw_remote = parse_release(name)
        remote, conflict = _merge_remote_episode_evidence(attrs, raw_remote)
        if conflict or remote.get("scope") in {"season_pack", "multi_episode"}:
            continue
        structured_type, _ = _candidate_release_type(attrs)
        remote_type = structured_type or _normalize_release_type(
            remote.get("release_type")
        )
        if remote_type not in {"WEB-DL", "WEBRip"}:
            continue
        if _candidate_resolution(attrs, remote) != local_resolution:
            continue
        group = extract_release_group(name).casefold()
        if not group:
            continue
        scope = (remote.get("season"), tuple(remote.get("episodes") or ()))
        scopes.setdefault(scope, {}).setdefault(group, set()).add(attrs.get("internal"))
    for groups in scopes.values():
        if len(groups) < 2:
            continue
        known = [
            flag
            for flags in groups.values()
            for flag in flags
            if isinstance(flag, bool)
        ]
        if not known or any(flag is False for flag in known):
            return True
    return False


def _distinct_web_release(
    parsed: Mapping[str, Any],
    attrs: Mapping[str, Any],
    *,
    local_type: str | None,
    remote_type: str | None,
    local_resolution: str | None,
    remote_resolution: str | None,
    web_precedent: bool | None = None,
) -> bool:
    """WEB-DL from another group with different bytes is a separate release.

    Applies when the policy allows it (``distinct-group`` always, ``precedent``
    only with a proven parallel-release precedent), both sides are
    WEB-DL/WEBRip of the same known resolution, both release groups are known
    and different, and both byte-exact payload vectors are known and different.
    """

    policy = web_policy()
    if policy == "strict" or (policy == "precedent" and not web_precedent):
        return False
    if local_type not in {"WEB-DL", "WEBRip"} or local_type != remote_type:
        return False
    if not local_resolution or local_resolution != remote_resolution:
        return False
    local_group = extract_release_group(str(parsed.get("name") or "")).casefold()
    remote_group = extract_release_group(str(attrs.get("name") or "")).casefold()
    if not local_group or not remote_group or local_group == remote_group:
        return False
    local_sizes = _local_media_file_sizes(parsed)
    remote_sizes = _remote_media_file_sizes(attrs)
    return bool(local_sizes and remote_sizes and local_sizes != remote_sizes)


def _same_release_payload_identity(
    parsed: Mapping[str, Any],
    attrs: Mapping[str, Any],
    remote_parsed: Mapping[str, Any],
    *,
    remote_type: str | None,
    remote_resolution: str | None,
    remote_hdr: tuple[str, ...],
    remote_hdr_known: bool,
    remote_codec: str | None,
) -> bool:
    """Prove a non-disc match despite an incomplete HDR label.

    This intentionally requires the same release group and exact media bytes.
    A merely similar size or a technically similar release from another group
    is not promoted to a duplicate.
    """

    local_group = extract_release_group(str(parsed.get("name") or "")).casefold()
    remote_group = extract_release_group(str(attrs.get("name") or "")).casefold()
    if not local_group or local_group != remote_group:
        return False

    local_sizes = _local_media_file_sizes(parsed)
    remote_sizes = _remote_media_file_sizes(attrs)
    if not local_sizes or local_sizes != remote_sizes:
        return False
    local_signature = _local_media_payload_signature(parsed)
    remote_signature = _remote_media_payload_signature(attrs)
    if not local_signature or local_signature != remote_signature:
        return False

    local_hdr, local_hdr_known = _hdr_evidence(parsed)
    if (
        local_hdr_known
        and remote_hdr_known
        and not _hdr_tokens_compatible(local_hdr, remote_hdr)
    ):
        return False

    local_editions = _structured_edition_tokens(parsed.get("disc_editions")) or _disc_editions(
        parsed.get("name") or ""
    )
    remote_editions = _structured_edition_tokens(attrs.get("disc_editions")) or _disc_editions(
        attrs.get("name") or ""
    )
    if local_editions != remote_editions:
        return False

    local_type = _normalize_release_type(parsed.get("release_type"))
    local_resolution = _normalize_resolution(parsed.get("resolution"))
    local_medium = parsed.get("medium")
    remote_medium = remote_parsed.get("medium")
    local_codec = _normalize_codec(parsed.get("codec"))

    required_pairs = (
        (local_type, remote_type),
        (local_resolution, remote_resolution),
        (local_medium, remote_medium),
        (local_codec, remote_codec),
    )
    return all(local is not None and local == remote for local, remote in required_pairs)


def _season_collection_contains_exact_release(
    parsed: Mapping[str, Any],
    attrs: Mapping[str, Any],
    remote_parsed: Mapping[str, Any],
    *,
    remote_type: str | None,
    remote_resolution: str | None,
    remote_hdr: tuple[str, ...],
    remote_hdr_known: bool,
    remote_codec: str | None,
) -> bool:
    """Prove that an episode upload is already present inside a season pack.

    UNIT3D exposes every file of a season torrent.  Comparing an episode's
    payload against the *aggregate* pack size necessarily fails, even when the
    byte-identical episode is one of those files.  Require the same release
    group, exact episode membership, exact extension+size payload evidence and
    a complete compatible technical signature before promoting the pack.
    """

    local_scope = parsed.get("scope")
    remote_scope = remote_parsed.get("scope")
    if local_scope not in {"episode", "multi_episode"} or remote_scope not in {
        "season_pack",
        "multi_episode",
    }:
        return False

    local_season = _nonnegative_int(parsed.get("season"))
    local_episodes = {
        episode
        for value in (parsed.get("episodes") or ())
        if (episode := _nonnegative_int(value)) is not None
    }
    if local_season is None or not local_episodes:
        return False

    local_group = extract_release_group(str(parsed.get("name") or "")).casefold()
    remote_group = extract_release_group(str(attrs.get("name") or "")).casefold()
    if not local_group or local_group != remote_group:
        return False

    local_signature = _local_media_payload_signature(parsed)
    if not local_signature:
        return False

    member_payloads: Counter[tuple[str, int]] = Counter()
    for item in _candidate_file_sequence(attrs):
        file_name = _file_name(item)
        file_size = _file_size(item)
        extension = _PAYLOAD_EXTENSION_RE.search(file_name)
        if not file_name or file_size is None or extension is None:
            continue
        file_parsed = parse_release(os.path.basename(file_name))
        file_season = _nonnegative_int(file_parsed.get("season"))
        file_episodes = {
            episode
            for value in (file_parsed.get("episodes") or ())
            if (episode := _nonnegative_int(value)) is not None
        }
        if file_season != local_season or not local_episodes.issubset(file_episodes):
            continue
        member_payloads[
            (extension.group(0).lstrip(".").casefold(), file_size)
        ] += 1

    if not (Counter(local_signature) <= member_payloads):
        return False

    local_hdr, local_hdr_known = _hdr_evidence(parsed)
    if (
        local_hdr_known
        and remote_hdr_known
        and not _hdr_tokens_compatible(local_hdr, remote_hdr)
    ):
        return False

    local_type = _normalize_release_type(parsed.get("release_type"))
    local_resolution = _normalize_resolution(parsed.get("resolution"))
    local_medium = parsed.get("medium")
    remote_medium = remote_parsed.get("medium")
    local_codec = _normalize_codec(parsed.get("codec"))
    required_pairs = (
        (local_type, remote_type),
        (local_resolution, remote_resolution),
        (local_medium, remote_medium),
        (local_codec, remote_codec),
    )
    return all(local is not None and local == remote for local, remote in required_pairs)


def _disc_total_size(parsed: Mapping[str, Any]) -> int | None:
    for key in ("disc_total_size", "total_size", "size", "disc_payload_size"):
        size = _byte_size(parsed.get(key))
        if size is not None:
            return size
    return _bdinfo_size(parsed.get("bd_info"))


def _remote_disc_size(attrs: Mapping[str, Any]) -> int | None:
    for key in ("size", "total_size", "disc_size"):
        size = _byte_size(attrs.get(key))
        if size is not None:
            return size
    return _bdinfo_size(attrs.get("bd_info"))


def _size_relation(local: int | None, remote: int | None) -> str:
    if local is None or remote is None:
        return "unknown"
    smaller = min(local, remote)
    difference = abs(local - remote)
    if difference == 0:
        return "exact"
    close_limit = max(64 * _MIB, int(smaller * 0.0025))
    mismatch_limit = max(512 * _MIB, int(smaller * 0.02))
    if difference <= close_limit:
        return "close"
    if difference >= mismatch_limit:
        return "different"
    return "uncertain"


def _opaque_iso_size_fits_remote(
    local_evidence: Sequence[_ManifestEvidence],
    remote_evidence: Sequence[_ManifestEvidence],
) -> bool:
    """Could an opaque ISO be a disc (or the whole set) of the remote torrent?

    The inside of an ISO is unknown, so its disc cannot be proven different
    from a remote disc whose size it matches.  An ISO is slightly larger than
    the disc payload (UDF metadata), and a multi-disc torrent holds several
    discs: compare the image with every remote disc root and with their sum.
    """

    iso_sizes = [
        payload
        for _, _, _, payload, _, kind in local_evidence
        if kind == "ISO" and payload
    ]
    remote_sizes = [
        payload
        for _, _, _, payload, _, kind in remote_evidence
        if kind in {"BDMV", "VIDEO_TS", "ISO"} and payload
    ]
    if not iso_sizes or not remote_sizes:
        return False
    candidates = [*remote_sizes, sum(remote_sizes)]
    return any(
        _size_relation(local, remote) != "different"
        for local in iso_sizes
        for remote in candidates
    )


def _near_manifest_variant(
    parsed: Mapping[str, Any],
    evidence: Sequence[_ManifestEvidence],
) -> bool:
    """Detect an almost-identical authored disc without claiming a duplicate.

    Equal entry count, equal largest payload and a close aggregate payload are
    characteristic of tiny Java/metadata authoring changes.  They are not
    proof of byte identity, so the result deliberately falls through to
    ``INDETERMINATE`` instead of becoming either MATCH or proven NO_MATCH.
    """

    local_evidence = _local_manifest_evidence(parsed)
    if len(local_evidence) != 1:
        return False
    _, local_version, local_count, local_payload, local_main, _ = local_evidence[0]
    if local_count is None or local_payload is None or local_main is None:
        return False

    for _, remote_version, count, payload, main, _ in evidence:
        if local_version and remote_version and local_version != remote_version:
            continue
        if (
            count == local_count
            and main == local_main
            and _size_relation(local_payload, payload) in {"exact", "close"}
        ):
            return True
    return False


def _full_disc_decision(
    parsed: Mapping[str, Any],
    attrs: Mapping[str, Any],
    remote_parsed: Mapping[str, Any],
) -> TechnicalDecision:
    """Compare disc editions without treating film identity as disc identity."""

    remote_name = str(attrs.get("name") or "")
    local_regions = _structured_region_tokens(parsed.get("disc_regions")) or _disc_regions(
        parsed.get("name") or ""
    )
    remote_regions = _structured_region_tokens(attrs.get("disc_regions")) or _disc_regions(
        " ".join((remote_name, str(attrs.get("bd_info") or "")))
    )
    if local_regions and remote_regions and local_regions != remote_regions:
        return TechnicalDecision.NO_MATCH

    # A conflict is a veto only when both release records explicitly name an
    # edition.  BDInfo playlist prose is not an edition label: using it here
    # falsely classified exact 2in1/3in1 member discs as different cuts.
    local_editions = _structured_edition_tokens(parsed.get("disc_editions")) or _disc_editions(
        parsed.get("name") or ""
    )
    remote_editions = _structured_edition_tokens(attrs.get("disc_editions")) or _disc_editions(
        remote_name
    )
    if local_editions and remote_editions and local_editions != remote_editions:
        return TechnicalDecision.NO_MATCH

    local_distributors = _disc_distributors(parsed.get("name"))
    remote_distributors = _disc_distributors(remote_name)
    if (
        local_distributors
        and remote_distributors
        and local_distributors != remote_distributors
    ):
        return TechnicalDecision.NO_MATCH
    distributor_unknown = bool(local_distributors) != bool(remote_distributors)

    disc_format_ambiguous = False
    for local_markers, remote_markers in (
        (
            _disc_capacity_markers(parsed.get("name")),
            _disc_capacity_markers(remote_name),
        ),
        (
            _disc_video_standards(parsed.get("name")),
            _disc_video_standards(remote_name),
        ),
        (
            _physical_region_codes(parsed.get("name")),
            _physical_region_codes(remote_name),
        ),
    ):
        marker_conflict, marker_ambiguous = _explicit_marker_relation(
            local_markers, remote_markers
        )
        if marker_conflict:
            return TechnicalDecision.NO_MATCH
        disc_format_ambiguous |= marker_ambiguous

    local_medium = parsed.get("medium")
    remote_medium = remote_parsed.get("medium")
    if local_medium is not None and remote_medium is not None and local_medium != remote_medium:
        return TechnicalDecision.NO_MATCH

    local_resolution = _normalize_resolution(parsed.get("resolution"))
    remote_resolution = _candidate_resolution(attrs, remote_parsed)
    if (
        local_resolution is not None
        and remote_resolution is not None
        and local_resolution != remote_resolution
    ):
        return TechnicalDecision.NO_MATCH

    local_codec = _normalize_codec(parsed.get("codec"))
    remote_codec = _candidate_codec(attrs, remote_parsed)
    if local_codec is not None and remote_codec is not None and local_codec != remote_codec:
        return TechnicalDecision.NO_MATCH

    local_technical_text = " ".join(
        str(parsed.get(key) or "") for key in ("name", "media_info", "bd_info")
    )
    remote_technical_text = " ".join(
        str(attrs.get(key) or "") for key in ("name", "media_info", "bd_info")
    )
    local_audio_formats, local_audio_channels = _release_audio_evidence(
        parsed.get("name"), parsed.get("media_info"), parsed.get("bd_info")
    )
    remote_audio_formats, remote_audio_channels = _release_audio_evidence(
        remote_name, attrs.get("media_info"), attrs.get("bd_info")
    )
    local_name_audio_formats, local_name_audio_channels = _audio_evidence(
        parsed.get("name")
    )
    remote_name_audio_formats, remote_name_audio_channels = _audio_evidence(
        remote_name
    )
    named_audio_variant = bool(
        (
            local_name_audio_formats
            and remote_name_audio_formats
            and local_name_audio_formats != remote_name_audio_formats
        )
        or (
            local_name_audio_channels
            and remote_name_audio_channels
            and local_name_audio_channels != remote_name_audio_channels
        )
    )
    if (
        local_audio_formats
        and remote_audio_formats
        and set(local_audio_formats).isdisjoint(remote_audio_formats)
    ):
        return TechnicalDecision.NO_MATCH
    if (
        local_audio_channels
        and remote_audio_channels
        and set(local_audio_channels).isdisjoint(remote_audio_channels)
    ):
        return TechnicalDecision.NO_MATCH

    local_hdr, _ = _hdr_evidence(parsed)
    remote_hdr, _, _ = _merge_remote_hdr_evidence(attrs, remote_parsed, remote_name)
    if local_hdr and remote_hdr and not _hdr_tokens_compatible(local_hdr, remote_hdr):
        return TechnicalDecision.NO_MATCH

    local_bit_depth = _bit_depth_markers(local_technical_text)
    remote_bit_depth = _bit_depth_markers(remote_technical_text)
    if local_bit_depth and remote_bit_depth and local_bit_depth != remote_bit_depth:
        return TechnicalDecision.NO_MATCH

    local_bdinfo = normalize_bdinfo_signature(parsed.get("bd_info"))
    remote_bdinfo = normalize_bdinfo_signature(attrs.get("bd_info"))
    if local_bdinfo and remote_bdinfo and local_bdinfo != remote_bdinfo:
        return TechnicalDecision.NO_MATCH

    # An overlapping name set (DTS 5.1 vs DTS 5.1 + TrueHD 7.1) is not a
    # proven different disc: either uploader may have omitted a secondary
    # track.  It is nevertheless too ambiguous for path/size evidence to turn
    # into an automatic duplicate.  Report-only extra tracks remain allowed.
    if named_audio_variant or disc_format_ambiguous:
        return TechnicalDecision.INDETERMINATE

    # File identity is stronger than optional release labels.  It is evaluated
    # after every explicit two-sided veto so a disc nested inside a multi-disc
    # torrent can match only when no technical label/report contradicts it.
    local_manifest_evidence = _local_manifest_evidence(parsed)
    remote_manifest_evidence = _candidate_manifest_evidence(attrs)
    local_has_opaque_iso = bool(
        str(parsed.get("disc_manifest_kind") or "").upper() == "ISO"
        or any(kind == "ISO" for _, _, _, _, _, kind in local_manifest_evidence)
    )
    if local_manifest_evidence and remote_manifest_evidence:
        local_current = [
            (fingerprint, kind)
            for fingerprint, version, _, _, _, kind in local_manifest_evidence
            if version == _DISC_MANIFEST_VERSION
        ]
        remote_current = [
            (fingerprint, kind)
            for fingerprint, version, _, _, _, kind in remote_manifest_evidence
            if version == _DISC_MANIFEST_VERSION
        ]
        if not local_current or not remote_current:
            return TechnicalDecision.INDETERMINATE

        local_counter = Counter(local_current)
        remote_counter = Counter(remote_current)
        overlap = local_counter & remote_counter
        overlap_count = sum(overlap.values())
        opaque_iso = any(kind == "ISO" for _, kind in local_current)
        local_is_covered = all(
            count <= remote_counter[key] for key, count in local_counter.items()
        )
        if local_is_covered:
            if not opaque_iso:
                return (
                    TechnicalDecision.INDETERMINATE
                    if distributor_unknown
                    else TechnicalDecision.MATCH
                )
        else:
            if overlap_count:
                return TechnicalDecision.INDETERMINATE
            local_fingerprints = {fingerprint for fingerprint, _ in local_current}
            remote_fingerprints = {fingerprint for fingerprint, _ in remote_current}
            if local_fingerprints & remote_fingerprints:
                # Equal digests with a missing/contradictory kind cannot prove
                # cross-kind coverage, especially for opaque ISO roots.
                return TechnicalDecision.INDETERMINATE
            if len(local_current) == 1 and _near_manifest_variant(
                parsed, remote_manifest_evidence
            ):
                return TechnicalDecision.INDETERMINATE
            local_core = str(parsed.get("disc_stream_core") or "").casefold()
            if local_core and local_core in _candidate_stream_cores(attrs):
                # Same STREAM/PLAYLIST/CLIPINF bytes, different Java/metadata:
                # most likely another pressing or re-authoring of one disc.
                return TechnicalDecision.INDETERMINATE
            if opaque_iso and _opaque_iso_size_fits_remote(
                local_manifest_evidence, remote_manifest_evidence
            ):
                # An image whose inside is unknown cannot be proven different
                # from a remote disc of the same size (or from one disc of a
                # multi-disc torrent).
                return TechnicalDecision.INDETERMINATE
            return TechnicalDecision.NO_MATCH

    size_relation = _size_relation(_disc_total_size(parsed), _remote_disc_size(attrs))
    if size_relation == "different":
        return TechnicalDecision.NO_MATCH

    local_count = _nonnegative_int(
        parsed.get("torrent_file_count", parsed.get("disc_file_count"))
    )
    remote_count = _candidate_file_count(attrs)
    if local_count is not None and remote_count is not None:
        count_difference = abs(local_count - remote_count)
        if count_difference > max(10, int(min(local_count, remote_count) * 0.10)):
            return TechnicalDecision.NO_MATCH

    if distributor_unknown:
        return TechnicalDecision.INDETERMINATE

    # A global BDInfo report describes at most the selected authored disc.  It
    # cannot prove the contents of a separate opaque ISO root in a mixed pack,
    # nor can it turn a pure ISO path/size fingerprint into content identity.
    # A future positive decision would require independent per-ISO content
    # evidence, which path-size-v2 deliberately does not claim to provide.
    if local_has_opaque_iso:
        return TechnicalDecision.INDETERMINATE

    # Matching BDInfo signatures are strong authored-disc evidence only after
    # the candidate's material size/count fields have passed their vetoes.
    if local_bdinfo and remote_bdinfo:
        return (
            TechnicalDecision.MATCH
            if size_relation in {"exact", "close"}
            else TechnicalDecision.INDETERMINATE
        )

    normalized_local_name = re.sub(
        r"[^a-z0-9]+",
        "",
        unicodedata.normalize("NFKD", str(parsed.get("name") or "")).casefold(),
    )
    normalized_remote_name = re.sub(
        r"[^a-z0-9]+",
        "",
        unicodedata.normalize("NFKD", remote_name).casefold(),
    )
    if (
        normalized_local_name
        and normalized_local_name == normalized_remote_name
        and local_regions
        and local_regions == remote_regions
        and local_editions == remote_editions
    ):
        return TechnicalDecision.MATCH

    # Exact byte size is only strong identity evidence when paired with the same explicit
    # regional edition and a known-equal disc medium.  Missing region tags must
    # never turn an arbitrary same-film Blu-ray into a duplicate.
    editions_compatible = local_editions == remote_editions
    if (
        size_relation == "exact"
        and local_regions
        and local_regions == remote_regions
        and editions_compatible
        and local_medium is not None
        and local_medium == remote_medium
    ):
        return TechnicalDecision.MATCH

    return TechnicalDecision.INDETERMINATE


def technical_decision(
    parsed: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    web_precedent: bool | None = None,
) -> TechnicalDecision:
    """Classify one Aither candidate using a strict, three-state matcher."""

    attrs = _candidate_attributes(candidate)
    remote_name = str(attrs.get("name") or "")
    raw_remote_parsed = parse_release(remote_name) if remote_name else {}
    if _candidate_internal_conflict(attrs, raw_remote_parsed, remote_name):
        return TechnicalDecision.INDETERMINATE

    remote_parsed, episode_conflict = _merge_remote_episode_evidence(
        attrs, raw_remote_parsed
    )
    if episode_conflict:
        return TechnicalDecision.INDETERMINATE
    structured_remote_type, _ = _candidate_release_type(attrs)
    remote_type = structured_remote_type or _normalize_release_type(
        remote_parsed.get("release_type")
    )
    remote_resolution = _candidate_resolution(attrs, remote_parsed)
    remote_hdr, remote_hdr_known, hdr_conflict = _merge_remote_hdr_evidence(
        attrs, remote_parsed, remote_name
    )
    if hdr_conflict:
        return TechnicalDecision.INDETERMINATE
    remote_codec = _candidate_codec(attrs, remote_parsed)
    indeterminate = False

    local_scope = parsed.get("scope")
    remote_scope = remote_parsed.get("scope")
    remote_pack_covers_local = (
        local_scope in {"episode", "multi_episode"}
        and remote_scope == "season_pack"
    )
    local_episodes = tuple(parsed.get("episodes") or ())
    remote_episodes = tuple(remote_parsed.get("episodes") or ())
    remote_multi_covers_local = bool(
        local_scope in {"episode", "multi_episode"}
        and remote_scope == "multi_episode"
        and local_episodes
        and set(local_episodes).issubset(remote_episodes)
    )
    remote_collection_covers_local = (
        remote_pack_covers_local or remote_multi_covers_local
    )
    if local_scope != "movie":
        if not remote_collection_covers_local:
            mismatch, unknown = _compare_known(local_scope, remote_scope)
            if mismatch:
                return TechnicalDecision.NO_MATCH
            indeterminate |= unknown

        mismatch, unknown = _compare_known(parsed.get("season"), remote_parsed.get("season"))
        if mismatch:
            return TechnicalDecision.NO_MATCH
        indeterminate |= unknown

        if (
            local_scope in {"episode", "multi_episode"}
            and not remote_collection_covers_local
        ):
            mismatch, unknown = _compare_known(
                local_episodes or None, remote_episodes or None
            )
            if mismatch:
                return TechnicalDecision.NO_MATCH
            indeterminate |= unknown
    elif remote_scope not in {None, "movie"}:
        return TechnicalDecision.NO_MATCH

    local_type = _normalize_release_type(parsed.get("release_type"))
    mismatch, unknown = _compare_known(local_type, remote_type)
    if mismatch:
        return TechnicalDecision.NO_MATCH
    indeterminate |= unknown

    local_revisions = _revision_markers(parsed.get("name"))
    remote_revisions = _revision_markers(remote_name)
    if (local_revisions or remote_revisions) and local_revisions != remote_revisions:
        return TechnicalDecision.NO_MATCH

    local_material = _material_markers(parsed.get("name"))
    remote_material = _material_markers(remote_name)
    if local_material != remote_material and not _exact_group_payload_match(
        parsed, attrs
    ):
        return TechnicalDecision.NO_MATCH

    local_languages = _language_markers(parsed.get("name"))
    remote_languages = _language_markers(remote_name)
    if local_languages and remote_languages and local_languages != remote_languages:
        return TechnicalDecision.NO_MATCH

    local_editions = _structured_edition_tokens(parsed.get("disc_editions")) or _disc_editions(
        parsed.get("name") or ""
    )
    remote_editions = _structured_edition_tokens(attrs.get("disc_editions")) or _disc_editions(
        remote_name
    )
    if local_editions and remote_editions and local_editions != remote_editions:
        return TechnicalDecision.NO_MATCH

    local_structured_audio = " ".join(
        str(parsed.get(key) or "")
        for key in ("audio", "audio_codec", "audio_format", "audio_channels")
    )
    remote_structured_audio = " ".join(
        str(attrs.get(key) or "")
        for key in ("audio", "audio_codec", "audio_format", "audio_channels")
    )
    local_audio_profiles = _release_audio_profile_evidence(
        " ".join((str(parsed.get("name") or ""), local_structured_audio)),
        parsed.get("media_info"),
        parsed.get("bd_info"),
    )
    remote_audio_profiles = _release_audio_profile_evidence(
        " ".join((remote_name, remote_structured_audio)),
        attrs.get("media_info"),
        attrs.get("bd_info"),
    )
    local_profile_base_formats, _ = _release_audio_evidence(
        " ".join((str(parsed.get("name") or ""), local_structured_audio)),
        parsed.get("media_info"),
        parsed.get("bd_info"),
    )
    remote_profile_base_formats, _ = _release_audio_evidence(
        " ".join((remote_name, remote_structured_audio)),
        attrs.get("media_info"),
        attrs.get("bd_info"),
    )
    audio_profile_conflict, audio_profile_ambiguous = _audio_profile_relation(
        local_audio_profiles,
        remote_audio_profiles,
        local_formats=local_profile_base_formats,
        remote_formats=remote_profile_base_formats,
    )
    if audio_profile_conflict:
        return TechnicalDecision.NO_MATCH

    if local_type == "Full Disc" or remote_type == "Full Disc":
        if local_type != "Full Disc" or remote_type != "Full Disc":
            return TechnicalDecision.INDETERMINATE
        full_disc_result = _full_disc_decision(parsed, attrs, remote_parsed)
        if (
            audio_profile_ambiguous
            and full_disc_result is TechnicalDecision.MATCH
        ):
            return TechnicalDecision.INDETERMINATE
        return full_disc_result

    if bool(local_editions) != bool(remote_editions):
        indeterminate = True

    named_conflict, named_missing = _non_disc_named_evidence_state(
        str(parsed.get("name") or ""), remote_name
    )
    if named_conflict:
        return TechnicalDecision.NO_MATCH

    # A season pack can contain the exact episode payload even though its
    # aggregate torrent size differs from the individual upload.
    if not audio_profile_ambiguous and _season_collection_contains_exact_release(
        parsed,
        attrs,
        remote_parsed,
        remote_type=remote_type,
        remote_resolution=remote_resolution,
        remote_hdr=remote_hdr,
        remote_hdr_known=remote_hdr_known,
        remote_codec=remote_codec,
    ):
        return TechnicalDecision.MATCH

    # Same group plus exact media bytes is release identity, even when one name
    # abbreviates HDR10+ as HDR or omits the HDR fallback next to Dolby Vision.
    if not audio_profile_ambiguous and _same_release_payload_identity(
        parsed,
        attrs,
        remote_parsed,
        remote_type=remote_type,
        remote_resolution=remote_resolution,
        remote_hdr=remote_hdr,
        remote_hdr_known=remote_hdr_known,
        remote_codec=remote_codec,
    ):
        return TechnicalDecision.MATCH
    indeterminate |= audio_profile_ambiguous
    indeterminate |= named_missing

    local_file_sizes = _local_media_file_sizes(parsed)
    remote_file_sizes = _remote_media_file_sizes(attrs)
    local_payload_signature = _local_media_payload_signature(parsed)
    remote_payload_signature = _remote_media_payload_signature(attrs)
    local_payload = _local_media_payload_size(parsed)
    remote_payload = _remote_media_payload_size(attrs)
    if (
        (local_file_sizes and remote_file_sizes and local_file_sizes != remote_file_sizes)
        or bool(local_file_sizes) != bool(remote_file_sizes)
        or (
            local_payload_signature
            and remote_payload_signature
            and local_payload_signature != remote_payload_signature
        )
        or bool(local_payload_signature) != bool(remote_payload_signature)
        or ((local_payload is None) != (remote_payload is None))
        or (
            local_payload is not None
            and remote_payload is not None
            and local_payload != remote_payload
        )
    ):
        # A different payload is not automatically a different allowed release,
        # but it must never be promoted by a relaxed name comparison.
        indeterminate = True

    local_hdr, local_hdr_known = _hdr_evidence(parsed)
    if local_hdr_known and remote_hdr_known:
        if not _hdr_tokens_compatible(local_hdr, remote_hdr):
            return TechnicalDecision.NO_MATCH
        if local_hdr != remote_hdr:
            # Compatible shorthand removes a false veto; it is not positive
            # identity evidence.  Only the exact payload/group proof above may
            # turn this naming difference directly into MATCH.
            indeterminate = True
    elif local_hdr_known != remote_hdr_known:
        # A missing label is unknown, not an explicit SDR declaration.  It
        # prevents an ordinary technical-signature match, while the stricter
        # same-group + exact-payload proof above may still identify the same
        # release bytes.
        indeterminate = True

    local_resolution = _normalize_resolution(parsed.get("resolution"))
    mismatch, unknown = _compare_known(local_resolution, remote_resolution)
    if mismatch:
        return TechnicalDecision.NO_MATCH
    indeterminate |= unknown

    if local_type in {"Encode", "Remux", "WEB-DL", "WEBRip", "HDTV", "Full Disc"}:
        mismatch, unknown = _compare_known(
            parsed.get("medium"), remote_parsed.get("medium")
        )
        if mismatch:
            return TechnicalDecision.NO_MATCH
        indeterminate |= unknown

    if local_type in {"Encode", "Remux", "WEB-DL", "WEBRip", "HDTV"}:
        local_codec = _normalize_codec(parsed.get("codec"))
        mismatch, unknown = _compare_known(local_codec, remote_codec)
        if mismatch:
            return TechnicalDecision.NO_MATCH
        indeterminate |= unknown

    if indeterminate and _distinct_web_release(
        parsed,
        attrs,
        local_type=local_type,
        remote_type=remote_type,
        local_resolution=local_resolution,
        remote_resolution=remote_resolution,
        web_precedent=web_precedent,
    ):
        return TechnicalDecision.NO_MATCH
    return (
        TechnicalDecision.INDETERMINATE
        if indeterminate
        else TechnicalDecision.MATCH
    )


def evaluate_candidates(
    parsed: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> tuple[TechnicalDecision, Mapping[str, Any] | None]:
    """Evaluate every candidate and return immediately only for a real match."""

    saw_indeterminate = False
    web_precedent = (
        _web_parallel_precedent(parsed, candidates)
        if web_policy() == "precedent"
        else None
    )
    for candidate in candidates:
        decision = technical_decision(parsed, candidate, web_precedent=web_precedent)
        if decision is TechnicalDecision.MATCH:
            return decision, candidate
        if decision is TechnicalDecision.INDETERMINATE:
            saw_indeterminate = True

    if saw_indeterminate:
        return TechnicalDecision.INDETERMINATE, None
    return TechnicalDecision.NO_MATCH, None


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _id_values(value: Any, field: str) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        values = [value]
    else:
        values = list(value)
    return [_positive_int(item, field) for item in values]


def _normalize_imdb_id(value: Any) -> int:
    text = str(value).strip()
    match = re.fullmatch(r"(?:tt)?(\d{5,10})", text, re.IGNORECASE)
    if not match:
        raise ValueError("imdb_id must be a numeric IMDb title ID, optionally prefixed by tt")
    return _positive_int(match.group(1), "imdb_id")


def build_aither_filter_params(
    parsed: Mapping[str, Any],
    *,
    tmdb_id: Any = None,
    imdb_id: Any = None,
    category_ids: Any = None,
    type_ids: Any = None,
    resolution_ids: Any = None,
) -> dict[str, Any]:
    """Build one exact UNIT3D/Aither filter request.

    Exactly one identity must be supplied.  IMDb IDs are converted to the
    integer form expected by UNIT3D.  The server-side maximum page size is used
    so the caller can make a technical decision from one relevant response.
    """

    if (tmdb_id is None) == (imdb_id is None):
        raise ValueError("exactly one of tmdb_id or imdb_id is required")

    params: dict[str, Any] = {"perPage": 100}
    if tmdb_id is not None:
        params["tmdbId"] = _positive_int(tmdb_id, "tmdb_id")
    else:
        params["imdbId"] = _normalize_imdb_id(imdb_id)

    # Explicit None checks preserve valid special-season/episode zero values.
    season = parsed.get("season")
    episode = parsed.get("episode")
    if season is not None:
        params["seasonNumber"] = int(season)
    if episode is not None:
        params["episodeNumber"] = int(episode)

    if category_ids is None:
        category_ids = parsed.get("category_ids", parsed.get("category_id"))
    if type_ids is None:
        type_ids = parsed.get("type_ids", parsed.get("type_id"))
    if resolution_ids is None:
        resolution_ids = parsed.get("resolution_ids", parsed.get("resolution_id"))

    categories = _id_values(category_ids, "category_ids")
    types = _id_values(type_ids, "type_ids")
    resolutions = _id_values(resolution_ids, "resolution_ids")
    if categories:
        params["categories[]"] = categories
    if types:
        params["types[]"] = types
    if resolutions:
        params["resolutions[]"] = resolutions

    return params


__all__ = [
    "BLACKLIST_ALL",
    "BLACKLIST_BY_TYPE",
    "BLACKLIST_NO_TYPE_GIVEN",
    "TechnicalDecision",
    "build_aither_filter_params",
    "canonical_title",
    "evaluate_candidates",
    "enrich_release",
    "extract_release_group",
    "is_blacklisted_release",
    "norm_name",
    "normalize_bdinfo_signature",
    "parse_release",
    "release_type_for_blacklist",
    "technical_decision",
]
