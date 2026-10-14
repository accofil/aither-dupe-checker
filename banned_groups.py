"""Keep the release-group blacklist in step with Aither's own banned list.

The static lists in ``matching.py`` are a snapshot of Upload-Assistant's cache
and drift (new bans such as FENiX/JFF/PAAI/BlackTV were missing, ``VD0N`` and
``UnKn0wn`` were typed with letter O).  The tracker publishes the list at
``/api/blacklists/releasegroups``; this module downloads it at most once a
day, keeps a private cache and falls back to the static snapshot whenever the
list cannot be obtained, so an outage never weakens the blacklist.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests

# UNIT3D type ids used by Aither.
TYPE_NAMES = {
    "1": "Full Disc",
    "2": "Remux",
    "3": "Encode",
    "4": "WEB-DL",
    "5": "WEBRip",
    "6": "HDTV",
}
MIN_PLAUSIBLE_ENTRIES = 20
MAX_PAGES = 30


@dataclass(frozen=True)
class BannedGroups:
    all_types: frozenset[str]
    no_type_given: frozenset[str]
    by_type: Mapping[str, frozenset[str]]

    @property
    def size(self) -> int:
        return len(self.all_types) + len(self.no_type_given) + len(self.by_type)


def parse_banned_groups(raw_items: Sequence[Mapping[str, Any]]) -> BannedGroups:
    """Map Aither's records to the checker's three blacklist shapes."""

    all_types: set[str] = set()
    no_type: set[str] = set()
    by_type: dict[str, set[str]] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip().casefold()
        if not name:
            continue
        types = item.get("types")
        if types == "all":
            all_types.add(name)
            continue
        ids = types.get("types") if isinstance(types, Mapping) else None
        names = {
            TYPE_NAMES[str(type_id)]
            for type_id in (ids or ())
            if str(type_id) in TYPE_NAMES
        }
        if names:
            by_type.setdefault(name, set()).update(names)
        else:
            # No (known) type given: Aither's conservative reading is "all".
            no_type.add(name)
    return BannedGroups(
        frozenset(all_types),
        frozenset(no_type),
        {name: frozenset(values) for name, values in by_type.items()},
    )


def fetch_banned_groups(
    base_url: str,
    token: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 30.0,
    per_page: int = 100,
) -> list[dict[str, Any]] | None:
    """Download every page of the list; ``None`` on any failure (never partial)."""

    http = session or requests.Session()
    if session is None:
        http.trust_env = False
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    url = f"{base_url.rstrip('/')}/api/blacklists/releasegroups"
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"per_page": per_page}
        if cursor:
            params["cursor"] = cursor
        try:
            response = http.get(url, headers=headers, params=params, timeout=timeout)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
            return items
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return None
        items.extend(item for item in payload["data"] if isinstance(item, dict))
        meta = payload.get("meta")
        cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        if not cursor:
            return items
    return None


def _read_cache(path: Path) -> tuple[list[dict[str, Any]], float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched = float(data["fetched_at"])
        raw = data["raw_data"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(raw, list):
        return None
    return [item for item in raw if isinstance(item, dict)], fetched


def _write_cache(path: Path, raw: list[dict[str, Any]], now: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"fetched_at": now, "raw_data": raw}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_banned_groups(
    cache_path: Path | str,
    base_url: str,
    token: str,
    *,
    max_age_seconds: float = 24 * 3600,
    now: float | None = None,
    fetch: Callable[..., list[dict[str, Any]] | None] = fetch_banned_groups,
) -> tuple[BannedGroups | None, str]:
    """Return ``(groups, source)``; ``source`` is api | cache | stale-cache | none."""

    cache_path = Path(cache_path)
    moment = time.time() if now is None else now
    cached = _read_cache(cache_path)
    if cached is not None and moment - cached[1] <= max_age_seconds:
        groups = parse_banned_groups(cached[0])
        if groups.size >= MIN_PLAUSIBLE_ENTRIES:
            return groups, "cache"
    raw = fetch(base_url, token)
    if raw is not None:
        groups = parse_banned_groups(raw)
        if groups.size >= MIN_PLAUSIBLE_ENTRIES:
            try:
                _write_cache(cache_path, raw, moment)
            except OSError:
                pass
            return groups, "api"
    if cached is not None:
        groups = parse_banned_groups(cached[0])
        if groups.size >= MIN_PLAUSIBLE_ENTRIES:
            return groups, "stale-cache"
    return None, "none"
