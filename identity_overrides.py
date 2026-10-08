"""Auditable catalogue hints bound to exact local payload evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
from pathlib import Path
import re
from typing import Any

from matching import norm_name


class IdentityOverrideError(ValueError):
    pass


_DISC_KINDS = frozenset({"BDMV", "VIDEO_TS", "ISO"})
_PAYLOAD_EXTENSIONS = frozenset(
    {
        "mkv",
        "mp4",
        "avi",
        "m2ts",
        "mts",
        "ts",
        "wmv",
        "mov",
        "mka",
        "m4a",
        "ac3",
        "eac3",
        "dts",
        "dtshd",
        "thd",
        "truehd",
        "flac",
        "aac",
        "wav",
        "mp3",
        "opus",
        "ogg",
        "sup",
        "mks",
        "srt",
        "ass",
        "ssa",
        "sub",
        "idx",
        "vtt",
    }
)
_MAX_PAYLOAD_FILES = 10_000
_MAX_BYTE_SIZE = (1 << 63) - 1
_MANUAL_REASON_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def _legacy_positive_int(value: Any) -> int:
    """Retain the v1 loader's integer coercion for disc compatibility."""

    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise IdentityOverrideError("override IDs and size must be integers") from exc
    if result <= 0:
        raise IdentityOverrideError("invalid identity override evidence")
    return result


def _strict_positive_int(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_BYTE_SIZE
    ):
        raise IdentityOverrideError(f"{field} must be a positive integer")
    return value


def _normalize_payload_signature(
    value: Any,
    *,
    strict_json_shape: bool,
) -> tuple[tuple[str, int], ...] | None:
    """Return an exact extension/size multiset or ``None`` for unsafe input.

    Override JSON uses explicit ``extension``/``size`` objects.  Runtime parsed
    releases use tuples (and JSON-restored state may use lists), so verification
    accepts both representations without accepting scalar or partial entries.
    """

    if (
        isinstance(value, (str, bytes, Mapping))
        or not isinstance(value, Iterable)
    ):
        return None
    entries = list(value)
    if not entries or len(entries) > _MAX_PAYLOAD_FILES:
        return None

    signature: list[tuple[str, int]] = []
    for item in entries:
        if isinstance(item, Mapping):
            if set(item) != {"extension", "size"}:
                return None
            extension = item.get("extension")
            size = item.get("size")
        elif (
            not strict_json_shape
            and isinstance(item, Sequence)
            and not isinstance(item, (str, bytes))
            and len(item) == 2
        ):
            extension, size = item
        else:
            return None

        normalized_extension = str(extension or "").strip().lstrip(".").casefold()
        if (
            normalized_extension not in _PAYLOAD_EXTENSIONS
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > _MAX_BYTE_SIZE
        ):
            return None
        signature.append((normalized_extension, size))
    return tuple(sorted(signature))


def _validated_identity(
    identity: Mapping[str, Any], *, source: str, strict_types: bool
) -> dict[str, Any]:
    if strict_types:
        tmdb_id = _strict_positive_int(identity.get("tmdb_id"), field="tmdb_id")
    else:
        tmdb_id = _legacy_positive_int(identity.get("tmdb_id"))
    imdb_id = str(identity.get("imdb_id") or "")
    media_type = str(identity.get("media_type") or "")
    title = str(identity.get("title") or "").strip()
    year = identity.get("year")
    if (
        not re.fullmatch(r"tt\d{7,10}", imdb_id)
        or media_type not in {"movie", "tv"}
        or not title
        or not isinstance(year, int)
        or isinstance(year, bool)
        or not 1800 <= year <= 2099
    ):
        raise IdentityOverrideError("invalid identity override evidence")
    return {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "source": source,
    }


def _validated_disc_match(
    match: Mapping[str, Any], *, strict_types: bool = False
) -> dict[str, Any]:
    fingerprint = str(match.get("disc_manifest_fingerprint") or "").lower()
    kind = str(match.get("disc_manifest_kind") or "").upper()
    total_size = (
        _strict_positive_int(match.get("disc_total_size"), field="disc_total_size")
        if strict_types
        else _legacy_positive_int(match.get("disc_total_size"))
    )
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or kind not in _DISC_KINDS:
        raise IdentityOverrideError("invalid identity override evidence")
    return {
        "disc_manifest_kind": kind,
        "disc_manifest_fingerprint": fingerprint,
        "disc_total_size": total_size,
    }


def _validated_payload_match(match: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "evidence_type",
        "media_payload_signature",
        "torrent_file_count",
        "disc_total_size",
        "torrent_infohash",
    }
    if set(match) != expected_fields:
        raise IdentityOverrideError("invalid media payload override fields")
    signature = _normalize_payload_signature(
        match.get("media_payload_signature"), strict_json_shape=True
    )
    if signature is None:
        raise IdentityOverrideError("invalid media payload signature")
    file_count = _strict_positive_int(
        match.get("torrent_file_count"), field="torrent_file_count"
    )
    total_size = _strict_positive_int(
        match.get("disc_total_size"), field="disc_total_size"
    )
    infohash = str(match.get("torrent_infohash") or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{40}", infohash) is None:
        raise IdentityOverrideError("invalid torrent infohash")
    if file_count < len(signature) or sum(size for _, size in signature) > total_size:
        raise IdentityOverrideError("inconsistent media payload evidence")
    return {
        "evidence_type": "media_payload",
        "media_payload_signature": signature,
        "torrent_file_count": file_count,
        "disc_total_size": total_size,
        "torrent_infohash": infohash,
    }


def _validated_payload_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"required", "manual_reason"}:
        raise IdentityOverrideError("invalid payload review marker")
    reason = value.get("manual_reason")
    if value.get("required") is not True or not isinstance(reason, str):
        raise IdentityOverrideError("invalid payload review marker")
    reason = reason.strip()
    if _MANUAL_REASON_RE.fullmatch(reason) is None:
        raise IdentityOverrideError("invalid payload review reason")
    return {"required": True, "manual_reason": reason}


def load_identity_overrides(path: Path) -> dict[str, dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityOverrideError("invalid identity override file") from exc
    if (
        not isinstance(payload, Mapping)
        or isinstance(payload.get("version"), bool)
        or not isinstance(payload.get("version"), int)
        or payload.get("version") not in {1, 2}
    ):
        raise IdentityOverrideError("unsupported identity override schema")
    schema_version = int(payload["version"])
    raw_overrides = payload.get("overrides")
    if not isinstance(raw_overrides, Mapping):
        raise IdentityOverrideError("identity overrides must be an object")

    overrides: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in raw_overrides.items():
        key = str(raw_key)
        if not key or norm_name(key) != key or not isinstance(raw_entry, Mapping):
            raise IdentityOverrideError("invalid normalized release override key")
        match = raw_entry.get("match")
        identity = raw_entry.get("identity")
        if not isinstance(match, Mapping) or not isinstance(identity, Mapping):
            raise IdentityOverrideError("override requires match and identity objects")

        if schema_version == 1:
            # V1 is the deployed disc-only shape.  Keep accepting and
            # canonicalising it exactly as before.
            canonical_match = _validated_disc_match(match)
            source = "verified_disc_override"
            if "payload_review" in raw_entry:
                raise IdentityOverrideError("payload review requires schema version 2")
            payload_review = None
        else:
            evidence_type = match.get("evidence_type")
            if evidence_type == "disc_manifest":
                if set(raw_entry) != {"match", "identity"}:
                    raise IdentityOverrideError("invalid disc override entry fields")
                if set(match) != {
                    "evidence_type",
                    "disc_manifest_kind",
                    "disc_manifest_fingerprint",
                    "disc_total_size",
                }:
                    raise IdentityOverrideError("invalid disc override fields")
                canonical_match = _validated_disc_match(match, strict_types=True)
                canonical_match["evidence_type"] = "disc_manifest"
                source = "verified_disc_override"
                if "payload_review" in raw_entry:
                    raise IdentityOverrideError("payload review requires media evidence")
                payload_review = None
            elif evidence_type == "media_payload":
                if set(raw_entry) not in (
                    {"match", "identity"},
                    {"match", "identity", "payload_review"},
                ):
                    raise IdentityOverrideError("invalid media override entry fields")
                canonical_match = _validated_payload_match(match)
                source = "verified_media_payload_override"
                payload_review = (
                    _validated_payload_review(raw_entry["payload_review"])
                    if "payload_review" in raw_entry
                    else None
                )
            else:
                raise IdentityOverrideError("invalid identity override evidence type")

        loaded_entry: dict[str, Any] = {
            "match": canonical_match,
            "identity": _validated_identity(
                identity,
                source=source,
                strict_types=schema_version == 2,
            ),
        }
        if payload_review is not None:
            loaded_entry["payload_review"] = payload_review
        overrides[key] = loaded_entry
    return overrides


def _disc_match_verified(parsed: Mapping[str, Any], match: Mapping[str, Any]) -> bool:
    try:
        same_size = int(parsed.get("disc_total_size") or 0) == int(
            match.get("disc_total_size") or -1
        )
    except (TypeError, ValueError):
        return False
    return bool(
        str(parsed.get("disc_manifest_kind") or "").upper()
        == str(match.get("disc_manifest_kind") or "").upper()
        and str(parsed.get("disc_manifest_fingerprint") or "").lower()
        == str(match.get("disc_manifest_fingerprint") or "").lower()
        and same_size
    )


def _payload_match_verified(
    parsed: Mapping[str, Any], match: Mapping[str, Any], torrent_hash: str | None
) -> bool:
    # A media-payload override must never be a weaker alternate route for a
    # detected disc image/structure.
    if str(parsed.get("disc_manifest_kind") or "").strip():
        return False
    expected_infohash = str(match.get("torrent_infohash") or "").strip().casefold()
    actual_infohash = str(torrent_hash or "").strip().casefold()
    if (
        re.fullmatch(r"[0-9a-f]{40}", expected_infohash) is None
        or re.fullmatch(r"[0-9a-f]{40}", actual_infohash) is None
        or actual_infohash != expected_infohash
    ):
        return False
    expected_signature = _normalize_payload_signature(
        match.get("media_payload_signature"), strict_json_shape=False
    )
    actual_signature = _normalize_payload_signature(
        parsed.get("media_payload_signature"), strict_json_shape=False
    )
    if expected_signature is None or actual_signature is None:
        return False
    try:
        expected_count = _strict_positive_int(
            match.get("torrent_file_count"), field="torrent_file_count"
        )
        actual_count = _strict_positive_int(
            parsed.get("torrent_file_count"), field="torrent_file_count"
        )
        expected_total = _strict_positive_int(
            match.get("disc_total_size"), field="disc_total_size"
        )
        actual_total = _strict_positive_int(
            parsed.get("disc_total_size"), field="disc_total_size"
        )
    except IdentityOverrideError:
        return False
    return bool(
        actual_signature == expected_signature
        and actual_count == expected_count
        and actual_total == expected_total
    )


def verified_identity_hint(
    release_key: str,
    parsed: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, Any]],
    *,
    torrent_hash: str | None = None,
) -> dict[str, Any] | None:
    entry = overrides.get(release_key)
    if not isinstance(entry, Mapping):
        return None
    match = entry.get("match")
    identity = entry.get("identity")
    if not isinstance(match, Mapping) or not isinstance(identity, Mapping):
        return None

    evidence_type = match.get("evidence_type")
    if evidence_type == "media_payload":
        verified = _payload_match_verified(parsed, match, torrent_hash)
    elif evidence_type in {None, "disc_manifest"}:
        verified = _disc_match_verified(parsed, match)
    else:
        return None
    if not verified:
        return None

    hint = dict(identity)
    payload_review = entry.get("payload_review")
    if evidence_type == "media_payload" and isinstance(payload_review, Mapping):
        # Keep review policy distinct from catalogue identity while returning
        # it through the existing hint API for the caller's later integration.
        hint["payload_review"] = dict(payload_review)
    return hint


__all__ = [
    "IdentityOverrideError",
    "load_identity_overrides",
    "verified_identity_hint",
]
