#!/usr/bin/env python3
"""Manual qBittorrent -> Aither duplicate checker.

The command performs at most one TMDb-ID Aither lookup per release and, only
when that response is empty, one IMDb-ID fallback.  Search decisions and media
preparation state live in one durable JSON file so a terminal release is never
looked up again.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import unicodedata
from urllib.parse import urlsplit

import requests

from matching import (
    TechnicalDecision,
    _audio_evidence,
    _audio_profile_evidence,
    _structured_edition_evidence,
    _structured_region_evidence,
    canonical_title,
    enrich_release,
    evaluate_candidates,
    extract_release_group,
    is_blacklisted_release,
    norm_name,
    parse_release,
)
from identity import (
    IMDbSuggestionClient,
    IMDbSuggestionError,
    TitleVariant,
    classify_iso_listing,
    classify_manifest,
    detect_source_aither,
    normalize_imdb_id as identity_normalize_imdb_id,
    release_identity_variants,
    scan_local_metadata,
    scan_matroska_metadata,
)
from identity_overrides import (
    IdentityOverrideError,
    load_identity_overrides,
    verified_identity_hint,
)
from state_store import (
    AitherRateGate,
    AlreadyRunningError,
    SingleProcessLock,
    StateError,
    StateStore,
    build_query_key,
)


RAR_CONTINUATION_RE = re.compile(r"(?i)\.r\d{2,3}$")
PART_RAR_RE = re.compile(r"(?i)\.part\d+\.rar$")

CATEGORY_IDS = {"movie": 1, "tv": 2}

DECISION_VERSION = 4
QUERY_POLICY_VERSION = 3
AITHER_EVIDENCE_SCHEMA_VERSION = 3
MATCHER_VERSION = 3
IDENTITY_RESOLVER_VERSION = 2
AUTOMATIC_IDENTITY_RECHECK_REASON = "IDENTITY_RESOLVER_PRE_AITHER_RECHECK"


class CheckerError(RuntimeError):
    pass


class ConfigurationError(CheckerError):
    pass


class ProviderError(CheckerError):
    pass


class AitherError(ProviderError):
    pass


class QbitError(CheckerError):
    pass


@dataclass(frozen=True)
class Config:
    qb_url: str
    qb_user: str
    qb_pass: str = field(repr=False)
    aither_base: str = "https://aither.cc"
    aither_token: str = field(default="", repr=False)
    tmdb_api_key: str = field(default="", repr=False)
    log_file: Path = Path.home() / "aither_missing.log"
    state_file: Path = Path.home() / "aither_checked_seen.json"
    ready_list_file: Path = Path.home() / "aither_upload_ready.txt"
    upload_root: Path = Path.home() / "torrents/qbittorrent/up"
    source_roots: tuple[Path, ...] = (
        Path.home() / "storage",
        Path.home() / "torrents/qbittorrent",
    )
    request_interval: float = 5.0
    batch_pause: float = 30.0
    request_timeout: float = 30.0
    lock_file: Path = Path.home() / ".aithercheck.lock"
    identity_overrides_file: Path = Path(__file__).resolve().with_name(
        "identity_overrides.json"
    )

    @classmethod
    def from_environment(cls) -> "Config":
        required = (
            "QB_URL",
            "QB_USER",
            "QB_PASS",
            "AITHER_TOKEN",
            "TMDB_API_KEY",
        )
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ConfigurationError(
                "Hiányzó konfigurációs kulcs(ok): " + ", ".join(missing)
            )

        qb_url = normalize_base_url(os.environ["QB_URL"], "QB_URL")
        aither_base = normalize_base_url(
            os.getenv("AITHER_BASE", "https://aither.cc"), "AITHER_BASE"
        )

        def nonnegative_float(name: str, default: str) -> float:
            try:
                value = float(os.getenv(name, default))
            except ValueError as exc:
                raise ConfigurationError(f"{name} nem szám") from exc
            if value < 0:
                raise ConfigurationError(f"{name} nem lehet negatív")
            return value

        return cls(
            qb_url=qb_url,
            qb_user=os.environ["QB_USER"],
            qb_pass=os.environ["QB_PASS"],
            aither_base=aither_base,
            aither_token=os.environ["AITHER_TOKEN"],
            tmdb_api_key=os.environ["TMDB_API_KEY"],
            log_file=Path(
                os.getenv("LOG_FILE", str(Path.home() / "aither_missing.log"))
            ).expanduser(),
            state_file=Path(
                os.getenv(
                    "STATE_FILE", str(Path.home() / "aither_checked_seen.json")
                )
            ).expanduser(),
            ready_list_file=Path(
                os.getenv(
                    "READY_LIST_FILE",
                    str(Path.home() / "aither_upload_ready.txt"),
                )
            ).expanduser(),
            upload_root=Path(
                os.getenv(
                    "UPLOAD_ROOT", str(Path.home() / "torrents/qbittorrent/up")
                )
            ).expanduser(),
            identity_overrides_file=Path(
                os.getenv(
                    "IDENTITY_OVERRIDES_FILE",
                    str(Path(__file__).resolve().with_name("identity_overrides.json")),
                )
            ).expanduser(),
            request_interval=nonnegative_float("AITHER_REQUEST_INTERVAL", "5"),
            batch_pause=nonnegative_float("AITHER_BATCH_PAUSE", "30"),
            request_timeout=nonnegative_float("AITHER_REQUEST_TIMEOUT", "30"),
        )


def normalize_base_url(value: str, label: str) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"Érvénytelen {label}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(f"A {label} nem tartalmazhat hitelesítést/queryt")
    return str(value).strip().rstrip("/")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class QbitClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        # Never send local qB credentials through inherited proxy settings.
        self.session.trust_env = False

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v2/{path.lstrip('/')}"

    def login(self) -> None:
        try:
            response = self.session.post(
                self._url("auth/login"),
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise QbitError("qBittorrent hálózati/bejelentkezési hiba") from None
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise QbitError(
                f"qBittorrent bejelentkezési hiba: HTTP {response.status_code}"
            )

    def torrents(
        self,
        filter_name: str = "all",
        hashes: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        params = {"filter": filter_name}
        if hashes:
            params["hashes"] = "|".join(hashes)
        try:
            response = self.session.get(
                self._url("torrents/info"), params=params, timeout=self.timeout
            )
        except requests.RequestException:
            raise QbitError("qBittorrent torrentlista-hálózati hiba") from None
        if response.status_code != 200:
            raise QbitError(
                f"qBittorrent torrentlista-hiba: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise QbitError("A qBittorrent torrentlistája nem JSON") from None
        if not isinstance(payload, list):
            raise QbitError("A qBittorrent torrentlistája hibás formátumú")
        return [dict(item) for item in payload if isinstance(item, Mapping)]

    def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        try:
            response = self.session.get(
                self._url("torrents/files"),
                params={"hash": torrent_hash},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise QbitError("qBittorrent fájllista-hálózati hiba") from None
        if response.status_code != 200:
            raise QbitError(
                f"qBittorrent fájllista-hiba: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise QbitError("A qBittorrent fájllistája nem JSON") from None
        if not isinstance(payload, list):
            raise QbitError("A qBittorrent fájllistája hibás formátumú")
        return [dict(item) for item in payload if isinstance(item, Mapping)]

    def properties(self, torrent_hash: str) -> dict[str, Any]:
        """Return qB properties without rendering tracker/comment values."""

        try:
            response = self.session.get(
                self._url("torrents/properties"),
                params={"hash": torrent_hash},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise QbitError("qBittorrent tulajdonság-hálózati hiba") from None
        if response.status_code != 200:
            raise QbitError(
                f"qBittorrent tulajdonság-hiba: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise QbitError("A qBittorrent tulajdonságválasza nem JSON") from None
        if not isinstance(payload, Mapping):
            raise QbitError("A qBittorrent tulajdonságválasza hibás formátumú")
        return dict(payload)

    def trackers(self, torrent_hash: str) -> list[dict[str, Any]]:
        """Return tracker records for in-memory source detection only."""

        try:
            response = self.session.get(
                self._url("torrents/trackers"),
                params={"hash": torrent_hash},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise QbitError("qBittorrent trackerlista-hálózati hiba") from None
        if response.status_code != 200:
            raise QbitError(
                f"qBittorrent trackerlista-hiba: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise QbitError("A qBittorrent trackerlistája nem JSON") from None
        if not isinstance(payload, list):
            raise QbitError("A qBittorrent trackerlistája hibás formátumú")
        return [dict(item) for item in payload if isinstance(item, Mapping)]

    def _post_hashes(self, endpoint: str, hashes: Sequence[str], **data: str) -> None:
        unique_hashes = sorted({str(item) for item in hashes if item})
        if not unique_hashes:
            raise QbitError("Üres qBittorrent hash-lista")
        payload = {"hashes": "|".join(unique_hashes), **data}
        try:
            response = self.session.post(
                self._url(endpoint), data=payload, timeout=self.timeout
            )
        except requests.RequestException:
            raise QbitError(f"qBittorrent {endpoint} hálózati hiba") from None
        if response.status_code != 200:
            raise QbitError(
                f"qBittorrent {endpoint} hiba: HTTP {response.status_code}"
            )

    def stop(self, hashes: Sequence[str]) -> None:
        unique_hashes = sorted({str(item) for item in hashes if item})
        self._post_hashes("torrents/stop", unique_hashes)
        deadline = time.monotonic() + max(5.0, self.timeout)
        while True:
            live = self.torrents("all", unique_hashes)
            states = {
                str(item.get("hash")): str(item.get("state") or "").casefold()
                for item in live
                if item.get("hash")
            }
            if set(states) == set(unique_hashes) and all(
                state.startswith(("stopped", "paused"))
                for state in states.values()
            ):
                return
            if time.monotonic() >= deadline:
                raise QbitError(
                    "A qBittorrent nem állította le időben az összes release-t"
                )
            time.sleep(0.25)

    def start(self, hashes: Sequence[str]) -> None:
        self._post_hashes("torrents/start", hashes)

    def remove_list_only(self, hashes: Sequence[str]) -> None:
        # Hard safety invariant: this program never asks qBittorrent to delete data.
        self._post_hashes("torrents/delete", hashes, deleteFiles="false")


class TmdbClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self._alternative_titles_cache: dict[tuple[str, int], tuple[str, ...]] = {}
        self._search_results_cache: dict[
            tuple[str, str, int | None], tuple[tuple[Any, ...], bool]
        ] = {}
        self._episode_identity_cache: dict[
            tuple[str, int | None, int, int, str, str], dict[str, Any]
        ] = {}

    @staticmethod
    def _episode_cache_key(
        title: str, parsed: Mapping[str, Any]
    ) -> tuple[str, int | None, int, int, str, str] | None:
        episode_title = str(parsed.get("episode_title") or "").strip()
        group = extract_release_group(str(parsed.get("name") or "")).casefold()
        season = parsed.get("season")
        episode = parsed.get("episode")
        if (
            not episode_title
            or not group
            or isinstance(season, bool)
            or isinstance(episode, bool)
        ):
            return None
        try:
            season_number = int(season)
            episode_number = int(episode)
        except (TypeError, ValueError):
            return None
        if season_number < 0 or episode_number < 0:
            return None
        year = _safe_year(parsed.get("year"))
        show_key = canonical_title(title)
        episode_key = canonical_title(episode_title)
        return (
            (show_key, year, season_number, episode_number, episode_key, group)
            if show_key and episode_key
            else None
        )

    def _identity_from_episode_context(
        self,
        title: str,
        parsed: Mapping[str, Any],
        results: Sequence[Any],
    ) -> dict[str, Any] | None:
        """Disambiguate same-named TV shows with an exact episode title."""

        cache_key = self._episode_cache_key(title, parsed)
        episode_title = str(parsed.get("episode_title") or "").strip()
        if cache_key is None or not episode_title:
            return None
        cache = getattr(self, "_episode_identity_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._episode_identity_cache = cache
        cached = cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        canonical_show = canonical_title(title)
        eligible: dict[int, Mapping[str, Any]] = {}
        for item in results:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            names = (item.get("name"), item.get("original_name"))
            if canonical_show not in {
                canonical_title(str(value)) for value in names if value
            }:
                continue
            try:
                eligible[int(item["id"])] = item
            except (TypeError, ValueError):
                continue
        # Bound provider use and reject an excessively ambiguous catalogue set.
        if not 1 <= len(eligible) <= 8:
            return None

        season_number = int(parsed["season"])
        episode_number = int(parsed["episode"])
        exact: dict[int, dict[str, Any]] = {}
        wanted_episode = canonical_title(episode_title)
        for tmdb_id, item in eligible.items():
            try:
                payload = self._get_json(
                    "https://api.themoviedb.org/3/tv/"
                    f"{tmdb_id}/season/{season_number}/episode/{episode_number}",
                    {"language": "en-US"},
                )
            except ProviderError as exc:
                if str(exc) == "TMDb HTTP hiba: 404":
                    continue
                raise
            remote_title = str(payload.get("name") or "")
            if canonical_title(remote_title) != wanted_episode:
                continue
            for field, expected in (
                ("season_number", season_number),
                ("episode_number", episode_number),
            ):
                value = payload.get(field)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value != expected
                ):
                    break
            else:
                exact[tmdb_id] = {
                    "tmdb_id": tmdb_id,
                    "media_type": "tv",
                    "title": item.get("name") or title,
                }
        if len(exact) != 1:
            return None
        identity = next(iter(exact.values()))
        cache[cache_key] = dict(identity)
        return identity

    def _get_json(self, url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        safe_params = {**params, "api_key": self.api_key}
        try:
            response = self.session.get(url, params=safe_params, timeout=self.timeout)
        except requests.RequestException:
            # Do not stringify Requests exceptions: their URL contains api_key.
            raise ProviderError("TMDb hálózati hiba") from None
        if response.status_code != 200:
            raise ProviderError(f"TMDb HTTP hiba: {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("A TMDb válasza nem JSON") from None
        if not isinstance(payload, dict):
            raise ProviderError("A TMDb válasza hibás formátumú")
        return payload

    def search(self, parsed: Mapping[str, Any]) -> dict[str, Any] | None:
        title = str(parsed.get("title") or "").strip()
        if not title:
            return None

        episode_cache_key = self._episode_cache_key(title, parsed)
        if episode_cache_key is not None:
            cache = getattr(self, "_episode_identity_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._episode_identity_cache = cache
            cached_episode = cache.get(episode_cache_key)
            if cached_episode is not None:
                return dict(cached_episode)

        # The caller's parsed media type is authoritative and the final
        # identity guard rejects the opposite namespace anyway.  Querying the
        # other endpoint can only spend a provider request or create noise.
        preferred = "tv" if parsed.get("media_type") == "tv" else "movie"
        for endpoint in (preferred,):
            params: dict[str, Any] = {
                "query": title,
                "language": "en-US",
            }
            year = parsed.get("year")
            if year is not None:
                params["year" if endpoint == "movie" else "first_air_date_year"] = int(year)
            search_key = (endpoint, canonical_title(title), _safe_year(year))
            search_cache = getattr(self, "_search_results_cache", None)
            if not isinstance(search_cache, dict):
                search_cache = {}
                self._search_results_cache = search_cache
            cached_page = search_cache.get(search_key)
            if cached_page is None:
                payload = self._get_json(
                    f"https://api.themoviedb.org/3/search/{endpoint}", params
                )
                raw_results = payload.get("results")
                if not isinstance(raw_results, list):
                    raw_results = []
                page_values = (
                    payload.get("page"),
                    payload.get("total_pages"),
                    payload.get("total_results"),
                )
                page_complete = bool(
                    all(
                        isinstance(value, int) and not isinstance(value, bool)
                        for value in page_values
                    )
                    and page_values[0] == 1
                    and page_values[1] in {0, 1}
                    and page_values[2] == len(raw_results)
                    and (page_values[1] == 1 or not raw_results)
                )
                cached_page = (tuple(raw_results), page_complete)
                search_cache[search_key] = cached_page
            cached_results, page_complete = cached_page
            results = list(cached_results)
            # Never call a first-page candidate globally unique when TMDb says
            # more pages exist (or omits the pagination proof).  Falling back
            # to strict IMDb resolution is cheaper and safer than paging.
            if not page_complete or not results:
                continue
            selected = select_tmdb_result(title, year, results, endpoint)
            if selected is not None:
                return selected
            if endpoint == "tv" and year is None:
                episode_identity = self._identity_from_episode_context(
                    title, parsed, results
                )
                if episode_identity is not None:
                    return episode_identity
            # TMDb search itself knows localized/AKA labels which are not
            # always returned in the search row.  For a small, exact-year
            # candidate set, confirm the local label against TMDb's official
            # alternative-title list instead of accepting a fuzzy result.
            exact_year = _safe_year(year)
            same_year = [
                item
                for item in results
                if exact_year is not None
                and isinstance(item, Mapping)
                and item.get("id") is not None
                and _tmdb_result_year(item) == exact_year
            ]
            alias_matches: list[Mapping[str, Any]] = []
            if 0 < len(same_year) <= 5:
                for item in same_year:
                    names = [
                        item.get("title"),
                        item.get("name"),
                        item.get("original_title"),
                        item.get("original_name"),
                        *self.alternative_titles(endpoint, int(item["id"])),
                    ]
                    if _catalog_alias_matches(title, names):
                        alias_matches.append(item)
            if len(alias_matches) == 1:
                item = alias_matches[0]
                return {
                    "tmdb_id": int(item["id"]),
                    "media_type": endpoint,
                    "title": item.get("title") or item.get("name") or title,
                }
        return None

    def alternative_titles(self, media_type: str, tmdb_id: int) -> tuple[str, ...]:
        if media_type not in {"movie", "tv"}:
            return ()
        key = (media_type, int(tmdb_id))
        cache = getattr(self, "_alternative_titles_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._alternative_titles_cache = cache
        if key in cache:
            return cache[key]
        payload = self._get_json(
            f"https://api.themoviedb.org/3/{media_type}/{key[1]}/alternative_titles",
            {},
        )
        values = payload.get("titles" if media_type == "movie" else "results")
        if not isinstance(values, list):
            cache[key] = ()
            return cache[key]
        cache[key] = tuple(
            str(item.get("title"))
            for item in values
            if isinstance(item, Mapping) and item.get("title")
        )
        return cache[key]

    def identity_covers_variants(
        self,
        identity: Mapping[str, Any],
        variants: Sequence[TitleVariant],
    ) -> bool:
        media_type = str(identity.get("media_type") or "")
        tmdb_id = identity.get("tmdb_id")
        if media_type not in {"movie", "tv"} or tmdb_id is None:
            return False
        names = (
            str(identity.get("title") or ""),
            *self.alternative_titles(media_type, int(tmdb_id)),
        )
        return bool(variants) and all(
            _catalog_alias_matches(variant.title, names) for variant in variants
        )

    def external_imdb_id(self, identity: Mapping[str, Any]) -> str | None:
        tmdb_id = identity.get("tmdb_id")
        media_type = identity.get("media_type")
        if not tmdb_id or media_type not in {"movie", "tv"}:
            return None
        payload = self._get_json(
            f"https://api.themoviedb.org/3/{media_type}/{int(tmdb_id)}/external_ids",
            {},
        )
        return normalize_imdb_id(payload.get("imdb_id"))

    def find_by_imdb(
        self, imdb_id: str, media_type: str
    ) -> dict[str, Any] | None:
        """Map an exact IMDb title ID back to TMDb when TMDb knows it."""

        normalized = normalize_imdb_id(imdb_id)
        if normalized is None:
            return None
        payload = self._get_json(
            f"https://api.themoviedb.org/3/find/{normalized}",
            {"external_source": "imdb_id", "language": "en-US"},
        )
        preferred = "tv" if media_type == "tv" else "movie"
        matches: list[tuple[str, Mapping[str, Any]]] = []
        for kind in (preferred, "movie" if preferred == "tv" else "tv"):
            values = payload.get(f"{kind}_results")
            if not isinstance(values, list):
                continue
            matches.extend(
                (kind, item)
                for item in values
                if isinstance(item, Mapping) and item.get("id") is not None
            )
        unique = {
            (kind, int(item["id"])): item for kind, item in matches
        }
        if len(unique) != 1:
            return None
        (kind, tmdb_id), item = next(iter(unique.items()))
        return {
            "tmdb_id": tmdb_id,
            "media_type": kind,
            "title": item.get("title") or item.get("name") or "",
        }


def _safe_year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1800 <= year <= 2099 else None


def _tmdb_result_year(item: Mapping[str, Any]) -> int | None:
    date = str(item.get("release_date") or item.get("first_air_date") or "")
    return int(date[:4]) if re.match(r"^\d{4}", date) else None


def _catalog_alias_matches(local_title: str, names: Sequence[Any]) -> bool:
    local = canonical_title(local_title)
    if not local:
        return False
    local_label = re.sub(r"\s+", " ", str(local_title)).strip().casefold()
    local_tokens = _catalog_word_tokens(local_title)
    for value in names:
        raw_candidate = re.sub(r"\s+", " ", str(value or "")).strip()
        candidate = canonical_title(raw_candidate)
        if not candidate:
            continue
        if candidate == local:
            return True
        candidate_tokens = _catalog_word_tokens(raw_candidate)
        local_without_and = tuple(token for token in local_tokens if token != "and")
        candidate_without_and = tuple(
            token for token in candidate_tokens if token != "and"
        )
        if (
            len(local_without_and) >= 2
            and local_without_and == candidate_without_and
            and local_tokens != candidate_tokens
            and (("and" in local_tokens) != ("and" in candidate_tokens))
        ):
            return True
        # A colon-delimited subtitle is a conventional expansion of the same
        # title ("Hellevator" -> "Hellevator: The Bottled Fools").  Generic
        # word-prefix overlap is deliberately rejected ("Crash" !=
        # "Crash Pad").
        if (
            len(local) >= 5
            and local_label
            and raw_candidate.casefold().startswith(local_label + ":")
            and candidate.startswith(local)
        ):
            return True
    return False


def _catalog_word_tokens(value: Any) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(value)).casefold()
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    normalized = normalized.replace("&", " and ")
    return tuple(re.findall(r"[a-z0-9]+", normalized))


def select_tmdb_result(
    local_title: str,
    local_year: Any,
    results: Sequence[Any],
    media_type: str,
) -> dict[str, Any] | None:
    local_canonical = canonical_title(local_title)
    if not local_canonical:
        return None
    exact_year = _safe_year(local_year)
    eligible: dict[int, Mapping[str, Any]] = {}
    for raw in results:
        if not isinstance(raw, Mapping) or raw.get("id") is None:
            continue
        names = [raw.get("title"), raw.get("name"), raw.get("original_title"), raw.get("original_name")]
        canonical_names = [canonical_title(str(item)) for item in names if item]
        exact_title = local_canonical in canonical_names
        remote_year = _tmdb_result_year(raw)
        # A wrong TMDb identity would turn an exact Aither lookup into a false
        # NOT_FOUND.  Canonical punctuation/article variants are exact here;
        # all other spellings must be corroborated by TMDb's official alias
        # endpoint in TmdbClient.search rather than a fuzzy similarity score.
        if not exact_title:
            continue
        if exact_year is not None and remote_year != exact_year:
            continue
        try:
            eligible[int(raw["id"])] = raw
        except (TypeError, ValueError):
            continue

    if len(eligible) != 1:
        return None
    selected = next(iter(eligible.values()))
    return {
        "tmdb_id": int(selected["id"]),
        "media_type": media_type,
        "title": selected.get("title") or selected.get("name") or local_title,
    }


_AITHER_EVIDENCE_FIELDS = frozenset(
    {
        "name",
        "type",
        "type_id",
        "typeId",
        "category",
        "category_id",
        "categoryId",
        "resolution",
        "resolution_id",
        "codec",
        "audio",
        "audio_codec",
        "audio_format",
        "audio_channels",
        "bit_depth",
        "bitDepth",
        "size",
        "total_size",
        "disc_size",
        "files",
        "tmdb_id",
        "imdb_id",
        "season_number",
        "seasonNumber",
        "episode_number",
        "episodeNumber",
        "bd_info",
        "media_info",
        "hdr",
        "disc_regions",
        "disc_editions",
        "disc_manifest_fingerprint",
        "bdmv_manifest_fingerprint",
        "manifest_fingerprint",
        "disc_manifest_version",
        "disc_manifest_kind",
    }
)

# Keep this independent list beside the sanitizer whitelist.  The invariant
# test requires every newly persisted matcher field to be added deliberately
# to the common live/cache schema validator as well.
_AITHER_VALIDATED_EVIDENCE_FIELDS = frozenset(
    {
        "name", "type", "type_id", "typeId", "category", "category_id",
        "categoryId", "resolution", "resolution_id", "codec", "audio",
        "audio_codec", "audio_format", "audio_channels", "bit_depth",
        "bitDepth", "size", "total_size", "disc_size", "files", "tmdb_id",
        "imdb_id", "season_number", "seasonNumber", "episode_number",
        "episodeNumber", "bd_info", "media_info", "hdr", "disc_regions",
        "disc_editions", "disc_manifest_fingerprint",
        "bdmv_manifest_fingerprint", "manifest_fingerprint",
        "disc_manifest_version", "disc_manifest_kind",
    }
)


def _safe_evidence_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_evidence_value(item)
            for key, item in value.items()
            if str(key).casefold()
            not in {
                "authorization", "details_link", "download_link", "magnet",
                "magnet_link", "passkey", "password", "token",
            }
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_evidence_value(item) for item in value]
    return str(value)


def sanitize_aither_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only matcher/audit facts; never tracker links or credentials."""

    if "attributes" in candidate:
        attrs = candidate.get("attributes")
        if not isinstance(attrs, Mapping):
            raise AitherError(
                "Az Aither talalat attributes mezoje nem objektum"
            )
    else:
        # UNIT3D normally nests torrent facts below ``attributes``.  Retain
        # support for an explicitly flat response, but never reinterpret a
        # present corrupt attributes value as that legacy shape.
        attrs = candidate
    safe_attrs = {
        key: _safe_evidence_value(value)
        for key, value in attrs.items()
        if str(key) in _AITHER_EVIDENCE_FIELDS
    }
    safe: dict[str, Any] = {"attributes": safe_attrs}
    if candidate.get("id") is not None:
        safe["id"] = _safe_evidence_value(candidate.get("id"))
    return safe


@dataclass(frozen=True)
class AitherPage:
    data: tuple[dict[str, Any], ...]
    has_next: bool
    complete: bool = True
    cache_key: str | None = None
    from_cache: bool = False
    evidence_schema_version: int | None = AITHER_EVIDENCE_SCHEMA_VERSION
    evidence_digest: str | None = None
    requested_per_page: int | None = 100
    attempt_nonce: str | None = None


def _strict_current_version(value: Any, expected: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value == expected
    )


def _aither_page_digest(
    data: Sequence[Mapping[str, Any]],
    *,
    has_next: bool,
    complete: bool,
    evidence_schema_version: int | None,
    requested_per_page: int | None = 100,
    attempt_nonce: str | None = None,
) -> str:
    """Bind a release decision to one exact sanitized query generation."""

    payload = {
        "query_policy": QUERY_POLICY_VERSION,
        "evidence_schema_version": evidence_schema_version,
        "requested_per_page": requested_per_page,
        "attempt_nonce": attempt_nonce,
        "data": list(data),
        "has_next": has_next,
        "complete": complete,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise AitherError("Az Aither evidence oldal nem kanonizalhato") from None
    return hashlib.sha256(encoded).hexdigest()


def _valid_evidence_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_attempt_nonce(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _pagination_state(
    payload: Mapping[str, Any], data_count: int, *, expected_per_page: int
) -> tuple[bool, bool]:
    """Return (has_next, complete) only for internally consistent metadata."""

    if (
        isinstance(expected_per_page, bool)
        or not isinstance(expected_per_page, int)
        or expected_per_page < 1
        or data_count < 0
        or data_count > expected_per_page
    ):
        raise AitherError("Az Aither lapozasi elemszama ervenytelen")

    links_present = "links" in payload
    links = payload.get("links")
    if links_present and not isinstance(links, Mapping):
        raise AitherError("Az Aither lapozasi links mezoje hibas")

    link_known = isinstance(links, Mapping) and "next" in links
    link_has_next = False
    if link_known:
        raw_next = links.get("next")
        if raw_next is None:
            link_has_next = False
        elif isinstance(raw_next, str) and raw_next.strip():
            link_has_next = True
        else:
            raise AitherError("Az Aither next lapozasi erteke hibas")

    meta_present = "meta" in payload
    meta = payload.get("meta")
    if meta_present and not isinstance(meta, Mapping):
        raise AitherError("Az Aither lapozasi meta mezoje hibas")

    meta_known = False
    meta_has_next = False
    if isinstance(meta, Mapping):
        full_required = ("current_page", "last_page", "total", "per_page")
        if all(key in meta for key in full_required):
            values = [meta[key] for key in full_required]
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in values
            ):
                raise AitherError("Az Aither lapozasi metaadata hibas")
            current_page, last_page, total, per_page = values
            if (
                current_page != 1
                or last_page < 1
                or total < 0
                or per_page < 1
                or per_page != expected_per_page
                or current_page > last_page
            ):
                raise AitherError("Az Aither lapozasi tartomanya hibas")
            expected_last = max(1, (total + per_page - 1) // per_page)
            offset = (current_page - 1) * per_page
            expected_count = min(per_page, max(total - offset, 0))
            if last_page != expected_last or data_count != expected_count:
                raise AitherError("Az Aither lapozasi elemszama ellentmondasos")
            range_present = "from" in meta or "to" in meta
            if range_present:
                if "from" not in meta or "to" not in meta:
                    raise AitherError("Az Aither lapozasi tartomanya hianyos")
                first_item, last_item = meta["from"], meta["to"]
                if (
                    isinstance(first_item, bool)
                    or not isinstance(first_item, int)
                    or isinstance(last_item, bool)
                    or not isinstance(last_item, int)
                    or first_item != 1
                    or last_item != data_count
                ):
                    raise AitherError("Az Aither lapozasi tartomanya hibas")
            meta_known = True
            meta_has_next = current_page < last_page
        else:
            # Some UNIT3D installations return the simple paginator shape:
            # current_page/per_page/from/to, while links.next is the only
            # authoritative indication of another page.  Never reinterpret a
            # partially present full-paginator total/last_page as this shape.
            partial_required = ("current_page", "per_page", "from", "to")
            if (
                any(key in meta for key in ("last_page", "total"))
                or not all(key in meta for key in partial_required)
                or not link_known
            ):
                raise AitherError("Az Aither lapozasi metaadata hianyos")
            values = [meta[key] for key in partial_required]
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in values
            ):
                raise AitherError("Az Aither lapozasi metaadata hibas")
            current_page, per_page, first_item, last_item = values
            if (
                current_page != 1
                or per_page < 1
                or per_page != expected_per_page
                or data_count > per_page
                or first_item != 1
                or last_item != data_count
                or (link_has_next and data_count != per_page)
            ):
                raise AitherError("Az Aither lapozasi elemszama ellentmondasos")
            meta_known = True
            meta_has_next = link_has_next

    if link_known and meta_known and link_has_next != meta_has_next:
        raise AitherError("Az Aither links es meta lapozasa ellentmondasos")
    if meta_known:
        return meta_has_next, not meta_has_next
    if link_known:
        return link_has_next, not link_has_next
    return False, False


def _pagination_next_hint(payload: Mapping[str, Any]) -> bool:
    """Retain only a conservative next-page hint after invalid pagination."""

    links = payload.get("links")
    if not isinstance(links, Mapping):
        return False
    raw_next = links.get("next")
    return isinstance(raw_next, str) and bool(raw_next.strip())


class AitherClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        rate_gate: AitherRateGate,
        *,
        timeout: float = 30.0,
        store: StateStore | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.rate_gate = rate_gate
        self.timeout = timeout
        self.store = store
        self.session = requests.Session()
        self.session.trust_env = False
        self._memory_cache: dict[str, AitherPage] = {}

    @staticmethod
    def _cache_key(params: Mapping[str, Any]) -> str:
        requested_per_page = params.get("perPage")
        if (
            isinstance(requested_per_page, bool)
            or not isinstance(requested_per_page, int)
            or requested_per_page < 1
        ):
            raise ValueError("Aither query requires a positive perPage")
        if params.get("tmdbId") is not None:
            identity: Any = {"tmdb": int(params["tmdbId"])}
        elif params.get("imdbId") is not None:
            identity = {"imdb": normalize_imdb_id(params["imdbId"])}
        else:
            raise ValueError("Aither query requires an exact identity")
        legacy_key = build_query_key(
            identity,
            params.get("categories[]"),
            params.get("seasonNumber"),
        )
        canonical = {
            "base_query": legacy_key,
            "query_policy": QUERY_POLICY_VERSION,
            "requested_per_page": requested_per_page,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return f"q{QUERY_POLICY_VERSION}:{hashlib.sha256(encoded).hexdigest()}"

    def has_cached(self, params: Mapping[str, Any]) -> bool:
        key = self._cache_key(params)
        return key in self._memory_cache or bool(
            self.store is not None and self.store.get_query(key) is not None
        )

    def _load_persistent_page(
        self,
        key: str,
        *,
        expected_per_page: int,
        expected_attempt_nonce: str | None = None,
        require_current_binding: bool = False,
    ) -> AitherPage | None:
        if self.store is None:
            return None
        cached = self.store.get_query(key)
        if cached is None:
            return None
        values = cached.get("data")
        schema_present = "evidence_schema_version" in cached
        schema_version = cached.get("evidence_schema_version")
        digest_present = "evidence_digest" in cached
        stored_digest = cached.get("evidence_digest")
        cached_attempt_nonce = cached.get("attempt_nonce")
        if (
            not isinstance(values, list)
            or any(not isinstance(item, Mapping) for item in values)
            or not _strict_current_version(
                cached.get("query_policy"), QUERY_POLICY_VERSION
            )
            or not schema_present
            or not _strict_current_version(
                schema_version, AITHER_EVIDENCE_SCHEMA_VERSION
            )
            or not _strict_current_version(
                cached.get("requested_per_page"), expected_per_page
            )
            or not _valid_attempt_nonce(cached_attempt_nonce)
            or (
                expected_attempt_nonce is not None
                and (
                    not _valid_attempt_nonce(expected_attempt_nonce)
                    or cached_attempt_nonce != expected_attempt_nonce
                )
            )
            or (
                require_current_binding
                and (
                    not schema_present
                    or not _strict_current_version(
                        schema_version, AITHER_EVIDENCE_SCHEMA_VERSION
                    )
                    or not digest_present
                    or not _valid_evidence_digest(stored_digest)
                )
            )
            or not isinstance(cached.get("candidate_count"), int)
            or cached.get("candidate_count") != len(values)
            or isinstance(cached.get("candidate_count"), bool)
            or not isinstance(cached.get("complete"), bool)
            or not isinstance(cached.get("has_next"), bool)
            or (
                bool(cached.get("complete"))
                and bool(cached.get("has_next"))
            )
        ):
            raise AitherError(
                "A tartos Aither query-cache nem ertekelheto biztonsagosan"
            )
        page_schema = (
            AITHER_EVIDENCE_SCHEMA_VERSION if schema_present else None
        )
        expected_digest = _aither_page_digest(
            values,
            has_next=bool(cached.get("has_next")),
            complete=bool(cached.get("complete")),
            evidence_schema_version=page_schema,
            requested_per_page=expected_per_page,
            attempt_nonce=str(cached_attempt_nonce),
        )
        if digest_present and (
            not _valid_evidence_digest(stored_digest)
            or stored_digest != expected_digest
        ):
            raise AitherError(
                "A tartos Aither query-cache digestje ervenytelen"
            )
        page = AitherPage(
            data=tuple(dict(item) for item in values),
            has_next=bool(cached.get("has_next")),
            complete=bool(cached.get("complete")),
            cache_key=key,
            from_cache=True,
            evidence_schema_version=page_schema,
            evidence_digest=(stored_digest if digest_present else None),
            requested_per_page=expected_per_page,
            attempt_nonce=str(cached_attempt_nonce),
        )
        return page

    def load_persistent_once(
        self, params: Mapping[str, Any], *, attempt_nonce: str
    ) -> AitherPage:
        """Load only a current digest-bound page; never use HTTP or memory."""

        key = self._cache_key(params)
        expected_per_page = int(params["perPage"])
        page = self._load_persistent_page(
            key,
            expected_per_page=expected_per_page,
            expected_attempt_nonce=attempt_nonce,
            require_current_binding=True,
        )
        if page is None:
            raise AitherError("A korabbi Aither-probahoz nincs tartos cache")
        return page

    def search_once(
        self,
        params: Mapping[str, Any],
        *,
        before_request: Callable[[str], None] | None = None,
        allow_persistent_cache: bool = False,
    ) -> AitherPage:
        raw_expected_per_page = params.get("perPage")
        if (
            isinstance(raw_expected_per_page, bool)
            or not isinstance(raw_expected_per_page, int)
            or raw_expected_per_page < 1
        ):
            raise AitherError("Az Aither keresesi oldalmeret hibas")
        key = self._cache_key(params)
        if key in self._memory_cache:
            return replace(self._memory_cache[key], from_cache=True)
        # A persistent page is evidence for a release that already spent its
        # one HTTP attempt, not an eternal snapshot for future releases of the
        # same film or season.  Normal in-process sharing stays enabled; crash
        # recovery opts in to persisted data explicitly.
        if allow_persistent_cache:
            page = self._load_persistent_page(
                key,
                expected_per_page=raw_expected_per_page,
            )
            if page is not None:
                return page

        attempt_nonce = secrets.token_hex(32)

        def request() -> requests.Response:
            if before_request is not None:
                # Persist the release marker after the rate reservation but
                # immediately before the actual HTTP operation.  A crash can
                # therefore never spend a request and then forget the release.
                before_request(attempt_nonce)
            try:
                return self.session.get(
                    f"{self.base_url}/api/torrents/filter",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/json",
                    },
                    params=dict(params),
                    timeout=self.timeout,
                )
            except requests.RequestException:
                raise AitherError("Aither hálózati hiba") from None

        response = self.rate_gate.call(request)
        if response.status_code == 429:
            raise AitherError("Aither rate limit (HTTP 429); nincs automatikus retry")
        if response.status_code != 200:
            raise AitherError(f"Aither HTTP hiba: {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise AitherError("Az Aither válasza nem JSON") from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise AitherError("Az Aither válasza hibás formátumú")
        if any(not isinstance(item, Mapping) for item in payload["data"]):
            raise AitherError(
                "Az Aither találati oldala nem kizárólag objektumokat tartalmaz"
            )

        data = tuple(
            sanitize_aither_candidate(item)
            for item in payload["data"]
        )
        try:
            has_next, complete = _pagination_state(
                payload,
                len(data),
                expected_per_page=raw_expected_per_page,
            )
        except AitherError:
            # The request is already spent.  A fully sanitizable candidate
            # page remains useful positive evidence even when its pagination
            # envelope is malformed.  Persist it as incomplete: MATCH may
            # still prove a duplicate, but it can never prove NOT_FOUND.
            has_next = _pagination_next_hint(payload)
            complete = False
        evidence_digest = _aither_page_digest(
            data,
            has_next=has_next,
            complete=complete,
            evidence_schema_version=AITHER_EVIDENCE_SCHEMA_VERSION,
            requested_per_page=raw_expected_per_page,
            attempt_nonce=attempt_nonce,
        )
        page = AitherPage(
            data=data,
            has_next=has_next,
            complete=complete,
            cache_key=key,
            from_cache=False,
            evidence_schema_version=AITHER_EVIDENCE_SCHEMA_VERSION,
            evidence_digest=evidence_digest,
            requested_per_page=raw_expected_per_page,
            attempt_nonce=attempt_nonce,
        )
        if self.store is not None:
            self.store.record_query(
                key,
                {
                    "query_policy": QUERY_POLICY_VERSION,
                    "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
                    "requested_per_page": raw_expected_per_page,
                    "attempt_nonce": attempt_nonce,
                    "cached_at": utc_now(),
                    "data": list(data),
                    "has_next": has_next,
                    "complete": complete,
                    "candidate_count": len(data),
                    "evidence_digest": evidence_digest,
                },
            )
        self._memory_cache[key] = page
        return page


def normalize_imdb_id(value: Any) -> str | None:
    return identity_normalize_imdb_id(value)


def _imdb_numeric_key(value: Any) -> int | None:
    normalized = normalize_imdb_id(value)
    return int(normalized[2:]) if normalized is not None else None


def aither_params(
    parsed: Mapping[str, Any], *, tmdb_id: Any = None, imdb_id: Any = None
) -> dict[str, Any]:
    """Build the broad lookup: identity + category + season only."""

    if (tmdb_id is None) == (imdb_id is None):
        raise ValueError("exactly one Aither identity is required")
    category = CATEGORY_IDS.get(str(parsed.get("media_type") or ""))
    params: dict[str, Any] = {"perPage": 100}
    if tmdb_id is not None:
        params["tmdbId"] = int(tmdb_id)
    else:
        normalized = normalize_imdb_id(imdb_id)
        if normalized is None:
            raise ValueError("invalid IMDb identity")
        params["imdbId"] = int(normalized[2:])
    if category is not None:
        params["categories[]"] = [category]
    if parsed.get("season") is not None:
        params["seasonNumber"] = int(parsed["season"])
    return params


def _identity_candidates(
    page: AitherPage, *, tmdb_id: Any = None, imdb_id: Any = None
) -> tuple[dict[str, Any], ...]:
    expected_tmdb = str(tmdb_id) if tmdb_id is not None else None
    expected_imdb = _imdb_numeric_key(imdb_id)
    result = []
    for item in page.data:
        attrs = item.get("attributes")
        if not isinstance(attrs, Mapping):
            continue
        if expected_tmdb is not None:
            if str(attrs.get("tmdb_id") or "") != expected_tmdb:
                continue
        elif expected_imdb is not None:
            if _imdb_numeric_key(attrs.get("imdb_id")) != expected_imdb:
                continue
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class CheckOutcome:
    status: str
    lookup: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    message: str = ""
    reason: str = ""
    matched_evidence: dict[str, Any] | None = None
    query_evidence: tuple[dict[str, Any], ...] = ()
    proven_not_found: bool = False
    attempt_nonce: str | None = None


def _effective_page_digest(page: AitherPage) -> str | None:
    if page.evidence_digest is not None:
        return page.evidence_digest
    if page.from_cache:
        return None
    return _aither_page_digest(
        page.data,
        has_next=page.has_next,
        complete=page.complete,
        evidence_schema_version=page.evidence_schema_version,
        requested_per_page=page.requested_per_page,
        attempt_nonce=page.attempt_nonce,
    )


def _query_evidence(
    page: AitherPage,
    lookup: str,
    *,
    tmdb_id: Any = None,
    imdb_id: Any = None,
) -> dict[str, Any]:
    return {
        "query_policy": QUERY_POLICY_VERSION,
        "lookup": lookup,
        "tmdb_id": int(tmdb_id) if tmdb_id is not None else None,
        "imdb_id": normalize_imdb_id(imdb_id),
        "candidate_count": len(page.data),
        "has_next": page.has_next,
        "complete": page.complete,
        "cache_key": page.cache_key,
        "from_cache": page.from_cache,
        "evidence_schema_version": page.evidence_schema_version,
        "evidence_digest": _effective_page_digest(page),
        "requested_per_page": page.requested_per_page,
        "attempt_nonce": page.attempt_nonce,
    }


def evaluate_page(
    parsed: Mapping[str, Any],
    page: AitherPage,
    *,
    lookup: str,
    tmdb_id: Any = None,
    imdb_id: Any = None,
) -> CheckOutcome:
    evidence = _query_evidence(
        page, lookup, tmdb_id=tmdb_id, imdb_id=imdb_id
    )
    schema_current = _strict_current_version(
        page.evidence_schema_version, AITHER_EVIDENCE_SCHEMA_VERSION
    ) and (
        isinstance(page.requested_per_page, int)
        and not isinstance(page.requested_per_page, bool)
        and page.requested_per_page > 0
    )
    if not schema_current and (page.data or page.evidence_schema_version is not None):
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
            imdb_id=normalize_imdb_id(imdb_id),
            message="Az Aither-oldal evidence semaja nem eleg eros a donteshez",
            reason="UNTRUSTED_AITHER_EVIDENCE_SCHEMA",
            query_evidence=(evidence,),
        )
    expected_digest = _aither_page_digest(
        page.data,
        has_next=page.has_next,
        complete=page.complete,
        evidence_schema_version=page.evidence_schema_version,
        requested_per_page=page.requested_per_page,
        attempt_nonce=page.attempt_nonce,
    )
    evidence_digest = _effective_page_digest(page)
    if (
        not _valid_evidence_digest(evidence_digest)
        or evidence_digest != expected_digest
    ):
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
            imdb_id=normalize_imdb_id(imdb_id),
            message="Az Aither-oldal nem kotheto tartosan ehhez a donteshez",
            reason="INVALID_AITHER_EVIDENCE_DIGEST",
            query_evidence=(evidence,),
        )
    candidates = _validated_page_candidates(
        parsed,
        page,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
    )
    if candidates is None:
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
            imdb_id=normalize_imdb_id(imdb_id),
            message="Az Aither ID-szűrője eltérő azonosítójú rekordot adott",
            reason="INVALID_AITHER_CANDIDATE_PAGE",
            query_evidence=(evidence,),
        )
    decision, matched = evaluate_candidates(parsed, candidates)
    if decision is TechnicalDecision.MATCH:
        return CheckOutcome(
            status="DUPLICATE",
            lookup=lookup,
            tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
            imdb_id=normalize_imdb_id(imdb_id),
            message="Egyező kiadás és technikai aláírás",
            reason="MATCHED_TECHNICAL_SIGNATURE",
            matched_evidence=(
                sanitize_aither_candidate(matched) if matched is not None else None
            ),
            query_evidence=(evidence,),
        )
    if decision is TechnicalDecision.INDETERMINATE:
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
            imdb_id=normalize_imdb_id(imdb_id),
            message="A technikai signature nem dönthető el biztonságosan",
            reason="INDETERMINATE_TECHNICAL_SIGNATURE",
            query_evidence=(evidence,),
        )
    if not page.complete:
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
            imdb_id=normalize_imdb_id(imdb_id),
            message=(
                "100-nál több találat; további API-lapozás letiltva"
                if page.has_next
                else "Az Aither-válasz lapozási teljessége nem bizonyítható"
            ),
            reason=("TRUNCATED_QUERY" if page.has_next else "UNKNOWN_PAGINATION"),
            query_evidence=(evidence,),
        )
    return CheckOutcome(
        status="NOT_FOUND",
        lookup=lookup,
        tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
        imdb_id=normalize_imdb_id(imdb_id),
        message="A teljes, széles találati oldalon nincs technikai egyezés",
        reason="COMPLETE_QUERY_NO_MATCH",
        query_evidence=(evidence,),
        proven_not_found=True,
    )


def _tmdb_identity_from_variants(
    parsed: Mapping[str, Any],
    variants: Sequence[TitleVariant],
    tmdb: TmdbClient,
    *,
    allow_alias_collapse: bool = True,
) -> tuple[dict[str, Any] | None, bool]:
    identities: dict[tuple[str, int], dict[str, Any]] = {}
    for variant in variants:
        query = dict(parsed)
        query["title"] = variant.title
        query["year"] = variant.year
        identity = tmdb.search(query)
        if identity is None:
            continue
        key = (
            str(identity.get("media_type") or parsed.get("media_type") or "movie"),
            int(identity["tmdb_id"]),
        )
        identities[key] = dict(identity)
    first = next(iter(identities.values())) if identities else None
    if len(identities) <= 1:
        return first, False
    coverage = getattr(tmdb, "identity_covers_variants", None)
    if allow_alias_collapse and callable(coverage):
        covering = [
            identity
            for identity in identities.values()
            if coverage(identity, variants)
        ]
        if len(covering) == 1:
            return covering[0], False
    return first, True


def _strict_imdb_from_variants(
    variants: Sequence[TitleVariant],
    media_type: str,
    imdb: IMDbSuggestionClient | None,
) -> tuple[str | None, bool]:
    if imdb is None:
        return None, False
    identities: list[str] = []
    try:
        for variant in variants:
            found = imdb.search_exact(
                variant.title,
                variant.year,
                "tv" if media_type == "tv" else "movie",
            )
            if found is not None and found.imdb_id not in identities:
                identities.append(found.imdb_id)
    except IMDbSuggestionError as exc:
        raise ProviderError(str(exc)) from None
    return (identities[0] if identities else None), len(identities) > 1


def _is_explicit_multi_feature(name: str) -> bool:
    """Recognize only labels where both joined works carry their own year."""

    return bool(
        re.search(
            r"\((?:18|19|20)\d{2}\)\s*(?:&|\+)\s*.+"
            r"\((?:18|19|20)\d{2}\)",
            str(name),
        )
    )


def _tmdb_find_by_imdb(
    tmdb: TmdbClient, imdb_id: str, media_type: str
) -> dict[str, Any] | None:
    finder = getattr(tmdb, "find_by_imdb", None)
    if callable(finder):
        return finder(imdb_id, media_type)
    return None


def check_release(
    parsed: Mapping[str, Any],
    torrent: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    tmdb: TmdbClient,
    aither: AitherClient,
    imdb: IMDbSuggestionClient | None = None,
    before_aither_request: (
        Callable[[str, int | None, str | None, str], None] | None
    ) = None,
    identity_hint: Mapping[str, Any] | None = None,
) -> CheckOutcome:
    def search(
        params: Mapping[str, Any],
        lookup: str,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
    ) -> AitherPage:
        if before_aither_request is None:
            return aither.search_once(params)
        return aither.search_once(
            params,
            before_request=lambda attempt_nonce: before_aither_request(
                lookup, tmdb_id, normalize_imdb_id(imdb_id), attempt_nonce
            ),
        )

    name = str(torrent.get("name") or parsed.get("name") or "")
    parsed = enrich_release(parsed, name, torrent, files)
    metadata = scan_local_metadata(torrent, files)
    if len(metadata.imdb_ids) > 1:
        return CheckOutcome(
            status="UNRESOLVED",
            message="A helyi metaadat több eltérő IMDb-azonosítót tartalmaz",
            reason="CONFLICTING_LOCAL_IMDB_IDS",
        )

    hint = dict(identity_hint) if identity_hint is not None else None
    identity: dict[str, Any] | None = None
    hint_imdb = normalize_imdb_id((hint or {}).get("imdb_id"))
    if (
        metadata.imdb_id is not None
        and hint_imdb is not None
        and _imdb_numeric_key(metadata.imdb_id) != _imdb_numeric_key(hint_imdb)
    ):
        return CheckOutcome(
            status="UNRESOLVED",
            message="A helyi IMDb-azonosító és a lemez-override eltér",
            reason="IDENTITY_OVERRIDE_LOCAL_ID_CONFLICT",
        )

    if hint is not None:
        hint_media_type = str(hint.get("media_type") or "")
        parsed_media_type = str(parsed.get("media_type") or "")
        try:
            hint_tmdb_id = int(hint.get("tmdb_id"))
            hint_year = int(hint.get("year"))
        except (TypeError, ValueError):
            return CheckOutcome(
                status="UNRESOLVED",
                message="A lemez-override katalógusadata hibás",
                reason="INVALID_IDENTITY_OVERRIDE",
            )
        if (
            hint_imdb is None
            or hint_media_type not in {"movie", "tv"}
            or parsed_media_type != hint_media_type
            or (
                parsed.get("year") is not None
                and _safe_year(parsed.get("year")) != hint_year
            )
        ):
            return CheckOutcome(
                status="UNRESOLVED",
                message="A lemez-override típusa vagy éve nem egyezik a release-szel",
                reason="IDENTITY_OVERRIDE_RELEASE_CONFLICT",
            )
        verified = _tmdb_find_by_imdb(tmdb, hint_imdb, hint_media_type)
        if (
            verified is None
            or int(verified.get("tmdb_id") or 0) != hint_tmdb_id
            or str(verified.get("media_type") or "") != hint_media_type
        ):
            return CheckOutcome(
                status="UNRESOLVED",
                message="A TMDb nem igazolta az override IMDb/TMDb párját",
                reason="IDENTITY_OVERRIDE_CATALOG_MISMATCH",
            )

        payload_review = hint.get("payload_review")
        if payload_review is not None:
            reason = (
                str(payload_review.get("manual_reason") or "").strip()
                if isinstance(payload_review, Mapping)
                else ""
            )
            if (
                not isinstance(payload_review, Mapping)
                or payload_review.get("required") is not True
                or re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", reason) is None
            ):
                return CheckOutcome(
                    status="UNRESOLVED",
                    message="Az override payload review jeloloje hibas",
                    reason="INVALID_IDENTITY_OVERRIDE",
                )
            # The catalogue pair is resolved, but this exact payload carries a
            # separately proven defect (for example an overlapping episode).
            # Stop before the first Aither request and never create READY.
            return CheckOutcome(
                status="MANUAL",
                lookup="tmdb",
                tmdb_id=hint_tmdb_id,
                imdb_id=hint_imdb,
                message=(
                    "Az identity feloldva, de az exact payload kulon "
                    "ellenorzest igenyel"
                ),
                reason=reason,
            )
        identity = {
            **verified,
            "imdb_id": hint_imdb,
            "source": str(hint.get("source") or "verified_identity_override"),
        }

    local_imdb = metadata.imdb_id or hint_imdb
    explicit_multi = _is_explicit_multi_feature(name)
    variants: tuple[TitleVariant, ...] = ()
    if identity is None:
        variants = release_identity_variants(
            name,
            str(parsed.get("title") or ""),
            parsed.get("year"),
            metadata.bdmv_titles,
        )
        if not variants:
            return CheckOutcome(
                status="UNRESOLVED",
                message="Nincs biztonságos címváltozat",
                reason="NO_TITLE_VARIANT",
            )

    # A unique, local NFO/XML IMDb identifier is stronger than guesses made
    # from a noisy or bilingual release label.  Resolve it before title search.
    if identity is None and local_imdb is not None:
        identity = _tmdb_find_by_imdb(
            tmdb, local_imdb, str(parsed.get("media_type") or "movie")
        )
        if identity is None:
            page = search(
                aither_params(parsed, imdb_id=local_imdb),
                "imdb",
                imdb_id=local_imdb,
            )
            return evaluate_page(
                parsed, page, lookup="imdb", imdb_id=local_imdb
            )
    elif identity is None:
        identity, tmdb_conflict = _tmdb_identity_from_variants(
            parsed,
            variants,
            tmdb,
            allow_alias_collapse=not explicit_multi,
        )
        # A double-feature disc necessarily names two different works.  Use
        # the first deterministically as the tracker identity, but keep an
        # ordinary alias conflict unresolved rather than guessing.
        if tmdb_conflict and not explicit_multi:
            return CheckOutcome(
                status="UNRESOLVED",
                message="A címváltozatok eltérő TMDb művekre mutatnak",
                reason="CONFLICTING_TMDB_IDENTITIES",
            )

    # Large Matroska files are probed only after the cheap, exact title/NFO
    # paths fail.  The probe reads bounded format tags through a safe file
    # descriptor; ambiguous IDs or titles remain unresolved.
    if identity is None and local_imdb is None:
        container = scan_matroska_metadata(torrent, files)
        if (
            len(container.imdb_ids) > 1
            or len(container.tmdb_ids) > 1
            or (container.tmdb_refs and container.tmdb_id is None)
        ):
            return CheckOutcome(
                status="UNRESOLVED",
                message="A kontener metaadatai tobb katalogusazonositot tartalmaznak",
                reason="CONFLICTING_CONTAINER_IDS",
            )

        container_imdb = container.imdb_id
        container_tmdb = container.tmdb_id
        explicit_container_types = {
            kind for kind, _ in container.tmdb_refs if kind is not None
        }
        if explicit_container_types and explicit_container_types != {
            str(parsed.get("media_type") or "movie")
        }:
            return CheckOutcome(
                status="UNRESOLVED",
                message="A kontener TMDb-tipusa nem egyezik a release tipussal",
                reason="CONFLICTING_CONTAINER_MEDIA_TYPE",
            )
        if container_imdb is not None:
            local_imdb = container_imdb
            mapped = _tmdb_find_by_imdb(
                tmdb, container_imdb, str(parsed.get("media_type") or "movie")
            )
            if mapped is not None:
                if (
                    container_tmdb is not None
                    and int(mapped.get("tmdb_id") or 0) != container_tmdb
                ):
                    return CheckOutcome(
                        status="UNRESOLVED",
                        message="A kontener IMDb- es TMDb-azonositoja nem ugyanaz a mu",
                        reason="CONFLICTING_CONTAINER_CATALOG_PAIR",
                    )
                if str(mapped.get("media_type") or "") != str(
                    parsed.get("media_type") or "movie"
                ):
                    return CheckOutcome(
                        status="UNRESOLVED",
                        message="A kontener IMDb-azonositoja mas mediatipusra mutat",
                        reason="CONFLICTING_CONTAINER_MEDIA_TYPE",
                    )
                identity = mapped

        trusted_container_titles = (
            container.series_titles
            if str(parsed.get("media_type") or "movie") == "tv"
            else container.titles
        )
        if identity is None and container_imdb is None and trusted_container_titles:
            container_variants = release_identity_variants(
                "", "", parsed.get("year"), trusted_container_titles
            )
            container_identity, container_conflict = _tmdb_identity_from_variants(
                parsed,
                container_variants,
                tmdb,
                allow_alias_collapse=True,
            )
            if container_conflict:
                return CheckOutcome(
                    status="UNRESOLVED",
                    message="A kontener cimei eltero TMDb-muvekre mutatnak",
                    reason="CONFLICTING_CONTAINER_TITLES",
                )
            if container_identity is not None:
                if (
                    container_tmdb is not None
                    and int(container_identity.get("tmdb_id") or 0) != container_tmdb
                ):
                    return CheckOutcome(
                        status="UNRESOLVED",
                        message="A kontener cime es TMDb-azonositoja nem egyezik",
                        reason="CONFLICTING_CONTAINER_TMDB_ID",
                    )
                identity = container_identity
            if container_variants:
                variants = container_variants

        if identity is None and container_tmdb is not None:
            # A bare numeric tag is not trusted without a matching exact title
            # or an IMDb -> TMDb pair returned by TMDb itself.
            return CheckOutcome(
                status="UNRESOLVED",
                message="A kontener TMDb-azonositoja nem kapott fuggetlen megerositest",
                reason="UNVERIFIED_CONTAINER_TMDB_ID",
            )

    if identity is None:
        imdb_id = local_imdb
        if imdb_id is None:
            imdb_id, imdb_conflict = _strict_imdb_from_variants(
                variants, str(parsed.get("media_type") or "movie"), imdb
            )
            if imdb_conflict and not explicit_multi:
                return CheckOutcome(
                    status="UNRESOLVED",
                    message="A címváltozatok eltérő IMDb művekre mutatnak",
                    reason="CONFLICTING_IMDB_IDENTITIES",
                )
        if imdb_id is None:
            return CheckOutcome(
                status="UNRESOLVED",
                message="Sem biztos TMDb-, sem egyértelmű IMDb-azonosító nincs",
                reason="NO_CATALOG_IDENTITY",
            )
        identity = _tmdb_find_by_imdb(
            tmdb, imdb_id, str(parsed.get("media_type") or "movie")
        )
        if identity is None:
            page = search(
                aither_params(parsed, imdb_id=imdb_id),
                "imdb",
                imdb_id=imdb_id,
            )
            return evaluate_page(
                parsed, page, lookup="imdb", imdb_id=imdb_id
            )

    if identity is not None:
        if str(identity.get("media_type") or "") != str(
            parsed.get("media_type") or "movie"
        ):
            return CheckOutcome(
                status="UNRESOLVED",
                message="A feloldott katalogusazonosito mas mediatipushoz tartozik",
                reason="CATALOG_MEDIA_TYPE_CONFLICT",
            )
        tmdb_id = int(identity["tmdb_id"])
        page = search(
            aither_params(parsed, tmdb_id=tmdb_id),
            "tmdb",
            tmdb_id=tmdb_id,
        )
        if page.data or not page.complete:
            return evaluate_page(parsed, page, lookup="tmdb", tmdb_id=tmdb_id)

        imdb_id = (
            local_imdb
            or normalize_imdb_id(identity.get("imdb_id"))
            or tmdb.external_imdb_id(identity)
        )
        if imdb_id:
            imdb_page = search(
                aither_params(parsed, imdb_id=imdb_id),
                "imdb",
                imdb_id=imdb_id,
            )
            outcome = evaluate_page(
                parsed,
                imdb_page,
                lookup="imdb",
                tmdb_id=None,
                imdb_id=imdb_id,
            )
            return replace(
                outcome,
                query_evidence=(
                    _query_evidence(page, "tmdb", tmdb_id=tmdb_id),
                    *outcome.query_evidence,
                ),
            )
        return CheckOutcome(
            status="NOT_FOUND",
            lookup="tmdb",
            tmdb_id=tmdb_id,
            message="Az Aither teljes TMDb-oldala üres",
            reason="COMPLETE_EMPTY_TMDB_QUERY",
            query_evidence=(
                _query_evidence(page, "tmdb", tmdb_id=tmdb_id),
            ),
            proven_not_found=True,
        )

    raise AssertionError("identity flow must return before this point")


def classify_local_payload(
    torrent: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    *,
    iso_timeout: float = 30.0,
    max_listing_bytes: int = 16 * 1024 * 1024,
) -> str:
    """Classify opaque ISO payloads via a bounded, read-only 7z listing."""

    status = classify_manifest(files)
    iso_items = [
        item
        for item in files
        if Path(str(item.get("name") or "")).suffix.casefold() == ".iso"
    ]
    if not iso_items:
        return status
    if len(iso_items) > 4:
        return "UNKNOWN"

    non_iso = [item for item in files if item not in iso_items]
    if non_iso and classify_manifest(non_iso) == "MEDIA":
        return "MEDIA"
    try:
        root = Path(str(torrent.get("save_path") or "")).resolve(strict=True)
    except OSError:
        return "UNKNOWN"
    if not root.is_dir():
        return "UNKNOWN"

    findings: list[str] = []
    for item in iso_items:
        pure = PurePosixPath(str(item.get("name") or "").replace("\\", "/"))
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            return "UNKNOWN"
        try:
            image = root.joinpath(*pure.parts).resolve(strict=True)
        except OSError:
            return "UNKNOWN"
        if not is_within(image, root) or image.is_symlink() or not image.is_file():
            return "UNKNOWN"
        try:
            with tempfile.TemporaryFile() as listing:
                result = subprocess.run(
                    ["7z", "l", "-slt", "--", str(image)],
                    stdin=subprocess.DEVNULL,
                    stdout=listing,
                    stderr=subprocess.DEVNULL,
                    timeout=iso_timeout,
                    check=False,
                )
                if result.returncode != 0:
                    return "UNKNOWN"
                listing.seek(0)
                raw = listing.read(max_listing_bytes + 1)
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN"
        if len(raw) > max_listing_bytes:
            return "UNKNOWN"
        findings.append(classify_iso_listing(raw.decode("utf-8", errors="replace")))

    if "MEDIA" in findings:
        return "MEDIA"
    if findings and all(item == "NOT_MEDIA" for item in findings):
        return "NOT_MEDIA"
    return "UNKNOWN"


def media_kind_from_manifest(files: Sequence[Mapping[str, Any]]) -> str:
    names = [str(item.get("name") or "").casefold() for item in files]
    has_rar = any(
        name.endswith(".rar")
        or PART_RAR_RE.search(name)
        or RAR_CONTINUATION_RE.search(name)
        for name in names
    )
    has_iso = any(name.endswith(".iso") for name in names)
    if has_rar:
        return "rar"
    if has_iso:
        return "iso"
    return "plain"


def safe_torrent_summary(torrent: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hash": str(torrent.get("hash") or ""),
        "name": str(torrent.get("name") or ""),
        "state": str(torrent.get("state") or ""),
        "progress": float(torrent.get("progress") or 0),
        "save_path": str(torrent.get("save_path") or ""),
        "content_path": str(torrent.get("content_path") or torrent.get("save_path") or ""),
    }


def candidate_record(
    key: str,
    name: str,
    torrents: Sequence[Mapping[str, Any]],
    files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    primary = torrents[0]
    media_kind = media_kind_from_manifest(files)
    content_path = str(primary.get("content_path") or primary.get("save_path") or "")
    processing = (
        {"status": "PENDING"}
        if media_kind in {"rar", "iso"}
        else {"status": "READY", "ready_path": content_path, "mode": "plain"}
    )
    hashes = sorted(
        {str(item.get("hash") or "") for item in torrents if item.get("hash")}
    )
    running_hashes = sorted(
        str(item.get("hash"))
        for item in torrents
        if item.get("hash")
        and not str(item.get("state") or "").casefold().startswith(
            ("stopped", "paused")
        )
    )
    return {
        "key": key,
        "name": name,
        "media_kind": media_kind,
        "content_path": content_path,
        "save_path": str(primary.get("save_path") or ""),
        "hashes": hashes,
        "running_hashes": running_hashes,
        "file_names": [
            str(item.get("name")) for item in files if item.get("name")
        ],
        "torrents": [safe_torrent_summary(item) for item in torrents],
        "processing": processing,
    }


def _canonical_infohashes(values: Iterable[Any]) -> tuple[str, ...]:
    """Return deterministic qB infohashes and ignore malformed state input."""

    hashes: set[str] = set()
    for value in values:
        normalized = str(value or "").strip().casefold()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", normalized):
            hashes.add(normalized)
    return tuple(sorted(hashes))


def _group_infohashes(torrents: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return _canonical_infohashes(item.get("hash") for item in torrents)


def _record_infohashes(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Read current and legacy hash bindings without trusting their shape."""

    values: list[Any] = []
    direct = record.get("torrent_hashes")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        values.extend(direct)
    candidate = record.get("candidate")
    if isinstance(candidate, Mapping):
        legacy = candidate.get("hashes")
        if isinstance(legacy, Sequence) and not isinstance(legacy, (str, bytes)):
            values.extend(legacy)
    return _canonical_infohashes(values)


def append_event(path: Path, event: str, name: str, details: str = "") -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{utc_now()}] {event}\n")
            handle.write(f"Name: {name}\n")
            if details:
                handle.write(f"Details: {details}\n")
            handle.write("\n")
        os.chmod(path, 0o600)
    except OSError:
        # The durable decision/cache is authoritative.  A secondary text log
        # must never strand an already-spent request between marker and result.
        print("FIGYELEM: az eseménynapló nem írható", file=sys.stderr)


def record_terminal(
    store: StateStore,
    key: str,
    name: str,
    parsed: Mapping[str, Any],
    outcome: CheckOutcome,
    *,
    candidate: Mapping[str, Any] | None = None,
    previous_decision: Mapping[str, Any] | None = None,
) -> None:
    processing = (
        dict(candidate.get("processing") or {}) if candidate is not None else {"status": "NONE"}
    )
    bound_hashes = _canonical_infohashes(
        (
            *(parsed.get("torrent_hashes") or ()),
            *(
                _record_infohashes(previous_decision)
                if isinstance(previous_decision, Mapping)
                else ()
            ),
            *(
                _record_infohashes({"candidate": candidate})
                if isinstance(candidate, Mapping)
                else ()
            ),
        )
    )
    store.record_release(
        key,
        {
            "status": outcome.status,
            "terminal": True,
            "name": name,
            "checked_at": utc_now(),
            "lookup": outcome.lookup,
            "tmdb_id": outcome.tmdb_id,
            "imdb_id": outcome.imdb_id,
            "message": outcome.message,
            "reason": outcome.reason,
            "decision_version": DECISION_VERSION,
            "query_policy": QUERY_POLICY_VERSION,
            "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
            "matcher_version": MATCHER_VERSION,
            "identity_resolver_version": IDENTITY_RESOLVER_VERSION,
            "matched_evidence": outcome.matched_evidence,
            "query_evidence": list(outcome.query_evidence),
            "proven_not_found": outcome.proven_not_found,
            "attempt_nonce": outcome.attempt_nonce,
            "torrent_hashes": list(bound_hashes),
            "parsed": {
                field_name: parsed.get(field_name)
                for field_name in (
                    "title", "year", "season", "episode", "episodes",
                    "episode_title", "scope",
                    "resolution", "medium", "release_type", "source", "codec", "hdr",
                    "media_type", "disc_manifest_kind",
                    "disc_manifest_fingerprint", "disc_manifest_version",
                    "disc_manifest_evidence",
                    "disc_file_count", "torrent_file_count", "disc_payload_size",
                    "disc_main_file_size", "disc_total_size", "disc_regions",
                    "disc_editions",
                    "media_payload_size", "media_main_file_size", "media_file_count",
                    "media_file_sizes", "media_payload_signature",
                )
            },
            "candidate": dict(candidate) if candidate is not None else None,
            "processing": processing,
            "previous_decision": (
                dict(previous_decision) if previous_decision is not None else None
            ),
        },
    )


def _group_is_from_aither(
    qb: QbitClient, torrents: Sequence[Mapping[str, Any]]
) -> bool:
    """Check every hash in a normalized group without persisting private URLs."""

    for torrent in torrents:
        if detect_source_aither(torrent):
            return True
    properties = getattr(qb, "properties", None)
    tracker_reader = getattr(qb, "trackers", None)
    for torrent in torrents:
        torrent_hash = str(torrent.get("hash") or "")
        if not torrent_hash:
            continue
        details: Mapping[str, Any] = {}
        trackers: Sequence[Mapping[str, Any] | str] = ()
        if callable(properties):
            details = properties(torrent_hash)
        if callable(tracker_reader):
            trackers = tracker_reader(torrent_hash)
        probe = {
            "tracker": torrent.get("tracker") or details.get("tracker"),
            "comment": torrent.get("comment") or details.get("comment"),
        }
        if detect_source_aither(probe, trackers):
            return True
    return False


def _is_legacy_record(record: Mapping[str, Any]) -> bool:
    try:
        return int(record.get("decision_version") or 1) < DECISION_VERSION
    except (TypeError, ValueError):
        return True


def _has_aither_attempt_evidence(record: Mapping[str, Any]) -> bool:
    current: Mapping[str, Any] | None = record
    visited: set[int] = set()
    for _ in range(16):
        if current is None or id(current) in visited:
            return current is not None
        visited.add(id(current))
        status = str(current.get("status") or "").upper()
        reason = str(current.get("reason") or "").upper()
        lookup = str(current.get("lookup") or "").lower()
        if (
            status != "UNRESOLVED"
            or reason == "AITHER_HTTP_RESERVED"
            or lookup in {"tmdb", "imdb"}
            or current.get("tmdb_id")
            or current.get("imdb_id")
            or current.get("query_evidence")
            or current.get("matched_evidence")
            or bool(current.get("proven_not_found"))
        ):
            return True
        nested = current.get("previous_decision")
        current = nested if isinstance(nested, Mapping) else None
    return current is not None


def _pre_aither_identity_recheck(record: Mapping[str, Any]) -> bool:
    try:
        record_version = int(record.get("decision_version") or 1)
    except (TypeError, ValueError):
        record_version = 1
    if str(record.get("status") or "").upper() != "UNRESOLVED":
        return False
    if _has_aither_attempt_evidence(record):
        return False
    if record_version <= 1:
        return True
    raw_resolver_version = record.get("identity_resolver_version", 1)
    if isinstance(raw_resolver_version, bool):
        return False
    try:
        resolver_version = int(raw_resolver_version)
    except (TypeError, ValueError):
        return False
    return (
        0 <= resolver_version < IDENTITY_RESOLVER_VERSION
        and str(record.get("reason") or "").upper() == "NO_CATALOG_IDENTITY"
    )


def _legacy_parser_suspect(record: Mapping[str, Any]) -> bool:
    if not _is_legacy_record(record):
        return False
    try:
        record_version = int(record.get("decision_version") or 1)
    except (TypeError, ValueError):
        record_version = 1
    status = str(record.get("status") or "").upper()
    if status == "UNRESOLVED":
        # V1 did not retain enough identity evidence, so every cleanly
        # pre-Aither unresolved V1 record needs the new resolver.  Later
        # versions are narrower: only releases that never obtained a catalogue
        # identity are eligible.  All other prior decisions stay frozen.
        return _pre_aither_identity_recheck(record)
    if status == "MANUAL":
        return record_version <= 1
    if record_version > 1:
        return False
    parsed = record.get("parsed")
    if not isinstance(parsed, Mapping):
        return False
    release_type = str(parsed.get("release_type") or "").casefold()
    source = " ".join(
        str(parsed.get(key) or "") for key in ("source", "medium")
    ).casefold()
    false_encode = release_type == "encode" and (
        "web" in source or "bluray" in source or "blu-ray" in source
    )
    if status == "NOT_FOUND":
        return false_encode
    if status == "DUPLICATE":
        return release_type == "encode" and (
            "bluray" in source or "blu-ray" in source
        )
    return False


def _legacy_aither_decision_no_repeat(record: Mapping[str, Any]) -> bool:
    """Treat every v1 outcome that may have queried Aither as already spent."""

    try:
        record_version = int(record.get("decision_version") or 1)
    except (TypeError, ValueError):
        record_version = 1
    return record_version <= 1 and str(
        record.get("status") or ""
    ).upper() in {"NOT_FOUND", "MISSING", "MANUAL", "DUPLICATE"}


def _legacy_aither_evidence_loss(
    record: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return an old Aither outcome whose candidate page was not persisted."""

    if not _is_legacy_record(record):
        return None

    prior: Mapping[str, Any] = record
    if (
        str(record.get("status") or "").upper() == "UNRESOLVED"
        and str(record.get("reason") or "").upper() == "NO_CATALOG_IDENTITY"
    ):
        nested = record.get("previous_decision")
        if not isinstance(nested, Mapping):
            return None
        prior = nested

    status = str(prior.get("status") or "").upper()
    lookup = str(prior.get("lookup") or "").lower()
    has_identity = (
        (lookup == "tmdb" and prior.get("tmdb_id") is not None)
        or (
            lookup == "imdb"
            and normalize_imdb_id(prior.get("imdb_id")) is not None
        )
    )
    if status in {"NOT_FOUND", "MISSING"}:
        return prior if has_identity and prior.get("proven_not_found") is not True else None

    if (
        status != "MANUAL"
        or not has_identity
        or prior.get("matched_evidence")
        or prior.get("query_evidence")
        or prior.get("candidate")
    ):
        return None
    return prior


def _manual_cached_matcher_record(record: Mapping[str, Any]) -> bool:
    """Identify cache-derived MANUAL classes eligible for matcher replay."""

    return (
        str(record.get("status") or "").upper() == "MANUAL"
        and str(record.get("reason") or "").upper()
        in {
            "INDETERMINATE_TECHNICAL_SIGNATURE",
            "CACHED_MATCHER_INDETERMINATE",
        }
    )


def _current_bound_query_evidence(record: Mapping[str, Any]) -> bool:
    """Require current, digest- and nonce-bound evidence before MANUAL replay."""

    evidence = record.get("query_evidence")
    if (
        not isinstance(evidence, Sequence)
        or isinstance(evidence, (str, bytes))
        or not evidence
    ):
        return False
    return all(
        isinstance(item, Mapping)
        and _strict_current_version(
            item.get("query_policy"), QUERY_POLICY_VERSION
        )
        and _strict_current_version(
            item.get("evidence_schema_version"),
            AITHER_EVIDENCE_SCHEMA_VERSION,
        )
        and _valid_evidence_digest(item.get("evidence_digest"))
        and _valid_attempt_nonce(item.get("attempt_nonce"))
        for item in evidence
    )


def _needs_cached_matcher_recheck(record: Mapping[str, Any]) -> bool:
    """Select stale decisions that the durable cache can safely re-evaluate."""

    status = str(record.get("status") or "").upper()
    if status == "NOT_FOUND" and record.get("proven_not_found") is True:
        if _valid_offline_operator_not_found(record):
            return False
        raw_version = record.get("matcher_version")
        if raw_version is None:
            return True
        return (
            isinstance(raw_version, int)
            and not isinstance(raw_version, bool)
            and 0 <= raw_version < MATCHER_VERSION
        )

    if not _manual_cached_matcher_record(record):
        return False
    raw_version = record.get("matcher_version")
    return (
        record.get("terminal") is True
        and _strict_current_version(
            record.get("decision_version"), DECISION_VERSION
        )
        and _strict_current_version(
            record.get("query_policy"), QUERY_POLICY_VERSION
        )
        and _strict_current_version(
            record.get("evidence_schema_version"),
            AITHER_EVIDENCE_SCHEMA_VERSION,
        )
        and isinstance(raw_version, int)
        and not isinstance(raw_version, bool)
        and 0 <= raw_version < MATCHER_VERSION
        and _current_bound_query_evidence(record)
    )


def _cached_matcher_provenance_invalid(record: Mapping[str, Any]) -> bool:
    """Reject present, non-canonical replay provenance without using cache."""

    status = str(record.get("status") or "").upper()
    proven_not_found = (
        status == "NOT_FOUND" and record.get("proven_not_found") is True
    )
    manual_replay = _manual_cached_matcher_record(record)
    if not proven_not_found and not manual_replay:
        return False

    # A one-time, offline operator audit classified these records from local
    # manifests with zero network requests.  Its policy/schema v2 fields
    # describe the migration runtime, not a replayed Aither page, so a q3 cache
    # provenance bump must not revoke an otherwise exact audit marker.
    if _valid_offline_operator_not_found(record):
        return False

    if manual_replay:
        raw_version = record.get("matcher_version")
        if (
            isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version < 0
            or raw_version > MATCHER_VERSION
        ):
            return True
        if raw_version == MATCHER_VERSION:
            return False
        return not (
            record.get("terminal") is True
            and _strict_current_version(
                record.get("decision_version"), DECISION_VERSION
            )
            and _strict_current_version(
                record.get("query_policy"), QUERY_POLICY_VERSION
            )
            and _strict_current_version(
                record.get("evidence_schema_version"),
                AITHER_EVIDENCE_SCHEMA_VERSION,
            )
            and _current_bound_query_evidence(record)
        )

    matcher_is_legacy = "matcher_version" not in record
    if "matcher_version" in record:
        value = record.get("matcher_version")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MATCHER_VERSION
        ):
            return True
        matcher_is_legacy = value < MATCHER_VERSION
    for key, expected in (
        ("query_policy", QUERY_POLICY_VERSION),
        ("evidence_schema_version", AITHER_EVIDENCE_SCHEMA_VERSION),
    ):
        if key not in record:
            if not matcher_is_legacy:
                return True
            continue
        if not _strict_current_version(record.get(key), expected):
            return True
    return False


def _valid_offline_operator_not_found(record: Mapping[str, Any]) -> bool:
    marker = record.get("operator_audit")
    previous = record.get("previous_decision")
    if not isinstance(marker, Mapping) or not isinstance(previous, Mapping):
        return False
    if set(marker) != {
        "version",
        "classification",
        "basis",
        "audited_at",
        "network_requests",
    }:
        return False
    basis = marker.get("basis")
    audited_at = marker.get("audited_at")
    if (
        not _strict_current_version(marker.get("version"), 1)
        or marker.get("classification") != "NOT_FOUND"
        or not _strict_current_version(marker.get("network_requests"), 0)
        or not isinstance(basis, str)
        or not basis.strip()
        or "\x00" in basis
        or len(basis) > 4096
        or not isinstance(audited_at, str)
        or record.get("checked_at") != audited_at
    ):
        return False
    try:
        parsed_time = datetime.fromisoformat(audited_at)
    except ValueError:
        return False
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        return False
    return (
        record.get("status") == "NOT_FOUND"
        and record.get("terminal") is True
        and _strict_current_version(
            record.get("decision_version"), DECISION_VERSION
        )
        and _strict_current_version(record.get("query_policy"), 2)
        and _strict_current_version(record.get("evidence_schema_version"), 2)
        and (
            _strict_current_version(record.get("matcher_version"), 1)
            or _strict_current_version(
                record.get("matcher_version"), MATCHER_VERSION
            )
        )
        and record.get("reason")
        == "OFFLINE_OPERATOR_AUDIT_DISTINCT_OR_ABSENT"
        and record.get("proven_not_found") is True
        and str(previous.get("status") or "").upper() == "NOT_FOUND"
        and "operator_audit" not in previous
    )


def _cache_schema_marker(mapping: Mapping[str, Any]) -> tuple[bool, int | None]:
    """Return a trusted current marker or a marker-less legacy schema."""

    if "evidence_schema_version" not in mapping:
        return True, None
    value = mapping.get("evidence_schema_version")
    if not _strict_current_version(value, AITHER_EVIDENCE_SCHEMA_VERSION):
        return False, None
    return True, AITHER_EVIDENCE_SCHEMA_VERSION


def _cached_page_for_matcher(
    store: StateStore,
    parsed: Mapping[str, Any],
    evidence: Any,
    seen_keys: set[str],
) -> tuple[AitherPage, str, int | None, str | None] | None:
    """Load one evidence-bound page without constructing an HTTP client call."""

    if not isinstance(evidence, Mapping):
        return None
    lookup = str(evidence.get("lookup") or "").lower()
    tmdb_id: int | None = None
    imdb_id: str | None = None
    try:
        if lookup == "tmdb":
            if isinstance(evidence.get("tmdb_id"), bool):
                return None
            tmdb_id = int(evidence.get("tmdb_id"))
            if tmdb_id <= 0 or evidence.get("imdb_id") not in {None, ""}:
                return None
            params = aither_params(parsed, tmdb_id=tmdb_id)
        elif lookup == "imdb":
            imdb_id = normalize_imdb_id(evidence.get("imdb_id"))
            if imdb_id is None or evidence.get("tmdb_id") not in {None, ""}:
                return None
            params = aither_params(parsed, imdb_id=imdb_id)
        else:
            return None
    except (TypeError, ValueError):
        return None

    cache_key = evidence.get("cache_key")
    expected_key = AitherClient._cache_key(params)
    if (
        not isinstance(cache_key, str)
        or cache_key != expected_key
        or cache_key in seen_keys
        or evidence.get("complete") is not True
        or evidence.get("has_next") is not False
    ):
        return None
    evidence_count = evidence.get("candidate_count")
    if not isinstance(evidence_count, int) or isinstance(evidence_count, bool):
        return None
    evidence_digest = evidence.get("evidence_digest")
    evidence_attempt_nonce = evidence.get("attempt_nonce")
    if not _valid_evidence_digest(evidence_digest):
        return None
    if not _valid_attempt_nonce(evidence_attempt_nonce):
        return None
    if not _strict_current_version(
        evidence.get("requested_per_page"), int(params["perPage"])
    ):
        return None
    if "query_policy" in evidence and not _strict_current_version(
        evidence.get("query_policy"), QUERY_POLICY_VERSION
    ):
        return None

    evidence_schema_valid, evidence_schema = _cache_schema_marker(evidence)
    if not evidence_schema_valid:
        return None

    cached = store.get_query(cache_key)
    if not isinstance(cached, Mapping):
        return None
    cached_schema_valid, cached_schema = _cache_schema_marker(cached)
    if not cached_schema_valid or cached_schema != evidence_schema:
        return None
    values = cached.get("data")
    cached_count = cached.get("candidate_count")
    cached_digest = cached.get("evidence_digest")
    cached_attempt_nonce = cached.get("attempt_nonce")
    if (
        not _strict_current_version(
            cached.get("query_policy"), QUERY_POLICY_VERSION
        )
        or not isinstance(values, list)
        or any(not isinstance(item, Mapping) for item in values)
        or not isinstance(cached_count, int)
        or isinstance(cached_count, bool)
        or cached_count != len(values)
        or evidence_count != len(values)
        or cached.get("complete") is not True
        or cached.get("has_next") is not False
        or not _strict_current_version(
            cached.get("requested_per_page"), int(params["perPage"])
        )
        or not _valid_evidence_digest(cached_digest)
        or not _valid_attempt_nonce(cached_attempt_nonce)
        or cached_attempt_nonce != evidence_attempt_nonce
    ):
        return None
    expected_digest = _aither_page_digest(
        values,
        has_next=False,
        complete=True,
        evidence_schema_version=cached_schema,
        requested_per_page=int(params["perPage"]),
        attempt_nonce=str(cached_attempt_nonce),
    )
    if cached_digest != expected_digest or evidence_digest != cached_digest:
        return None

    seen_keys.add(cache_key)
    return (
        AitherPage(
            data=tuple(dict(item) for item in values),
            has_next=False,
            complete=True,
            cache_key=cache_key,
            from_cache=True,
            evidence_schema_version=cached_schema,
            evidence_digest=cached_digest,
            requested_per_page=int(params["perPage"]),
            attempt_nonce=str(cached_attempt_nonce),
        ),
        lookup,
        tmdb_id,
        imdb_id,
    )


def _record_outcome_identity(
    record: Mapping[str, Any],
) -> tuple[str | None, int | None, str | None]:
    lookup = str(record.get("lookup") or "").lower() or None
    tmdb_id: int | None = None
    if lookup == "tmdb" and not isinstance(record.get("tmdb_id"), bool):
        try:
            value = int(record.get("tmdb_id"))
        except (TypeError, ValueError):
            pass
        else:
            tmdb_id = value if value > 0 else None
    imdb_id = normalize_imdb_id(record.get("imdb_id")) if lookup == "imdb" else None
    return lookup, tmdb_id, imdb_id


def _normalized_cached_file_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    if (
        not path
        or "\x00" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
    ):
        return None
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts or parts == (".",):
        return None
    return "/".join(parts)


def _safe_cached_file_path(value: Any) -> bool:
    return _normalized_cached_file_path(value) is not None


def _cached_candidate_files_valid(attrs: Mapping[str, Any]) -> bool:
    """Validate tracker file evidence without inventing missing facts."""

    total_keys = [
        key for key in ("size", "total_size", "disc_size") if key in attrs
    ]
    declared_totals: list[int] = []
    for key in total_keys:
        value = attrs[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
        declared_totals.append(value)
    if declared_totals and len(set(declared_totals)) != 1:
        return False

    files = attrs.get("files")
    if isinstance(files, (str, bytes)):
        return False
    if files is None:
        return True
    declared_counts: list[int] = []
    file_items: Sequence[Any] | None = None
    if isinstance(files, Mapping):
        nested_keys = [key for key in ("data", "files") if key in files]
        count_keys = [key for key in ("count", "total") if key in files]
        if not nested_keys and not count_keys:
            return False
        if len(nested_keys) > 1:
            return False
        if nested_keys:
            nested = files[nested_keys[0]]
            if (
                not isinstance(nested, Sequence)
                or isinstance(nested, (str, bytes))
            ):
                return False
            file_items = nested
        for key in count_keys:
            value = files[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
            declared_counts.append(value)
    elif isinstance(files, Sequence):
        file_items = files
    elif isinstance(files, int) and not isinstance(files, bool) and files >= 0:
        # Some UNIT3D responses expose only the total file count.
        return True
    else:
        return False

    if declared_counts and len(set(declared_counts)) != 1:
        return False
    if file_items is None:
        return bool(declared_counts)
    if declared_counts and declared_counts[0] != len(file_items):
        return False

    file_total = 0
    for item in file_items:
        if not isinstance(item, Mapping):
            return False
        path_aliases = [key for key in ("name", "path") if key in item]
        if not path_aliases:
            return False
        normalized_paths = [
            _normalized_cached_file_path(item[key]) for key in path_aliases
        ]
        if any(path is None for path in normalized_paths) or len(
            set(normalized_paths)
        ) != 1:
            return False
        size_keys = [key for key in ("size", "length", "bytes") if key in item]
        if not size_keys or any(
            isinstance(item[key], bool)
            or not isinstance(item[key], int)
            or item[key] < 0
            for key in size_keys
        ):
            return False
        if len({item[key] for key in size_keys}) != 1:
            return False
        file_total += item[size_keys[0]]

    if declared_totals and declared_totals[0] < file_total:
        return False
    return True


def _candidate_category_valid(
    parsed: Mapping[str, Any], attrs: Mapping[str, Any]
) -> bool:
    """Validate optional Aither category echoes against the local media type."""

    has_category = "category" in attrs
    id_keys = [key for key in ("category_id", "categoryId") if key in attrs]
    if not has_category and not id_keys:
        return True
    expected = CATEGORY_IDS.get(str(parsed.get("media_type") or "").lower())
    if expected is None:
        return False

    observed: list[int] = []
    if has_category:
        raw_category = attrs["category"]
        if not isinstance(raw_category, str):
            return False
        category_id = {"movie": 1, "tv": 2}.get(
            raw_category.strip().casefold()
        )
        if category_id is None:
            return False
        observed.append(category_id)
    for key in id_keys:
        value = attrs[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value not in {1, 2}
        ):
            return False
        observed.append(value)
    return bool(observed) and len(set(observed)) == 1 and observed[0] == expected


_AITHER_RELEASE_TYPE_BY_ID = {
    1: "Full Disc",
    2: "Remux",
    3: "Encode",
    4: "WEB-DL",
    5: "WEBRip",
    6: "HDTV",
}
_AITHER_RESOLUTION_NAMES_BY_ID = {
    1: frozenset({"4320p"}),
    2: frozenset({"2160p"}),
    # Upload Assistant maps both 1440p and 1080p to UNIT3D resolution 3.
    3: frozenset({"1440p", "1080p"}),
    4: frozenset({"1080i"}),
    5: frozenset({"720p"}),
    6: frozenset({"576p"}),
    7: frozenset({"576i"}),
    8: frozenset({"480p"}),
    9: frozenset({"480i"}),
    10: frozenset({"8640p"}),
}


def _canonical_small_id(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= maximum:
        return value
    if isinstance(value, str) and re.fullmatch(
        rf"(?:[1-9]|10)" if maximum == 10 else rf"[1-{maximum}]",
        value,
    ):
        parsed = int(value)
        return parsed if parsed <= maximum else None
    return None


def _canonical_candidate_type(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > 128
    ):
        return None
    compact = re.sub(r"[\s._-]+", "", value).casefold()
    if compact in {"disc", "fulldisc", "bdmv"}:
        return "Full Disc"
    if compact == "remux":
        return "Remux"
    if compact in {"encode", "encoded"}:
        return "Encode"
    if compact in {"webdl", "webdownload"}:
        return "WEB-DL"
    if compact == "webrip":
        return "WEBRip"
    if compact == "hdtv":
        return "HDTV"
    return None


def _candidate_type_id_valid(attrs: Mapping[str, Any]) -> bool:
    """Validate flat and nested UNIT3D type forms as one consistent fact."""

    normalized_ids: list[int] = []
    normalized_names: list[str] = []
    for key in ("type_id", "typeId"):
        if key in attrs:
            parsed = _canonical_small_id(attrs[key], maximum=6)
            if parsed is None:
                return False
            normalized_ids.append(parsed)

    if "type" in attrs:
        raw_type = attrs["type"]
        if isinstance(raw_type, Mapping):
            if not raw_type or not set(raw_type).issubset({"id", "name", "type"}):
                return False
            for key in ("name", "type"):
                if key in raw_type:
                    parsed_name = _canonical_candidate_type(raw_type[key])
                    if parsed_name is None:
                        return False
                    normalized_names.append(parsed_name)
            if "id" in raw_type:
                parsed_id = _canonical_small_id(raw_type["id"], maximum=6)
                if parsed_id is None:
                    return False
                normalized_ids.append(parsed_id)
            if not normalized_names and not normalized_ids:
                return False
        else:
            parsed_name = _canonical_candidate_type(raw_type)
            if parsed_name is None:
                return False
            normalized_names.append(parsed_name)

    if normalized_ids and len(set(normalized_ids)) != 1:
        return False
    if normalized_names and len(set(normalized_names)) != 1:
        return False
    return not (
        normalized_ids
        and normalized_names
        and _AITHER_RELEASE_TYPE_BY_ID[normalized_ids[0]] != normalized_names[0]
    )


def _canonical_resolution_name(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value) > 64
    ):
        return None
    normalized = value.strip().casefold()
    known = {
        item
        for names in _AITHER_RESOLUTION_NAMES_BY_ID.values()
        for item in names
    }
    return normalized if normalized in known else None


def _candidate_resolution_valid(attrs: Mapping[str, Any]) -> bool:
    names: list[str] = []
    ids: list[int] = []
    if "resolution_id" in attrs:
        parsed_id = _canonical_small_id(attrs["resolution_id"], maximum=10)
        if parsed_id is None:
            return False
        ids.append(parsed_id)

    if "resolution" in attrs:
        raw_resolution = attrs["resolution"]
        if isinstance(raw_resolution, Mapping):
            if (
                not raw_resolution
                or not set(raw_resolution).issubset({"id", "name", "resolution"})
            ):
                return False
            for key in ("name", "resolution"):
                if key in raw_resolution:
                    parsed_name = _canonical_resolution_name(raw_resolution[key])
                    if parsed_name is None:
                        return False
                    names.append(parsed_name)
            if "id" in raw_resolution:
                parsed_id = _canonical_small_id(
                    raw_resolution["id"], maximum=10
                )
                if parsed_id is None:
                    return False
                ids.append(parsed_id)
            # An ID-only mapping is invisible to the current matcher.  Do not
            # allow it to silently fall back to the release name.
            if not names:
                return False
        else:
            parsed_name = _canonical_resolution_name(raw_resolution)
            if parsed_name is None:
                return False
            names.append(parsed_name)

    # A top-level ID without its human-readable resolution would otherwise be
    # dropped by the matcher, so require both sides of that API echo.
    if ids and not names:
        return False
    if names and len(set(names)) != 1:
        return False
    if ids and len(set(ids)) != 1:
        return False
    return not (
        names
        and ids
        and names[0] not in _AITHER_RESOLUTION_NAMES_BY_ID[ids[0]]
    )


def _candidate_scalar_evidence_valid(attrs: Mapping[str, Any]) -> bool:
    for key in (
        "codec",
        "audio",
        "audio_codec",
        "audio_format",
        "audio_channels",
    ):
        if key not in attrs:
            continue
        value = attrs[key]
        if (
            not isinstance(value, str)
            or not value.strip()
            or "\x00" in value
            or len(value) > 4096
        ):
            return False

    if "codec" in attrs:
        codec_tokens: set[str] = set()
        codec_text = attrs["codec"]
        codec_patterns = (
            ("H265", r"(?:h[ ._-]?265|x265|hevc)"),
            ("H264", r"(?:h[ ._-]?264|x264|avc)"),
            ("VC1", r"vc[ ._-]?1"),
            ("AV1", r"av[ ._-]?1"),
            ("MPEG2", r"mpeg[ ._-]?2"),
        )
        for canonical, pattern in codec_patterns:
            if re.search(
                rf"(?i)(?<![a-z0-9]){pattern}(?![a-z0-9])",
                codec_text,
            ):
                codec_tokens.add(canonical)
        if len(codec_tokens) != 1:
            return False

    audio_evidence: dict[str, tuple[set[str], set[str]]] = {}
    for key in ("audio", "audio_codec", "audio_format", "audio_channels"):
        if key not in attrs:
            continue
        formats, channels = _audio_evidence(attrs[key])
        format_set = set(formats)
        channel_set = set(channels)
        audio_evidence[key] = (format_set, channel_set)
        object_audio, dts_profiles = _audio_profile_evidence(attrs[key])
        if key == "audio":
            if not (
                format_set or channel_set or object_audio or dts_profiles
            ):
                return False
        elif key in {"audio_codec", "audio_format"}:
            if not format_set:
                return False
        elif not channel_set:
            return False

    format_keys = [
        key for key in ("audio_codec", "audio_format") if key in audio_evidence
    ]
    if len(format_keys) == 2 and audio_evidence[format_keys[0]][0].isdisjoint(
        audio_evidence[format_keys[1]][0]
    ):
        return False
    if "audio" in audio_evidence:
        generic_formats, generic_channels = audio_evidence["audio"]
        for key in format_keys:
            role_formats = audio_evidence[key][0]
            if generic_formats and role_formats.isdisjoint(generic_formats):
                return False
        if "audio_channels" in audio_evidence:
            role_channels = audio_evidence["audio_channels"][1]
            if generic_channels and role_channels.isdisjoint(generic_channels):
                return False

    bit_depths: list[int] = []
    for key in ("bit_depth", "bitDepth"):
        if key not in attrs:
            continue
        value = attrs[key]
        if isinstance(value, bool):
            return False
        if isinstance(value, int) and value in {8, 10}:
            bit_depths.append(value)
            continue
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if re.fullmatch(r"(?:8|8[ ._-]?bits?)", normalized):
                bit_depths.append(8)
                continue
            if re.fullmatch(r"(?:10|10[ ._-]?bits?|hi10p)", normalized):
                bit_depths.append(10)
                continue
        return False
    return not bit_depths or len(set(bit_depths)) == 1


def _candidate_identity_evidence_valid(attrs: Mapping[str, Any]) -> bool:
    if "tmdb_id" in attrs:
        value = attrs["tmdb_id"]
        if isinstance(value, bool) or not (
            (isinstance(value, int) and value > 0)
            or (
                isinstance(value, str)
                and re.fullmatch(r"[1-9]\d*", value) is not None
            )
        ):
            return False
    if "imdb_id" in attrs:
        value = attrs["imdb_id"]
        if isinstance(value, bool) or not (
            (isinstance(value, int) and value > 0)
            or (
                isinstance(value, str)
                and re.fullmatch(r"(?:tt)?[1-9]\d{0,9}", value) is not None
            )
        ):
            return False
    return True


_HDR_ATOM_RE = re.compile(
    r"(?i)(?:dolby[ ._-]?vision|dovi|hdr10\+|hdr10[ ._-]?plus|hdr10|"
    r"hdr|hlg|dv|sdr|no[ ._-]?hdr|none|false|0)"
)
_HDR_NEGATIVE_RE = re.compile(
    r"(?i)(?:no[ ._-]?hdr|sdr|none|false|(?<!\d)0(?!\d))"
)
_HDR_POSITIVE_RE = re.compile(
    r"(?i)(?:dolby[ ._-]?vision|dovi|hdr10\+|hdr10[ ._-]?plus|"
    r"hdr10|hdr|hlg|dv)"
)


def _hdr_atom_valid(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > 512
    ):
        return False
    stripped = _HDR_ATOM_RE.sub("", value)
    return not re.sub(r"[\s,;/|+._()\[\]-]+", "", stripped)


def _candidate_hdr_valid(attrs: Mapping[str, Any]) -> bool:
    if "hdr" not in attrs or attrs["hdr"] is None:
        return True
    value = attrs["hdr"]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value or len(value) > 16:
            return False
        values = tuple(value)
    else:
        return False
    if not all(_hdr_atom_valid(item) for item in values):
        return False
    has_negative = any(_HDR_NEGATIVE_RE.search(item) for item in values)
    has_positive = any(
        _HDR_POSITIVE_RE.search(_HDR_NEGATIVE_RE.sub("", item))
        for item in values
    )
    return not (has_negative and has_positive)


def _candidate_reports_valid(attrs: Mapping[str, Any]) -> bool:
    for key in ("media_info", "bd_info"):
        if key not in attrs or attrs[key] is None:
            continue
        value = attrs[key]
        if (
            not isinstance(value, str)
            or "\x00" in value
            or len(value) > 8 * 1024 * 1024
        ):
            return False
    return True


def _label_shape_bounded(
    value: Any, *, mapping_fields: frozenset[str], depth: int = 0
) -> bool:
    if value is None:
        return True
    if depth > 4:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and "\x00" not in value and len(value) <= 512
    if isinstance(value, Mapping):
        return (
            bool(value)
            and set(value).issubset(mapping_fields)
            and all(
                _label_shape_bounded(
                    item,
                    mapping_fields=mapping_fields,
                    depth=depth + 1,
                )
                for item in value.values()
            )
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value) <= 128 and all(
            _label_shape_bounded(
                item,
                mapping_fields=mapping_fields,
                depth=depth + 1,
            )
            for item in value
        )
    return False


def _candidate_disc_labels_valid(attrs: Mapping[str, Any]) -> bool:
    if "disc_regions" in attrs:
        value = attrs["disc_regions"]
        if not _label_shape_bounded(
            value, mapping_fields=frozenset({"name", "code", "region"})
        ):
            return False
        _, invalid = _structured_region_evidence(value)
        if invalid:
            return False
    if "disc_editions" in attrs:
        value = attrs["disc_editions"]
        if not _label_shape_bounded(
            value, mapping_fields=frozenset({"name", "code", "edition"})
        ):
            return False
        _, invalid = _structured_edition_evidence(value)
        if invalid:
            return False
    return True


def _candidate_manifest_fields_valid(attrs: Mapping[str, Any]) -> bool:
    fingerprint_keys = (
        "disc_manifest_fingerprint",
        "bdmv_manifest_fingerprint",
        "manifest_fingerprint",
    )
    fingerprints: dict[str, str] = {}
    for key in fingerprint_keys:
        if key not in attrs:
            continue
        value = attrs[key]
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()) is None
        ):
            return False
        fingerprints[key] = value.strip().casefold()
    if len(set(fingerprints.values())) > 1:
        return False
    matcher_fingerprint = bool(
        {"disc_manifest_fingerprint", "bdmv_manifest_fingerprint"}
        & fingerprints.keys()
    )
    if "manifest_fingerprint" in fingerprints and not matcher_fingerprint:
        return False

    if "disc_manifest_version" in attrs:
        value = attrs["disc_manifest_version"]
        if (
            not matcher_fingerprint
            or not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value.strip()) is None
        ):
            return False
    if "disc_manifest_kind" in attrs:
        value = attrs["disc_manifest_kind"]
        if (
            not matcher_fingerprint
            or not isinstance(value, str)
            or value.strip().upper() not in {"BDMV", "VIDEO_TS", "ISO"}
        ):
            return False
    return True


def _canonical_episode_values(value: Any) -> tuple[int, ...] | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return (value,) if 0 <= value <= 999 else None
    if isinstance(value, str):
        text = value.strip()
        single = re.fullmatch(r"(?i)(?:E)?(0|[1-9]\d{0,2})", text)
        if single:
            return (int(single.group(1)),)
        episode_range = re.fullmatch(
            r"(?i)(?:E)?(0|[1-9]\d{0,2})[-:](?:E)?(0|[1-9]\d{0,2})",
            text,
        )
        if episode_range:
            first, last = map(int, episode_range.groups())
            return tuple(range(first, last + 1)) if last >= first else None
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        combined: set[int] = set()
        for item in value:
            parsed = _canonical_episode_values(item)
            if parsed is None:
                return None
            combined.update(parsed)
        return tuple(sorted(combined)) if combined else None
    return None


def _candidate_episode_aliases_valid(attrs: Mapping[str, Any]) -> bool:
    seasons: list[tuple[int, ...]] = []
    for key in ("season_number", "seasonNumber"):
        if key in attrs:
            parsed = _canonical_episode_values(attrs[key])
            if parsed is None or len(parsed) != 1:
                return False
            seasons.append(parsed)
    if seasons and len(set(seasons)) != 1:
        return False

    episodes: list[tuple[int, ...]] = []
    for key in ("episode_number", "episodeNumber"):
        if key in attrs:
            parsed = _canonical_episode_values(attrs[key])
            if parsed is None:
                return False
            episodes.append(parsed)
    return not episodes or len(set(episodes)) == 1


def _validated_page_candidates(
    parsed: Mapping[str, Any],
    page: AitherPage,
    *,
    tmdb_id: Any = None,
    imdb_id: Any = None,
) -> tuple[dict[str, Any], ...] | None:
    """Return candidates only when the whole tracker page is trustworthy."""

    for item in page.data:
        if not isinstance(item, Mapping):
            return None
        attrs = item.get("attributes")
        if not isinstance(attrs, Mapping):
            return None
        release_name = attrs.get("name")
        if (
            not isinstance(release_name, str)
            or not release_name.strip()
            or "\x00" in release_name
            or len(release_name) > 4096
            or not _candidate_category_valid(parsed, attrs)
            or not _candidate_type_id_valid(attrs)
            or not _candidate_resolution_valid(attrs)
            or not _candidate_scalar_evidence_valid(attrs)
            or not _candidate_identity_evidence_valid(attrs)
            or not _candidate_episode_aliases_valid(attrs)
            or not _candidate_hdr_valid(attrs)
            or not _candidate_reports_valid(attrs)
            or not _candidate_disc_labels_valid(attrs)
            or not _candidate_manifest_fields_valid(attrs)
            or not _cached_candidate_files_valid(attrs)
        ):
            return None
    candidates = _identity_candidates(page, tmdb_id=tmdb_id, imdb_id=imdb_id)
    return candidates if len(candidates) == len(page.data) else None


def _reevaluate_not_found_from_cache(
    parsed: Mapping[str, Any],
    record: Mapping[str, Any],
    store: StateStore,
) -> CheckOutcome:
    """Apply the current matcher to durable pages, without any provider call."""

    raw_evidence = record.get("query_evidence")
    if (
        not isinstance(raw_evidence, Sequence)
        or isinstance(raw_evidence, (str, bytes))
        or not raw_evidence
    ):
        lookup, tmdb_id, imdb_id = _record_outcome_identity(record)
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            message="A tartos Aither-cache bizonyiteka hianyos vagy ervenytelen",
            reason="CACHED_MATCHER_CACHE_INVALID",
        )

    combined_evidence: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    invalid = len(raw_evidence) > 2
    indeterminate = False
    loaded_chain: list[
        tuple[str, int | None, str | None, AitherPage]
    ] = []
    last_identity: tuple[str | None, int | None, str | None] = (
        _record_outcome_identity(record)
    )
    matched: Mapping[str, Any] | None = None
    matched_identity: tuple[str, int | None, str | None] | None = None

    for raw_item in raw_evidence:
        loaded = _cached_page_for_matcher(store, parsed, raw_item, seen_keys)
        if loaded is None:
            invalid = True
            continue
        page, lookup, tmdb_id, imdb_id = loaded
        loaded_chain.append((lookup, tmdb_id, imdb_id, page))
        last_identity = (lookup, tmdb_id, imdb_id)
        combined_evidence.append(
            _query_evidence(
                page,
                lookup,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
            )
        )
        if (
            page.data
            and not _strict_current_version(
                page.evidence_schema_version,
                AITHER_EVIDENCE_SCHEMA_VERSION,
            )
        ):
            # Pre-schema non-empty pages may have lost a matcher conflict
            # during sanitization.  They can prove neither a match nor an
            # absence under the current policy.  Empty complete pages remain
            # monotonic evidence that the exact identity query had no rows.
            invalid = True
            continue
        candidates = _validated_page_candidates(
            parsed,
            page,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
        )
        if candidates is None:
            invalid = True
            continue
        try:
            decision, candidate = evaluate_candidates(parsed, candidates)
        except Exception:
            # Persisted input is untrusted state.  Any matcher failure is a
            # local MANUAL outcome and must never fall through to HTTP.
            invalid = True
            continue
        if decision is TechnicalDecision.MATCH and matched is None:
            matched = candidate
            matched_identity = (lookup, tmdb_id, imdb_id)
        elif decision is TechnicalDecision.INDETERMINATE:
            indeterminate = True

    record_identity = _record_outcome_identity(record)
    if len(loaded_chain) != len(raw_evidence):
        invalid = True
    elif len(loaded_chain) == 1:
        if loaded_chain[0][:3] != record_identity:
            invalid = True
    elif len(loaded_chain) == 2:
        first, second = loaded_chain
        if (
            first[0] != "tmdb"
            or second[0] != "imdb"
            or bool(first[3].data)
            or second[:3] != record_identity
        ):
            invalid = True
    else:
        invalid = True

    evidence_tuple = tuple(combined_evidence)
    lookup, tmdb_id, imdb_id = last_identity
    if invalid or not combined_evidence:
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            message="A tartos Aither-cache ervenytelen vagy hianyos",
            reason="CACHED_MATCHER_CACHE_INVALID",
            query_evidence=evidence_tuple,
        )

    if matched is not None and matched_identity is not None:
        lookup, tmdb_id, imdb_id = matched_identity
        return CheckOutcome(
            status="DUPLICATE",
            lookup=lookup,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            message="A jelenlegi matcher egyezest talalt a tartos Aither-cache-ben",
            reason="CACHED_MATCHER_MATCH",
            matched_evidence=sanitize_aither_candidate(matched),
            query_evidence=evidence_tuple,
        )

    if indeterminate:
        return CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            message="A jelenlegi matcher nem tud biztonsagosan donteni a cache-bol",
            reason="CACHED_MATCHER_INDETERMINATE",
            query_evidence=evidence_tuple,
        )

    return CheckOutcome(
        status="NOT_FOUND",
        lookup=lookup,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        message="Minden teljes tartos Aither-oldal technikai eredmenye NO_MATCH",
        reason="CACHED_MATCHER_NO_MATCH",
        query_evidence=evidence_tuple,
        proven_not_found=True,
    )


def _known_decision_identity(
    record: Mapping[str, Any],
) -> tuple[str | None, int | None, str | None]:
    """Find the nearest persisted catalogue IDs in a decision audit chain."""

    current: Mapping[str, Any] | None = record
    visited: set[int] = set()
    for _ in range(16):
        if current is None or id(current) in visited:
            break
        visited.add(id(current))
        lookup = str(current.get("lookup") or "").lower() or None
        tmdb_id: int | None = None
        raw_tmdb = current.get("tmdb_id")
        if not isinstance(raw_tmdb, bool):
            try:
                parsed_tmdb = int(raw_tmdb)
            except (TypeError, ValueError):
                pass
            else:
                tmdb_id = parsed_tmdb if parsed_tmdb > 0 else None
        imdb_id = normalize_imdb_id(current.get("imdb_id"))
        if tmdb_id is not None or imdb_id is not None:
            if lookup not in {"tmdb", "imdb"}:
                lookup = (
                    "tmdb"
                    if tmdb_id is not None and imdb_id is None
                    else "imdb" if imdb_id is not None and tmdb_id is None else None
                )
            return lookup, tmdb_id, imdb_id
        nested = current.get("previous_decision")
        current = nested if isinstance(nested, Mapping) else None
    return None, None, None


def _record_spent_aither_failure(
    store: StateStore,
    key: str,
    name: str,
    parsed: Mapping[str, Any],
    previous: Mapping[str, Any],
    *,
    details: str,
) -> None:
    """Freeze a spent request as manual without ever authorizing a retry."""

    outcome = _spent_aither_failure_outcome(previous, details=details)
    record_terminal(
        store,
        key,
        name,
        parsed,
        outcome,
        previous_decision=previous,
    )


def _spent_aither_failure_outcome(
    previous: Mapping[str, Any], *, details: str
) -> CheckOutcome:
    lookup, tmdb_id, imdb_id = _known_decision_identity(previous)
    return CheckOutcome(
        status="MANUAL",
        lookup=lookup,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        message=(
            "Az elkoltott Aither-keres nem adott ertekelheto oldalt; "
            f"az ismetelt kereses tiltott. {details}"
        ),
        reason="AITHER_ATTEMPT_FAILED_NO_RETRY",
    )


def _resume_persisted_attempt(
    parsed: Mapping[str, Any],
    marker: Mapping[str, Any],
    aither: Any,
) -> CheckOutcome:
    """Evaluate one crash marker only from its exact durable query page."""

    def failed(details: str) -> CheckOutcome:
        return _spent_aither_failure_outcome(marker, details=details)

    if (
        str(marker.get("status") or "").upper() != "AITHER_ATTEMPTED"
        or marker.get("terminal") is not True
        or str(marker.get("reason") or "").upper() != "AITHER_HTTP_RESERVED"
        or not _strict_current_version(
            marker.get("decision_version"), DECISION_VERSION
        )
        or not _strict_current_version(
            marker.get("query_policy"), QUERY_POLICY_VERSION
        )
        or not _strict_current_version(
            marker.get("evidence_schema_version"),
            AITHER_EVIDENCE_SCHEMA_VERSION,
        )
        or not _strict_current_version(
            marker.get("matcher_version"), MATCHER_VERSION
        )
    ):
        return failed("A tartos Aither-marker provenance adata hibas")

    marker_parsed = marker.get("parsed")
    attempt_nonce = marker.get("attempt_nonce")
    if not _valid_attempt_nonce(attempt_nonce):
        return failed("A tartos Aither-marker attempt nonce adata hibas")
    if not isinstance(marker_parsed, Mapping):
        return failed("A tartos Aither-markerbol hianyzik a keresesi kategoria")
    marker_media_type = marker_parsed.get("media_type")
    marker_season = marker_parsed.get("season")
    current_media_type = parsed.get("media_type")
    current_season = parsed.get("season")
    valid_season = marker_season is None or (
        isinstance(marker_season, int)
        and not isinstance(marker_season, bool)
        and marker_season >= 0
    )
    current_season_valid = current_season is None or (
        isinstance(current_season, int)
        and not isinstance(current_season, bool)
        and current_season >= 0
    )
    if (
        "media_type" not in marker_parsed
        or "season" not in marker_parsed
        or marker_media_type not in CATEGORY_IDS
        or current_media_type != marker_media_type
        or not valid_season
        or not current_season_valid
        or current_season != marker_season
    ):
        return failed("Az Aither-marker kategoria/szezon kotese elter")

    lookup = marker.get("lookup")
    tmdb_id: int | None = None
    imdb_id: str | None = None
    try:
        if lookup == "tmdb":
            raw_tmdb = marker.get("tmdb_id")
            if (
                isinstance(raw_tmdb, bool)
                or not isinstance(raw_tmdb, int)
                or raw_tmdb <= 0
                or marker.get("imdb_id") is not None
            ):
                return failed("Az Aither-marker TMDb-azonositoja hibas")
            tmdb_id = raw_tmdb
            params = aither_params(marker_parsed, tmdb_id=tmdb_id)
        elif lookup == "imdb":
            raw_imdb = marker.get("imdb_id")
            imdb_id = normalize_imdb_id(raw_imdb)
            if (
                imdb_id is None
                or raw_imdb != imdb_id
                or marker.get("tmdb_id") is not None
            ):
                return failed("Az Aither-marker IMDb-azonositoja hibas")
            params = aither_params(marker_parsed, imdb_id=imdb_id)
        else:
            return failed("Az Aither-marker lookup tipusa hibas")
    except (TypeError, ValueError):
        return failed("Az Aither-marker keresesi parameterei hibasak")

    loader = getattr(aither, "load_persistent_once", None)
    if not callable(loader):
        return failed("A tartos Aither-cache nem erheto el")
    try:
        page = loader(params, attempt_nonce=str(attempt_nonce))
    except (AitherError, TypeError, ValueError):
        return failed("A markerhez tartozo cache hianyzik vagy ervenytelen")

    # A normal TMDb-empty lookup would require a new IMDb fallback.  A crash
    # marker cannot resolve that fallback without repeating provider work, so
    # freeze it rather than claiming absence from an incomplete identity chain.
    try:
        if lookup == "tmdb" and page.complete and not page.data:
            return failed(
                "A TMDb-cache ures; a tiltott IMDb fallback nelkul nem vegleges"
            )
        outcome = evaluate_page(
            parsed,
            page,
            lookup=str(lookup),
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
        )
    except Exception:
        # The persisted page is untrusted input; a matcher/schema exception is
        # a local quarantine decision and must never reopen the HTTP path.
        return failed("A markerhez tartozo cache nem ertekelheto biztonsagosan")
    if outcome.reason in {
        "UNTRUSTED_AITHER_EVIDENCE_SCHEMA",
        "INVALID_AITHER_EVIDENCE_DIGEST",
        "INVALID_AITHER_CANDIDATE_PAGE",
    }:
        return failed("A markerhez tartozo cache bizonyiteka ervenytelen")
    return outcome


def _record_legacy_no_repeat(
    store: StateStore,
    key: str,
    name: str,
    parsed: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> None:
    lookup, tmdb_id, imdb_id = _known_decision_identity(previous)
    record_terminal(
        store,
        key,
        name,
        parsed,
        CheckOutcome(
            status="MANUAL",
            lookup=lookup,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            message=(
                "A korabbi dontesnel az Aither-hivas hianya nem bizonyithato; "
                "az ismetelt kereses tiltott"
            ),
            reason="LEGACY_DECISION_NO_REPEAT",
        ),
        previous_decision=previous,
    )


def _quarantine_recheck(
    store: StateStore,
    key: str,
    name: str,
    parsed: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> None:
    audit = previous.get("previous_decision")
    if not isinstance(audit, Mapping):
        audit = previous
    record_terminal(
        store,
        key,
        name,
        parsed,
        CheckOutcome(
            status="RECHECK_REQUIRED",
            message=(
                "Az új identity-feloldó az Aither első keresése előtt "
                "újraértékelheti a tételt"
            ),
            reason=AUTOMATIC_IDENTITY_RECHECK_REASON,
        ),
        previous_decision=audit,
    )


@dataclass
class ScanSummary:
    total_groups: int = 0
    already_terminal: int = 0
    blacklisted: int = 0
    duplicate: int = 0
    not_found: int = 0
    unresolved: int = 0
    manual: int = 0
    not_media: int = 0
    recheck_required: int = 0
    errors: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)
    live_keys: tuple[str, ...] = ()


def scan_completed(
    qb: QbitClient,
    tmdb: TmdbClient,
    aither: AitherClient,
    store: StateStore,
    log_file: Path,
    *,
    imdb: IMDbSuggestionClient | None = None,
    repair_input: Callable[[str], str] | None = None,
    identity_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> ScanSummary:
    torrents = qb.torrents("completed")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for torrent in torrents:
        name = str(torrent.get("name") or "")
        if name:
            groups[norm_name(name)].append(torrent)

    # The normalized name is a convenient state key, but the qB infohash is
    # the stronger release identity.  Backfill every current terminal record
    # in one atomic state write, bind all future decisions, and honor legacy
    # NOT_FOUND candidates that already stored hashes.  A rename must never
    # turn a spent release into a fresh API lookup.
    existing_records = store.terminal_records()
    hash_backfill: dict[str, tuple[str, ...]] = {}
    for existing_key, group in groups.items():
        existing_record = existing_records.get(existing_key)
        current_hashes = _group_infohashes(group)
        if existing_record is None or not current_hashes:
            continue
        raw_bound = existing_record.get("torrent_hashes")
        direct_hashes = (
            _canonical_infohashes(raw_bound)
            if isinstance(raw_bound, Sequence)
            and not isinstance(raw_bound, (str, bytes))
            else ()
        )
        merged_hashes = _canonical_infohashes(
            (*_record_infohashes(existing_record), *current_hashes)
        )
        if direct_hashes != merged_hashes:
            hash_backfill[existing_key] = merged_hashes
    if hash_backfill:
        store.bind_release_hashes(hash_backfill)
        existing_records = store.terminal_records()

    prior_by_hash: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for prior_key, prior_record in sorted(existing_records.items()):
        for torrent_hash in _record_infohashes(prior_record):
            prior_by_hash[torrent_hash].append((prior_key, prior_record))

    # Refresh zero-Aither-call overrides for every existing live record before
    # the terminal fast path.  A newly refreshed blacklist or an Aither source
    # tracker must be able to revoke a stale READY decision.
    preflight_files: dict[str, list[dict[str, Any]]] = {}
    automatic_rechecks: set[str] = set()
    for key, group in sorted(groups.items()):
        previous = store.get_release(key)
        primary = group[0]
        name = str(primary.get("name") or "")
        parsed = parse_release(name)
        current_hashes = _group_infohashes(group)
        parsed["torrent_hashes"] = current_hashes
        blocked, blocked_group, rule, release_type = is_blacklisted_release(
            name, parsed
        )
        if blocked:
            if previous is None:
                # Brand-new blacklist entries are counted and recorded by the
                # normal scan loop below.  This preflight only refreshes old
                # terminal decisions before their fast-path skip.
                continue
            if str(previous.get("status") or "").upper() != "BLACKLISTED":
                outcome = CheckOutcome(
                    status="BLACKLISTED",
                    message=(
                        f"group={blocked_group}; rule={rule}; type={release_type}"
                    ),
                    reason="BLACKLIST_RULE",
                )
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    outcome,
                    previous_decision=previous,
                )
                append_event(log_file, "BLACKLISTED", name, outcome.message)
            continue

        prior_hits: list[tuple[str, dict[str, Any]]] = []
        seen_prior_keys: set[str] = set()
        for torrent_hash in current_hashes:
            for prior_key, prior_record in prior_by_hash.get(torrent_hash, ()):
                if prior_key == key or prior_key in seen_prior_keys:
                    continue
                seen_prior_keys.add(prior_key)
                prior_hits.append((prior_key, prior_record))

        previous_status = str((previous or {}).get("status") or "").upper()
        previous_prior = (
            previous.get("previous_decision")
            if isinstance(previous, Mapping)
            else None
        )
        network_eligible_previous = bool(
            previous is not None
            and (
                _pre_aither_identity_recheck(previous)
                or (
                    previous_status == "RECHECK_REQUIRED"
                    and str(previous.get("reason") or "").upper()
                    == AUTOMATIC_IDENTITY_RECHECK_REASON
                    and isinstance(previous_prior, Mapping)
                    and _pre_aither_identity_recheck(previous_prior)
                )
            )
        )
        if prior_hits and (previous is None or network_eligible_previous):
            # Multiple old keys for one hash indicate a rename chain or
            # tampered state.  Both cases are fail-closed: preserve one
            # deterministic audit source, expose every source key in the
            # message, clear READY, and perform no provider/Aither work.
            prior_hits.sort(key=lambda item: item[0])
            source_key, source_record = prior_hits[0]
            lookup, tmdb_id, imdb_id = _known_decision_identity(source_record)
            source_keys = ",".join(item[0] for item in prior_hits)
            record_terminal(
                store,
                key,
                name,
                parsed,
                CheckOutcome(
                    status="MANUAL",
                    lookup=lookup,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    message=(
                        "Ugyanez a qB infohash mar korabban ellenorizve volt "
                        f"mas nev alatt: {source_keys}"
                    ),
                    reason="PREVIOUSLY_CHECKED_INFOHASH",
                ),
                previous_decision=(previous or source_record),
            )
            append_event(
                log_file,
                "MANUAL NO-REPEAT HASH ALIAS",
                name,
                f"source={source_key}",
            )
            continue
        if previous is None:
            continue

        if _group_is_from_aither(qb, group):
            files = qb.files(str(primary.get("hash") or ""))
            preflight_files[key] = files
            parsed = enrich_release(parsed, name, primary, files)
            if not (
                str(previous.get("status") or "").upper() == "DUPLICATE"
                and previous.get("lookup") == "source_tracker"
            ):
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    CheckOutcome(
                        status="DUPLICATE",
                        lookup="source_tracker",
                        message="A qBittorrent forrás-trackere Aither",
                        reason="SOURCE_TRACKER_AITHER",
                    ),
                    previous_decision=previous,
                )
            continue

        if str(previous.get("status") or "").upper() == "API_ERROR":
            # Older/current builds used API_ERROR for an already-spent Aither
            # attempt with no evaluable page.  It is an operator-review state,
            # not a retryable API queue.  Convert it locally before the normal
            # terminal fast path and retain the complete prior decision.
            _record_spent_aither_failure(
                store,
                key,
                name,
                parsed,
                previous,
                details=str(previous.get("message") or "Aither response error"),
            )
            continue

        if str(previous.get("status") or "").upper() == "AITHER_ATTEMPTED":
            # Resolve every stranded attempt before the main loop can perform
            # any new Aither request.  Only the marker's persisted identity,
            # category and season may address the exact durable cache page;
            # catalog resolvers and HTTP are forbidden on this path.
            files = qb.files(str(primary.get("hash") or ""))
            preflight_files[key] = files
            parsed = enrich_release(parsed, name, primary, files)
            blocked, blocked_group, rule, release_type = is_blacklisted_release(
                name, parsed
            )
            if blocked:
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    CheckOutcome(
                        status="BLACKLISTED",
                        message=(
                            f"group={blocked_group}; rule={rule}; type={release_type}"
                        ),
                        reason="BLACKLIST_RULE",
                    ),
                    previous_decision=previous,
                )
                continue
            if classify_local_payload(primary, files) == "NOT_MEDIA":
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    CheckOutcome(
                        status="NOT_MEDIA",
                        lookup="local_manifest",
                        message="A manifest bizonyitottan nem film/sorozat payload",
                        reason="MANIFEST_NOT_MEDIA",
                    ),
                    previous_decision=previous,
                )
                continue
            outcome = _resume_persisted_attempt(parsed, previous, aither)
            candidate = (
                candidate_record(key, name, group, files)
                if outcome.status == "NOT_FOUND" and outcome.proven_not_found
                else None
            )
            record_terminal(
                store,
                key,
                name,
                parsed,
                outcome,
                candidate=candidate,
                previous_decision=previous,
            )
            continue

        if _cached_matcher_provenance_invalid(previous):
            lookup, tmdb_id, imdb_id = _known_decision_identity(previous)
            record_terminal(
                store,
                key,
                name,
                parsed,
                CheckOutcome(
                    status="MANUAL",
                    lookup=lookup,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    message=(
                        "A tartos matcher-ujraertekeles provenance jeloloje "
                        "hibas vagy nem tamogatott"
                    ),
                    reason="CACHED_MATCHER_PROVENANCE_INVALID",
                ),
                previous_decision=previous,
            )
            continue

        if _needs_cached_matcher_recheck(previous):
            # Re-run only the pure technical matcher against pages already
            # durably stored by this exact identity/category/season query.
            # No title resolver, provider client, or Aither client is involved.
            files = qb.files(str(primary.get("hash") or ""))
            preflight_files[key] = files
            parsed = enrich_release(parsed, name, primary, files)
            blocked, blocked_group, rule, release_type = is_blacklisted_release(
                name, parsed
            )
            if blocked:
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    CheckOutcome(
                        status="BLACKLISTED",
                        message=(
                            f"group={blocked_group}; rule={rule}; type={release_type}"
                        ),
                        reason="BLACKLIST_RULE",
                    ),
                    previous_decision=previous,
                )
                continue

            outcome = _reevaluate_not_found_from_cache(parsed, previous, store)
            candidate = (
                candidate_record(key, name, group, files)
                if outcome.status == "NOT_FOUND" and outcome.proven_not_found
                else None
            )
            record_terminal(
                store,
                key,
                name,
                parsed,
                outcome,
                candidate=candidate,
                previous_decision=previous,
            )
            continue

        if str(previous.get("status") or "").upper() == "RECHECK_REQUIRED":
            prior = previous.get("previous_decision")
            if (
                str(previous.get("reason") or "").upper()
                == AUTOMATIC_IDENTITY_RECHECK_REASON
                and isinstance(prior, Mapping)
                and _pre_aither_identity_recheck(prior)
            ):
                # This survives a crash after quarantine but before resolution.
                # The validated prior decision never reached Aither, so this is
                # still the release's first and only allowed search.
                automatic_rechecks.add(key)
            else:
                # Old prompt-approved parser rechecks cannot prove that their
                # original Aither attempt never happened.  Freeze them without
                # offering an approval path or touching any provider.
                _record_legacy_no_repeat(
                    store,
                    key,
                    name,
                    parsed,
                    previous,
                )
            continue

        identity_recheck = _pre_aither_identity_recheck(previous)
        if (
            (not _is_legacy_record(previous) and not identity_recheck)
            or str(previous.get("status") or "").upper()
            in {"BLACKLISTED", "LEGACY_SEEN"}
        ):
            continue

        if _legacy_aither_decision_no_repeat(previous):
            # V1 did not persist enough request evidence to distinguish a
            # parser-only decision from a completed Aither check.  Unless the
            # durable matcher-cache replay above handled it, the request must
            # be considered spent regardless of IDs or old parser metadata.
            _record_legacy_no_repeat(
                store,
                key,
                name,
                parsed,
                previous,
            )
            continue

        if (
            _legacy_parser_suspect(previous)
            and not _pre_aither_identity_recheck(previous)
        ):
            # A parser bug makes the old decision unsafe, but does not prove
            # that Aither was never queried.  Human approval cannot make a
            # repeated request safe, so retire READY and require manual review.
            _record_legacy_no_repeat(
                store,
                key,
                name,
                parsed,
                previous,
            )
            continue

        # Only a proven pre-Aither UNRESOLVED decision can reach the automatic
        # identity recheck path below.
        files = qb.files(str(primary.get("hash") or ""))
        preflight_files[key] = files
        parsed = enrich_release(parsed, name, primary, files)
        blocked, blocked_group, rule, release_type = is_blacklisted_release(
            name, parsed
        )
        if blocked:
            record_terminal(
                store,
                key,
                name,
                parsed,
                CheckOutcome(
                    status="BLACKLISTED",
                    message=(
                        f"group={blocked_group}; rule={rule}; type={release_type}"
                    ),
                    reason="BLACKLIST_RULE",
                ),
                previous_decision=previous,
            )
            continue
        if classify_local_payload(primary, files) == "NOT_MEDIA":
            record_terminal(
                store,
                key,
                name,
                parsed,
                CheckOutcome(
                    status="NOT_MEDIA",
                    lookup="local_manifest",
                    message="A manifest bizonyítottan nem film/sorozat payload",
                    reason="MANIFEST_NOT_MEDIA",
                ),
                previous_decision=previous,
            )
            continue
        lost_aither = _legacy_aither_evidence_loss(previous)
        if lost_aither is not None:
            # The legacy checker called Aither but did not retain the response.
            # Preserve the known catalogue identity and require human review;
            # never repeat the forbidden Aither request just to reconstruct it.
            record_terminal(
                store,
                key,
                name,
                parsed,
                CheckOutcome(
                    status="MANUAL",
                    lookup=str(lost_aither.get("lookup") or ""),
                    tmdb_id=lost_aither.get("tmdb_id"),
                    imdb_id=normalize_imdb_id(lost_aither.get("imdb_id")),
                    message=(
                        "A korábbi Aither-válasz jelöltjei nem maradtak meg; "
                        "az ismételt keresés tiltott"
                    ),
                    reason="LEGACY_AITHER_EVIDENCE_NOT_PERSISTED",
                ),
                previous_decision=previous,
            )
            continue
        if _legacy_parser_suspect(previous) or identity_recheck:
            automatic_identity = _pre_aither_identity_recheck(previous)
            if automatic_identity:
                _quarantine_recheck(
                    store,
                    key,
                    name,
                    parsed,
                    previous,
                )
                # Legacy UNRESOLVED records never reached Aither, so resolving
                # them now is a first search rather than a forbidden repeat.
                automatic_rechecks.add(key)
            else:
                _record_legacy_no_repeat(
                    store,
                    key,
                    name,
                    parsed,
                    previous,
                )

    # No user approval may authorize a second Aither lookup.  Only the
    # automatically validated pre-Aither identity case can enter this set.
    approved_rechecks = automatic_rechecks

    summary = ScanSummary(total_groups=len(groups))
    for index, (key, group) in enumerate(sorted(groups.items()), start=1):
        existing = store.get_release(key)
        is_approved_recheck = key in approved_rechecks
        if (
            existing
            and str(existing.get("status") or "").upper() == "RECHECK_REQUIRED"
            and not is_approved_recheck
        ):
            summary.recheck_required += 1
            continue
        if store.is_terminal(key) and not is_approved_recheck:
            summary.already_terminal += 1
            continue

        previous_decision = None
        if is_approved_recheck and existing is not None:
            previous = existing.get("previous_decision")
            previous_decision = (
                previous if isinstance(previous, Mapping) else existing
            )

        primary = group[0]
        name = str(primary.get("name") or "")
        parsed = parse_release(name)
        parsed["torrent_hashes"] = _group_infohashes(group)
        blocked, blocked_group, rule, release_type = is_blacklisted_release(name, parsed)
        if blocked:
            outcome = CheckOutcome(
                "BLACKLISTED",
                message=f"group={blocked_group}; rule={rule}; type={release_type}",
            )
            record_terminal(store, key, name, parsed, outcome)
            append_event(log_file, "BLACKLISTED", name, outcome.message)
            summary.blacklisted += 1
            print(f"[{index}/{len(groups)}] BLACKLIST: {name}")
            continue

        print(f"[{index}/{len(groups)}] Ellenőrzés: {name}")
        try:
            files = preflight_files.get(key)
            if files is None:
                files = qb.files(str(primary.get("hash") or ""))
            parsed = enrich_release(parsed, name, primary, files)

            if _group_is_from_aither(qb, group):
                outcome = CheckOutcome(
                    status="DUPLICATE",
                    lookup="source_tracker",
                    message="A qBittorrent forrás-trackere Aither",
                    reason="SOURCE_TRACKER_AITHER",
                )
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    outcome,
                    previous_decision=previous_decision,
                )
                summary.duplicate += 1
                print("  Eredmény: DUPLICATE (source_tracker)")
                continue

            manifest_status = classify_local_payload(primary, files)
            if manifest_status == "NOT_MEDIA":
                outcome = CheckOutcome(
                    status="NOT_MEDIA",
                    lookup="local_manifest",
                    message="A manifest bizonyítottan nem film/sorozat payload",
                    reason="MANIFEST_NOT_MEDIA",
                )
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    outcome,
                    previous_decision=previous_decision,
                )
                summary.not_media += 1
                print("  Eredmény: NOT_MEDIA")
                continue

            # Manifest enrichment may reveal the true Full Disc/WEB-DL type;
            # apply type-sensitive blacklist rules again before providers.
            blocked, blocked_group, rule, release_type = is_blacklisted_release(
                name, parsed
            )
            if blocked:
                outcome = CheckOutcome(
                    status="BLACKLISTED",
                    message=(
                        f"group={blocked_group}; rule={rule}; type={release_type}"
                    ),
                    reason="BLACKLIST_RULE",
                )
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    outcome,
                    previous_decision=previous_decision,
                )
                summary.blacklisted += 1
                continue

            def mark_attempt(
                lookup: str,
                tmdb_id: int | None,
                imdb_id: str | None,
                attempt_nonce: str,
            ) -> None:
                record_terminal(
                    store,
                    key,
                    name,
                    parsed,
                    CheckOutcome(
                        "AITHER_ATTEMPTED",
                        lookup=lookup,
                        tmdb_id=tmdb_id,
                        imdb_id=imdb_id,
                        message="A HTTP-kérés előtti tartós marker",
                        reason="AITHER_HTTP_RESERVED",
                        attempt_nonce=attempt_nonce,
                    ),
                    previous_decision=previous_decision,
                )

            raw_hashes = [
                str(item.get("hash") or "").strip().casefold() for item in group
            ]
            torrent_hash = (
                raw_hashes[0]
                if raw_hashes
                and len(raw_hashes) == len(group)
                and len(set(raw_hashes)) == 1
                and re.fullmatch(r"[0-9a-f]{40}", raw_hashes[0]) is not None
                else None
            )
            identity_hint = verified_identity_hint(
                key,
                parsed,
                identity_overrides or {},
                torrent_hash=torrent_hash,
            )
            outcome = check_release(
                parsed,
                primary,
                files,
                tmdb,
                aither,
                imdb=imdb,
                before_aither_request=mark_attempt,
                identity_hint=identity_hint,
            )
        except (AitherError, ProviderError, QbitError) as exc:
            current = store.get_release(key)
            spent_aither_request = bool(
                current
                and str(current.get("status") or "").upper()
                == "AITHER_ATTEMPTED"
            )
            if spent_aither_request:
                # Once an Aither request was actually attempted, even a 429 or
                # network/schema error is terminal under the no-repeat
                # requirement.  Keep it in MANUAL rather than an apparent
                # retry/error queue, while retaining the attempt marker.
                spent_record: Mapping[str, Any] = (
                    current
                    if isinstance(current, Mapping)
                    else (previous_decision or {})
                )
                _record_spent_aither_failure(
                    store,
                    key,
                    name,
                    parsed,
                    spent_record,
                    details=str(exc),
                )
                append_event(
                    log_file,
                    "AITHER ATTEMPT FAILED - MANUAL",
                    name,
                    str(exc),
                )
            else:
                # Local qB/TMDb failures before the first Aither HTTP operation
                # must not falsely consume the release's one allowed search.
                append_event(log_file, "PRE_SEARCH_ERROR", name, str(exc))
            summary.errors += 1
            print(f"  HIBA: {exc}")
            # Stop immediately so an outage cannot consume or terminalize the backlog.
            break

        candidate = None
        pending_event: tuple[str, str] | None = None
        if outcome.status == "NOT_FOUND":
            if outcome.proven_not_found:
                candidate = candidate_record(key, name, group, files)
                summary.not_found += 1
                pending_event = ("NOT FOUND ON AITHER", outcome.reason)
            else:
                outcome = replace(
                    outcome,
                    status="MANUAL",
                    message="A hiány nem bizonyított teljes Aither-válaszból",
                    reason="UNPROVEN_NOT_FOUND",
                )
                summary.manual += 1
        elif outcome.status == "DUPLICATE":
            summary.duplicate += 1
        elif outcome.status == "UNRESOLVED":
            summary.unresolved += 1
            pending_event = ("UNRESOLVED", outcome.message)
        elif outcome.status == "MANUAL":
            summary.manual += 1
            pending_event = ("MANUAL REVIEW", outcome.message)
        elif outcome.status == "NOT_MEDIA":
            summary.not_media += 1

        record_terminal(
            store,
            key,
            name,
            parsed,
            outcome,
            candidate=candidate,
            previous_decision=previous_decision,
        )
        if pending_event is not None:
            append_event(log_file, pending_event[0], name, pending_event[1])
        print(f"  Eredmény: {outcome.status}")

    current_counts: dict[str, int] = defaultdict(int)
    for key in groups:
        record = store.get_release(key)
        status = str((record or {}).get("status") or "UNPROCESSED").upper()
        current_counts[status] += 1
    summary.state_counts = dict(sorted(current_counts.items()))
    summary.live_keys = tuple(sorted(groups))
    return summary


def run_media_review(
    store: StateStore,
    qb: QbitClient,
    config: Config,
) -> Any:
    """Import the destructive subsystem only after the read-only scan completed."""

    from media_prepare import review_and_process

    return review_and_process(
        store=store,
        qbit_client=qb,
        source_roots=config.source_roots,
        upload_root=config.upload_root,
        ready_list_file=config.ready_list_file,
    )


def print_summary(summary: ScanSummary, store: StateStore, ready_file: Path) -> None:
    counts = summary.state_counts

    def count(*statuses: str) -> int:
        return sum(counts.get(status, 0) for status in statuses)

    print("\n=== Aither összesítés ===")
    print(f"qB release-csoportok: {summary.total_groups}")
    print(f"korábban lezárt: {summary.already_terminal}")
    print(f"blacklist: {count('BLACKLISTED')}")
    print(f"dupe: {count('DUPLICATE', 'FOUND')}")
    print(f"nem található: {count('NOT_FOUND', 'MISSING')}")
    print(f"feloldatlan: {count('UNRESOLVED')}")
    print(f"manuális: {count('MANUAL')}")
    print(f"nem média: {count('NOT_MEDIA')}")
    print(f"újraellenőrzésre vár: {count('RECHECK_REQUIRED')}")
    print(f"hiba: {count('API_ERROR', 'ERROR', 'AITHER_ATTEMPTED')}")
    pending = sum(
        1
        for key in summary.live_keys
        if (
            (record := store.get_release(key)) is not None
            and str(record.get("status") or "").upper() == "NOT_FOUND"
            and str((record.get("processing") or {}).get("status", "")).upper()
            == "PENDING"
        )
    )
    print(f"feldolgozásra váró RAR/ISO: {pending}")
    print(f"kész lista: {ready_file}")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        print("Használat: aither", file=sys.stderr)
        return 2

    os.umask(0o077)
    from dotenv import load_dotenv

    load_dotenv(Path.home() / ".aithercheck.env")
    try:
        config = Config.from_environment()
        with SingleProcessLock(config.lock_file):
            # StateStore may migrate and atomically rewrite a legacy file in
            # its constructor, so the process lock must already be held.
            store = StateStore(config.state_file)
            try:
                identity_overrides = load_identity_overrides(
                    config.identity_overrides_file
                )
            except IdentityOverrideError as exc:
                raise ConfigurationError(str(exc)) from exc
            rate_gate = AitherRateGate(
                store,
                batch_size=10,
                normal_delay=config.request_interval,
                batch_delay=config.batch_pause,
                request_timeout=config.request_timeout,
            )
            qb = QbitClient(
                config.qb_url,
                config.qb_user,
                config.qb_pass,
                timeout=config.request_timeout,
            )
            tmdb = TmdbClient(config.tmdb_api_key, timeout=config.request_timeout)
            imdb = IMDbSuggestionClient(timeout=config.request_timeout)
            aither = AitherClient(
                config.aither_base,
                config.aither_token,
                rate_gate,
                timeout=config.request_timeout,
                store=store,
            )
            qb.login()
            summary = scan_completed(
                qb,
                tmdb,
                aither,
                store,
                config.log_file,
                imdb=imdb,
                repair_input=input,
                identity_overrides=identity_overrides,
            )
            run_media_review(store, qb, config)
            print_summary(summary, store, config.ready_list_file)
        return 1 if summary.errors else 0
    except AlreadyRunningError:
        print("Egy másik aither folyamat már fut.", file=sys.stderr)
        return 1
    except (CheckerError, StateError, OSError) as exc:
        print(f"HIBA: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nMegszakítva; nem történt automatikus jóváhagyás.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
